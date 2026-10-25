#!/usr/bin/env python3
"""CLI: deterministic CEG outcome replay from recorded-event fixtures (TASK-033).

Accepts any input schema registered in engine.replay.outcome.ACCEPTED_INPUT_SCHEMAS
(today the Odoo exporter's l9.odoo.outcome_replay_input.v1).

No Gate / Neo4j / network calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.replay.outcome import ReplayError, replay_outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CEG offline outcome replay")
    parser.add_argument("--input", required=True, help="Recorded-event replay input JSON (registered producer schema)")
    parser.add_argument("--output", required=True, help="Write replay outcome JSON")
    args = parser.parse_args(argv)
    try:
        document = json.loads(Path(args.input).read_text(encoding="utf-8"))
        result = replay_outcomes(document)
    except (OSError, json.JSONDecodeError, ReplayError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(out),
                "outcome_set_hash": result["outcome_set_hash"],
                "event_count": result["event_count"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
