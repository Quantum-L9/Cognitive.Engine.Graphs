<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, runbooks]
tags: [runbook, audit, temporal, read-only]
owner: engine-team
status: active
/L9_META -->

# Runbook: temporal storage audit (ceg-scoring-audit L1, operator decision D1)

## Why
Until native temporal storage (PR #563), node sync wrote a declared `datetime` /
`date` / `duration` as the ISO-8601 string the row carried. Cypher temporal
comparisons and `duration.inDays` refuse or null a string, so such a value
silently scores and gates wrongly. D1: no historical data is converted in this
campaign; conversion needs a separate approval, and only if this audit finds any.

## What it does
`tools/audit_temporal_storage.py` (`engine/health/temporal_storage_audit.py`)
reads every declared temporal of the tenant's spec — node and relationship
properties, scalar and `list<temporal>` — and counts, per tenant database, the
stored values that are still `STRING` (`x IS :: STRING NOT NULL`). Every statement
runs through `GraphDriver.execute_read`, a managed READ transaction: the server
refuses any write and the tenant database is never provisioned. Labels and
property names come from the tenant's own spec through `sanitize_label`.

Requires Neo4j 5.10+ (type predicate expressions with `NOT NULL`, `ANY` and `LIST<…>`).
A relationship type declared for several endpoint pairs is audited once per pair,
constrained to both declared labels.

## Command
```bash
NEO4J_URI=bolt://… NEO4J_USERNAME=… NEO4J_PASSWORD=… DOMAINS_ROOT=/path/to/published/packs \
  python tools/audit_temporal_storage.py plasticos [other-tenant …]
```
The spec comes from the pack root the engine serves, with the loader's own
precedence (`--domains-root`, then `DOMAIN_SPECS_PATH`, then `DOMAINS_ROOT`),
never from the repository's design packs.

## Output and exit code
JSON `{"reports": [{"domain", "database", "checked", "string_valued_total",
"conversion_required", "findings": [{"owner", "label", "property", "declared",
"endpoints", "string_valued"}]}]}` (`endpoints` is `"From->To"` for a relationship,
`null` for a node).

| Exit | Meaning | Next step |
|---|---|---|
| 0 | No string-valued declared temporal | none |
| 2 | At least one found (`conversion_required: true`) | ask for a conversion approval, attaching the report |
| 1 | Spec not found or invalid | fix `DOMAINS_ROOT` / tenant id |
