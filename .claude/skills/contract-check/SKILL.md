---
name: contract-check
description: Run contract verification, or extend a contract file, in the CEG codebase
disable-model-invocation: true
---

# Contract Verification and Extension

## "20 contracts" vs "24 contracts" — two different sets

Both numbers are correct and refer to different artifacts. Confusing them sends
you editing the wrong file.

| Set | Location | Count | Verified by |
|-----|----------|-------|-------------|
| Contract **docs** — prose rules agents must read | `docs/contracts/*.md` | 20 required (`REQUIRED_CONTRACTS`) | `tools/verify_contracts.py` |
| Contract **invariants** — machine-checked behavioral contracts | `contracts/contract_*.yaml` | 24 | `tools/contract_scanner.py`, `tests/contracts/` |

`docs/contracts/` holds 22 `.md` files; `README.md` and `VERSIONING.md` are not
in the required set.

## Verification

```bash
make agent-check                  # everything CI blocks on
python tools/contract_scanner.py  # banned pattern scan, maps to contract IDs
python tools/verify_contracts.py  # 20 contract docs present AND referenced by an agent file
pytest tests/contracts/           # do NOT add -m contract — the marker deselects over 80%
```

`verify_contracts.py` enforces two things: the file exists, and its basename is
mentioned in at least one agent file (`AGENT_FILES`). Adding a contract doc that
no agent file references is a hard failure.

## Extending a contract doc

Extensions must be **additive**. Renaming a section or a field breaks the
scanner's ID mapping and any test asserting on that text.

1. Read the whole contract file first.
2. Append to the end of the relevant section, matching existing formatting.
3. Include both a WRONG and a RIGHT example. Cite the audit finding ID if the
   rule came from one.
4. If you add a rule to `BANNED_PATTERNS.md`, it is not enforced until you also:
   - add the detection to `tools/audit_engine.py`
   - add a test under `tests/compliance/`
   A documented-but-unscanned rule is decoration.
5. If the file's SHA-256 is tracked in `tools/l9_template_manifest.yaml`, update it.
6. Run `make agent-check`.

### Never

- Rename an existing section heading
- Change a documented field name or method signature
- Remove an anti-pattern example — they encode past incidents
- Add a rule without WRONG/RIGHT examples

## Adding a new contract doc

A new `docs/contracts/<NAME>.md` must be added to `REQUIRED_CONTRACTS` in
`tools/verify_contracts.py` **and** referenced by name in an agent file
(`.cursorrules`, `CLAUDE.md`, or `AGENTS.md`). Otherwise it is invisible to the
gate and to agents.

## Manual contract audit

For each contract, verify all four layers:

1. **Static** — does `contract_scanner.py` have a rule?
2. **Unit** — does `tests/contracts/` cover it?
3. **Integration** — is it exercised end-to-end?
4. **Property** — are invariants checked with Hypothesis (`tests/property/`)?

## Banned pattern reference

### Critical — merge blocked
- `SEC-001`–`007`: Cypher injection, `eval`, `exec`, `pickle`, `yaml.load`
- `ARCH-001`–`003`: FastAPI / Starlette / uvicorn in `engine/`
- `DEL-001`–`002`: `httpx` / `requests` in `engine/`
- `MEM-001`–`002`: direct INSERT into packetstore / memory_embeddings
- `STUB-001`: `NotImplementedError` outside `tests/`

### High — merge blocked
- `ERR-001`–`002`: bare `except`, swallowed exceptions
- `DI-001`: FastAPI `Depends` in `engine/`
- `OBS-001`–`002`: `structlog.configure` / `logging.basicConfig` in `engine/`
- `NAME-001`: Pydantic `Field(alias=...)`
- `SHARED-001`–`003`: redefining `PacketEnvelope` / `TenantContext` / `ExecuteRequest`
