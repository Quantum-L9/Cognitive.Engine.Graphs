"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [sync]
tags: [sync, projection, tombstone, revision]
owner: engine-team
status: active
--- /L9_META ---

In-memory rebuildable projection applicator for PACK-029 sync-projection payloads.
Odoo remains authority; CEG stores rebuildable projections keyed by entity_ref+revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.models.payloads import SyncApplyResult, SyncOperation, SyncProjection, SyncProjectionRecord


@dataclass
class ProjectionEntry:
    entity_ref: str
    entity_type: str
    revision: int
    properties: dict[str, Any]
    tombstoned: bool = False
    relationship_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


class ProjectionStore:
    """Revision-aware upsert/tombstone store (idempotent apply + rebuild)."""

    def __init__(self) -> None:
        self._entries: dict[str, ProjectionEntry] = {}

    def get(self, entity_ref: str) -> ProjectionEntry | None:
        return self._entries.get(entity_ref)

    def snapshot(self) -> dict[str, ProjectionEntry]:
        return dict(self._entries)

    def rebuild_from(self, records: list[SyncProjectionRecord]) -> None:
        """Full rebuild from an ordered authority record stream."""
        self._entries.clear()
        for record in sorted(records, key=lambda r: (r.entity_ref, r.revision)):
            self._apply_one(record, accept_stale=True)

    def apply(self, projection: SyncProjection) -> SyncApplyResult:
        accepted: list[str] = []
        reused: list[str] = []
        rejected: list[dict[str, str]] = []
        for record in projection.records:
            status, detail = self._apply_one(record, accept_stale=False)
            if status == "accepted":
                accepted.append(record.entity_ref)
            elif status == "reused":
                reused.append(record.entity_ref)
            else:
                rejected.append({"entity_ref": record.entity_ref, "reason": detail})
        return SyncApplyResult(
            accepted=accepted,
            reused=reused,
            rejected=rejected,
            projection_version=projection.projection_version,
            store_revision_map={ref: entry.revision for ref, entry in self._entries.items()},
        )

    def _apply_one(self, record: SyncProjectionRecord, *, accept_stale: bool) -> tuple[str, str]:
        existing = self._entries.get(record.entity_ref)
        if existing is not None and record.revision < existing.revision and not accept_stale:
            return "rejected", "stale_revision"
        if existing is not None and record.revision == existing.revision:
            # Idempotent replay of the same revision.
            same_op = (record.operation == SyncOperation.TOMBSTONE and existing.tombstoned) or (
                record.operation == SyncOperation.UPSERT
                and not existing.tombstoned
                and existing.properties == dict(record.properties)
            )
            if same_op:
                return "reused", "idempotent_revision"
            return "rejected", "revision_conflict"

        if record.operation == SyncOperation.TOMBSTONE:
            self._entries[record.entity_ref] = ProjectionEntry(
                entity_ref=record.entity_ref,
                entity_type=record.entity_type,
                revision=record.revision,
                properties={},
                tombstoned=True,
                relationship_refs=list(record.relationship_refs),
                evidence_refs=list(record.evidence_refs),
            )
            return "accepted", "tombstone"

        self._entries[record.entity_ref] = ProjectionEntry(
            entity_ref=record.entity_ref,
            entity_type=record.entity_type,
            revision=record.revision,
            properties=dict(record.properties),
            tombstoned=False,
            relationship_refs=list(record.relationship_refs),
            evidence_refs=list(record.evidence_refs),
        )
        return "accepted", "upsert"
