# CEG payload contracts (domain-neutral)

Payload-only schemas for Gate_SDK `TransportPacket`. No alternate-envelope inheritance.
No transport fields. Improvement proposals lock `direct_mutation: false`.

Schemas:
- match request/response, improvement proposal (TASK-040)
- sync-projection (TASK-028)
- canonical-projection + outcome-feedback (TASK-061)

Native models: `engine.models.payloads`. See ADR-106 / ADR-108 / ADR-112.

The `match` action admits `match-request` payloads (those carrying
`contract_version`) through `admit_payload(MatchRequest, ...)` against the
tenant's pack and maps them onto the legacy match internals; the legacy
`match_direction` + flat `query` shape stays accepted (RU-012, flag
`MATCH_REQUEST_CONTRACT_ADMISSION`; see `docs/contracts/HANDLER_PAYLOADS.md`).

## Domain neutrality (Stage 2, R03)

The schemas pin no domain. Every payload root carries a `domain`, and the
values a payload may name for `direction`, `entity_type`, `outcome_type` and
`source_record.system` are **not** enumerated here: they resolve at admission
against `engine.models.payloads.DomainContract`, derived from the authorized,
versioned domain spec that `DomainPackLoader` admits:

| Payload field | Resolves against |
|---|---|
| `domain` | `domain.id` of the loaded pack |
| `direction` | `queryschema.matchdirections` |
| `entity_type` | ontology node labels + `sync.endpoints[].targetnode` |
| `outcome_type` | `outcome_signals[].signal` |
| `source_record.system` | `ceg`, `operator`, `external_source` + `feature_catalog[].owner` |

Validation without a contract in the Pydantic context fails closed:

```python
from engine.models.payloads import DomainContract, MatchRequest, admit_payload

contract = DomainContract.from_spec(loader.load_domain(domain_id))
request = admit_payload(MatchRequest, payload, contract)
```

The fixtures under `examples/` and `negative_examples/` are the **PlasticOS
example** of these shapes; they admit against the PlasticOS pack. Any other
approved domain supplies its own fixture set.

The `$id` URNs still carry the `urn:l9:plasticos:...` namespace they were
first published under. Renaming them is a contract-identity decision for the
contract owner (recorded in ADR-112); the shapes they identify are neutral.

## Compiler validator (TASK-034 / ADR-109 / ADR-112)

```bash
PYTHONPATH=. python3 tools/payload_contract_compiler.py --stdout-only
PYTHONPATH=. python3 tools/payload_contract_compiler.py --stdout-only \
  --domains-root /path/to/domains --fixtures-root /path/to/fixtures --domain my-domain
```

Draft-2020-12 checks the live schemas, rejects any schema that pins `domain`,
admits positive/negative fixtures against the `DomainContract` of the domain the
fixtures name (loaded through `DomainPackLoader` from `--domains-root`), and
emits a deterministic digest report. It succeeds for any admitted domain and
needs no PlasticOS directory; a missing or malformed schema, fixture set or
domain pack is a hard failure.
