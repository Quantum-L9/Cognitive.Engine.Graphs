<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [contracts, metadata, l9-meta]
owner: platform
status: active
/L9_META -->

# L9_META Header Contract

**Enforces:** `CONTRACT-18` (`contracts/contract_18.yaml`)
**Closes:** Agents hand-writing metadata headers with invented fields or the wrong comment syntax

## Rule

Every tracked source file carries an L9_META header (schema version 1). Headers are
**injected by `tools/l9_meta_injector.py`**, not typed by hand. Run the injector, commit,
done.

## Fields

| Field | Meaning |
|---|---|
| `l9_schema` | Header schema version — always `1` |
| `origin` | `l9-template` (shared) or `engine-specific` (this repo) |
| `engine` | `graph` |
| `layer` | List, e.g. `[config]`, `[docs, contracts]`, `[tools]` |
| `tags` | List of free-form tags |
| `owner` | `platform` or `engine-team` |
| `status` | `active`, `deprecated` |

## Format by filetype

The syntax changes with the comment style of the host language; the field set does not.

**Python** — inside the module docstring:

```python
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [compliance, prohibited-factors]
owner: engine-team
status: active
--- /L9_META ---

Module description follows.
"""
```

**Markdown / HTML** — HTML comment at the top:

```markdown
<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts]
owner: engine-team
status: active
/L9_META -->
```

**YAML / shell** — `#` comment block:

```yaml
# --- L9_META ---
# l9_schema: 1
# origin: engine-specific
# engine: graph
# layer: [domains]
# tags: [domain-spec]
# owner: engine-team
# status: active
# --- /L9_META ---
```

**JSON** uses an `"_l9_meta"` object key; **TOML** uses a `[tool.l9_meta]` table.

## New files

Add a `FileMeta` entry for the file to `FILE_REGISTRY` in `tools/l9_meta_injector.py`, then
run `python tools/l9_meta_injector.py --apply`. A tracked file that is not registered must
still carry a canonical schema-1 header, or the check reports it as `NOHEADER`/`INVALID`.

## Exemptions

- `__init__.py` files may have no header (one that is present is still verified).
- `artifacts/**` is not eligible: those files are regenerated without a header by
  `tools/audit_engine.py`, `tools/spec_extract.py` and `tools/audit_harness.py`.
- `reports/end-session/**` is not eligible: those session and governance handoff
  reports are written on every publish by the governance post-publish hook, without a header.
- `EXCLUDED_PATTERNS` and `HEADERLESS_BASELINE` in `tools/l9_meta_injector.py` list the
  remaining exceptions with their reasons. The baseline is a ratchet of files that predate
  the check; new files are never added to it.

## Wrong

```python
# L9: graph engine, owned by team          # WRONG → not the schema, no delimiters
```

Do not invent fields, reorder them arbitrarily, or place the block below imports. If a
file is missing a header, run the injector rather than pasting one in.

## Verified by

- `tests/contracts/test_contracts.py::TestContract18L9Meta`
- `python tools/l9_meta_injector.py check` (the default dry-run is the same check) — run by
  `.github/workflows/contracts.yml` (`meta-headers`) and the `l9-meta-check` pre-commit hook.
  It considers every `FILE_REGISTRY` entry plus every git-tracked file in the
  `contracts/contract_18.yaml` scope, and exits 1 when any is `PENDING` (would change),
  `MISSING`, `NOHEADER`, `INVALID` (not schema 1) or `ERROR`.
