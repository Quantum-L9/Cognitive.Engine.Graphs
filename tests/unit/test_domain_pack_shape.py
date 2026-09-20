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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from engine.config.loader import SPEC_FILENAME, DomainPackLoader
from engine.config.schema import DomainSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAINS_ROOT = REPO_ROOT / "domains"


def _domain_dirs() -> list[Path]:
    return sorted(p for p in DOMAINS_ROOT.iterdir() if p.is_dir())


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


def _cypher_defects(spec) -> list[str]:
    """Gate compilations that would fail or silently reject every candidate.

    Two shapes, both found only after CEG-009 made these packs readable:

    * ``RELATES_TO`` — the compiler's fallback when a ``type: traversal`` gate
      declares no ``edgetype``. Packs written with ``pattern``/``condition``
      hit it, and no ontology here declares ``RELATES_TO``, so the gate is a
      hard filter that matches nothing.
    * ``$<non-identifier>`` — a scalar ``queryparam`` (``85.0``, ``5``, ``1``).
      GateSpec coerces it to a string and the compiler used to emit it as a
      parameter *name*, so Cypher received ``$85.0``. Since C-009 validates
      parameter names like labels, the compiler now refuses the gate outright;
      that refusal is the same defect, reported at compile time.
    """
    from engine.gates.compiler import GateCompiler

    compiler = GateCompiler(spec)
    defects: list[str] = []
    for gate in spec.gates:
        try:
            cypher = compiler.compile(gate)
        except (ValueError, KeyError) as exc:
            defects.append(f"{gate.name}: refused by the compiler — {exc}")
            continue
        if "RELATES_TO" in cypher and not gate.edgetype:
            defects.append(f"{gate.name}: RELATES_TO fallback — {cypher}")
        for param in re.findall(r"\$([^\s)]+)", cypher):
            if not _IDENTIFIER.fullmatch(param):
                defects.append(f"{gate.name}: '${param}' is not a parameter name — {cypher}")
    return defects


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_discoverable_domain_gates_compile_to_executable_cypher(domain_dir: Path) -> None:
    """A pack the loader will serve must produce Cypher that can run.

    The gap this closes: validating a spec against DomainSpec proves it parses,
    not that its gates execute. Five packs passed Pydantic validation and still
    compiled to `exists((candidate)-[:RELATES_TO]->(t))` or `$85.0`.

    A pack behind a feature flag is exempt — that is what the flag records.
    """
    from engine.config.loader import _DOMAIN_FEATURE_FLAGS

    if domain_dir.name in _DOMAIN_FEATURE_FLAGS:
        pytest.skip(f"{domain_dir.name} is flag-gated: {_DOMAIN_FEATURE_FLAGS[domain_dir.name]}")

    raw = yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8"))
    defects = _cypher_defects(DomainSpec.model_validate(raw))
    assert defects == [], f"{domain_dir.name} would serve unexecutable Cypher:\n  " + "\n  ".join(defects)


def test_flag_gated_packs_are_gated_for_a_reason_that_still_holds() -> None:
    """Shrink-only: when a dormant pack starts compiling, un-gate it.

    Without this, a pack fixed later stays invisible and the flag becomes a
    place defects go to be forgotten.
    """
    from engine.config.loader import _DOMAIN_FEATURE_FLAGS

    still_broken, now_clean = [], []
    for name, flag in _DOMAIN_FEATURE_FLAGS.items():
        if flag != "unvalidated_domain_packs_enabled":
            continue
        path = DOMAINS_ROOT / name / SPEC_FILENAME
        if not path.is_file():
            continue
        spec = DomainSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        (still_broken if _cypher_defects(spec) else now_clean).append(name)

    assert now_clean == [], (
        f"{now_clean} now compile cleanly — remove them from _DOMAIN_FEATURE_FLAGS so the loader serves them again."
    )
    assert still_broken, "no pack is gated as unvalidated; drop the flag and this test"
