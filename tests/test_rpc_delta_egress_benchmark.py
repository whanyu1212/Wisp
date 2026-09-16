from __future__ import annotations

import anyio
import pytest

from benchmarks.rpc_delta_egress import BenchmarkConfig, run_benchmark

pytestmark = pytest.mark.benchmark


def test_rpc_delta_egress_benchmark_preserves_text_and_reduces_frames() -> None:
    report = anyio.run(
        run_benchmark,
        BenchmarkConfig(response_bytes=256, chunk_bytes=1, iterations=1),
    )

    raw, coalesced = report.samples
    assert raw.frames == 256
    assert coalesced.frames == 1
    assert raw.content_matches and coalesced.content_matches
    assert raw.wire_bytes > coalesced.wire_bytes
    assert raw.egress_and_parse.wall_ms > 0
    assert coalesced.egress_and_parse.cpu_ms > 0
