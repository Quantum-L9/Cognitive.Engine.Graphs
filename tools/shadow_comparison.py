#!/usr/bin/env python3
"""CLI: emit observational CEG shadow comparison JSON (TASK-055).

Does not call Neo4j or alter primary match responses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.shadow.compare import RankedCandidate, emit_shadow_comparison


def _load_ranked(items: list[dict]) -> list[RankedCandidate]:
    out: list[RankedCandidate] = []
    for item in items:
        out.append(
            RankedCandidate(
                candidate_id=str(item["candidate_id"]),
                score=float(item["score"]),
                rank=int(item["rank"]),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Emit CEG shadow comparison artifact")
    p.add_argument("--input", required=True, help="JSON with packet_id, primary[], shadow[]")
    p.add_argument("--output", required=True, help="Write comparison JSON here")
    args = p.parse_args(argv)
    data = json.loads(Path(args.input).read_text())
    comparison = emit_shadow_comparison(
        packet_id=str(data["packet_id"]),
        primary=_load_ranked(data.get("primary") or []),
        shadow=_load_ranked(data.get("shadow") or []),
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = comparison.to_dict()
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {"ok": True, "output": str(out), "checksum": payload["checksum"], "mismatches": len(payload["mismatches"])}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
