#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [tooling, ci]
tags: [tooling, ci, compliance]
owner: engine-team
status: active
--- /L9_META ---

Compliance layout checks: banned engine files and design-pack DomainSpec validation.

One owner for what compliance.yml used to run inline, so the workflow and
Repo.mk `repo-validate` (and with it the required Analyze (central Core) job)
run the same code:

* engine/middleware.py must not exist: tenant resolution is chassis-only.
* engine/api/ must not exist: the app factory lives in chassis/chassis_app.py.
* every tests/fixtures/design_packs/<id>/spec.yaml loads as a DomainSpec, and
  at least one does (an empty glob fails rather than validating nothing).

Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BANNED_PATHS: dict[str, str] = {
    "engine/middleware.py": "tenant resolution is chassis-only",
    "engine/api": "the app factory belongs in chassis/chassis_app.py",
}
DESIGN_PACK_GLOB = "tests/fixtures/design_packs/*/spec.yaml"


def banned_path_errors(root: Path) -> list[str]:
    return [f"{path} exists ({reason})" for path, reason in BANNED_PATHS.items() if (root / path).exists()]


def design_pack_errors(root: Path) -> list[str]:
    from engine.config.schema import DomainSpec

    paths = sorted(root.glob(DESIGN_PACK_GLOB))
    if not paths:
        return [f"no {DESIGN_PACK_GLOB} found: the glob matches nothing"]
    errors: list[str] = []
    for path in paths:
        try:
            DomainSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except Exception as exc:  # any load or validation failure is a failed pack
            errors.append(f"{path.relative_to(root)} does not validate as a DomainSpec: {exc}")
        else:
            print(f"  {path.relative_to(root)}: OK")
    return errors


def main(root: Path = ROOT) -> int:
    errors = banned_path_errors(root) + design_pack_errors(root)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print("compliance layout: banned files absent; every design pack validates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
