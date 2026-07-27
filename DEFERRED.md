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

## DEFERRED-002-UPDATE (2026-07-24)

**Title:** DEFERRED-002 partially resolved — OpenAI backend is wired

**Rationale:** Re-inspection of `engine/security/P2_9_llm_schemas.py` (`_LLMBackend` class, lines 112-212) found that the "empty JSON object + warning" stub described in DEFERRED-002 no longer matches the code. `_LLMBackend._ensure_client()` and `_LLMBackend.call()` lazily construct a real OpenAI SDK client from `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`LLM_PROVIDER` and issue actual chat completions; `ValidatedLLMClient._call()` dispatches to it and raises `FeatureNotEnabled` only when no API key is configured. `docs/FEATURE_GATES.md` §8 has been corrected to state "active" instead of "stub" for the OpenAI provider.

**Still outstanding (original DEFERRED-002 scope not fully closed):**
- Anthropic (or other non-OpenAI-compatible) provider support is NOT implemented — `LLM_PROVIDER` only supports `openai` / `openai-compatible`.
- DEFERRED-001 (token/cost extraction) remains open and unaffected by this update.

**Owner:** engine-team

**Priority:** LOW — OpenAI path is production-ready; Anthropic support remains a future enhancement, not a blocker.

---

## DEFERRED-003

**Title:** Quarantined — broken alternate chassis auth FastAPI factory

**File:** `chassis/auth/app.py` (removed 2026-07-24)

**Owner:** platform-team

**Rationale:** Alternate `create_app()` imported nonexistent `engine.api.auth.BearerAuthMiddleware` and duplicated the single-ingress factory already owned by `chassis/chassis_app.py`. Not on the Docker/`make local-api` path. Removed rather than repaired to avoid a second HTTP entrypoint.

**Acceptance Criteria (if resurrected):**
- Auth middleware lives in `chassis/auth/auth.py` and is applied from `chassis.chassis_app.create_app` only
- No second FastAPI factory under `chassis/auth/`
- No imports of `engine.api.*` (directory must not exist — Contract 1)

**Priority:** LOW — auth helpers in `chassis/auth/auth.py` remain; only the broken factory was removed

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
## DEFERRED-005

**Title:** SDK chassis divergences from the locked build plan (`L9_CHASSIS=sdk`)

**File:** `chassis/node_app.py`, `chassis/handler_registration.py`, `.env.template`, `docker-compose.yml`

**Owner:** platform-team

**Rationale:** The Gate SDK chassis instantiation plan assumed SDK capabilities that
the installed `constellation-node-sdk` does not provide. Each was implemented against
the actual SDK surface rather than skipped, but the following gaps remain open:

1. **`/v1/relay` does not exist.** The plan expected a relay route to assert absent
   for CONTRACT-01. `create_node_app()` exposes only `/v1/execute`, `/v1/health`,
   `/metrics`. The absence test in `tests/unit/test_node_app.py` therefore passes
   trivially and would not catch a future SDK version that adds relay.

2. **Gate-only ingress is not an SDK feature.** `NodeRuntimeConfig` has no
   gate-only field, so `L9_ENFORCE_GATE_ONLY_INGRESS` / `L9_GATE_NODE_NAME` are
   consumed by a local `BaseHTTPMiddleware` in `chassis/node_app.py`, not by the
   SDK. That middleware reads and replays the request body (`request._receive`),
   which is a documented Starlette workaround, not a supported API. If the SDK
   grows native gate-only enforcement, delete the middleware in favour of it.

3. **`PacketStore.persist` type mismatch is unverified at runtime.** The store
   annotates `TransportPacket`; the audit wrapper passes `PacketEnvelope` from
   `engine/packet/chassis_contract.py`. Persist failures are caught and logged as
   warnings, so a shape mismatch would degrade the audit trail silently rather
   than fail the request.

4. **`trace_id` is synthesised, not propagated.** SDK handlers receive only
   `(tenant, payload)`, so `_with_packet_audit` generates a fresh `uuid4()` per
   call. The Gate's own trace ID is therefore not carried into the packet pair,
   breaking cross-node trace correlation on the SDK path.

5. **`L9_MAX_ATTACHMENT_SIZE_BYTES` must be pinned.** The SDK defaults the
   attachment cap to 10MB and the packet cap to 256KB, then rejects that pair —
   so `get_runtime_config()` cannot validate from SDK defaults alone.
   `.env.template`, `docker-compose.yml`, and `make local-api-sdk` all set it to
   `0`. Guarded by
   `tests/unit/test_node_app.py::test_sdk_default_attachment_caps_are_mutually_invalid`.

**Acceptance Criteria:**
- SDK pinned to a tag or commit (see tech debt: it is currently an unpinned git dep)
- Gate-only ingress moved to native SDK enforcement, or the body-replay workaround
  replaced with a pure-ASGI middleware
- `PacketStore.persist` accepts `PacketEnvelope` explicitly, or the wrapper converts
- Gate trace ID reaches the audit packets (requires an SDK context accessor)

**Priority:** MEDIUM — `L9_CHASSIS` defaults to `legacy`, so none of these affect the
production path today. All become blocking before the SDK chassis becomes default.

---

