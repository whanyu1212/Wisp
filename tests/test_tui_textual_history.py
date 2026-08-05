from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

import pytest
from textual.content import Content
from textual.widget import Widget

from wisp.events import JsonObject
from wisp.tui.history import HistoricalToolCard, HistoricalTranscriptMessage
from wisp.tui.textual_history import TextualHistoryController
from wisp.tui.transcript_window import TranscriptWindow


@dataclass(eq=False)
class _HistoryWidget:
    label: str
    name: str | None = None
    arguments: JsonObject | None = None
    status: str | None = None
    detail: str | Content = ""


@dataclass
class _HistorySurface:
    following: bool = True
    at_top: bool = False
    widgets: list[_HistoryWidget] = field(default_factory=list)
    historical_cards: dict[str, _HistoryWidget] = field(default_factory=dict)
    tool_cards: dict[str, _HistoryWidget] = field(default_factory=dict)
    window_availability: list[bool] = field(default_factory=list)
    render_starts: int = 0
    render_finishes: int = 0
    prepend_starts: int = 0
    prepend_finishes: int = 0
    follow_requests: int = 0
    latest_history_requests: int = 0
    fail_line_mount: bool = False
    marker_boundaries: list[Widget | None] = field(default_factory=list)

    def replace_transcript(self) -> None:
        self.widgets.clear()
        self.historical_cards.clear()
        self.tool_cards.clear()

    def mount_history_marker(self, message: str, *, before: Widget | None) -> None:
        self.marker_boundaries.append(before)
        self._mount(message, before=before)

    def history_is_at_top(self) -> bool:
        return self.at_top

    def history_is_following(self) -> bool:
        return self.following

    def begin_history_prepend(self) -> None:
        self.prepend_starts += 1

    def finish_history_prepend(self) -> None:
        self.prepend_finishes += 1

    def begin_history_render(self) -> None:
        self.render_starts += 1

    def finish_history_render(self) -> None:
        self.render_finishes += 1

    def follow_transcript_tail_after_refresh(self) -> None:
        self.follow_requests += 1

    def request_latest_history(self) -> bool:
        self.latest_history_requests += 1
        return True

    def set_history_window_available(self, *, has_older: bool) -> None:
        self.window_availability.append(has_older)

    def history_insertion_boundary(self, history_widgets: set[Widget]) -> Widget | None:
        widget = next(
            (
                candidate
                for candidate in self.widgets
                if cast(Widget, candidate) not in history_widgets
                and not candidate.label.startswith("resumed session:")
            ),
            None,
        )
        return cast(Widget, widget) if widget is not None else None

    def remove_historical_widget(self, widget: Widget) -> None:
        target = cast(_HistoryWidget, widget)
        self.widgets.remove(target)
        self.historical_cards = {
            card_id: card for card_id, card in self.historical_cards.items() if card is not target
        }
        self.tool_cards = {
            call_id: card for call_id, card in self.tool_cards.items() if card is not target
        }

    def mount_historical_line(
        self,
        role: str,
        message: str,
        *,
        before: Widget | None = None,
    ) -> Widget:
        if self.fail_line_mount:
            raise RuntimeError("mount failed")
        return cast(Widget, self._mount(f"{role}: {message}", before=before))

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: JsonObject,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        before: Widget | None = None,
    ) -> Widget:
        del historical
        widget = self._mount(f"tool: {name}", before=before)
        widget.name = name
        widget.arguments = arguments
        self.tool_cards[call_id] = widget
        if historical_card_id is not None:
            self.historical_cards[historical_card_id] = widget
        return cast(Widget, widget)

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: JsonObject,
        *,
        status: str,
        detail: str | Content,
        full_output: str,
        truncated: bool,
    ) -> bool:
        del full_output, truncated
        widget = self.historical_cards.get(card_id)
        if widget is None:
            return False
        widget.name = name
        widget.arguments = arguments
        widget.status = status
        widget.detail = detail
        return True

    def resolve_tool_call(
        self,
        call_id: str,
        status: str,
        *,
        detail: str | Content = "",
        full_output: str = "",
        truncated: bool = False,
    ) -> None:
        del full_output, truncated
        widget = self.tool_cards.get(call_id)
        if widget is None:
            return
        widget.status = status
        widget.detail = detail
        if status != "pending":
            del self.tool_cards[call_id]

    def historical_tool_card(self, card_id: str) -> Widget | None:
        widget = self.historical_cards.get(card_id)
        return cast(Widget, widget) if widget is not None else None

    def add_live_widget(self, label: str) -> _HistoryWidget:
        return self._mount(label, before=None)

    def _mount(self, label: str, *, before: Widget | None) -> _HistoryWidget:
        widget = _HistoryWidget(label)
        if before is None:
            self.widgets.append(widget)
            return widget
        index = self.widgets.index(cast(_HistoryWidget, before))
        self.widgets.insert(index, widget)
        return widget

    @property
    def history_labels(self) -> list[str]:
        return [
            widget.label
            for widget in self.widgets
            if not widget.label.startswith("resumed session:")
            and not widget.label.startswith("live:")
        ]


def _messages(
    role: Literal["user", "assistant"], prefix: str, count: int
) -> tuple[HistoricalTranscriptMessage, ...]:
    return tuple(
        HistoricalTranscriptMessage(role=role, content=f"{prefix} {index}")
        for index in range(count)
    )


def test_history_controller_reconciles_a_bounded_window_without_full_history_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    current = _messages("assistant", "current", 300)
    older = _messages("user", "older", 75)

    controller.replace_entries(current, session_label="Windowed")
    surface.add_live_widget("live: output")
    controller.prepend_entries(older)

    def reject_full_history_scan(_window: object) -> tuple[object, ...]:
        raise AssertionError("history reconciliation must not scan every retained entry")

    monkeypatch.setattr(TranscriptWindow, "entries", property(reject_full_history_scan))

    assert controller.shift_older()
    assert surface.history_labels[0] == "user: older 0"
    assert surface.history_labels[-1] == "assistant: current 224"
    assert len(surface.history_labels) == 300
    assert any(widget.label == "live: output" for widget in surface.widgets)
    assert surface.window_availability[-1] is False

    assert controller.show_latest()
    assert surface.history_labels[0] == "assistant: current 0"
    assert surface.history_labels[-1] == "assistant: current 299"
    assert len(surface.history_labels) == 300
    assert any(widget.label == "live: output" for widget in surface.widgets)
    assert surface.window_availability[-1] is True


def test_history_controller_mounts_session_marker_before_queued_history() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)

    controller.replace_entries(
        (HistoricalTranscriptMessage(role="assistant", content="restored"),),
        session_label="Resumed",
    )

    assert surface.marker_boundaries == [None]
    assert [widget.label for widget in surface.widgets] == [
        "resumed session: Resumed",
        "assistant: restored",
    ]


def test_history_controller_clears_entries_for_new_session() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    controller.render_entries(_messages("assistant", "old", 2))

    controller.clear_entries()

    assert controller.retained_entry_count == 0
    assert surface.widgets == []
    assert surface.historical_cards == {}
    assert surface.tool_cards == {}


def test_history_controller_pairs_boundary_tool_cards_and_resets_on_session_replace() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    result = HistoricalToolCard(
        card_id="history:result",
        name="bash",
        arguments={},
        output="done",
        is_error=False,
        tool_call_id="call-1",
        call_missing=True,
    )
    missing_call = HistoricalToolCard(
        card_id="history:missing:call-1",
        name="bash",
        arguments={"command": "printf done"},
        output="No persisted tool result.",
        is_error=True,
        tool_call_id="call-1",
        status="cancelled",
        missing_result=True,
    )

    controller.replace_entries((result,), session_label="First")
    controller.prepend_entries((missing_call,))

    cards = [widget for widget in surface.widgets if widget.name == "bash"]
    assert len(cards) == 1
    assert cards[0].arguments == {"command": "printf done"}
    assert cards[0].status == "done"

    controller.replace_entries((missing_call,), session_label="Second")

    cards = [widget for widget in surface.widgets if widget.name == "bash"]
    assert len(cards) == 1
    assert cards[0].arguments == {"command": "printf done"}
    assert cards[0].status == "cancelled"


def test_history_controller_finishes_a_render_batch_when_mounting_fails() -> None:
    surface = _HistorySurface(fail_line_mount=True)
    controller = TextualHistoryController(surface)

    with pytest.raises(RuntimeError, match="mount failed"):
        controller.render_entries((HistoricalTranscriptMessage(role="user", content="prompt"),))

    assert surface.render_starts == surface.render_finishes == 1
    assert surface.prepend_starts == surface.prepend_finishes == 0


def test_history_controller_reloads_latest_after_older_paging_evicts_it() -> None:
    surface = _HistorySurface(at_top=True)
    controller = TextualHistoryController(surface, retained_capacity=300)
    current = _messages("assistant", "current", 300)
    older = _messages("user", "older", 75)

    controller.replace_entries(current, session_label="Windowed")
    controller.prepend_entries(older)

    assert controller.show_latest()
    assert surface.latest_history_requests == 1
    assert surface.history_labels[0] == "user: older 0"

    reloaded = _messages("assistant", "latest", 75)
    follow_requests_before_reload = surface.follow_requests
    controller.replace_latest_entries(reloaded)

    assert surface.history_labels == [f"assistant: latest {index}" for index in range(75)]
    assert surface.follow_requests == follow_requests_before_reload + 1
    assert not controller.show_latest()


def test_history_controller_defers_a_latest_reload_after_reader_leaves_tail() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    controller.replace_entries(
        (HistoricalTranscriptMessage(role="assistant", content="retained"),),
        session_label="Windowed",
    )
    controller.capture_latest_reload_live_entries()
    follow_requests_before_reload = surface.follow_requests
    surface.following = False

    assert not controller.replace_latest_entries(
        (HistoricalTranscriptMessage(role="assistant", content="reloaded"),)
    )
    assert surface.history_labels == ["assistant: retained"]
    assert surface.follow_requests == follow_requests_before_reload


def test_history_controller_excludes_live_entries_from_a_latest_reload() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    controller.replace_entries(
        (HistoricalTranscriptMessage(role="assistant", content="previous"),),
        session_label="Windowed",
    )
    controller.record_live_message("user", "prompt")
    controller.record_live_message("assistant", "reply")
    controller.record_live_tool_call("call-1")
    controller.record_live_tool_result("call-1")

    controller.replace_latest_entries(
        (
            HistoricalTranscriptMessage(role="assistant", content="previous"),
            HistoricalTranscriptMessage(role="user", content="prompt"),
            HistoricalTranscriptMessage(role="assistant", content="reply"),
            HistoricalToolCard(
                card_id="history:result",
                name="bash",
                arguments={},
                output="done",
                is_error=False,
                tool_call_id="call-1",
            ),
        )
    )

    assert surface.history_labels == ["assistant: previous"]


def test_history_controller_discards_a_failed_live_prompt() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    controller.record_live_message("user", "failed prompt")
    controller.discard_live_message("user", "failed prompt")

    controller.replace_latest_entries(
        (HistoricalTranscriptMessage(role="assistant", content="previous"),)
    )

    assert surface.history_labels == ["assistant: previous"]


def test_history_controller_uses_the_live_snapshot_from_the_reload_request() -> None:
    surface = _HistorySurface(at_top=True)
    controller = TextualHistoryController(surface, retained_capacity=300)
    controller.replace_entries(_messages("assistant", "current", 300), session_label="Windowed")
    controller.prepend_entries(_messages("user", "older", 75))
    controller.record_live_message("user", "persisted before reload")

    assert controller.show_latest()
    controller.capture_latest_reload_live_entries()
    controller.record_live_message("user", "submitted after reload")
    controller.replace_latest_entries(
        (HistoricalTranscriptMessage(role="user", content="persisted before reload"),)
    )

    assert surface.latest_history_requests == 1
    assert surface.history_labels == []


def test_history_controller_releases_evicted_live_widget_identity() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    widget = Widget()
    controller.record_live_message("assistant", "evicted response", widget=widget)
    controller.capture_latest_reload_live_entries()
    controller.forget_live_widget(widget)

    controller.replace_latest_entries(
        (HistoricalTranscriptMessage(role="assistant", content="evicted response"),)
    )

    assert surface.history_labels == ["assistant: evicted response"]
