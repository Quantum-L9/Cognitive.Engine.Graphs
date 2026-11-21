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

semgrep --test fixture for .semgrep/ceg-consumer-rules.yaml (the four CEG consumer rules).

Run: semgrep --test --config .semgrep/ceg-consumer-rules.yaml .semgrep/fixtures/ceg-consumer-rules.py
Excluded from the policy scan by .semgrepignore; not collected by pytest.
"""

import json
import math
import os
import sys
import threading

import httpx
import numpy as np
import structlog


def env_threshold() -> float:
    # ruleid: l9.float-requires-try-except
    return float(os.environ["CEG_THRESHOLD"])


def env_default() -> float:
    # ruleid: l9.float-requires-try-except
    return float(os.getenv("CEG_ALPHA", "0.5"))


def argv_value() -> float:
    # ruleid: l9.float-requires-try-except
    return float(sys.argv[1])


def parsed(text: str) -> float:
    doc = json.loads(text)
    # ruleid: l9.float-requires-try-except
    return float(doc["score"])


async def handle_thing(tenant: str, payload: dict) -> dict:
    raw = payload.get("weight", "0")
    # ruleid: l9.float-requires-try-except
    weight = float(raw)
    return {"weight": weight}


async def handle_guarded(tenant: str, payload: dict) -> dict:
    try:
        # ok: l9.float-requires-try-except
        weight = float(payload["weight"])
    except (ValueError, TypeError):
        weight = 0.0
    return {"weight": weight}


def literal_probe(expr: str) -> bool:
    try:
        # ok: l9.float-requires-try-except
        float(expr)
    except ValueError:
        return False
    else:
        return True


def numeric_only(scores: list[float], row: np.ndarray) -> list[float]:
    # ok: l9.float-requires-try-except
    a = float(np.mean(scores))
    # ok: l9.float-requires-try-except
    b = float(row[0] * 2.0)
    # ok: l9.float-requires-try-except
    c = float("inf")
    # ok: l9.float-requires-try-except
    d = float(math.sqrt(a))
    return [a, b, c, d]


async def handle_cast(tenant: str, payload: dict) -> dict:
    count = int(payload["count"])
    # ok: l9.float-requires-try-except
    return {"ratio": float(count) / 2}


# ── l9.singleton-requires-lock ─────────────────────────────────────────────────

_client = None
_lock = threading.Lock()


def make() -> object:
    return object()


def unlocked_global() -> object:
    global _client
    # ruleid: l9.singleton-requires-lock
    if _client is None:
        _client = make()
    return _client


def locked_global() -> object:
    global _client
    with _lock:
        # ok: l9.singleton-requires-lock
        if _client is None:
            _client = make()
    return _client


class Registry:
    instance: object | None = None
    cache: object | None = None

    @classmethod
    def get(cls) -> object:
        # ruleid: l9.singleton-requires-lock
        if cls.instance is None:
            cls.instance = make()
        return cls.instance

    @staticmethod
    def get_static() -> object:
        # ruleid: l9.singleton-requires-lock
        if Registry.instance is None:
            Registry.instance = make()
        return Registry.instance

    def lazy(self) -> object:
        # ok: l9.singleton-requires-lock
        if self.cache is None:
            self.cache = make()
        return self.cache


def local_default(k: object = None) -> object:
    # ok: l9.singleton-requires-lock
    if k is None:
        k = make()
    return k


# ── l9.httpx-async-context-required ─────────────────────────────────────────


async def leaked_client() -> None:
    # ruleid: l9.httpx-async-context-required
    client = httpx.AsyncClient()
    await client.aclose()


async def scoped_client() -> None:
    # ok: l9.httpx-async-context-required
    async with httpx.AsyncClient() as client:
        await client.get("https://example.invalid")


# ── l9.no-structlog-getchild ────────────────────────────────────────────────

log = structlog.get_logger(__name__)


def child_logger() -> object:
    # ruleid: l9.no-structlog-getchild
    return log.getChild("canary")


def bound_logger() -> object:
    # ok: l9.no-structlog-getchild
    return log.bind(component="canary")
