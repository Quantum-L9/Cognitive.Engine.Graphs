"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [hoprag]
tags: [hoprag, subsystem, init]
owner: engine-team
status: active
--- /L9_META ---

PARKED (CEG-KGE-PARK, operator decision 2026-11-14): HopRAG is parked. No runtime path
may construct, train or run it; GraphIndexBuilder raises
engine.kge.parked.ParkedSubsystemError. Code kept, not deleted — see
docs/PARKED_SUBSYSTEMS.md.

HopRAG subsystem for Cognitive Engine Graphs.

Provides multi-hop reasoning-augmented graph retrieval capabilities
based on HopRAG (ACL 2025). This subsystem adds:

- Graph-structured index construction with pseudo-queries on edges
- Multi-hop BFS traversal with optional LLM-guided reasoning
- Helpfulness scoring combining similarity and traversal importance
- Edge density control for sparse, traversal-friendly graphs

There is no ``hoprag_enabled`` setting: the subsystem is parked, and
``settings.parked_subsystems_enabled`` (default ``False``) is the only
override.

Quick start::

    from engine.hoprag.config import HopRAGConfig
    from engine.scoring.helpfulness import HelpfulnessScorer
    from engine.scoring.importance import ImportanceScorer
    from engine.traversal.multihop import MultiHopTraverser

    config = HopRAGConfig()
    scorer = HelpfulnessScorer(alpha=config.alpha)
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "HopRAGConfig",
]

from engine.hoprag.config import HopRAGConfig
