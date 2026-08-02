# ADR-106: Detach Match and Improvement Payloads from Alternate Packet

**Status:** Accepted
**Task:** TASK-040
**Date:** 2026-08-02

## Decision

Match request/response and improvement proposal contracts are **payload-only**
schemas carried inside Gate_SDK `TransportPacket`. They do not extend
PacketEnvelope / alternate `packet.schema.yaml`.

Artifacts:

- `contracts/payloads/match-request.schema.yaml`
- `contracts/payloads/match-response.schema.yaml`
- `contracts/payloads/improvement-proposal.schema.yaml`
- native models in `engine.models.payloads`

## Why

PACK-029 `match-execution-packet` and `improvement-packet` inherited an
alternate envelope that duplicated transport identity, tenant context,
correlation, and fingerprints already owned by Gate_SDK (F-029-05 / F-029-06).

Detaching retains useful payload concepts while restoring a single wire format.

## Acceptance

1. **No transport fields** on payload roots (`additionalProperties: false` +
   explicit rejection of forbidden duplicates).
2. **Direct mutation impossible** for improvements: `direct_mutation` is
   constantly `false` and `review_required` is constantly `true`.

## Non-goals

- Removing historical PacketEnvelope modules from the tree in this task.
- Publishing schemas outside draft-unpublished status.
- Implementing sync-projection / outcome-feedback (later tasks).
