<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs]
tags: [parked, kge, hoprag, compound-e3d, operator-decision]
owner: engine-team
status: active
/L9_META -->

# Parked Subsystems

A **parked** subsystem is code that stays in the repository but that no
runtime path may construct, train or run. Parking is not deletion and not a
dormant feature waiting for a flag: it is an operator decision that the code is
out of service until a new decision says otherwise.

## Decision

| Field | Value |
|---|---|
| Decision id | `CEG-KGE-PARK` |
| Decided | 2026-11-14, by the operator (binding) |
| Scope | HopRAG and CompoundE3D, with the KGE beam search and ensemble pieces that exist only to serve them |
| Not in scope | KGE as a generic embedding-score consumer: the KGE scoring dimensions in `engine/scoring/assembler.py` and `KGE_ENABLED` are unchanged |

Why: a cross-check of the 2026-10-11 remediation audit against `main` found
the code shipped but unused or unsound — HopRAG had no caller and no flag
(SI-01); CompoundE3D trains with a simplified loop (SI-02); beam search needs a
CompoundE3D model and the Pareto and cross-dimensional ensembles have no live
consumer (SI-03, SI-21); and admin `trigger_kge` reported `kge_activated`
without checking anything (SI-04).

## What is parked

| Subsystem | Code | Entry point that refuses |
|---|---|---|
| HopRAG | `engine/hoprag/` (`config.py`, `indexer.py`) | `GraphIndexBuilder.__init__` |
| CompoundE3D | `engine/kge/compound_e3d.py` | `CompoundE3D.__init__`, `engine.kge.parked.construct_compound_e3d` |
| KGE beam search | `engine/kge/beam_search.py` | `BeamSearchEngine.__init__` |
| KGE Pareto ensemble | `engine/kge/pareto_ensemble.py` | `ParetoEnsembleController.__init__` |
| KGE cross-dimensional ensemble | `engine/kge/cross_dimensional_ensemble.py` | `CrossDimensionalEnsemble.__init__` |

Not parked: `engine/kge/ensemble.py` (`EnsembleController`) and
`engine/kge/transformations.py` — generic, harmless primitives with no
dependency on a parked model. `HopRAGConfig` can still be built; it runs
nothing. The HopRAG-tagged helpers under `engine/traversal/` and
`engine/scoring/` are outside this decision.

## How the parking works

- `engine/kge/parked.py` holds the decision: `ParkedSubsystemError` (carries
  the subsystem, decision id, date and this document), `require_unparked()`,
  `is_parked_model()` and `construct_compound_e3d()`, the one gateway a runtime
  caller may use. It imports no parked module at load time.
- Every entry point above calls `require_unparked()` first. A parked call is an
  explicit typed refusal, never a silent no-op and never a fake success.
- Importing any parked module is harmless; `engine/kge/__init__.py` resolves its
  exports lazily, so `import engine.kge` loads nothing parked.
- Admin `trigger_kge` refuses a parked model with `FeatureNotEnabled`
  (`detail="parked"`, 422, decision in `context`), refuses an unimplemented
  model with `ValidationError` instead of silently running CompoundE3D, and,
  when un-parked, reports `kge_activated` only after `SHOW INDEXES` returns the
  vector index. `kge_status` reports a `parked` block and per-domain
  `model_parked`.
- `KGESpec.model` keeps its `"CompoundE3D"` default so every existing pack
  validates unchanged; the parked model is refused at use, not at admission.
- `tests/architecture/test_parked_subsystems_guard.py` fails if
  `engine/handlers.py`, `engine/boot.py`, `chassis/`, `engine/scoring/` or
  `engine/gds/` imports parked code other than through `engine.kge.parked`.
- `tests/unit/test_kge_parked.py` asserts each refusal. The existing
  mechanism tests for the parked code still run, under an explicit un-park
  fixture.

## Flag

`PARKED_SUBSYSTEMS_ENABLED` (`parked_subsystems_enabled`, default `False`) in
`engine/config/settings.py`. Only a literal `True` un-parks. It exists so the
engine can prove the parked mechanisms in tests and so a recorded decision can
take effect without a code change. Setting it is not itself a decision.

## How to un-park

1. Record a new operator decision in this file (id, date, scope) that
   supersedes `CEG-KGE-PARK` for the named subsystems.
2. Close the findings that caused the parking for those subsystems (for
   CompoundE3D, at least SI-02 training soundness; for HopRAG, a wired,
   flag-gated caller).
3. Set `PARKED_SUBSYSTEMS_ENABLED=True` in the target environment, or remove
   the `require_unparked()` call from the subsystem's entry point together with
   its row in `engine/kge/parked.py`, the architecture guard and this table.
4. Update the Dormant Subsystems table in `.claude/rules/system-state.md` and
   §1a of `docs/FEATURE_GATES.md`.
