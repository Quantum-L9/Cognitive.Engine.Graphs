"""
--- L9_META ---
l9_schema: 1
origin: tools
engine: graph
layer: [tools]
tags: [hoprag, benchmark, quality, testing]
owner: engine-team
status: active
--- /L9_META ---

Benchmark tool for measuring HopRAG traversal quality.

Provides utilities for evaluating:
- Retrieval F1 / Precision / Recall against ground truth
- Traversal efficiency (hops vs quality)
- Visit count distribution analysis
- Helpfulness metric calibration
- Comparison with baseline (no multi-hop) retrieval

Usage::

    python -m tools.hoprag_benchmark \\
        --ground-truth ground_truth.jsonl \\
        --predictions predictions.jsonl \\
        --output-dir benchmark_results/
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.path_policy import (
    PathPolicyError,
    resolve_existing_file_under_root,
    resolve_root,
    resolve_under_root,
    write_text_no_follow,
)

logger = logging.getLogger(__name__)


@dataclass
class RetrievalMetrics:
    """Standard retrieval quality metrics.

    Attributes:
        precision: Fraction of retrieved items that are relevant.
        recall: Fraction of relevant items that are retrieved.
        f1: Harmonic mean of precision and recall.
        top_k: Number of items retrieved.
        query_id: Query identifier.
    """

    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    top_k: int = 0
    query_id: str = ""


@dataclass
class BenchmarkResult:
    """Aggregated benchmark results.

    Attributes:
        mean_precision: Average precision across queries.
        mean_recall: Average recall across queries.
        mean_f1: Average F1 across queries.
        num_queries: Number of queries evaluated.
        per_query: Per-query metrics.
        execution_time_s: Total benchmark time.
    """

    mean_precision: float = 0.0
    mean_recall: float = 0.0
    mean_f1: float = 0.0
    num_queries: int = 0
    per_query: list[RetrievalMetrics] = field(default_factory=list)
    execution_time_s: float = 0.0


def compute_retrieval_metrics(
    retrieved: list[str],
    relevant: list[str],
    query_id: str = "",
) -> RetrievalMetrics:
    """Compute precision, recall, F1 for a single query.

    Args:
        retrieved: List of retrieved document/vertex IDs.
        relevant: List of ground-truth relevant document/vertex IDs.
        query_id: Optional query identifier.

    Returns:
        RetrievalMetrics for this query.
    """
    if not retrieved or not relevant:
        return RetrievalMetrics(
            precision=0.0,
            recall=0.0,
            f1=0.0,
            top_k=len(retrieved),
            query_id=query_id,
        )

    retrieved_set = set(retrieved)
    relevant_set = set(relevant)

    true_positives = len(retrieved_set & relevant_set)
    precision = true_positives / len(retrieved_set) if retrieved_set else 0.0
    recall = true_positives / len(relevant_set) if relevant_set else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return RetrievalMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        top_k=len(retrieved),
        query_id=query_id,
    )


def run_benchmark(
    ground_truth: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
) -> BenchmarkResult:
    """Run full benchmark comparing predictions against ground truth.

    Args:
        ground_truth: List of dicts with 'query_id' and 'relevant_ids'.
        predictions: List of dicts with 'query_id' and 'retrieved_ids'.

    Returns:
        BenchmarkResult with aggregated and per-query metrics.
    """
    start_time = time.monotonic()

    # Build lookup for predictions
    pred_map: dict[str, list[str]] = {}
    for pred in predictions:
        qid = pred.get("query_id", "")
        pred_map[qid] = pred.get("retrieved_ids", [])

    per_query: list[RetrievalMetrics] = []
    for gt in ground_truth:
        qid = gt.get("query_id", "")
        relevant = gt.get("relevant_ids", [])
        retrieved = pred_map.get(qid, [])

        metrics = compute_retrieval_metrics(
            retrieved=retrieved,
            relevant=relevant,
            query_id=qid,
        )
        per_query.append(metrics)

    num_queries = len(per_query)
    mean_precision = sum(m.precision for m in per_query) / num_queries if num_queries else 0.0
    mean_recall = sum(m.recall for m in per_query) / num_queries if num_queries else 0.0
    mean_f1 = sum(m.f1 for m in per_query) / num_queries if num_queries else 0.0

    execution_time_s = time.monotonic() - start_time

    return BenchmarkResult(
        mean_precision=mean_precision,
        mean_recall=mean_recall,
        mean_f1=mean_f1,
        num_queries=num_queries,
        per_query=per_query,
        execution_time_s=execution_time_s,
    )


def analyze_visit_distribution(
    visit_counts: dict[str, int],
) -> dict[str, Any]:
    """Analyze the distribution of visit counts from traversal.

    Args:
        visit_counts: Dict mapping vertex IDs to visit counts.

    Returns:
        Distribution statistics.
    """
    if not visit_counts:
        return {"total_vertices": 0, "total_visits": 0}

    counts = list(visit_counts.values())
    total = sum(counts)

    return {
        "total_vertices": len(counts),
        "total_visits": total,
        "mean_visits": total / len(counts),
        "max_visits": max(counts),
        "min_visits": min(counts),
        "single_visit_pct": sum(1 for c in counts if c == 1) / len(counts),
        "multi_visit_pct": sum(1 for c in counts if c > 1) / len(counts),
    }


def compare_hop_efficiency(
    metrics_by_hop: dict[int, BenchmarkResult],
) -> dict[str, Any]:
    """Compare retrieval quality across different hop counts.

    Args:
        metrics_by_hop: Dict mapping n_hop → BenchmarkResult.

    Returns:
        Comparison table and optimal hop recommendation.
    """
    comparison: list[dict[str, Any]] = []
    best_f1 = 0.0
    best_hop = 0

    for n_hop in sorted(metrics_by_hop.keys()):
        result = metrics_by_hop[n_hop]
        entry = {
            "n_hop": n_hop,
            "mean_f1": round(result.mean_f1, 4),
            "mean_precision": round(result.mean_precision, 4),
            "mean_recall": round(result.mean_recall, 4),
        }
        comparison.append(entry)

        if result.mean_f1 > best_f1:
            best_f1 = result.mean_f1
            best_hop = n_hop

    return {
        "comparison": comparison,
        "optimal_n_hop": best_hop,
        "optimal_f1": round(best_f1, 4),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for benchmark tool."""
    import argparse

    parser = argparse.ArgumentParser(description="HopRAG Benchmark Tool")
    parser.add_argument(
        "--ground-truth",
        type=str,
        required=True,
        help="Path to ground truth JSONL file",
    )
    parser.add_argument(
        "--predictions",
        type=str,
        required=True,
        help="Path to predictions JSONL file",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmark_results",
        help="Output directory for results",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Allowed root for input and output paths (default: current directory)",
    )
    args = parser.parse_args(argv)

    try:
        root = resolve_root(args.root)
        ground_truth_path = resolve_existing_file_under_root(args.ground_truth, root, option="--ground-truth")
        predictions_path = resolve_existing_file_under_root(args.predictions, root, option="--predictions")
        output_dir = resolve_under_root(args.output_dir, root, option="--output-dir")
    except PathPolicyError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Load data
    try:
        ground_truth = [json.loads(line) for line in ground_truth_path.read_text(encoding="utf-8").splitlines()]
        predictions = [json.loads(line) for line in predictions_path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Run benchmark
    result = run_benchmark(ground_truth, predictions)

    # Output results. Resolve the file itself so a summary.json symlink
    # inside an allowed directory cannot redirect the write outside --root.
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary_path = resolve_under_root(output_dir / "summary.json", root, option="summary.json")
    except PathPolicyError as exc:
        parser.error(str(exc))

    summary = {
        "mean_precision": round(result.mean_precision, 4),
        "mean_recall": round(result.mean_recall, 4),
        "mean_f1": round(result.mean_f1, 4),
        "num_queries": result.num_queries,
        "execution_time_s": round(result.execution_time_s, 3),
    }

    try:
        write_text_no_follow(summary_path.parent, summary_path.name, json.dumps(summary, indent=2))
    except PathPolicyError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Benchmark complete: F1={result.mean_f1:.4f}, P={result.mean_precision:.4f}, R={result.mean_recall:.4f}")
    print(f"Results saved to {output_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
