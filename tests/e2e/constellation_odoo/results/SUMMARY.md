# Published evidence — Odoo consumer rail + Gate rail

Each bundle is one run's complete, redacted evidence directory, packed as a
deterministic `.tar.gz` (sorted entries, fixed mtime and owner) so this PR stays within
CEG's reviewable-size policy. Secret scan over every env value: **PASS** for every bundle.
Unpack with `tar -xzf <bundle>.tar.gz`. Logs are `*.log.txt`.

## `odoo-rail-20260927T022821Z-sdk-participation.tar.gz`

- **What:** the Gate_SDK participation closure (L9-PARTICIPATION-01) in SDK participation mode.
- **Images:**
  - Gate_SDK `eaac6a8` in all five images.
  - EIE and CEG carry their SDK adoption diffs.
  - The zero-code `sdk-minimal-node`.
- **sha256:** `9d160b3697ac40594053fe27a7901f4786ec89249582fe798fbdfce54002f45e`

```text
MANDATORY  O_INSTALL                                 PASS
MANDATORY  O_CONFIG                                  PASS
MANDATORY  O_ISOLATION                               PASS
MANDATORY  O_T1_ROUNDTRIP                            PASS
MANDATORY  O_T2_OPERATOR_FAIL_CLOSED                 PASS
MANDATORY  O_B1_CONVERGE_REVIEW                      PASS
MANDATORY  O_B2_NO_WRITE_BEFORE_APPROVAL             PASS
MANDATORY  O_B3_APPROVE_INJECT                       PASS
MANDATORY  O_B4_IDEMPOTENT_REPLAY                    PASS
MANDATORY  O_N1_UNSIGNED_REJECTED                    PASS
MANDATORY  O_N2_UNKNOWN_CONSUMER_REJECTED            PASS
MANDATORY  O_N3_FORGED_SIGNATURE_REJECTED            PASS
MANDATORY  O_N4_CONSUMER_SELF_REGISTRATION_REJECTED  PASS
MANDATORY  O_N5_DIRECT_WORKER_BYPASS_IMPOSSIBLE      PASS
MANDATORY  O_G1_CONSUMER_ACTION_SCOPE                ENFORCED
MANDATORY  GATE_RESTART_RECOVERY                     PASS
MANDATORY  DOCKER_ISOLATION                          PASS
MANDATORY  EVIDENCE_no_secrets                       PASS
MANDATORY  REG_live_registration                     PASS
MANDATORY  C_ADMISSION_RECEIPT                       PASS
MANDATORY  C_REQUIRED_ACTION_MISSING                 PASS
MANDATORY  C_ADMISSION_UNKNOWN_KEY_REJECTED          PASS
MANDATORY  C_TYPED_403                               PASS
MANDATORY  P_SDK_NODE_ACTIVE                         PASS
MANDATORY  P_SDK_NODE_ROUTABLE                       PASS
MANDATORY  P_SDK_NODE_RECOVERY                       PASS
MATCH      O_M1_MATCH_ODOO_CONTRACT                  PASS
MATCH      O_M2_MATCH_CEG_SPEC_DIRECTION             PASS
PROPOSAL   O_P1_SCHEMA_FIX_REACHES_WRITEBACK         PASS
FINDING    O_F1_EIE_EMPTY_RESULT_REPORTED_COMPLETED  GAP
FINDING    O_G2_KEY_TO_IDENTITY_BINDING              GAP
FINDING    O_G3_KEY_TO_TENANT_BINDING                GAP
FINDING    O_G4_REGISTRY_DISCLOSURE                  GAP
MANDATORY  PROVENANCE_images_and_sdk                 PASS
SDK commits: {"gate": "eaac6a84d49c", "eie": "eaac6a84d49c", "ceg": "eaac6a84d49c", "odoo": "eaac6a84d49c", "sdk-node": "eaac6a84d49c"}
SDK aligned: True
DEVIATION  ceg: Gate_SDK overlay eaac6a84d49c
DEVIATION  ceg: SDK adoption ceg-sdk-adoption.diff@sha256:fa3cdeba0edac9f3
DEVIATION  eie: Gate_SDK overlay eaac6a84d49c
DEVIATION  eie: SDK adoption eie-sdk-adoption.diff@sha256:728d340c770d44e0
DEVIATION  gate: Gate_SDK overlay eaac6a84d49c
DEVIATION  odoo: Gate_SDK overlay eaac6a84d49c
DEVIATION  eie business phase: L9_ENVIRONMENT=test L9_ENRICHMENT_PROVIDER=deterministic
VERDICT: PASS
```

## `odoo-rail-20260926T191817Z-after-fixes.tar.gz`

- AFTER fixes 1-5 — Odoo rail, pristine images, no accommodation · sha256 `bef89ff8fcc6fc5a6f332b121cb17972adadc625ec23d81dadbaf72ace60b92a`
- Re-verified with the mandatory `PROVENANCE_images_and_sdk` row added after review: **PASS** (every image revision equals its recorded source head; SDK `e9f829f98211` in all four images)

```text
MANDATORY  O_INSTALL                                 PASS
MANDATORY  O_CONFIG                                  PASS
MANDATORY  O_ISOLATION                               PASS
MANDATORY  O_T1_ROUNDTRIP                            PASS
MANDATORY  O_T2_OPERATOR_FAIL_CLOSED                 PASS
MANDATORY  O_B1_CONVERGE_REVIEW                      PASS
MANDATORY  O_B2_NO_WRITE_BEFORE_APPROVAL             PASS
MANDATORY  O_B3_APPROVE_INJECT                       PASS
MANDATORY  O_B4_IDEMPOTENT_REPLAY                    PASS
MANDATORY  O_N1_UNSIGNED_REJECTED                    PASS
MANDATORY  O_N2_UNKNOWN_CONSUMER_REJECTED            PASS
MANDATORY  O_N3_FORGED_SIGNATURE_REJECTED            PASS
MANDATORY  O_N4_CONSUMER_SELF_REGISTRATION_REJECTED  PASS
MANDATORY  O_N5_DIRECT_WORKER_BYPASS_IMPOSSIBLE      PASS
MANDATORY  O_G1_CONSUMER_ACTION_SCOPE                ENFORCED
MANDATORY  GATE_RESTART_RECOVERY                     PASS
MANDATORY  DOCKER_ISOLATION                          PASS
MANDATORY  EVIDENCE_no_secrets                       PASS
MANDATORY  REG_live_registration                     PASS
MATCH      O_M1_MATCH_ODOO_CONTRACT                  PASS
MATCH      O_M2_MATCH_CEG_SPEC_DIRECTION             PASS
PROPOSAL   O_P1_SCHEMA_FIX_REACHES_WRITEBACK         PASS
FINDING    O_F1_EIE_EMPTY_RESULT_REPORTED_COMPLETED  GAP
FINDING    O_G2_KEY_TO_IDENTITY_BINDING              GAP
FINDING    O_G3_KEY_TO_TENANT_BINDING                GAP
FINDING    O_G4_REGISTRY_DISCLOSURE                  GAP
SDK commits: {"gate": "e9f829f98211", "eie": "e9f829f98211", "ceg": "e9f829f98211", "odoo": "e9f829f98211"}
SDK aligned: True
DEVIATION  eie business phase: L9_ENVIRONMENT=test L9_ENRICHMENT_PROVIDER=deterministic
VERDICT: PASS
```

## `gate-rail-20260926T192218Z-after-fixes.tar.gz`

- AFTER fixes 1-5 — Gate 3-node rail, pristine images, no accommodation · sha256 `3c4731f70881cc979d5fb2b2009baba41e5ee6a2d5e6d54c5338a8d0675953c8`

```text
{
 "verdict": "PASS",
 "results": {
  "P1_gate_to_eie": "PASS",
  "P2_gate_to_ceg_sync": "PASS",
  "P3_gate_to_ceg_match": "PASS",
  "P4_replay_same_packet": "PASS",
  "N1_unsigned_rejected": "PASS",
  "N2_bad_signature_rejected": "PASS",
  "N3_unknown_action_404": "PASS",
  "N4_destination_override_refused": "PASS",
  "N7_unknown_key_id_rejected": "PASS",
  "N5_worker_down_fails_closed": "PASS",
  "P7_recovery_after_restart": "PASS",
  "P6_eie_to_gate_to_ceg": "PASS",
  "P5_ceg_to_gate_to_eie": "PASS",
  "N6_peer_isolation": "PASS",
  "REG_live_registration": "PASS",
  "SDK_provenance_captured": "PASS",
  "PROVENANCE_sources_clean": "PASS",
  "PROVENANCE_images_bound": "PASS",
  "SDK_gate_matches_lock": "PASS",
  "PERSIST_neo4j_effect": "PASS",
  "EVIDENCE_no_secrets": "PASS"
 },
 "sdk_commits": {
  "gate": "e9f829f982110be13752da8f18c7a9692e8ed908",
  "eie": "e9f829f982110be13752da8f18c7a9692e8ed908",
  "ceg": "e9f829f982110be13752da8f18c7a9692e8ed908"
 },
 "sdk_commit_sources": {
  "gate": "container_dist_info (archive url)",
  "eie": "container_dist_info (vcs)",
  "ceg": "container_dist_info (vcs)"
 },
 "failed_checks": []
}
```

## `odoo-rail-20260926T184524Z.tar.gz`

- BEFORE fixes — Odoo rail (EIE accommodated) · sha256 `de106b1d5e52db81df322e06cd3ceb0af72a0fe4ffda0db6c2b7bbe70c024338`

```text
MANDATORY  O_INSTALL                                 PASS
MANDATORY  O_CONFIG                                  PASS
MANDATORY  O_ISOLATION                               PASS
MANDATORY  O_T1_ROUNDTRIP                            PASS
MANDATORY  O_T2_OPERATOR_FAIL_CLOSED                 PASS
MANDATORY  O_B1_CONVERGE_REVIEW                      PASS
MANDATORY  O_B2_NO_WRITE_BEFORE_APPROVAL             PASS
MANDATORY  O_B3_APPROVE_INJECT                       FAIL -> FAIL
MANDATORY  O_B4_IDEMPOTENT_REPLAY                    PASS
MANDATORY  O_N1_UNSIGNED_REJECTED                    PASS
MANDATORY  O_N2_UNKNOWN_CONSUMER_REJECTED            PASS
MANDATORY  O_N3_FORGED_SIGNATURE_REJECTED            PASS
MANDATORY  O_N4_CONSUMER_SELF_REGISTRATION_REJECTED  PASS
MANDATORY  O_N5_DIRECT_WORKER_BYPASS_IMPOSSIBLE      PASS
MANDATORY  DOCKER_ISOLATION                          PASS
MANDATORY  EVIDENCE_no_secrets                       PASS
MANDATORY  REG_live_registration                     PASS
MATCH      O_M1_MATCH_ODOO_CONTRACT                  FAIL
MATCH      O_M2_MATCH_CEG_SPEC_DIRECTION             PASS
PROPOSAL   O_P1_SCHEMA_FIX_REACHES_WRITEBACK         PASS
FINDING    O_F1_EIE_EMPTY_RESULT_REPORTED_COMPLETED  GAP
FINDING    O_G1_CONSUMER_ACTION_SCOPE                GAP
FINDING    O_G2_KEY_TO_IDENTITY_BINDING              GAP
FINDING    O_G3_KEY_TO_TENANT_BINDING                GAP
FINDING    O_G4_REGISTRY_DISCLOSURE                  GAP
SDK commits: {"gate": "e9f829f98211", "eie": "e9f829f98211", "ceg": "e9f829f98211", "odoo": "e9f829f98211"}
SDK aligned: True
DEVIATION  eie: image accommodation sqlalchemy[asyncio],alembic
DEVIATION  eie business phase: L9_ENVIRONMENT=test L9_ENRICHMENT_PROVIDER=deterministic
VERDICT: FAIL (1 mandatory)
```

## `gate-rail-20260926T181056Z-pristine-heads.tar.gz`

- BEFORE fixes — Gate 3-node rail, pristine heads (EIE does not boot) · sha256 `5dcc09b8447a6e615c2689ef22f798e66b2df7fba565c99a11f125888785ea78`

```text
{
 "verdict": "FAIL",
 "results": {
  "P1_gate_to_eie": "FAIL",
  "P2_gate_to_ceg_sync": "PASS",
  "P3_gate_to_ceg_match": "PASS",
  "P4_replay_same_packet": "PASS",
  "N1_unsigned_rejected": "PASS",
  "N2_bad_signature_rejected": "PASS",
  "N3_unknown_action_404": "PASS",
  "N4_destination_override_refused": "PASS",
  "N7_unknown_key_id_rejected": "PASS",
  "N5_worker_down_fails_closed": "PASS",
  "P7_recovery_after_restart": "PASS",
  "P6_eie_to_gate_to_ceg": "MISSING",
  "P5_ceg_to_gate_to_eie": "FAIL",
  "N6_peer_isolation": "FAIL",
  "REG_live_registration": "FAIL",
  "SDK_provenance_captured": "PASS",
  "PROVENANCE_sources_clean": "PASS",
  "PROVENANCE_images_bound": "PASS",
  "SDK_gate_matches_lock": "PASS",
  "PERSIST_neo4j_effect": "FAIL",
  "EVIDENCE_no_secrets": "PASS"
 },
 "sdk_commits": {
  "gate": "e9f829f982110be13752da8f18c7a9692e8ed908",
  "eie": "e9f829f982110be13752da8f18c7a9692e8ed908",
  "ceg": "e9f829f982110be13752da8f18c7a9692e8ed908"
 },
 "sdk_commit_sources": {
  "gate": "container_dist_info (archive url)",
  "eie": "container_dist_info (vcs)",
  "ceg": "container_dist_info (vcs)"
 },
 "failed_checks": [
  "P1_gate_to_eie",
  "P6_eie_to_gate_to_ceg",
  "P5_ceg_to_gate_to_eie",
  "N6_peer_isolation",
  "REG_live_registration",
  "PERSIST_neo4j_effect"
 ]
}
```
