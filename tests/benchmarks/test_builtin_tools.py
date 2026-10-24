from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from benchmarks.builtin_tools import BenchmarkConfig, main, run_benchmark

pytestmark = pytest.mark.benchmark


def test_builtin_tools_benchmark_exercises_public_tool_and_executor_paths() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            file_counts=(8,),
            file_bytes=512,
            iterations=1,
        )
    )

    assert len(report.samples) == 18
    assert {sample.layer for sample in report.samples} == {"tool.run", "executor"}
    assert {sample.scenario for sample in report.samples} == {
        "read_first_page",
        "read_tail_page",
        "ls_sorted_prefix",
        "find_sorted_prefix",
        "grep_literal_miss",
        "grep_regex_miss",
        "grep_ignore_case_miss",
        "grep_literal_capped",
        "grep_regex_capped",
    }
    assert all(sample.file_count == 8 for sample in report.samples)
    assert all(sample.measurement.wall_ms >= 0 for sample in report.samples)
    assert all(sample.measurement.cpu_ms >= 0 for sample in report.samples)
    assert all(sample.output_bytes >= 0 for sample in report.samples)

    direct = {sample.scenario: sample for sample in report.samples if sample.layer == "tool.run"}
    assert direct["read_first_page"].result_count == 100
    assert direct["read_tail_page"].result_count == 100
    assert direct["ls_sorted_prefix"].result_count == 8
    assert direct["find_sorted_prefix"].result_count == 8
    assert direct["grep_literal_miss"].result_count == 0
    assert direct["grep_regex_miss"].result_count == 0
    assert direct["grep_ignore_case_miss"].result_count == 0
    assert direct["grep_literal_capped"].result_count == 8
    assert direct["grep_regex_capped"].result_count == 8
    assert not any(sample.truncated for sample in report.samples)
    assert '"scenario": "grep_literal_miss"' in report.to_json()


def test_builtin_tools_benchmark_rejects_too_small_file_payload() -> None:
    with pytest.raises(ValueError, match="file_bytes must be at least 64"):
        run_benchmark(BenchmarkConfig(file_counts=(1,), file_bytes=1))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_output_bytes": 1_024}, "max_output_bytes must be at least"),
        ({"max_output_lines": 100}, "max_output_lines must be at least 101"),
    ],
)
def test_builtin_tools_benchmark_rejects_budgets_below_fixed_scenario_minimums(
    overrides: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        run_benchmark(BenchmarkConfig(file_counts=(1,), file_bytes=512, **overrides))


def test_builtin_tools_benchmark_accepts_expected_listing_truncation() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            file_counts=(102,),
            file_bytes=512,
            max_output_lines=101,
            iterations=1,
        )
    )

    listing = [sample for sample in report.samples if sample.scenario == "ls_sorted_prefix"]
    assert len(listing) == 2
    assert all(sample.truncated for sample in listing)


def test_builtin_tools_cli_creates_output_parent(tmp_path: Path) -> None:
    output = tmp_path / "profiles" / "builtin-tools.json"

    main(
        [
            "--file-counts",
            "8",
            "--file-bytes",
            "512",
            "--iterations",
            "1",
            "--tool-only",
            "--output",
            str(output),
        ]
    )

    assert output.is_file()
    assert '"scenario": "grep_literal_miss"' in output.read_text(encoding="utf-8")


def test_builtin_tools_benchmark_adds_batch_ceiling_and_bash_layers() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            file_counts=(8,),
            file_bytes=512,
            iterations=1,
            include_executor=False,
            batch_size=3,
            external_ceiling=True,
            include_bash=True,
        )
    )

    batches = {sample.scenario: sample for sample in report.samples if sample.layer == "batch"}
    assert len(batches) == 9
    assert all(sample.result_count == 3 for sample in batches.values())
    assert batches["grep_literal_miss"].input_bytes == 3 * 8 * 512
    serial = {s.scenario: s for s in report.samples if s.layer == "batch.serial"}
    assert set(serial) == set(batches)
    assert all(sample.result_count == 3 for sample in serial.values())
    ceilings = {sample.scenario for sample in report.samples if sample.layer == "ripgrep"}
    if shutil.which("rg") is None:
        assert ceilings == set()
    else:
        assert ceilings == {
            "find_sorted_prefix",
            "grep_literal_miss",
            "grep_regex_miss",
            "grep_ignore_case_miss",
        }
    bash = [sample for sample in report.samples if sample.scenario.startswith("bash_")]
    assert [(sample.scenario, sample.layer) for sample in bash] == [
        ("bash_true", "tool.run"),
        ("bash_true", "sh"),
        ("bash_short_lines_1mib", "tool.run"),
        ("bash_short_lines_1mib", "sh"),
        ("bash_long_line_1mib", "tool.run"),
        ("bash_long_line_1mib", "sh"),
    ]
    assert bash[3].output_bytes > 1_000_000


def test_builtin_tools_benchmark_searches_an_existing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "project" / "package"
    package.mkdir(parents=True)
    (package / "module.py").write_text("def first():\n    pass\n\ndef second():\n    pass\n")
    (package / "notes.txt").write_text("plain text\n")
    # Protected by default, so the searches skip it and it must not inflate the counts.
    (package / ".env").write_text("SECRET=" + "x" * 10_000 + "\n")

    report = run_benchmark(
        BenchmarkConfig(root=Path("project"), iterations=1, include_executor=False)
    )

    direct = {sample.scenario: sample for sample in report.samples}
    assert set(direct) == {
        "read_first_page",
        "find_sorted_prefix",
        "grep_literal_miss",
        "grep_regex_miss",
        "grep_ignore_case_miss",
        "grep_regex_capped",
        "grep_literal_capped",
    }
    assert all(sample.file_count == 2 for sample in report.samples)
    module_and_notes = len("def first():\n    pass\n\ndef second():\n    pass\n") + 11
    assert direct["grep_literal_miss"].input_bytes == module_and_notes
    assert direct["find_sorted_prefix"].result_count == 1
    assert direct["grep_regex_capped"].result_count == 2
    assert direct["grep_literal_miss"].result_count == 0


def test_builtin_tools_benchmark_rejects_a_root_outside_the_working_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="inside the current working directory"):
        run_benchmark(BenchmarkConfig(root=tmp_path, iterations=1))


def test_builtin_tools_benchmark_skips_the_read_scenario_without_a_utf8_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "image.bin").write_bytes(b"\xff\xfe\x00binary")

    report = run_benchmark(
        BenchmarkConfig(root=Path("assets"), iterations=1, include_executor=False)
    )

    scenarios = {sample.scenario for sample in report.samples}
    assert "read_first_page" not in scenarios
    assert "grep_literal_miss" in scenarios
