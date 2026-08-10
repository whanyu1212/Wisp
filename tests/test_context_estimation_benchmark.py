from __future__ import annotations

import pytest

from benchmarks.context_estimation import BenchmarkConfig, run_benchmark

pytestmark = pytest.mark.benchmark


def test_context_estimation_benchmark_reports_each_production_operation() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            message_counts=(3, 7),
            content_bytes=8,
            tool_count=1,
            iterations=1,
            track_memory=True,
        )
    )

    assert [sample.message_count for sample in report.samples] == [3, 7]
    assert all(sample.approximate_input_bytes > 0 for sample in report.samples)
    assert all(sample.estimated_tokens > 0 for sample in report.samples)
    assert all(sample.estimate.peak_memory_bytes is not None for sample in report.samples)
    assert all(sample.fingerprint.wall_ms >= 0 for sample in report.samples)
    assert all(sample.estimate_and_fingerprint.cpu_ms >= 0 for sample in report.samples)
    assert '"estimate_and_fingerprint"' in report.to_json()
