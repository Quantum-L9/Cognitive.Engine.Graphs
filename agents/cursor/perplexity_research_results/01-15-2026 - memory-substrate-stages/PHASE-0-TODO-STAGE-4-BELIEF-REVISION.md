<!-- L9_META
l9_schema: 2
origin: engine-specific
engine: graph
layer: [docs]
tags: [governance]
status: active
/L9_META -->

# Phase 0 TODO Plan: Stage 4 - Belief Revision System

**GMP ID:** GMP-STAGE4-BELIEF-REVISION
**Tier:** KERNEL_TIER (memory substrate core)
**Date:** 2026-01-15
**Status:** PHASE 0 - PLAN LOCKED

---

## Scope Boundaries

### IN SCOPE
- `ExplanationEngine` - Contradiction detection + causal explanation
- `ConflictResolver` - Multi-strategy resolution with persistence
- Data models in existing `substrate_models.py`
- PostgreSQL migration for belief revision audit tables
- Integration with existing `MemorySubstrateService`
- Unit and integration tests

### OUT OF SCOPE
- Stage 5 (Predictive Warming) - separate GMP
- Stage 6 (Multi-Agent Consensus) - separate GMP
- Neo4j graph changes (use existing PacketStore)
- UI/API changes (memory internals only)

---

## Files to Modify

### 1. Extend Existing Models
**File:** `memory/substrate_models.py`
**Action:** INSERT at end (before closing comments)
**Lines:** ~460+ (after existing models)

```python
# Stage 4: Belief Revision Models (GMP-STAGE4)

class ContradictionType(str, Enum):
    """Types of contradictions between facts."""
    DIRECT = "direct"
    SEMANTIC = "semantic"
    TEMPORAL = "temporal"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class ResolutionStrategy(str, Enum):
    """Strategies for resolving belief conflicts."""
    REPLACE = "replace"
    BRANCH = "branch"
    MERGE = "merge"
    DEFER = "defer"
    IGNORE = "ignore"


class ConflictingFactPair(BaseModel):
    """Pair of facts in conflict."""
    fact_a_id: UUID
    fact_b_id: UUID
    contradiction_type: ContradictionType
    semantic_similarity: float = Field(ge=0.0, le=1.0)
    entity_overlap: float = Field(ge=0.0, le=1.0)
    detected_at: datetime


class ConflictExplanation(BaseModel):
    """Explanation of conflict with resolution recommendation."""
    explanation_id: UUID = Field(default_factory=uuid4)
    conflict_pair_id: UUID
    contradiction_type: ContradictionType
    causal_analysis: str
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_resolution: ResolutionStrategy
    resolution_reasoning: str
    generated_at: datetime


class ResolutionRecord(BaseModel):
    """Audit record of resolution execution."""
    resolution_id: UUID = Field(default_factory=uuid4)
    explanation_id: UUID
    selected_strategy: ResolutionStrategy
    executed_at: datetime
    fact_a_id: UUID
    fact_b_id: UUID
    outcome_fact_id: Optional[UUID] = None
    context_metadata: dict = Field(default_factory=dict)


class BeliefResolutionAuditRow(BaseModel):
    """DTO for belief_resolution_audit table."""
    audit_id: UUID
    resolution_id: UUID
    timestamp: datetime
    strategy: str
    fact_a_id: UUID
    fact_b_id: UUID
    outcome_fact_id: Optional[UUID] = None
    metadata: dict = Field(default_factory=dict)
```

---

### 2. Create ExplanationEngine
**File:** `memory/explanation_engine.py` (NEW)
**Action:** CREATE

```
memory/explanation_engine.py (NEW FILE)
├── Imports: structlog, uuid, datetime, numpy, Optional, Any
├── from memory.substrate_models import ContradictionType, ConflictingFactPair, ConflictExplanation
├── from memory.substrate_semantic import SemanticService (for embeddings)
├── from core.observability.circuit_breaker import CircuitBreaker
│
├── class ExplanationEngine:
│   ├── __init__(semantic_service, llm_client, threshold_config)
│   ├── async detect_contradictions(new_fact, existing_facts) -> list[ConflictingFactPair]
│   │   ├── Phase 1: Semantic similarity via embeddings
│   │   └── Phase 2: Entity overlap analysis
│   ├── async generate_explanation(pair, fact_a, fact_b) -> ConflictExplanation
│   │   ├── Build analysis prompt
│   │   ├── Call LLM for causal analysis
│   │   ├── Parse resolution recommendation
│   │   └── Calibrate confidence
│   ├── _classify_contradiction_type(fact_a, fact_b, overlap) -> ContradictionType
│   ├── _compute_cosine_similarity(vec_a, vec_b) -> float
│   ├── _compute_entity_overlap(entities_a, entities_b) -> float
│   └── _calibrate_confidence(similarity, overlap, authority_a, authority_b) -> float
│
└── def get_explanation_engine() -> ExplanationEngine  (singleton factory)
```

**Estimated Lines:** 250-300

---

### 3. Create ConflictResolver
**File:** `memory/conflict_resolver.py` (NEW)
**Action:** CREATE

```
memory/conflict_resolver.py (NEW FILE)
├── Imports: structlog, uuid, datetime, json
├── from memory.substrate_models import ResolutionStrategy, ResolutionRecord, BeliefResolutionAuditRow
├── from memory.substrate_repository import SubstrateRepository
├── from memory.explanation_engine import ExplanationEngine
├── from telemetry.memory_metrics import record_belief_resolution
│
├── class ConflictResolver:
│   ├── __init__(repository, explanation_engine, audit_batch_size=100)
│   ├── async process_new_fact(fact, auto_resolve=True) -> ResolutionRecord
│   │   ├── Query existing facts from repository
│   │   ├── Detect contradictions via ExplanationEngine
│   │   ├── Generate explanations for each conflict
│   │   ├── Apply resolution strategy
│   │   └── Log to audit trail
│   ├── async _apply_resolution(explanation, new_fact, existing_fact) -> ResolutionRecord
│   ├── async _execute_replace_strategy(new_fact, existing_fact) -> Fact
│   ├── async _execute_branch_strategy(new_fact, existing_fact) -> Fact
│   ├── async _execute_merge_strategy(new_fact, existing_fact, explanation) -> Fact
│   ├── async _log_resolution_to_audit(record) -> None
│   └── async _flush_audit_buffer() -> None
│
└── def get_conflict_resolver() -> ConflictResolver  (singleton factory)
```

**Estimated Lines:** 300-350

---

### 4. Database Migration
**File:** `migrations/0021_belief_revision.sql` (NEW)
**Action:** CREATE

```sql
-- Migration 0021: Belief Revision System (Stage 4)
-- GMP: GMP-STAGE4-BELIEF-REVISION

-- Belief resolution audit table
CREATE TABLE IF NOT EXISTS belief_resolution_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resolution_id UUID NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy VARCHAR(50) NOT NULL,
    fact_a_id UUID NOT NULL,
    fact_b_id UUID NOT NULL,
    outcome_fact_id UUID,
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Multi-tenant columns (RLS)
    tenant_id UUID NOT NULL,
    org_id UUID NOT NULL,
    user_id UUID NOT NULL,

    CONSTRAINT belief_audit_strategy_check CHECK (strategy IN ('replace', 'branch', 'merge', 'defer', 'ignore'))
);

-- Indexes
CREATE INDEX idx_belief_audit_resolution ON belief_resolution_audit(resolution_id);
CREATE INDEX idx_belief_audit_facts ON belief_resolution_audit(fact_a_id, fact_b_id);
CREATE INDEX idx_belief_audit_timestamp ON belief_resolution_audit(timestamp DESC);
CREATE INDEX idx_belief_audit_tenant ON belief_resolution_audit(tenant_id, org_id, user_id);

-- RLS Policy (match existing patterns)
ALTER TABLE belief_resolution_audit ENABLE ROW LEVEL SECURITY;

CREATE POLICY belief_audit_tenant_isolation ON belief_resolution_audit
    USING (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        AND org_id = current_setting('app.org_id', true)::uuid
        AND (
            user_id = current_setting('app.user_id', true)::uuid
            OR current_setting('app.role', true) IN ('platform_admin', 'tenant_admin', 'org_admin')
        )
    );

-- Add contradiction_count column to semantic_facts if not exists (for tracking)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'semantic_facts' AND column_name = 'contradiction_count'
    ) THEN
        ALTER TABLE semantic_facts ADD COLUMN contradiction_count INTEGER DEFAULT 0;
    END IF;
END $$;
```

---

### 5. Extend SubstrateRepository
**File:** `memory/substrate_repository.py`
**Action:** INSERT new methods

```python
# ADD to SubstrateRepository class (after existing methods, ~line 400+)

async def get_conflicting_facts_for_subject(
    self,
    subject: str,
    entity_type: Optional[str] = None,
    limit: int = 50,
) -> list[SemanticFactRow]:
    """Get facts that may conflict with a given subject."""
    ...

async def insert_belief_resolution_audit(
    self,
    resolution_id: UUID,
    strategy: str,
    fact_a_id: UUID,
    fact_b_id: UUID,
    outcome_fact_id: Optional[UUID] = None,
    metadata: Optional[dict] = None,
) -> BeliefResolutionAuditRow:
    """Insert belief resolution audit record."""
    ...

async def update_fact_contradiction_count(
    self,
    fact_id: UUID,
    increment: int = 1,
) -> None:
    """Increment contradiction count for a fact."""
    ...

async def get_resolution_audit_history(
    self,
    fact_id: Optional[UUID] = None,
    strategy: Optional[str] = None,
    limit: int = 100,
) -> list[BeliefResolutionAuditRow]:
    """Get resolution audit history."""
    ...
```

**Estimated Lines:** 80-100 additional

---

### 6. Integrate into MemorySubstrateService
**File:** `memory/substrate_service.py`
**Action:** INSERT (lazy init pattern, ~line 115, plus new methods ~line 1050+)

```python
# ADD to __init__ (line ~115, after _retention_engine)
self._explanation_engine: Optional[ExplanationEngine] = None
self._conflict_resolver: Optional[ConflictResolver] = None

# ADD accessor methods (after get_retention_engine, ~line 940)
def get_explanation_engine(self) -> ExplanationEngine:
    """Get explanation engine instance (lazy initialization)."""
    if self._explanation_engine is not None:
        return self._explanation_engine

    logger.info("Initializing explanation_engine...")
    from memory.explanation_engine import ExplanationEngine
    self._explanation_engine = ExplanationEngine(
        semantic_service=self._semantic_service,
        # llm_client injected at runtime
    )
    logger.info("explanation_engine loaded successfully")
    return self._explanation_engine

def get_conflict_resolver(self) -> ConflictResolver:
    """Get conflict resolver instance (lazy initialization)."""
    if self._conflict_resolver is not None:
        return self._conflict_resolver

    logger.info("Initializing conflict_resolver...")
    from memory.conflict_resolver import ConflictResolver
    explanation_engine = self.get_explanation_engine()
    self._conflict_resolver = ConflictResolver(
        repository=self._repository,
        explanation_engine=explanation_engine,
    )
    logger.info("conflict_resolver loaded successfully")
    return self._conflict_resolver

# ADD high-level API method
async def resolve_belief_conflicts(
    self,
    fact_content: str,
    entity_type: str,
    source_id: str,
    auto_resolve: bool = True,
) -> dict[str, Any]:
    """
    Process a new fact through belief revision pipeline.

    Detects contradictions with existing facts, generates explanations,
    and applies resolution strategies.
    """
    ...
```

---

### 7. Add Telemetry
**File:** `telemetry/memory_metrics.py`
**Action:** INSERT new metrics

```python
# ADD after existing metrics (~line 50+)

BELIEF_RESOLUTIONS = Counter(
    "memory_belief_resolutions_total",
    "Total belief conflict resolutions",
    ["strategy", "contradiction_type"],
)

CONTRADICTION_DETECTIONS = Counter(
    "memory_contradictions_detected_total",
    "Total contradictions detected",
    ["contradiction_type"],
)

BELIEF_RESOLUTION_LATENCY = Histogram(
    "memory_belief_resolution_latency_seconds",
    "Time to resolve belief conflicts",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
)

def record_belief_resolution(
    strategy: str,
    contradiction_type: str,
    latency_seconds: float,
) -> None:
    """Record belief resolution metrics."""
    BELIEF_RESOLUTIONS.labels(strategy=strategy, contradiction_type=contradiction_type).inc()
    BELIEF_RESOLUTION_LATENCY.observe(latency_seconds)

def record_contradiction_detection(contradiction_type: str) -> None:
    """Record contradiction detection."""
    CONTRADICTION_DETECTIONS.labels(contradiction_type=contradiction_type).inc()
```

---

### 8. Unit Tests
**File:** `tests/memory/test_belief_revision.py` (NEW)
**Action:** CREATE

```
tests/memory/test_belief_revision.py (NEW FILE)
├── Fixtures:
│   ├── mock_semantic_service
│   ├── mock_repository
│   ├── sample_facts
│   └── sample_conflicts
│
├── class TestContradictionDetection:
│   ├── test_detect_direct_contradiction
│   ├── test_detect_semantic_contradiction
│   ├── test_detect_temporal_contradiction
│   ├── test_no_contradiction_low_similarity
│   └── test_entity_overlap_threshold
│
├── class TestExplanationGeneration:
│   ├── test_generate_explanation_direct
│   ├── test_generate_explanation_semantic
│   ├── test_confidence_calibration
│   └── test_resolution_recommendation
│
├── class TestConflictResolution:
│   ├── test_replace_strategy
│   ├── test_branch_strategy
│   ├── test_merge_strategy
│   ├── test_defer_strategy
│   └── test_audit_logging
│
└── class TestIntegration:
    ├── test_full_pipeline
    └── test_resolution_with_governance
```

**Estimated Lines:** 300-400

---

### 9. Integration Tests
**File:** `tests/integration/test_belief_revision_integration.py` (NEW)
**Action:** CREATE

```
tests/integration/test_belief_revision_integration.py (NEW FILE)
├── @pytest.mark.integration
├── test_contradiction_detection_with_real_embeddings
├── test_resolution_audit_persistence
├── test_rls_isolation_for_resolutions
└── test_concurrent_conflict_resolution
```

---

## Constraints (from GMP Rules)

1. **No changes to `executor.py`** - memory internals only
2. **Preserve `PacketEnvelope` schema** - use existing audit patterns
3. **RLS enforcement** - all new tables must have tenant isolation
4. **structlog** - use existing logging patterns
5. **Circuit breaker** - wrap LLM calls in resilience patterns
6. **Async throughout** - no blocking I/O

## Dependencies

| Component | Dependency |
|-----------|------------|
| `explanation_engine.py` | `substrate_semantic.py` (embeddings) |
| `conflict_resolver.py` | `explanation_engine.py`, `substrate_repository.py` |
| `substrate_service.py` | `conflict_resolver.py` (lazy) |
| Migration 0021 | Migration 0018, 0019 (semantic_facts) |

## Test Coverage Requirements

| Test Type | Minimum | Target |
|-----------|---------|--------|
| Unit tests | 80% | 90% |
| Integration tests | 70% | 80% |
| Contradiction detection | 90% | 95% |
| Resolution strategies | 85% | 90% |

## Rollback Plan

1. **Migration:** Drop table `belief_resolution_audit`
2. **Code:** Revert added files and service integration
3. **No breaking changes** - all new code is additive

---

## Phase Progression

| Phase | Action | Exit Criteria |
|-------|--------|---------------|
| Phase 0 | Lock this TODO plan | Plan reviewed, no ambiguity |
| Phase 1 | Baseline - run existing memory tests | All pass |
| Phase 2 | Implement models + migration | Migration applies cleanly |
| Phase 2 | Implement ExplanationEngine | Unit tests pass |
| Phase 2 | Implement ConflictResolver | Unit tests pass |
| Phase 2 | Integrate into MemorySubstrateService | Lazy init works |
| Phase 3 | Add telemetry metrics | Metrics recorded |
| Phase 4 | Run full test suite | All tests pass |
| Phase 5 | Recursive verify against this plan | No scope drift |
| Phase 6 | Generate GMP report | Audit complete |

---

## Estimated Effort

| File | Lines | Complexity |
|------|-------|------------|
| `substrate_models.py` (extend) | +80 | Low |
| `explanation_engine.py` (new) | 280 | High |
| `conflict_resolver.py` (new) | 320 | High |
| `substrate_repository.py` (extend) | +90 | Medium |
| `substrate_service.py` (extend) | +60 | Low |
| `memory_metrics.py` (extend) | +30 | Low |
| `migrations/0021_...sql` (new) | 50 | Low |
| `test_belief_revision.py` (new) | 350 | Medium |
| `test_belief_revision_integration.py` (new) | 150 | Medium |
| **Total** | ~1,410 | |

---

## Approval

- [ ] Plan reviewed for completeness
- [ ] Scope boundaries agreed
- [ ] No "maybe", "likely", "should" language
- [ ] All file paths explicit
- [ ] Rollback plan documented

---

**Phase 0 LOCKED: 2026-01-15**
