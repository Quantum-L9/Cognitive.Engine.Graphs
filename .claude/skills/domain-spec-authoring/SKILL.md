---
name: domain-spec-authoring
description: Create or validate a domain spec YAML file for the CEG engine
---

# Domain Spec Authoring

The domain spec is the source of truth for all matching behavior (Contract 12).
No matching logic is hardcoded in the engine.

## Layout — this matters

`DomainPackLoader` resolves **exactly one** path:

```
tests/fixtures/design_packs/<domain_id>/spec.yaml
```

The flat `tests/fixtures/design_packs/<name>_domain_spec.yaml` files are **not loadable**. They are
reference drafts. `DomainPackLoader.list_domains()` returns only directories
whose packs pass native admission. Inspect the current list rather than assuming
an inventory from this guide. Putting a new spec at
the flat path produces a `DomainNotFoundError` at load time, not a validation
error, so it fails late and confusingly.

Symlinked `spec.yaml` files are rejected by the loader.

## Steps

1. **Confirm the domain ID is unused**: no `tests/fixtures/design_packs/<domain_id>/` directory yet.
2. **Read the schema**: `engine/config/schema.py` defines every spec type.
3. **Read a working example**: `tests/fixtures/design_packs/plasticos/spec.yaml` is the graph-owned
   specimen. Validate it under the current admission flags; do not assume every
   repository example is admitted or that it is EIE's enrichment artifact.
4. **Create `tests/fixtures/design_packs/<domain_id>/spec.yaml`** with these sections:
   - `domain` — `id`, `name`, `version` (quoted, see Versioning below)
   - `ontology` — node types, edge types, property declarations
   - `matchentities` — source/candidate entity definitions, as the model declares
   - `queryschema` — input parameter definitions
   - `traversal` — path patterns, hops bounded (max 10)
   - `gates` — filters using the 10 gate types
   - `scoring` — ranking dimensions using the 13 computation types
   - `sync` — entity ingestion endpoints
   - `compliance` — PII fields and handling mode, if applicable
5. **Validate**:
   ```bash
   python tools/validate_domain.py tests/fixtures/design_packs/<domain_id>/spec.yaml --strict
   ```
   Add `--json` for CI-parseable output.
6. **Confirm the loader sees it**:
   ```bash
   python -c "from engine.config.loader import DomainPackLoader; \
     print(DomainPackLoader('domains').list_domains())"
   ```
7. **Add a test** in `tests/unit/test_config_loader.py` asserting the spec loads
   and required sections are present.
8. **Run `make agent-check`**.

Keep `DomainSpec` and `DomainPackLoader` as the semantic owners. The native CLI
must return the loader's admission verdict, including dormant withholding and
the enabled compilation checks. Do not create a second compiler or use EIE's
scanner to invent graph gates/scoring. Before release, run EIE's operator
`scripts/check_domain_compatibility.py` against both independent native artifacts;
require approved receipts and a passing pair proof, not identical YAML.

## Versioning

Format is `{major}.{minor}.{patch}-{stage}` (`docs/contracts/DOMAIN_SPEC_VERSIONING.md`).
The stage records how the spec was produced, so pick it by origin, not by taste:

| Origin | Version |
|--------|---------|
| Incomplete CRM discovery draft (not publishable) | `0.1.0-seed` |
| Native graph authoring, human-reviewed publisher input | `1.0.0-reviewed` |

A hand-written spec starting at `0.1.0-seed` falsely claims the enrichment
pipeline produced it. Stages advance `seed → discovered → inferred → proposed
→ reviewed → production`; each transition may add columns but never drop them.

## Contracts to read first

- `docs/contracts/DOMAIN_SPEC_VERSIONING.md`
- `docs/contracts/FIELD_NAMES.md`
- `docs/contracts/PYDANTIC_YAML_MAPPING.md`

## Naming

YAML keys are the Python field names — there are no Pydantic aliases (NAME-001
is a banned pattern). Gate type and scoring computation identifiers are flatcase
(`temporalrange`, `geodecay`); many native structural keys are also flatcase
(`matchentities`, `queryschema`, `candidateprop`, `idproperty`). Copy the live
schema spellings, not presumed snake_case replacements. Preserve the schema's
existing edge input keys `from` and `to`; this guide does not authorize a naming
migration or changes to native model meaning.

## Gate types (10)

`range`, `threshold`, `boolean`, `composite`, `enummap`, `exclusion`,
`selfrange`, `freshness`, `temporalrange`, `traversal`

## Scoring computations (13)

`geodecay`, `lognormalized`, `communitymatch`, `inverselinear`,
`candidateproperty`, `weightedrate`, `pricealignment`, `temporalproximity`,
`customcypher`, `traversalalias`, `kge`, `variantdiscovery`, `ensembleconfidence`

## Validation checklist

- [ ] Spec lives at `tests/fixtures/design_packs/<domain_id>/spec.yaml`
- [ ] `list_domains()` includes the new domain
- [ ] Edge `from`/`to` types reference declared node types
- [ ] Gate field references match declared node properties
- [ ] Scoring weight overrides sum ≤ 1.0 (asserted at boot in `engine/boot.py`)
- [ ] Traversal hops bounded (max 10)
- [ ] No prohibited compliance fields referenced (Contract 10 — fails at compile time)
- [ ] `python tools/validate_domain.py ... --strict` exits 0
- [ ] Publication binds the exact reviewed bytes, supplied reviewer and approval reference
- [ ] CEG and EIE versions/hashes remain independent; their sync intersection passes the release check
