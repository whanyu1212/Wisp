from __future__ import annotations

import anyio
import pytest

from benchmarks.tui_long_session import ScenarioConfig, run_scenario
from wisp.tui.transcript_window import TUI_TRANSCRIPT_WINDOW_SIZE

pytestmark = pytest.mark.benchmark


def test_tui_long_session_scenario_reports_complete_history_hydration() -> None:
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
    assert report.complete_history_convert_ms >= 0
    assert report.complete_history_mount_ms >= 0
    assert report.persisted_message_count == 12
    assert report.represented_row_count == 12
    assert report.hydrated_entry_count == 10
    assert report.mounted_widget_count < report.represented_row_count
    assert report.retained_entry_count == report.hydrated_entry_count
    assert report.persisted_rows_per_widget > 1
    # These broad limits catch a renewed animation/layout backlog without binding
    # the benchmark to one machine's absolute Textual timings.
    assert 0 <= report.first_wheel_up_ms < 1_000
    assert report.first_wheel_up_rows > 0
    assert report.first_wheel_up_attempts in {1, 2}
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
