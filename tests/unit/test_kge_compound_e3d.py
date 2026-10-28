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

Unit tests for engine/kge/compound_e3d.py — CompoundE3D model.
Target Coverage: 85%+
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from engine.kge.compound_e3d import CompoundE3D, CompoundE3DConfig

# ============================================================================
# FIXTURES
# ============================================================================

TRAINING_TRIPLES = [
    ("alpha_recycling", "SUPPLIES_TO", "beta_compounding"),
    ("beta_compounding", "SUPPLIES_TO", "gamma_mrf"),
    ("alpha_recycling", "ACCEPTS_POLYMER", "hdpe"),
    ("beta_compounding", "ACCEPTS_POLYMER", "pp"),
    ("gamma_mrf", "ACCEPTS_POLYMER", "hdpe"),
    ("alpha_recycling", "LOCATED_IN", "los_angeles"),
    ("beta_compounding", "LOCATED_IN", "los_angeles"),
    ("gamma_mrf", "LOCATED_IN", "new_york"),
]


@pytest.fixture
def config() -> CompoundE3DConfig:
    """Small config for fast tests."""
    return CompoundE3DConfig(
        embedding_dim=32,
        max_epochs=5,
        negative_sample_size=4,
    )


@pytest.fixture
def trained_model(config: CompoundE3DConfig) -> CompoundE3D:
    """Model trained on test triples."""
    model = CompoundE3D(config)
    with patch("engine.kge.compound_e3d.settings") as mock_settings:
        mock_settings.kge_enabled = True
        model.train(TRAINING_TRIPLES, epochs=3)
    return model


# ============================================================================
# TEST CLASSES
# ============================================================================


@pytest.mark.unit
class TestCompoundE3DConfig:
    """Test CompoundE3DConfig construction."""

    def test_defaults(self) -> None:
        """Default config has expected values."""
        c = CompoundE3DConfig()
        assert c.embedding_dim == 256
        assert c.learning_rate == pytest.approx(1e-3)
        assert c.margin == 1.0

    def test_from_kge_spec(self) -> None:
        """from_kge_spec extracts dim and relations."""
        spec = MagicMock()
        spec.embeddingdim = 128
        spec.trainingrelations = ["REL_A", "REL_B"]
        c = CompoundE3DConfig.from_kge_spec(spec)
        assert c.embedding_dim == 128
        assert c.training_relations == ["REL_A", "REL_B"]

    def test_from_settings(self) -> None:
        """from_settings reads kge_embedding_dim."""
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_embedding_dim = 512
            c = CompoundE3DConfig.from_settings()
            assert c.embedding_dim == 512


@pytest.mark.unit
class TestCompoundE3DTraining:
    """Test CompoundE3D training."""

    def test_train_skips_when_disabled(self, config: CompoundE3DConfig) -> None:
        """train() skips when kge_enabled=False."""
        model = CompoundE3D(config)
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = False
            result = model.train(TRAINING_TRIPLES)
            assert result["status"] == "skipped"
            assert not model._trained

    def test_train_completes(self, config: CompoundE3DConfig) -> None:
        """train() completes and sets _trained=True."""
        model = CompoundE3D(config)
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = model.train(TRAINING_TRIPLES, epochs=3)
            assert result["status"] == "completed"
            assert result["epochs"] == 3
            assert model._trained is True

    def test_train_creates_embeddings(self, config: CompoundE3DConfig) -> None:
        """train() creates entity and relation embeddings."""
        model = CompoundE3D(config)
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            model.train(TRAINING_TRIPLES, epochs=2)
            assert len(model._entity_embeddings) >= 6
            assert len(model._relation_embeddings) >= 3

    def test_train_reports_metrics(self, config: CompoundE3DConfig) -> None:
        """train() returns num_entities and num_relations."""
        model = CompoundE3D(config)
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = model.train(TRAINING_TRIPLES, epochs=1)
            assert result["num_entities"] >= 6
            assert result["num_relations"] >= 3
            assert "final_loss" in result


@pytest.mark.unit
class TestCompoundE3DInference:
    """Test CompoundE3D inference methods."""

    def test_score_triple_untrained_returns_zero(self, config: CompoundE3DConfig) -> None:
        """score_triple returns 0.0 for untrained model."""
        model = CompoundE3D(config)
        assert model.score_triple("a", "r", "b") == 0.0

    def test_score_triple_trained(self, trained_model: CompoundE3D) -> None:
        """score_triple returns value in [0, 1] for trained model."""
        score = trained_model.score_triple("alpha_recycling", "SUPPLIES_TO", "beta_compounding")
        assert 0.0 <= score <= 1.0

    def test_score_triple_unknown_entity(self, trained_model: CompoundE3D) -> None:
        """score_triple returns near-zero for unknown entity."""
        score = trained_model.score_triple("alpha_recycling", "SUPPLIES_TO", "nonexistent")
        assert score < 0.5  # inf distance → low sigmoid score

    def test_embed_known_entity(self, trained_model: CompoundE3D) -> None:
        """embed() returns numpy array for known entity."""
        emb = trained_model.embed("alpha_recycling")
        assert emb is not None
        assert isinstance(emb, np.ndarray)
        assert len(emb) == 32

    def test_embed_unknown_entity(self, trained_model: CompoundE3D) -> None:
        """embed() returns None for unknown entity."""
        assert trained_model.embed("ghost_entity") is None

    def test_predict_tail_untrained(self, config: CompoundE3DConfig) -> None:
        """predict_tail returns empty list for untrained model."""
        model = CompoundE3D(config)
        assert model.predict_tail("a", "r") == []

    def test_predict_tail_trained(self, trained_model: CompoundE3D) -> None:
        """predict_tail returns ranked candidates."""
        results = trained_model.predict_tail(
            "alpha_recycling",
            "SUPPLIES_TO",
            top_k=3,
        )
        assert len(results) <= 3
        assert all(isinstance(r, tuple) and len(r) == 2 for r in results)
        # Scores should be descending
        if len(results) >= 2:
            assert results[0][1] >= results[1][1]

    def test_predict_tail_with_candidates(self, trained_model: CompoundE3D) -> None:
        """predict_tail respects candidate list."""
        results = trained_model.predict_tail(
            "alpha_recycling",
            "SUPPLIES_TO",
            candidates=["beta_compounding", "gamma_mrf"],
        )
        ids = {r[0] for r in results}
        assert ids.issubset({"beta_compounding", "gamma_mrf"})


@pytest.mark.unit
class TestCompoundE3DSimilarity:
    """Test similarity method."""

    def test_similarity_known_entities(self, trained_model: CompoundE3D) -> None:
        """similarity returns value in [-1, 1]."""
        sim = trained_model.similarity("alpha_recycling", "beta_compounding")
        assert -1.0 <= sim <= 1.0

    def test_similarity_unknown_entity(self, trained_model: CompoundE3D) -> None:
        """similarity returns 0.0 for unknown entity."""
        assert trained_model.similarity("alpha_recycling", "ghost") == 0.0

    def test_similarity_both_unknown(self, config: CompoundE3DConfig) -> None:
        """similarity returns 0.0 when both entities unknown."""
        model = CompoundE3D(config)
        assert model.similarity("a", "b") == 0.0


@pytest.mark.unit
class TestCompoundE3DScores:
    """Test compute_kge_scores batch method."""

    def test_batch_scores(self, trained_model: CompoundE3D) -> None:
        """compute_kge_scores returns dict of scores."""
        scores = trained_model.compute_kge_scores(
            "alpha_recycling",
            "SUPPLIES_TO",
            ["beta_compounding", "gamma_mrf"],
        )
        assert isinstance(scores, dict)
        assert "beta_compounding" in scores
        assert "gamma_mrf" in scores
        assert all(0.0 <= s <= 1.0 for s in scores.values())


# ============================================================================
# CORRECTNESS REMEDIATION (lane G)
# ============================================================================

DISJOINT_TRIPLES = [(f"h{i}", "R", f"t{i}") for i in range(20)]


def _seeded_rng(monkeypatch: pytest.MonkeyPatch, seed: int = 1234) -> None:
    """train() draws from an unseeded default_rng(); pin it for a deterministic test."""
    real = np.random.default_rng
    monkeypatch.setattr(np.random, "default_rng", lambda *_a, **_k: real(seed))


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-049")
class TestCompoundE3DTrainingDescendsMarginLoss:
    """RU-113: train() follows the TransE margin-loss gradient instead of collapsing."""

    def _train(self, monkeypatch: pytest.MonkeyPatch) -> CompoundE3D:
        _seeded_rng(monkeypatch)
        model = CompoundE3D(CompoundE3DConfig(embedding_dim=16, learning_rate=0.05, margin=1.0))
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            model.train(list(DISJOINT_TRIPLES), epochs=200)
        return model

    def test_true_tails_outscore_corrupted_tails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = self._train(monkeypatch)
        wins = total = 0
        for i in range(20):
            true_score = model.score_triple(f"h{i}", "R", f"t{i}")
            for j in range(20):
                if i == j:
                    continue
                total += 1
                wins += true_score > model.score_triple(f"h{i}", "R", f"t{j}")
        assert wins / total >= 0.90, f"{wins}/{total}"

    def test_entities_do_not_collapse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = self._train(monkeypatch)
        vecs = np.stack([model._entity_embeddings[f"t{i}"] for i in range(20)])
        dists = [np.linalg.norm(vecs[a] - vecs[b]) for a in range(20) for b in range(a + 1, 20)]
        assert float(np.mean(dists)) > 0.5
        for i in range(20):
            score = model.score_triple(f"h{i}", "R", f"t{i}")
            assert 0.0 <= score <= 1.0
            assert model._entity_embeddings[f"h{i}"].shape == (16,)


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-112")
class TestScoreAgainstIcpDoesNotMutate:
    """RU-125: the ICP centroid is registered only for the duration of one _distance call."""

    def test_centroid_not_left_in_entity_table(self, trained_model: CompoundE3D) -> None:
        trained_model.build_icp_centroid(["alpha_recycling", "beta_compounding"])
        before = set(trained_model._entity_embeddings)
        trained_model.score_against_icp("gamma_mrf", "SUPPLIES_TO")
        assert "__ICP_CENTROID__" not in trained_model._entity_embeddings
        assert set(trained_model._entity_embeddings) == before
        pool = [eid for eid, _ in trained_model.predict_tail("alpha_recycling", "SUPPLIES_TO", top_k=100)]
        assert "__ICP_CENTROID__" not in pool

    def test_score_unchanged(self, trained_model: CompoundE3D) -> None:
        from engine.kge.beam_search import _CascadeTransformation

        centroid = trained_model.build_icp_centroid(["alpha_recycling", "beta_compounding"])
        h = trained_model._entity_embeddings["gamma_mrf"]
        r = trained_model._relation_embeddings["SUPPLIES_TO"]
        # RU-119: the trained relation scores through its operator cascade,
        # ||H_r(h) + r - T_r(ICP)||; with no operators this is ||h + r - ICP||.
        head = _CascadeTransformation(trained_model._head_ops.get("SUPPLIES_TO", []))
        tail = _CascadeTransformation(trained_model._tail_ops.get("SUPPLIES_TO", []))
        dist = float(np.linalg.norm(head.apply(h) + r - tail.apply(centroid)))
        logit = -(trained_model._platt_alpha * dist - trained_model._platt_beta)
        expected = 1.0 / (1.0 + np.exp(-logit))
        score = trained_model.score_against_icp("gamma_mrf", "SUPPLIES_TO")
        assert score == pytest.approx(float(expected))
        assert 0.0 <= score <= 1.0

    def test_prior_entity_under_the_key_is_restored(self, trained_model: CompoundE3D) -> None:
        trained_model.build_icp_centroid(["alpha_recycling"])
        prior = np.full(trained_model.dim, 7.0)
        trained_model._entity_embeddings["__ICP_CENTROID__"] = prior
        trained_model.score_against_icp("gamma_mrf", "SUPPLIES_TO")
        assert trained_model._entity_embeddings["__ICP_CENTROID__"] is prior


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-113")
class TestTrainDoesNotReorderInput:
    """RU-126: train() shuffles a local copy, never the caller's list."""

    def test_triples_list_unchanged(self) -> None:
        triples = [(f"h{i}", "R", f"t{i}") for i in range(50)]
        original = list(triples)
        model = CompoundE3D(CompoundE3DConfig(embedding_dim=6))
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            result = model.train(triples, epochs=3)
        assert result["status"] == "completed"
        assert triples == original


@pytest.mark.unit
@pytest.mark.finding("CEG-COR-106")
class TestRelationOperatorCascades:
    """RG-154 (RU-119, DEC-45 "implement"): train() builds a per-relation operator
    cascade, and _distance scores ||H_r(h) + r - T_r(t)|| through it."""

    RELATIONS = {"SUPPLIES_TO", "ACCEPTS_POLYMER", "LOCATED_IN"}

    @staticmethod
    def _transe(model: CompoundE3D, h: str, r: str, t: str) -> float:
        emb = model._entity_embeddings
        return float(np.linalg.norm(emb[h] + model._relation_embeddings[r] - emb[t]))

    def test_train_builds_operators_for_each_training_relation(self, trained_model: CompoundE3D) -> None:
        assert set(trained_model._head_ops) == self.RELATIONS
        assert set(trained_model._tail_ops) == self.RELATIONS
        for relation in self.RELATIONS:
            assert trained_model._head_ops[relation], relation

    def test_distance_uses_a_non_identity_cascade(self, trained_model: CompoundE3D) -> None:
        from engine.kge.transformations import Rotation, Scale

        relation = "SUPPLIES_TO"
        trained_model._head_ops[relation] = [Rotation(angle=30.0, axis=(0.0, 0.0, 1.0)), Scale(factor=1.5)]
        trained_model._tail_ops[relation] = []
        h = trained_model._entity_embeddings["alpha_recycling"]
        t = trained_model._entity_embeddings["beta_compounding"]
        r = trained_model._relation_embeddings[relation]
        expected = float(np.linalg.norm(Scale(1.5).apply(Rotation(30.0, (0.0, 0.0, 1.0)).apply(h)) + r - t))

        dist = trained_model._distance("alpha_recycling", relation, "beta_compounding")

        assert dist == pytest.approx(expected)
        assert dist != pytest.approx(self._transe(trained_model, "alpha_recycling", relation, "beta_compounding"))

    def test_trained_cascade_is_not_transe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _seeded_rng(monkeypatch)
        model = CompoundE3D(CompoundE3DConfig(embedding_dim=16, learning_rate=0.05))
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            model.train(list(DISJOINT_TRIPLES), epochs=50)
        rotation, scale = model._head_ops["R"]
        assert (rotation.angle, scale.factor) != (0.0, 1.0)  # parameters were trained
        assert model._distance("h0", "R", "t0") != pytest.approx(self._transe(model, "h0", "R", "t0"))

    def test_transe_fallback_for_relation_without_operators(self, trained_model: CompoundE3D) -> None:
        del trained_model._head_ops["LOCATED_IN"]
        del trained_model._tail_ops["LOCATED_IN"]
        dist = trained_model._distance("gamma_mrf", "LOCATED_IN", "new_york")
        assert dist == pytest.approx(self._transe(trained_model, "gamma_mrf", "LOCATED_IN", "new_york"))

    def test_scores_stay_in_unit_interval(self, trained_model: CompoundE3D) -> None:
        for h, r, t in TRAINING_TRIPLES:
            assert 0.0 <= trained_model.score_triple(h, r, t) <= 1.0

    def test_retraining_keeps_existing_operators(self, trained_model: CompoundE3D) -> None:
        before = {r: list(ops) for r, ops in trained_model._head_ops.items()}
        with patch("engine.kge.compound_e3d.settings") as mock_settings:
            mock_settings.kge_enabled = True
            trained_model.train(TRAINING_TRIPLES, epochs=1)
        for relation, ops in before.items():
            assert [type(op) for op in trained_model._head_ops[relation]] == [type(op) for op in ops]
            assert trained_model._head_ops[relation][0].axis == ops[0].axis

    def test_cascade_gradients_match_finite_differences(self) -> None:
        from engine.kge.transformations import Rotation, Scale

        rng = np.random.default_rng(7)
        ops = [Rotation(angle=20.0, axis=(0.3, 0.5, 0.81)), Scale(factor=1.3)]
        x = rng.normal(size=8)
        upstream = rng.normal(size=8)

        def objective(ops_: list, x_: np.ndarray) -> float:
            out = x_
            for op in ops_:
                out = op.apply(out)
            return float(np.dot(upstream, out))

        grad_x, (g_angle, g_factor) = CompoundE3D._cascade_backward(ops, x, upstream)
        eps = 1e-6
        num_x = np.array(
            [
                (objective(ops, x + eps * np.eye(8)[i]) - objective(ops, x - eps * np.eye(8)[i])) / (2 * eps)
                for i in range(8)
            ]
        )
        d_rad = np.degrees(eps)
        num_angle = (
            objective([Rotation(20.0 + d_rad, ops[0].axis), ops[1]], x)
            - objective([Rotation(20.0 - d_rad, ops[0].axis), ops[1]], x)
        ) / (2 * eps)
        num_factor = (objective([ops[0], Scale(1.3 + eps)], x) - objective([ops[0], Scale(1.3 - eps)], x)) / (2 * eps)
        assert np.allclose(grad_x, num_x, atol=1e-5)
        assert g_angle == pytest.approx(num_angle, abs=1e-5)
        assert g_factor == pytest.approx(num_factor, abs=1e-5)
