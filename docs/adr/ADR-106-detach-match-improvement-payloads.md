# ADR-106: Detach Match and Improvement Payloads from Alternate Packet

**Status:** Accepted  
**Task:** TASK-040  
**Date:** 2026-08-02

## Decision

Match request/response and improvement proposal are payload-only contracts carried
inside Gate_SDK `TransportPacket`. They do not extend the alternate
`packet.schema.yaml`.

## Acceptance

- No transport fields on payload roots.
- Improvement proposals cannot mutate production (`direct_mutation: false`,
  `review_required: true`).

## Artifacts

`contracts/payloads/*`, `engine.models.payloads`, unit tests.
