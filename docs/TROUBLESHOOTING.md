<!-- L9_META
l9_schema: 2
origin: engine-specific
engine: graph
layer: [scripts]
tags: [platform]
status: active
/L9_META -->

# TROUBLESHOOTING.md — CEG Common Issues & Resolutions

**Purpose**: Agent reference for diagnosing and resolving common development, CI, and runtime failures.

**Provenance**: Extracted from external audit documentation pack (2026-04-02). Verified against live repo 2026-04-26.

---

## Neo4j Connection Failures

### Symptom
```
neo4j.exceptions.ServiceUnavailable: Failed to establish connection
```

### Diagnosis
```bash
# Check Neo4j is running
docker ps | grep neo4j

# Check connection config
grep NEO4J_URI .env
```

### Resolution
```bash
# Start Neo4j via docker-compose
make dev

# Or manually
docker run -d \
  --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  neo4j:5

# Update .env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password
```

**Agent Action**: If connection fails in integration tests, ensure `testcontainers` has Docker access.

---

## Contract Scanner False Positives

### Symptom
```
❌ SEC-001: f-string Cypher MATCH without sanitize_label()
File: engine/gates/custom.py:42
```

### Diagnosis
```python
# Check if label was actually sanitized
from engine.utils.security import sanitize_label

label = sanitize_label(spec.targetnode)  # ✅ sanitized
cypher = f"MATCH (n:{label})"  # ❌ still flagged (false positive)
```

### Resolution
```python
# Contract scanner doesn't track variable flow — use inline pattern
from engine.utils.security import sanitize_label

cypher = f"MATCH (n:{sanitize_label(spec.targetnode)})"  # ✅ passes scanner
```

**Agent Action**: When scanner flags sanitized code, inline the `sanitize_label()` call in the f-string.

---

## Pre-Commit Hook Failures

### Symptom
```
ruff....................................................................Failed
- hook id: ruff
- exit code: 1

engine/gates/new_gate.py:12:5: F401 [*] `unused_import` imported but unused
```

### Diagnosis
```bash
# Run ruff manually to see all errors
ruff check engine/gates/new_gate.py
```

### Resolution
```bash
# Auto-fix most issues
ruff check --fix engine/gates/new_gate.py

# Format code
ruff format engine/gates/new_gate.py

# Re-run pre-commit
git add engine/gates/new_gate.py
git commit -m "fix: gate implementation"
```

**Agent Action**: Always run `make lint-fix` before committing.

---

## `make cypher-lint` Failures

### Symptom
```
❌ Unparameterized value interpolation detected
File: engine/sync/generator.py:89
Pattern: f"SET n.status = '{status}'"
```

### Diagnosis
This violates **Contract C-009** (Cypher Injection Prevention). Values must be parameterized.

### Resolution
```python
# ❌ WRONG
cypher = f"SET n.status = '{status}'"
await driver.execute_query(cypher)

# ✅ CORRECT
cypher = "SET n.status = $status"
await driver.execute_query(cypher, {"status": status})
```

**Agent Action**: Never interpolate values into Cypher. Only labels (after `sanitize_label()`).

---

## Docker Startup Issues

### Symptom
```
Error response from daemon: Conflict. The container name "/neo4j" is already in use
```

### Diagnosis
```bash
# Check for existing containers
docker ps -a | grep neo4j
```

### Resolution
```bash
# Stop and remove existing container
docker stop neo4j
docker rm neo4j

# Restart via docker-compose
make dev
```

**Agent Action**: `make dev` handles cleanup automatically. Use it instead of manual `docker run`.

---

## Weight Sum Assertion Failure at Startup

### Symptom
```
AssertionError: Default scoring weights sum to 0.95, exceeds ceiling of 1.0
File: engine/boot.py::_assert_default_weight_sum
```

### Diagnosis
This violates **Contract C-022** (Scoring Weight Ceiling). You added a new weight without reducing existing ones.

### Resolution
```python
# In engine/config/settings.py
# Before (sum = 0.95):
w_structural = 0.30
w_geo = 0.25
w_reinforcement = 0.20
w_freshness = 0.10
w_new_dimension = 0.10  # ❌ causes sum = 0.95

# After (sum = 0.95 → 0.90):
w_structural = 0.28  # reduced
w_geo = 0.23        # reduced
w_reinforcement = 0.19  # reduced
w_freshness = 0.10  # unchanged
w_new_dimension = 0.10  # new
```

**Agent Action**: When adding a scoring weight, proportionally reduce existing weights to keep sum ≤ 1.0.

---

## Import Boundary Violations

### Symptom
```
❌ ARCH-001: from fastapi import (in engine/)
File: engine/gates/custom_gate.py:5
```

### Diagnosis
This violates **Contract C-001** (Single Ingress). Engine NEVER imports FastAPI.

### Resolution
```python
# ❌ WRONG (in engine/)
from fastapi import HTTPException
raise HTTPException(status_code=400, detail="Invalid gate")

# ✅ CORRECT (in engine/)
msg = "Invalid gate configuration"
raise ValueError(msg)

# ✅ CORRECT (in chassis/ only)
from fastapi import HTTPException
raise HTTPException(status_code=400, detail="Invalid gate")
```

**Agent Action**: Engine code uses standard Python exceptions. Only chassis/ imports FastAPI.

---

## Test Coverage Below Threshold

### Symptom
```
FAILED: coverage check failed: 68% < 70%
File: engine/gates/new_gate.py (35% coverage)
```

### Diagnosis
```bash
# Check which lines are uncovered
pytest --cov=engine/gates/new_gate.py --cov-report=term-missing
```

### Resolution
```python
# Add missing test cases
# tests/unit/gates/test_new_gate.py

def test_new_gate_compile_happy_path():
    # covers main path

def test_new_gate_null_behavior_pass():
    # covers null_behavior=pass

def test_new_gate_null_behavior_fail():
    # covers null_behavior=fail

def test_new_gate_invalid_spec_raises():
    # covers error path
```

**Agent Action**: Minimum 70% global coverage, 95% for engine/gates/ and engine/scoring/.

---

## CI Fails But Local Tests Pass

### Symptom
```
CI: ❌ test_integration_match_pipeline FAILED
Local: ✅ test_integration_match_pipeline PASSED
```

### Diagnosis
```bash
# Check CI Python version vs local
python --version  # Local: 3.12.2

# Check CI in .github/workflows/ci.yml
# PYTHON_VERSION: '3.12'  # Could be 3.12.0 in CI
```

### Resolution
```bash
# Use exact CI environment locally
docker run -it --rm -v $(pwd):/workspace python:3.12 bash
cd /workspace
pip install -r requirements-ci.txt
pytest tests/integration/
```

**Agent Action**: CI uses pinned versions. Check `ci.yml` for exact ruff/mypy versions (not pyproject.toml dev versions).

---

## Prohibited Factor Compilation Error

### Symptom
```
ProhibitedFactorError: Gate references prohibited field 'age'
File: domains/plasticos_domain_spec.yaml:45
```

### Diagnosis
This violates **Contract C-010** (Prohibited Factors). Age is banned at compile-time.

### Resolution
```yaml
# ❌ WRONG
gates:
  - type: range
    candidateprop: age  # prohibited
    min: 18
    max: 65

# ✅ CORRECT
gates:
  - type: range
    candidateprop: experience_years  # allowed proxy
    min: 0
    max: 40
```

**Prohibited fields**: race, ethnicity, religion, gender, age, disability, familial_status, national_origin

**Agent Action**: Never reference prohibited fields in gate specs. Compilation will fail.

---

## Missing L9_META Header

### Symptom
```
MISSING  engine/gates/new_gate.py

606/607 files consistent (1 missing, 0 drift, 0 misplaced, 0 invalid)
```

### Diagnosis
This violates **Contract C-018** (L9_META Headers). Every tracked file needs
metadata. Field values are not authored per file — they are resolved from
`l9-meta.yaml` by path, so a missing header usually just means the file is new.

Other statuses the check can report:

| Status | Meaning |
|---|---|
| `MISSING` | No header at all |
| `DRIFT` | Header present, values disagree with `l9-meta.yaml` |
| `MISPLACED` | Values correct, but the block is in the wrong position |
| `INVALID` | Value outside the vocabulary, or tag facet cardinality violated |

### Resolution
```bash
# Write or repair headers for every tracked file
python tools/l9_meta_injector.py apply

# Confirm
python tools/l9_meta_injector.py check
```

`apply` writes this into a Python file, merging into the existing module
docstring rather than adding a second one:

```python
"""New gate implementation.

--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [gates]
tags: [matching, gates]
status: active
--- /L9_META ---
"""
```

If `check` reports `INVALID`, the fix is in `l9-meta.yaml`, not the file — add
the value to `vocabulary` or correct the rule that resolves that path.

**Agent Action**: Run `tools/l9_meta_injector.py apply` on new files before
committing. The `l9-meta-check` pre-commit hook blocks the commit otherwise.

---

## Unknown / Undocumented Error

If you encounter an error not listed here:

1. **Search the codebase**:
   ```bash
   rg "error_message_substring" engine/ chassis/
   ```

2. **Check contract scanner rules**:
   ```bash
   rg "error_code" tools/contract_scanner.py
   ```

3. **Review recent changes**:
   ```bash
   git log --oneline --since="1 week ago" -- engine/
   ```

4. **Report to Founder**:
   - Include: full error message, file/line number, steps to reproduce
   - Attach: relevant code snippet, CI logs if applicable
   - Label: "troubleshooting-gap" for documentation update

**Agent Action**: Do not guess or invent solutions. Surface unknowns to Founder.

---

## Related Documents

- **GUARDRAILS.md** — Safety rules and forbidden patterns
- **AGENTS.md** — Commands and git workflow
- **TESTING.md** — Test structure and coverage thresholds
- **.cursorrules** — Banned patterns registry with rule IDs
- **ci.yml** — CI pipeline configuration
