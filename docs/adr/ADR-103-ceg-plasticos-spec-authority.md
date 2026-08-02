# ADR-103: Single Executable CEG PlasticOS Spec

**Status:** Accepted  
**Task:** TASK-015  
**Date:** 2026-08-02

## Decision

`domains/plasticos/spec.yaml` is the **only** executable PlasticOS domain source for
Cognitive.Engine.Graphs.

Runtime authority is exclusively:

- `engine.config.loader.DomainPackLoader`
- path resolution: `domains/<domain_id>/spec.yaml`
- PlasticOS load: `DomainPackLoader.load_domain("plasticos")`

## Non-authoritative materials

Alternate PlasticOS specifications are archived under `docs/archive/plasticos/` and
are **not** discovered or loaded by `DomainPackLoader`. See
`docs/archive/plasticos/ARCHIVE_MAP.md`.

## Match direction (current executable)

Operator ballot D10 and DEC-006 evidence retain the live loader direction
`intake_to_buyer` until TASK-016/017 introduce explicit bidirectional renames.

## Consequences

- Flat `domains/plasticos_domain_spec.yaml` must not exist at the domains root.
- Docs/tools must not treat archived YAML as runtime authority.
- Duplicate-authority tests fail closed if a second PlasticOS executable path appears.

## Recovery

Restore the previous `domains/plasticos/spec.yaml` from git history and rebuild
derived graph state. Do not promote archived YAML into the loader path without a
new reviewed ADR.
