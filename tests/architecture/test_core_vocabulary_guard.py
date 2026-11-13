"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, architecture, domain-neutrality, vocabulary-guard]
owner: engine-team
status: active
--- /L9_META ---

Core vocabulary guard (ADR-112 Stage 3).

ADR-112 makes ``engine/`` and ``chassis/`` a domain-neutral core and names "a CI
guard that fails on reintroduced product vocabulary" as Stage 3. This is that
guard. It is a ratchet over two committed files beside this module:

* ``core_vocabulary_denylist.yaml``: the denied terms, each group with its
  provenance. Hand-maintained; no pack directory is read, here or at test time.
* ``core_vocabulary_baseline.yaml``: every occurrence that exists today, as
  ``file`` / ``term`` / ``count`` / ``reason``.

The scan must equal the baseline exactly. A new occurrence or a higher count
fails. A lower count, or an entry whose file no longer carries the term, also
fails and tells the author to lower or delete the entry, so the baseline can
only shrink.

Coverage (explicit, because partial coverage must not read as complete):

* Scanned: every ``*.py`` file under ``engine/`` and ``chassis/``. Per file the
  repository-relative path, every identifier (``NAME`` token), every string
  literal including docstrings and f-string text, and every comment.
* Not scanned: non-Python files in those trees (``engine/spec.yaml``, ``*.sql``,
  ``chassis/Dockerfile.chassis``) and everything outside the two trees.
* A file that cannot be read, parsed or tokenized fails the guard; it is never
  skipped. A core tree with no Python file fails the guard.

False-positive policy: a term is denied only in a precise spelling. The
``identifier`` mode is case-sensitive and whole-identifier; the ``name`` mode is
a case-insensitive substring whose ``-`` matches ``-``, ``_`` or nothing but
never a space, so product and pack names are caught as identifiers and slugs
while ordinary prose is not. Words that are also plain English or generic graph
words are listed under ``not_denied`` in the deny list with the reason.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DENYLIST_PATH = HERE / "core_vocabulary_denylist.yaml"
BASELINE_PATH = HERE / "core_vocabulary_baseline.yaml"
CORE_TREES = ("engine", "chassis")

DENYLIST_SCHEMA = "l9.ceg.core_vocabulary_denylist.v1"
BASELINE_SCHEMA = "l9.ceg.core_vocabulary_baseline.v1"
MODES = ("identifier", "name")

_IDENT = "A-Za-z0-9_"
# String-bearing token types. FSTRING_MIDDLE / TSTRING_MIDDLE exist from Python
# 3.12 / 3.14; before that an f-string is one STRING token.
_TEXT_TOKENS = frozenset(
    kind
    for kind in (
        tokenize.NAME,
        tokenize.STRING,
        tokenize.COMMENT,
        getattr(tokenize, "FSTRING_MIDDLE", None),
        getattr(tokenize, "TSTRING_MIDDLE", None),
    )
    if kind is not None
)

Occurrences = dict[tuple[str, str], int]


class GuardError(Exception):
    """The guard could not evaluate its subject or its own inputs."""


@dataclass(frozen=True)
class Term:
    term: str
    mode: str
    group: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class BaselineEntry:
    file: str
    term: str
    count: int
    reason: str


def compile_term(term: str, mode: str) -> re.Pattern[str]:
    if mode == "identifier":
        return re.compile(rf"(?<![{_IDENT}]){re.escape(term)}(?![{_IDENT}])")
    if mode == "name":
        return re.compile("[-_]?".join(re.escape(part) for part in term.split("-")), re.IGNORECASE)
    msg = f"unknown matching mode {mode!r} for term {term!r}"
    raise GuardError(msg)


def _mapping(path: Path, schema: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        msg = f"{path.name}: unreadable ({exc})"
        raise GuardError(msg) from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        msg = f"{path.name}: expected a mapping with schema {schema!r}"
        raise GuardError(msg)
    return data


def load_denylist(path: Path = DENYLIST_PATH) -> list[Term]:
    data = _mapping(path, DENYLIST_SCHEMA)
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        msg = f"{path.name}: `groups` must be a non-empty list"
        raise GuardError(msg)
    terms: list[Term] = []
    for group in groups:
        group_id, mode, provenance, names = (group.get(key) for key in ("id", "mode", "provenance", "terms"))
        if not isinstance(group_id, str) or not group_id:
            msg = f"{path.name}: a group has no id"
            raise GuardError(msg)
        if not isinstance(provenance, str) or not provenance.strip():
            msg = f"{path.name}: group {group_id!r} has no provenance note"
            raise GuardError(msg)
        if not isinstance(names, list) or not names or not all(isinstance(name, str) and name for name in names):
            msg = f"{path.name}: group {group_id!r} needs a non-empty list of string terms"
            raise GuardError(msg)
        terms.extend(Term(name, str(mode), group_id, compile_term(name, str(mode))) for name in names)
    duplicates = sorted(name for name, seen in Counter(term.term for term in terms).items() if seen > 1)
    if duplicates:
        msg = f"{path.name}: duplicate terms {duplicates}"
        raise GuardError(msg)
    return terms


def load_baseline(path: Path = BASELINE_PATH) -> list[BaselineEntry]:
    data = _mapping(path, BASELINE_SCHEMA)
    entries = data.get("entries")
    if not isinstance(entries, list):
        msg = f"{path.name}: `entries` must be a list"
        raise GuardError(msg)
    out: list[BaselineEntry] = []
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != {"file", "term", "count", "reason"}:
            msg = f"{path.name}: every entry has exactly file, term, count, reason; got {raw!r}"
            raise GuardError(msg)
        out.append(BaselineEntry(raw["file"], raw["term"], raw["count"], raw["reason"]))
    return out


def _texts(relative: str, source: str) -> Iterator[str]:
    """Everything in one source file a term may occur in: path, identifiers, strings, comments."""
    yield relative
    try:
        ast.parse(source, filename=relative)
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in _TEXT_TOKENS:
                yield token.string
    except (SyntaxError, tokenize.TokenError, ValueError) as exc:
        msg = f"{relative}: cannot be parsed, so it cannot be cleared ({exc})"
        raise GuardError(msg) from exc


def scan_source(relative: str, source: str, terms: Iterable[Term]) -> Occurrences:
    found: Counter[tuple[str, str]] = Counter()
    wanted = list(terms)
    for text in _texts(relative, source):
        for term in wanted:
            hits = len(term.pattern.findall(text))
            if hits:
                found[(relative, term.term)] += hits
    return dict(found)


def scan_core(root: Path, terms: Iterable[Term], trees: tuple[str, ...] = CORE_TREES) -> Occurrences:
    """Occurrences of every denied term in the Python source of the core trees under ``root``."""
    wanted = list(terms)
    found: Occurrences = {}
    for tree in trees:
        paths = sorted((root / tree).rglob("*.py"))
        if not paths:
            msg = f"{tree}/: no Python source found under {root}; an empty scan proves nothing"
            raise GuardError(msg)
        for path in paths:
            relative = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                msg = f"{relative}: unreadable, so it cannot be cleared ({exc})"
                raise GuardError(msg) from exc
            found.update(scan_source(relative, source, wanted))
    return found


def compare(found: Occurrences, baseline: Iterable[BaselineEntry]) -> list[str]:
    """Every way the scan differs from the baseline. Empty means they agree exactly."""
    allowed = {(entry.file, entry.term): entry.count for entry in baseline}
    problems: list[str] = []
    for key in sorted(set(found) | set(allowed)):
        file, term = key
        now, was = found.get(key, 0), allowed.get(key, 0)
        if now > was and was == 0:
            problems.append(
                f"NEW {file}: {term!r} occurs {now} time(s). Product vocabulary must not enter the "
                "domain-neutral core (ADR-112); take it from the domain spec instead."
            )
        elif now > was:
            problems.append(
                f"RAISED {file}: {term!r} occurs {now} time(s), baseline allows {was}. "
                "The baseline only shrinks; remove the new occurrence."
            )
        elif now == 0:
            problems.append(
                f"STALE {file}: baseline lists {term!r} x{was} but the file no longer contains it. "
                "Delete that entry from core_vocabulary_baseline.yaml."
            )
        elif now < was:
            problems.append(
                f"LOWERED {file}: {term!r} occurs {now} time(s), baseline says {was}. "
                f"Lower that entry's count to {now} in core_vocabulary_baseline.yaml."
            )
    return problems


# ── The committed inputs are well formed ────────────────────────────────────


def test_denylist_terms_have_provenance_and_match_themselves() -> None:
    terms = load_denylist()
    assert {term.mode for term in terms} <= set(MODES)
    for term in terms:
        spelled = term.term.replace("-", "_") if term.mode == "name" else term.term
        assert term.pattern.search(spelled), f"{term.term!r} does not match its own spelling"
        assert len(term.term) >= 4, f"{term.term!r} is too short to be a precise term"


def test_matching_modes_keep_ordinary_words_out() -> None:
    """The scoping rules: what each mode catches and, as important, what it does not."""
    terms = {term.term: term.pattern for term in load_denylist()}

    facility = terms["Facility"]
    assert facility.search("MATCH (f:Facility)")
    assert facility.search("# Facility fallback removed")
    for clean in ("facility", "a facility role", "Facilityx", "SubFacility", "Facility_node", "FACILITY"):
        assert not facility.search(clean), clean

    assert terms["community_id"].search("candidate.community_id")
    assert not terms["community_id"].search("outcome_community_identifier")
    assert not terms["RESULTED_IN"].search("NOT_RESULTED_IN_X")

    pack = terms["idea-portfolio"]
    for hit in ("idea-portfolio", "idea_portfolio", "IdeaPortfolioHydrator", "engine/sync/idea_portfolio.py"):
        assert pack.search(hit), hit
    assert not pack.search("an idea portfolio"), "prose with a space is not a slug"

    assert terms["odoo"].search("ODOO_INPUT_SCHEMA")
    assert terms["plasticos"].search("a PlasticOS convention")
    assert not terms["material-grade"].search("key material, graded")
    assert not terms["melt-flow"].search("melt flow")


def test_baseline_is_well_formed() -> None:
    known = {term.term for term in load_denylist()}
    entries = load_baseline()
    keys = [(entry.file, entry.term) for entry in entries]
    assert keys == sorted(keys), "baseline entries are sorted by (file, term)"
    assert len(set(keys)) == len(keys), "duplicate baseline entry"
    for entry in entries:
        where = f"{entry.file} / {entry.term}"
        assert entry.term in known, f"{where}: term is not in the deny list"
        assert entry.file.split("/", 1)[0] in CORE_TREES, f"{where}: outside the core trees"
        assert entry.file.endswith(".py"), where
        assert isinstance(entry.count, int), where
        assert not isinstance(entry.count, bool), where
        assert entry.count >= 1, f"{where}: count must be a positive integer"
        assert isinstance(entry.reason, str), where
        assert entry.reason.strip(), f"{where}: needs a reason"
        assert "\n" not in entry.reason.strip(), f"{where}: the reason is one line"


# ── The guard ───────────────────────────────────────────────────────────────


def test_core_vocabulary_matches_baseline() -> None:
    """The ratchet: the core's product vocabulary is exactly the committed baseline."""
    problems = compare(scan_core(ROOT, load_denylist()), load_baseline())
    assert problems == [], "core vocabulary guard (ADR-112 Stage 3):\n" + "\n".join(problems)


def test_baseline_is_complete() -> None:
    """Scanned against an empty baseline, the tree reports the committed baseline and nothing else."""
    found = scan_core(ROOT, load_denylist())
    reported = compare(found, [])
    assert all(line.startswith("NEW ") for line in reported)
    committed = {(entry.file, entry.term): entry.count for entry in load_baseline()}
    assert found == committed, "an occurrence on this tree is not accounted for by the baseline, or the reverse"
    assert len(reported) == len(committed)


def test_token_scan_agrees_with_raw_text() -> None:
    """Independent count: a plain regex over each file's whole text finds what the token scan finds.

    A term cannot span two tokens, so the two methods must agree; a disagreement
    means the token scan dropped a kind of text.
    """
    terms = load_denylist()
    raw: Occurrences = {}
    for tree in CORE_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            text = relative + "\n" + path.read_text(encoding="utf-8")
            for term in terms:
                hits = len(term.pattern.findall(text))
                if hits:
                    raw[(relative, term.term)] = hits
    assert scan_core(ROOT, terms) == raw


def test_every_core_python_file_is_scanned() -> None:
    """Coverage witness: a term in the path of each core file is found, so no file is skipped."""
    for tree in CORE_TREES:
        probe = [Term(tree, "name", "probe", compile_term(tree, "name"))]
        files = {path.relative_to(ROOT).as_posix() for path in (ROOT / tree).rglob("*.py")}
        scanned = {file for file, _ in scan_core(ROOT, probe, trees=(tree,))}
        assert files, tree
        assert scanned == files, tree


# ── Negative witnesses ──────────────────────────────────────────────────────

_CLEAN = '"""A neutral module."""\n\n\ndef resolve(label: str) -> str:\n    return label\n'


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for tree in CORE_TREES:
        (tmp_path / tree).mkdir()
        (tmp_path / tree / "__init__.py").write_text(_CLEAN, encoding="utf-8")
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("relative", "source", "term"),
    [
        ("engine/a.py", "Facility = 1\n", "Facility"),
        ("engine/a.py", "def run(node):\n    return node.community_id\n", "community_id"),
        ("engine/a.py", 'LABEL = "TransactionOutcome"\n', "TransactionOutcome"),
        ("engine/a.py", 'def q(x):\n    return f"MATCH (a)-[:RESULTED_IN]->({x})"\n', "RESULTED_IN"),
        ("engine/a.py", "X = 1  # same as plasticos\n", "plasticos"),
        ("engine/a.py", '"""Hydrates the IdeaOS projection."""\n', "ideaos"),
        ("chassis/a.py", "from x import closed_won\n", "closed-won"),
        ("chassis/deep/pkg/a.py", "def infer_facility_tier_from_capacity():\n    return None\n", "facility-tier"),
        ("engine/sync/freight_matching.py", _CLEAN, "freight-matching"),
    ],
    ids=[
        "identifier",
        "attribute",
        "string",
        "fstring",
        "comment",
        "docstring",
        "import-in-chassis",
        "nested-def",
        "file-name",
    ],
)
def test_planted_term_is_caught(tmp_path: Path, relative: str, source: str, term: str) -> None:
    root = _tree(tmp_path, {relative: source})
    found = scan_core(root, load_denylist())
    assert found == {(relative, term): 1}
    problems = compare(found, [])
    assert len(problems) == 1
    assert problems[0].startswith(f"NEW {relative}: {term!r}")


def test_clean_tree_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"engine/a.py": _CLEAN, "chassis/b.py": "# a facility, key material, an idea portfolio\n"})
    assert compare(scan_core(root, load_denylist()), []) == []


def test_raised_count_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"engine/a.py": 'A = "Facility"\nB = "Facility"\n'})
    baseline = [BaselineEntry("engine/a.py", "Facility", 1, "one was there")]
    problems = compare(scan_core(root, load_denylist()), baseline)
    assert len(problems) == 1
    assert problems[0].startswith("RAISED engine/a.py: 'Facility' occurs 2 time(s), baseline allows 1")


def test_lowered_count_fails_and_says_to_lower_the_baseline(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"engine/a.py": 'A = "Facility"\n'})
    baseline = [BaselineEntry("engine/a.py", "Facility", 3, "three were there")]
    problems = compare(scan_core(root, load_denylist()), baseline)
    assert len(problems) == 1
    assert problems[0].startswith("LOWERED engine/a.py")
    assert "Lower that entry's count to 1" in problems[0]


def test_stale_baseline_entry_is_reported(tmp_path: Path) -> None:
    """A baseline entry for a file that no longer contains the term, or no longer exists."""
    root = _tree(tmp_path, {"engine/a.py": _CLEAN})
    baseline = [
        BaselineEntry("engine/a.py", "Facility", 2, "cleaned since"),
        BaselineEntry("engine/gone.py", "plasticos", 1, "file deleted since"),
    ]
    problems = compare(scan_core(root, load_denylist()), baseline)
    assert [line.split(":")[0] for line in problems] == ["STALE engine/a.py", "STALE engine/gone.py"]
    assert all("Delete that entry" in line for line in problems)


def test_unparseable_core_file_fails_closed(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"engine/a.py": "def broken(:\n"})
    with pytest.raises(GuardError, match=r"engine/a\.py: cannot be parsed"):
        scan_core(root, load_denylist())


def test_undecodable_core_file_fails_closed(tmp_path: Path) -> None:
    root = _tree(tmp_path, {})
    (root / "chassis" / "a.py").write_bytes(b"X = '\xff\xfe'\n")
    with pytest.raises(GuardError, match=r"chassis/a\.py: unreadable"):
        scan_core(root, load_denylist())


def test_empty_core_tree_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "a.py").write_text(_CLEAN, encoding="utf-8")
    with pytest.raises(GuardError, match=r"chassis/: no Python source found"):
        scan_core(tmp_path, load_denylist())


def test_denylist_without_provenance_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "denylist.yaml"
    path.write_text(
        f"schema: {DENYLIST_SCHEMA}\ngroups:\n  - id: g\n    mode: name\n    terms: [plasticos]\n", encoding="utf-8"
    )
    with pytest.raises(GuardError, match="no provenance note"):
        load_denylist(path)
