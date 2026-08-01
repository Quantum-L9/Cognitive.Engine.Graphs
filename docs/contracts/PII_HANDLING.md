<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts, compliance, pii]
owner: engine-team
status: active
/L9_META -->

# PII Handling Contract

**Enforces:** `CONTRACT-11` (`contracts/contract_11.yaml`)
**Closes:** Agents logging raw PII or inventing ad-hoc masking schemes

## Rule

PII fields are declared in the domain spec, never inferred. Each domain picks exactly one
handling mode, and the encryption key always comes from a declared source — never from a
literal in code.

## Spec shape

`PIISpec` (`engine/config/schema.py`):

```yaml
compliance:
  pii:
    fields:
      - contact_email
      - contact_phone
    handling: hash              # hash | encrypt | redact | tokenize
    encryptionkeysource: env    # env | vault | kms
```

| Field | Default | Values |
|---|---|---|
| `handling` | `hash` | `hash`, `encrypt`, `redact`, `tokenize` |
| `encryptionkeysource` | `env` | `env`, `vault`, `kms` |

## Handling modes

| Mode | Behavior | Reversible |
|---|---|---|
| `hash` | One-way digest; equality matching still works | No |
| `encrypt` | Symmetric encryption using the declared key source | Yes, with the key |
| `redact` | Value replaced with a fixed marker | No |
| `tokenize` | Value swapped for an opaque token resolved out of band | Yes, via the token store |

## Logging

The engine **never** logs PII values. Log the field *name* and the handling decision, not
the content. structlog filters are installed by the chassis (see `OBSERVABILITY.md`); the
engine does not configure them and must not assume they will catch a mistake.

## Correct

```python
logger.info("pii_field_hashed", field="contact_email", handling=spec.compliance.pii.handling)
```

## Wrong

```python
logger.info("processing contact", email=candidate["contact_email"])  # WRONG → PII in logs
key = "s3cr3t-aes-key"                                               # WRONG → hardcoded key
```

## Key sources

`encryptionkeysource` declares *where* the key lives, never the key itself:

- `env` — read from an environment variable at startup
- `vault` — fetched from HashiCorp Vault
- `kms` — fetched from a cloud KMS

## Verified by

- `tests/contracts/test_contracts.py::TestContract11PIIHandling`
- `tests/compliance/`
