"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, config]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for engine/config/loader.py — DomainPackLoader.
Target Coverage: 85%+
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.config.loader import (
    MAX_SPEC_BYTES,
    DomainNotFoundError,
    DomainPackLoader,
    DomainSpecError,
)
from tests.fixtures.domain_contracts import DESIGN_PACKS_ROOT


def _minimal_spec_yaml(domain_id: str) -> str:
    """Smallest spec the loader admits: validates and declares no gates."""
    return f"""
domain:
  id: {domain_id}
  name: {domain_id}
  version: 0.0.1
ontology:
  nodes:
    - label: Thing
      managedby: sync
      candidate: true
      matchdirection: d1
      properties:
        - {{name: thing_id, type: string, required: true}}
    - label: Ask
      managedby: api
      queryentity: true
      matchdirection: d1
      properties:
        - {{name: ask_id, type: string, required: true}}
  edges: []
matchentities:
  candidate: [{{label: Thing, matchdirection: d1}}]
  queryentity: [{{label: Ask, matchdirection: d1}}]
queryschema:
  matchdirections: [d1]
  fields: []
traversal:
  steps: []
gates: []
scoring:
  dimensions: []
"""


# ============================================================================
# TEST CLASSES
# ============================================================================


@pytest.mark.unit
class TestDomainPackLoaderInit:
    """Test DomainPackLoader initialization."""

    def test_init_with_explicit_path(self, tmp_path: Path) -> None:
        """DomainPackLoader resolves explicit config_path."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        assert loader._base_path == tmp_path.resolve()

    def test_init_from_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """DomainPackLoader reads DOMAINS_ROOT through Settings; the legacy DOMAIN_SPECS_PATH alias wins when set."""
        import engine.config.loader as loader_mod
        from engine.config.settings import Settings

        monkeypatch.setenv("DOMAINS_ROOT", str(tmp_path))
        monkeypatch.delenv("DOMAIN_SPECS_PATH", raising=False)
        monkeypatch.setattr(loader_mod, "settings", Settings(_env_file=None))
        assert DomainPackLoader()._base_path == tmp_path.resolve()

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        monkeypatch.setenv("DOMAIN_SPECS_PATH", str(legacy))
        assert DomainPackLoader()._base_path == legacy.resolve()

    def test_init_default_path(self) -> None:
        """DomainPackLoader defaults to 'domains' when no path given."""
        with patch.dict(os.environ, {}, clear=True):
            loader = DomainPackLoader()
            assert loader._base_path == Path("domains").resolve()


@pytest.mark.unit
class TestResolveSpecPath:
    """Test _resolve_spec_path security checks."""

    def test_blocks_empty_domain_id(self, tmp_path: Path) -> None:
        """Empty domain_id raises DomainNotFoundError."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError, match="cannot be empty"):
            loader._resolve_spec_path("")

    def test_blocks_whitespace_domain_id(self, tmp_path: Path) -> None:
        """Whitespace-only domain_id raises DomainNotFoundError."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError, match="cannot be empty"):
            loader._resolve_spec_path("   ")

    def test_blocks_null_bytes(self, tmp_path: Path) -> None:
        """Null byte in domain_id raises DomainNotFoundError."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError, match="null byte"):
            loader._resolve_spec_path("evil\x00domain")

    def test_blocks_path_traversal(self, tmp_path: Path) -> None:
        """Path traversal attempts raise DomainNotFoundError."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError):
            loader._resolve_spec_path("../../etc/passwd")

    def test_blocks_symlinks(self, tmp_path: Path) -> None:
        """Symlinked spec.yaml raises DomainNotFoundError."""
        # A legal tenant id, so the symlink check (not the grammar) refuses it.
        domain_dir = tmp_path / "evil-domain"
        domain_dir.mkdir()
        target = tmp_path / "real_spec.yaml"
        target.write_text("domain: {id: evil}")
        symlink = domain_dir / "spec.yaml"
        symlink.symlink_to(target)
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError, match="symlink"):
            loader._resolve_spec_path("evil-domain")

    @pytest.mark.parametrize(
        ("domain_id", "why"),
        [
            ("Acme", "uppercase"),
            ("acme_tenant", "underscore"),
            ("systemdb", "reserved prefix"),
            ("ab", "too short"),
            ("1acme", "leading digit"),
        ],
    )
    def test_blocks_ids_outside_the_tenant_grammar_before_touching_disk(
        self, tmp_path: Path, domain_id: str, why: str
    ) -> None:
        """tenant id == domain.id == directory == database (CEG-IDENT-01).

        A directory with that name and a spec inside exists, and is still never
        opened: the grammar is checked before path resolution.
        """
        domain_dir = tmp_path / domain_id
        domain_dir.mkdir()
        (domain_dir / "spec.yaml").write_text(f"domain: {{id: {domain_id}}}")
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError, match="not a legal tenant id|reserved prefix") as excinfo:
            loader._resolve_spec_path(domain_id)
        assert "spec.yaml" not in str(excinfo.value), why

    def test_missing_spec_file_raises(self, tmp_path: Path) -> None:
        """Non-existent spec.yaml raises DomainNotFoundError."""
        domain_dir = tmp_path / "missing"
        domain_dir.mkdir()
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainNotFoundError, match="not found"):
            loader._resolve_spec_path("missing")


@pytest.mark.unit
class TestLoadAndValidate:
    """Test _load_and_validate."""

    def test_oversized_file_raises(self, tmp_path: Path) -> None:
        """File exceeding MAX_SPEC_BYTES raises DomainSpecError."""
        domain_dir = tmp_path / "big"
        domain_dir.mkdir()
        spec_file = domain_dir / "spec.yaml"
        spec_file.write_text("x" * (MAX_SPEC_BYTES + 1))
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainSpecError, match="exceeds maximum size"):
            loader._load_and_validate(spec_file, "big")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        """Malformed YAML raises DomainSpecError."""
        domain_dir = tmp_path / "bad_yaml"
        domain_dir.mkdir()
        spec_file = domain_dir / "spec.yaml"
        spec_file.write_text("{{{{invalid yaml:::::")
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainSpecError, match="Invalid YAML"):
            loader._load_and_validate(spec_file, "bad_yaml")

    def test_non_dict_yaml_raises(self, tmp_path: Path) -> None:
        """YAML that parses to non-dict raises DomainSpecError."""
        domain_dir = tmp_path / "list_yaml"
        domain_dir.mkdir()
        spec_file = domain_dir / "spec.yaml"
        spec_file.write_text("- item1\n- item2")
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainSpecError, match="must be a YAML mapping"):
            loader._load_and_validate(spec_file, "list_yaml")

    def test_invalid_schema_raises(self, tmp_path: Path) -> None:
        """Valid YAML but invalid DomainSpec schema raises DomainSpecError."""
        domain_dir = tmp_path / "bad_schema"
        domain_dir.mkdir()
        spec_file = domain_dir / "spec.yaml"
        spec_file.write_text("domain: {id: test}")
        loader = DomainPackLoader(config_path=str(tmp_path))
        with pytest.raises(DomainSpecError, match="validation failed"):
            loader._load_and_validate(spec_file, "bad_schema")


@pytest.mark.unit
class TestLoadDomain:
    """Test load_domain caching behavior."""

    def test_caches_on_first_load(self, tmp_path: Path) -> None:
        """load_domain caches spec on first load."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        mock_spec = MagicMock()
        with (
            patch.object(loader, "_resolve_spec_path") as mock_resolve,
            patch.object(loader, "_load_and_validate", return_value=(mock_spec, MagicMock())),
        ):
            mock_path = MagicMock()
            mock_path.stat.return_value.st_mtime = 1000.0
            mock_resolve.return_value = mock_path
            result = loader.load_domain("test_domain")
            assert result == mock_spec
            assert "test_domain" in loader._cache

    def test_returns_cached_when_mtime_unchanged(self, tmp_path: Path) -> None:
        """load_domain returns cached spec when mtime is unchanged."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        mock_spec = MagicMock()
        loader._cache["cached"] = (mock_spec, 1000.0, 0.0)
        with patch.object(loader, "_resolve_spec_path") as mock_resolve:
            mock_path = MagicMock()
            mock_path.stat.return_value.st_mtime = 1000.0
            mock_resolve.return_value = mock_path
            result = loader.load_domain("cached")
            assert result == mock_spec

    def test_reloads_when_mtime_changes(self, tmp_path: Path) -> None:
        """load_domain reloads when file mtime changes."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        old_spec = MagicMock()
        new_spec = MagicMock()
        loader._cache["stale"] = (old_spec, 1000.0, 0.0)
        with (
            patch.object(loader, "_resolve_spec_path") as mock_resolve,
            patch.object(loader, "_load_and_validate", return_value=(new_spec, MagicMock())),
        ):
            mock_path = MagicMock()
            mock_path.stat.return_value.st_mtime = 2000.0
            mock_resolve.return_value = mock_path
            result = loader.load_domain("stale")
            assert result == new_spec


@pytest.mark.unit
class TestListDomains:
    """Test list_domains discovery."""

    def test_discovers_domain_dirs(self, tmp_path: Path) -> None:
        """list_domains finds directories holding an admissible spec.yaml."""
        for name in ["alpha", "beta"]:
            d = tmp_path / name
            d.mkdir()
            (d / "spec.yaml").write_text(_minimal_spec_yaml(name))
        (tmp_path / "no_spec_dir").mkdir()
        loader = DomainPackLoader(config_path=str(tmp_path))
        domains = loader.list_domains()
        assert "alpha" in domains
        assert "beta" in domains
        assert "no_spec_dir" not in domains

    def test_unadmitted_packs_are_not_listed(self, tmp_path: Path) -> None:
        """A pack that fails validation, or declares itself dormant, is never listed as servable."""
        good = tmp_path / "good"
        good.mkdir()
        (good / "spec.yaml").write_text(_minimal_spec_yaml("good"))
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "spec.yaml").write_text("placeholder")
        dormant = tmp_path / "dormant"
        dormant.mkdir()
        (dormant / "spec.yaml").write_text(
            _minimal_spec_yaml("dormant").replace("version: 0.0.1", "version: 0.0.1\n  activation: dormant")
        )
        loader = DomainPackLoader(config_path=str(tmp_path))
        assert loader.list_domains() == ["good"]
        with pytest.raises(DomainSpecError):
            loader.load_domain("broken")
        with pytest.raises(DomainNotFoundError, match="dormant"):
            loader.load_domain("dormant")

    def test_returns_empty_for_nonexistent_path(self) -> None:
        """list_domains returns [] for non-existent base path."""
        loader = DomainPackLoader(config_path="/nonexistent/path/xyz")
        assert loader.list_domains() == []


@pytest.mark.unit
class TestInvalidate:
    """Test cache invalidation."""

    def test_invalidate_single_domain(self, tmp_path: Path) -> None:
        """invalidate(domain_id) clears single entry."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        loader._cache["d1"] = (MagicMock(), 100.0)
        loader._cache["d2"] = (MagicMock(), 200.0)
        loader.invalidate("d1")
        assert "d1" not in loader._cache
        assert "d2" in loader._cache

    def test_invalidate_all(self, tmp_path: Path) -> None:
        """invalidate() with no args clears entire cache."""
        loader = DomainPackLoader(config_path=str(tmp_path))
        loader._cache["d1"] = (MagicMock(), 100.0)
        loader._cache["d2"] = (MagicMock(), 200.0)
        loader.invalidate()
        assert len(loader._cache) == 0


# ============================================================================
# RG-026 (RU-020 / CEG-COR-027..029): scoring dimensions that cannot execute
# withhold the pack at admission instead of failing every match.
# ============================================================================

_REPO_DOMAINS = DESIGN_PACKS_ROOT


def _spec_with_dimensions(  # type: ignore[no-untyped-def]
    dimensions: list[dict[str, object]],
    fields: list[dict[str, object]] | None = None,
    candidate_properties: list[dict[str, object]] | None = None,
):
    import yaml

    from engine.config.schema import DomainSpec

    raw = yaml.safe_load(_minimal_spec_yaml("scoring-check"))
    raw["scoring"]["dimensions"] = dimensions
    raw["queryschema"]["fields"] = fields or []
    raw["ontology"]["nodes"][0]["properties"] += candidate_properties or []
    if any(d.get("computation") == "preference_attention" for d in dimensions):
        # D3 strict scoring validation: preference_attention matches from `qe`,
        # so the pack binds it with a traversal step.
        raw["ontology"]["edges"] = [
            {
                "type": "ASKS",
                "from": "Ask",
                "to": "Thing",
                "direction": "DIRECTED",
                "category": "capability",
                "managedby": "sync",
            }
        ]
        raw["traversal"] = {"steps": [{"name": "query_entity", "pattern": "(qe:Ask)-[:ASKS]->(candidate:Thing)"}]}
    return DomainSpec.model_validate(raw)


def _repo_spec(domain_id: str):  # type: ignore[no-untyped-def]
    import yaml

    from engine.config.schema import DomainSpec

    return DomainSpec.model_validate(yaml.safe_load((_REPO_DOMAINS / domain_id / "spec.yaml").read_text()))


def _pre_repair_freight_spec():  # type: ignore[no-untyped-def]
    """freight-matching as it was before RU-037: deadhead/rate dims declare no operands.

    geodecay then reads candidate.lat/lon vs $lat/$lon and pricealignment
    candidate.price_per_unit vs $target_price (CEG-COR-011).
    """
    import yaml

    from engine.config.schema import DomainSpec

    raw = yaml.safe_load((_REPO_DOMAINS / "freight-matching" / "spec.yaml").read_text())
    for dim in raw["scoring"]["dimensions"]:
        if dim["name"] in ("deadhead_distance", "rate_alignment"):
            for key in ("candidateprop", "candidateprop_lon", "queryprop", "queryprop_lon"):
                dim.pop(key, None)
    return DomainSpec.model_validate(raw)


# Community dimensions declare the community property they read (Stage 2
# follow-up, item 8); these fixtures exercise the scoring admission checks.
_BRIDGE = {
    "name": "bridge",
    "source": "computed",
    "computation": "community_bridge",
    "candidateprop": "community_id",
    "weightkey": "wb",
    "defaultweight": 0.1,
}
_ATTENTION = {
    "name": "attention",
    "source": "computed",
    "computation": "preference_attention",
    "candidateprop": "community_id",
    "weightkey": "wa",
    "defaultweight": 0.1,
}
_POPMEAN = {
    "name": "rating",
    "source": "candidateproperty",
    "candidateprop": "rating",
    "computation": "candidateproperty",
    "null_strategy": "population_mean",
    "weightkey": "wr",
    "defaultweight": 0.1,
}
_PRIOR = {**_POPMEAN, "name": "prior", "null_strategy": "inherit_prior"}
_STRUCTURAL = {**_POPMEAN, "name": "cold", "null_strategy": "zero", "cold_start_fallback": "structural_similarity"}


@pytest.mark.unit
class TestScoringAdmission:
    """admission_defects compiles each direction's scoring and checks its parameters bind."""

    @pytest.fixture(autouse=True)
    def _default_admission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config import loader as loader_module

        monkeypatch.setattr(loader_module.settings, "unvalidated_domain_packs_enabled", False)
        monkeypatch.setattr(loader_module.settings, "dormant_domain_packs_enabled", False)
        # The check ships off (owner decision 2026-10-29); this class tests it on.
        monkeypatch.setattr(loader_module.settings, "scoring_admission_check_enabled", True)
        # ND-PARAM: strict scoring validation refuses options without a producer
        # at load; these admission defects are its rollback (flag-off) path
        # (tests/unit/test_scoring_spec_strict_validation.py covers the refusal).
        monkeypatch.setattr(loader_module.settings, "scoring_spec_strict_validation_enabled", False)

    def test_community_bridge_without_query_field_names_the_parameter(self) -> None:
        from engine.config.loader import withholding_reason

        reason = withholding_reason(_spec_with_dimensions([_BRIDGE]))
        assert reason is not None
        assert "query_community_id" in reason

    def test_community_bridge_with_its_query_field_is_admitted(self) -> None:
        from engine.config.loader import admission_defects

        spec = _spec_with_dimensions([_BRIDGE], fields=[{"name": "query_community_id", "type": "int"}])
        assert admission_defects(spec) == []

    def test_preference_attention_defect_quotes_the_compiler(self) -> None:
        from engine.config.loader import admission_defects

        defects = admission_defects(_spec_with_dimensions([_ATTENTION]))
        assert len(defects) == 1
        assert "attention" in defects[0]
        assert "requires metadata.success_value" in defects[0]

    def test_population_mean_names_its_unbound_parameter(self) -> None:
        from engine.config.loader import admission_defects

        defects = admission_defects(_spec_with_dimensions([_POPMEAN]))
        assert defects
        assert all("rating" in d for d in defects)
        assert any("_popmean_rating" in d for d in defects)

    @pytest.mark.parametrize("dim", [_PRIOR, _STRUCTURAL], ids=["inherit_prior", "structural_similarity"])
    def test_options_without_a_producer_are_defects(self, dim: dict[str, object]) -> None:
        from engine.config.loader import admission_defects

        defects = admission_defects(_spec_with_dimensions([dim]))
        assert len(defects) == 1
        assert dim["name"] in defects[0]
        assert "no producer" in defects[0]

    @pytest.mark.parametrize(
        "dim",
        [_POPMEAN, {**_POPMEAN, "null_strategy": "zero", "cold_start_fallback": "population_mean"}],
        ids=["null_strategy", "cold_start_fallback"],
    )
    @pytest.mark.parametrize(
        "producer",
        [
            {"fields": [{"name": "_popmean_rating", "type": "float", "default": 0.5}]},
            {"derived": {"name": "_popmean_rating", "expression": "0.5", "type": "float"}},
        ],
        ids=["queryschema", "derived"],
    )
    def test_population_mean_with_a_declared_parameter_is_admitted(
        self, dim: dict[str, object], producer: dict[str, object]
    ) -> None:
        """handle_match binds a declared `_popmean_<dim>`; the option executes, so it is not a defect."""
        from engine.config.loader import admission_defects, withholding_reason

        spec = _spec_with_dimensions([dim], fields=producer.get("fields"))  # type: ignore[arg-type]
        if "derived" in producer:
            from engine.config.schema import DerivedParameterSpec

            spec = spec.model_copy(
                update={"derivedparameters": [DerivedParameterSpec.model_validate(producer["derived"])]}
            )
        assert admission_defects(spec) == []
        assert withholding_reason(spec) is None

    @pytest.mark.asyncio
    async def test_admitted_population_mean_pack_binds_every_match_parameter(self) -> None:
        """The admitted pack executes: handle_match binds `$_popmean_rating` and every other reference."""
        import re
        from typing import Any
        from unittest.mock import MagicMock

        from engine.handlers import handle_match, init_dependencies

        spec = _spec_with_dimensions([_POPMEAN], fields=[{"name": "_popmean_rating", "type": "float", "default": 0.5}])
        calls: list[tuple[str, dict[str, Any]]] = []

        class _Driver:
            async def execute_query(
                self, *, cypher: str, parameters: dict[str, Any], database: str
            ) -> list[dict[str, Any]]:
                calls.append((cypher, parameters))
                return []

        loader = MagicMock()
        loader.load_domain.return_value = spec
        loader.list_domains.return_value = ["scoring-check"]
        init_dependencies(_Driver(), loader)  # type: ignore[arg-type]
        await handle_match("scoring-check", {"query": {}, "match_direction": "d1"})

        cypher, parameters = calls[0]
        assert "$_popmean_rating" in cypher
        assert parameters["_popmean_rating"] == 0.5
        assert set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", cypher)) <= set(parameters)

    def test_population_mean_without_its_parameter_is_one_unbound_defect(self) -> None:
        from engine.config.loader import admission_defects

        defects = admission_defects(_spec_with_dimensions([_POPMEAN]))
        assert len(defects) == 1
        assert "'_popmean_rating'" in defects[0]

    @pytest.mark.parametrize(
        ("dim", "prop"),
        [(_PRIOR, "_prior_prior"), (_STRUCTURAL, "shared_neighbor_score")],
        ids=["inherit_prior", "structural_similarity"],
    )
    def test_options_whose_candidate_property_the_pack_declares_are_admitted(
        self, dim: dict[str, object], prop: str
    ) -> None:
        """A property declared on the candidate node is written by sync (`SET n += row`): a producer."""
        from engine.config.loader import admission_defects

        spec = _spec_with_dimensions([dim], candidate_properties=[{"name": prop, "type": "float", "managedby": "sync"}])
        assert admission_defects(spec) == []

    def test_one_defect_per_offending_dimension(self) -> None:
        from engine.config.loader import admission_defects

        ok = {**_POPMEAN, "name": "plain", "null_strategy": "zero"}
        defects = admission_defects(_spec_with_dimensions([_BRIDGE, _ATTENTION, _PRIOR, ok]))
        # attention's missing success value is the named admission defect
        # (preference_attention_metadata_required_enabled), reported once and
        # not again by the scoring compiler.
        assert sorted(d.split(":", 1)[0] for d in defects) == sorted(
            ["scoring[d1] bridge", "undeclared scoring metadata", "scoring prior"]
        )

    def test_freight_matching_is_withheld_for_its_unbound_scoring_parameters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-RU-037 freight shape: geodecay/pricealignment reference $lat, $lon, $target_price."""
        from engine.config import loader as loader_module
        from engine.config.loader import admission_defects, withholding_reason

        spec = _pre_repair_freight_spec()
        # Stage 2 follow-up (item 8): the undeclared geo properties are named first.
        defects = "\n".join(admission_defects(spec))
        assert "scoring 'deadhead_distance' (geodecay) declares no candidateprop" in defects
        assert "'target_price'" in defects
        assert withholding_reason(spec) is not None
        # The RU-020 unbound-parameter check underneath, with that requirement off.
        monkeypatch.setattr(loader_module.settings, "declared_structural_properties_required_enabled", False)
        defects = "\n".join(admission_defects(spec))
        for param in ("'lat'", "'lon'", "'target_price'"):
            assert param in defects
        # RU-037 declared the operands: the repository pack is admitted.
        assert admission_defects(_repo_spec("freight-matching")) == []

    def test_flag_off_restores_gate_only_admission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config import loader as loader_module

        monkeypatch.setattr(loader_module.settings, "scoring_admission_check_enabled", False)
        # RU-039's full-match check also compiles scoring; isolate this unit's flag.
        monkeypatch.setattr(loader_module.settings, "admission_checks_full_match", False)
        # Gate-only admission: the undeclared-structural-property check is its own unit.
        monkeypatch.setattr(loader_module.settings, "declared_structural_properties_required_enabled", False)
        # So is the preference_attention success-value check (test_scoring_dimension_metadata.py).
        monkeypatch.setattr(loader_module.settings, "preference_attention_metadata_required_enabled", False)
        for spec in (_spec_with_dimensions([_BRIDGE, _ATTENTION, _POPMEAN, _PRIOR]), _pre_repair_freight_spec()):
            assert loader_module.admission_defects(spec) == []
            assert loader_module.withholding_reason(spec) is None

    @pytest.mark.parametrize("domain_id", ["plasticos", "mortgage-brokerage", "freight-matching"])
    def test_served_packs_have_no_scoring_defect(self, domain_id: str) -> None:
        """The scoring check alone withholds neither served pack (p_param_binding.py: unbound={})."""
        from engine.config.loader import admission_defects

        assert [d for d in admission_defects(_repo_spec(domain_id)) if d.startswith("scoring")] == []

    def test_unvalidated_override_still_serves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engine.config import loader as loader_module

        monkeypatch.setattr(loader_module.settings, "unvalidated_domain_packs_enabled", True)
        assert loader_module.withholding_reason(_spec_with_dimensions([_BRIDGE])) is None


# ============================================================================
# RG-065 (RU-051 / CEG-COR-078): the loader cache is bounded by Settings, read
# from the contract variable names DOMAIN_CACHE_MAXSIZE / DOMAIN_CACHE_TTL_SECONDS.
# ============================================================================


@pytest.mark.unit
class TestCacheBoundsFromSettings:
    @pytest.fixture
    def fresh_settings(self, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
        """Rebuild the Settings the loader reads after the test has set its environment."""
        from engine.config import loader as loader_module
        from engine.config.settings import Settings

        def _apply() -> None:
            monkeypatch.setattr(loader_module, "settings", Settings())

        for name in ("DOMAIN_CACHE_MAXSIZE", "DOMAIN_CACHE_MAX_SIZE", "DOMAIN_CACHE_TTL_SECONDS"):
            monkeypatch.delenv(name, raising=False)
        return _apply

    def test_contract_variable_bounds_the_cache(self, monkeypatch: pytest.MonkeyPatch, fresh_settings) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("DOMAIN_CACHE_MAXSIZE", "2")
        monkeypatch.setenv("DOMAIN_CACHE_TTL_SECONDS", "7")
        fresh_settings()
        loader = DomainPackLoader()
        assert loader._max_size == 2
        assert loader._ttl_seconds == 7.0

    def test_defaults_come_from_settings(self, fresh_settings) -> None:  # type: ignore[no-untyped-def]
        fresh_settings()
        loader = DomainPackLoader()
        assert (loader._max_size, loader._ttl_seconds) == (100, 30.0)

    def test_legacy_spelling_is_honoured_only_when_the_contract_variable_is_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fresh_settings,  # type: ignore[no-untyped-def]
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("DOMAIN_CACHE_MAX_SIZE", "5")
        fresh_settings()
        with caplog.at_level("WARNING", logger="engine.config.loader"):
            assert DomainPackLoader()._max_size == 5
        assert "deprecated" in caplog.text

        monkeypatch.setenv("DOMAIN_CACHE_MAXSIZE", "3")
        fresh_settings()
        assert DomainPackLoader()._max_size == 3

    def test_cache_eviction_honours_the_bound(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fresh_settings,  # type: ignore[no-untyped-def]
    ) -> None:
        monkeypatch.setenv("DOMAIN_CACHE_MAXSIZE", "2")
        fresh_settings()
        for name in ("pack-a", "pack-b", "pack-c"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "spec.yaml").write_text(_minimal_spec_yaml(name))
        loader = DomainPackLoader(config_path=str(tmp_path))
        for name in ("pack-a", "pack-b", "pack-c"):
            loader.load_domain(name)
        assert len(loader._cache) == 2
