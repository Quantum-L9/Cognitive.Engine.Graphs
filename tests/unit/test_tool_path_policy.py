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

Regression coverage for explicit CLI path roots.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tools import (
    contract_scanner,
    hoprag_benchmark,
    hydrate_idea_portfolio,
    outcome_replay,
    shadow_comparison,
    spec_extract,
    validate_domain,
)
from tools.path_policy import PathPolicyError, resolve_under_root

pytestmark = pytest.mark.unit


def _root_and_escape(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "allowed"
    root.mkdir()
    escaped = tmp_path / "outside.json"
    escaped.write_text("{}", encoding="utf-8")
    return root, escaped


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink support is required for this path-policy test: {exc}")


def test_shared_policy_resolves_relative_paths_and_rejects_escape(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    _symlink(root / "linked", external_dir)

    assert resolve_under_root("reports/summary.json", root, option="--output") == root / "reports" / "summary.json"
    with pytest.raises(PathPolicyError, match="must stay under --root"):
        resolve_under_root("../outside.json", root, option="--output")
    with pytest.raises(PathPolicyError, match="must stay under --root"):
        resolve_under_root("linked/summary.json", root, option="--output")


def test_contract_scanner_rejects_precommit_path_outside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = _root_and_escape(tmp_path)
    escaped_python = tmp_path / "outside.py"
    escaped_python.write_text("print('outside')\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(contract_scanner.sys, "argv", ["contract_scanner.py", str(escaped_python)])

    assert contract_scanner.main() == 2


def test_contract_scanner_skips_symlinked_file_during_default_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _root_and_escape(tmp_path)
    external_python = tmp_path / "outside.py"
    external_python.write_text("print('outside')\n", encoding="utf-8")
    _symlink(root / "linked.py", external_python)
    seen: list[Path] = []
    monkeypatch.chdir(root)
    monkeypatch.setattr(contract_scanner.sys, "argv", ["contract_scanner.py"])
    monkeypatch.setattr(contract_scanner, "scan_file", lambda path, text, scan_root: seen.append(path) or [])

    assert contract_scanner.main() == 0
    assert seen == []


CliRunner = Callable[[list[str] | None], object]


@pytest.mark.parametrize(
    ("runner", "arguments"),
    [
        (
            hoprag_benchmark.main,
            lambda root, escaped: [
                "--ground-truth",
                str(escaped),
                "--predictions",
                "predictions.jsonl",
                "--output-dir",
                "results",
                "--root",
                str(root),
            ],
        ),
        (hydrate_idea_portfolio.main, lambda root, escaped: [str(escaped), "--root", str(root)]),
        (
            outcome_replay.main,
            lambda root, escaped: [
                "--input",
                str(escaped),
                "--output",
                "results/out.json",
                "--root",
                str(root),
            ],
        ),
        (
            shadow_comparison.main,
            lambda root, escaped: [
                "--input",
                str(escaped),
                "--output",
                "results/out.json",
                "--root",
                str(root),
            ],
        ),
        (
            spec_extract.main,
            lambda root, escaped: ["--spec", str(escaped), "--root", str(root), "--fail-on", "NONE"],
        ),
        (validate_domain.main, lambda root, escaped: [str(escaped), "--root", str(root)]),
    ],
    ids=["hoprag", "hydrate", "outcome-replay", "shadow-comparison", "spec-extract", "validate-domain"],
)
def test_cli_rejects_paths_outside_declared_root(
    tmp_path: Path,
    runner: CliRunner,
    arguments: Callable[[Path, Path], list[str]],
) -> None:
    root, escaped = _root_and_escape(tmp_path)

    with pytest.raises(SystemExit) as raised:
        runner(arguments(root, escaped))

    assert raised.value.code == 2


@pytest.mark.parametrize(
    ("runner", "arguments", "external_output"),
    [
        (
            hoprag_benchmark.main,
            lambda root: [
                "--ground-truth",
                "ground.jsonl",
                "--predictions",
                "predictions.jsonl",
                "--output-dir",
                "linked-output/results",
                "--root",
                str(root),
            ],
            "results/summary.json",
        ),
        (
            outcome_replay.main,
            lambda root: [
                "--input",
                "input.json",
                "--output",
                "linked-output/out.json",
                "--root",
                str(root),
            ],
            "out.json",
        ),
        (
            shadow_comparison.main,
            lambda root: [
                "--input",
                "input.json",
                "--output",
                "linked-output/out.json",
                "--root",
                str(root),
            ],
            "out.json",
        ),
    ],
    ids=["hoprag-output", "outcome-output", "shadow-output"],
)
def test_output_cli_rejects_symlinked_destination(
    tmp_path: Path,
    runner: CliRunner,
    arguments: Callable[[Path], list[str]],
    external_output: str,
) -> None:
    root, _ = _root_and_escape(tmp_path)
    external_dir = tmp_path / "external-output"
    external_dir.mkdir()
    _symlink(root / "linked-output", external_dir)
    (root / "ground.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "predictions.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "input.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        runner(arguments(root))

    assert raised.value.code == 2
    assert not (external_dir / external_output).exists()


def test_outcome_replay_keeps_missing_in_root_input_as_runtime_failure(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)

    assert outcome_replay.main(["--input", "missing.json", "--output", "out.json", "--root", str(root)]) == 1


@pytest.mark.parametrize(
    ("runner", "arguments"),
    [
        (
            hoprag_benchmark.main,
            lambda root: [
                "--ground-truth",
                "missing.jsonl",
                "--predictions",
                "predictions.jsonl",
                "--output-dir",
                "results",
                "--root",
                str(root),
            ],
        ),
        (hydrate_idea_portfolio.main, lambda root: ["missing.json", "--root", str(root)]),
        (shadow_comparison.main, lambda root: ["--input", "missing.json", "--output", "out.json", "--root", str(root)]),
        (spec_extract.main, lambda root: ["--spec", "missing.yaml", "--root", str(root), "--fail-on", "NONE"]),
    ],
    ids=["hoprag", "hydrate", "shadow-comparison", "spec-extract"],
)
def test_cli_keeps_missing_in_root_input_as_runtime_failure(
    tmp_path: Path,
    runner: CliRunner,
    arguments: Callable[[Path], list[str]],
) -> None:
    root, _ = _root_and_escape(tmp_path)

    assert runner(arguments(root)) == 1


def test_domain_validator_keeps_missing_in_root_input_as_exit_one(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)

    with pytest.raises(SystemExit) as raised:
        validate_domain.main(["missing.yaml", "--root", str(root)])

    assert raised.value.code == 1


def test_spec_extractor_rejects_auto_detected_symlinked_spec(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)
    external_spec = tmp_path / "outside-spec.yaml"
    external_spec.write_text("gates:\n  types: {}\n", encoding="utf-8")
    _symlink(root / "my-spec.yaml", external_spec)

    with pytest.raises(SystemExit) as raised:
        spec_extract.main(["--root", str(root), "--fail-on", "NONE"])

    assert raised.value.code == 2


def test_hoprag_rejects_summary_symlink_inside_allowed_directory(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)
    outside = tmp_path / "outside-summary.json"
    outside.write_text("untouched", encoding="utf-8")
    output_dir = root / "results"
    output_dir.mkdir()
    _symlink(output_dir / "summary.json", outside)
    (root / "ground.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "predictions.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        hoprag_benchmark.main(
            [
                "--ground-truth",
                "ground.jsonl",
                "--predictions",
                "predictions.jsonl",
                "--output-dir",
                "results",
                "--root",
                str(root),
            ]
        )

    assert raised.value.code == 2
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_spec_extractor_rejects_artifact_file_symlinks(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)
    (root / "my-spec.yaml").write_text("gates:\n  types: {}\n", encoding="utf-8")
    outside = tmp_path / "outside-artifact.json"
    outside.write_text("untouched", encoding="utf-8")
    artifacts = root / "artifacts"
    artifacts.mkdir()
    _symlink(artifacts / "spec_checklist.json", outside)

    with pytest.raises(SystemExit) as raised:
        spec_extract.main(["--spec", "my-spec.yaml", "--root", str(root), "--fail-on", "NONE"])

    assert raised.value.code == 2
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_spec_extractor_rejects_symlinked_artifacts_directory(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)
    (root / "my-spec.yaml").write_text("gates:\n  types: {}\n", encoding="utf-8")
    external_dir = tmp_path / "external-artifacts"
    external_dir.mkdir()
    _symlink(root / "artifacts", external_dir)

    with pytest.raises(SystemExit) as raised:
        spec_extract.main(["--root", str(root), "--fail-on", "NONE"])

    assert raised.value.code == 2
    assert list(external_dir.iterdir()) == []


def test_spec_extractor_skips_symlinked_research_and_source_files(tmp_path: Path) -> None:
    root, _ = _root_and_escape(tmp_path)
    external_research = tmp_path / "outside-research.json"
    external_research.write_text(
        '{"external": {"engine_mapping": {"search_tokens": ["external_marker"], "search_files": ["engine/**/*.py"]}}}',
        encoding="utf-8",
    )
    research_dir = root / "tools" / "research"
    research_dir.mkdir(parents=True)
    _symlink(research_dir / spec_extract.RESEARCH_PATTERNS_FILE, external_research)
    assert spec_extract.extract_research_features(root) == []

    engine_dir = root / "engine"
    engine_dir.mkdir()
    external_source = tmp_path / "outside.py"
    external_source.write_text("external_marker = True\n", encoding="utf-8")
    _symlink(engine_dir / "linked.py", external_source)
    feature = spec_extract.SpecFeature(
        category="test",
        name="external",
        spec_reference="test",
        search_tokens=["external_marker"],
        search_files=["engine/**/*.py"],
    )

    spec_extract.scan_codebase(root, [feature])

    assert feature.status == spec_extract.Status.MISSING.value
    assert feature.evidence_files == []
