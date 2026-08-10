from __future__ import annotations

import pytest

from benchmarks.process_output import BenchmarkConfig, run_benchmark
from wisp.tools.process_manager import DEFAULT_MAX_RETAINED_BYTES, DEFAULT_MAX_RETAINED_LINES

pytestmark = pytest.mark.benchmark


def test_process_output_benchmark_reports_production_tail_accounting() -> None:
    report = run_benchmark(
        BenchmarkConfig(
            sample_sizes=(64 * 1024,),
            max_retained_bytes=DEFAULT_MAX_RETAINED_BYTES,
            max_retained_lines=DEFAULT_MAX_RETAINED_LINES,
        )
    )

    assert {sample.workload for sample in report.samples} == set(report.config.workloads)
    assert all(sample.input_bytes == 64 * 1024 for sample in report.samples)
    assert all(sample.chunk_count > 0 for sample in report.samples)
    assert all(sample.measurement.wall_ms >= 0 for sample in report.samples)
    assert all(sample.measurement.cpu_ms >= 0 for sample in report.samples)
    assert all(sample.max_chunk_ms >= 0 for sample in report.samples)
    assert all(sample.throughput_bytes_per_second > 0 for sample in report.samples)
    assert all(sample.retained_bytes <= DEFAULT_MAX_RETAINED_BYTES for sample in report.samples)
    assert all(sample.dropped_bytes > 0 for sample in report.samples)
    assert all(
        sample.dropped_bytes + sample.retained_bytes == sample.input_bytes
        for sample in report.samples
    )
    assert '"throughput_bytes_per_second"' in report.to_json()
