<!-- L9_META
l9_schema: 1
origin: engine-specific
engine: graph
layer: [docs, contracts]
tags: [contracts, kge, embeddings]
owner: engine-team
status: active
/L9_META -->

# KGE Embeddings Contract

**Enforces:** `CONTRACT-20` (`contracts/contract_20.yaml`)
**Closes:** Agents wiring embeddings as a side channel or sharing vectors across tenants

## Status: dormant

`kge_enabled` defaults to `False` in `engine/config/settings.py`. The subsystem exists and
is tested, but no production path runs it. Activating it is an operator decision — see
`FEATURE_FLAG_DISCIPLINE.md`.

## Rule

Knowledge-graph embeddings are a **scoring dimension**, not a parallel ranking system.
KGE scores feed the same `WITH` clause as every other dimension and are subject to the
same weight ceiling.

## Model

| Property | Value |
|---|---|
| Model | CompoundE3D |
| Default dimension | 256 (`kge_embedding_dim`, must match `KGESpec.embeddingdim`) |
| Confidence threshold | 0.3 (`kge_confidence_threshold`) |
| Training relations | Derived from `spec.ontology.edges` |
| Link prediction | Beam search, width 10, depth 3 |
| Vector index | Neo4j, cosine similarity |

Ensemble strategies: `weighted_average`, `rank_aggregation`, `mixture_of_experts`.

## Tenant isolation

Embeddings are **domain-specific and never shared across tenants**. A vector trained on
one tenant's graph must not be read, indexed, or ensembled into another tenant's scoring.
This is the same isolation boundary as every Neo4j query (`CONTRACT-03`) — the vector
index does not get an exception.

## Correct

```yaml
scoring:
  dimensions:
    - name: kge_similarity
      computation: custom_cypher
      weight: 0.15          # counts against the 1.0 ceiling like any other dimension
```

## Wrong

```python
# WRONG — embeddings ranked separately, then merged in Python
kge_ranked = await kge_service.rank(query)
gate_ranked = await run_match(tenant, payload)
return merge(kge_ranked, gate_ranked)
```

Post-hoc merging in Python violates `CONTRACT-13` (gate-then-score in Cypher). KGE scores
belong in the single scoring clause.

```python
# WRONG — cross-tenant vector reuse
index = shared_vector_index                 # violates tenant isolation
```

## Verified by

- `tests/contracts/test_contracts.py::TestContract20KGEEmbeddings`
- `tests/unit/` (KGE scoring dimension)
