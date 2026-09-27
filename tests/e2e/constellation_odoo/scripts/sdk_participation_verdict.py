#!/usr/bin/env python3
"""Evidence writers for the SDK participation checks (L9-PARTICIPATION-01).

  sdk_participation_verdict.py boot     <bundle>           P_SDK_NODE_ACTIVE, P_SDK_NODE_ROUTABLE
  sdk_participation_verdict.py registry <bundle> <nodes>   flows/gate_restart_recovery.json
  sdk_participation_verdict.py recovery <bundle>           P_SDK_NODE_RECOVERY

Inputs are the probe files run_odoo_e2e.sh writes into the bundle; every
verdict lands in flows/*.json for assert_odoo_evidence.py.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

SDK_NODES = {"enrichment-engine", "graph", "sdk-minimal-node"}


def _read_ready(path: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3:
            container, code, state = parts
            out[container] = {"http": int(code), "state": state}
    return out


def _flows(bundle: Path) -> Path:
    return bundle / "flows" / "sdk_participation.json"


def boot(bundle: Path) -> int:
    ready = _read_ready(bundle / "sdk_ready_boot.txt")
    try:
        routed = json.loads((bundle / "sdk_routable.txt").read_text().strip().splitlines()[-1])
    except (OSError, IndexError, json.JSONDecodeError):
        text = (bundle / "sdk_routable.txt").read_text() if (bundle / "sdk_routable.txt").exists() else ""
        routed = {"error": text[-600:]}
    try:
        registry = sorted(json.loads((bundle / "gate_registry.json").read_text()))
    except (OSError, json.JSONDecodeError):
        registry = []
    active = len(ready) == 3 and all(v["http"] == 200 and v["state"] == "active" for v in ready.values())
    echo = (routed.get("payload") or {}).get("echo")
    routable = echo == {"probe": "l9e2e"} and routed.get("signing_key_id") == "gate-e2e"
    checks = {
        "P_SDK_NODE_ACTIVE": {
            "status": "PASS" if active and set(registry) >= SDK_NODES else "FAIL",
            "ready": ready,
            "registry": registry,
        },
        "P_SDK_NODE_ROUTABLE": {"status": "PASS" if routable else "FAIL", "response": routed},
    }
    _flows(bundle).write_text(json.dumps({"checks": checks}, indent=1))
    for name, check in checks.items():
        sys.stdout.write(f"  {name}: {check['status']}\n")
    return 0


def registry(bundle: Path, nodes: int) -> int:
    want = {"enrichment-engine", "graph"} | ({"sdk-minimal-node"} if nodes == 3 else set())
    t0 = time.time()
    seen: dict[str, float] = {}
    current: list[str] = []
    while time.time() - t0 < 120:
        try:
            with urllib.request.urlopen("http://127.0.0.1:19000/v1/registry", timeout=4) as resp:
                current = sorted(json.load(resp))
        except Exception:  # Gate is restarting
            current = []
        for name in current:
            seen.setdefault(name, round(time.time() - t0, 1))
        if want <= set(current):
            break
        time.sleep(2)
    result = {
        "reregistered_after_s": seen,
        "registry": current,
        "expected": sorted(want),
        "verdict": "PASS" if want <= set(current) else "FAIL",
    }
    (bundle / "flows" / "gate_restart_recovery.json").write_text(json.dumps(result, indent=1))
    sys.stdout.write(f"gate restart recovery: {result['verdict']} {seen}\n")
    return 0


def recovery(bundle: Path) -> int:
    outage = _read_ready(bundle / "sdk_ready_outage.txt")
    recovered = _read_ready(bundle / "sdk_ready_recovered.txt")
    degraded_file = bundle / "sdk_degraded_after_s.txt"
    degraded_after = degraded_file.read_text().strip() if degraded_file.exists() else None
    dropped = (
        degraded_after is not None
        and len(outage) == 3
        and all(v["http"] == 503 and v["state"] == "degraded" for v in outage.values())
    )
    back = len(recovered) == 3 and all(v["http"] == 200 and v["state"] == "active" for v in recovered.values())
    path = _flows(bundle)
    data = json.loads(path.read_text()) if path.exists() else {"checks": {}}
    data["checks"]["P_SDK_NODE_RECOVERY"] = {
        "status": "PASS" if dropped and back else "FAIL",
        "degraded_after_s": degraded_after,
        "not_ready_during_gate_outage": outage,
        "ready_after_gate_returned": recovered,
    }
    path.write_text(json.dumps(data, indent=1))
    sys.stdout.write(f"  P_SDK_NODE_RECOVERY: {data['checks']['P_SDK_NODE_RECOVERY']['status']}\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "boot":
        return boot(Path(argv[2]))
    if len(argv) == 4 and argv[1] == "registry":
        return registry(Path(argv[2]), int(argv[3]))
    if len(argv) >= 3 and argv[1] == "recovery":
        return recovery(Path(argv[2]))
    sys.stderr.write(str(__doc__) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
