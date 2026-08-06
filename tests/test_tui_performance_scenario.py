from __future__ import annotations

import anyio
import pytest

from benchmarks.tui_long_session import ScenarioConfig, run_scenario
from wisp.tui.transcript_window import (
    TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT,
    TUI_TRANSCRIPT_WINDOW_SIZE,
)

pytestmark = pytest.mark.benchmark


def test_tui_long_session_scenario_reports_bounded_widget_growth() -> None:
    report = anyio.run(
        run_scenario,
        ScenarioConfig(
            message_count=12,
            page_size=4,
            stream_chunks=2,
            stream_interval_seconds=0.001,
        ),
    )

    assert report.session_entry_count == 12
    assert report.session_size_bytes > 0
    assert report.newest_page_read_ms >= 0
    assert report.warm_newest_page_read_ms >= 0
    assert len(report.older_page_read_ms) == 2
    assert len(report.prepend_render_ms) == 2
    assert max(report.mounted_widget_counts) <= TUI_TRANSCRIPT_WINDOW_SIZE + 1
    assert max(report.retained_entry_counts) <= TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT
    # These broad limits catch a renewed animation/layout backlog without binding
    # the benchmark to one machine's absolute Textual timings.
    assert 0 <= report.idle_page_up_ms < 1_000
    assert report.stream_following_tail_ms >= 0
    assert 0 <= report.stream_page_up_ms < 1_000
    assert report.stream_scrolled_back_ms >= 0
    assert 0 <= report.stream_max_event_loop_stall_ms < 1_000
    assert 1 <= report.stream_markdown_writes <= report.config.stream_chunks
    assert report.settled_live_widget_count <= TUI_TRANSCRIPT_WINDOW_SIZE
    assert not report.final_following
    assert report.final_unseen_output_count == 1
    assert report.process_state in {"cancelled", "completed"}
    assert '"session_entry_count": 12' in report.to_json()
