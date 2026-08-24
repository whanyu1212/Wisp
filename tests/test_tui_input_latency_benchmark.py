from __future__ import annotations

import anyio
import pytest

from benchmarks.tui_input_latency import BenchmarkConfig, _summarize, run_benchmark
from wisp.tui.textual_renderer import TextualTuiRenderer

pytestmark = pytest.mark.benchmark


def test_tui_input_latency_reports_idle_and_streaming_stage_distributions() -> None:
    report = anyio.run(
        run_benchmark,
        BenchmarkConfig(
            runs=1,
            stream_chunks=20,
            stream_interval_seconds=0.01,
            action_interval_seconds=0.01,
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
    assert report.summaries
    for sample in report.samples:
        assert sample.handler_ms >= 0
        assert sample.queued_ms >= 0
        assert sample.display_ms >= 0
        assert sample.total_ms >= sample.handler_ms + sample.queued_ms + sample.display_ms
        assert sample.display_kind in {"layout", "chops", "other"}
    assert '"condition": "streaming"' in report.to_json()
    assert '"queued_ms":' in report.to_json()


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
            viewport_width=80,
            viewport_height=16,
        ),
    )

    assert submitted_modes
    assert set(submitted_modes) == {"idle", "running"}
    assert submitted_modes == sorted(submitted_modes, key=("idle", "running").index)


def test_tui_input_latency_summary_omits_categories_without_samples() -> None:
    assert _summarize(()) == ()


@pytest.mark.parametrize(
    "config",
    [
        BenchmarkConfig(runs=0),
        BenchmarkConfig(stream_chunks=0),
        BenchmarkConfig(stream_interval_seconds=0),
        BenchmarkConfig(action_interval_seconds=0),
        BenchmarkConfig(viewport_width=0),
        BenchmarkConfig(viewport_height=0),
    ],
)
def test_tui_input_latency_rejects_invalid_config(config: BenchmarkConfig) -> None:
    with pytest.raises(ValueError):
        anyio.run(run_benchmark, config)
