from __future__ import annotations

import anyio
import pytest

from benchmarks.session_loading import BenchmarkConfig, run_benchmark

pytestmark = pytest.mark.benchmark


def test_session_loading_benchmark_separates_cold_warm_and_replay_paths() -> None:
    report = anyio.run(
        run_benchmark,
        BenchmarkConfig(entry_counts=(12,), page_size=4, iterations=1),
    )

    sample = report.samples[0]
    assert sample.entry_count == 12
    assert sample.session_bytes > 0
    assert sample.cold_newest_page.wall_ms >= 0
    assert sample.warm_newest_page.wall_ms >= 0
    assert sample.complete_parse.cpu_ms >= 0
    assert sample.context_replay.wall_ms >= 0
    assert sample.older_page is not None
    assert sample.indexed_append.wall_ms >= 0
    assert '"cold_newest_page"' in report.to_json()
