# PlasticOS payload contracts (CEG)

These schemas define **action/business payloads only**. They are carried inside
Gate_SDK `TransportPacket` and must never substitute a packet envelope.

| Schema | Action / role | Owner |
|--------|---------------|-------|
| `match-request.schema.yaml` | `match` request | CEG |
| `match-response.schema.yaml` | `match` response | CEG |
| `improvement-proposal.schema.yaml` | review-only proposal | program governance (hosted here for detach) |
| `common.schema.yaml` | shared `$defs` | PlasticOS contracts |

## Laws

- No transport fields (`packet_uuid`, `tenant_uuid`, `correlation_id`, …).
- Improvement proposals set `direct_mutation: false` and `review_required: true`.
- Do not `$ref` alternate `packet.schema` / PacketEnvelope.

Native Pydantic models: `engine.models.payloads`.
