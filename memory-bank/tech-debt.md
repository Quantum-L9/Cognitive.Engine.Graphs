# Tech debt — revisit later

- **`constellation-node-sdk` is an unpinned git dependency** (`pyproject.toml`), so upstream
  changes to `create_node_app`, `NodeRuntimeConfig`, or `TransportPacket` can break the SDK
  chassis without a version bump. Pin to a tag or commit before the SDK path becomes default.
- **`chassis/auth/app.py` is deleted but not resolved** — quarantined rather than fixed forward.
  Decide its fate when the SDK chassis takes over auth.
- **Health probes assert HTTP 200 only** (3 Dockerfiles + `docker-compose.yml:71`). Correct for
  the legacy chassis (returns 503 when unready) but silently false-healthy under the SDK, whose
  `/v1/health` always returns 200 with a `ready` boolean. Tracked as step 6 of the SDK plan.
- **Graphiti memory was unreachable on 2026-07-24** — session state went to `memory-bank/` only,
  so nothing from this session is in the episodic graph. Two consecutive sessions now missing.
- **`mypy` on PATH is Python 3.9 (Xcode), project targets 3.12** — `make agent-check` and
  `make lint` fail locally at the type-check step with a bogus "Invalid syntax" on
  `engine/inference_rule_registry.py:462`. `mypy` is not installed in the project venv. CI is
  unaffected. Install mypy into the venv so the local gate matches CI.
- **`jsonschema` and `openapi-spec-validator` are undeclared dependencies** — three contract
  tests `skipif` on the import, so they silently pass as skips in a clean environment while
  appearing to cover the OpenAPI/JSON-Schema surface. Documented in `docs/contracts/README.md`
  known-gaps table; the real fix is declaring them in `pyproject.toml`.
- **Contract count is inconsistent across sources** — `AGENTS.md`/`CLAUDE.md` say 24 contracts,
  `tools/verify_contracts.py` checks 20 markdown files in `docs/contracts/`, and
  `contracts/*.yaml` now holds 24 machine-readable specs. Both numbers are defensible but the
  docs do not explain the split.
- **The main working tree carries ≥3 interleaved uncommitted workstreams** (~130 files). PR #144
  had to be assembled in a separate worktree to isolate one of them. Land or shelve the others.
