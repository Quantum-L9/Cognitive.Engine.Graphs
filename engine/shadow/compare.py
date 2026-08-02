"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [shadow]
tags: [shadow, comparison, observational, task-055]
owner: engine-team
status: active
--- /L9_META ---

Deterministic primary-vs-shadow ranking comparison (observational only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

MismatchClass = Literal["rank", "score", "missing", "extra"]


@dataclass(frozen=True)
class RankedCandidate:
    candidate_id: str
    score: float
    rank: int


@dataclass
class Mismatch:
    mismatch_class: MismatchClass
    candidate_id: str
    primary_rank: int | None = None
    shadow_rank: int | None = None
    primary_score: float | None = None
    shadow_score: float | None = None
    detail: str = ""


@dataclass
class ShadowComparison:
    schema: str = "l9.ceg.shadow_comparison.v1"
    packet_id: str = ""
    observational: bool = True
    replaces_primary: bool = False
    primary: list[RankedCandidate] = field(default_factory=list)
    shadow: list[RankedCandidate] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    checksum: str = ""

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "packet_id": self.packet_id,
            "observational": self.observational,
            "replaces_primary": self.replaces_primary,
            "primary": [asdict(x) for x in self.primary],
            "shadow": [asdict(x) for x in self.shadow],
            "mismatches": [asdict(x) for x in self.mismatches],
        }
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.checksum = "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
        body["checksum"] = self.checksum
        return body


def _normalize(rows: list[RankedCandidate]) -> list[RankedCandidate]:
    ordered = sorted(rows, key=lambda r: (r.rank, r.candidate_id))
    return [RankedCandidate(candidate_id=r.candidate_id, score=float(r.score), rank=int(r.rank)) for r in ordered]


def emit_shadow_comparison(
    *,
    packet_id: str,
    primary: list[RankedCandidate],
    shadow: list[RankedCandidate],
    score_epsilon: float = 1e-9,
) -> ShadowComparison:
    """Compare primary match ranking to a shadow scorer ranking.

    Pure function: never mutates match handler outputs; observational artifact only.
    """
    primary_n = _normalize(primary)
    shadow_n = _normalize(shadow)
    primary_by_id = {r.candidate_id: r for r in primary_n}
    shadow_by_id = {r.candidate_id: r for r in shadow_n}
    mismatches: list[Mismatch] = []

    for cid, prow in primary_by_id.items():
        srow = shadow_by_id.get(cid)
        if srow is None:
            mismatches.append(
                Mismatch(
                    mismatch_class="missing",
                    candidate_id=cid,
                    primary_rank=prow.rank,
                    primary_score=prow.score,
                    detail="present in primary, absent in shadow",
                )
            )
            continue
        if prow.rank != srow.rank:
            mismatches.append(
                Mismatch(
                    mismatch_class="rank",
                    candidate_id=cid,
                    primary_rank=prow.rank,
                    shadow_rank=srow.rank,
                    primary_score=prow.score,
                    shadow_score=srow.score,
                    detail="rank differs",
                )
            )
        if abs(prow.score - srow.score) > score_epsilon:
            mismatches.append(
                Mismatch(
                    mismatch_class="score",
                    candidate_id=cid,
                    primary_rank=prow.rank,
                    shadow_rank=srow.rank,
                    primary_score=prow.score,
                    shadow_score=srow.score,
                    detail="score differs beyond epsilon",
                )
            )

    for cid, srow in shadow_by_id.items():
        if cid not in primary_by_id:
            mismatches.append(
                Mismatch(
                    mismatch_class="extra",
                    candidate_id=cid,
                    shadow_rank=srow.rank,
                    shadow_score=srow.score,
                    detail="present in shadow, absent in primary",
                )
            )

    mismatches.sort(key=lambda m: (m.mismatch_class, m.candidate_id))
    return ShadowComparison(
        packet_id=packet_id,
        primary=primary_n,
        shadow=shadow_n,
        mismatches=mismatches,
    )
