from __future__ import annotations

import json
from functools import partial

import anyio
import pytest

from benchmarks import tui_stream_renderers
from benchmarks.tui_long_session import ScenarioConfig
from benchmarks.tui_stream_renderers import run_comparison
from wisp.tui.widgets import StreamMessage

pytestmark = pytest.mark.benchmark


def test_stream_renderer_comparison_rotates_modes_and_restores_plain_patch() -> None:
    original_render_source = StreamMessage._render_source

    report = anyio.run(
        partial(
            run_comparison,
            ScenarioConfig(
                message_count=4,
                page_size=2,
                stream_chunks=1,
                stream_interval_seconds=0.001,
            ),
            runs=2,
        )
    )

    assert StreamMessage._render_source is original_render_source
    assert report["runs_per_mode"] == 2
    samples = report["samples"]
    assert isinstance(samples, list)
    assert [(sample["run"], sample["mode"]) for sample in samples] == [
        (1, "rich"),
        (1, "plain"),
        (2, "plain"),
        (2, "rich"),
    ]
    assert all(sample["stream_update_count"] >= 1 for sample in samples)
    assert all(sample["final_unseen_output_count"] == 1 for sample in samples)
    assert set(report["summary"]) == {
        "stream_following_tail_ms",
        "stream_max_event_loop_stall_ms",
        "stream_page_up_ms",
        "stream_scrolled_back_ms",
    }
    assert json.loads(json.dumps(report))["runs_per_mode"] == 2


def test_stream_renderer_comparison_restores_plain_patch_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_render_source = StreamMessage._render_source

    async def fail_scenario(_config: ScenarioConfig) -> None:
        assert StreamMessage._render_source is not original_render_source
        raise RuntimeError("plain renderer failed")

    monkeypatch.setattr(tui_stream_renderers, "run_scenario", fail_scenario)

    with pytest.raises(RuntimeError, match="plain renderer failed"):
        anyio.run(
            tui_stream_renderers._run_plain_scenario,
            ScenarioConfig(
                message_count=4,
                page_size=2,
                stream_chunks=1,
                stream_interval_seconds=0.001,
            ),
        )

    assert StreamMessage._render_source is original_render_source
