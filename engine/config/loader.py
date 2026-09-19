"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, domain-loader]
owner: engine-team
status: active
--- /L9_META ---

Domain pack loader.
Discovers, validates, caches, and hot-reloads domain spec YAML files.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from engine.config.schema import DomainSpec
from engine.config.settings import settings

logger = logging.getLogger(__name__)

MAX_SPEC_BYTES = 5 * 1024 * 1024
SPEC_FILENAME = "spec.yaml"
_DOMAIN_FEATURE_FLAGS = {"idea-portfolio": "idea_portfolio_enabled"}


class DomainNotFoundError(Exception):
    """Raised when a requested domain spec does not exist or is disabled."""


class DomainSpecError(Exception):
    """Raised when a domain spec fails validation."""


def _domain_enabled(domain_id: str) -> bool:
    flag = _DOMAIN_FEATURE_FLAGS.get(domain_id)
    return flag is None or bool(getattr(settings, flag, False))


class DomainPackLoader:
    """Load and cache folder-shaped domain specs with bounded hot reload."""

    def __init__(self, config_path: str | None = None) -> None:
        raw = config_path or os.getenv("DOMAIN_SPECS_PATH") or "domains"
        self._base_path = Path(raw).resolve()
        self._cache: dict[str, tuple[DomainSpec, float, float]] = {}
        self._lock = threading.Lock()
        self._max_size = int(os.getenv("DOMAIN_CACHE_MAX_SIZE", "100"))
        self._ttl_seconds = float(os.getenv("DOMAIN_CACHE_TTL_SECONDS", "30"))

    def load_domain(self, domain_id: str) -> DomainSpec:
        """Load a domain only when its optional feature gate admits it."""
        if not _domain_enabled(domain_id):
            raise DomainNotFoundError(f"Domain '{domain_id}' is disabled by configuration")
        spec_path = self._resolve_spec_path(domain_id)

        with self._lock:
            if domain_id in self._cache:
                cached_spec, cached_mtime, cached_at = self._cache[domain_id]
                if (time.monotonic() - cached_at) < self._ttl_seconds:
                    return cached_spec
                current_mtime = spec_path.stat().st_mtime
                if cached_mtime >= current_mtime:
                    self._cache[domain_id] = (cached_spec, cached_mtime, time.monotonic())
                    return cached_spec
                logger.info("Domain spec changed on disk, reloading: %s", domain_id)
            else:
                current_mtime = spec_path.stat().st_mtime

            spec = self._load_and_validate(spec_path, domain_id)
            if len(self._cache) >= self._max_size and domain_id not in self._cache:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][2])
                del self._cache[oldest_key]
                logger.debug("Evicted oldest domain cache entry: %s", oldest_key)

            self._cache[domain_id] = (spec, current_mtime, time.monotonic())
            return spec

    def invalidate(self, domain_id: str | None = None) -> None:
        """Force cache invalidation."""
        with self._lock:
            if domain_id:
                self._cache.pop(domain_id, None)
            else:
                self._cache.clear()

    async def load_domain_async(self, domain_id: str) -> DomainSpec:
        """Async domain loading with per-domain stampede prevention."""
        if not _domain_enabled(domain_id):
            raise DomainNotFoundError(f"Domain '{domain_id}' is disabled by configuration")
        with self._lock:
            if domain_id in self._cache:
                cached_spec, _cached_mtime, cached_at = self._cache[domain_id]
                if (time.monotonic() - cached_at) < self._ttl_seconds:
                    return cached_spec

        if not hasattr(self, "_async_locks"):
            self._async_locks: dict[str, asyncio.Lock] = {}
        if domain_id not in self._async_locks:
            self._async_locks[domain_id] = asyncio.Lock()

        async with self._async_locks[domain_id]:
            with self._lock:
                if domain_id in self._cache:
                    cached_spec, _cached_mtime, cached_at = self._cache[domain_id]
                    if (time.monotonic() - cached_at) < self._ttl_seconds:
                        return cached_spec
            return await asyncio.to_thread(self.load_domain, domain_id)

    def list_domains(self) -> list[str]:
        """Discover enabled domain directories containing spec.yaml."""
        if not self._base_path.is_dir():
            return []
        return [
            d.name
            for d in sorted(self._base_path.iterdir())
            if d.is_dir() and (d / SPEC_FILENAME).exists() and _domain_enabled(d.name)
        ]

    def _resolve_spec_path(self, domain_id: str) -> Path:
        """Resolve and validate spec file path, preventing traversal and symlinks."""
        if not domain_id or not domain_id.strip():
            raise DomainNotFoundError("Domain ID cannot be empty")
        if "\x00" in domain_id:
            raise DomainNotFoundError(f"Invalid domain ID: {domain_id!r} contains null byte")
        if Path(domain_id).is_absolute():
            raise DomainNotFoundError(f"Invalid domain ID: {domain_id!r} must be a relative path")

        candidate = (self._base_path / domain_id / SPEC_FILENAME).resolve()
        raw_path = self._base_path / domain_id / SPEC_FILENAME
        if raw_path.is_symlink():
            raise DomainNotFoundError(f"Invalid domain path: {domain_id!r} spec.yaml is a symlink")
        try:
            candidate.relative_to(self._base_path.resolve())
        except ValueError as exc:
            raise DomainNotFoundError(f"Invalid domain path: {domain_id!r} resolves outside base directory") from exc
        if not candidate.exists():
            raise DomainNotFoundError(f"Domain spec not found: {candidate}")
        return candidate

    def _load_and_validate(self, path: Path, domain_id: str) -> DomainSpec:
        """Load YAML and validate against DomainSpec schema."""
        file_size = path.stat().st_size
        if file_size > MAX_SPEC_BYTES:
            raise DomainSpecError(
                f"Domain spec {domain_id} exceeds maximum size: {file_size} bytes > {MAX_SPEC_BYTES} bytes"
            )
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise DomainSpecError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise DomainSpecError(f"Domain spec must be a YAML mapping, got {type(raw).__name__}")
        try:
            return DomainSpec.model_validate(raw)
        except PydanticValidationError as exc:
            raise DomainSpecError(f"Domain '{domain_id}' validation failed:\n{exc}") from exc
