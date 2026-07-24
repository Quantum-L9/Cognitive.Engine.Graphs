<!-- L9_META
l9_schema: 2
origin: engine-specific
engine: graph
layer: [docs]
tags: [governance]
status: active
/L9_META -->

# DEFERRED.md — Tracked Deferments

All inline TODO comments must be migrated here with a unique ID, owner, rationale, and acceptance criteria.

---

## DEFERRED-001

**Title:** Token and cost extraction from LLM responses in `track_llm_usage`

**File:** `engine/security/5_llm_security.py` — `track_llm_usage` context manager

**Owner:** engine-team

**Rationale:** Token counts live inside provider-specific response objects (OpenAI `usage` field, Anthropic `usage.input_tokens`, etc.). Implementing this requires knowing which provider SDK is in use at call time and accessing the response object, which the context manager currently does not receive.

**Acceptance Criteria:**
- `cost_logger` emits `input_tokens`, `output_tokens`, `estimated_cost_usd` per LLM call
- Supports at minimum: OpenAI, Anthropic
- Passes `mypy --strict` and `ruff check`
- Covered by unit tests with mocked provider responses

**Blocked by:** Provider SDK selection (not yet finalized for production)

**Priority:** MEDIUM — nice-to-have for cost observability, not blocking functionality

---

## DEFERRED-002

**Title:** LLM SDK integration in `ValidatedLLMClient._call`

**File:** `engine/security/P2_9_llm_schemas.py` — `ValidatedLLMClient._call` method

**Owner:** engine-team

**Rationale:** The `_call` method is the integration point where a concrete LLM provider SDK (OpenAI, Anthropic, etc.) should be wired in. Currently returns an empty JSON object and logs a warning. Callers receive schema validation errors until a real provider is connected.

**Acceptance Criteria:**
- `_call` dispatches to a configured LLM provider SDK
- Supports at minimum: OpenAI, Anthropic
- Input sanitization and output validation remain enforced via existing wrappers
- Passes `mypy --strict` and `ruff check`
- Covered by integration tests with mocked provider responses

**Blocked by:** Provider SDK selection (not yet finalized for production)

**Priority:** HIGH — required for any LLM-powered feature to function

---
## DEFERRED-003

**Title:** Surgical JSON header injection (JSON files excluded from L9_META stamp)

**File:** `tools/l9_meta/formats/jsonmeta.py` — `inject` / `strip`

**Owner:** platform

**Rationale:** Injection round-trips through `json.loads` / `json.dumps`, so stamping a file also reserializes it: blank lines disappear and compact arrays are exploded one element per line. `docs/contracts/data/models/packet-envelope.schema.json` came back +159/−29 for a 9-line header, and `.devcontainer/devcontainer.json` lost its section spacing. The output is semantically identical JSON, but the diff noise is unrelated to metadata, so `**/*.json` is excluded in `l9-meta.yaml` until injection is textual.

**Acceptance Criteria:**
- `inject` inserts `_l9_meta` as the first key by text edit, matching the file's existing indentation; every other byte is unchanged
- `strip` removes the key by brace matching, restoring the original bytes
- Round-trip and idempotency fixtures in `tests/unit/test_l9_meta_formats.py` extended to assert byte equality on a file with compact arrays and blank lines
- `**/*.json` removed from `exclude` in `l9-meta.yaml` and the 16 JSON files stamped

**Blocked by:** Nothing — deferred to keep the pipeline PR scoped to the header mechanism.

**Priority:** LOW — 16 files, no runtime impact; C-018 coverage is otherwise complete.

---
