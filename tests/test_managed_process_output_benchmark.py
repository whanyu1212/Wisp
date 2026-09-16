from __future__ import annotations

import pytest

from benchmarks.managed_process_output import BenchmarkConfig, run_benchmark

pytestmark = [pytest.mark.benchmark, pytest.mark.process]


def test_managed_process_output_benchmark_preserves_source_byte_accounting() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            sample_sizes=(64 * 1024,),
            workloads=("ascii_lines", "invalid_utf8"),
            iterations=1,
        )
    )

    assert {sample.workload for sample in report.samples} == {"ascii_lines", "invalid_utf8"}
    assert all(sample.input_bytes == 64 * 1024 for sample in report.samples)
    assert all(sample.poll_count > 0 for sample in report.samples)
    assert all(sample.max_poll_ms >= 0 for sample in report.samples)
    assert all(sample.throughput_bytes_per_second > 0 for sample in report.samples)
    assert all(
        sample.retained_source_bytes + sample.dropped_bytes == sample.input_bytes
        for sample in report.samples
    )
    assert '"max_poll_ms"' in report.to_json()
