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
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

MismatchClass = Literal["rank", "score", "missing", "extra", "duplicate"]


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
    # ``RankedCandidate.score`` is typed ``float``; this only widens int / numpy
    # scalars. The one external producer (tools/shadow_comparison.py) validates.
    return [
        RankedCandidate(
            candidate_id=r.candidate_id,
            score=float(r.score),  # nosemgrep: semgrep.float-requires-try-except
            rank=int(r.rank),
        )
        for r in ordered
    ]


def _first_by_id(
    rows: list[RankedCandidate], side: Literal["primary", "shadow"], mismatches: list[Mismatch]
) -> dict[str, RankedCandidate]:
    """Index rows by candidate_id, keeping the first (best-ranked) occurrence.

    RU-105 / CEG-COR-128: every further occurrence of an id is reported as a
    ``duplicate`` mismatch instead of silently replacing the earlier row.
    """
    by_id: dict[str, RankedCandidate] = {}
    for row in rows:
        if row.candidate_id not in by_id:
            by_id[row.candidate_id] = row
            continue
        mismatches.append(
            Mismatch(
                mismatch_class="duplicate",
                candidate_id=row.candidate_id,
                primary_rank=row.rank if side == "primary" else None,
                shadow_rank=row.rank if side == "shadow" else None,
                primary_score=row.score if side == "primary" else None,
                shadow_score=row.score if side == "shadow" else None,
                detail=f"candidate_id repeated in {side} ranking",
            )
        )
    return by_id


def _scores_differ(primary_score: float, shadow_score: float, epsilon: float) -> bool:
    """A non-finite score on either side is always a mismatch (NaN compares false)."""
    if not (math.isfinite(primary_score) and math.isfinite(shadow_score)):
        return True
    return abs(primary_score - shadow_score) > epsilon


def _mismatch_sort_key(m: Mismatch) -> tuple[str, str, int, int]:
    return (
        m.mismatch_class,
        m.candidate_id,
        -1 if m.primary_rank is None else m.primary_rank,
        -1 if m.shadow_rank is None else m.shadow_rank,
    )


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
    mismatches: list[Mismatch] = []
    primary_by_id = _first_by_id(primary_n, "primary", mismatches)
    shadow_by_id = _first_by_id(shadow_n, "shadow", mismatches)

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
        if _scores_differ(prow.score, srow.score, score_epsilon):
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

    mismatches.sort(key=_mismatch_sort_key)
    return ShadowComparison(
        packet_id=packet_id,
        primary=primary_n,
        shadow=shadow_n,
        mismatches=mismatches,
    )
