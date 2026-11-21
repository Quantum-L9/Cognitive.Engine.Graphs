"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test]
tags: [semgrep, rules, fixtures]
owner: platform
status: active
--- /L9_META ---

semgrep --test fixture for .semgrep/semgrep-rules.yaml (float-requires-try-except, singleton-requires-lock).

Run: semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py
Excluded from the policy scan by .semgrepignore; not collected by pytest.
"""

import json
import math
import os
import sys
import threading
from typing import Any

import numpy as np


def env_threshold() -> float:
    # ruleid: float-requires-try-except
    return float(os.environ["CEG_THRESHOLD"])


def env_default() -> float:
    # ruleid: float-requires-try-except
    return float(os.getenv("CEG_ALPHA", "0.5"))


def argv_value() -> float:
    # ruleid: float-requires-try-except
    return float(sys.argv[1])


def parsed(text: str) -> float:
    doc = json.loads(text)
    # ruleid: float-requires-try-except
    return float(doc["score"])


async def handle_thing(tenant: str, payload: dict) -> dict:
    raw = payload.get("weight", "0")
    # ruleid: float-requires-try-except
    weight = float(raw)
    return {"weight": weight}


async def handle_guarded(tenant: str, payload: dict) -> dict:
    try:
        # ok: float-requires-try-except
        weight = float(payload["weight"])
    except (ValueError, TypeError):
        weight = 0.0
    return {"weight": weight}


def literal_probe(expr: str) -> bool:
    try:
        # ok: float-requires-try-except
        float(expr)
    except ValueError:
        return False
    else:
        return True


def numeric_only(scores: list[float], row: np.ndarray) -> list[float]:
    # ok: float-requires-try-except
    a = float(np.mean(scores))
    # ok: float-requires-try-except
    b = float(row[0] * 2.0)
    # ok: float-requires-try-except
    c = float("inf")
    # ok: float-requires-try-except
    d = float(math.sqrt(a))
    return [a, b, c, d]


async def handle_cast(tenant: str, payload: dict) -> dict:
    count = int(payload["count"])
    # ok: float-requires-try-except
    return {"ratio": float(count) / 2}


# ── singleton-requires-lock ─────────────────────────────────────────────────

_client = None
_lock = threading.Lock()


def make() -> object:
    return object()


def unlocked_global() -> object:
    global _client
    # ruleid: singleton-requires-lock
    if _client is None:
        _client = make()
    return _client


def locked_global() -> object:
    global _client
    with _lock:
        # ok: singleton-requires-lock
        if _client is None:
            _client = make()
    return _client


class Registry:
    instance: object | None = None
    cache: object | None = None

    @classmethod
    def get(cls) -> object:
        # ruleid: singleton-requires-lock
        if cls.instance is None:
            cls.instance = make()
        return cls.instance

    @staticmethod
    def get_static() -> object:
        # ruleid: singleton-requires-lock
        if Registry.instance is None:
            Registry.instance = make()
        return Registry.instance

    def lazy(self) -> object:
        # ok: singleton-requires-lock
        if self.cache is None:
            self.cache = make()
        return self.cache


def local_default(k: object = None) -> object:
    # ok: singleton-requires-lock
    if k is None:
        k = make()
    return k


# --- CI canary (DO NOT MERGE): no-structlog-getchild known-bad / known-good ---
def canary_getchild(log: Any) -> object:
    # ruleid: no-structlog-getchild
    return log.getChild("canary")


def canary_bind(log: Any) -> object:
    # ok: no-structlog-getchild
    return log.bind(component="canary")
