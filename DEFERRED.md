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

## DEFERRED-004

**Title:** Delete four superseded `docs/agent-tasks/` development playbooks

**File:** `docs/agent-tasks/add-action-handler.md`, `add-domain-spec.md`, `add-gate-type.md`, `extend-contract.md`

**Owner:** engine-team

**Rationale:** Content was consolidated into `.claude/skills/` (`action-handler-development`, `domain-spec-authoring`, `gate-development`, `contract-check`), which is the path agents actually load. Two of the four carry guidance that is now wrong: `add-action-handler.md` documents a stale handler return shape, and `add-gate-type.md` describes a one-file-per-gate layout that does not match `engine/gates/types/all_gates.py`. Following either produces a broken change. The files are marked `status: deprecated` with a banner and are referenced by nothing in the repo, so they are inert — but leaving them keeps two competing sources of guidance. Deletion is pending Founder approval per the destructive-operation rule.

**Acceptance Criteria:**
- The four files are removed from `docs/agent-tasks/`
- `grep -rn "agent-tasks"` returns no references outside `DEFERRED.md`
- `make agent-check` exits 0

**Blocked by:** Explicit Founder approval to delete (files marked, not removed)

**Priority:** LOW — files are deprecated and unreferenced; deletion is cleanup, not a fix

---
