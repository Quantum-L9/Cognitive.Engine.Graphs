"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [config]
tags: [ingestion, sync]
status: active
--- /L9_META ---

Sync system.
"""

from engine.sync.generator import SyncGenerator

__all__ = ["SyncGenerator"]
