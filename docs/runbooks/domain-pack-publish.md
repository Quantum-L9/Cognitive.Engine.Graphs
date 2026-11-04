<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, runbooks]
tags: [domain-pack, native-publication, runbook]
owner: engine-team
status: active
/L9_META -->

# Native CEG domain-pack publication (offline operator workflow)

This mechanism does not authorize a release. It performs no deployment, live promotion, graph writes, Gate action, compiler call, network operation, or approval discovery. A trusted operator must separately authorize the reviewer, external approval record, store permissions, and read-only runtime delivery. CEG and EIE retain distinct native specs and independent digests; a cross-engine compatibility record is a separate operator responsibility.

## Native admission owner and CLI parity

`engine.config.loader.validate_native_bytes(data, tenant_id=None, *, complete=False, require_reviewed=False)` is the shared native runtime/operator admission surface. `validate_native_file(path, tenant_id=None, *, complete=False, require_reviewed=False)` performs one secure, bounded file read and calls it. The default and `--strict` CLI instead delegate to the same owner's `validate_authoring_file`: predecessor-compatible authoring checks include the global scoring-weight ceiling, gate compilation, traversal warnings and prohibited factors. Runtime bypass flags do not disable those authoring checks. Authoring global-weight policy is intentionally distinct from native runtime per-direction weights; see `docs/contracts/NATIVE_AUTHORING_VALIDATION.md`.

Default CLI validation uses **authoring mode**, not legacy runtime-parity mode. It returns **0** for clean input, **1** for any failure, and **2** for warnings only. `--strict` escalates warning-only input to exit **1** while retaining `WARN` diagnostics. `--json` reports `results`, `exit_code`, and `spec_path`. Folder-shaped `spec.yaml` inputs use the parent directory as the expected tenant; arbitrary draft filenames derive identity from `domain.id`, or an operator can pass `--tenant-id`.

`--complete` selects full native runtime-owner/operator admission rather than authoring policy: it uses per-direction weights and fails closed on admission defects, without runtime withholding overrides. It returns **0** for admitted or **1** for refused input; `--strict` does not weaken it. Programmatic `tools.validate_domain.validate_spec(path)` without authoring/strict/complete/receipt options retains legacy runtime parity and its existing feature flags. No CLI validation result constitutes approval or publication.

Run these commands from the repository root. CLI `--root` defaults to the current directory; both spec and any `--receipt` path must remain under that allowed root. For an absolute external input, supply an explicit, existing allowed root containing it (and its receipt, if supplied). Usage/path-policy errors also exit **2**, but are reported on stderr rather than as warning-only validation JSON.

```bash
.venv/bin/python tools/validate_domain.py domains/plasticos/spec.yaml --complete --json
.venv/bin/python tools/validate_domain.py /operator/input/candidate.yaml \
  --root /operator/input --tenant-id acme --complete --require-reviewed --json
```

Complete admission uses the existing native gate, traversal, scoring, compliance, and sync owners. It refuses dormant packs, executable admission defects, undeclared traversal references, unknown root keys, and disabled schema-safety guards even if runtime withholding overrides are enabled. It does not change global settings, translate the native schema, or edit input bytes. YAML parsing is SafeLoader-based, rejects explicit duplicate keys/non-string keys/cyclic aliases, bounds nesting and alias-expanded structure, and preserves the ordinary merge aliases in native template fixtures. Inputs are bounded to 5 MiB; sidecars to 16 KiB. File reads reject nonregular files and symlinks in every path component using directory-fd traversal with `O_NOFOLLOW`.

## Required review assertion and publication

The operator supplies all three pieces of review evidence: an explicit named human reviewer, an external approval reference, and the exact SHA-256 of the bytes that reviewer approved. A computed digest alone is not approval. The publisher never adds a stage suffix, changes fields, or infers approval from a feature flag, tier, file name, or compiler receipt.

```bash
# Values must come from an actual separately authorized review, not these examples.
.venv/bin/python tools/publish_domain_pack.py publish /operator/input/candidate.yaml \
  --root /operator/ceg-packs \
  --tenant-id acme \
  --reviewed-by '<named human>' \
  --approval-ref '<external approval record>' \
  --reviewed-input-sha256 '<exact reviewed lowercase 64-character digest>' \
  --reviewed-at '<ISO-8601 timezone-aware review timestamp>'
```

Python API:

```python
publish_pack(spec_path: Path, root: Path, *, tenant_id: str,
             reviewed_by: str, approval_ref: str,
             reviewed_input_sha256: str, reviewed_at: str | None = None) -> dict
rollback_pack(root: Path, *, tenant_id: str, sha256: str) -> dict
```

`reviewed_at` must be timezone-aware and not in the future. If omitted, it records when the operator supplied the review assertion; it does not independently authenticate the actual historical review time. No reviewer/ref/digest argument has an approval-generating default. Only native versions explicitly ending `-reviewed` or `-production` can be published, irrespective of runtime stage flags. Unsuffixed repository examples are valid native specimens but are not publication-ready. Changed bytes require a changed version; forward publication refuses a numeric-version decrease or production-to-reviewed stage downgrade. Explicit rollback is the only path selecting an older admitted revision.

## Exact immutable layout and schemas

SHA-256 values are **bare lowercase hex**, without `sha256:`. The only artifact path comes from the fixed tenant/revision layout, never a receipt-supplied path:

```text
<root>/<tenant>/
  .publish.lock                         # operator-writer lock, not read by runtime
  current.json                          # atomically replaced pointer, mode 0444
  revisions/
    <sha256>/                           # immutable revision directory, mode 0555
      spec.yaml                         # exact reviewed input bytes, mode 0444
      receipt.json                      # immutable review assertion, mode 0444
```

`current.json` uses precisely these required fields (unknown/duplicate fields are refused):

```json
{
  "schema": "ceg.domain-current/v1",
  "tenant_id": "acme",
  "version": "1.0.0-reviewed",
  "sha256": "<64 lowercase hex characters>"
}
```

`receipt.json` uses precisely these required fields:

```json
{
  "schema": "ceg.domain-approval/v1",
  "tenant_id": "acme",
  "version": "1.0.0-reviewed",
  "sha256": "<64 lowercase hex characters>",
  "reviewed_input_sha256": "<same exact reviewed-byte digest>",
  "reviewed_by": "<explicit named human>",
  "approval_ref": "<external approval record>",
  "reviewed_at": "<timezone-aware ISO-8601 timestamp>",
  "previous_sha256": null
}
```

`previous_sha256` is required, null for the first publication, otherwise the admitted current digest at first installation of that immutable revision. Reusing an existing digest never rewrites its original receipt or changes its predecessor. A different reviewer/ref cannot overwrite the same revision. Pointer and receipt versions must equal the admitted YAML version, and pointer/receipt/spec/revision-dir digests must all agree. Reviewed-input and published-spec digests must be equal because this native publisher does not transform the file.

`DomainCurrent` and `DomainApproval` are strict Pydantic record models in the existing loader. Cross-engine tooling can use `read_native_record(path, Model)` and `load_native_revision(root, tenant_id, sha256) -> (DomainSpec, DomainApproval)` for the same native checks. CEG does not consume EIE schema fields or require equal CEG/EIE digests.

Writers serialize with a per-tenant `flock`, stage and fsync complete spec/receipt files together, install the revision directory atomically, and only then fsync/atomically replace the current JSON pointer. Readers observe the old or new complete revision. Interrupted publication may leave an unreferenced approved revision; it never activates an incomplete stage. A clean retry can reuse an admitted, identically reviewed revision. Temporary names start `.stage-`/`.current-` and are never selected by the reader. Atomicity is per CEG tenant pointer, not a distributed CEG/EIE transaction.

## Runtime feature discipline

Both new feature flags default to **False**. No defaults or environment files are changed by the tooling.

- `DOMAIN_PACK_RECEIPTS_ENABLED`: the loader reads `current.json` plus immutable revision/receipt; verifies bytes, tenant, version, exact reviewed input, reviewer/ref/time, and complete admission **on every synchronous or asynchronous load**. It bypasses legacy cache hits and never falls back to `<root>/<tenant>/spec.yaml` after a pointer/receipt/integrity failure. `list_domains()` applies the identical policy and logs/omits refused tenants. In production, loader construction requires an explicitly supplied, absolute, existing, nonsymlink root; implicit relative `domains` is refused.
- `DOMAIN_PRODUCTION_STAGE_REQUIRED`: under `L9_ENV=prod`, legacy owner admission also requires `-reviewed` or `-production`. Off preserves unsuffixed example-version admission. Receipt mode and the publisher require these stages regardless of this flag or dormant/unvalidated overrides.

With receipt mode off, legacy folder-shaped `<root>/<tenant>/spec.yaml` discovery/caching remains available for explicitly configured nonproduction development/tests. An actual rollout needs separate authorization to set the flags and mount operator-owned native artifacts read-only. Boot, Docker, Compose, handlers, root files, and deployment defaults are not changed by this implementation.

## Rollback and integrity caveats

```bash
.venv/bin/python tools/publish_domain_pack.py rollback \
  --root /operator/ceg-packs --tenant-id acme --sha256 '<previous approved digest>'
```

Rollback freshly admits both the current publication and the selected immutable target, atomically switches the pointer, and leaves the target receipt byte-identical. It refuses a missing/tampered current or target rather than silently repairing evidence. Old/new EIE compatibility must be separately proved before coordinated release/rollback.

These receipts are **unsigned assertions**, not proof of trusted human identity. A writable operator store can be forged consistently by its owner; SHA-256 protects byte integrity only relative to the asserted receipt/digest. Read-only mounts, operator authorization, and independent approval/audit verification remain mandatory deployment controls. The loader has no externally pinned compatible-pair revision and cannot identify a fully valid but operator-unapproved older pointer as stale by itself; cross-engine release admission must pin and verify its chosen pair. No signing infrastructure, live publication, or real reviewer approval is invented by this runbook or its disposable tests.
