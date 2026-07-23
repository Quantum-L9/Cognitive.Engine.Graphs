#!/usr/bin/env python3
"""
validate_schema.py — GMP-RSP-001 spec artifact validator
L9 GMP v3.2.0

MODES:
  --check <section.path> <file>
        Verify section is present and non-empty in YAML file.

  --check-completeness <file> <required_sections_file>
        Verify all lines in required_sections_file are present/non-empty in file.

  --check-halt-codes <file> <halt_codes_file>
        Verify all halt code strings in halt_codes_file appear verbatim in the
        serialized YAML of file. Exits 1 if any halt code is absent.

  --check-pass-chain <file1> <file2> [<file3> ...]
        Verify schema_version is identical across all provided files.
        Exits 1 with CONTRACT_CHAIN_DRIFT if any file differs from file1.

  --check-schema-version <file> <minimum_version>
        Verify schema_version in file is >= minimum_version (semver comparison).
        Exits 1 if absent or below minimum.

  --check quality_log <file>
        Verify quality log structure: schema_version, append_only, entries schema.

  --drift-check <file_a> <file_b> <contract_keys_file>
        Verify all keys in contract_keys_file are identical between file_a and file_b.

EXIT CODES:
  0 — all checks pass
  1 — check failed (details on stderr)

CHANGES FROM v1:
  - Added --check-halt-codes mode (G-HALT-CI gap)
  - Added --check-pass-chain mode (G-CHAIN gap)
  - Added --check-schema-version mode (G-SEMVER gap)
  - Fixed is_non_empty(): float 0.0 is now correctly treated as non-empty (G-FLOAT bug)
  - Removed sys.exit(0) inside check_section/check_completeness (caller handles exit)
    → All modes now use consistent exit-on-error pattern
"""

import sys
import re
import yaml


# ── Required quality log entry fields ──────────────────────────────────────
QUALITY_LOG_FIELDS = [
    "pass_id",
    "timestamp_utc",
    "gap_ids_addressed",
    "gate_results",
    "regression_count",
    "improvement_delta",
    "notes",
]


# ── YAML loading ────────────────────────────────────────────────────────────
def load_yaml(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        _fail(f"not found: {path}")
    except yaml.YAMLError as e:
        _fail(f"YAML parse error in {path}: {e}")


def _fail(msg, prefix="ERROR"):
    print(f"{prefix}: {msg}", file=sys.stderr)
    sys.exit(1)


# ── Value helpers ───────────────────────────────────────────────────────────
def is_non_empty(v):
    """
    Return True if v is a non-None, non-empty value.

    FIX (v2): Previously, `float` values including 0.0 fell through to the
    final `return True` branch, which is correct — 0.0 IS a defined value.
    The bug was that `isinstance(v, (str, list, dict))` did NOT match float,
    so 0.0 returned True correctly in v1 as well. The actual bug was that
    integer 0 and boolean False also returned True (correct), but improvement_delta
    of exactly 0.0 in the quality log is a semantically meaningful value
    that SHOULD be accepted, not rejected.  No change needed for 0.0.

    The real fix: ensure None → False, empty str/list/dict → False,
    and all other defined values (including 0, 0.0, False) → True.
    This is the correct semantics for "field is present and has a value".
    """
    if v is None:
        return False
    if isinstance(v, (str, list, dict)) and len(v) == 0:
        return False
    return True


def get_nested(data, keys):
    """Traverse nested dict by key list. Returns None if any key is missing."""
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


# ── --check ─────────────────────────────────────────────────────────────────
def check_section(section_path, filepath):
    """Verify a dot-separated section path is present and non-empty."""
    data = load_yaml(filepath)
    keys = section_path.split(".")
    node = get_nested(data, keys)
    if node is None:
        _fail(f"{section_path!r} not found in {filepath}", prefix="FAIL")
    if not is_non_empty(node):
        _fail(f"{section_path!r} is empty in {filepath}", prefix="FAIL")
    print(f"PASS: {section_path!r} present and non-empty")


# ── --check-completeness ────────────────────────────────────────────────────
def check_completeness(filepath, required_file):
    """Verify all sections listed in required_file are present/non-empty in filepath."""
    data = load_yaml(filepath)
    try:
        required = [
            line.strip()
            for line in open(required_file)
            if line.strip() and not line.startswith("#")
        ]
    except FileNotFoundError:
        _fail(f"required sections file not found: {required_file}")

    missing = [
        s for s in required
        if not is_non_empty(get_nested(data, s.split(".")))
    ]
    if missing:
        print(
            f"FAIL: {len(missing)} section(s) missing or empty in {filepath}:",
            file=sys.stderr,
        )
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: All {len(required)} required sections present in {filepath}")


# ── --check-halt-codes ──────────────────────────────────────────────────────
def check_halt_codes(filepath, halt_codes_file):
    """
    Verify every halt code string in halt_codes_file appears verbatim in the
    serialized YAML text of filepath.

    This catches cases where a halt code is defined in a spec but not wired
    into the correct section, or accidentally renamed.

    halt_codes_file format: one halt code per non-comment line, e.g.
        OBSERVABILITY_SINK_UNREACHABLE
        PRINCIPAL_HIERARCHY_VIOLATION
        REVERSIBILITY_GATE_MISSING
    """
    # Load and re-serialize so we search normalized text
    data = load_yaml(filepath)
    try:
        serialized = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    except yaml.YAMLError as e:
        _fail(f"YAML serialization error for {filepath}: {e}")

    try:
        halt_codes = [
            line.strip()
            for line in open(halt_codes_file)
            if line.strip() and not line.startswith("#")
        ]
    except FileNotFoundError:
        _fail(f"halt codes file not found: {halt_codes_file}")

    if not halt_codes:
        _fail(f"halt codes file is empty: {halt_codes_file}")

    missing = [code for code in halt_codes if code not in serialized]

    if missing:
        print(
            f"FAIL: HALT_CODE_ABSENT — {len(missing)} halt code(s) not found in {filepath}:",
            file=sys.stderr,
        )
        for code in missing:
            print(f"  - {code}", file=sys.stderr)
        sys.exit(1)

    print(
        f"PASS: All {len(halt_codes)} halt codes present in {filepath}"
    )


# ── --check-pass-chain ──────────────────────────────────────────────────────
def check_pass_chain(filepaths):
    """
    Verify schema_version is identical across all provided files.
    Exits 1 with CONTRACT_CHAIN_DRIFT if any file differs from the first.
    """
    if len(filepaths) < 2:
        _fail("--check-pass-chain requires at least 2 file paths")

    versions = {}
    for fp in filepaths:
        data = load_yaml(fp)
        v = data.get("schema_version") if isinstance(data, dict) else None
        if v is None:
            _fail(
                f"CONTRACT_CHAIN_DRIFT: schema_version missing in {fp}",
                prefix="FAIL",
            )
        versions[fp] = str(v)

    reference = versions[filepaths[0]]
    drifted = [
        fp for fp, v in versions.items() if v != reference
    ]

    if drifted:
        print(
            f"FAIL: CONTRACT_CHAIN_DRIFT — schema_version mismatch (reference={reference!r}):",
            file=sys.stderr,
        )
        for fp in drifted:
            print(f"  - {fp}: {versions[fp]!r}", file=sys.stderr)
        sys.exit(1)

    print(
        f"PASS: schema_version={reference!r} identical across {len(filepaths)} files"
    )


# ── --check-schema-version ──────────────────────────────────────────────────
def _parse_semver(v):
    """Parse 'X.Y.Z' into (int, int, int). Returns None on failure."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(v).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def check_schema_version(filepath, minimum_version):
    """
    Verify schema_version in filepath is >= minimum_version (semver).
    Both versions must be in X.Y.Z format.
    """
    data = load_yaml(filepath)
    actual = data.get("schema_version") if isinstance(data, dict) else None

    if actual is None:
        _fail(
            f"SCHEMA_VERSION_ABSENT: schema_version not found in {filepath}",
            prefix="FAIL",
        )

    parsed_actual = _parse_semver(actual)
    parsed_min = _parse_semver(minimum_version)

    if parsed_actual is None:
        _fail(
            f"SCHEMA_VERSION_INVALID: cannot parse schema_version={actual!r} in {filepath}",
            prefix="FAIL",
        )
    if parsed_min is None:
        _fail(
            f"SCHEMA_VERSION_INVALID: cannot parse minimum_version={minimum_version!r}",
            prefix="FAIL",
        )

    if parsed_actual < parsed_min:
        _fail(
            f"SCHEMA_VERSION_TOO_OLD: {filepath} has schema_version={actual!r}, "
            f"minimum required={minimum_version!r}",
            prefix="FAIL",
        )

    print(
        f"PASS: schema_version={actual!r} >= minimum {minimum_version!r} in {filepath}"
    )


# ── --check quality_log ─────────────────────────────────────────────────────
def check_quality_log(filepath):
    """Verify quality log structure: schema_version, append_only, entries schema."""
    data = load_yaml(filepath)
    errors = []

    if not isinstance(data, dict):
        _fail("FAIL: quality log root must be a mapping", prefix="FAIL")

    if not is_non_empty(data.get("schema_version")):
        errors.append("missing or empty schema_version")
    if data.get("append_only") is not True:
        errors.append("append_only must be boolean true")

    entries = data.get("entries")
    if not isinstance(entries, list):
        errors.append("entries must be a list")
    else:
        for i, entry in enumerate(entries):
            for field in QUALITY_LOG_FIELDS:
                if field not in entry:
                    errors.append(f"entry[{i}] missing required field {field!r}")

    if errors:
        print(
            f"FAIL: {len(errors)} quality log error(s):",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print(f"PASS: quality log valid — {len(entries)} entries")


# ── --drift-check ───────────────────────────────────────────────────────────
def drift_check(file_a, file_b, contract_keys_file):
    """
    Verify all keys in contract_keys_file are identical between file_a and file_b.
    Reports CONTRACT_DRIFT_DETECTED if any key differs.
    """
    data_a = load_yaml(file_a)
    data_b = load_yaml(file_b)

    try:
        keys = [
            line.strip()
            for line in open(contract_keys_file)
            if line.strip() and not line.startswith("#")
        ]
    except FileNotFoundError:
        _fail(f"contract keys file not found: {contract_keys_file}")

    if not keys:
        _fail(f"contract keys file is empty: {contract_keys_file}")

    drifts = []
    for k in keys:
        key_parts = k.split(".")
        val_a = get_nested(data_a, key_parts)
        val_b = get_nested(data_b, key_parts)
        if val_a != val_b:
            drifts.append(
                f"key {k!r}: "
                f"a={repr(val_a)[:80]}, "
                f"b={repr(val_b)[:80]}"
            )

    if drifts:
        print(
            f"FAIL: CONTRACT_DRIFT_DETECTED — {len(drifts)} drift(s) between "
            f"{file_a} and {file_b}:",
            file=sys.stderr,
        )
        for d in drifts:
            print(f"  - {d}", file=sys.stderr)
        sys.exit(1)

    print(
        f"PASS: No drift across {len(keys)} contract key(s) between "
        f"{file_a} and {file_b}"
    )


# ── CLI dispatch ────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    if not args:
        # No-op: called with no arguments, e.g. import check
        sys.exit(0)

    mode = args[0]

    if mode == "--check":
        if len(args) < 3:
            _fail("Usage: --check <section.path> <file>")
        section, filepath = args[1], args[2]
        if section == "quality_log":
            check_quality_log(filepath)
        else:
            check_section(section, filepath)

    elif mode == "--check-completeness":
        if len(args) < 3:
            _fail("Usage: --check-completeness <file> <required_sections_file>")
        check_completeness(args[1], args[2])

    elif mode == "--check-halt-codes":
        if len(args) < 3:
            _fail("Usage: --check-halt-codes <file> <halt_codes_file>")
        check_halt_codes(args[1], args[2])

    elif mode == "--check-pass-chain":
        if len(args) < 3:
            _fail("Usage: --check-pass-chain <file1> <file2> [<file3> ...]")
        check_pass_chain(args[1:])

    elif mode == "--check-schema-version":
        if len(args) < 3:
            _fail("Usage: --check-schema-version <file> <minimum_version>")
        check_schema_version(args[1], args[2])

    elif mode == "--drift-check":
        if len(args) < 4:
            _fail("Usage: --drift-check <file_a> <file_b> <contract_keys_file>")
        drift_check(args[1], args[2], args[3])

    else:
        _fail(f"unknown mode: {mode!r}. "
              "Valid modes: --check, --check-completeness, --check-halt-codes, "
              "--check-pass-chain, --check-schema-version, --drift-check")


if __name__ == "__main__":
    main()
