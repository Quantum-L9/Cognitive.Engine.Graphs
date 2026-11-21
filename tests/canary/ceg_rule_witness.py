"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [ci, canary, semgrep, fixtures]
owner: platform
status: active
--- /L9_META ---

CI canary fixture (DO NOT MERGE): CEG-local rule no-structlog-getchild.

Inert: never imported, never collected by pytest.
"""

import structlog

logger = structlog.get_logger(__name__)


def known_bad() -> object:
    # Known-bad: structlog loggers have no getChild(). CEG rule must report this.
    return logger.getChild("canary")


def known_good() -> object:
    # Known-good: bind() is the structlog idiom. CEG rule must not report this.
    return logger.bind(component="canary")
