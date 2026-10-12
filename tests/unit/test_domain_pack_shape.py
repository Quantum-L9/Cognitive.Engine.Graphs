"""CEG-009 — every domain under domains/ must be in the shape the loader reads.

``DomainPackLoader`` resolves ``domains/<domain_id>/spec.yaml``
(``loader.SPEC_FILENAME``). Nine specs used a flat
``<name>_domain_spec.yaml`` convention instead and were therefore unreachable:
they looked like available verticals in a directory listing, but a tenant id
matching one of them failed to resolve. Only the packs already in the directory
shape loaded by default.

Two of those nine were additionally invalid — ``executive-assistant`` declared
an edge to an undeclared ``Skill`` node, ``roofing-company`` one to ``ZipCode``
— and nothing caught it, because a file the loader never opens is never
validated either.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from engine.config.loader import SPEC_FILENAME, DomainPackLoader, admission_defects, withholding_reason
from engine.config.schema import DomainActivation, DomainSpec

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


def test_loader_discovers_every_admitted_domain() -> None:
    """Everything on disk but not listed is withheld by the generic policy, for a stated reason."""
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    discovered = set(loader.list_domains())
    on_disk = {d.name for d in _domain_dirs()}
    assert discovered <= on_disk
    for name in sorted(on_disk - discovered):
        raw = yaml.safe_load((DOMAINS_ROOT / name / SPEC_FILENAME).read_text(encoding="utf-8"))
        assert withholding_reason(DomainSpec.model_validate(raw)) is not None, f"{name} withheld without a reason"


def test_every_discovered_domain_actually_loads() -> None:
    loader = DomainPackLoader(str(DOMAINS_ROOT))
    for domain_id in sorted(loader.list_domains()):
        assert loader.load_domain(domain_id).domain.id == domain_id


# --------------------------------------------------------------------------
# Readable is not the same as correct.
#
# The defect classifier lives in the engine (``engine.config.loader.admission_defects``)
# and is the loader's own admission check, so these tests exercise the policy
# that runs in production rather than a test-only reimplementation.
# --------------------------------------------------------------------------


def _spec(domain_dir: Path) -> DomainSpec:
    return DomainSpec.model_validate(yaml.safe_load((domain_dir / SPEC_FILENAME).read_text(encoding="utf-8")))


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_admitted_domain_gates_compile_to_executable_cypher(domain_dir: Path) -> None:
    """A pack the loader will serve must produce Cypher that can run.

    The gap this closes: validating a spec against DomainSpec proves it parses,
    not that its gates execute. Packs that passed Pydantic validation still
    compiled to `exists((candidate)-[:RELATES_TO]->(t))` or `$85.0`. The generic
    policy withholds exactly those packs; a pack it withholds is exempt here,
    because withholding is what the policy records.
    """
    spec = _spec(domain_dir)
    reason = withholding_reason(spec)
    if reason is not None:
        pytest.skip(reason)
    defects = admission_defects(spec)
    assert defects == [], f"{domain_dir.name} would serve unexecutable Cypher:\n  " + "\n  ".join(defects)


def test_withheld_packs_are_withheld_for_a_reason_that_still_holds() -> None:
    """Every pack on disk that the policy withholds has a live, generic reason.

    Either its gates still do not compile (``admission_defects``) or it declares
    itself dormant. No pack is withheld by name: there is no per-domain flag to
    forget a fixed pack in.
    """
    withheld = [d for d in _domain_dirs() if withholding_reason(_spec(d)) is not None]
    assert withheld, "no pack is withheld; this suite documents a policy that is no longer exercised"
    for domain_dir in withheld:
        spec = _spec(domain_dir)
        dormant = spec.domain.activation is DomainActivation.DORMANT
        assert dormant or admission_defects(spec), (
            f"{domain_dir.name} is withheld but compiles cleanly and is not dormant — the policy has no reason"
        )


def test_operator_overrides_are_generic_and_default_off() -> None:
    """The only ways to serve a withheld pack are two generic settings, both off by default."""
    from engine.config.settings import Settings

    defaults = Settings(neo4j_password="test-pw", api_secret_key="test-key")
    assert defaults.unvalidated_domain_packs_enabled is False
    assert defaults.dormant_domain_packs_enabled is False
    assert not hasattr(defaults, "idea_portfolio_enabled")


def test_loader_source_names_no_domain() -> None:
    """The admission policy must not special-case any domain id."""
    loader_file = inspect.getsourcefile(withholding_reason)
    assert loader_file is not None
    source = Path(loader_file).read_text(encoding="utf-8")
    assert "_DOMAIN_FEATURE_FLAGS" not in source
    for name in (d.name for d in _domain_dirs()):
        assert f'"{name}"' not in source, f"loader names domain {name!r}"


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("exists((candidate)-[:SERVES]->(t {route_code: $route_code}))", ["route_code"]),
        (
            "candidate.capacity >= $min_capacity AND candidate.tier IN [$tier_a, $tier_b]",
            ["min_capacity", "tier_a", "tier_b"],
        ),
        ("candidate.ids CONTAINS $x; RETURN $y", ["x", "y"]),
        ("candidate.utilization <= $85.0", ["85.0"]),
        ("candidate.active = $True)", ["True"]),
    ],
)
def test_parameter_capture_stops_at_closing_punctuation(cypher: str, expected: list[str]) -> None:
    """`$param}` in a traversal target filter is the identifier `param`, not a defect (PR #318 review)."""
    from engine.config.loader import _IDENTIFIER, _PARAM_REF

    captured = _PARAM_REF.findall(cypher)
    assert captured == expected
    # The scalar-queryparam defect the capture exists to report is still reported.
    assert [p for p in captured if not _IDENTIFIER.fullmatch(p)] == [
        p for p in expected if not p[0].isalpha() and p[0] != "_"
    ]
