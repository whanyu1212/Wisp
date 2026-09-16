from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.repository_search import BenchmarkConfig, main, run_benchmark
from wisp.tools.result import ToolError

pytestmark = pytest.mark.benchmark


def _project(root: Path) -> None:
    (root / "a.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def two():\n    return 2\n", encoding="utf-8")
    (root / ".env").write_text("def hidden():\n    return 3\n", encoding="utf-8")


def test_repository_search_measures_public_tools_on_existing_project(tmp_path: Path) -> None:
    _project(tmp_path)

    report = run_benchmark(BenchmarkConfig(cwd=tmp_path, iterations=1, max_results=1))

    assert report.python_files_observed == 2
    assert not report.python_files_capped
    assert {sample.scenario for sample in report.samples} == {
        "find_sorted_python_prefix",
        "grep_literal_miss",
        "grep_literal_capped",
    }
    by_name = {sample.scenario: sample for sample in report.samples}
    assert by_name["find_sorted_python_prefix"].result_count == 2
    assert by_name["find_sorted_python_prefix"].truncated
    assert by_name["grep_literal_miss"].result_count == 0
    assert not by_name["grep_literal_miss"].truncated
    assert by_name["grep_literal_capped"].result_count == 1
    assert by_name["grep_literal_capped"].truncated
    assert all(sample.measurement.wall_ms_per_iteration >= 0 for sample in report.samples)
    assert all(sample.measurement.cpu_ms_per_iteration >= 0 for sample in report.samples)
    assert "hidden" not in report.to_json()


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (BenchmarkConfig(iterations=0), "iterations must be positive"),
        (BenchmarkConfig(max_results=0), "max_results must be positive"),
        (BenchmarkConfig(common_token=""), "common_token must not be empty"),
        (BenchmarkConfig(max_output_lines=0), "output bounds must be positive"),
    ],
)
def test_repository_search_rejects_invalid_config(config: BenchmarkConfig, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run_benchmark(config)


def test_repository_search_rejects_root_outside_selected_cwd(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ToolError, match="outside the tool working directory"):
        run_benchmark(BenchmarkConfig(cwd=inside, root=outside, iterations=1))


def test_repository_search_cli_prints_and_writes_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "profiles" / "search.json"

    main(["--iterations", "1", "--max-results", "1", "--output", str(output)])

    stdout = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert stdout == saved
    assert stdout["python_files_observed"] == 2
    assert len(stdout["samples"]) == 3
