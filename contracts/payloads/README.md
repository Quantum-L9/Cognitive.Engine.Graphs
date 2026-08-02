# PlasticOS payload contracts (CEG)

Payload-only schemas for Gate_SDK `TransportPacket`. No alternate-envelope inheritance.
No transport fields. Improvement proposals lock `direct_mutation: false`.

Schemas:
- match request/response, improvement proposal (TASK-040)
- sync-projection (TASK-028)
- canonical-projection + outcome-feedback (TASK-061)

Native models: `engine.models.payloads`. See ADR-106 / ADR-108.
