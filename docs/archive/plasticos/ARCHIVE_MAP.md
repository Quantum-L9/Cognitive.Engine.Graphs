# PlasticOS Spec Archive Map

Authoritative executable source: `domains/plasticos/spec.yaml`
Loader: `engine.config.loader.DomainPackLoader`
ADR: `docs/adr/ADR-103-ceg-plasticos-spec-authority.md`

| Archived path | Former path | Status |
|---|---|---|
| `docs/archive/plasticos/plasticos_domain_spec.yaml` | `domains/plasticos_domain_spec.yaml` | NON-AUTHORITATIVE reference / schema fixture |
| `docs/archive/plasticos/plasticos_domain_spec_v0.3.0.yaml` | `docs/plasticos_domain_spec_v0.3.0.yaml` | NON-AUTHORITATIVE historical |
| `docs/archive/plasticos/plasticos_domain_spec_v0.4.yaml` | `docs/plasticos_domain_spec_v0.4.yaml` | NON-AUTHORITATIVE historical |
| `docs/archive/plasticos/PlasticOS Graph Cognitive Engine.yaml` | `docs/PlasticOS Graph Cognitive Engine.yaml` | NON-AUTHORITATIVE spec patch |
| `docs/archive/plasticos/plasticos_domain_spec_changes.md` | `docs/plasticos_domain_spec_changes.md` | NON-AUTHORITATIVE changelog |

Rules:

1. Never place a second `plasticos/**/spec.yaml` under `domains/`.
2. Never restore a flat `domains/plasticos_domain_spec.yaml` as runtime input.
3. Schema activation tests may read archived YAML explicitly; runtime code must not.
