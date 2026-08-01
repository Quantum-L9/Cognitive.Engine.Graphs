# L9 Engine Audit Report

- Generated: 2026-07-24T21:09:10.262518+00:00
- Repo root: `/private/tmp/ceg-pr-wiring`
- Template tag: `L9_TEMPLATE`

## CRITICAL
No findings.

## HIGH
No findings.

## MEDIUM
### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/__init__.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/__init__.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/boot.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/boot.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/contract_enforcement.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/contract_enforcement.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/convergence_controller_patch.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/convergence_controller_patch.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/gate_client.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/gate_client.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/gate_registration.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/gate_registration.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_GDS_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/gds/__init__.py`
- Issue: GDS scheduler should exist and include known algorithms.
- Fix: Implement required flow anchors or algorithms as per engine contract.

```
Missing required tokens: ['class GDSScheduler', 'register_jobs', 'execute_job', '_run_louvain', '_run_cooccurrence', '_run_reinforcement', '_run_temporal_recency']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/graph_return_channel.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/graph_return_channel.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/inference_bridge.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/inference_bridge.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/inference_rule_registry.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/inference_rule_registry.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/packet_bridge.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/packet_bridge.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/startup_wiring.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/startup_wiring.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

### TRACE_MATCH_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/state.py`
- Issue: Match flow should reference GateCompiler, TraversalAssembler, ScoringAssembler, GraphDriver.execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_match', 'GateCompiler(', 'TraversalAssembler(', 'ScoringAssembler(', 'execute_query(']
```

### TRACE_SYNC_FLOW_ANCHORS
- File: `/private/tmp/ceg-pr-wiring/engine/state.py`
- Issue: Sync flow should reference SyncGenerator and execute_query.
- Fix: Ensure lifecycle entrypoints reference expected components.

```
None of required-any tokens found: ['handle_sync', 'SyncGenerator(', 'generate_sync_query(', 'execute_query(']
```

## LOW
No findings.
