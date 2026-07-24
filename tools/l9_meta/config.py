"""
--- L9_META ---
l9_schema: 2
origin: l9-template
engine: graph
layer: [meta]
tags: [governance, portability]
status: active
--- /L9_META ---

Load `l9-meta.yaml`, the per-repo source of truth for header values.

The config replaces the hardcoded `FILE_REGISTRY` that used to live in
`tools/l9_meta_injector.py`. That registry drifted: it named 10 paths that no
longer existed and omitted ~240 tracked files. Path rules cannot drift the same
way, because they describe intent rather than enumerate files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "l9-meta.yaml"

# Fields a rule or override may set.
SETTABLE = ("origin", "layer", "tags", "status")

# Unknown keys in these blocks are rejected rather than ignored. A misspelled
# vocabulary key silently disables the constraint it was meant to declare, and
# `check` then reports a clean repo while enforcing nothing — the exact failure
# this pipeline exists to prevent.
VOCAB_KEYS = ("origin", "status", "layer", "tags")
VOCAB_TAG_KEYS = ("family", "capability", "concern", "max_tags", "max_per_facet", "min_files")
TOP_KEYS = ("engine", "defaults", "rules", "overrides", "exclude", "vocabulary")


class ConfigError(ValueError):
    """Raised when `l9-meta.yaml` is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class Rule:
    """A path pattern and the fields it sets. Later matching rules win."""

    path: str
    values: dict[str, Any]


@dataclass
class Vocabulary:
    """Allowed values. Empty lists mean "unconstrained"."""

    origin: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    layer: list[str] = field(default_factory=list)
    tag_family: list[str] = field(default_factory=list)
    tag_capability: list[str] = field(default_factory=list)
    tag_concern: list[str] = field(default_factory=list)
    max_tags: int = 3
    max_per_facet: dict[str, int] = field(default_factory=dict)
    min_files: int = 1

    @property
    def all_tags(self) -> set[str]:
        return set(self.tag_family) | set(self.tag_capability) | set(self.tag_concern)

    @property
    def constrains_tags(self) -> bool:
        return bool(self.all_tags)


@dataclass
class Config:
    """Parsed `l9-meta.yaml`."""

    engine: str
    defaults: dict[str, Any]
    rules: list[Rule]
    overrides: dict[str, dict[str, Any]]
    exclude: list[str]
    vocabulary: Vocabulary
    root: Path

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = f"{where} must be a mapping, got {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _check_settable(values: dict[str, Any], where: str) -> dict[str, Any]:
    unknown = set(values) - set(SETTABLE)
    if unknown:
        msg = f"{where} sets unknown field(s) {sorted(unknown)}; allowed: {list(SETTABLE)}"
        raise ConfigError(msg)
    return values


def _reject_unknown(values: dict[str, Any], allowed: tuple[str, ...], where: str) -> dict[str, Any]:
    unknown = set(values) - set(allowed)
    if unknown:
        msg = f"{where}: unknown key(s) {sorted(unknown)}; allowed: {list(allowed)}"
        raise ConfigError(msg)
    return values


def load(root: Path | str = ".") -> Config:
    """Read `l9-meta.yaml` from `root`."""
    root_path = Path(root).resolve()
    path = root_path / CONFIG_NAME
    if not path.is_file():
        msg = f"{CONFIG_NAME} not found at {path}. Run `l9-meta init` to scaffold one."
        raise ConfigError(msg)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"{path} is not valid YAML: {exc}"
        raise ConfigError(msg) from exc

    raw = _reject_unknown(_require_mapping(raw, CONFIG_NAME), TOP_KEYS, CONFIG_NAME)

    engine = raw.get("engine")
    if not engine or not isinstance(engine, str):
        msg = f"{CONFIG_NAME} must set a string `engine`"
        raise ConfigError(msg)

    defaults = _check_settable(_require_mapping(raw.get("defaults"), "defaults"), "defaults")
    for required in ("origin", "layer"):
        if required not in defaults:
            msg = f"defaults must set `{required}`"
            raise ConfigError(msg)

    rules: list[Rule] = []
    raw_rules = raw.get("rules") or []
    if not isinstance(raw_rules, list):
        msg = "rules must be a list"
        raise ConfigError(msg)
    for i, raw_entry in enumerate(raw_rules):
        entry = _require_mapping(raw_entry, f"rules[{i}]")
        pattern = entry.get("path")
        if not pattern or not isinstance(pattern, str):
            msg = f"rules[{i}] must set a string `path`"
            raise ConfigError(msg)
        values = _check_settable({k: v for k, v in entry.items() if k != "path"}, f"rules[{i}] ({pattern})")
        if not values:
            msg = f"rules[{i}] ({pattern}) sets no fields"
            raise ConfigError(msg)
        rules.append(Rule(path=pattern, values=values))

    overrides: dict[str, dict[str, Any]] = {}
    for key, value in _require_mapping(raw.get("overrides"), "overrides").items():
        overrides[key] = _check_settable(_require_mapping(value, f"overrides[{key}]"), f"overrides[{key}]")

    exclude = raw.get("exclude") or []
    if not isinstance(exclude, list):
        msg = "exclude must be a list of glob patterns"
        raise ConfigError(msg)

    vocab_raw = _reject_unknown(_require_mapping(raw.get("vocabulary"), "vocabulary"), VOCAB_KEYS, "vocabulary")
    tags_raw = _reject_unknown(
        _require_mapping(vocab_raw.get("tags"), "vocabulary.tags"), VOCAB_TAG_KEYS, "vocabulary.tags"
    )
    vocabulary = Vocabulary(
        origin=list(vocab_raw.get("origin") or []),
        status=list(vocab_raw.get("status") or []),
        layer=list(vocab_raw.get("layer") or []),
        tag_family=list(tags_raw.get("family") or []),
        tag_capability=list(tags_raw.get("capability") or []),
        tag_concern=list(tags_raw.get("concern") or []),
        max_tags=int(tags_raw.get("max_tags", 3)),
        max_per_facet={
            k: int(v)
            for k, v in _require_mapping(tags_raw.get("max_per_facet") or {}, "vocabulary.tags.max_per_facet").items()
        },
        min_files=int(tags_raw.get("min_files", 1)),
    )
    overlap = (
        (set(vocabulary.tag_family) & set(vocabulary.tag_capability))
        | (set(vocabulary.tag_family) & set(vocabulary.tag_concern))
        | (set(vocabulary.tag_capability) & set(vocabulary.tag_concern))
    )
    if overlap:
        # A tag in two facets makes facet cardinality ambiguous and ordering
        # dependent on which list is checked first.
        msg = f"vocabulary.tags: {sorted(overlap)} appear in more than one facet"
        raise ConfigError(msg)

    return Config(
        engine=engine,
        defaults=defaults,
        rules=rules,
        overrides=overrides,
        exclude=[str(p) for p in exclude],
        vocabulary=vocabulary,
        root=root_path,
    )
