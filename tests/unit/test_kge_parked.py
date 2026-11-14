"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tests, kge, hoprag]
tags: [tests, parked, kge, hoprag, compound-e3d]
owner: engine-team
status: active
--- /L9_META ---

CEG-KGE-PARK: HopRAG and CompoundE3D are parked (operator decision 2026-11-14).

Every entry point refuses with ParkedSubsystemError by default; importing the
modules stays harmless; ``parked_subsystems_enabled`` is the only override and
only a literal ``True`` un-parks.
"""

# ruff: noqa: S603 — only the current interpreter runs, with fixed in-repo code.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.config.schema import KGESpec
from engine.config.settings import Settings, settings
from engine.kge import parked
from engine.kge.parked import (
    COMPOUND_E3D,
    HOPRAG,
    PARKED_SUBSYSTEMS,
    ParkedSubsystemError,
    construct_compound_e3d,
    is_parked_model,
    require_unparked,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def parked_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "parked_subsystems_enabled", False)


def _assert_parked(err: ParkedSubsystemError, subsystem: str) -> None:
    assert err.subsystem == subsystem
    assert err.decision == "CEG-KGE-PARK"
    assert err.decided == "2026-11-14"
    assert "docs/PARKED_SUBSYSTEMS.md" in str(err)
    assert err.to_context()["parked"] is True


@pytest.mark.unit
class TestFlagDefault:
    def test_flag_defaults_parked(self) -> None:
        assert Settings.model_fields["parked_subsystems_enabled"].default is False

    def test_only_literal_true_unparks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("true", 1, MagicMock(), None):
            monkeypatch.setattr(settings, "parked_subsystems_enabled", value)
            assert parked.parked_subsystems_enabled() is False
        monkeypatch.setattr(settings, "parked_subsystems_enabled", True)
        assert parked.parked_subsystems_enabled() is True

    def test_unknown_subsystem_is_not_silently_allowed(self) -> None:
        with pytest.raises(ValueError, match="not a parked subsystem"):
            require_unparked("Louvain")


@pytest.mark.unit
@pytest.mark.usefixtures("parked_default")
class TestEntryPointsRefuse:
    def test_compound_e3d_refuses(self) -> None:
        from engine.kge.compound_e3d import CompoundE3D, CompoundE3DConfig

        with pytest.raises(ParkedSubsystemError) as err:
            CompoundE3D(CompoundE3DConfig())
        _assert_parked(err.value, COMPOUND_E3D)

    def test_gateway_refuses(self) -> None:
        with pytest.raises(ParkedSubsystemError) as err:
            construct_compound_e3d(KGESpec())
        _assert_parked(err.value, COMPOUND_E3D)

    def test_beam_search_refuses(self) -> None:
        from engine.kge.beam_search import BeamSearchConfig, BeamSearchEngine

        with pytest.raises(ParkedSubsystemError) as err:
            BeamSearchEngine(MagicMock(), BeamSearchConfig())
        _assert_parked(err.value, "KGE beam search")

    def test_pareto_ensemble_refuses(self) -> None:
        from engine.kge.pareto_ensemble import ParetoEnsembleController

        with pytest.raises(ParkedSubsystemError) as err:
            ParetoEnsembleController()
        _assert_parked(err.value, "KGE Pareto ensemble")

    def test_cross_dimensional_ensemble_refuses(self) -> None:
        from engine.kge.cross_dimensional_ensemble import CrossDimensionalEnsemble

        with pytest.raises(ParkedSubsystemError) as err:
            CrossDimensionalEnsemble()
        _assert_parked(err.value, "KGE cross-dimensional ensemble")

    def test_hoprag_index_builder_refuses(self) -> None:
        from engine.hoprag.config import HopRAGConfig
        from engine.hoprag.indexer import GraphIndexBuilder

        store: Any = MagicMock()
        with pytest.raises(ParkedSubsystemError) as err:
            GraphIndexBuilder(config=HopRAGConfig(), query_generator=MagicMock(), graph_store=store)
        _assert_parked(err.value, HOPRAG)
        store.assert_not_called()

    def test_every_parked_subsystem_refuses(self) -> None:
        for subsystem in PARKED_SUBSYSTEMS:
            with pytest.raises(ParkedSubsystemError):
                require_unparked(subsystem)

    def test_default_kge_spec_selects_a_parked_model(self) -> None:
        # KGESpec validation is unchanged; the parked default is refused at use.
        assert KGESpec().model == COMPOUND_E3D
        assert is_parked_model(KGESpec().model) is True
        assert is_parked_model("TransE") is False


@pytest.mark.unit
class TestUnparkOverride:
    def test_gateway_builds_when_unparked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "parked_subsystems_enabled", True)
        model = construct_compound_e3d(KGESpec(trainingrelations=["SUPPLIES"]))
        assert model.config.training_relations == ["SUPPLIES"]
        assert is_parked_model(COMPOUND_E3D) is False


@pytest.mark.unit
class TestImportsStayHarmless:
    def test_importing_parked_modules_constructs_nothing(self) -> None:
        code = (
            "import engine.kge.compound_e3d, engine.kge.beam_search, "
            "engine.kge.pareto_ensemble, engine.kge.cross_dimensional_ensemble, "
            "engine.hoprag, engine.hoprag.indexer; print('ok')"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        assert out.stdout.strip() == "ok"

    def test_parking_module_loads_no_parked_module(self) -> None:
        code = (
            "import sys, engine.kge, engine.kge.parked\n"
            "loaded = sorted(m for m in sys.modules if m.startswith(('engine.kge.', 'engine.hoprag')))\n"
            "print(','.join(loaded))"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        assert out.stdout.strip() == "engine.kge.parked"

    def test_package_exports_resolve_lazily(self) -> None:
        from engine import kge

        assert kge.ParkedSubsystemError is ParkedSubsystemError
        with pytest.raises(AttributeError):
            _ = kge.NotAThing  # type: ignore[attr-defined]
