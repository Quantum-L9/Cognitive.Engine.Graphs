"""CEG-009 — every domain under domains/ must be in the shape the loader reads.

``DomainPackLoader`` resolves ``domains/<domain_id>/spec.yaml``
(``loader.SPEC_FILENAME``). Nine specs used a flat
``<name>_domain_spec.yaml`` convention instead and were therefore unreachable:
they looked like available verticals in a directory listing, but a tenant id
matching one of them failed to resolve. Only ``plasticos`` loaded by default.

Two of those nine were additionally invalid — ``executive-assistant`` declared
an edge to an undeclared ``Skill`` node, ``roofing-company`` one to ``ZipCode``
— and nothing caught it, because a file the loader never opens is never
validated either.

Readable is not the same as correct, and statically correct is not the same
as proven: ``_cypher_defects`` below is the static half, and
``tests/integration/test_domain_packs_neo4j.py`` is the live Neo4j 5 witness
a pack needs before it leaves ``unvalidated_domain_packs_enabled``
(CEG-009d). The loader keeps the two ledgers — ``STATIC_DEFECT_PACKS`` and
``WITNESS_PENDING_PACKS`` — and this module holds each to its reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from engine.config.loader import (
    SPEC_FILENAME,
    STATIC_DEFECT_PACKS,
    WITNESS_PENDING_PACKS,
    DomainPackLoader,
)
from engine.config.schema import DomainSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAINS_ROOT = REPO_ROOT / "domains"


def _domain_dirs() -> list[Path]:
    return sorted(p for p in DOMAINS_ROOT.iterdir() if p.is_dir())


def _load(domain_id: str) -> DomainSpec:
    raw = yaml.safe_load((DOMAINS_ROOT / domain_id / SPEC_FILENAME).read_text(encoding="utf-8"))
    return DomainSpec.model_validate(raw)


def test_no_flat_domain_spec_files_remain() -> None:
    stragglers = sorted(p.name for p in DOMAINS_ROOT.glob("*_domain_spec.yaml"))
    assert stragglers == [], (
        f"{stragglers} use the flat shape the loader never reads. Move each to "
        f"domains/<domain_id>/{SPEC_FILENAME}, or delete it if superseded."
    )


def test_every_domain_directory_holds_a_spec() -> None:
    missing = [d.name for d in _domain_dirs() if not (d / SPEC_FILENAME).is_file()]
    assert missing == [], f"domain directories without {SPEC_FILENAME}: {missing}"


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_domain_spec_validates(domain_dir: Path) -> None:
    """A spec the loader can find is a spec that must survive validation."""
    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    DomainSpec.model_validate(raw)


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_directory_name_matches_declared_domain_id(domain_dir: Path) -> None:
    """The loader resolves by domain id, so the directory name IS the lookup key."""
    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    assert raw["domain"]["id"] == domain_dir.name


def test_loader_discovers_every_unflagged_domain() -> None:
    from engine.config.loader import _DOMAIN_FEATURE_FLAGS

    loader = DomainPackLoader(str(DOMAINS_ROOT))
    discovered = set(loader.list_domains())
    on_disk = {d.name for d in _domain_dirs()}
    assert discovered <= on_disk
    # Everything withheld is withheld deliberately, by a declared flag.
    assert on_disk - discovered <= set(_DOMAIN_FEATURE_FLAGS)


def test_every_discovered_domain_actually_loads() -> None:
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    for domain_id in sorted(loader.list_domains()):
        assert loader.load_domain(domain_id).domain.id == domain_id


# --------------------------------------------------------------------------
# Readable is not the same as correct.
# --------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Every `$…` token in a compiled fragment, up to the next delimiter. The
# capture is deliberately loose so a malformed name (`$85.0`) is captured and
# reported rather than skipped.
_PARAM_TOKEN = re.compile(r"\$([^\s(),}\]]*)")


def _cypher_defects(spec: DomainSpec) -> list[str]:
    """Gate compilations that would fail, or that no parameter could ever satisfy.

    A spec that validates against ``DomainSpec`` has proven it parses. A gate
    that compiles has proven it emits a fragment. Neither proves the fragment
    can execute: the match handler supplies exactly the query-schema fields,
    the derived parameters and whatever the compiler bound itself
    (``GateCompiler.last_query_params``), so every ``$name`` the fragment
    references must come from one of those three or Neo4j answers
    ``ParameterMissing`` at execution time.

    Shapes this has caught, in the order they were found:

    * ``$<non-identifier>`` — a scalar ``queryparam`` (``85.0``) stringified
      into a parameter name. Bound as a literal since CEG-009b.
    * ``RELATES_TO`` — the former fallback relationship type for a traversal
      gate without ``edgetype``; no ontology declares it, so the gate rejected
      every candidate. Such a gate is refused outright since CEG-009a.
    * ``$True`` / ``$False`` — ``queryparam: true`` stringified; a valid
      identifier, so the earlier name check let it through.
    * ``$value`` — the placeholder the range compiler emitted when it ignored
      a declared ``queryparam_min`` / ``queryparam_max``.
    * a declared name that is in no query schema, which nothing supplies.
    """
    from engine.gates.compiler import GateCompiler

    compiler = GateCompiler(spec)
    declared = {f.name for f in spec.queryschema.fields} | {p.name for p in spec.derivedparameters}
    defects: list[str] = []
    for gate in spec.gates:
        try:
            cypher = compiler.compile(gate)
        except (ValueError, KeyError) as exc:
            defects.append(f"{gate.name}: refused by the compiler — {exc}")
            continue
        bound = set(compiler.last_query_params)
        for param in _PARAM_TOKEN.findall(cypher):
            if not _IDENTIFIER.fullmatch(param):
                defects.append(f"{gate.name}: '${param}' is not a parameter name — {cypher}")
            elif param not in declared and param not in bound:
                defects.append(
                    f"{gate.name}: '${param}' is neither a queryschema field, a derived parameter, "
                    f"nor bound by the compiler — {cypher}"
                )
    return defects


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_discoverable_domain_gates_compile_to_executable_cypher(domain_dir: Path) -> None:
    """A pack the loader will serve must produce Cypher that can run.

    The gap this closes: validating a spec against DomainSpec proves it parses,
    not that its gates execute. Seven packs passed Pydantic validation and still
    compiled to `exists((candidate)-[:RELATES_TO]->(t))`, `$85.0` or `$True`.

    A pack behind a feature flag is exempt — that is what the flag records.
    """
    from engine.config.loader import _DOMAIN_FEATURE_FLAGS

    if domain_dir.name in _DOMAIN_FEATURE_FLAGS:
        pytest.skip(f"{domain_dir.name} is flag-gated: {_DOMAIN_FEATURE_FLAGS[domain_dir.name]}")

    defects = _cypher_defects(_load(domain_dir.name))
    assert defects == [], f"{domain_dir.name} would serve unexecutable Cypher:\n  " + "\n  ".join(defects)


# --------------------------------------------------------------------------
# The two reasons a pack sits behind `unvalidated_domain_packs_enabled`, each
# held to itself so the flag can never become a place defects go to be
# forgotten.
# --------------------------------------------------------------------------


def test_unvalidated_flag_is_exactly_the_two_ledgers() -> None:
    from engine.config.loader import _DOMAIN_FEATURE_FLAGS

    gated = {name for name, flag in _DOMAIN_FEATURE_FLAGS.items() if flag == "unvalidated_domain_packs_enabled"}
    assert gated == set(STATIC_DEFECT_PACKS) | set(WITNESS_PENDING_PACKS)
    assert not set(STATIC_DEFECT_PACKS) & set(WITNESS_PENDING_PACKS), "a pack is on both ledgers"
    assert gated, "no pack is gated as unvalidated; drop the flag and these tests"
    for name in sorted(gated):
        assert (DOMAINS_ROOT / name / SPEC_FILENAME).is_file(), f"{name} is gated but has no spec on disk"


@pytest.mark.parametrize("name", sorted(WITNESS_PENDING_PACKS))
def test_witness_pending_pack_has_no_static_defect(name: str) -> None:
    """A pack waiting on the Neo4j 5 witness owes nothing to the static check.

    If one of these regresses, the static reason has come back and the pack
    belongs on ``STATIC_DEFECT_PACKS`` with that reason written down.
    """
    defects = _cypher_defects(_load(name))
    assert defects == [], f"{name} is listed as witness-pending but has static defects:\n  " + "\n  ".join(defects)


def test_static_defect_packs_still_have_the_defect_on_record() -> None:
    """Shrink-only: when a statically defective pack starts compiling, move it on.

    Without this, a pack fixed later stays invisible behind the flag. The ledger
    may be empty (it is, since mortgage-brokerage was rewritten); an entry that
    exists must still be earning its place.
    """
    for name in sorted(STATIC_DEFECT_PACKS):
        assert STATIC_DEFECT_PACKS[name].strip(), f"{name} is on the static ledger with no reason recorded"
        defects = _cypher_defects(_load(name))
        assert defects, (
            f"{name} now compiles cleanly — move it from STATIC_DEFECT_PACKS to WITNESS_PENDING_PACKS "
            "so the Neo4j 5 witness is the only thing between it and the loader."
        )
