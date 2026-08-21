from __future__ import annotations

import gc
import pstats
from functools import partial
from pathlib import Path

import anyio
import pytest
from rich.theme import Theme
from textual.screen import Screen
from textual.widget import Widget

from benchmarks.tui_stream_hotpaths import (
    BenchmarkConfig,
    TimingDistribution,
    _HotpathCollector,
    run_benchmark,
)
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.widgets import (
    StreamMessage,
    _AssistantMarkdown,
    _SafeAssistantMarkdown,
)

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


def test_hotpath_collector_does_not_retain_superseded_markdown() -> None:
    collector = _HotpathCollector(
        target_screen=Screen(),
        settled_stream_messages=set(),
    )
    markdown = _SafeAssistantMarkdown(
        "benchmark",
        _AssistantMarkdown(
            "benchmark",
            theme=Theme(),
            code_theme="monokai",
            native_ansi=False,
        ),
    )
    collector.markdown_owners[markdown] = StreamMessage()

    assert len(collector.markdown_owners) == 1

    del markdown
    gc.collect()

    assert len(collector.markdown_owners) == 0


def test_tui_stream_hotpaths_reports_real_stream_and_restores_textual_methods() -> None:
    original_layout = Screen._refresh_layout
    original_compositor = Screen._compositor_refresh
    original_refresh = Widget.refresh
    original_content_height = Widget.get_content_height
    original_markdown_render = _SafeAssistantMarkdown.__rich_console__
    original_source_render = StreamMessage._render_source

    report = anyio.run(
        run_benchmark,
        BenchmarkConfig(
            message_count=8,
            retained_history_entries=(4,),
            stream_chunks=4,
            stream_interval_seconds=0.08,
            heartbeat_interval_seconds=0.001,
            viewport_width=80,
            viewport_height=12,
            runs=1,
        ),
    )

    assert Screen._refresh_layout is original_layout
    assert Screen._compositor_refresh is original_compositor
    assert Widget.refresh is original_refresh
    assert Widget.get_content_height is original_content_height
    assert _SafeAssistantMarkdown.__rich_console__ is original_markdown_render
    assert StreamMessage._render_source is original_source_render
    assert len(report.samples) == 1
    sample = report.samples[0]
    assert sample.run == 1
    assert sample.retained_history_entries == 4
    assert sample.mounted_history_entries == 4
    assert sample.mounted_widget_count == 6 + report.config.pending_tool_cards
    assert sample.stream_total_ms >= 0
    assert sample.stream_cpu_ms >= 0
    assert (
        report.config.stream_chunks
        <= sample.stream_update_count
        <= (report.config.stream_chunks + 1)
    )
    assert sample.layout_request_count == sum(sample.layout_requests.values())
    assert sample.layout_request_count > 0
    assert sample.layout_requests["StreamMessage"] <= sample.stream_update_count + 1
    assert sample.layout_passes_per_stream_update == (
        sample.layout_passes.sample_count / sample.stream_update_count
    )
    assert sample.layout_passes.sample_count <= sample.stream_update_count + 2
    assert sample.content_height_call_count == sum(sample.content_height_calls.values())
    assert sample.content_height_call_count > 0
    assert sample.content_height_calls["StreamMessage"] > 0
    assert sample.markdown_renders.total == (
        sample.markdown_renders.active + sample.markdown_renders.settled
    )
    assert sample.markdown_renders.active > 0
    assert sample.markdown_renders.active < sample.markdown_source_rebuild_count * 2
    assert sample.markdown_source_rebuild_count >= sample.stream_update_count
    assert sample.markdown_source_chars_processed >= sample.markdown_source_rebuild_count
    assert sample.markdown_drains.sample_count == sample.markdown_drain_success_count
    assert sample.markdown_drain_failure_count == 0
    assert sample.markdown_drains.sample_count >= 1
    assert sample.displayed_frame_count >= 1
    assert sample.input_chop_spans >= sample.emitted_chop_spans
    assert sample.input_chop_spans == (sample.emitted_chop_spans + sample.suppressed_chop_spans)
    assert sample.display_frame_fail_open_count >= 0
    assert sample.history_prepend_suppressed_update_count == 0
    assert sample.history_prepend_escaped_update_count == 0
    assert sample.layout_passes_per_displayed_frame == (
        sample.layout_passes.sample_count / sample.displayed_frame_count
    )
    assert sample.event_loop_delay.sample_count >= 1
    assert sample.layout_passes.sample_count >= 1
    assert sample.compositor_renders.sample_count >= 1
    assert not sample.working_indicator_active
    assert sample.final_following
    assert sample.final_at_tail
    assert sample.source_complete
    assert report.summaries[0].retained_history_entries == 4
    assert report.summaries[0].mounted_history_entries == 4
    summary = report.summaries[0]
    assert summary.sample_count == 1
    assert summary.stream_cpu_median_ms == sample.stream_cpu_ms
    assert summary.layout_request_count_median == sample.layout_request_count
    assert summary.layout_passes_per_stream_update_median == sample.layout_passes_per_stream_update
    assert summary.content_height_call_count_median == sample.content_height_call_count
    assert summary.active_markdown_render_median == sample.markdown_renders.active
    assert summary.settled_markdown_render_median == sample.markdown_renders.settled
    assert summary.markdown_source_chars_processed_median == sample.markdown_source_chars_processed
    assert summary.markdown_drain_p95_median_ms == sample.markdown_drains.p95_ms
    assert summary.displayed_frame_count_median == sample.displayed_frame_count
    assert summary.suppressed_chop_spans_median == sample.suppressed_chop_spans
    assert summary.display_frame_fail_open_count_median == sample.display_frame_fail_open_count
    assert (
        summary.history_prepend_escaped_update_count_median
        == sample.history_prepend_escaped_update_count
    )
    assert '"retained_history_entries": 4' in report.to_json()
    assert '"mounted_history_entries": 4' in report.to_json()
    assert '"stream_cpu_ms":' in report.to_json()
    assert '"layout_requests":' in report.to_json()
    assert '"layout_passes_per_stream_update":' in report.to_json()
    assert '"markdown_renders":' in report.to_json()
    assert '"markdown_source_chars_processed":' in report.to_json()
    assert '"markdown_drains":' in report.to_json()
    assert '"display_updates":' in report.to_json()
    assert '"display_frame_fail_open_count":' in report.to_json()


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
    original_refresh = Widget.refresh
    original_content_height = Widget.get_content_height
    original_markdown_render = _SafeAssistantMarkdown.__rich_console__
    original_source_render = StreamMessage._render_source

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
    assert Widget.refresh is original_refresh
    assert Widget.get_content_height is original_content_height
    assert _SafeAssistantMarkdown.__rich_console__ is original_markdown_render
    assert StreamMessage._render_source is original_source_render
