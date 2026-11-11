"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [config]
tags: [config, admission, domain-neutrality, gates, scoring, gds]
owner: engine-team
status: active
--- /L9_META ---

Structural properties a domain must declare (Stage 2 follow-up, item 8).

Owner decision "declare all, including geo": the exclusion edge and its target
property, the community property and the geo coordinate / identity properties
are the domain's vocabulary. The gate compiler, the scoring assembler and the
GDS scheduler used to fill an undeclared one with one pack's name
(``EXCLUDED_FROM``, ``entity_id``, ``community_id``, ``communityId``, ``lat``,
``lon``). With ``settings.declared_structural_properties_required_enabled`` on
(default):

* the loader withholds a pack that uses one undeclared, with a named defect
  (:func:`structural_property_defects`, prefix :data:`STRUCTURAL_DEFECT_PREFIX`);
* the compilers raise :class:`UndeclaredStructuralPropertyError` instead of
  substituting a name, for callers that bypass admission.

Off, the former defaults return verbatim (C-21 rollback). They live only in
:data:`_LEGACY_DEFAULTS`, read by :func:`declared` and nowhere else.
"""

from __future__ import annotations

from typing import Any

STRUCTURAL_DEFECT_PREFIX = "undeclared structural property: "

# The names the compilers borrowed before this follow-up. Read only with
# declared_structural_properties_required_enabled off (C-21 rollback).
_LEGACY_DEFAULTS: dict[str, str] = {
    "exclusion_edge": "EXCLUDED_FROM",
    "exclusion_target": "entity_id",
    "community": "community_id",
    "louvain_write": "communityId",
    "latitude": "lat",
    "longitude": "lon",
    "geo_id": "entity_id",
}


class UndeclaredStructuralPropertyError(ValueError):
    """A gate, scoring dimension or GDS job uses a structural property the domain does not declare."""


def required() -> bool:
    """Whether undeclared structural properties are refused (the flag)."""
    from engine.config.settings import settings

    return bool(settings.declared_structural_properties_required_enabled)


def declared(value: Any, kind: str, defect: str) -> str:
    """``value`` when declared; otherwise refuse, or (flag off) the former default for ``kind``."""
    if value is not None and str(value).strip():
        return str(value)
    if required():
        raise UndeclaredStructuralPropertyError(STRUCTURAL_DEFECT_PREFIX + defect)
    return _LEGACY_DEFAULTS[kind]


def _job_setting(job: Any, key: str) -> Any | None:
    for prop in getattr(job, "writeproperties", None) or []:
        if key in prop:
            return prop[key]
    return None


def _present(value: Any) -> bool:
    return value is not None and bool(str(value).strip())


def _value(item: Any, attribute: str) -> str:
    raw = getattr(item, attribute, None)
    return str(getattr(raw, "value", raw))


def gate_defects(gate: Any) -> list[str]:
    """An exclusion gate declares its edge type, and its target property when it names a query parameter."""
    if _value(gate, "type") != "exclusion":
        return []
    defects: list[str] = []
    if not _present(getattr(gate, "edgetype", None)):
        defects.append(f"gate {gate.name!r} (exclusion) declares no edgetype (the exclusion edge type)")
    if getattr(gate, "queryparam", None) is not None and not _present(getattr(gate, "candidateprop", None)):
        defects.append(
            f"gate {gate.name!r} (exclusion) names a query parameter but declares no candidateprop "
            "(the excluded node's property it is compared with)"
        )
    return defects


def dimension_defects(dim: Any) -> list[str]:
    """Geo and community scoring dimensions declare the properties they read."""
    computation = _value(dim, "computation")
    name = getattr(dim, "name", "?")
    candidateprop = getattr(dim, "candidateprop", None)
    if computation == "geodecay":
        defects = []
        if not _present(candidateprop):
            defects.append(f"scoring {name!r} (geodecay) declares no candidateprop (the latitude property)")
        if not _present(getattr(dim, "candidateprop_lon", None)):
            defects.append(f"scoring {name!r} (geodecay) declares no candidateprop_lon (the longitude property)")
        return defects
    if computation in ("communitymatch", "preference_attention") and not _present(candidateprop):
        return [f"scoring {name!r} ({computation}) declares no candidateprop (the community property)"]
    if computation == "community_bridge":
        metadata = getattr(dim, "metadata", None)
        listed = metadata.get("community_properties") if isinstance(metadata, dict) else None
        if not listed and not _present(candidateprop):
            return [community_bridge_defect(name)]
    return []


def community_bridge_defect(name: str) -> str:
    """The defect for a community_bridge dimension that names no community property."""
    return f"scoring {name!r} (community_bridge) declares no community property (candidateprop or metadata.community_properties)"


def gds_job_defects(job: Any) -> list[str]:
    """Louvain declares the property it writes; geoproximity declares its coordinate and id properties."""
    algorithm = getattr(job, "algorithm", None)
    name = getattr(job, "name", "?")
    if algorithm == "louvain" and not _present(getattr(job, "writeproperty", None)):
        return [f"GDS job {name!r} (louvain) declares no writeproperty (the community property it writes)"]
    if algorithm == "geoproximity":
        return [
            f"GDS job {name!r} (geoproximity) declares no writeproperties entry {key!r}"
            for key in ("lat_property", "lon_property", "id_property")
            if not _present(_job_setting(job, key))
        ]
    return []


def structural_property_defects(spec: Any) -> list[str]:
    """Every undeclared structural property the pack's gates, scoring and GDS jobs use (flag on only)."""
    if not required():
        return []
    defects: list[str] = []
    for gate in getattr(spec, "gates", None) or []:
        defects.extend(gate_defects(gate))
    scoring = getattr(spec, "scoring", None)
    for dim in getattr(scoring, "dimensions", None) or []:
        defects.extend(dimension_defects(dim))
    for job in getattr(spec, "gdsjobs", None) or []:
        defects.extend(gds_job_defects(job))
    return [STRUCTURAL_DEFECT_PREFIX + defect for defect in defects]
