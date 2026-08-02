# ADR-109: CEG payload contract compiler validator

**Status:** Accepted
**Task:** TASK-034
**Date:** 2026-08-02

## Decision

Add `tools/payload_contract_compiler.py` as the CEG contract compiler validator.

It:

- Draft-2020-12 checks live `contracts/payloads/*.schema.yaml`
- Validates positive/negative fixtures against native `engine.models.payloads` models
- Confirms `DomainPackLoader.load_domain("plasticos")` remains the sole domain authority
- Emits a deterministic digest report under `artifacts/`

It does **not** create a parallel domain cartridge, alternate transport envelope, or tensor runtime (pack ADR-107 / GATE-018 intent).

## Consequences

- `make agent-check-unit` / scoped validation runs the compiler validator
- TASK-060 may compare CEG/EIE compiler digests for parity
- Residual: relative `./common.schema.yaml` `$ref` in match/improvement schemas remain owner-local; portable URN rewrite belongs to cross-repo registry tooling (TASK-043)
