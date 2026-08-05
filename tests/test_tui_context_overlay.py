from __future__ import annotations

from decimal import Decimal

import anyio
import pytest
from textual.content import Content
from textual.widgets import Button, Label, ProgressBar, Static

from wisp.events import (
    CompactionPolicyStatus,
    ContextBudget,
    ContextEstimate,
    SessionCostSummary,
    SessionStats,
    TokenUsage,
    ToolApprovalRequested,
)
from wisp.tui.context_widget import ContextStatusOverlay, context_status_presentation
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import DecisionPanel, PromptEditor, Transcript

_DEFAULT_POLICY = CompactionPolicyStatus(
    threshold_eligible=True,
    threshold_ineligible_reason=None,
)
_DEFAULT_COST = SessionCostSummary(known_usd=Decimal("0.42"))


def _stats(
    *,
    estimated: int = 80_000,
    observed: int | None = 92_000,
    current: bool = True,
    window: int | None = 128_000,
    reserve: int = 8_000,
    remaining: int | None = 28_000,
    over_budget: bool | None = False,
    policy: CompactionPolicyStatus | None = _DEFAULT_POLICY,
    cost: SessionCostSummary = _DEFAULT_COST,
) -> SessionStats:
    return SessionStats(
        session_id="session-1",
        entry_count=4,
        active_message_count=4,
        compaction_count=0,
        usage_record_count=1,
        usage=TokenUsage(input_tokens=90_000, output_tokens=2_000, total_tokens=92_000),
        context=ContextBudget(
            estimate=ContextEstimate(
                system_tokens=1_000,
                message_tokens=estimated - 2_000,
                tool_schema_tokens=1_000,
                total_tokens=estimated,
            ),
            observed_tokens=observed,
            observed_is_current=current,
            context_window=window,
            reserve_tokens=reserve,
            remaining_tokens=remaining,
            estimated_percent=estimated / window * 100 if window is not None else None,
            over_budget=over_budget,
        ),
        compaction=policy,
        cost=cost,
    )


def _plain(widget: Label | Static) -> str:
    content = widget.render()
    assert isinstance(content, Content)
    return content.plain


def test_context_presentation_prefers_current_provider_observation() -> None:
    view = context_status_presentation(_stats())

    assert view.current_tokens == 92_000
    assert view.percentage == pytest.approx(71.875)
    assert view.source == "provider observation"
    assert view.reserve == "8k"
    assert view.remaining == "28k"
    assert view.trigger == ">120k"
    assert view.compaction == "on"
    assert view.eligibility == "eligible"
    assert view.overflow_recovery == "on"
    assert view.cost == "$0.420"


def test_context_presentation_recomputes_remaining_from_displayed_observation() -> None:
    view = context_status_presentation(_stats(observed=92_000, remaining=40_000))

    assert view.current_tokens == 92_000
    assert view.remaining == "28k"


def test_context_presentation_preserves_meaningful_small_costs() -> None:
    view = context_status_presentation(_stats(cost=SessionCostSummary(known_usd=Decimal("0.0042"))))

    assert view.cost == "$0.0042"


def test_context_presentation_marks_estimates_and_partial_cost() -> None:
    view = context_status_presentation(
        _stats(
            observed=91_000,
            current=False,
            cost=SessionCostSummary(
                known_usd=Decimal("0.10"),
                complete=False,
                priced_record_count=1,
                unpriced_record_count=1,
            ),
        )
    )

    assert view.current_tokens == 80_000
    assert view.source == "deterministic estimate (approximate)"
    assert view.cost == "$0.100 known · partial pricing"


def test_context_overlay_renders_progress_and_authoritative_policy() -> None:
    async def scenario() -> tuple[float | None, bool, list[str], bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.context_status(_stats())
            await pilot.pause()
            overlay = app.query_one("#context-status", ContextStatusOverlay)
            progress = app.query_one("#context-status-progress", ProgressBar)
            rows = [
                _plain(app.query_one(selector, Label))
                for selector in (
                    "#context-status-usage",
                    "#context-status-source",
                    "#context-status-detail",
                    "#context-status-policy",
                    "#context-status-eligibility",
                    "#context-status-recovery",
                    "#context-status-cost",
                )
            ]
            warning = app.query_one("#context-status-warning", Static)
            return progress.progress, progress.display, rows, overlay.is_open and warning.display

    progress, displayed, rows, warning = anyio.run(scenario)
    assert progress == pytest.approx(71.875)
    assert displayed
    assert rows == [
        "Usage: 92k / 128k · 72%",
        "Source: provider observation",
        "Reserve: 8k · Remaining: 28k",
        "Automatic compaction: on · Trigger: >120k",
        "Threshold: eligible",
        "Overflow recovery: on",
        "Cost: $0.420",
    ]
    assert not warning


def test_context_overlay_hides_progress_when_window_is_unknown() -> None:
    async def scenario() -> tuple[bool, str]:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.context_status(_stats(window=None, remaining=None))
            await pilot.pause()
            progress = app.query_one("#context-status-progress", ProgressBar)
            usage = app.query_one("#context-status-usage", Label)
            return progress.display, _plain(usage)

    displayed, usage = anyio.run(scenario)
    assert not displayed
    assert usage == "Usage: 92k / unknown · unknown"


def test_context_overlay_clamps_progress_and_shows_threshold_warning() -> None:
    async def scenario() -> tuple[float | None, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.context_status(_stats(observed=140_000, remaining=-20_000, over_budget=True))
            await pilot.pause()
            return (
                app.query_one("#context-status-progress", ProgressBar).progress,
                app.query_one("#context-status-warning", Static).display,
            )

    progress, warning = anyio.run(scenario)
    assert progress == 100
    assert warning


def test_context_overlay_escape_restores_draft_focus_and_viewport() -> None:
    async def scenario() -> tuple[str, bool, tuple[float, bool], tuple[float, bool], bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft survives context"
            for index in range(20):
                app.write_assistant(f"line {index}")
            await pilot.pause()
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.stop_following()
            transcript.scroll_to(y=3, animate=False)
            await pilot.pause()
            before = transcript.viewport_state()

            renderer.context_status(_stats())
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            after = transcript.viewport_state()
            overlay = app.query_one("#context-status", ContextStatusOverlay)
            return (
                editor.value,
                editor.has_focus,
                (before.scroll_y, before.following),
                (after.scroll_y, after.following),
                overlay.is_open,
            )

    draft, focused, before, after, open_ = anyio.run(scenario)
    assert draft == "draft survives context"
    assert focused
    assert after == before
    assert not open_


def test_context_overlay_close_button_dismisses_report() -> None:
    async def scenario() -> bool:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.context_status(_stats())
            await pilot.pause()
            close = app.query_one("#context-status-close", Button)
            close.press()
            await pilot.pause()
            return app.query_one("#context-status", ContextStatusOverlay).is_open

    assert not anyio.run(scenario)


def test_context_overlay_yields_to_approval() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.context_status(_stats())
            await pilot.pause()
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="call-1",
                    name="write",
                    arguments={"path": "file.txt", "content": "new"},
                    safety="mutating",
                )
            )
            await pilot.pause()
            return (
                app.query_one("#context-status", ContextStatusOverlay).is_open,
                app.query_one("#decision-panel", DecisionPanel).is_open,
            )

    context_open, decision_open = anyio.run(scenario)
    assert not context_open
    assert decision_open


@pytest.mark.parametrize("size", [(40, 14), (72, 20)])
@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_context_overlay_fits_supported_sizes_and_themes(size: tuple[int, int], theme: str) -> None:
    async def scenario() -> tuple[int, int, int, int, int, int]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=size) as pilot:
            app.theme = theme
            renderer.context_status(_stats())
            await pilot.pause()
            panel = app.query_one("#context-status-panel")
            return (
                panel.region.x,
                panel.region.y,
                panel.region.right,
                panel.region.bottom,
                app.size.width,
                app.size.height,
            )

    left, top, right, bottom, width, height = anyio.run(scenario)
    assert 0 <= left < right <= width
    assert 0 <= top < bottom <= height
