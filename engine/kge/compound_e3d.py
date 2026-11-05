"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [kge]
tags: [kge, compound-e3d, model]
owner: engine-team
status: active
--- /L9_META ---

CompoundE3D — 3-D Knowledge Graph Embedding Model.

Implements the core embedding model referenced by:
- engine.config.schema.KGESpec  (model = "CompoundE3D")
- engine.config.settings  (kge_enabled, kge_embedding_dim)
- engine.scoring.assembler._compile_kge()

The model learns dense vector representations for entities and relations
via 3D geometric transformations (rotation, scaling, translation, flip,
hyperplane reflection).  During inference it scores (head, relation, tail)
triples for link prediction, entity similarity, and attribute completion.

Phase 4 implementation — gated behind settings.kge_enabled.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from engine.config.schema import KGESpec
from engine.config.settings import settings
from engine.kge.transformations import Rotation, Scale, Transformation3D

logger = logging.getLogger(__name__)

# Floor for a TransE residual norm when normalising it into a unit gradient.
_NORM_EPS = 1e-12

# Lower bound for a learned Scale factor (Scale requires factor > 0).
_MIN_SCALE = 1e-3


@dataclass
class CompoundE3DConfig:
    """Runtime configuration for CompoundE3D.

    Defaults pulled from KGESpec / Settings so the model can be
    instantiated with just ``CompoundE3DConfig.from_domain_spec(spec)``.
    """

    embedding_dim: int = 256
    learning_rate: float = 1e-3
    margin: float = 1.0
    negative_sample_size: int = 64
    regularization: float = 1e-5
    max_epochs: int = 200
    batch_size: int = 512
    training_relations: list[str] = field(default_factory=list)

    @classmethod
    def from_kge_spec(cls, spec: KGESpec) -> CompoundE3DConfig:
        """Construct from domain-pack KGESpec."""
        return cls(
            embedding_dim=spec.embeddingdim,
            training_relations=list(spec.trainingrelations),
        )

    @classmethod
    def from_settings(cls) -> CompoundE3DConfig:
        """Construct from global settings (fallback)."""
        config = cls(embedding_dim=settings.kge_embedding_dim)
        if config.embedding_dim != 256:
            logger.warning(
                "KGE embedding_dim=%d differs from schema default (256). "
                "Ensure domain spec KGESpec.embeddingdim matches.",
                config.embedding_dim,
            )
        return config


class CompoundE3D:
    """CompoundE3D embedding model.

    Public surface consumed by:
    - ``engine.kge.beam_search.BeamSearchEngine`` (scoring candidates)
    - ``engine.kge.ensemble.EnsembleController`` (fusing variant scores)
    - ``engine.scoring.assembler._compile_kge()`` (Cypher integration)

    Lifecycle:
        1. ``__init__`` — allocate embedding tables (numpy; PyTorch optional).
        2. ``train()`` — fit on (h, r, t) triples extracted from Neo4j.
        3. ``score_triple()`` — score a single (head, relation, tail).
        4. ``embed()`` — return dense vector for an entity/relation.
        5. ``predict_tail()`` — rank candidate tails for (head, relation, ?).
        6. ``write_to_graph()`` — persist kge_score properties for assembler.
    """

    def __init__(self, config: CompoundE3DConfig) -> None:
        self.config = config
        self.dim = config.embedding_dim
        self._entity_embeddings: dict[str, npt.NDArray[np.float64]] = {}
        self._relation_embeddings: dict[str, npt.NDArray[np.float64]] = {}
        self._trained = False

        # --- CompoundE3D operator stores (Ge et al. 2023 §3.2) ---
        # Keyed by relation name → ordered list of Transformation3D objects
        # (ops[0] applied first). Populated by _build_relation_operators() at
        # train time with a learnable head cascade [Rotation, Scale] and an
        # identity (empty) tail cascade per training relation (RU-119).
        self._head_ops: dict[str, list[Transformation3D]] = {}
        self._tail_ops: dict[str, list[Transformation3D]] = {}

        # Platt scaling calibration parameters (learned post-training).
        # Maps raw distance d → probability via sigmoid(-(alpha*d - beta)).
        self._platt_alpha: float = 1.0
        self._platt_beta: float = 0.0

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(
        self,
        triples: list[tuple[str, str, str]],
        epochs: int | None = None,
    ) -> dict[str, Any]:
        """Train on (head_id, relation_type, tail_id) triples.

        Returns training metrics dict.
        """
        if not settings.kge_enabled:
            logger.warning("KGE training skipped — kge_enabled=False")
            return {"status": "skipped", "reason": "kge_enabled=False"}

        epochs = epochs or self.config.max_epochs
        logger.info(
            "CompoundE3D.train: %d triples, dim=%d, epochs=%d",
            len(triples),
            self.dim,
            epochs,
        )

        # Collect unique entities and relations
        entities: set[str] = set()
        relations: set[str] = set()
        for h, r, t in triples:
            entities.update([h, t])
            relations.add(r)

        rng = np.random.default_rng()

        # Initialize embeddings (Xavier uniform)
        bound = np.sqrt(6.0 / self.dim)
        for eid in entities:
            if eid not in self._entity_embeddings:
                self._entity_embeddings[eid] = rng.uniform(-bound, bound, size=self.dim)
        for rid in relations:
            if rid not in self._relation_embeddings:
                self._relation_embeddings[rid] = rng.uniform(-bound, bound, size=self.dim)
        # Per-relation operator cascades, trained jointly with the embeddings.
        self._build_relation_operators(sorted(relations), rng)

        # Simplified training loop (production: replace with PyTorch optim)
        entity_list = list(entities)
        losses: list[float] = []
        # Shuffle a local copy each epoch; the caller's list is never reordered
        # (CEG-COR-113).
        order = list(triples)
        for _ in range(epochs):
            epoch_loss = 0.0
            rng.shuffle(order)
            for h, r, t in order:
                pos_score = self._distance(h, r, t)
                # Negative sampling — index the list so the type stays str
                neg_t = entity_list[int(rng.integers(len(entity_list)))]
                neg_score = self._distance(h, r, neg_t)
                loss = max(0.0, self.config.margin + pos_score - neg_score)
                epoch_loss += loss
                # SGD step along the gradient of the margin loss
                # L = margin + d(h, r, t) - d(h, r, t') (CEG-COR-049, RU-119).
                if loss > 0:
                    self._margin_sgd_step(h, r, t, neg_t)
            losses.append(epoch_loss / max(len(triples), 1))

        self._trained = True
        return {
            "status": "completed",
            "epochs": epochs,
            "final_loss": losses[-1] if losses else 0.0,
            "num_entities": len(entities),
            "num_relations": len(relations),
        }

    def _build_relation_operators(self, relations: list[str], rng: np.random.Generator) -> None:
        """Build the CompoundE3D operator cascade for each relation that has none.

        RU-119 / CEG-COR-106 (DEC-45 "implement"): every training relation gets
        a head cascade ``[Rotation(angle, axis), Scale(factor)]`` and an
        identity (empty) tail cascade, so ``_distance`` scores
        ``||S_r R_r h + r - t||`` (CompoundE3D-Head with the relation
        embedding as the translation component). The rotation axis is a random
        unit vector per relation; angle and factor start at the identity
        (0 degrees, 1.0) and are learned by ``_margin_sgd_step``. Relations
        that already carry operators keep them (retraining does not rebuild).
        """
        for relation in relations:
            if relation in self._head_ops:
                continue
            axis = rng.normal(size=3)
            norm = np.linalg.norm(axis).item()
            if norm < _NORM_EPS:
                axis = np.array([0.0, 0.0, 1.0])
                norm = 1.0
            unit = axis / norm
            self._head_ops[relation] = [
                Rotation(angle=0.0, axis=(unit[0].item(), unit[1].item(), unit[2].item())),
                Scale(factor=1.0),
            ]
            self._tail_ops[relation] = []

    def _effective_ops(self, relation: str) -> tuple[list[Transformation3D], list[Transformation3D]]:
        """Head and tail cascades used by ``_distance`` for ``scoring_mode``."""
        mode = getattr(self.config, "scoring_mode", "complete")
        head_ops = [] if mode == "tail" else list(self._head_ops.get(relation, []))
        tail_ops = [] if mode == "head" else list(self._tail_ops.get(relation, []))
        return head_ops, tail_ops

    @staticmethod
    def _rotation_derivative(op: Rotation, x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """d/d(angle in radians) of ``op.apply(x)`` (Rodrigues, per 3-block)."""
        out = np.zeros_like(x)
        ax = np.array(op.axis, dtype=np.float64)
        norm = np.linalg.norm(ax).item()
        if norm < 1e-9:
            return out
        ax = ax / norm
        rad = math.radians(op.angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        for i in range(0, len(x) - 2, 3):
            v = x[i : i + 3]
            out[i : i + 3] = -sin_a * v + cos_a * np.cross(ax, v) + sin_a * ax * np.dot(ax, v)
        return out

    @classmethod
    def _cascade_backward(
        cls,
        ops: list[Transformation3D],
        x: npt.NDArray[np.float64],
        upstream: npt.NDArray[np.float64],
    ) -> tuple[npt.NDArray[np.float64], list[float]]:
        """Backpropagate ``upstream`` (dL/d cascade(x)) through ``ops``.

        Returns (dL/dx, [dL/d param of op_k]) where the parameter of a Scale is
        its factor and of a Rotation its angle in radians. Only the operator
        family ``_build_relation_operators`` builds is trainable.
        """
        inputs: list[npt.NDArray[np.float64]] = []
        current = x
        for op in ops:
            inputs.append(current)
            current = op.apply(current)
        grad = upstream
        param_grads = [0.0] * len(ops)
        for k in range(len(ops) - 1, -1, -1):
            op, x_in = ops[k], inputs[k]
            if isinstance(op, Scale):
                param_grads[k] = np.dot(grad, x_in).item()
                grad = op.factor * grad
            elif isinstance(op, Rotation):
                param_grads[k] = np.dot(grad, cls._rotation_derivative(op, x_in)).item()
                grad = op.inverse().apply(grad)  # rotation is orthogonal: R^T = R^-1
            else:
                msg = f"CompoundE3D operator {type(op).__name__} has no training gradient"
                raise TypeError(msg)
        return grad, param_grads

    @staticmethod
    def _stepped_ops(ops: list[Transformation3D], grads: list[float], lr: float) -> list[Transformation3D]:
        """``ops`` with each learnable parameter moved one SGD step against its gradient."""
        stepped: list[Transformation3D] = []
        for op, g in zip(ops, grads, strict=True):
            if isinstance(op, Scale):
                stepped.append(Scale(factor=max(_MIN_SCALE, op.factor - lr * g)))
            elif isinstance(op, Rotation):
                stepped.append(Rotation(angle=math.degrees(math.radians(op.angle) - lr * g), axis=op.axis))
            else:
                stepped.append(op)
        return stepped

    def _compound_sgd_step(self, head: str, relation: str, tail: str, neg_tail: str) -> None:
        """One SGD step on L = margin + d(h, r, t) - d(h, r, t') through the cascades.

        d(h, r, t) = ||H_r(h) + r - T_r(t)||. With g = residual / ||residual||
        for the positive and g' for the negative triple, the upstream gradient
        is (g - g') into the head cascade and r, -g into T_r(t) and +g' into
        T_r(t'). All deltas are computed before any parameter moves, so an
        aliased entity receives the sum of its gradients.
        """
        from engine.kge.beam_search import _CascadeTransformation

        lr = self.config.learning_rate
        head_ops, tail_ops = self._effective_ops(relation)
        h_emb = self._entity_embeddings[head]
        r_emb = self._relation_embeddings[relation]
        t_emb = self._entity_embeddings[tail]
        n_emb = self._entity_embeddings[neg_tail]
        head_tx = _CascadeTransformation(head_ops)
        tail_tx = _CascadeTransformation(tail_ops)
        mapped_h = head_tx.apply(h_emb.copy())
        pos = mapped_h + r_emb - tail_tx.apply(t_emb.copy())
        neg = mapped_h + r_emb - tail_tx.apply(n_emb.copy())
        g_pos = pos / max(np.linalg.norm(pos).item(), _NORM_EPS)
        g_neg = neg / max(np.linalg.norm(neg).item(), _NORM_EPS)

        grad_h, head_param_grads = self._cascade_backward(head_ops, h_emb, g_pos - g_neg)
        grad_t, tail_grads_pos = self._cascade_backward(tail_ops, t_emb, -g_pos)
        grad_n, tail_grads_neg = self._cascade_backward(tail_ops, n_emb, g_neg)

        entity_deltas: dict[str, npt.NDArray[np.float64]] = {}
        for eid, delta in ((head, -lr * grad_h), (tail, -lr * grad_t), (neg_tail, -lr * grad_n)):
            entity_deltas[eid] = entity_deltas[eid] + delta if eid in entity_deltas else delta
        for eid, delta in entity_deltas.items():
            self._entity_embeddings[eid] = self._entity_embeddings[eid] + delta
        self._relation_embeddings[relation] = r_emb - lr * (g_pos - g_neg)
        if head_ops:
            self._head_ops[relation] = self._stepped_ops(head_ops, head_param_grads, lr)
        if tail_ops:
            tail_param_grads = [a + b for a, b in zip(tail_grads_pos, tail_grads_neg, strict=True)]
            self._tail_ops[relation] = self._stepped_ops(tail_ops, tail_param_grads, lr)

    def _margin_sgd_step(self, head: str, relation: str, tail: str, neg_tail: str) -> None:
        """One SGD step on the TransE margin loss for (h, r, t) against (h, r, t').

        A relation with an operator cascade (RU-119) steps through
        ``_compound_sgd_step`` instead; the TransE step below is the fallback
        for relations without operators.

        With g = (h + r - t) / ||h + r - t|| and g' = (h + r - t') / ||h + r - t'||:
        h -= lr * (g - g'); r -= lr * (g - g'); t += lr * g; t' -= lr * g'.
        All deltas are computed before any embedding moves, so an aliased
        entity (t' == h or t' == t) receives the sum of its gradients.
        """
        if relation in self._head_ops:
            self._compound_sgd_step(head, relation, tail, neg_tail)
            return
        lr = self.config.learning_rate
        h_emb = self._entity_embeddings[head]
        r_emb = self._relation_embeddings[relation]
        pos = h_emb + r_emb - self._entity_embeddings[tail]
        neg = h_emb + r_emb - self._entity_embeddings[neg_tail]
        g_pos = pos / max(np.linalg.norm(pos).item(), _NORM_EPS)
        g_neg = neg / max(np.linalg.norm(neg).item(), _NORM_EPS)

        entity_deltas: dict[str, npt.NDArray[np.float64]] = {}
        for eid, delta in ((head, -lr * (g_pos - g_neg)), (tail, lr * g_pos), (neg_tail, -lr * g_neg)):
            entity_deltas[eid] = entity_deltas[eid] + delta if eid in entity_deltas else delta
        for eid, delta in entity_deltas.items():
            self._entity_embeddings[eid] = self._entity_embeddings[eid] + delta
        self._relation_embeddings[relation] = r_emb - lr * (g_pos - g_neg)

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def score_triple(self, head: str, relation: str, tail: str) -> float:
        """Score a (head, relation, tail) triple.  Lower distance = better fit.

        Returns normalized score in [0, 1] where 1 = best.
        """
        if not self._trained:
            return 0.0
        dist = self._distance(head, relation, tail)
        # Convert distance to similarity score via sigmoid
        return float(1.0 / (1.0 + np.exp(dist - self.config.margin)))  # nosemgrep: semgrep.float-requires-try-except

    def embed(self, entity_id: str) -> npt.NDArray[np.float64] | None:
        """Return embedding vector for entity (or None if unknown)."""
        return self._entity_embeddings.get(entity_id)

    def predict_tail(
        self,
        head: str,
        relation: str,
        candidates: list[str] | None = None,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Rank candidate tails for (head, relation, ?).

        Returns list of (entity_id, score) sorted descending by score.
        """
        if not self._trained:
            return []
        pool = candidates or list(self._entity_embeddings.keys())
        scored = [(eid, self.score_triple(head, relation, eid)) for eid in pool]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def similarity(self, entity_a: str, entity_b: str) -> float:
        """Cosine similarity between two entity embeddings."""
        ea = self._entity_embeddings.get(entity_a)
        eb = self._entity_embeddings.get(entity_b)
        if ea is None or eb is None:
            return 0.0
        dot = np.dot(ea, eb)
        norm = np.linalg.norm(ea) * np.linalg.norm(eb)
        return float(dot / norm) if norm > 1e-9 else 0.0  # nosemgrep: semgrep.float-requires-try-except

    # ------------------------------------------------------------------
    # Graph Writeback (for assembler._compile_kge)
    # ------------------------------------------------------------------

    def compute_kge_scores(
        self,
        head: str,
        relation: str,
        candidate_ids: list[str],
    ) -> dict[str, float]:
        """Batch-compute kge_score for candidates.

        Output dict maps entity_id → score [0, 1].
        These are written to Neo4j as node properties consumed by
        ``engine.scoring.assembler._compile_kge()``.
        """
        return {cid: self.score_triple(head, relation, cid) for cid in candidate_ids}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _distance(self, head: str, relation: str, tail: str) -> float:
        """CompoundE3D triple scoring (Ge et al. 2023, arXiv:2304.00378).

        ``d = ||H_r(h) + r - T_r(t)||`` where H_r / T_r are the ordered operator
        cascades in ``self._head_ops[relation]`` / ``self._tail_ops[relation]``
        (applied with ``_CascadeTransformation``, ops[0] first) and ``r`` is the
        relation embedding (the cascade's translation component). Three
        scoring modes, controlled by ``self.config.scoring_mode``:
          'head'     : ||H_r(h) + r - t||          (CompoundE3D-Head)
          'tail'     : ||h + r - T_r(t)||          (CompoundE3D-Tail)
          'complete' : ||H_r(h) + r - T_r(t)||     (CompoundE3D-Complete, default)

        ``_build_relation_operators`` builds the cascades at train time
        (RU-119). A relation without operators falls back to TransE,
        ``||h + r - t||`` (cold-start / pre-training compatibility).
        """
        h_emb = self._entity_embeddings.get(head)
        t_emb = self._entity_embeddings.get(tail)
        r_emb = self._relation_embeddings.get(relation)

        if h_emb is None or t_emb is None:
            return float("inf")  # nosemgrep: semgrep.float-requires-try-except
        if r_emb is None:
            return float("inf")  # nosemgrep: semgrep.float-requires-try-except

        # --- Fallback to TransE if operators not yet built ---
        if relation not in self._head_ops:
            return float(np.linalg.norm((h_emb + r_emb) - t_emb))  # nosemgrep: semgrep.float-requires-try-except

        from engine.kge.beam_search import _CascadeTransformation

        head_ops, tail_ops = self._effective_ops(relation)
        transformed_h = _CascadeTransformation(head_ops).apply(h_emb.copy())
        transformed_t = _CascadeTransformation(tail_ops).apply(t_emb.copy())
        distance: float = np.linalg.norm(transformed_h + r_emb - transformed_t).item()
        return distance

    def calibrate_platt(
        self,
        val_distances: list[float],
        val_labels: list[int],
    ) -> None:
        """Fit Platt scaling parameters (alpha, beta) on a validation set.

        So that sigmoid(-(alpha * d - beta)) ≈ P(correct triple).
        Stores results in self._platt_alpha and self._platt_beta.

        Args:
            val_distances: Raw _distance() outputs for validation triples.
            val_labels:    1 for positive (correct) triple, 0 for negative.

        Raises:
            ValueError: If inputs are empty or mismatched in length.
        """
        if not val_distances or len(val_distances) != len(val_labels):
            msg = "calibrate_platt: val_distances and val_labels must be non-empty and equal length."
            raise ValueError(msg)
        from scipy.optimize import minimize

        d = np.array(val_distances, dtype=np.float64)
        y = np.array(val_labels, dtype=np.float64)

        def nll(params: npt.NDArray[np.float64]) -> float:
            alpha, beta = params
            logit = -(alpha * d - beta)
            prob = 1.0 / (1.0 + np.exp(-logit))
            prob = np.clip(prob, 1e-9, 1 - 1e-9)
            nll_value: float = -np.sum(y * np.log(prob) + (1 - y) * np.log(1 - prob))
            return nll_value

        result = minimize(nll, x0=[1.0, 0.0], method="L-BFGS-B")
        self._platt_alpha, self._platt_beta = (
            float(result.x[0]),  # nosemgrep: semgrep.float-requires-try-except
            float(result.x[1]),  # nosemgrep: semgrep.float-requires-try-except
        )  # nosemgrep: semgrep.float-requires-try-except

    def build_icp_centroid(
        self,
        account_ids: list[str],
    ) -> npt.NDArray[np.float64]:
        """Construct a synthetic ICP (Ideal Customer Profile) entity.

        The ICP is the centroid of known high-value account embeddings.
        Stores result in self._icp_centroid for use by score_against_icp().

        Args:
            account_ids: Entity IDs of known high-value accounts.
                         Must all exist in the embedding table.

        Returns:
            Centroid embedding as np.ndarray of shape (embed_dim,).

        Raises:
            ValueError: If account_ids is empty or any ID is unknown.
        """
        if not account_ids:
            msg = "build_icp_centroid: account_ids must be non-empty."
            raise ValueError(msg)
        embeddings = []
        for aid in account_ids:
            emb = self._entity_embeddings.get(aid)
            if emb is None:
                msg = f"build_icp_centroid: entity '{aid}' not found in embedding table. Run train() first."
                raise ValueError(msg)
            embeddings.append(emb)
        centroid: npt.NDArray[np.float64] = np.mean(np.stack(embeddings, axis=0), axis=0)
        self._icp_centroid = centroid
        return centroid

    def score_against_icp(
        self,
        candidate_id: str,
        relation: str,
    ) -> float:
        """Score a candidate entity against the ICP centroid via CompoundE3D.

        Returns calibrated probability in [0, 1].

        kge_score = sigmoid(-(alpha * d(candidate, relation, ICP) - beta))

        Args:
            candidate_id: Entity to score.
            relation:     Relation type for the triple (h, r, ICP).

        Returns:
            Float in [0, 1].

        Raises:
            RuntimeError: If build_icp_centroid() has not been called.
        """
        if not hasattr(self, "_icp_centroid"):
            msg = "score_against_icp: ICP centroid not built. Call build_icp_centroid(account_ids) first."
            raise RuntimeError(msg)
        # Register the ICP centroid as a named entity for this one _distance
        # call only (CEG-COR-112): leaving it in the table put it in
        # predict_tail's candidate pool. Any prior value under the key returns.
        icp_key = "__ICP_CENTROID__"
        prior = self._entity_embeddings.get(icp_key)
        self._entity_embeddings[icp_key] = self._icp_centroid
        try:
            raw_dist = self._distance(candidate_id, relation, icp_key)
        finally:
            if prior is None:
                del self._entity_embeddings[icp_key]
            else:
                self._entity_embeddings[icp_key] = prior
        # Apply Platt calibration
        logit = -(self._platt_alpha * raw_dist - self._platt_beta)
        prob = 1.0 / (1.0 + np.exp(-logit))
        return float(np.clip(prob, 0.0, 1.0))  # nosemgrep: semgrep.float-requires-try-except
