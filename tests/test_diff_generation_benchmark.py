from __future__ import annotations

import pytest

from benchmarks.diff_generation import DEFAULT_WORKLOADS, BenchmarkConfig, run_benchmark

pytestmark = pytest.mark.benchmark


def test_diff_generation_benchmark_uses_bounded_structured_presentations() -> None:
    report = run_benchmark(
        BenchmarkConfig(line_counts=(20,), workloads=DEFAULT_WORKLOADS, iterations=1)
    )

    assert {sample.workload for sample in report.samples} == set(DEFAULT_WORKLOADS)
    assert all(sample.input_bytes > 0 for sample in report.samples)
    assert all(not sample.guarded for sample in report.samples)
    assert all(sample.retained_rows > 0 for sample in report.samples)
    assert all(sample.additions > 0 for sample in report.samples)
    assert all(sample.deletions > 0 for sample in report.samples)
    assert all(sample.generation.wall_ms >= 0 for sample in report.samples)
    assert '"retained_rows"' in report.to_json()


def test_diff_generation_benchmark_reports_the_production_input_guard() -> None:
    report = run_benchmark(
        BenchmarkConfig(line_counts=(10_000,), workloads=("localized",), iterations=1)
    )

    sample = report.samples[0]
    assert sample.guarded
    assert sample.retained_rows == 0
    assert sample.additions == 0
    assert sample.deletions == 0
