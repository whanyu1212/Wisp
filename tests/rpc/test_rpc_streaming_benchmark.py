from __future__ import annotations

import pytest

from benchmarks.rpc_streaming import BenchmarkConfig, run_benchmark

pytestmark = pytest.mark.benchmark


def test_rpc_streaming_benchmark_exposes_delta_size_cost() -> None:
    report = run_benchmark(BenchmarkConfig(response_bytes=64, chunk_sizes=(1, 16), iterations=1))

    tiny, batched = report.samples
    assert tiny.event_count == 64
    assert batched.event_count == 4
    assert tiny.encoded_bytes > batched.encoded_bytes
    assert all(sample.construction.wall_ms >= 0 for sample in report.samples)
    assert all(sample.serialization.cpu_ms >= 0 for sample in report.samples)
    assert all(sample.parsing.wall_ms >= 0 for sample in report.samples)
    assert all(sample.complete_round_trip.wall_ms >= 0 for sample in report.samples)
    assert all(sample.round_trips_per_second > 0 for sample in report.samples)
    assert '"complete_round_trip"' in report.to_json()
