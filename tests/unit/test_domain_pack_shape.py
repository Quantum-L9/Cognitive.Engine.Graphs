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
from pydantic import ValidationError

from engine.config.loader import (
    SPEC_FILENAME,
    DomainNotFoundError,
    DomainPackLoader,
    admission_defects,
    withholding_reason,
)
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


# --------------------------------------------------------------------------
# Identity contract (CEG-IDENT-01 / CEG-DBNAME-01, decided 2026-10-22):
# tenant id == domain.id == directory name == Neo4j database name.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("domain_dir", _domain_dirs(), ids=lambda p: p.name)
def test_every_domain_directory_is_a_legal_tenant_id(domain_dir: Path) -> None:
    """A shipped pack's directory is what a tenant id and a database will be named."""
    from engine.utils.security import validate_tenant_id

    assert validate_tenant_id(domain_dir.name) == domain_dir.name


def test_loader_refuses_an_illegal_tenant_id_before_reading_disk(tmp_path: Path) -> None:
    """A pack whose directory could not name a database is never opened.

    The grammar runs before path resolution, so even a directory that exists
    with a valid spec inside is refused by id, not by content.
    """
    pack = tmp_path / "Acme_Roofing"
    pack.mkdir()
    (pack / SPEC_FILENAME).write_text((DOMAINS_ROOT / "plasticos" / SPEC_FILENAME).read_text(encoding="utf-8"))
    loader = DomainPackLoader(str(tmp_path))
    with pytest.raises(DomainNotFoundError, match="not a legal tenant id"):
        loader.load_domain("Acme_Roofing")
    assert loader.list_domains() == []


def test_customer_spec_carries_template_provenance_and_keeps_tenant_identity() -> None:
    """A customer cut from a vertical keeps its own id; the vertical is provenance only."""
    raw = yaml.safe_load((DOMAINS_ROOT / "plasticos" / SPEC_FILENAME).read_text(encoding="utf-8"))
    raw["domain"]["id"] = "acme.recycling"
    raw["domain"]["template"] = {"id": "plasticos", "version": raw["domain"]["version"], "source": "crm-scan"}
    spec = DomainSpec.model_validate(raw)
    assert spec.domain.id == "acme.recycling"
    assert spec.domain.template is not None
    assert (spec.domain.template.id, spec.domain.template.source) == ("plasticos", "crm-scan")
    # Shipped verticals are the templates themselves: no provenance block.
    assert DomainSpec.model_validate(_raw("plasticos")).domain.template is None


def _raw(domain_id: str) -> dict[str, object]:
    raw = yaml.safe_load((DOMAINS_ROOT / domain_id / SPEC_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_template_provenance_requires_id_and_version() -> None:
    raw = _raw("plasticos")
    raw["domain"]["id"] = "acme.recycling"
    raw["domain"]["template"] = {"id": "plasticos"}
    with pytest.raises(ValidationError):
        DomainSpec.model_validate(raw)
