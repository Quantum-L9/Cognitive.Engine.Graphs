#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [audit]
tags: [delivery, harness]
status: active
--- /L9_META ---

L9 Contract Files Existence + Wiring Check

Two layered passes:

1. REQUIRED_CONTRACTS -- a literal ratchet floor. Docs on this list must exist and be
   referenced from an agent file. This list only ever grows.
2. contracts/*.yaml `docs:` pointers -- every doc a machine-readable contract claims to
   be described by must also exist and be wired. This catches docs added to the YAML
   registry without being added to the floor.

Exit code 1 = missing file or unwired -> blocks CI/merge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REQUIRED_CONTRACTS = [
    "docs/contracts/FIELD_NAMES.md",
    "docs/contracts/METHOD_SIGNATURES.md",
    "docs/contracts/CYPHER_SAFETY.md",
    "docs/contracts/ERROR_HANDLING.md",
    "docs/contracts/HANDLER_PAYLOADS.md",
    "docs/contracts/PYDANTIC_YAML_MAPPING.md",
    "docs/contracts/DEPENDENCY_INJECTION.md",
    "docs/contracts/TEST_PATTERNS.md",
    "docs/contracts/RETURN_VALUES.md",
    "docs/contracts/BANNED_PATTERNS.md",
    "docs/contracts/PACKET_ENVELOPE_FIELDS.md",
    "docs/contracts/DELEGATION_PROTOCOL.md",
    "docs/contracts/PACKET_TYPE_REGISTRY.md",
    "docs/contracts/DOMAIN_SPEC_VERSIONING.md",
    "docs/contracts/FEEDBACK_LOOPS.md",
    "docs/contracts/NODE_REGISTRATION.md",
    "docs/contracts/ENV_VARS.md",
    "docs/contracts/OBSERVABILITY.md",
    "docs/contracts/MEMORY_SUBSTRATE_ACCESS.md",
    "docs/contracts/SHARED_MODELS.md",
    "docs/contracts/PROHIBITED_FACTORS.md",
    "docs/contracts/PII_HANDLING.md",
    "docs/contracts/BIDIRECTIONAL_MATCHING.md",
    "docs/contracts/L9_META_HEADERS.md",
    "docs/contracts/KGE_EMBEDDINGS.md",
    "docs/contracts/FEATURE_FLAG_DISCIPLINE.md",
    "docs/contracts/SCORING_WEIGHT_CEILING.md",
]

AGENT_FILES = [".cursorrules", "CLAUDE.md", "AGENTS.md"]

CONTRACTS_DIR = "contracts"


def yaml_declared_docs(root: Path, errors: list[str]) -> list[str]:
    """Collect every `docs:` pointer declared across contracts/*.yaml."""
    declared: list[str] = []
    for path in sorted((root / CONTRACTS_DIR).glob("contract_*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            errors.append(f"Cannot parse {path.name}: {e}")
            continue
        for doc in data.get("docs") or []:
            if doc not in declared:
                declared.append(doc)
    return declared


def main() -> int:
    root = Path.cwd()
    errors: list[str] = []

    declared = yaml_declared_docs(root, errors)
    # The literal list is the ratchet floor; YAML pointers layer on top of it.
    to_check = REQUIRED_CONTRACTS + [d for d in declared if d not in REQUIRED_CONTRACTS]

    for rel in to_check:
        path = root / rel
        if not path.is_file():
            errors.append(f"Missing contract file: {rel}")

    # Each contract must be referenced in at least one agent file
    agent_contents: list[tuple[str, str]] = []
    for agent_file in AGENT_FILES:
        path = root / agent_file
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            agent_contents.append((agent_file, content))
        except OSError as e:
            errors.append(f"Cannot read {agent_file}: {e}")

    for rel in to_check:
        name = Path(rel).name
        if not any(name in c or rel in c for _f, c in agent_contents):
            errors.append(f"No agent file references contract: {name}")

    if not errors:
        extra = len(to_check) - len(REQUIRED_CONTRACTS)
        suffix = f" (+{extra} from contracts/*.yaml)" if extra else ""
        print(f"L9 contract files: all {len(to_check)} present and wired{suffix}.")
        return 0

    print("L9 contract verification failed:\n", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
