"""Odoo consumer scenarios for the Constellation Docker rail.

Runs INSIDE the real Odoo 19 container, under ``odoo shell`` against a
database where plasticos_gate / plasticos_enrichment / plasticos_matching were
installed by Odoo itself. ``env`` is Odoo's real Environment — there is no ORM
stand-in anywhere in this file. Every Gate call goes through the addon code
Odoo runs in production (plasticos_gate.services.*) and the installed
Gate_SDK; the driver never builds, signs or posts a packet on the positive
path.

Adversarial scenarios that need a caller Odoo would never be (a rogue key, a
spoofed node name) use the installed SDK's public GateClient directly, from
the same container and network position as Odoo.

Phase is selected by L9E2E_PHASE: configure | transport | business | match |
adversarial | consumer_sdk. One JSON object per phase is printed between the
L9E2E-RESULT-BEGIN/END markers. Secrets are never printed.
"""

# ruff: noqa: F821
# `env` is injected by `odoo shell`; this file is not importable on its own.
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any

PHASE = os.environ.get("L9E2E_PHASE", "")
GATE_URL = "http://gate:9000"
TENANT = "plasticos"
RESULT: dict[str, Any] = {"phase": PHASE, "checks": {}}

ICP_BASELINE = {
    "plasticos.gate.url": GATE_URL,
    "plasticos.gate.allow_insecure_http": "1",  # in-network plain HTTP, see README
    "plasticos.gate.local_node": "odoo",
    "plasticos.gate.org_id": TENANT,
    "plasticos.gate.signing_key_id": "odoo-e2e",
    "plasticos.gate.signing_algorithm": "hmac-sha256",
    "plasticos.gate.verify_response_signatures": "1",
    "plasticos.gate.enrichment_enabled": "1",
    "plasticos.gate.matching_enabled": "1",
    "plasticos.gate.timeout_seconds": "30",
    "plasticos.gate.auto_writeback": "0",
    "plasticos.gate.auto_writeback_operator_approved": "0",
}


def check(name: str, status: str, **evidence: Any) -> None:
    RESULT["checks"][name] = {"status": status, **evidence}


def packet_summary(packet: Any) -> dict[str, Any]:
    sec = packet.security
    return {
        "packet_id": str(packet.header.packet_id),
        "packet_type": str(packet.header.packet_type),
        "action": packet.header.action,
        "source_node": packet.address.source_node,
        "destination_node": packet.address.destination_node,
        "signing_key_id": sec.signing_key_id,
        "signature_present": sec.signature is not None,
        "idempotency_key": packet.header.idempotency_key,
        "correlation_id": packet.header.correlation_id,
    }


def icp() -> Any:
    return env["ir.config_parameter"].sudo()


def apply_icp(overrides: dict[str, str] | None = None) -> None:
    for key, value in {**ICP_BASELINE, **(overrides or {})}.items():
        icp().set_param(key, value)
    env.cr.commit()


def installed_modules() -> dict[str, str]:
    mods = env["ir.module.module"].sudo().search([("name", "like", "plasticos_%"), ("state", "=", "installed")])
    return {m.name: m.latest_version or "" for m in mods}


def new_partner(tag: str) -> Any:
    # Blank website/city/phone/email so a merge-not-overwrite writeback has
    # something legitimate to fill.
    partner = env["res.partner"].create({"name": f"L9 E2E Polymer Recycler {tag}", "is_company": True})
    env.cr.commit()
    return partner


def new_run(partner: Any) -> Any:
    run = env["plasticos.enrichment.run"].create({"partner_id": partner.id})
    env.cr.commit()
    return run


def execute_run(run: Any) -> dict[str, Any]:
    """Press the real Execute button (action_execute) and read back the run."""
    from odoo.exceptions import UserError

    raised = None
    try:
        run.action_execute()
        env.cr.commit()
    except UserError as exc:
        raised = str(exc)
        env.cr.rollback()
    run.invalidate_recordset()
    fresh = env["plasticos.enrichment.run"].browse(run.id)
    proposal = fresh.gate_proposal if isinstance(fresh.gate_proposal, dict) else {}
    return {
        "run_id": fresh.id,
        "state": fresh.state,
        "engine_used": fresh.engine_used,
        "failure_class": fresh.failure_class or None,
        "availability_status": fresh.availability_status or None,
        "gate_packet_id": fresh.gate_packet_id or None,
        "gate_correlation_id": fresh.gate_correlation_id or None,
        "validation_issues": fresh.validation_issues or None,
        "user_error": raised,
        "proposed_partner_fields": sorted((proposal.get("proposed_partner_fields") or {}).keys()),
        "eie_provenance_keys": sorted((proposal.get("eie_provenance") or {}).keys()),
    }


def direct_converge(run: Any) -> dict[str, Any]:
    """The same adapter call _run_gate_converge makes, keeping the raw reply."""
    from odoo.addons.plasticos_gate.services.gate_builders import build_converge_request
    from odoo.addons.plasticos_gate.services.gate_client import send_converge_action

    request = build_converge_request(env, run)
    result = send_converge_action(
        env,
        payload=request.to_dict(),
        correlation_id=request.odoo.get("correlation_id") if request.odoo else None,
        idempotency_key=request.idempotency_key,
    )
    payload = result["payload"]
    return {
        "request_idempotency_key": request.idempotency_key,
        "response_packet": packet_summary(result["packet"]),
        "response_payload_keys": sorted(payload.keys()),
        "state": payload.get("state"),
        "failure_reason": payload.get("failure_reason"),
        "field_names": sorted((payload.get("fields") or {}).keys()),
    }


def gate_error(exc: BaseException) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)[:400]}
    fc = getattr(exc, "failure_class", None)
    if fc:
        out["failure_class"] = fc
    cause = exc.__cause__
    if cause is not None:
        out["cause_type"] = type(cause).__name__
        out["cause"] = str(cause)[:400]
        for attr in ("status_code", "status"):
            if getattr(cause, attr, None) is not None:
                out["http_status"] = getattr(cause, attr)
    for attr in ("status_code", "status"):
        if getattr(exc, attr, None) is not None:
            out["http_status"] = getattr(exc, attr)
    return out


# ───────────────────────────── phases ──────────────────────────────────────


def phase_configure() -> None:
    from odoo.addons.plasticos_gate.services.gate_config import (
        GateCapability,
        classify_gate_availability,
        gate_signing_configured,
    )

    apply_icp()
    mods = installed_modules()
    needed = {"plasticos_gate", "plasticos_enrichment", "plasticos_matching"}
    check(
        "O_INSTALL",
        "PASS" if needed <= set(mods) else "FAIL",
        installed=mods,
        missing=sorted(needed - set(mods)),
    )
    avail = classify_gate_availability(env, capability=GateCapability.ENRICHMENT)
    check(
        "O_CONFIG",
        "PASS" if gate_signing_configured(env) else "FAIL",
        signing_configured=gate_signing_configured(env),
        availability=avail.status,
        availability_reasons=list(avail.reasons),
        icp=dict(ICP_BASELINE),
        secrets_in_icp=False,
    )
    # Egress proof: Odoo can resolve Gate and nothing behind it.
    dns = {}
    for host in ("gate", "enrichment-engine", "graph", "neo4j", "eie-postgres"):
        try:
            dns[host] = socket.gethostbyname(host)
        except OSError as exc:
            dns[host] = f"UNRESOLVABLE ({exc.__class__.__name__})"
    isolated = all(v.startswith("UNRESOLVABLE") for k, v in dns.items() if k != "gate")
    check(
        "O_ISOLATION",
        "PASS" if isolated and not dns["gate"].startswith("UNRESOLVABLE") else "FAIL",
        dns=dns,
    )


def phase_transport() -> None:
    """EIE unchanged (staging, live provider, no provider egress)."""
    apply_icp()
    partner = new_partner("TRANSPORT")
    run = new_run(partner)
    try:
        raw = direct_converge(run)
        pkt = raw["response_packet"]
        # Gate relays the worker's response packet (source_node stays the
        # worker) and re-signs it with its own key; Odoo's SDK verifies that
        # signature against Gate's key only. Gate authority on the return leg
        # is therefore the signature, not the source_node string.
        ok = (
            pkt["packet_type"] == "response"
            and pkt["signing_key_id"] == "gate-e2e"
            and pkt["signature_present"]
            and pkt["idempotency_key"] == raw["request_idempotency_key"]
            and raw["state"] is not None
        )
        check("O_T1_ROUNDTRIP", "PASS" if ok else "FAIL", **raw)
        # EIE contract probe: with no provider reachable, is an empty result
        # reported as `completed`? (Odoo must not trust it; see O_T2.)
        empty_success = raw["state"] == "completed" and not raw["field_names"] and not raw["failure_reason"]
        check(
            "O_F1_EIE_EMPTY_RESULT_REPORTED_COMPLETED",
            "GAP" if empty_success else "ENFORCED",
            state=raw["state"],
            field_names=raw["field_names"],
            failure_reason=raw["failure_reason"],
        )
    except Exception as exc:
        check("O_T1_ROUNDTRIP", "FAIL", error=gate_error(exc), tb=traceback.format_exc()[-1500:])
    # The operator path on a fresh run: Odoo must fail CLOSED on a non-completed
    # EIE answer — degraded, never injected, never a local fallback.
    run2 = new_run(new_partner("TRANSPORT-UI"))
    res = execute_run(run2)
    closed = res["state"] in {"degraded", "failed", "retryable"}
    check("O_T2_OPERATOR_FAIL_CLOSED", "PASS" if closed else "FAIL", **res)


def phase_business() -> None:
    """EIE on the deterministic source: the completed business path."""
    apply_icp()
    partner = new_partner("BUSINESS")
    before = {f: partner[f] or None for f in ("website", "city", "phone", "email")}
    run = new_run(partner)
    res = execute_run(run)
    check(
        "O_B1_CONVERGE_REVIEW",
        "PASS" if res["state"] == "review" and res["proposed_partner_fields"] and res["gate_packet_id"] else "FAIL",
        **res,
    )
    partner.invalidate_recordset()
    untouched = all((partner[f] or None) == v for f, v in before.items())
    check("O_B2_NO_WRITE_BEFORE_APPROVAL", "PASS" if untouched else "FAIL", partner_before=before)

    # Human approval: the real Inject button.
    injected: dict[str, Any] = {}
    try:
        fresh = env["plasticos.enrichment.run"].browse(run.id)
        fresh.action_inject()
        env.cr.commit()
        fresh.invalidate_recordset()
        partner.invalidate_recordset()
        injected = {
            "state": fresh.state,
            "fields_written": fresh.fields_written,
            "partner_after": {f: bool(partner[f]) for f in ("website", "city", "phone", "email")},
        }
        injected["provenance_rows"] = env["plasticos.enrichment.provenance"].search_count(
            [("partner_id", "=", partner.id)]
        )
        check(
            "O_B3_APPROVE_INJECT",
            "PASS" if fresh.state == "injected" and (fresh.fields_written or 0) > 0 else "FAIL",
            **injected,
        )
    except Exception as exc:
        env.cr.rollback()
        check("O_B3_APPROVE_INJECT", "FAIL", error=gate_error(exc), tb=traceback.format_exc()[-1500:])

    # Duplicate delivery of one logical operation (same idempotency key):
    # Gate's idempotency cache must answer, EIE must not run twice.
    dup_run = new_run(new_partner("IDEMPOTENCY"))
    try:
        first = direct_converge(dup_run)
        second = direct_converge(dup_run)
        same = first["response_packet"]["packet_id"] == second["response_packet"]["packet_id"]
        check(
            "O_B4_IDEMPOTENT_REPLAY",
            "PASS" if same and first["state"] == "completed" else "FAIL",
            first=first["response_packet"],
            second=second["response_packet"],
            state=first["state"],
        )
    except Exception as exc:
        check("O_B4_IDEMPOTENT_REPLAY", "FAIL", error=gate_error(exc))

    phase_business_schema_probe()


def phase_business_schema_probe() -> None:
    """PROPOSAL probe (not gated): Odoo's request + a target `schema`.

    EIE fills only the fields named in EnrichRequest.schema; Odoo's
    build_converge_request sends none, so EIE has nothing to target and echoes
    the input. This probe sends the SAME Odoo request with `schema` derived from
    Odoo's own writeback allowlist, then lands the answer through the real
    review -> Inject path — i.e. it proves the proposed one-line Odoo fix
    end to end without changing Odoo's code.
    """
    from odoo.addons.plasticos_gate.services.gate_allowlists import PARTNER_WRITEBACK_FIELD_ALLOWLIST
    from odoo.addons.plasticos_gate.services.gate_builders import build_converge_request
    from odoo.addons.plasticos_gate.services.gate_client import send_converge_action
    from odoo.addons.plasticos_gate.services.gate_mappers import (
        extract_audit_metadata,
        map_converge_response,
        partner_writeback_from_converge,
    )

    partner = new_partner("SCHEMA-PROBE")
    run = new_run(partner)
    try:
        request = build_converge_request(env, run)
        payload = {
            **request.to_dict(),
            "schema": dict.fromkeys(sorted(PARTNER_WRITEBACK_FIELD_ALLOWLIST), "string"),
        }
        key = f"{request.idempotency_key}:schema-probe"
        payload["idempotency_key"] = key
        result = send_converge_action(env, payload=payload, idempotency_key=key)
        resp = map_converge_response(result["payload"])
        proposed = partner_writeback_from_converge(resp)
        audit = extract_audit_metadata(result["packet"])
        run.write(
            {
                "engine_used": "gate",
                "state": "review",
                "gate_packet_id": audit.get("gate_packet_id"),
                "gate_correlation_id": audit.get("gate_correlation_id"),
                "gate_proposal": {"final_fields": resp.final_fields, "proposed_partner_fields": proposed},
            }
        )
        env.cr.commit()
        name_before = partner.name
        run.action_inject()
        env.cr.commit()
        run.invalidate_recordset()
        partner.invalidate_recordset()
        filled = sorted(f for f in ("website", "city", "zip", "street", "email", "phone") if partner[f])
        # ADR-012: every automated partner write carries a provenance row.
        provenance_rows = env["plasticos.enrichment.provenance"].search_count([("partner_id", "=", partner.id)])
        check(
            "O_P1_SCHEMA_FIX_REACHES_WRITEBACK",
            "PASS"
            if run.state == "injected"
            and filled
            and partner.name == name_before
            and provenance_rows == (run.fields_written or 0)
            else "FAIL",
            provenance_rows=provenance_rows,
            eie_state=resp.status,
            proposed_fields=sorted(proposed),
            run_state=run.state,
            fields_written=run.fields_written,
            partner_fields_filled=filled,
            existing_name_preserved=partner.name == name_before,
        )
    except Exception as exc:
        env.cr.rollback()
        check("O_P1_SCHEMA_FIX_REACHES_WRITEBACK", "FAIL", error=gate_error(exc), tb=traceback.format_exc()[-1500:])


def phase_match() -> None:
    """Odoo -> Gate -> CEG match, through the matching adapter Odoo uses.

    The request is Odoo's own MatchRequest contract (gate_contracts.py), so the
    match_direction on the wire is whatever Odoo sends in production. A second
    probe differs ONLY in match_direction, set to the direction CEG's plasticos
    spec declares, so a contract mismatch is diagnosed rather than guessed.
    """
    from odoo.addons.plasticos_gate.services.gate_client import send_match_action
    from odoo.addons.plasticos_gate.services.gate_contracts import MatchRequest

    apply_icp()
    query = {
        "polymer_type": "HDPE",
        "form": "regrind",
        "color": "natural",
        "quantity_per_load_lbs": 40000,
        "contamination_pct": 2.0,
        "lat": 41.88,
        "lon": -87.63,
        "source_type": "post_industrial",
        "mode": "strict",
    }
    odoo_request = MatchRequest(query=query, top_n=5)
    probes = {
        "O_M1_MATCH_ODOO_CONTRACT": odoo_request.to_dict(),
        "O_M2_MATCH_CEG_SPEC_DIRECTION": {
            **odoo_request.to_dict(),
            "match_direction": "supply_opportunity_to_buyer_facility",
        },
    }
    for name, payload in probes.items():
        try:
            result = send_match_action(
                env,
                payload=payload,
                idempotency_key=f"odoo:matching:e2e:{secrets.token_hex(4)}",
            )
            body = result["payload"]
            pkt = packet_summary(result["packet"])
            ok = "candidates" in body and pkt["packet_type"] == "response" and pkt["signing_key_id"] == "gate-e2e"
            check(
                name,
                "PASS" if ok else "FAIL",
                match_direction=payload["match_direction"],
                response_packet=pkt,
                payload_keys=sorted(body.keys()),
                candidate_count=len(body.get("candidates") or []),
                ceg_status=body.get("status"),
                error=body.get("error") or body.get("detail"),
                message=body.get("message"),
            )
        except Exception as exc:
            check(
                name,
                "FAIL",
                match_direction=payload["match_direction"],
                error=gate_error(exc),
                tb=traceback.format_exc()[-1500:],
            )


def _sdk_call(
    *,
    local_node: str,
    key: str | None,
    key_id: str | None,
    action: str,
    payload: dict[str, Any],
    tenant: str = TENANT,
    gate_url: str = GATE_URL,
) -> dict[str, Any]:
    from constellation_node_sdk import GateClient, GateClientConfig

    verifying = json.loads(os.environ["PLASTICOS_GATE_VERIFYING_KEYS_JSON"])
    cfg_kwargs: dict[str, Any] = {
        "gate_url": gate_url,
        "local_node": local_node,
        "timeout_seconds": 15,
        "allowed_gate_destination": "gate",
        "verify_response_signatures": True,
        "verifying_keys": verifying,
    }
    if key:
        cfg_kwargs.update(signing_key=key, signing_key_id=key_id, signing_algorithm="hmac-sha256")
    client = GateClient(GateClientConfig(**cfg_kwargs))

    async def go() -> Any:
        return await client.execute(
            action=action,
            payload=payload,
            tenant={"actor": tenant, "on_behalf_of": tenant, "org_id": tenant, "originator": local_node},
            timeout_ms=15000,
        )

    try:
        pkt = asyncio.run(go())
        return {"accepted": True, "response_packet": packet_summary(pkt), "payload_keys": sorted(pkt.payload.keys())}
    except Exception as exc:
        return {"accepted": False, "error": gate_error(exc)}


def phase_adversarial() -> None:
    from odoo.addons.plasticos_gate.services.gate_builders import build_converge_request

    odoo_key = os.environ["PLASTICOS_GATE_SIGNING_KEY"]
    run = new_run(new_partner("ADVERSARIAL"))
    converge_payload = build_converge_request(env, run).to_dict()

    # A1 — Odoo with signing removed (the addon's own path, key-id param cleared).
    apply_icp({"plasticos.gate.signing_key_id": ""})
    saved = os.environ.pop("PLASTICOS_GATE_SIGNING_KEY")
    try:
        r = execute_run(new_run(new_partner("UNSIGNED")))
        rejected = r["state"] in {"failed", "degraded", "retryable"} and r["gate_packet_id"] is None
        check("O_N1_UNSIGNED_REJECTED", "PASS" if rejected else "FAIL", **r)
    finally:
        os.environ["PLASTICOS_GATE_SIGNING_KEY"] = saved
        apply_icp()

    # A2 — a key id Gate has never been given (any SDK install can do this).
    r = _sdk_call(
        local_node="odoo",
        key=secrets.token_hex(32),
        key_id="rogue-consumer",
        action="converge",
        payload=converge_payload,
    )
    check("O_N2_UNKNOWN_CONSUMER_REJECTED", "PASS" if not r["accepted"] else "FAIL", **r)

    # A3 — Odoo's key id with the wrong secret.
    r = _sdk_call(
        local_node="odoo", key=secrets.token_hex(32), key_id="odoo-e2e", action="converge", payload=converge_payload
    )
    check("O_N3_FORGED_SIGNATURE_REJECTED", "PASS" if not r["accepted"] else "FAIL", **r)

    # A4 — a consumer tries to register itself as the converge owner.
    reg_body = json.dumps(
        {"odoo": {"internal_url": "http://odoo:8069", "supported_actions": ["converge"], "metadata": {"owner": "eie"}}}
    ).encode()
    req = urllib.request.Request(  # noqa: S310 — fixed http://gate literal
        f"{GATE_URL}/v1/admin/register?overwrite=true",
        data=reg_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    check("O_N4_CONSUMER_SELF_REGISTRATION_REJECTED", "PASS" if code in {401, 403} else "FAIL", http_status=code)

    # A5 — direct worker bypass: point the SDK at EIE's address.
    r = _sdk_call(
        local_node="odoo",
        key=odoo_key,
        key_id="odoo-e2e",
        action="converge",
        payload=converge_payload,
        gate_url="http://enrichment-engine:8000",
    )
    check("O_N5_DIRECT_WORKER_BYPASS_IMPOSSIBLE", "PASS" if not r["accepted"] else "FAIL", **r)

    # ── Authorization-gap probes. These are FINDINGs, not failures: they record
    # what a VALID consumer key can do today. "GAP" means Gate accepted it.
    # G1 — the admitted consumer invokes a CEG write action it has no business calling.
    r = _sdk_call(
        local_node="odoo",
        key=odoo_key,
        key_id="odoo-e2e",
        action="sync",
        payload={
            "entity_type": "facilities",
            "batch": [{"facility_id": "E2E-ODOO-AUTHZ-PROBE", "e2e_marker": "odoo-authz-probe"}],
        },
    )
    check("O_G1_CONSUMER_ACTION_SCOPE", "GAP" if r["accepted"] else "ENFORCED", **r)

    # G2 — the admitted consumer's key used under another node's name.
    r = _sdk_call(
        local_node="enrichment-engine", key=odoo_key, key_id="odoo-e2e", action="converge", payload=converge_payload
    )
    check("O_G2_KEY_TO_IDENTITY_BINDING", "GAP" if r["accepted"] else "ENFORCED", **r)

    # G3 — the admitted consumer's key used for another tenant.
    r = _sdk_call(
        local_node="odoo",
        key=odoo_key,
        key_id="odoo-e2e",
        action="converge",
        payload=converge_payload,
        tenant="some-other-tenant",
    )
    check("O_G3_KEY_TO_TENANT_BINDING", "GAP" if r["accepted"] else "ENFORCED", **r)

    # G4 — unauthenticated registry disclosure of worker internal URLs.
    try:
        with urllib.request.urlopen(f"{GATE_URL}/v1/registry", timeout=10) as resp:  # noqa: S310
            body = json.loads(resp.read())
        urls = sorted(v.get("internal_url", "") for v in body.values())
        check("O_G4_REGISTRY_DISCLOSURE", "GAP" if urls else "ENFORCED", internal_urls=urls)
    except urllib.error.HTTPError as exc:
        check("O_G4_REGISTRY_DISCLOSURE", "ENFORCED", http_status=exc.code)


def phase_consumer_sdk() -> None:
    """L9-PARTICIPATION-01, consumer half: Odoo's own client config + Gate_SDK only.

    The config comes from the addon's production builder (build_gate_client_config),
    so these checks prove what Odoo gets from the SDK without Odoo-side glue:
    admission confirmed up front, and Gate's refusals typed and classified.
    """
    from constellation_node_sdk import GateAuthorizationError, GateClient, GateHTTPError
    from odoo.addons.plasticos_gate.services.gate_config import build_gate_client_config

    apply_icp()
    config = build_gate_client_config(env)

    def run(coro: Any) -> Any:
        return asyncio.run(coro)

    # C1 — the admitted consumer learns its admission and grant before any business call.
    try:
        receipt = run(GateClient(config).activate(required_actions=("converge", "match")))
        ok = (
            receipt.key_id == "odoo-e2e"
            and receipt.scope == "restricted"
            and set(receipt.granted_actions) == {"converge", "match"}
            and receipt.admitted
        )
        check(
            "C_ADMISSION_RECEIPT",
            "PASS" if ok else "FAIL",
            key_id=receipt.key_id,
            scope=receipt.scope,
            scoped_actions=receipt.scoped_actions,
            granted_actions=receipt.granted_actions,
            routable_actions=receipt.routable_actions,
        )
    except Exception as exc:
        check("C_ADMISSION_RECEIPT", "FAIL", error=gate_error(exc))

    # C2 — asking for an action Gate did not grant is a typed refusal, not a surprise later.
    try:
        run(GateClient(config).activate(required_actions=("converge", "sync")))
        check("C_REQUIRED_ACTION_MISSING", "FAIL", error="activate() accepted an ungranted action")
    except GateAuthorizationError as exc:
        ok = exc.code == "action_not_permitted" and exc.retryable is False
        check("C_REQUIRED_ACTION_MISSING", "PASS" if ok else "FAIL", code=exc.code, retryable=exc.retryable)
    except Exception as exc:
        check("C_REQUIRED_ACTION_MISSING", "FAIL", error=gate_error(exc))

    # C3 — a key Gate does not know is refused at admission (and is not an authorization error).
    stranger = config.model_copy(update={"signing_key": secrets.token_hex(32), "signing_key_id": "stranger-e2e"})
    try:
        run(GateClient(stranger).activate())
        check("C_ADMISSION_UNKNOWN_KEY_REJECTED", "FAIL", error="unknown key was admitted")
    except GateHTTPError as exc:
        ok = exc.status_code == 400 and not isinstance(exc, GateAuthorizationError)
        check(
            "C_ADMISSION_UNKNOWN_KEY_REJECTED",
            "PASS" if ok else "FAIL",
            status_code=exc.status_code,
            code=exc.code,
            retryable=exc.retryable,
        )
    except Exception as exc:
        check("C_ADMISSION_UNKNOWN_KEY_REJECTED", "FAIL", error=gate_error(exc))

    # C4 — an out-of-scope business call is typed: no consumer classifier needed.
    try:
        run(
            GateClient(config).execute(
                action="sync",
                payload={"entity_type": "facilities", "batch": []},
                tenant={"actor": TENANT, "on_behalf_of": TENANT, "org_id": TENANT, "originator": "odoo"},
                timeout_ms=15000,
            )
        )
        check("C_TYPED_403", "FAIL", error="out-of-scope sync was accepted")
    except GateAuthorizationError as exc:
        ok = exc.status_code == 403 and exc.code == "action_not_permitted" and not exc.retryable
        check("C_TYPED_403", "PASS" if ok else "FAIL", status_code=exc.status_code, code=exc.code)
    except Exception as exc:
        check("C_TYPED_403", "FAIL", error=gate_error(exc))


PHASES = {
    "configure": phase_configure,
    "transport": phase_transport,
    "business": phase_business,
    "match": phase_match,
    "adversarial": phase_adversarial,
    "consumer_sdk": phase_consumer_sdk,
}

try:
    PHASES[PHASE]()
except Exception:
    RESULT["driver_error"] = traceback.format_exc()[-3000:]
sys.stdout.write("L9E2E-RESULT-BEGIN\n" + json.dumps(RESULT, indent=1, default=str) + "\nL9E2E-RESULT-END\n")
