"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [scoring, assembler, cypher]
owner: engine-team
status: active
--- /L9_META ---

Scoring assembler: dimensions -> WITH clause.
Compiles scoring dimensions into Cypher WITH clause with weighted aggregation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from engine.config.schema import (
    ColdStartFallback,
    ComputationType,
    DomainSpec,
    NullStrategy,
    ScoringDimensionSpec,
    dimension_metadata,
)
from engine.graph.driver import GraphDriver
from engine.utils.security import cypher_number, sanitize_label

logger = logging.getLogger(__name__)

# Dangerous Cypher keywords that must be blocked in CUSTOMCYPHER expressions
_DANGEROUS_CYPHER_KEYWORDS = (
    "detach",  # must be before "delete" so DETACH DELETE reports detach
    "call",
    "create",
    "merge",
    "delete",
    "remove",
    "set",
    "match",
    "return",
    "with",
    "unwind",
    "foreach",
    "load",
    "using",
    "optional",
    "union",
    "apoc",
    "gds",
    "dbms",
)


def _validate_custom_expression(expression: str, dim_name: str) -> str:
    """
    Validate CUSTOMCYPHER expression for dangerous patterns.
    Raises ValueError if expression contains potentially dangerous Cypher keywords.
    """
    # Check for dangerous keywords as word boundaries
    for keyword in _DANGEROUS_CYPHER_KEYWORDS:
        keyword_re = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
        if keyword_re.search(expression):
            msg = f"Dimension '{dim_name}': CUSTOMCYPHER expression contains forbidden keyword '{keyword}'"
            raise ValueError(msg)

    # Block comment injection
    if "//" in expression or "/*" in expression:
        msg = f"Dimension '{dim_name}': CUSTOMCYPHER expression contains comment syntax"
        raise ValueError(msg)

    # Block shell/template injection patterns
    if "$$" in expression or "${" in expression:
        msg = f"Dimension '{dim_name}': CUSTOMCYPHER expression contains injection pattern"
        raise ValueError(msg)

    return expression


class ScoringAssembler:
    """Assembles scoring dimensions into Cypher WITH clause."""

    def __init__(
        self,
        domain_spec: DomainSpec,
        graph_driver: GraphDriver | None = None,
    ) -> None:
        self.domain_spec = domain_spec
        self.scoring_spec = domain_spec.scoring
        self._last_active_dims: list[str] = []
        self._last_effective_weights: dict[str, float] = {}
        self._query_params: dict[str, Any] = {}
        self._graph_driver = graph_driver
        self._learned_weights: dict[str, float] | None = None
        self._population_means: dict[str, float] = {}  # S2-01: cached population means
        self._negative_slope: float = 0.02  # S2-02: LeakyReLU alpha

    async def load_learned_weights(self) -> dict[str, float]:
        """Query DimensionWeight nodes from Neo4j for convergence loop.

        Returns a mapping of dimension_name -> learned weight adjustment factor.
        Falls back to empty dict if feedback loop is disabled or no weights exist.

        RU-022 / CEG-COR-030: the writer (``SignalWeightCalculator._store_weights``)
        keys DimensionWeight on ``{dimension, tenant}``. While
        ``settings.learned_weights_tenant_keyed`` is on (default) the reader
        filters on exactly those properties, with ``$tenant`` = the domain id;
        off emits the legacy ``dimension_name`` / ``domain_id`` reader, which
        matches nothing the writer stores.
        """
        from engine.config.settings import settings

        if not self._graph_driver:
            return {}

        fl_spec = self.domain_spec.feedbackloop
        if not fl_spec.enabled or not fl_spec.signal_weights.enabled:
            return {}

        domain_id = self.domain_spec.domain.id
        dim_names = [dim.name for dim in self.scoring_spec.dimensions]
        if not dim_names:
            return {}

        parameters: dict[str, Any]
        if settings.learned_weights_tenant_keyed:
            cypher = """
        MATCH (dw:DimensionWeight {tenant: $tenant})
        WHERE dw.dimension IN $names
        RETURN dw.dimension AS name, dw.weight AS weight
        """
            parameters = {"names": dim_names, "tenant": domain_id}
        else:
            cypher = """
        MATCH (dw:DimensionWeight)
        WHERE dw.dimension_name IN $names AND dw.domain_id = $domain_id
        RETURN dw.dimension_name AS name, dw.weight AS weight
        """
            parameters = {"names": dim_names, "domain_id": domain_id}
        results = await self._graph_driver.execute_query(
            cypher=cypher,
            parameters=parameters,
            database=domain_id,
        )
        if settings.learned_weight_renormalization:
            # RU-023: a learned 0.0 is a weight (it takes the penalty branch), not absence.
            self._learned_weights = {
                r["name"]: r["weight"] for r in results if r.get("name") and r.get("weight") is not None
            }
        else:
            self._learned_weights = {r["name"]: r["weight"] for r in results if r.get("name") and r.get("weight")}
        logger.info(
            "Loaded %d learned dimension weights for domain %s",
            len(self._learned_weights),
            domain_id,
        )
        return self._learned_weights

    def assemble_scoring_clause(
        self,
        match_direction: str,
        weights: dict[str, float],
        pareto_candidates: list[Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Assemble WITH clause for scoring.

        Args:
            match_direction: Direction for dimension filtering.
            weights: Weight overrides keyed by weightkey.
            pareto_candidates: Optional list of ParetoCandidate for pre-filtering.

        Returns:
            Tuple of (cypher_clause, pareto_metadata).
            pareto_metadata is None if Pareto disabled or not applicable,
            otherwise contains front_size, total_candidates, non_dominated_ids.
        """
        from engine.config.settings import settings

        self._query_params = {}
        pareto_metadata: dict[str, Any] | None = None

        # Pareto pre-filter (lazy import to avoid circular deps)
        if settings.pareto_enabled and pareto_candidates is not None and len(pareto_candidates) > 1:
            from engine.scoring.pareto import compute_pareto_front

            front = compute_pareto_front(pareto_candidates)
            logger.info(
                "Pareto pre-filter: %d/%d candidates non-dominated",
                front.front_size,
                len(pareto_candidates),
            )
            pareto_metadata = {
                "front_size": front.front_size,
                "total_candidates": len(pareto_candidates),
                "non_dominated_ids": [c.candidate_id for c in front.non_dominated],
                "dimension_names": front.dimension_names,
            }

        dimension_exprs: list[str] = []
        weight_entries: list[tuple[float, str]] = []  # (coefficient, alias)
        active_dim_names: list[str] = []
        # RU-023: indices of non-penalised dimensions and their pre-learning weights.
        rescalable: dict[int, float] = {}
        learned_multiplier_applied = False

        learned = self._learned_weights or {}
        sw_spec = self.domain_spec.feedbackloop.signal_weights if learned else None

        for dim in self.scoring_spec.dimensions:
            if dim.matchdirections and match_direction not in dim.matchdirections:
                continue
            expr = self._compile_dimension(dim)
            # W1-02: clamp each dimension expression to [0.0, 1.0] when enabled
            if settings.score_clamp_enabled:
                expr = self._clamp_expression(expr)
            alias = sanitize_label(dim.name)
            dimension_exprs.append(f"{expr} AS {alias}")
            weight = self._request_weight(dim, weights)
            pre_learning = weight
            penalised = False
            # Convergence loop: multiply spec weight by learned adjustment factor
            if dim.name in learned and sw_spec is not None:
                learned_w = learned[dim.name]
                # Negative penalty: when learned weight < penalty_threshold,
                # apply negative penalty instead of just reduced weight.
                # Implements "primitive subtraction" from Lippl et al.
                penalised = learned_w < sw_spec.penalty_threshold
                weight = -abs(sw_spec.penalty_factor) if penalised else weight * learned_w
                learned_multiplier_applied = learned_multiplier_applied or not penalised
            if not penalised:
                rescalable[len(weight_entries)] = pre_learning
            weight_entries.append((weight, alias))
            active_dim_names.append(dim.name)

        if settings.learned_weight_renormalization and learned_multiplier_applied:
            self._renormalize_learned(weight_entries, rescalable)
        weight_exprs = [f"({cypher_number(w)} * {a})" for w, a in weight_entries]
        # RU-032: the monoculture check reads the coefficients actually emitted.
        effective_weights = {name: w for name, (w, _a) in zip(active_dim_names, weight_entries, strict=True)}

        self._last_active_dims = list(active_dim_names)
        self._last_effective_weights = effective_weights
        all_exprs = ", ".join(dimension_exprs)
        score_expr = self._build_score_expression(weight_exprs)

        if all_exprs:
            dim_names_passthrough = ", ".join(active_dim_names)
            clause = f"WITH candidate, {all_exprs}\nWITH candidate, {dim_names_passthrough}, {score_expr} AS score"
        else:
            clause = f"WITH candidate, {score_expr} AS score"

        return clause, pareto_metadata

    @property
    def last_active_dimension_names(self) -> list[str]:
        """Return dimension names from the most recent assemble_scoring_clause call."""
        return list(self._last_active_dims)

    @staticmethod
    def _renormalize_learned(weight_entries: list[tuple[float, str]], rescalable: dict[int, float]) -> None:
        """RU-023 / CEG-COR-083 (C-22): rescale the non-penalised coefficients in place.

        Learned multipliers redistribute weight among the non-penalised
        dimensions; their sum is rescaled back to its pre-learning value so
        learning never raises the positive weight sum.
        """
        pre_sum = sum(rescalable.values())
        post_sum = sum(weight_entries[i][0] for i in rescalable)
        if post_sum > 0:
            scale = pre_sum / post_sum
            for i in rescalable:
                weight_entries[i] = (weight_entries[i][0] * scale, weight_entries[i][1])

    @staticmethod
    def _request_weight(dim: ScoringDimensionSpec, weights: dict[str, float]) -> Any:
        """The request override for this dimension's weightkey, else the spec default."""
        return weights.get(dim.weightkey, dim.defaultweight)

    def merged_request_weights(self, match_direction: str, weights: dict[str, float]) -> dict[str, Any]:
        """RU-009: per active dimension, the request-merged weight scoring will use.

        Same merge (``_request_weight``) and same direction filter as
        ``assemble_scoring_clause``, before any learned-weight adjustment, so the
        match weight-ceiling check validates what the score expression applies.
        Values are returned as supplied: the caller's validator parses them, so a
        non-numeric override surfaces as its ValidationError, not a ValueError here.
        """
        return {
            dim.name: self._request_weight(dim, weights)
            for dim in self.scoring_spec.dimensions
            if not (dim.matchdirections and match_direction not in dim.matchdirections)
        }

    @property
    def last_effective_weights(self) -> dict[str, float]:
        """Weight coefficient per active dimension emitted by the most recent assemble call.

        RU-032: request override or spec default, times any learned adjustment —
        exactly the number written into the score expression.
        """
        return dict(self._last_effective_weights)

    @property
    def last_query_params(self) -> dict[str, Any]:
        """Copy of Cypher parameters collected during the last assemble call."""
        return dict(self._query_params)

    def _compile_dimension(self, dim: ScoringDimensionSpec) -> str:
        """Dispatch to computation-specific compiler.

        Applies HGKR Pass 2 enhancements:
        - S2-01: Null inheritance strategy wrapping
        - S2-20: Cold-start fallback for applicable dimensions
        """
        dispatch = {
            ComputationType.GEODECAY: self._compile_geodecay,
            ComputationType.LOGNORMALIZED: self._compile_lognormalized,
            ComputationType.COMMUNITYMATCH: self._compile_communitymatch,
            ComputationType.INVERSELINEAR: self._compile_inverselinear,
            ComputationType.WEIGHTEDRATE: self._compile_weightedrate,
            ComputationType.PRICEALIGNMENT: self._compile_pricealignment,
            ComputationType.TEMPORALPROXIMITY: self._compile_temporalproximity,
            ComputationType.TRAVERSALALIAS: self._compile_traversalalias,
            ComputationType.KGE: self._compile_kge,
            ComputationType.VARIANTDISCOVERY: self._compile_variantdiscovery,
            ComputationType.ENSEMBLECONFIDENCE: self._compile_ensembleconfidence,
            ComputationType.PREFERENCEATTENTION: self._compile_preference_attention,
            ComputationType.COMMUNITYBRIDGE: self._compile_community_bridge,
        }

        if dim.computation == ComputationType.CANDIDATEPROPERTY:
            base_expr = self._compile_candidateproperty(dim)
        elif dim.computation == ComputationType.CUSTOMCYPHER:
            if not dim.expression:
                raise ValueError(f"Dimension '{dim.name}': customcypher requires 'expression'")
            base_expr = _validate_custom_expression(dim.expression, dim.name)
        else:
            compiler = dispatch.get(dim.computation)
            if compiler is None:
                logger.warning("Unknown computation type: %s", dim.computation)
                return str(dim.defaultwhennull)
            base_expr = compiler(dim)

        # S2-01: Apply null inheritance strategy
        base_expr = self._apply_null_strategy(dim, base_expr)

        # S2-20: Apply cold-start fallback for eligible dimensions
        if getattr(dim, "cold_start_fallback", None) and dim.cold_start_fallback != ColdStartFallback.ZERO:
            base_expr = self._apply_cold_start_fallback(dim, base_expr)

        return base_expr

    def _compile_geodecay(self, dim: ScoringDimensionSpec) -> str:
        """Distance decay between the candidate's and the query's coordinates.

        RU-037 / CEG-COR-011: with ``settings.geodecay_declared_longitude`` on
        (default) the longitude operands are the dimension's declared
        ``candidateprop_lon`` / ``queryprop_lon``; off ignores the declarations.

        Stage 2 follow-up (item 8): with
        ``declared_structural_properties_required_enabled`` on (default) the
        latitude (``candidateprop``) and longitude (``candidateprop_lon``)
        properties are the dimension's declarations, and the query operands
        default to those same declared names; an undeclared one raises
        ``UndeclaredStructuralPropertyError`` (the loader withholds such a pack).
        Off: the former ``lat`` / ``lon`` defaults.
        """
        from engine.config.settings import settings
        from engine.config.structural_vocabulary import declared, required

        k = cypher_number(dim.decayconstant or 50000.0)
        lat_prop = sanitize_label(
            declared(dim.candidateprop, "latitude", f"scoring {dim.name!r} (geodecay) declares no candidateprop")
        )
        query_lat_param = sanitize_label(dim.queryprop or lat_prop)
        lon_prop = "lon"
        query_lon_param = "lon"
        if settings.geodecay_declared_longitude:
            # getattr: duck-typed dimension doubles predate the fields (cf. soft_match).
            lon_prop = sanitize_label(
                declared(
                    getattr(dim, "candidateprop_lon", None),
                    "longitude",
                    f"scoring {dim.name!r} (geodecay) declares no candidateprop_lon",
                )
            )
            query_lon_default = lon_prop if required() else "lon"
            query_lon_param = sanitize_label(getattr(dim, "queryprop_lon", None) or query_lon_default)
        return (
            f"1.0 / (1.0 + point.distance("
            f"point({{latitude: candidate.{lat_prop}, longitude: candidate.{lon_prop}}}),"
            f" point({{latitude: ${query_lat_param}, longitude: ${query_lon_param}}})"
            f") / {k})"
        )

    def _compile_lognormalized(self, dim: ScoringDimensionSpec) -> str:
        max_val = cypher_number(dim.maxvalue or 1000.0)
        prop = sanitize_label(dim.candidateprop or "value")
        return f"log(1 + coalesce(candidate.{prop}, 0)) / log(1 + {max_val})"

    def _compile_communitymatch(self, dim: ScoringDimensionSpec) -> str:
        """Community match scoring.

        S2-17: When soft_match=True, uses graduated scoring based on community
        distance rather than binary match/no-match. Falls back to original
        binary logic when soft_match=False.

        Returns bias score if communities match, graduated score otherwise.
        Does not require APOC - uses native Cypher only.

        SI-19 / SI-20: with ``communitymatch_membership_scoring_enabled`` on, a
        community id is a label: only membership equality scores, so
        ``soft_match`` compiles exactly like the binary match (no partial
        credit from the numeric difference of two ids), and an undeclared
        ``bias`` is 1.0 (full credit for a match) instead of a hidden 1.5 the
        score clamp cut to 1.0. A declared ``bias`` (including 0.0) is used
        as declared. Off: legacy.
        """
        from engine.config.settings import settings
        from engine.config.structural_vocabulary import declared, required

        membership = settings.communitymatch_membership_scoring_enabled
        if membership:
            bias = cypher_number(dim.bias if dim.bias is not None else 1.0)
        else:
            bias = cypher_number(dim.bias or 1.5)
        # Stage 2 follow-up (item 8): the community property is the domain's
        # declaration; the query parameter defaults to the same declared name.
        cand_prop = sanitize_label(
            declared(dim.candidateprop, "community", f"scoring {dim.name!r} (communitymatch) declares no candidateprop")
        )
        query_prop = sanitize_label(dim.queryprop or (cand_prop if required() else "community_id"))

        if getattr(dim, "soft_match", False) and not membership:
            # S2-17: Graduated community scoring — Hierarchical Attention Network pattern.
            # Uses absolute community ID difference as proxy for dendrogram distance.
            # Closer community IDs ≈ closer in hierarchy → higher partial credit.
            return (
                f"CASE "
                f"  WHEN candidate.{cand_prop} IS NULL OR ${query_prop} IS NULL THEN 0.5 "
                f"  WHEN candidate.{cand_prop} = ${query_prop} THEN {bias} "
                f"  ELSE toFloat({bias}) / (1.0 + abs(candidate.{cand_prop} - ${query_prop})) "
                f"END"
            )

        return (
            f"CASE "
            f"  WHEN candidate.{cand_prop} IS NULL OR ${query_prop} IS NULL THEN 0.5 "
            f"  WHEN candidate.{cand_prop} = ${query_prop} THEN {bias} "
            f"  ELSE 0.2 "
            f"END"
        )

    def _compile_inverselinear(self, dim: ScoringDimensionSpec) -> str:
        min_val = cypher_number(dim.minvalue or 0.0)
        max_val = cypher_number(dim.maxvalue or 100.0)
        prop = sanitize_label(dim.candidateprop or "value")
        return f"1.0 - (coalesce(candidate.{prop}, {max_val}) - {min_val}) / ({max_val} - {min_val})"

    def _compile_candidateproperty(self, dim: ScoringDimensionSpec) -> str:
        """C-06 FIX: defaultwhennull emitted as validated numeric literal."""
        default = float(dim.defaultwhennull)
        prop = sanitize_label(dim.candidateprop or "value")
        return f"coalesce(candidate.{prop}, {default})"

    def _compile_weightedrate(self, dim: ScoringDimensionSpec) -> str:
        rate_prop = sanitize_label(dim.candidateprop or "rate")
        confidence_prop = sanitize_label(dim.queryprop or "confidence")
        default = float(dim.defaultwhennull)
        return f"coalesce(candidate.{rate_prop}, {default}) * coalesce(candidate.{confidence_prop}, 1.0)"

    def _compile_pricealignment(self, dim: ScoringDimensionSpec) -> str:
        """Log-ratio distance preferred over linear for multi-order-of-magnitude pricing.

        Formula: 1 - |log(candidate_p / target_p)| / tau
        Handles SaaS $10/mo to $10k/mo ranges appropriately.

        RU-017 / CEG-COR-006: the ratio is computed in floating point. Cypher
        divides two INTEGER operands as INTEGER (450 / 500 -> 0), so both
        operands are wrapped in ``toFloat()`` while
        ``settings.pricealignment_float_division`` is on (default). Off emits
        the legacy ``candidate.<p> / $<q>`` text for C-21 rollback.
        """
        from engine.config.settings import settings

        cand_prop = sanitize_label(dim.candidateprop or "price_per_unit")
        query_prop = sanitize_label(dim.queryprop or "target_price")
        tau = cypher_number(dim.maxvalue or 2.0)  # tolerance: 2.0 = ~7.4x ratio scores 0
        default = float(dim.defaultwhennull)
        if settings.pricealignment_float_division:
            ratio = f"toFloat(candidate.{cand_prop}) / toFloat(${query_prop})"
        else:
            ratio = f"candidate.{cand_prop} / ${query_prop}"
        return (
            f"CASE "
            f"  WHEN ${query_prop} IS NULL OR ${query_prop} <= 0 THEN {default} "
            f"  WHEN candidate.{cand_prop} IS NULL OR candidate.{cand_prop} <= 0 THEN {default} "
            f"  ELSE toFloat(1.0 - abs(log({ratio})) / {tau}) "
            f"END"
        )

    def _compile_temporalproximity(self, dim: ScoringDimensionSpec) -> str:
        """Multi-signal temporal scoring.

        score = w1 * recency_decay + w2 * touch_frequency + w3 * acceleration_flag

        Reads last_activity_date, touch_count_30d, and is_accelerating from node.

        CEG-FEEDBACK-1 (FEATURE_GATES §37): with
        ``settings.temporalproximity_declared_signals_enabled`` on, the
        auxiliary signals are only those the dimension declares in
        ``metadata`` (``_compile_temporalproximity_declared``); off keeps the
        legacy fixed weights below.
        """
        from engine.config.settings import settings

        if settings.temporalproximity_declared_signals_enabled:
            return self._compile_temporalproximity_declared(dim)
        date_prop = sanitize_label(dim.candidateprop or "last_activity_date")
        decay_days = cypher_number(dim.maxvalue or 90.0)
        default = float(dim.defaultwhennull)
        # Weights for 3 signals
        w1, w2, w3 = 0.6, 0.25, 0.15
        return (
            f"CASE WHEN candidate.{date_prop} IS NULL THEN {default} "
            f"ELSE ("
            f"  {w1} * exp(-1.0 * duration.inDays(candidate.{date_prop}, datetime()).days / {decay_days})"
            f"  + {w2} * toFloat(coalesce(candidate.touch_count_30d, 0)) / 30.0"
            f"  + {w3} * toFloat(coalesce(candidate.is_accelerating, 0))"
            f") END"
        )

    def _compile_temporalproximity_declared(self, dim: ScoringDimensionSpec) -> str:
        """temporalproximity over the signals the dimension declares (FEATURE_GATES §37).

        score = w_r * recency_decay + w_f * min(frequency / scale, 1) + w_a * acceleration

        ``metadata.frequency_property`` / ``metadata.acceleration_property`` name
        the auxiliary candidate properties; each contributes only when declared,
        with ``frequency_weight`` (default 0.25) / ``acceleration_weight``
        (default 0.15) and ``frequency_scale`` (default 30.0, the count that
        scores 1.0). Recency takes the remaining weight ``1 - w_f - w_a``, so a
        dimension declaring no auxiliary signal is pure recency decay in
        [0, 1]. A weight or scale declared without its property, or auxiliary
        weights summing past 1.0, is refused. Weights and the scale bind as
        query parameters; every property name passes ``sanitize_label`` (C-9).
        """
        metadata = dimension_metadata(dim)
        for key, prop_key in (
            ("frequency_weight", "frequency_property"),
            ("frequency_scale", "frequency_property"),
            ("acceleration_weight", "acceleration_property"),
        ):
            if key in metadata and prop_key not in metadata:
                msg = f"Dimension '{dim.name}': temporalproximity metadata.{key} requires metadata.{prop_key}"
                raise ValueError(msg)

        date_prop = sanitize_label(dim.candidateprop or "last_activity_date")
        decay_days = cypher_number(dim.maxvalue or 90.0)
        default = float(dim.defaultwhennull)
        safe_dim = sanitize_label(dim.name)

        terms: list[str] = []
        aux_weight = 0.0
        if "frequency_property" in metadata:
            freq_prop = sanitize_label(metadata["frequency_property"])
            freq_weight = cypher_number(metadata.get("frequency_weight", 0.25))
            freq_scale = cypher_number(metadata.get("frequency_scale", 30.0))
            if freq_scale <= 0:
                msg = f"Dimension '{dim.name}': temporalproximity metadata.frequency_scale must be > 0"
                raise ValueError(msg)
            weight_key, scale_key = f"tp_freq_w_{safe_dim}", f"tp_freq_scale_{safe_dim}"
            self._query_params[weight_key] = freq_weight
            self._query_params[scale_key] = freq_scale
            aux_weight += freq_weight
            freq = f"toFloat(coalesce(candidate.{freq_prop}, 0)) / ${scale_key}"
            terms.append(f"${weight_key} * CASE WHEN {freq} > 1.0 THEN 1.0 WHEN {freq} < 0.0 THEN 0.0 ELSE {freq} END")
        if "acceleration_property" in metadata:
            accel_prop = sanitize_label(metadata["acceleration_property"])
            accel_weight = cypher_number(metadata.get("acceleration_weight", 0.15))
            weight_key = f"tp_accel_w_{safe_dim}"
            self._query_params[weight_key] = accel_weight
            aux_weight += accel_weight
            terms.append(
                f"${weight_key} * CASE WHEN candidate.{accel_prop} = true OR candidate.{accel_prop} = 1 "
                f"THEN 1.0 ELSE 0.0 END"
            )
        if aux_weight > 1.0 + 1e-9:
            msg = (
                f"Dimension '{dim.name}': temporalproximity auxiliary signal weights sum to {aux_weight:.4f}, "
                "exceeding 1.0"
            )
            raise ValueError(msg)
        recency_key = f"tp_recency_w_{safe_dim}"
        self._query_params[recency_key] = max(0.0, 1.0 - aux_weight)
        recency = f"${recency_key} * exp(-1.0 * duration.inDays(candidate.{date_prop}, datetime()).days / {decay_days})"
        body = " + ".join([recency, *terms])
        return f"CASE WHEN candidate.{date_prop} IS NULL THEN {default} ELSE ({body}) END"

    def _compile_traversalalias(self, dim: ScoringDimensionSpec) -> str:
        """Read a property from a traversal step alias."""
        if not dim.alias:
            raise ValueError(f"Dimension '{dim.name}': traversalalias requires 'alias' field")
        alias = sanitize_label(dim.alias)
        prop = sanitize_label(dim.candidateprop or "score")
        default = float(dim.defaultwhennull)
        return f"coalesce({alias}.{prop}, {default})"

    def _compile_kge(self, dim: ScoringDimensionSpec) -> str:
        """KGE embedding similarity score (CompoundE3D)."""
        default = float(dim.defaultwhennull)
        if dim.alias:
            alias = sanitize_label(dim.alias)
            prop = sanitize_label(dim.candidateprop or "kge_score")
            return f"coalesce({alias}.{prop}, {default})"
        if dim.candidateprop:
            prop = sanitize_label(dim.candidateprop)
            return f"coalesce(candidate.{prop}, {default})"
        raise ValueError(f"Dimension '{dim.name}': kge requires 'alias' or 'candidateprop'")

    def _compile_variantdiscovery(self, dim: ScoringDimensionSpec) -> str:
        """Variant discovery score from BeamSearchEngine.

        Reads pre-computed variant_discovery_score from candidate node.
        This score represents novel scoring dimensions discovered autonomously
        through beam search over CompoundE3D geometric space.

        The score is written to Neo4j by BeamSearchEngine.search() and
        consumed here during match scoring.

        Ref: engine/kge/beam_search.py, Deep Research Directive §8
        """
        default = float(dim.defaultwhennull)
        prop = sanitize_label(dim.candidateprop or "variant_discovery_score")
        if dim.alias:
            alias = sanitize_label(dim.alias)
            return f"coalesce({alias}.{prop}, {default})"
        return f"coalesce(candidate.{prop}, {default})"

    def _compile_ensembleconfidence(self, dim: ScoringDimensionSpec) -> str:
        """Ensemble confidence from multi-variant fusion.

        Reads pre-computed ensemble_confidence from candidate node.
        This score indicates inference robustness from WDS, Borda count,
        or Mixture-of-Experts ensemble fusion.

        Higher confidence = more agreement across KGE variants.
        Lower confidence = divergent predictions (higher uncertainty).

        The score is written to Neo4j by EnsembleController.predict() and
        consumed here during match scoring.

        Ref: engine/kge/ensemble.py, Deep Research Directive §9
        """
        default = float(dim.defaultwhennull)
        prop = sanitize_label(dim.candidateprop or "ensemble_confidence")
        if dim.alias:
            alias = sanitize_label(dim.alias)
            return f"coalesce({alias}.{prop}, {default})"
        return f"coalesce(candidate.{prop}, {default})"

    def _compile_preference_attention(self, dim: ScoringDimensionSpec) -> str:
        """Preference-attention scoring inspired by HGKR knowledge-perceiving filter.

        Traverses from query_entity through configurable outcome relationship,
        finds successful outcomes, samples top-K by recency, computes
        community-overlap between candidate and each outcome, and returns
        normalized weighted score.

        Properties from domain spec YAML (via dim.metadata):
        - outcome_relation (default: the domain's feedbackloop.outcome_edge_type)
        - outcome_node (default: the domain's feedbackloop.outcome_node_label)
        - success_property (default: "outcome_type")
        - success_value (REQUIRED — which outcome value counts as success is
          business semantics the domain must declare; there is no engine default)

        Ref: Liu et al., Scientific Reports (2023) 13:6987, §3.2 preference attention.
        """
        default = float(dim.defaultwhennull)
        sample_k = (
            self.scoring_spec.preference_sample_size if hasattr(self.scoring_spec, "preference_sample_size") else 28
        )

        # Outcome vocabulary comes from the domain spec; only the structural
        # property name has an engine default.
        metadata = dimension_metadata(dim)
        feedback = self.domain_spec.feedbackloop
        outcome_rel = sanitize_label(metadata.get("outcome_relation", feedback.outcome_edge_type))
        success_prop = sanitize_label(metadata.get("success_property", "outcome_type"))
        success_value = metadata.get("success_value")
        if success_value is None:
            from engine.config.outcome_vocabulary import preference_attention_success_value_defect

            raise ValueError(preference_attention_success_value_defect(dim.name))
        if not isinstance(success_value, str):
            msg = f"Dimension '{dim.name}': success_value must be a string"
            raise ValueError(msg)
        # The outcome node is metadata.outcome_node, else the domain's declared
        # feedbackloop.outcome_node_label (no engine default; Stage 2 follow-up).
        if "outcome_node" in metadata:
            outcome_node = sanitize_label(metadata["outcome_node"])
        else:
            from engine.config.outcome_vocabulary import outcome_node_label

            outcome_node = sanitize_label(
                outcome_node_label(self.domain_spec, f"preference_attention dimension '{dim.name}'")
            )

        from engine.config.settings import settings
        from engine.config.structural_vocabulary import declared

        # Stage 2 follow-up (item 8): the candidate's community property is declared.
        cand_community_prop = sanitize_label(
            declared(
                dim.candidateprop, "community", f"scoring {dim.name!r} (preference_attention) declares no candidateprop"
            )
        )
        # Item 7 / M5b: the outcome nodes' community property is the declared
        # metadata.outcome_community_property, defaulting to the candidate's
        # declared community property. Off: the hardcoded `community_id` (C-21
        # rollback of preference_attention_declared_community_enabled).
        if settings.preference_attention_declared_community_enabled:
            outcome_community_prop = sanitize_label(metadata.get("outcome_community_property") or cand_community_prop)
        else:
            outcome_community_prop = "community_id"
        safe_dim = sanitize_label(dim.name)
        success_key = f"pref_success_{safe_dim}"
        default_key = f"pref_default_{safe_dim}"
        sample_key = f"pref_sample_k_{safe_dim}"
        self._query_params[success_key] = success_value
        self._query_params[default_key] = default
        self._query_params[sample_key] = int(sample_k)

        # RU-021 / CEG-COR-082: the ratio is (#community matches within the K
        # most recent successes) / (size of that sample). The sample is taken
        # once — successes ordered by created_at DESC (undated last, elementId
        # tie-break), first K — and the community filter runs inside it. The
        # former expression sliced the filtered list and the unfiltered list
        # separately, i.e. min(K, matches) / min(K, total) (100 successes, 50
        # matching, K=28 scored 1.0 instead of 0.5). Variables are dimension-
        # scoped so the COLLECT subquery cannot correlate with an outer name.
        sample_var = f"pa_sample_{safe_dim}"
        outcome_var = f"pa_o_{safe_dim}"
        member_var = f"pa_m_{safe_dim}"
        # Labels and relationship type above passed sanitize_label() (C-9).
        success_path = f"(qe)-[:{outcome_rel}]->({outcome_var}:{outcome_node})"
        return (
            f"[{sample_var} IN [COLLECT {{ "
            f"MATCH {success_path} "
            f"WHERE {outcome_var}.{success_prop} = ${success_key} "
            f"RETURN {outcome_var} "
            f"ORDER BY {outcome_var}.created_at IS NULL, {outcome_var}.created_at DESC, elementId({outcome_var}) "
            f"LIMIT ${sample_key} }}] | "
            f"CASE WHEN size({sample_var}) = 0 THEN ${default_key} "
            f"ELSE toFloat(size([{member_var} IN {sample_var} "
            f"WHERE {member_var}.{outcome_community_prop} = candidate.{cand_community_prop}])) "
            f"/ toFloat(size({sample_var})) END][0]"
        )

    def _compile_community_bridge(self, dim: ScoringDimensionSpec) -> str:
        """Multi-community bridge scoring for cross-graph overlap.

        Reads multiple community_id properties (from different per-relation
        Louvain runs), counts how many match between candidate and query_entity,
        and returns score = matching / total community properties.

        NULL-safe: NULL community values treated as non-match.

        Properties from domain spec YAML (via dim.metadata or defaults):
        - community_properties: list[str] (e.g., ["community_id_supply", "community_id_geo"])

        Ref: Liu et al., Scientific Reports (2023) 13:6987, §3.3 cross-graph propagation.
        """
        default = float(dim.defaultwhennull)

        # Extract community properties from metadata or use sensible defaults
        metadata = dimension_metadata(dim)
        community_props = metadata.get("community_properties", [])
        if not community_props:
            # Fall back to the declared candidateprop (Stage 2 follow-up, item 8:
            # no borrowed community property).
            from engine.config.structural_vocabulary import community_bridge_defect, declared

            community_props = [declared(dim.candidateprop, "community", community_bridge_defect(dim.name))]

        # Build CASE expressions for each community property
        match_cases = []
        for prop in community_props:
            safe_prop = sanitize_label(prop)
            match_cases.append(
                f"CASE "
                f"WHEN candidate.{safe_prop} IS NOT NULL "
                f"AND $query_{safe_prop} IS NOT NULL "
                f"AND candidate.{safe_prop} = $query_{safe_prop} "
                f"THEN 1 ELSE 0 END"
            )

        total = len(community_props)
        if total == 0:
            return str(default)

        sum_expr = " + ".join(match_cases)
        return f"CASE WHEN {total} = 0 THEN {default} ELSE toFloat({sum_expr}) / {total}.0 END"

    @staticmethod
    def _clamp_expression(expr: str) -> str:
        """W1-02: Wrap a Cypher expression in a CASE-based clamp to [0.0, 1.0].

        Prevents unbounded scores from propagating through the scoring pipeline.
        Mirrors seL4's invariant enforcement on all capability-transfer outputs.

        A null dimension is outside [0.0, 1.0] too: a candidate missing a scored
        property (``point.distance`` on a null ``lat``, a freshness date never
        synced) made the whole weighted ``score`` null, which the handler then
        tried to rank. Null clamps to 0.0, the ``NullStrategy.ZERO`` contract.

        RU-031 / CEG-COR-166: NaN (``log`` of a negative argument, ``0.0 / 0.0``)
        compares false to both bounds and so fell through to ELSE. It is treated
        like null and clamps to 0.0; the test follows IS NULL so ``isNaN`` never
        receives a null.
        """
        return (
            f"CASE WHEN ({expr}) IS NULL THEN 0.0 WHEN isNaN(({expr})) THEN 0.0 "
            f"WHEN ({expr}) < 0.0 THEN 0.0 "
            f"WHEN ({expr}) > 1.0 THEN 1.0 ELSE ({expr}) END"
        )

    def _apply_null_strategy(self, dim: ScoringDimensionSpec, base_expr: str) -> str:
        """S2-01: Apply null inheritance strategy to dimension expression.

        HGKR Eq. 4: non-participant entities inherit prior embeddings.
        - ZERO: score 0.0 when NULL (current default behavior)
        - INHERIT_PRIOR: carry forward candidate's last known score for dimension
        - POPULATION_MEAN: substitute the population mean score
        """
        strategy = getattr(dim, "null_strategy", NullStrategy.ZERO)
        if strategy == NullStrategy.ZERO:
            return base_expr  # No change — coalesce(..., 0.0) already handles this
        if strategy == NullStrategy.INHERIT_PRIOR:
            prop_name = sanitize_label(f"_prior_{dim.name}")
            return f"coalesce(({base_expr}), candidate.{prop_name}, {cypher_number(dim.defaultwhennull)})"
        if strategy == NullStrategy.POPULATION_MEAN:
            # Population mean is injected as a parameter at query time
            param = sanitize_label(f"_popmean_{dim.name}")
            return f"coalesce(({base_expr}), ${param}, {cypher_number(dim.defaultwhennull)})"
        return base_expr

    def _apply_cold_start_fallback(self, dim: ScoringDimensionSpec, base_expr: str) -> str:
        """S2-20: Cold-start fallback for preference_attention-type dimensions.

        HGKR paper's core motivation: KGs solve cold-start via structural signals.
        When a candidate has zero historical outcomes, fall back to graph-structural
        similarity (shared neighbors count) or population mean.
        """
        fallback = getattr(dim, "cold_start_fallback", ColdStartFallback.ZERO)
        if fallback == ColdStartFallback.ZERO:
            return base_expr
        if fallback == ColdStartFallback.STRUCTURAL_SIMILARITY:
            # Adamic-Adar proxy: count shared neighbors as structural similarity
            return (
                f"CASE WHEN ({base_expr}) IS NULL OR ({base_expr}) = 0.0 "
                f"THEN coalesce(candidate.shared_neighbor_score, 0.1) "
                f"ELSE ({base_expr}) END"
            )
        if fallback == ColdStartFallback.POPULATION_MEAN:
            param = sanitize_label(f"_popmean_{dim.name}")
            return (
                f"CASE WHEN ({base_expr}) IS NULL OR ({base_expr}) = 0.0 "
                f"THEN coalesce(${param}, 0.1) "
                f"ELSE ({base_expr}) END"
            )
        return base_expr

    @staticmethod
    def _leaky_relu(expr: str, negative_slope: float = 0.02) -> str:
        """S2-02: LeakyReLU activation between propagation passes.

        HGKR Eq. 5: LeakyReLU with alpha=0.02 prevents signal collapse.
        Standard ReLU zeros out negative signals permanently; LeakyReLU
        allows weak negative contribution.
        """
        return f"CASE WHEN ({expr}) >= 0 THEN ({expr}) ELSE {negative_slope} * ({expr}) END"

    def _build_score_expression(self, weight_exprs: list[str]) -> str:
        if not weight_exprs:
            return "0.0"
        return " + ".join(weight_exprs)
