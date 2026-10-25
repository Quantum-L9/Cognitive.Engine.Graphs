# ADR-112: Domain-neutral CEG core

**Status:** Accepted
**Task:** CEG domain-neutrality Stage 2 (R01–R11)
**Date:** 2026-10-11
**Supersedes in part:** ADR-103 (PlasticOS as the loader's domain authority), ADR-109 (PlasticOS
as the compiler's authority). Both ADRs stay in force for what they still decide; see below.

## Decision

Cognitive.Engine.Graphs is a domain-agnostic graph execution engine. It does not intrinsically
understand PlasticOS, IdeaOS, or any other business domain. Domain-specific semantics are
supplied by the authorized domain owner through the existing validated contracts:
`DomainSpec` loaded by `DomainPackLoader`, and payload contracts admitted against a
`DomainContract` derived from that spec.

Concretely:

1. **Generic pack admission (R07).** `DomainPackLoader` admits a pack through one fail-closed
   policy (`engine.config.loader.withholding_reason`): the pack validates against `DomainSpec`,
   its gates compile to executable Cypher (`admission_defects`), and it does not declare
   `domain.activation: dormant`. Two generic operator overrides exist, both default off:
   `UNVALIDATED_DOMAIN_PACKS_ENABLED` and `DORMANT_DOMAIN_PACKS_ENABLED`. The per-domain flag
   allowlist (`idea_portfolio_enabled` and seven named CEG-009 packs) is gone; the same packs
   are withheld as before, for reasons the policy can state.
2. **GDS vocabulary is the job spec's (R01).** Every label, relationship type and property a
   GDS algorithm touches comes from `projection`, `sourceedge`, `writeedge`, `writeto` or
   `writeproperties`. A job without the vocabulary it needs fails explicitly; there is no
   built-in fallback label, interaction edge or equipment table.
3. **No business inference in the engine (R02).** The inference registry carries domain-neutral
   rules only. The admin `emit_inference_feedback` pathway executes exactly the rules the domain
   authorizes under `plugins.inference_rules`; an absent declaration authorizes none.
4. **Domain-neutral payload contracts (R03/R04).** `engine.models.payloads` pins no domain,
   direction, entity type, outcome type or source system. Each payload root carries a `domain`
   and is admitted only with a `DomainContract` in the validation context; validation without
   one fails closed. The compiler validator admits fixtures against the contract of the domain
   the fixtures name, loaded from a configurable domains root, and succeeds for any admitted
   domain with no PlasticOS directory present.
5. **Intake recipes are inputs (R06).** Vertical keyword tables, derivation chains and
   field-name ambiguity patterns are caller-supplied; the engine ships none. `intake_*` actions
   remain unexposed as Gate actions.
6. **Outcome vocabulary is the domain's (R09).** Handlers read and write the outcome node label
   from `feedbackloop.outcome_node_label`; `preference_attention` scoring requires the domain
   to declare `success_value`.
7. **No industry-specific global defaults (R10).** Settings that encoded one vertical's
   vocabulary with no caller were removed; the semantic registry derives from the domain spec;
   feature-catalog owners and source systems are declared identifiers, not closed engine
   enumerations; offline replay accepts registered producer schemas (the Odoo exporter's
   `l9.odoo.outcome_replay_input.v1`, verified against IB-Odoo_19 ADR-126, is one entry).
8. **Packs are an explicit selection (R08).** The core image takes its pack directory from the
   `DOMAIN_PACKS_SRC` build argument (default: this repository's example packs) and the engine
   boots with an empty pack directory. `Dockerfile.prod` and the compose files are unchanged;
   production packaging is a deployment decision outside this ADR.

## What this does not decide

- **IdeaOS portfolio hydration (R05) is BLOCKED, not relocated.** `engine/sync/idea_portfolio.py`
  and `tools/hydrate_idea_portfolio.py` carry IdeaOS projection semantics. IdeaOS
  (`.l9/architecture.yaml`, `.l9/ownership.yaml`) declares no graph-projection, revision-chain
  or tombstone contract, and Quantum-L9/.github assigns no owner for these semantics. The code
  stays in CEG in a safe, non-activated state (the pack declares itself dormant; the CLI refuses
  without the generic operator override) until an authorized owner exists. Every behaviour it
  implements (revision chain, parent conflict detection, canonical digests, idempotent replay,
  uniqueness precondition, transactional atomicity, tombstones, provenance) is preserved.
- **The former plastics inference rules** (facility tier from processing capacity, material grade
  from melt-flow index, contamination tolerance) were removed from CEG, not moved. No authorized
  mechanism supplies executable rules to CEG from a domain pack yet; the PlasticOS owner decides
  whether and how to resupply them. Their bodies remain in git history at `7925c79`.
- **Payload contract identity.** The payload schemas keep their `urn:l9:plasticos:...` `$id`
  values and `1.0.0-draft` version. The change is strictly widening for every valid prior
  payload and the contracts are `draft-unpublished` with no external consumer; renaming the
  URN namespace or cutting a new draft version before publication is the contract owner's call.
- **`RESULTED_IN` as the outcome edge type** is written by `handle_outcomes` in lines PR #317
  is concurrently changing (causal rejection enforcement). CEG's own system graph schema
  (`docs/contracts/data/graph-schema.yaml`) documents it as system vocabulary, and the domain
  may override it through `feedbackloop.outcome_edge_type`; honouring that override in the
  write path is deferred until #317 lands, to avoid rewriting its hunk.

## Consequences

- An operator unfamiliar with PlasticOS can install the core, point `DOMAINS_ROOT` at a valid
  authorized pack, and run match/sync/outcomes/GDS without editing engine source. The
  acceptance proof is `tests/unit/test_domain_neutrality_acceptance.py` with two unrelated
  synthetic packs under `tests/fixtures/domains/`.
- PlasticOS remains an example: `domains/plasticos/spec.yaml` is still the only executable
  PlasticOS source (ADR-103) and the repository's payload fixtures are PlasticOS-shaped.
- Stage 3 (a CI guard that fails on reintroduced product vocabulary) and Stage 4 (the
  container-backed integration campaign) are not part of this decision.

## Recovery

Revert the Stage 2 commits. No data migration is involved: graph labels written before and
after are identical for domains whose `feedbackloop.outcome_node_label` is the default.
