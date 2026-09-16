from __future__ import annotations

import pytest

from benchmarks.builtin_tools import BenchmarkConfig, run_benchmark

pytestmark = pytest.mark.benchmark


def test_builtin_tools_benchmark_exercises_public_tool_and_executor_paths() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            file_counts=(8,),
            file_bytes=512,
            iterations=1,
        )
    )

    assert len(report.samples) == 12
    assert {sample.layer for sample in report.samples} == {"tool.run", "executor"}
    assert {sample.scenario for sample in report.samples} == {
        "read_first_page",
        "read_tail_page",
        "ls_sorted_prefix",
        "find_sorted_prefix",
        "grep_literal_miss",
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
        ({"max_output_lines": 99}, "max_output_lines must be at least 100"),
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
            file_counts=(101,),
            file_bytes=512,
            max_output_lines=100,
            iterations=1,
        )
    )

    listing = [sample for sample in report.samples if sample.scenario == "ls_sorted_prefix"]
    assert len(listing) == 2
    assert all(sample.truncated for sample in listing)
