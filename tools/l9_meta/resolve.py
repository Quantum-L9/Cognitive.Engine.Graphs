"""Resolve a repo-relative path to the `MetaRecord` it should carry.

Precedence, most specific last: `defaults` -> `rules` (last match wins) ->
`overrides[exact path]`. Rules express intent for a directory; overrides handle
the files whose directory says nothing useful about them (repo root, mixed-layer
packages).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from tools.l9_meta.config import Config, Vocabulary
from tools.l9_meta.model import MetaRecord, as_tuple


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern to a regex.

    `**` crosses path separators, `*` and `?` do not. A pattern ending in `/`
    matches everything beneath that directory. A pattern with no `/` at all
    matches on basename, so `*.md` behaves as expected.
    """
    pat = pattern
    if pat.endswith("/"):
        pat += "**"
    anchored_to_basename = "/" not in pat

    out = ["(?:.*/)?"] if anchored_to_basename else []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat.startswith("**/", i):
                out.append("(?:[^/]+/)*")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(pattern: str, rel_path: str) -> bool:
    """True when `rel_path` (posix, repo-relative) matches `pattern`."""
    return _compiled(pattern).search(rel_path) is not None


def resolve_fields(config: Config, rel_path: str) -> dict[str, Any]:
    """Merged field values for `rel_path`, before validation."""
    values: dict[str, Any] = dict(config.defaults)
    for rule in config.rules:
        if matches(rule.path, rel_path):
            values.update(rule.values)
    override = config.overrides.get(rel_path)
    if override:
        values.update(override)
    return values


def order_tags(config: Config, tags: tuple[str, ...]) -> tuple[str, ...]:
    """Sort tags by facet, then alphabetically inside each facet.

    Ordering has to be a pure function of the tag set, or every `apply` run
    produces a different byte sequence for the same metadata and the diff never
    settles. Tags outside the vocabulary sort last so they stay visible.
    """
    vocab = config.vocabulary
    rank = {
        **dict.fromkeys(vocab.tag_family, 0),
        **dict.fromkeys(vocab.tag_capability, 1),
        **dict.fromkeys(vocab.tag_concern, 2),
    }
    unique = dict.fromkeys(tags)
    return tuple(sorted(unique, key=lambda t: (rank.get(t, 3), t)))


def resolve(config: Config, rel_path: str) -> MetaRecord:
    """The record `rel_path` should carry according to config."""
    values = resolve_fields(config, rel_path)
    return MetaRecord(
        origin=str(values["origin"]),
        engine=config.engine,
        layer=as_tuple(values.get("layer")),
        tags=order_tags(config, as_tuple(values.get("tags"))),
        status=str(values.get("status", "active")),
    )


def validate(config: Config, record: MetaRecord, rel_path: str) -> list[str]:
    """Vocabulary violations for `record`. Empty list means valid."""
    vocab = config.vocabulary
    problems: list[str] = []

    if vocab.origin and record.origin not in vocab.origin:
        problems.append(f"origin '{record.origin}' not in vocabulary")
    if vocab.status and record.status not in vocab.status:
        problems.append(f"status '{record.status}' not in vocabulary")
    if vocab.layer:
        for layer in record.layer:
            if layer not in vocab.layer:
                problems.append(f"layer '{layer}' not in vocabulary")
    if not record.layer:
        problems.append("layer is empty")

    if vocab.constrains_tags:
        for tag in record.tags:
            if tag not in vocab.all_tags:
                problems.append(f"tag '{tag}' not in vocabulary")
        problems.extend(_facet_problems(vocab, record.tags))
        if record.tags != order_tags(config, record.tags):
            want = ", ".join(order_tags(config, record.tags))
            problems.append(f"tags not in facet order (expected [{want}])")
    if len(record.tags) > vocab.max_tags:
        problems.append(f"{len(record.tags)} tags exceeds max_tags={vocab.max_tags}")

    return [f"{rel_path}: {p}" for p in problems]


def _facet_problems(vocab: Vocabulary, tags: tuple[str, ...]) -> list[str]:
    """Cardinality violations per facet.

    Exactly one family, so every file is classified and no file claims two
    product surfaces. Capability and concern are optional but bounded — an
    unbounded facet degenerates back into the free-text tag soup this replaces.
    """
    problems: list[str] = []
    counts = {
        "family": [t for t in tags if t in set(vocab.tag_family)],
        "capability": [t for t in tags if t in set(vocab.tag_capability)],
        "concern": [t for t in tags if t in set(vocab.tag_concern)],
    }
    if len(counts["family"]) != 1:
        found = counts["family"] or "none"
        problems.append(f"expected exactly one family tag, found {found}")
    for facet in ("capability", "concern"):
        limit = vocab.max_per_facet.get(facet, 0)
        if limit and len(counts[facet]) > limit:
            problems.append(f"{len(counts[facet])} {facet} tags exceeds limit {limit}: {counts[facet]}")
    return problems
