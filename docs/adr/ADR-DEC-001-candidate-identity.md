# ADR-DEC-001: Candidate Identity Is a Contract-Defined Namespaced Key

**Status:** Accepted
**Decision Record:** DEC-001 (EVID-004 / UNK-003)
**Date:** 2026-08-05

## Context

The canonical CEG match contract identifies every match candidate by a
namespaced `entity_ref` string constrained by
`^[a-z0-9_.-]+:[^\s]+$` (e.g. the Odoo case `res.partner:102`), resolved through
a `SourceRecord{system, record_ref}` mapping:

- `engine/models/payloads.py:50-51` — `ENTITY_REF_PATTERN` definition.
- `engine/models/payloads.py:164-166` — `MatchCandidate.entity_ref` (pattern-validated).
- `engine/models/payloads.py:262-266` — `SourceRecord.system` / `record_ref`.
- `contracts/payloads/examples/match-response.json` — canonical example carries
  `"entity_ref": "res.partner:102"`.

The **live** match handler instead reads a bare Neo4j node property `entity_id`:

- `engine/handlers.py:509`, `:616`, `:1497` — candidate identity read from the
  `entity_id` property.
- No canonical writer sets `entity_id` at all. The live sync path is
  `engine/handlers.py::handle_sync` → `engine/sync/generator.py::SyncGenerator`,
  which MERGEs on the domain-declared `idproperty` (`facility_id`, `code`,
  `form_id`, `opportunity_id`, `demand_id` in `domains/plasticos/spec.yaml`) —
  never on `entity_id`, which is **not** defined in `engine/config/schema.py`.

This is a live-vs-contract divergence: the contract's identity is a governed,
namespaced `entity_ref`; the running code keys on an ungoverned `entity_id` node
property that no canonical writer populates. DEC-001 records how candidate identity is defined so the
divergence is resolved deliberately rather than by accident.

## Options Considered

- **OPTION-A — Identity is the bare Odoo integer.** Candidate identity is the raw
  `res.partner` id (e.g. `102`). Rejected: it collides across source systems, carries
  no system namespace, and cannot express non-Odoo sources (EIE, operator, external).
- **OPTION-B — Identity is a domain/contract-defined namespaced key.** Candidate
  identity is the namespaced `entity_ref` (e.g. `res.partner:102`), and mapping it to
  the underlying Odoo `res.partner` id requires an explicit resolver over
  `SourceRecord{system, record_ref}`. **Selected.**
- **OPTION-C — Identity is the Neo4j-native node id.** Use the database-internal
  element/node id. Rejected: Neo4j-native ids are not stable across restore/rebuild,
  are not portable across tenant databases, and leak storage internals into the
  contract surface.

## Decision

Adopt **OPTION-B**. Candidate identity in the CEG match contract is the
contract-defined, namespaced `entity_ref` matching `^[a-z0-9_.-]+:[^\s]+$`. It is
**not** the bare `res.partner` integer (OPTION-A) and **not** a Neo4j-native node id
(OPTION-C). Translating an `entity_ref` to the concrete Odoo `res.partner` id is an
explicit resolver responsibility mapping through `SourceRecord{system, record_ref}`,
never an implicit reinterpretation of a raw integer or a database node id.

## Consequences

- The contract surface stays source-system agnostic and stable across graph
  rebuilds and tenant databases.
- A resolver mapping `entity_ref` ↔ source record is required wherever the concrete
  Odoo id is needed; this mapping is explicit and testable.

## Residual Reconciliation Task

The live handler still keys candidate identity on the ungoverned `entity_id` node
property (`engine/handlers.py:509,616,1497`), which is not schema-defined and which
no canonical writer produces — the handler reads it through silent fallbacks
(`.get("entity_id", "")`). A follow-up must align the live handler and sync path
with the contract `entity_ref`
(schema-define the identity property, or resolve `entity_ref` → stored key through the
resolver) so runtime identity matches the contract this ADR ratifies. Until then, the
divergence is a tracked residual risk, not a resolved state.

## Citation Correction (2026-08-23)

This ADR originally cited `engine/graph/graph_sync_client_fix.py:113` as the place
where `entity_id` was "client-supplied at sync time". That module was an unwired
gap-fix artifact with no caller anywhere in the repository, and it was removed by
the gap-fix artifact convergence audit
(`docs/audits/2026-08-23-gap-fix-artifact-convergence/`). Its Cypher
(`MERGE (n {entity_id: row.entity_id, tenant: $tenant})`) never executed.

The correction widens rather than narrows the divergence this ADR records: the
handler reads `entity_id`, and nothing writes it. The decision (OPTION-B) and the
residual reconciliation task are unchanged.

## Artifacts

`engine/models/payloads.py`, `contracts/payloads/examples/match-response.json`,
`contracts/match_response.json`, `engine/handlers.py`,
`engine/sync/generator.py`.
