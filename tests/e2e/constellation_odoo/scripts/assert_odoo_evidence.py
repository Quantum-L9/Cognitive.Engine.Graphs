#!/usr/bin/env python3
"""One verdict from one Odoo-rail evidence bundle.

  assert_odoo_evidence.py <bundle-dir>

MANDATORY checks must be PASS. A mandatory check that is missing — its phase
crashed or never ran — is a FAIL, never a skip. MATCH checks cover the
Odoo -> Gate -> CEG leg and are reported but do not gate the Odoo -> EIE verdict
the rail was built for. GAP probes are authorization findings: they record
what an admitted consumer key can do, and are reported, not gated. Test
accommodations read from image labels are listed so a PASS can never hide one.
PROVENANCE is mandatory: every node image must carry the revision of the source
tree recorded for the run, and every image must contain the same, known SDK
commit — otherwise the verdict would certify a release set it did not run.
Exit 0 only when every mandatory check passed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MANDATORY = {
    "configure": ["O_INSTALL", "O_CONFIG", "O_ISOLATION"],
    "transport": ["O_T1_ROUNDTRIP", "O_T2_OPERATOR_FAIL_CLOSED"],
    "business": [
        "O_B1_CONVERGE_REVIEW",
        "O_B2_NO_WRITE_BEFORE_APPROVAL",
        "O_B3_APPROVE_INJECT",
        "O_B4_IDEMPOTENT_REPLAY",
    ],
    "adversarial": [
        "O_N1_UNSIGNED_REJECTED",
        "O_N2_UNKNOWN_CONSUMER_REJECTED",
        "O_N3_FORGED_SIGNATURE_REJECTED",
        "O_N4_CONSUMER_SELF_REGISTRATION_REJECTED",
        "O_N5_DIRECT_WORKER_BYPASS_IMPOSSIBLE",
    ],
}
MATCH = {"match": ["O_M1_MATCH_ODOO_CONTRACT", "O_M2_MATCH_CEG_SPEC_DIRECTION"]}
# Authorization probes that must be refused now that Gate scopes consumer keys.
MUST_ENFORCE = {"adversarial": ["O_G1_CONSUMER_ACTION_SCOPE"]}
PROPOSALS = {"business": ["O_P1_SCHEMA_FIX_REACHES_WRITEBACK"]}
GAPS = {
    "transport": ["O_F1_EIE_EMPTY_RESULT_REPORTED_COMPLETED"],
    "adversarial": [
        "O_G2_KEY_TO_IDENTITY_BINDING",
        "O_G3_KEY_TO_TENANT_BINDING",
        "O_G4_REGISTRY_DISCLOSURE",
    ],
}


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


IMAGE_NODES = ("gate", "eie", "ceg", "odoo")


def provenance_problems(prov: dict, sources: dict) -> list[str]:
    problems = []
    for node in IMAGE_NODES:
        info = prov.get(node) or {}
        rev = (info.get("labels") or {}).get("org.opencontainers.image.revision")
        head = (sources.get(node) or {}).get("head")
        if not rev or not head or rev != head:
            problems.append(f"{node} image {str(rev)[:12]} != source {str(head)[:12]}")
    sdk = {(prov.get(n) or {}).get("sdk_commit") for n in IMAGE_NODES}
    if None in sdk or "" in sdk or len(sdk) != 1:
        problems.append(f"sdk commits not aligned: {sorted(str(c)[:12] for c in sdk)}")
    return problems


def main(bundle: Path) -> int:
    rows: list[tuple[str, str, str]] = []
    failed = 0

    def status_of(phase: str, check: str) -> str:
        data = load(bundle / "flows" / f"odoo_{phase}.json")
        return str(((data.get("checks") or {}).get(check) or {}).get("status", "NOT_RUN"))

    for phase, checks in MANDATORY.items():
        for c in checks:
            st = status_of(phase, c)
            ok = st == "PASS"
            failed += 0 if ok else 1
            rows.append(("MANDATORY", c, st if ok else f"{st} -> FAIL"))

    for phase, checks in MUST_ENFORCE.items():
        for c in checks:
            st = status_of(phase, c)
            ok = st == "ENFORCED"
            failed += 0 if ok else 1
            rows.append(("MANDATORY", c, st if ok else f"{st} -> FAIL"))

    recovery = load(bundle / "flows" / "gate_restart_recovery.json").get("verdict", "NOT_RUN")
    failed += 0 if recovery == "PASS" else 1
    rows.append(("MANDATORY", "GATE_RESTART_RECOVERY", recovery))

    iso = load(bundle / "flows" / "isolation.json").get("verdict", "NOT_RUN")
    failed += 0 if iso == "PASS" else 1
    rows.append(("MANDATORY", "DOCKER_ISOLATION", iso))

    scan = load(bundle / "secret_scan.json").get("secret_scan", "NOT_RUN")
    failed += 0 if scan == "PASS" else 1
    rows.append(("MANDATORY", "EVIDENCE_no_secrets", scan))

    registry = load(bundle / "gate_registry.json")
    reg_ok = {"enrichment-engine", "graph"} <= set(registry)
    failed += 0 if reg_ok else 1
    rows.append(("MANDATORY", "REG_live_registration", "PASS" if reg_ok else f"FAIL {sorted(registry)}"))

    for phase, checks in MATCH.items():
        for c in checks:
            rows.append(("MATCH", c, status_of(phase, c)))
    for phase, checks in PROPOSALS.items():
        for c in checks:
            rows.append(("PROPOSAL", c, status_of(phase, c)))
    for phase, checks in GAPS.items():
        for c in checks:
            rows.append(("FINDING", c, status_of(phase, c)))

    prov = load(bundle / "image_provenance.json")
    problems = provenance_problems(prov, load(bundle / "source_revisions.json"))
    failed += 1 if problems else 0
    rows.append(("MANDATORY", "PROVENANCE_images_and_sdk", "; ".join(problems) + " -> FAIL" if problems else "PASS"))
    deviations = []
    for node, info in sorted(prov.items()):
        acc = (info.get("labels") or {}).get("io.l9.e2e.accommodation")
        if acc:
            deviations.append(f"{node}: image accommodation {acc}")
    profile = bundle / "eie_business_profile.txt"
    if profile.exists():
        deviations.append(f"eie business phase: {profile.read_text().strip()}")
    sdk = {n: (i.get("sdk_commit") or "?")[:12] for n, i in prov.items()}

    width = max(len(r[1]) for r in rows)
    for kind, name, st in rows:
        sys.stdout.write(f"{kind:<10} {name:<{width}}  {st}\n")
    sys.stdout.write(f"SDK commits: {json.dumps(sdk)}\n")
    sys.stdout.write(f"SDK aligned: {len(set(sdk.values())) == 1 and '?' not in sdk.values()}\n")
    for d in deviations:
        sys.stdout.write(f"DEVIATION  {d}\n")
    verdict = "PASS" if failed == 0 else f"FAIL ({failed} mandatory)"
    sys.stdout.write(f"VERDICT: {verdict}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(str(__doc__) + "\n")
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
