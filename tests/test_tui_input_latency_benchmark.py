from __future__ import annotations

import asyncio
from collections import Counter

import anyio
import pytest
from rich.console import RenderableType
from textual import events

from benchmarks.tui_input_latency import (
    BenchmarkConfig,
    _InputCollector,
    _reader_remained_parked,
    _summarize,
    _validate_condition_samples,
    run_benchmark,
)
from wisp.tui.diagnostics import InputEventCategory, InputLatencyDiagnostic
from wisp.tui.textual_app import TextualTui
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.widgets import DecisionPanel

pytestmark = pytest.mark.benchmark


def test_tui_input_latency_reports_idle_and_streaming_stage_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_samples_per_frame: list[int] = []
    original_record = TextualTui._record_input_latency_diagnostics

    def record_input_latency_diagnostics(
        app: TextualTui,
        renderable: RenderableType,
        display_started: float,
        displayed_at: float,
    ) -> None:
        cursor_samples_per_frame.append(
            sum(sample.category == "cursor" for sample in app._pending_input_latency)
        )
        original_record(app, renderable, display_started, displayed_at)

    monkeypatch.setattr(
        TextualTui,
        "_record_input_latency_diagnostics",
        record_input_latency_diagnostics,
    )
    report = anyio.run(
        run_benchmark,
        BenchmarkConfig(
            runs=1,
            stream_chunks=20,
            stream_interval_seconds=0.01,
            action_interval_seconds=0.01,
            gesture_repetitions=2,
            viewport_width=80,
            viewport_height=16,
        ),
    )

    assert {sample.condition for sample in report.samples} == {"idle", "streaming"}
    categories = {sample.category for sample in report.samples}
    assert {
        "typing",
        "cursor",
        "navigation",
        "wheel",
        "submission",
        "approval",
        "cancellation",
    } <= categories
    for condition in ("idle", "streaming"):
        counts = Counter(
            sample.category for sample in report.samples if sample.condition == condition
        )
        assert counts["cursor"] == 4
        assert counts["navigation"] == 2
        assert counts["wheel"] == 2
        assert counts["typing"] == 7
        assert counts["submission"] == 1
        assert counts["approval"] == 1
        assert counts["cancellation"] == 1
    assert cursor_samples_per_frame
    assert max(cursor_samples_per_frame) == 1
    assert sum(cursor_samples_per_frame) == 8
    assert report.summaries
    for sample in report.samples:
        assert sample.handler_ms >= 0
        assert sample.queued_ms >= 0
        assert sample.display_ms >= 0
        assert sample.total_ms >= sample.handler_ms + sample.queued_ms + sample.display_ms
        assert sample.display_kind in {"layout", "chops", "other"}
    assert len(report.streaming_runs) == 1
    streaming = report.streaming_runs[0]
    assert streaming.run == 1
    assert streaming.stream_total_ms >= streaming.stream_flush_ms >= 0
    assert streaming.produced_chunk_count >= report.config.stream_chunks
    assert streaming.expected_source_chars > 0
    assert streaming.rendered_source_chars == streaming.expected_source_chars
    assert 1 <= streaming.stream_write_count <= streaming.produced_chunk_count
    assert streaming.source_complete
    assert streaming.reader_remained_parked
    assert streaming.final_following
    assert streaming.final_at_tail
    assert '"condition": "streaming"' in report.to_json()
    assert '"queued_ms":' in report.to_json()
    assert '"stream_flush_ms":' in report.to_json()
    assert '"source_complete": true' in report.to_json()
    assert '"gesture_repetitions": 2' in report.to_json()


def test_input_collector_waits_for_the_next_category_sample() -> None:
    async def exercise() -> None:
        collector = _InputCollector()
        previous_count = collector.sample_count("cursor")
        waiter = asyncio.create_task(
            collector.wait_for_next(
                "cursor",
                previous_count=previous_count,
                action="left",
            )
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        collector.record_input_latency(
            InputLatencyDiagnostic(
                category="cursor",
                handler_seconds=0.001,
                queued_seconds=0.002,
                display_seconds=0.003,
                total_seconds=0.006,
                display_kind="chops",
            )
        )
        await waiter

    anyio.run(exercise)


def test_tui_input_latency_rejects_unexpected_diagnostic_counts() -> None:
    categories: tuple[InputEventCategory, ...] = (
        *("typing" for _ in "latency"),
        "cursor",
        "cursor",
        "navigation",
        "wheel",
        "submission",
        "approval",
        "cancellation",
    )
    samples = tuple(
        InputLatencyDiagnostic(
            category=category,
            handler_seconds=0.001,
            queued_seconds=0.002,
            display_seconds=0.003,
            total_seconds=0.006,
            display_kind="chops",
        )
        for category in categories
    )

    _validate_condition_samples(samples, BenchmarkConfig(gesture_repetitions=1))
    with pytest.raises(RuntimeError, match="unexpected input diagnostic counts"):
        _validate_condition_samples(
            (*samples, samples[7]),
            BenchmarkConfig(gesture_repetitions=1),
        )


def test_tui_input_latency_submits_in_the_condition_input_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted_modes: list[str] = []
    original_capture = TextualTuiRenderer._capture_submitted_input_mode

    def capture_mode(renderer: TextualTuiRenderer) -> str:
        mode = original_capture(renderer)
        submitted_modes.append(mode)
        return mode

    monkeypatch.setattr(TextualTuiRenderer, "_capture_submitted_input_mode", capture_mode)

    anyio.run(
        run_benchmark,
        BenchmarkConfig(
            runs=1,
            stream_chunks=20,
            stream_interval_seconds=0.01,
            action_interval_seconds=0.01,
            gesture_repetitions=1,
            viewport_width=80,
            viewport_height=16,
        ),
    )

    assert submitted_modes
    assert set(submitted_modes) == {"idle", "running"}
    assert submitted_modes == sorted(submitted_modes, key=("idle", "running").index)


def test_tui_input_latency_keeps_streaming_and_closes_approval_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed_chunks = 0
    cancellation_panel_states: list[bool] = []
    cancellation_after_escape: list[bool] = []
    escape_handled = False
    original_token_delta = TextualTuiRenderer.token_delta
    original_cancelling = TextualTuiRenderer.cancelling
    original_on_event = TextualTui.on_event

    def record_token_delta(renderer: TextualTuiRenderer, text: str) -> None:
        nonlocal streamed_chunks
        streamed_chunks += 1
        original_token_delta(renderer, text)

    def record_cancelling(renderer: TextualTuiRenderer, message: str) -> None:
        nonlocal escape_handled
        cancellation_after_escape.append(escape_handled)
        escape_handled = False
        original_cancelling(renderer, message)

    async def record_event(app: TextualTui, event: events.Event) -> None:
        nonlocal escape_handled
        is_escape = isinstance(event, events.Key) and event.key == "escape"
        if is_escape:
            panel = app.query_one("#decision-panel", DecisionPanel)
            cancellation_panel_states.append(panel.display)
        await original_on_event(app, event)
        if is_escape:
            escape_handled = True

    monkeypatch.setattr(TextualTuiRenderer, "token_delta", record_token_delta)
    monkeypatch.setattr(TextualTuiRenderer, "cancelling", record_cancelling)
    monkeypatch.setattr(TextualTui, "on_event", record_event)

    anyio.run(
        run_benchmark,
        BenchmarkConfig(
            runs=1,
            stream_chunks=1,
            stream_interval_seconds=0.01,
            action_interval_seconds=0.01,
            gesture_repetitions=1,
            viewport_width=80,
            viewport_height=16,
        ),
    )

    assert streamed_chunks > 1
    assert cancellation_panel_states
    assert not any(cancellation_panel_states)
    assert cancellation_after_escape == [True, True]


def test_tui_input_latency_summary_omits_categories_without_samples() -> None:
    assert _summarize(()) == ()


@pytest.mark.parametrize(
    ("page_up_following", "wheel_following", "expected"),
    [
        (False, False, True),
        (True, False, False),
        (False, True, False),
        (True, True, False),
    ],
)
def test_tui_input_latency_requires_both_navigation_gestures_to_remain_parked(
    page_up_following: bool,
    wheel_following: bool,
    expected: bool,
) -> None:
    assert (
        _reader_remained_parked(
            page_up_following=page_up_following,
            wheel_following=wheel_following,
        )
        is expected
    )


@pytest.mark.parametrize(
    "config",
    [
        BenchmarkConfig(runs=0),
        BenchmarkConfig(stream_chunks=0),
        BenchmarkConfig(stream_interval_seconds=0),
        BenchmarkConfig(action_interval_seconds=0),
        BenchmarkConfig(gesture_repetitions=0),
        BenchmarkConfig(viewport_width=0),
        BenchmarkConfig(viewport_height=0),
    ],
)
def test_tui_input_latency_rejects_invalid_config(config: BenchmarkConfig) -> None:
    with pytest.raises(ValueError):
        anyio.run(run_benchmark, config)
