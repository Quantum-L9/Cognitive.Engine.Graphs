# ADR-105: Second PlasticOS Match Direction

**Status:** Accepted
**Task:** TASK-017
**Date:** 2026-08-02

## Decision

Second executable direction:

`buyer_demand_to_supply_opportunity`

Candidate model: `SupplyOpportunity` (active supply)
Query entity: `BuyerDemand`

Rejected: treating `Facility` capability as the reverse-direction candidate.

## Why

UNK-002 / acceptance: active supply must not be conflated with Facility capability
(`capacity_tons_month`, `PROCESSES`). Facility remains the capability node for
`supply_opportunity_to_buyer_facility`. Reverse matching uses distinct active-supply
nodes and gates (`available_tons`, `active`).

## Authority

Recorded in `ledger/artifacts/wave3/TASK-017-DIRECTION-AUTHORITY.json` under L4
operator authorization to resolve blockers.
