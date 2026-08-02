# ADR-107: Harvest CEG Gates, Features, Explanations, Outcomes

**Status:** Accepted
**Task:** TASK-018
**Date:** 2026-08-02

## Decision

Harvest PACK-026 domain intelligence into the live PlasticOS `DomainSpec`:

- hard gate addition mapped to existing `GateType` (`active_supply_polymer`)
- `feature_catalog` linking scoring dimensions and evidence owners
- `explanations` taxonomy with deterministic contribution ordering
- `outcome_signals` aligned to outcome-feedback contract
- `feedbackloop` enabled for calibration

Reject parallel tensor-cartridge runtime (`engine.tensor`).

## Acceptance

- No tensor runtime path.
- Explanation/action outputs are deterministic from DomainSpec catalogs.
