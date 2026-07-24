"""Derive `l9-meta.yaml` from evidence already in the repo.

Two evidence sources, in priority order:

1. Headers present on disk — 375 files. These are ground truth: a human wrote
   them or a prior injector run did.
2. The legacy `FILE_REGISTRY` in `tools/l9_meta_injector.py` — 245 entries, of
   which 10 name paths that no longer exist.

Files covered by neither (~240) get values from path rules alone, which is why
the plan requires a human review pass before Phase 5 stamps them.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.l9_meta.discover import eligible_files
from tools.l9_meta.formats import for_path
from tools.l9_meta.model import MetaRecord

# A rule is only worth emitting if it covers at least this many files; below the
# floor an override is smaller and clearer than a rule.
MIN_GROUP_SIZE = 2

_REGISTRY_CALL = re.compile(r"FileMeta\((?P<args>.*?)\)\s*,\s*\n", re.DOTALL)


@dataclass
class Evidence:
    """What we know a file should carry, and where that knowledge came from."""

    rel_path: str
    origin: str
    layer: tuple[str, ...]
    tags: tuple[str, ...]
    status: str
    source: str  # "header" | "registry"


def read_header_evidence(root: Path, rel_paths: list[str]) -> dict[str, Evidence]:
    """Parse every on-disk header. Files without one are simply absent."""
    out: dict[str, Evidence] = {}
    for rel in rel_paths:
        fmt = for_path(rel)
        if fmt is None:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            record = fmt.parse(text)
        except Exception:
            continue
        if record is None:
            continue
        out[rel] = Evidence(
            rel_path=rel,
            origin=record.origin,
            layer=record.layer,
            tags=record.tags,
            status=record.status,
            source="header",
        )
    return out


def read_registry_evidence(injector_path: Path) -> dict[str, Evidence]:
    """Scrape `FILE_REGISTRY` from the legacy injector without importing it.

    Importing would execute the module and couple adoption to code we are about
    to delete; a positional-argument scrape is enough for a one-shot migration.
    """
    if not injector_path.is_file():
        return {}
    source = injector_path.read_text(encoding="utf-8")
    out: dict[str, Evidence] = {}
    for match in _REGISTRY_CALL.finditer(source):
        args = _split_args(match.group("args"))
        if len(args) < 4:
            continue
        try:
            rel = _literal_str(args[0])
            origin = _literal_str(args[1])
            layer = tuple(_literal_list(args[2]))
            tags = tuple(_literal_list(args[3]))
        except ValueError:
            continue
        status = "active"
        if len(args) >= 6:
            try:
                status = _literal_str(args[5])
            except ValueError:
                status = "active"
        out[rel] = Evidence(
            rel_path=rel,
            origin=origin,
            layer=layer,
            tags=tags,
            status=status,
            source="registry",
        )
    return out


def _split_args(raw: str) -> list[str]:
    """Split a call's argument text on top-level commas."""
    args: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in raw:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        args.append("".join(current))
    return [a.strip() for a in args if a.strip()]


def _literal_str(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    msg = f"not a string literal: {raw!r}"
    raise ValueError(msg)


def _literal_list(raw: str) -> list[str]:
    text = raw.strip()
    if not (text.startswith("[") and text.endswith("]")):
        msg = f"not a list literal: {raw!r}"
        raise ValueError(msg)
    return [_literal_str(part) for part in _split_args(text[1:-1])]


def merge_evidence(headers: dict[str, Evidence], registry: dict[str, Evidence]) -> dict[str, Evidence]:
    """Headers win over registry; registry fills gaps for files with no header."""
    merged = dict(registry)
    merged.update(headers)
    return merged


def group_key(rel_path: str) -> str:
    """The rule scope a path belongs to: its first two segments, or `<root>`."""
    parts = rel_path.split("/")
    if len(parts) == 1:
        return "<root>"
    if len(parts) == 2:
        return parts[0]
    return f"{parts[0]}/{parts[1]}"


def _majority(values: list[tuple[str, ...] | str]) -> Any:
    return Counter(values).most_common(1)[0][0]


@dataclass
class Derivation:
    """The generated config plus the numbers needed to judge it."""

    defaults: dict[str, Any]
    rules: list[dict[str, Any]]
    overrides: dict[str, dict[str, Any]]
    covered_by_rule: int
    covered_by_override: int
    unresolved: list[str]


def derive(evidence: dict[str, Evidence], all_files: list[str]) -> Derivation:
    """Collapse per-file evidence into rules plus the residue as overrides."""
    by_group: dict[str, list[Evidence]] = defaultdict(list)
    for ev in evidence.values():
        by_group[group_key(ev.rel_path)].append(ev)

    default_origin = _majority([ev.origin for ev in evidence.values()]) if evidence else "engine-specific"

    rules: list[dict[str, Any]] = []
    overrides: dict[str, dict[str, Any]] = {}
    covered_by_rule = 0

    for group in sorted(by_group):
        members = by_group[group]
        if group == "<root>" or len(members) < MIN_GROUP_SIZE:
            for ev in members:
                overrides[ev.rel_path] = _values(ev, default_origin)
            continue

        maj_layer = _majority([ev.layer for ev in members])
        maj_origin = _majority([ev.origin for ev in members])
        maj_tags = _majority([ev.tags for ev in members])
        maj_status = _majority([ev.status for ev in members])

        rule: dict[str, Any] = {"path": f"{group}/", "layer": list(maj_layer)}
        if maj_origin != default_origin:
            rule["origin"] = maj_origin
        if maj_tags:
            rule["tags"] = list(maj_tags)
        if maj_status != "active":
            rule["status"] = maj_status
        rules.append(rule)

        for ev in members:
            if ev.layer == maj_layer and ev.origin == maj_origin and ev.status == maj_status:
                covered_by_rule += 1
            else:
                values = _values(ev, maj_origin, maj_status)
                values.pop("tags", None)
                overrides[ev.rel_path] = values

    unresolved = sorted(set(all_files) - set(evidence))
    return Derivation(
        defaults={"origin": default_origin, "layer": ["engine"], "status": "active"},
        rules=rules,
        overrides=overrides,
        covered_by_rule=covered_by_rule,
        covered_by_override=len(overrides),
        unresolved=unresolved,
    )


def _values(ev: Evidence, inherited_origin: str, inherited_status: str = "active") -> dict[str, Any]:
    values: dict[str, Any] = {"layer": list(ev.layer)}
    if ev.origin != inherited_origin:
        values["origin"] = ev.origin
    if ev.tags:
        values["tags"] = list(ev.tags)
    if ev.status != inherited_status:
        values["status"] = ev.status
    return values


def collect(root: Path, excludes: list[str]) -> tuple[dict[str, Evidence], list[str]]:
    """Gather evidence for every eligible file under `root`."""
    files = eligible_files(root, excludes)
    eligible = set(files)
    headers = read_header_evidence(root, files)
    registry = read_registry_evidence(root / "tools" / "l9_meta_injector.py")
    # The registry names 10 paths that no longer exist and several under
    # `artifacts/`; neither should shape the rules we are about to generate.
    registry = {k: v for k, v in registry.items() if k in eligible}
    return merge_evidence(headers, registry), files


def header_records(root: Path, rel_paths: list[str]) -> dict[str, MetaRecord]:
    """Every on-disk header as a record, for the Phase 2 reproduction gate."""
    out: dict[str, MetaRecord] = {}
    for rel, ev in read_header_evidence(root, rel_paths).items():
        out[rel] = MetaRecord(
            origin=ev.origin,
            engine="graph",
            layer=ev.layer,
            tags=ev.tags,
            status=ev.status,
        )
    return out
