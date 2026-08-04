"""Focused tests for transient Textual live-transcript presentation state."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
from textual.widget import Widget

from wisp.tui.history import TUI_HISTORY_PAGE_LIMIT
from wisp.tui.textual_transcript import (
    TUI_SETTLED_LIVE_DURABLE_ENTRY_LIMIT,
    TUI_SETTLED_LIVE_WIDGET_LIMIT,
    TextualTranscriptController,
)
from wisp.tui.widgets import ToolCard, WorkingIndicator


@dataclass
class _Surface:
    available: bool = True
    following: bool = True
    mounted: list[Widget] = field(default_factory=list)
    removed: list[Widget] = field(default_factory=list)
    unseen_counts: list[int] = field(default_factory=list)
    unseen_hidden: int = 0
    evicted: list[Widget] = field(default_factory=list)
    follow_requests: int = 0
    returned_to_latest: int = 0
    controller: TextualTranscriptController | None = field(default=None, repr=False)

    def transcript_available(self) -> bool:
        return self.available

    def mount_live_transcript_widget(self, widget: Widget, *, before: Widget | None = None) -> None:
        if before is None:
            self.mounted.append(widget)
        else:
            self.mounted.insert(self.mounted.index(before), widget)

    def remove_live_transcript_widget(self, widget: Widget) -> None:
        self.removed.append(widget)
        if widget in self.mounted:
            self.mounted.remove(widget)
        if isinstance(widget, WorkingIndicator):
            widget.on_unmount()

    def transcript_is_following(self) -> bool:
        return self.following

    def follow_transcript_tail_after_refresh(self) -> None:
        self.follow_requests += 1

    def record_live_transcript_update(self, widget: Widget) -> None:
        assert self.controller is not None
        self.controller.note_update(widget)

    def return_transcript_to_latest(self) -> None:
        self.returned_to_latest += 1
        self.following = True

    def is_newest_transcript_widget(self, widget: Widget) -> bool:
        return bool(self.mounted) and self.mounted[-1] is widget

    def show_unseen_output(self, count: int) -> None:
        self.unseen_counts.append(count)

    def hide_unseen_output(self) -> None:
        self.unseen_hidden += 1

    def live_transcript_widget_evicted(self, widget: Widget) -> None:
        self.evicted.append(widget)


def _controller(surface: _Surface) -> TextualTranscriptController:
    controller = TextualTranscriptController(surface)
    surface.controller = controller
    return controller


def test_unseen_output_counts_distinct_widgets_and_clears_at_tail() -> None:
    surface = _Surface(following=False)
    controller = _controller(surface)
    first = Widget()
    second = Widget()

    controller.note_update(first)
    controller.note_update(first)
    controller.note_update(second)

    assert controller.unseen_output_count == 2
    assert surface.unseen_counts == [1, 1, 2]

    controller.clear_unseen_output()

    assert controller.unseen_output_count == 0
    assert surface.unseen_hidden == 1


def test_working_indicator_lifecycle_removes_its_unseen_identity() -> None:
    async def scenario() -> tuple[bool, bool, int, int]:
        surface = _Surface(following=False)
        controller = _controller(surface)

        controller.show_working_indicator()
        indicator = controller.working_indicator
        assert isinstance(indicator, WorkingIndicator)
        controller.show_retry_indicator("Retrying provider")
        controller.hide_working_indicator()

        return (
            indicator in surface.mounted,
            indicator in surface.removed,
            controller.unseen_output_count,
            surface.unseen_hidden,
        )

    mounted, removed, unseen, hidden = anyio.run(scenario)
    assert not mounted
    assert removed
    assert unseen == 0
    assert hidden == 1


def test_tool_cards_resolve_by_id_and_terminal_states_do_not_leak() -> None:
    surface = _Surface()
    controller = _controller(surface)

    first = controller.mount_tool_call("one", "read", {"path": "one"})
    second = controller.mount_tool_call("two", "bash", {"command": "two"})
    assert isinstance(first, ToolCard)
    assert isinstance(second, ToolCard)

    controller.resolve_tool_call("two", "done", detail="two complete")
    assert controller.pending_tool_count == 1
    assert second._glyph == "✓"

    controller.fail_pending_tool_calls("cancelled")
    assert controller.pending_tool_count == 0
    assert first._glyph == "⊘"
    assert first._timer is None


def test_historical_card_lookup_is_evicted_with_its_widget() -> None:
    surface = _Surface()
    controller = _controller(surface)

    card = controller.mount_tool_call(
        "history-call",
        "write",
        {"path": "history.py"},
        historical_card_id="history:card",
        historical=True,
    )
    assert isinstance(card, ToolCard)
    assert controller.historical_tool_card("history:card") is card

    controller.forget_widget(card)

    assert controller.historical_tool_card("history:card") is None
    assert controller.pending_tool_count == 0


def test_only_newest_focused_card_repins_until_user_scrolls() -> None:
    surface = _Surface(following=True)
    controller = _controller(surface)
    first = controller.mount_tool_call("one", "read", {})
    second = controller.mount_tool_call("two", "read", {})
    assert isinstance(first, ToolCard)
    assert isinstance(second, ToolCard)

    controller.tool_card_focused(second)
    surface.following = False  # Textual's deferred focus scroll.
    controller.tool_card_toggled(second)
    assert surface.returned_to_latest == 1

    controller.tool_card_focused(second)
    controller.user_scrolled()
    controller.tool_card_toggled(second)
    assert surface.returned_to_latest == 1

    controller.tool_card_focused(first)
    controller.tool_card_toggled(first)
    assert surface.returned_to_latest == 1


def test_settled_live_widgets_are_bounded_and_released_for_durable_history() -> None:
    surface = _Surface()
    controller = TextualTranscriptController(surface, settled_capacity=2)
    first = Widget()
    second = Widget()
    third = Widget()

    controller.settle_widget(first)
    controller.settle_widget(second)
    controller.settle_widget(third)

    assert controller.settled_widget_count == 2
    assert surface.removed == [first]
    assert surface.evicted == [first]


def test_settled_live_limit_keeps_the_first_eviction_in_one_history_page() -> None:
    assert TUI_SETTLED_LIVE_WIDGET_LIMIT == TUI_HISTORY_PAGE_LIMIT - 1
    assert TUI_SETTLED_LIVE_DURABLE_ENTRY_LIMIT == TUI_HISTORY_PAGE_LIMIT - 1


def test_settled_live_widgets_are_also_bounded_by_durable_entry_count() -> None:
    surface = _Surface()
    controller = TextualTranscriptController(
        surface,
        settled_capacity=4,
        durable_entry_capacity=2,
    )
    first = Widget()
    second = Widget()

    controller.settle_widget(first, durable_entry_count=2)
    controller.settle_widget(second, durable_entry_count=2)

    assert controller.settled_widget_count == 1
    assert surface.removed == [first]


def test_pending_tool_cards_are_not_eligible_for_settled_widget_eviction() -> None:
    surface = _Surface()
    controller = TextualTranscriptController(surface, settled_capacity=1)
    surface.controller = controller

    first = controller.mount_tool_call("one", "read", {})
    second = controller.mount_tool_call("two", "bash", {})
    assert isinstance(first, ToolCard)
    assert isinstance(second, ToolCard)
    assert controller.settled_widget_count == 0

    controller.resolve_tool_call("one", "done")
    controller.resolve_tool_call("two", "done")

    assert controller.settled_widget_count == 1
    assert surface.removed == [first]
    assert surface.evicted == [first]


def test_reset_clears_live_state_without_creating_missing_transcript_widgets() -> None:
    async def scenario() -> tuple[int, int, bool, int]:
        surface = _Surface(following=False)
        controller = _controller(surface)
        controller.show_working_indicator()
        controller.mount_tool_call("one", "read", {})
        controller.note_update(Widget())

        controller.reset()
        surface.available = False
        controller.show_working_indicator()
        controller.restart_working_indicator()

        return (
            controller.pending_tool_count,
            controller.unseen_output_count,
            controller.working_indicator is None,
            len(surface.mounted),
        )

    pending, unseen, no_indicator, mounted = anyio.run(scenario)
    assert pending == 0
    assert unseen == 0
    assert no_indicator
    assert mounted == 1  # the original tool card remains for app-level transcript clearing
