# Native CEG authoring validation

## Policy and ownership

`tools/validate_domain.py` restores the predecessor authoring CLI policy. Its
default and `--strict` paths use `engine.config.loader.validate_authoring_file`,
the same bounded file reader, duplicate-rejecting safe YAML parser, `DomainSpec`
schema and tenant identity checks used by native admission. Gate compilation,
traversal diagnostics and prohibited-factor checks are delegated to the existing
CEG owners. There is no additional compiler, settings mutation or graph access.

After schema/identity checks, authoring validation always checks ontology edge
endpoints and candidate labels, gate edge references, the **global** scoring
default-weight total, gate compilation for every declared match direction,
traversal warnings for every declared direction and prohibited factors in gates.
Runtime dormant/unvalidated-pack overrides do not disable these authoring checks.
Schema and native-owner feature flags are unchanged; authoring does not rewrite
their defaults or reinterpret their semantics.

| Verdict | Default exit | `--strict` exit |
| --- | ---: | ---: |
| Clean | 0 | 0 |
| Warnings only | 2 | 1 |
| Any failure | 1 | 1 |

Strict escalation preserves `WARN` diagnostics, so callers can distinguish a
strictly blocking warning from a failed check. Non-JSON output reports warnings
as warnings, never as a clean result. Invalid or unreadable native inputs are
reported as failures without a traceback.

## Global versus per-direction weights: deliberate compatibility boundary

The authoring CLI retains the predecessor global rule:

`sum(d.defaultweight for d in spec.scoring.dimensions) <= 1.0 + 1e-9`.

This counts every dimension once, regardless of direction scopes. It is an
authoring compatibility policy, **not** the runtime weight policy. Native runtime
schema/admission still checks weights per direction, including unscoped weights
under the existing flag. Neither `engine/config/schema.py` nor live matching,
scoring or boot semantics changes. A multi-direction pack can consequently pass
native admission but fail the default/strict authoring CLI. The unchanged
PlasticOS fixture demonstrates this: global total 2.0 yields authoring exit 1 in
both modes while runtime admission remains valid. Freight matching remains exit
0 in both authoring modes. Tests compare both fixtures against the pinned
predecessor CLI at `f8d933d83a11244a2ada79c7f0d0dea74fbbe6ce`.

## Native admission and operator evidence

`--complete` selects the existing full native operator-admission policy, not the
legacy global-total authoring policy. Traversal warnings are admission defects;
runtime bypass flags cannot admit defective or dormant packs. Existing complete
schema guards, scoring/match/compliance/sync checks and per-direction weights
remain unchanged. `--strict` adds no bypass to complete admission.

`--require-reviewed` retains reviewed/production stage enforcement. `--receipt`
retains complete admission plus reviewed-stage enforcement and verification of
the supplied native review assertion against the exact spec bytes, tenant,
version and reviewed-input digest. A missing, malformed or mismatching supplied
receipt fails closed. `--complete` alone is a diagnostic and is not human
approval; existing receipt opt-in semantics are unchanged. Operator usage is:

```bash
.venv/bin/python tools/validate_domain.py /absolute/input.yaml \
  --tenant-id acme --complete --require-reviewed \
  --receipt /absolute/receipt.json --json
```

No validation command publishes a pack, relabels a draft, creates approval,
changes a pointer or performs graph/database migration.

## Programmatic compatibility

Existing callers of `tools.validate_domain.validate_spec(path)` retain native
runtime-parity defaults, including per-direction weights and withholding reasons.
The command-line entry point explicitly requests `authoring=True`. Python callers
can request the same diagnostic with `validate_spec(path, authoring=True)`;
`strict=True` also selects authoring policy and escalates warnings. Explicit
`complete=True` or a supplied `receipt_path` takes precedence and remains full
native admission. This separation preserves the existing runtime-parity tests
without weakening the restored authoring CLI.

Validation evidence is local only. Test receipts identify test-only reviewers;
they do not constitute real review, promotion or release admission.
