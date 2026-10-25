<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts]
owner: platform
status: active
/L9_META -->

<!-- L9_TEMPLATE: true -->
# L9 Domain Spec Versioning Contract

## Rule
Domain spec YAMLs use semantic versioning with stage suffixes that indicate
how the spec was produced. The version drives schema migration behavior.

## Version Format
```

{major}.{minor}.{patch}-{stage}

```

## Stages (in order)
| Stage | Meaning | Who/What Creates It |
|-------|---------|-------------------|
| `seed` | Auto-generated from CRM field scan | ENRICH schema discovery (Phase 0) |
| `discovered` | Fields added by enrichment passes | ENRICH convergence loop (Phase 1-2) |
| `inferred` | Derived fields added by inference | ENRICH inference engine (Phase 2-4) |
| `proposed` | Gates/scoring auto-proposed | ENRICH convergence (Phase 6) |
| `reviewed` | Human-reviewed and approved | Human approval |
| `production` | Deployed and active | Release process |

## Example Progression
```

0.1.0-seed         → CRM scan: name, city, phone, notes, category
0.2.0-discovered   → Enrichment added: materials_handled, process_types, contamination_tolerance
0.3.0-inferred     → Inference added: material_grade, facility_tier, buyer_class
0.4.0-proposed     → Auto-proposed gates and scoring dimensions
1.0.0-reviewed     → Human approved all proposed changes
1.0.0-production   → Deployed to graph engine
1.1.0-discovered   → New enrichment cycle discovered: moisture_tolerance

```

## Field Metadata on Discovered/Inferred Fields
```yaml
- name: material_grade
  type: enum
  values: [A, B+, B, C, D]
  managed_by: computed                    # "api" | "computed" | "enrichment" | "gds"
  derived_from: [materials_handled, contamination_tolerance, process_types]
  discovery_confidence: 0.85
  auto_proposed: true
```


## BANNED

```python
# ❌ No version-less specs
domain:
  id: plasticos
  # version: ???     ← MISSING — every spec MUST have a version

# ❌ No arbitrary version strings
  version: "latest"     # WRONG
  version: "v2"         # WRONG — use semver
  version: "2026-03-01" # WRONG — use semver with stage
```


## Identity (CEG-IDENT-01 / CEG-DBNAME-01, decided 2026-10-22)

One customer, one directory, one versioned spec, one database:

```
tenant_id == domain.id == domains/<tenant_id>/spec.yaml == Neo4j database name
```

- The **tenant id** carried by a packet (`packet.tenant.org_id`) is the spec id
  the loader resolves (`domains/<tenant_id>/spec.yaml`, directory name is the
  lookup key) and the database the driver binds. There is no mapping layer,
  table or alias between the three; a payload `domain` that differs from the
  packet tenant is rejected, not translated.
- **Grammar** (`engine.utils.security.validate_tenant_id`, applied by the loader
  before any path is built and by the driver before `CREATE DATABASE`): the
  Neo4j 5 database-name grammar, lowercase only — 3 to 63 characters, starts
  with a letter, then only letters, digits, dots (`.`) and dashes (`-`), and
  never starting with `system`. Regex after the lowercase requirement:
  `^[a-z][a-z0-9.-]{2,62}$`. Neo4j would lowercase a mixed-case name and accept
  an underscore; this contract refuses both so the identifier is byte-identical
  on every node.
- **Vertical templates are provenance, not tenants.** A shipped pack such as
  `roofing-company` is the template it is named after. A customer spec derived
  from it records that origin and nothing else:

```yaml
domain:
  id: acme.roofing          # the tenant id, the directory, the database
  name: Acme Roofing
  version: 1.0.0-reviewed
  template:                 # REQUIRED on customer specs; absent on shipped verticals
    id: roofing-company
    version: 1.2.0-production
    source: crm-scan        # optional: what produced the first revision
```

### Identity BANNED

```yaml
# ❌ tenant ids outside the grammar (refused by the loader before disk is touched)
domain:
  id: Acme_Roofing          # uppercase and underscore
  id: systemacme            # reserved prefix
  id: ab                    # under 3 characters

# ❌ a vertical name reused as a tenant id for a second customer
#    (two customers, one directory, one database — the contract forbids it)
# ❌ any tenant→domain or tenant→database mapping in code, config or payload
```

## Migration Rules

- `seed` → `discovered`: ADD columns only. Never drop.
- `discovered` → `inferred`: ADD computed columns. Set `managed_by: computed`.
- Any stage → `production`: Requires human review OR auto-approval (Autonomous tier).
- Major version bump: Breaking ontology change (node/edge type renamed or removed).
- Minor version bump: New fields, gates, or scoring dimensions added.
- Patch version bump: Threshold/parameter tuning only.

```
