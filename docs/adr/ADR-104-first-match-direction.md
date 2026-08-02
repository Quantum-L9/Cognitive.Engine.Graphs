# ADR-104: First PlasticOS Match Direction

**Status:** Accepted
**Task:** TASK-016
**Date:** 2026-08-02

## Decision

The first executable PlasticOS match direction is:

`supply_opportunity_to_buyer_facility`

This replaces the misnamed `intake_to_buyer` label in `domains/plasticos/spec.yaml`.
Semantics remain buyer-demand / material-intake seeking supplier facilities.

## Non-goals

- Second reverse direction (TASK-017)
- Ranking authority changes outside CEG DomainSpec

## Consequences

Clients and fixtures must send `match_direction=supply_opportunity_to_buyer_facility`.
`intake_to_buyer` is non-authoritative after this change.
