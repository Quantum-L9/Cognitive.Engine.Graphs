"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [test, unit, kge]
owner: engine-team
status: active
--- /L9_META ---

Unit tests for engine/kge/beam_search.py — BeamSearchEngine.
Target Coverage: 85%+
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from engine.kge.beam_search import (
    BeamCandidate,
    BeamSearchConfig,
    BeamSearchEngine,
    PruneStrategy,
)
from engine.kge.compound_e3d import CompoundE3D, CompoundE3DConfig
from engine.kge.transformations import Scale

# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def model() -> CompoundE3D:
    """Create a minimal CompoundE3D model."""
    return CompoundE3D(CompoundE3DConfig(embedding_dim=64))


@pytest.fixture
def config() -> BeamSearchConfig:
    """Create a beam search config with small dimensions for testing."""
    return BeamSearchConfig(
        beam_width=3,
        max_depth=2,
        prune_strategy=PruneStrategy.SCORE_THRESHOLD,
        score_threshold=0.3,
    )


@pytest.fixture
def engine(model: CompoundE3D, config: BeamSearchConfig) -> BeamSearchEngine:
    """Create a BeamSearchEngine."""
    return BeamSearchEngine(model=model, config=config)


# ============================================================================
# TEST CLASSES
# ============================================================================


@pytest.mark.unit
class TestBeamCandidate:
    """Test BeamCandidate dataclass."""

    def test_to_dict_serialization(self) -> None:
        """to_dict returns all fields."""
        c = BeamCandidate(
            transformation_id="var_0001",
            transformation_type="rotation",
            params={"angle": 45.0},
            score=0.85,
            depth=1,
            parent_id="var_0000",
        )
        d = c.to_dict()
        assert d["id"] == "var_0001"
        assert d["type"] == "rotation"
        assert d["score"] == pytest.approx(0.85)
        assert d["depth"] == 1
        assert d["parent_id"] == "var_0000"

    def test_ordering_by_score_desc(self) -> None:
        """BeamCandidates sort by score descending via __lt__."""
        a = BeamCandidate("a", "r", {}, score=0.9, depth=1)
        b = BeamCandidate("b", "r", {}, score=0.7, depth=1)
        assert a < b  # higher score sorts first (negated)

    def test_ordering_tiebreak_by_id(self) -> None:
        """Equal scores tiebreak by transformation_id."""
        a = BeamCandidate("a", "r", {}, score=0.8, depth=1)
        b = BeamCandidate("b", "r", {}, score=0.8, depth=1)
        assert a < b  # "a" < "b" lexically


@pytest.mark.unit
class TestBeamSearchConfig:
    """Test BeamSearchConfig."""

    def test_defaults(self) -> None:
        """Default config has sensible values."""
        c = BeamSearchConfig()
        assert c.beam_width == 10  # C-20: KGEBeamSearchSpec().beamwidth (CEG-COR-110; was 5)
        assert c.max_depth == 3
        assert c.prune_strategy == PruneStrategy.COMBINED

    def test_from_spec_with_none(self) -> None:
        """from_spec(None) returns defaults."""
        c = BeamSearchConfig.from_spec(None)
        assert c.beam_width == 10  # C-20: KGEBeamSearchSpec().beamwidth (CEG-COR-110; was 5)
        assert c.max_depth == 3

    @pytest.mark.finding("CEG-COR-110")
    def test_default_width_agrees_with_schema_default(self) -> None:
        """RU-123: both constructors agree with KGEBeamSearchSpec (C-20)."""
        from engine.config.schema import KGEBeamSearchSpec

        spec_default = KGEBeamSearchSpec()
        assert BeamSearchConfig.from_spec(None).beam_width == BeamSearchConfig().beam_width == spec_default.beamwidth
        assert BeamSearchConfig.from_spec(None).max_depth == spec_default.maxdepth == 3

    def test_from_spec_with_spec(self) -> None:
        """from_spec extracts beamwidth and maxdepth."""
        spec = MagicMock()
        spec.beamwidth = 10
        spec.maxdepth = 5
        c = BeamSearchConfig.from_spec(spec)
        assert c.beam_width == 10
        assert c.max_depth == 5


@pytest.mark.unit
class TestBeamSearchEngine:
    """Test BeamSearchEngine execution."""

    def test_search_skips_when_kge_disabled(self, model: CompoundE3D) -> None:
        """search() returns skipped when kge_enabled=False."""
        config = BeamSearchConfig(beam_width=3, max_depth=1)
        engine = BeamSearchEngine(model, config)
        with patch("engine.kge.beam_search.settings") as mock_settings:
            mock_settings.kge_enabled = False
            result = engine.search()
            assert result["status"] == "skipped"
            assert result["variants"] == []

    def test_search_returns_variants(self, model: CompoundE3D) -> None:
        """search() returns variant dicts when kge_enabled=True."""
        config = BeamSearchConfig(
            beam_width=3,
            max_depth=1,
            prune_strategy=PruneStrategy.SCORE_THRESHOLD,
            score_threshold=0.0,
        )
        engine = BeamSearchEngine(model, config)
        with patch("engine.kge.beam_search.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = engine.search()
            assert "variants" in result
            assert len(result["variants"]) <= config.beam_width
            assert "depth_levels" in result
            assert "audit_trail" in result

    def test_search_audit_trail_length(self, model: CompoundE3D) -> None:
        """Audit trail has one entry per depth level."""
        config = BeamSearchConfig(beam_width=3, max_depth=2, score_threshold=0.0)
        engine = BeamSearchEngine(model, config)
        with patch("engine.kge.beam_search.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = engine.search()
            assert len(result["audit_trail"]) == config.max_depth

    def test_search_config_snapshot(self, model: CompoundE3D) -> None:
        """search() includes config snapshot in result."""
        config = BeamSearchConfig(beam_width=5, max_depth=2, score_threshold=0.4)
        engine = BeamSearchEngine(model, config)
        with patch("engine.kge.beam_search.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = engine.search()
            sc = result["search_config"]
            assert sc["beam_width"] == 5
            assert sc["max_depth"] == 2
            assert sc["score_threshold"] == pytest.approx(0.4)

    def test_pruned_candidates_logged(self, model: CompoundE3D) -> None:
        """Pruned candidates are logged when log_pruned=True."""
        config = BeamSearchConfig(
            beam_width=2,
            max_depth=1,
            prune_strategy=PruneStrategy.SCORE_THRESHOLD,
            score_threshold=0.5,
            log_pruned=True,
        )
        engine = BeamSearchEngine(model, config)
        with patch("engine.kge.beam_search.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = engine.search()
            # Some candidates should be pruned since threshold is 0.5
            assert "pruned" in result


@pytest.mark.unit
class TestPruneStrategies:
    """Test individual pruning strategies."""

    def test_prune_by_threshold(self, engine: BeamSearchEngine) -> None:
        """_prune_by_threshold keeps candidates above threshold."""
        candidates = [
            BeamCandidate("a", "r", {}, score=0.8, depth=1),
            BeamCandidate("b", "r", {}, score=0.2, depth=1),
            BeamCandidate("c", "r", {}, score=0.5, depth=1),
        ]
        kept = engine._prune_by_threshold(candidates)
        assert len(kept) == 2
        scores = {c.transformation_id for c in kept}
        assert "a" in scores
        assert "c" in scores
        assert "b" not in scores

    def test_prune_by_diversity(self, engine: BeamSearchEngine) -> None:
        """_prune_by_diversity filters similar candidates."""
        c1 = BeamCandidate("a", "r", {"angle": 45.0}, score=0.9, depth=1)
        c2 = BeamCandidate("b", "r", {"angle": 45.001}, score=0.85, depth=1)
        c3 = BeamCandidate("c", "r", {"angle": 180.0}, score=0.7, depth=1)
        engine.config.diversity_threshold = 0.9
        kept = engine._prune_by_diversity([c1, c2, c3])
        assert len(kept) >= 1
        assert kept[0].transformation_id == "a"

    def test_prune_by_constraint(self, engine: BeamSearchEngine) -> None:
        """_prune_by_constraint filters invalid transformations."""
        engine.config.constraint_validators = [lambda tx: isinstance(tx, Scale)]
        candidates = [
            BeamCandidate(
                "a", "rotation", {"angle": 45.0, "axis_x": 1.0, "axis_y": 0.0, "axis_z": 0.0}, score=0.9, depth=1
            ),
            BeamCandidate("b", "scale", {"factor": 1.5}, score=0.8, depth=1),
        ]
        kept = engine._prune_by_constraint(candidates)
        assert len(kept) == 1
        assert kept[0].transformation_id == "b"

    def test_prune_by_constraint_no_validators(self, engine: BeamSearchEngine) -> None:
        """_prune_by_constraint passes all when no validators."""
        engine.config.constraint_validators = []
        candidates = [
            BeamCandidate("a", "r", {}, score=0.9, depth=1),
        ]
        kept = engine._prune_by_constraint(candidates)
        assert len(kept) == 1


@pytest.mark.unit
class TestParamSimilarity:
    """Test _param_similarity static method."""

    def test_identical_params(self) -> None:
        """Identical params give similarity 1.0."""
        sim = BeamSearchEngine._param_similarity({"a": 1.0}, {"a": 1.0})
        assert sim == pytest.approx(1.0)

    def test_different_params(self) -> None:
        """Different params give similarity < 1.0."""
        sim = BeamSearchEngine._param_similarity({"a": 0.0}, {"a": 100.0})
        assert sim < 0.1

    def test_empty_params(self) -> None:
        """Empty params give similarity 1.0."""
        sim = BeamSearchEngine._param_similarity({}, {})
        assert sim == pytest.approx(1.0)


@pytest.mark.unit
class TestSuccessorGeneration:
    """Test successor generation."""

    def test_generates_successors(self, engine: BeamSearchEngine) -> None:
        """_generate_successors produces successor candidates."""
        parent = BeamCandidate("p", "identity", {}, score=1.0, depth=0)
        successors = engine._generate_successors(parent)
        assert len(successors) > 0
        assert all(s.depth == 1 for s in successors)
        assert all(s.parent_id == "p" for s in successors)

    def test_successor_types(self, engine: BeamSearchEngine) -> None:
        """Successors include all transformation types."""
        parent = BeamCandidate("p", "identity", {}, score=1.0, depth=0)
        successors = engine._generate_successors(parent)
        types = {s.transformation_type for s in successors}
        assert types == {"rotation", "scale", "translation", "flip", "hyperplane"}

    def test_build_unknown_type_raises(self, engine: BeamSearchEngine) -> None:
        """_build_transformation raises on unknown type."""
        with pytest.raises(ValueError, match="Unknown transformation type"):
            engine._build_transformation("warp", {"factor": 1.0})


# ============================================================================
# CORRECTNESS REMEDIATION (lane G)
# ============================================================================


def _model_with_embeddings() -> CompoundE3D:
    import numpy as np

    model = CompoundE3D(CompoundE3DConfig(embedding_dim=6))
    rng = np.random.default_rng(3)
    model._entity_embeddings = {f"e{i}": rng.normal(size=6) for i in range(10)}
    return model


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-108")
class TestComposedCascades:
    """RU-121: a depth-d successor is its parent's cascade plus one new op, scored composed."""

    def test_depth_two_chain_includes_parent_op(self) -> None:
        engine = BeamSearchEngine(_model_with_embeddings(), BeamSearchConfig())
        root = BeamCandidate("root", "identity", {}, score=1.0, depth=0)
        depth1 = engine._generate_successors(root)
        assert all(len(c.chain) == 1 for c in depth1)
        parent = next(c for c in depth1 if c.transformation_type == "scale" and c.params == {"factor": 2.0})
        depth2 = engine._generate_successors(parent)
        assert all(len(c.chain) == 2 for c in depth2)
        assert all(c.chain[0] == ("scale", {"factor": 2.0}) for c in depth2)
        serialised = depth2[0].to_dict()["chain"]
        assert serialised[0] == {"type": "scale", "params": {"factor": 2.0}}
        assert serialised[1] == {"type": depth2[0].transformation_type, "params": depth2[0].params}

    def test_depth_two_score_reflects_composition(self) -> None:
        engine = BeamSearchEngine(_model_with_embeddings(), BeamSearchConfig())
        root = BeamCandidate("root", "identity", {}, score=1.0, depth=0)
        depth1 = engine._generate_successors(root)
        parent = next(c for c in depth1 if c.transformation_type == "scale" and c.params == {"factor": 2.0})
        depth2 = engine._generate_successors(parent)

        def _score(cands: list[BeamCandidate], tx_type: str, params: dict[str, float]) -> float:
            return next(c.score for c in cands if c.transformation_type == tx_type and c.params == params)

        new_op = ("translation", {"offset_x": 0.1, "offset_y": 0.0, "offset_z": 0.0})
        assert _score(depth2, *new_op) != pytest.approx(_score(depth1, *new_op))

    def test_depth_one_isinstance_validator_scores_as_bare_op(self) -> None:
        engine = BeamSearchEngine(_model_with_embeddings(), BeamSearchConfig())
        engine.config.constraint_validators = [lambda tx: isinstance(tx, Scale)]
        depth1 = engine._generate_successors(BeamCandidate("root", "identity", {}, score=1.0, depth=0))
        for c in depth1:
            bare = engine._build_transformation(c.transformation_type, c.params)
            assert c.score == pytest.approx(engine._score_candidate(bare))

    def test_scoring_and_pruning_validators_see_same_cascade(self) -> None:
        from engine.kge.transformations import Flip

        def _no_flip(tx: object) -> bool:
            return not any(isinstance(op, Flip) for op in getattr(tx, "ops", [tx]))

        engine = BeamSearchEngine(_model_with_embeddings(), BeamSearchConfig(constraint_validators=[_no_flip]))
        parent = BeamCandidate("p", "flip", {"axis": 0.0}, score=1.0, depth=1)
        depth2 = engine._generate_successors(parent)
        for c in depth2:
            composed = engine._composed_transformation(c)
            assert c.score == pytest.approx(engine._score_candidate(composed))
        assert engine._prune_by_constraint(depth2) == []

    def test_search_depth_levels_differ(self) -> None:
        engine = BeamSearchEngine(
            _model_with_embeddings(),
            BeamSearchConfig(beam_width=3, max_depth=3, prune_strategy=PruneStrategy.SCORE_THRESHOLD),
        )
        with patch("engine.kge.beam_search.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = engine.search()
        levels = result["depth_levels"]

        def _triples(level: list[dict[str, object]]) -> list[tuple[object, object, object]]:
            return [(c["type"], c["params"], c["score"]) for c in level]

        assert _triples(levels[1]) != _triples(levels[2])
        assert all(len(c["chain"]) == 2 for c in levels[2])
        assert all(len(c["chain"]) == 3 for c in levels[3])


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-109")
class TestDiversityComparesSameTypeOnly:
    """RU-122: candidates of different transformation types are never near-duplicates."""

    def test_equal_score_translation_and_flip_both_kept(self, engine: BeamSearchEngine) -> None:
        translation = BeamCandidate(
            "t", "translation", {"offset_x": 0.1, "offset_y": 0.0, "offset_z": 0.0}, score=0.9, depth=1
        )
        flip = BeamCandidate("f", "flip", {"axis": 0.0}, score=0.9, depth=1)
        engine.config.diversity_threshold = 0.8
        kept = engine._prune_by_diversity([translation, flip])
        assert {c.transformation_id for c in kept} == {"t", "f"}

    def test_cross_type_similarity_is_zero(self) -> None:
        a = BeamCandidate("t", "translation", {"offset_x": 0.1}, score=0.9, depth=1)
        b = BeamCandidate("f", "flip", {"axis": 0.0}, score=0.9, depth=1)
        assert BeamSearchEngine._candidate_similarity(a, b) == 0.0

    def test_same_type_similarity_unchanged(self) -> None:
        a = BeamCandidate("a", "scale", {"factor": 0.5}, score=0.9, depth=1)
        b = BeamCandidate("b", "scale", {"factor": 0.75}, score=0.8, depth=1)
        assert BeamSearchEngine._candidate_similarity(a, b) == pytest.approx(0.8)
        assert BeamSearchEngine._param_similarity(a.params, b.params) == pytest.approx(0.8)
        engine = BeamSearchEngine(CompoundE3D(CompoundE3DConfig(embedding_dim=6)), BeamSearchConfig())
        engine.config.diversity_threshold = 0.75
        kept = engine._prune_by_diversity([a, b])
        assert [c.transformation_id for c in kept] == ["a"]


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-108")
@pytest.mark.finding("CEG-COR-109")
class TestPruningSeesCompleteCascade:
    """RU-121/RU-122 follow-up (PR #352 review): constraint and diversity pruning
    judge a candidate's whole op chain, not only its terminal op."""

    @staticmethod
    def _flip_then_scale(score: float = 0.9, cid: str = "fs") -> BeamCandidate:
        return BeamCandidate(
            cid,
            "scale",
            {"factor": 1.5},
            score=score,
            depth=2,
            parent_id="p",
            chain=[("flip", {"axis": 0.0}), ("scale", {"factor": 1.5})],
        )

    def test_constraint_rejects_cascade_whose_earlier_op_violates(self, engine: BeamSearchEngine) -> None:
        from engine.kge.transformations import Flip

        def _no_flip(tx: object) -> bool:
            ops = getattr(tx, "ops", [tx])
            return not any(isinstance(op, Flip) for op in ops)

        engine.config.constraint_validators = [_no_flip]
        clean = BeamCandidate(
            "ss",
            "scale",
            {"factor": 1.5},
            score=0.8,
            depth=2,
            parent_id="q",
            chain=[("scale", {"factor": 2.0}), ("scale", {"factor": 1.5})],
        )
        kept = engine._prune_by_constraint([self._flip_then_scale(), clean])
        assert [c.transformation_id for c in kept] == ["ss"]

    def test_constraint_validator_receives_composed_cascade(self, engine: BeamSearchEngine) -> None:
        import numpy as np

        seen: list[object] = []

        def _record(tx: object) -> bool:
            seen.append(tx)
            return True

        engine.config.constraint_validators = [_record]
        engine._prune_by_constraint([self._flip_then_scale()])
        (tx,) = seen
        v = np.arange(6, dtype=np.float64)
        expected = np.array([-0.0, 1.0, 2.0, -3.0, 4.0, 5.0]) * 1.5
        assert np.allclose(tx.apply(v), expected)  # type: ignore[attr-defined]

    def test_same_terminal_op_with_different_parents_both_kept(self, engine: BeamSearchEngine) -> None:
        via_flip = self._flip_then_scale(score=0.9, cid="via_flip")
        via_rotation = BeamCandidate(
            "via_rotation",
            "scale",
            {"factor": 1.5},
            score=0.85,
            depth=2,
            parent_id="r",
            chain=[
                ("rotation", {"angle": 45.0, "axis_x": 1.0, "axis_y": 0.0, "axis_z": 0.0}),
                ("scale", {"factor": 1.5}),
            ],
        )
        engine.config.diversity_threshold = 0.8
        kept = engine._prune_by_diversity([via_flip, via_rotation])
        assert {c.transformation_id for c in kept} == {"via_flip", "via_rotation"}
        assert BeamSearchEngine._candidate_similarity(via_flip, via_rotation) == 0.0

    def test_identical_chains_still_pruned_as_duplicates(self, engine: BeamSearchEngine) -> None:
        a = self._flip_then_scale(score=0.9, cid="a")
        b = self._flip_then_scale(score=0.8, cid="b")
        engine.config.diversity_threshold = 0.8
        kept = engine._prune_by_diversity([a, b])
        assert [c.transformation_id for c in kept] == ["a"]

    def test_chain_similarity_counts_parent_params(self) -> None:
        a = BeamCandidate(
            "a",
            "scale",
            {"factor": 1.5},
            score=0.9,
            depth=2,
            chain=[("scale", {"factor": 0.5}), ("scale", {"factor": 1.5})],
        )
        b = BeamCandidate(
            "b",
            "scale",
            {"factor": 1.5},
            score=0.8,
            depth=2,
            chain=[("scale", {"factor": 2.0}), ("scale", {"factor": 1.5})],
        )
        assert BeamSearchEngine._candidate_similarity(a, b) == pytest.approx(1.0 / (1.0 + 1.5))
