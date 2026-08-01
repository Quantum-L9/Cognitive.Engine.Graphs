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

**JSON** uses an `"_l9_meta"` object key; **TOML** uses an `[l9_meta]` table.

## Exemptions

`__init__.py` files are exempt from the engine header check.

## Wrong

```python
# L9: graph engine, owned by team          # WRONG → not the schema, no delimiters
```

Do not invent fields, reorder them arbitrarily, or place the block below imports. If a
file is missing a header, run the injector rather than pasting one in.

## Verified by

- `tests/contracts/test_contracts.py::TestContract18L9Meta`
- `tools/l9_meta_injector.py`
