<!-- L9_META
l9_schema: 1
origin: l9-template
engine: graph
layer: [docs, contracts]
tags: [L9_TEMPLATE, contracts, cypher]
owner: platform
status: active
/L9_META -->

<!-- L9_TEMPLATE: true -->
# L9 Cypher Safety Contract

## Rule 1: Labels and Relationship Types → sanitize_label()
Labels/types CANNOT be parameterized in Neo4j. They MUST be validated via
`sanitize_label()` before f-string interpolation.

```python
# ✅ CORRECT
label = sanitize_label(spec.target_node)
cypher = f"MATCH (n:{label})"

# ❌ WRONG — unsanitized
cypher = f"MATCH (n:{spec.target_node})"
```


## Rule 2: ALL Values → Cypher Parameters (\$var)

Every value that comes from user input, payload, or domain spec properties
MUST use Cypher parameterization. This includes LIMIT, SKIP, property values.

```python
# ✅ CORRECT
cypher = "MATCH (n:Facility) RETURN n LIMIT $limit"
parameters = {"limit": top_n}

# ❌ WRONG — f-string interpolation of value
cypher = f"MATCH (n:Facility) RETURN n LIMIT {top_n}"
```


## Rule 3: No eval(), no exec(), no compile()

These functions are BANNED in all engine code. No exceptions. No "safe" wrappers.

```python
# ❌ BANNED — even with restricted builtins
result = eval(expression, {"__builtins__": {}}, safe_vars)

# ✅ CORRECT — use explicit computation
if expression_type == "multiply":
    result = a * b
elif expression_type == "convert":
    result = value * conversion_factor
```

For derived parameter computation, use a whitelist dispatch table:

```python
OPERATORS: dict[str, Callable] = {
    "multiply": operator.mul,
    "divide": operator.truediv,
    "add": operator.add,
    "min": min,
    "max": max,
}
```


## Rule 4: Query Dict Keys

There is no engine-wide allowlist of query keys: the keys a match may carry are the ones the
domain declares. A `$parameter` in compiled Cypher is one of

- a `queryschema.fields` name (backfilled with its declared default),
- a `derivedparameters` name (resolved by `ParameterResolver`),
- a scoring dimension's `queryprop` (backfilled with null), or
- a value a compiler binds itself under a generated, sanitized name.

Query **values** only ever travel as `$params`. Parameter **names** that reach Cypher text are
validated like labels (C-009: `sanitize_label`, identifier pattern); a gate whose parameter is
not an identifier is refused by the compiler and withholds the pack at admission
(`engine/config/loader.py::admission_defects`). With `domain_undeclared_param_rejected` a gate
referencing an undeclared parameter is refused at spec load.

```python
# WRONG — a static key list names one domain's vocabulary and drifts from the spec
ALLOWED_QUERY_KEYS = {"capacity", "material_type", ...}

# RIGHT — the declared contract is the allowlist
declared = {f.name for f in spec.queryschema.fields} | {p.name for p in spec.derivedparameters}
```


## Enforcement

- `tools/audit_engine.py` regex rule `SEC_LABEL_SANITIZATION_REQUIRED`
- `tools/audit_engine.py` pattern rule for `eval(`, `exec(`, `compile(`
- Pre-commit hook scans for unsafe patterns
- CI: `tools/contract_scanner.py` blocks PRs with violations, in `contracts.yml` and in the required Analyze (central Core) job (`make validate`)

```
