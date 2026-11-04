"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [test, security]
tags: [path-policy, cli, filesystem-boundary]
owner: engine-team
status: active
--- /L9_META ---

PR-394-01: child outputs must not follow pre-existing symlinks.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import pytest

from tools import hoprag_benchmark, path_policy, spec_extract

pytestmark = pytest.mark.unit


def _inputs(root: Path) -> None:
    (root / "ground.jsonl").write_text('{"query_id": "q", "relevant_ids": ["a"]}\n', encoding="utf-8")
    (root / "predictions.jsonl").write_text('{"query_id": "q", "retrieved_ids": ["a"]}\n', encoding="utf-8")
    (root / "my-spec.yaml").write_text("gates:\n  types: {}\n", encoding="utf-8")


def _benchmark(root: Path) -> int:
    return hoprag_benchmark.main(
        [
            "--ground-truth",
            "ground.jsonl",
            "--predictions",
            "predictions.jsonl",
            "--output-dir",
            "results/nested",
            "--root",
            str(root),
        ]
    )


def _extract(root: Path) -> int:
    return spec_extract.main(["--root", str(root), "--spec", "my-spec.yaml", "--fail-on", "NONE"])


@pytest.mark.parametrize(
    ("runner", "relative_child"),
    [
        (_benchmark, "results/nested/summary.json"),
        (_extract, "artifacts/spec_checklist.json"),
        (_extract, "artifacts/coverage_matrix.json"),
        (_extract, "artifacts/coverage_report.md"),
    ],
    ids=["benchmark-summary", "extract-checklist", "extract-matrix", "extract-report"],
)
def test_cli_refuses_preexisting_outside_child_symlink(
    tmp_path: Path, runner: Callable[[Path], int], relative_child: str
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    _inputs(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside sentinel", encoding="utf-8")
    child = root / relative_child
    child.parent.mkdir(parents=True)
    child.symlink_to(outside)

    with pytest.raises(SystemExit) as raised:
        runner(root)

    assert raised.value.code == 2
    assert outside.read_text(encoding="utf-8") == "outside sentinel"
    assert child.is_symlink()


def test_benchmark_writes_real_nested_output_and_overwrites_regular_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _inputs(tmp_path)
    assert _benchmark(tmp_path) == 0
    summary = tmp_path / "results/nested/summary.json"
    summary.write_text("long stale output" * 100, encoding="utf-8")
    assert _benchmark(tmp_path) == 0
    data = json.loads(summary.read_text(encoding="utf-8"))
    assert set(data) == {"mean_precision", "mean_recall", "mean_f1", "num_queries", "execution_time_s"}
    assert data["mean_f1"] == 1.0
    assert data["num_queries"] == 1
    assert "Benchmark complete: F1=1.0000, P=1.0000, R=1.0000" in capsys.readouterr().out


def test_extractor_writes_all_real_outputs_and_preserves_fail_on(tmp_path: Path) -> None:
    _inputs(tmp_path)
    assert _extract(tmp_path) == 0
    artifacts = tmp_path / "artifacts"
    assert {p.name for p in artifacts.iterdir()} == {
        "spec_checklist.json",
        "coverage_matrix.json",
        "coverage_report.md",
    }
    checklist = json.loads((artifacts / "spec_checklist.json").read_text(encoding="utf-8"))
    matrix = json.loads((artifacts / "coverage_matrix.json").read_text(encoding="utf-8"))
    assert matrix["totals"]["total"] == len(checklist)
    assert matrix["totals"]["MISSING"] > 0
    assert (artifacts / "coverage_report.md").read_text(encoding="utf-8").startswith("# L9 Spec Coverage Report")
    assert spec_extract.main(["--root", str(tmp_path), "--spec", "my-spec.yaml", "--fail-on", "MISSING"]) == 1


def test_benchmark_preserves_resolved_in_root_output_directory_symlink(tmp_path: Path) -> None:
    _inputs(tmp_path)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    (tmp_path / "results").symlink_to(real_output, target_is_directory=True)
    assert _benchmark(tmp_path) == 0
    assert json.loads((real_output / "nested/summary.json").read_text(encoding="utf-8"))["mean_f1"] == 1.0


@pytest.mark.parametrize("child", ["../outside.txt", "/outside.txt", "nested/out.txt", "..", ".", ""])
def test_safe_writer_rejects_non_child_names(tmp_path: Path, child: str) -> None:
    with pytest.raises(path_policy.PathPolicyError):
        path_policy.write_text_no_follow(tmp_path, child, "payload")


def test_safe_writer_refuses_dangling_child_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    (tmp_path / "summary.json").symlink_to(outside)
    with pytest.raises(path_policy.PathPolicyError):
        path_policy.write_text_no_follow(tmp_path, "summary.json", "payload")
    assert not outside.exists()


def test_safe_writer_refuses_symlink_inserted_after_directory_resolution(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    output = root / "results"
    output.mkdir()
    resolved = path_policy.resolve_under_root("results", root, option="--output-dir")
    outside = tmp_path / "outside"
    outside.mkdir()
    output.rmdir()
    output.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        path_policy.write_text_no_follow(resolved, "summary.json", "payload")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("failure", ["directory-open", "mkdir", "child-open", "fstat", "truncate", "fdopen", "write"])
def test_safe_writer_closes_all_owned_fds_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    opened: list[int] = []
    real_open = os.open
    real_fstat = os.fstat
    real_fdopen = os.fdopen

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "injected descriptor-path failure")

    def track_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if (failure == "directory-open" and path == "nested") or (failure == "child-open" and path == "summary.json"):
            fail()
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    @contextmanager
    def failing_write(fd: int, mode: str, *, encoding: str, closefd: bool) -> Iterator[TextIO]:
        with real_fdopen(fd, mode, encoding=encoding, closefd=closefd) as stream:
            monkeypatch.setattr(stream, "write", fail)
            yield stream

    with monkeypatch.context() as scoped:
        scoped.setattr(path_policy.os, "open", track_open)
        if failure == "mkdir":
            scoped.setattr(path_policy.os, "mkdir", fail)
        elif failure == "fstat":
            scoped.setattr(path_policy.os, "fstat", fail)
        elif failure == "truncate":
            scoped.setattr(path_policy.os, "ftruncate", fail)
        elif failure == "fdopen":
            scoped.setattr(path_policy.os, "fdopen", fail)
        elif failure == "write":
            scoped.setattr(path_policy.os, "fdopen", failing_write)

        with pytest.raises(OSError, match="injected descriptor-path failure"):
            path_policy.write_text_no_follow(tmp_path / "nested", "summary.json", "payload")

    assert opened, "exception must exercise owned descriptor cleanup"
    for fd in opened:
        with pytest.raises(OSError) as raised:
            real_fstat(fd)
        assert raised.value.errno == errno.EBADF
