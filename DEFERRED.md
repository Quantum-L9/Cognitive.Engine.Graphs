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

**Title:** `contracts/contract_NN.yaml` registry does not conform to `test_contract_registry.py` schema

**File:** `contracts/contract_01.yaml` through `contract_24.yaml` (all 24, committed via PR #70)

**Owner:** engine-team

**Rationale:** `tests/contracts/test_contract_registry.py` (added alongside the dual-chassis/SDK migration work) enforces a newer contract-YAML schema than the one the existing 24 `contracts/*.yaml` files were authored against:
- Every contract YAML is missing the required `docs: [...]` key entirely.
- Every contract's `verification.test` points at a retired per-contract test file (e.g. `tests/contracts/test_contract_01.py`) instead of a pytest node ID into the current monolithic `tests/contracts/test_contracts.py` (20 classes, not a 1:1 match against 24 contracts).
- Two aggregate checks also fail: every doc in `verify_contracts.py::REQUIRED_CONTRACTS` must be claimed by some contract's `docs` list, and every scanner rule ID in `tools/contract_scanner.py` must be claimed by some contract's `verification.scanner_rules` list.

Fixing this requires a deliberate, per-contract mapping decision (which of the 29 `docs/contracts/*.md` files and which `test_contracts.py` class each of the 24 contracts corresponds to) — not a mechanical fix, and getting the mapping wrong would create false confidence in a compliance-tracking system. Deferred rather than guessed.

**Acceptance Criteria:**
- Every `contracts/contract_NN.yaml` has a non-empty `docs` list of files that exist under `docs/contracts/`
- Every `contracts/contract_NN.yaml`'s `verification.test` is a resolvable `tests/contracts/test_contracts.py::ClassName` node ID
- Every `verification.scanner_rules` entry exists in `tools/contract_scanner.py`
- Every doc in `tools/verify_contracts.py::REQUIRED_CONTRACTS` is claimed by at least one contract's `docs` list
- Every scanner rule ID in `tools/contract_scanner.py` is claimed by at least one contract's `verification.scanner_rules` list
- `pytest tests/contracts/test_contract_registry.py` passes in full

**Blocked by:** Requires the contract author (or someone with full context on the 24-contract ↔ 29-doc ↔ 20-test-class intended mapping) to make the mapping decisions

**Priority:** MEDIUM — compliance/audit tooling gap, not a functional regression; existing `tools/verify_contracts.py` and `tools/contract_scanner.py` still run and enforce their own (older) contract set independently

---
