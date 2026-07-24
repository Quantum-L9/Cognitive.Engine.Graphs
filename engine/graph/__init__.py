"""
--- L9_META ---
l9_schema: 2
origin: engine-specific
engine: graph
layer: [config]
tags: [platform, driver]
status: active
--- /L9_META ---

Graph database interface.
"""

from engine.graph.driver import GraphDriver

__all__ = ["GraphDriver"]
