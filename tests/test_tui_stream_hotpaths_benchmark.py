from __future__ import annotations

import pstats
from functools import partial
from pathlib import Path

import anyio
import pytest
from textual.screen import Screen

from benchmarks.tui_stream_hotpaths import BenchmarkConfig, TimingDistribution, run_benchmark
from wisp.tui.textual_renderer import TextualTuiRenderer

pytestmark = pytest.mark.benchmark


def test_timing_distribution_handles_empty_and_nearest_rank_samples() -> None:
    assert TimingDistribution.from_samples(()) == TimingDistribution(0, 0, 0, 0, 0, 0)
    assert TimingDistribution.from_samples((4.0,)) == TimingDistribution(1, 4, 4, 4, 4, 4)

    distribution = TimingDistribution.from_samples(tuple(float(value) for value in range(1, 21)))

    assert distribution.sample_count == 20
    assert distribution.total_ms == 210
    assert distribution.p50_ms == 10
    assert distribution.p95_ms == 19
    assert distribution.p99_ms == 20
    assert distribution.max_ms == 20


def test_tui_stream_hotpaths_reports_real_stream_and_restores_textual_methods() -> None:
    original_layout = Screen._refresh_layout
    original_compositor = Screen._compositor_refresh

    report = anyio.run(
        run_benchmark,
        BenchmarkConfig(
            message_count=8,
            retained_history_entries=(4,),
            stream_chunks=2,
            stream_interval_seconds=0.001,
            heartbeat_interval_seconds=0.001,
            viewport_width=80,
            viewport_height=12,
            runs=1,
        ),
    )

    assert Screen._refresh_layout is original_layout
    assert Screen._compositor_refresh is original_compositor
    assert len(report.samples) == 1
    sample = report.samples[0]
    assert sample.run == 1
    assert sample.retained_history_entries == 4
    assert sample.mounted_history_entries == 4
    assert sample.mounted_widget_count == 6
    assert sample.stream_total_ms >= 0
    assert 1 <= sample.stream_update_count <= report.config.stream_chunks + 1
    assert sample.event_loop_delay.sample_count >= 1
    assert sample.layout_passes.sample_count >= 1
    assert sample.compositor_renders.sample_count >= 1
    assert sample.working_indicator_active
    assert sample.final_following
    assert sample.final_at_tail
    assert sample.source_complete
    assert report.summaries[0].retained_history_entries == 4
    assert report.summaries[0].mounted_history_entries == 4
    assert report.summaries[0].sample_count == 1
    assert '"retained_history_entries": 4' in report.to_json()
    assert '"mounted_history_entries": 4' in report.to_json()


def test_tui_stream_hotpaths_profile_is_readable_and_rejects_matrix(tmp_path: Path) -> None:
    profile_path = tmp_path / "stream.prof"
    config = BenchmarkConfig(
        message_count=4,
        retained_history_entries=(2,),
        stream_chunks=1,
        stream_interval_seconds=0.001,
        heartbeat_interval_seconds=0.001,
        viewport_width=80,
        viewport_height=12,
        runs=1,
    )

    report = anyio.run(partial(run_benchmark, config, profile_output=profile_path))

    assert report.samples[0].source_complete
    assert profile_path.stat().st_size > 0
    assert pstats.Stats(str(profile_path)).total_calls > 0

    with pytest.raises(ValueError, match="exactly one run and one retained-history value"):
        anyio.run(
            partial(
                run_benchmark,
                BenchmarkConfig(
                    message_count=4,
                    retained_history_entries=(2, 3),
                    stream_chunks=1,
                    stream_interval_seconds=0.001,
                    heartbeat_interval_seconds=0.001,
                    runs=1,
                ),
                profile_output=profile_path,
            )
        )


def test_tui_stream_hotpaths_restores_textual_methods_after_stream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_layout = Screen._refresh_layout
    original_compositor = Screen._compositor_refresh

    def fail_stream(_renderer: TextualTuiRenderer, _delta: str) -> None:
        raise RuntimeError("benchmark stream failed")

    monkeypatch.setattr(TextualTuiRenderer, "token_delta", fail_stream)

    with pytest.raises(RuntimeError, match="benchmark stream failed"):
        anyio.run(
            run_benchmark,
            BenchmarkConfig(
                message_count=4,
                retained_history_entries=(2,),
                stream_chunks=1,
                stream_interval_seconds=0.001,
                heartbeat_interval_seconds=0.001,
                viewport_width=80,
                viewport_height=12,
                runs=1,
            ),
        )

    assert Screen._refresh_layout is original_layout
    assert Screen._compositor_refresh is original_compositor
