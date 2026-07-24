---
name: action-handler-development
description: Add a new engine action handler to the CEG engine
---

# Action Handler Development

An action is a named entry in the single ingress `POST /v1/execute`. Handlers own
domain logic; the chassis owns HTTP, auth, tenant resolution, and packet framing
(Contracts 1–3).

## Signature

```python
async def handle_<action>(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
```

Return a **flat domain dict**. Do not build a `{"status": ..., "data": ...}`
envelope — `chassis/actions.py` wraps the return value via `deflate_egress()`
into a response `PacketEnvelope`. Wrapping it yourself double-nests the payload.

## Seven registration points

An action is only live when all seven are updated. Missing any one produces a
route that 404s, bypasses auth, or fails a contract test.

| # | File | What to add |
|---|------|-------------|
| 1 | `engine/handlers.py` | The `handle_<action>` function |
| 2 | `engine/handlers.py` → `register_all()` | `chassis_router.register_handler("<action>", handle_<action>)` |
| 3 | `chassis/actions.py` → `_engine_handlers` | `"<action>": handle_<action>` + the import |
| 4 | `engine/auth/capabilities.py` → `ACTION_PERMISSION_MAP` | `"<action>": "<scope>:<read\|write>"` |
| 5 | `engine/packet/packet_envelope.py` → `PacketType` | enum member if the action emits a new packet kind |
| 6 | `tests/contracts/_constants.py` → `KNOWN_ACTIONS` | the action name |
| 7 | `docs/contracts/api/openapi.yaml` | add to the `action` enum and the request examples |

Point 4 is the security-critical one: an action absent from `ACTION_PERMISSION_MAP`
has no capability requirement.

## Handler preamble

Every handler opens with the same four calls, in this order:

```python
graph_driver, domain_loader = _require_deps()
_validate_tenant_access(tenant, "<action>")
domain_spec = domain_loader.load_domain(tenant)
_enforce_capability(tenant, "<action>", domain_spec)
```

## Payload validation

Handlers validate explicitly and raise `ValidationError` with `action` and
`tenant` context — they do not assume a Pydantic model is applied upstream:

```python
entity_type = payload.get("entity_type")
if not entity_type:
    raise ValidationError("entity_type required", action="<action>", tenant=tenant)
```

Document the payload schema in the docstring under `Payload schema:` — the
contract tests read handler docstrings.

## Cypher rules

- Labels and property names: `sanitize_label()` before interpolation (Contract 9)
- Values: always `$params` passed to `graph_driver.execute_query(...)`
- Scope every query with `database=domain_spec.domain.id` (Contract 3)

## Contracts to read first

- `docs/contracts/HANDLER_PAYLOADS.md`
- `docs/contracts/FIELD_NAMES.md`
- `docs/contracts/RETURN_VALUES.md`
- `docs/contracts/METHOD_SIGNATURES.md`

## Tests

Add to `tests/unit/test_handlers.py`:

- Valid payload returns the documented keys
- Missing required field raises `ValidationError`
- Caller without the mapped capability is rejected
- Injection attempt in a label-bearing field is rejected

Then run `make agent-check`.
