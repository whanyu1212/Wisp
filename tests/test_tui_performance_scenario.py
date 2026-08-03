from __future__ import annotations

import anyio

from benchmarks.tui_long_session import ScenarioConfig, run_scenario


def test_tui_long_session_scenario_reports_current_widget_growth() -> None:
    report = anyio.run(
        run_scenario,
        ScenarioConfig(
            message_count=12,
            page_size=4,
            stream_chunks=2,
        ),
    )

    assert report.session_entry_count == 12
    assert report.session_size_bytes > 0
    assert len(report.older_page_read_ms) == 2
    assert len(report.prepend_render_ms) == 2
    assert report.mounted_widget_counts == tuple(sorted(report.mounted_widget_counts))
    assert report.mounted_widget_counts[0] < report.mounted_widget_counts[-1]
    assert report.stream_following_tail_ms >= 0
    assert report.stream_scrolled_back_ms >= 0
    assert not report.final_following
    assert report.final_unseen_output_count == 1
    assert report.process_state in {"cancelled", "completed"}
    assert '"session_entry_count": 12' in report.to_json()
