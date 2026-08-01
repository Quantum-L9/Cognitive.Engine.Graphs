# Machine-Readable Contract Specifications

Each YAML file defines one CEG contract with:
- **id**: Unique contract identifier
- **name**: Human-readable name
- **layer**: Which governance layer (chassis, packet, security, engine, testing, intelligence, hardening)
- **level**: MUST (violation = blocked) or SHOULD (quality measure)
- **scope.paths**: Which files this contract applies to
- **preconditions**: What triggers this contract
- **postconditions**: What must be true after
- **verification.scanner_rules**: contract_scanner.py rule IDs
- **verification.test**: pytest node ID (`file.py::TestClass`) that verifies this contract
- **docs**: the prose contract doc(s) in `docs/contracts/` covering this invariant

## Relationship to `docs/contracts/`

The two folders are one registry split by concern:

| Folder | Owns |
|---|---|
| `contracts/*.yaml` | Contract **identity and wiring** — id, layer, level, scope, scanner rules, test node ID, docs pointer |
| `docs/contracts/*.md` | **Prose** — the human/agent-facing explanation, correct/wrong examples |

`tests/contracts/test_contract_registry.py` is the drift gate: it fails if a `docs:` path
does not exist, a `scanner_rules` entry is not registered in `tools/contract_scanner.py`,
a `verification.test` node ID does not resolve, or a required doc is claimed by no
contract.

## Usage

```bash
python3 tools/contract_report.py    # prints a per-contract verification-coverage table
python3 tools/verify_contracts.py   # asserts every required doc exists and is wired
python3 tools/contract_scanner.py   # greps the codebase for banned patterns
```

`contract_report.py` prints its table to stdout; it writes no artifact. Do not confuse it
with `artifacts/coverage_matrix.json`, which is produced by `tools/spec_extract.py` and
measures **domain-spec feature coverage**, not contract verification coverage.
