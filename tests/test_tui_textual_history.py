from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

import pytest
from textual.content import Content
from textual.widget import Widget

from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import JsonObject
from wisp.tui.history import (
    TUI_HISTORY_PAGE_LIMIT,
    HistoricalSkillInvocation,
    HistoricalToolCard,
    HistoricalTranscriptMessage,
)
from wisp.tui.process_lifecycle import ProcessLifecyclePresentation
from wisp.tui.textual_history import TextualHistoryController
from wisp.tui.transcript_window import (
    TUI_TRANSCRIPT_WINDOW_SHIFT,
    TUI_TRANSCRIPT_WINDOW_SIZE,
    TranscriptWindow,
)


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

    def mount_process_card(
        self,
        process_id: str,
        *,
        historical: bool = False,
        before: Widget | None = None,
    ) -> Widget:
        del historical
        existing = self.historical_cards.get(f"process:{process_id}")
        if existing is not None:
            return cast(Widget, existing)
        widget = self._mount(f"process: {process_id}", before=before)
        self.historical_cards[f"process:{process_id}"] = widget
        return cast(Widget, widget)

    def update_process_card(
        self,
        presentation: ProcessLifecyclePresentation,
        *,
        elapsed: float | None = None,
        settle_terminal: bool = False,
    ) -> Widget | None:
        del elapsed, settle_terminal
        widget = self.historical_cards.get(f"process:{presentation.process_id}")
        if widget is None:
            return None
        widget.status = presentation.display_state
        widget.detail = presentation.full_output
        return cast(Widget, widget)

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: JsonObject,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        arguments_available: bool = True,
        before: Widget | None = None,
    ) -> Widget:
        del historical, arguments_available
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
    current = _messages("assistant", "current", TUI_TRANSCRIPT_WINDOW_SIZE)
    older = _messages("user", "older", TUI_TRANSCRIPT_WINDOW_SHIFT)

    controller.replace_entries(current, session_label="Windowed")
    surface.add_live_widget("live: output")
    controller.prepend_entries(older)

    def reject_full_history_scan(_window: object) -> tuple[object, ...]:
        raise AssertionError("history reconciliation must not scan every retained entry")

    monkeypatch.setattr(TranscriptWindow, "entries", property(reject_full_history_scan))

    assert controller.shift_older()
    assert surface.history_labels[0] == "user: older 0"
    assert surface.history_labels[-1] == (
        f"assistant: current {TUI_TRANSCRIPT_WINDOW_SIZE - TUI_TRANSCRIPT_WINDOW_SHIFT - 1}"
    )
    assert len(surface.history_labels) == TUI_TRANSCRIPT_WINDOW_SIZE
    assert any(widget.label == "live: output" for widget in surface.widgets)
    assert surface.window_availability[-1] is False

    assert controller.show_latest()
    assert surface.history_labels[0] == "assistant: current 0"
    assert surface.history_labels[-1] == (f"assistant: current {TUI_TRANSCRIPT_WINDOW_SIZE - 1}")
    assert len(surface.history_labels) == TUI_TRANSCRIPT_WINDOW_SIZE
    assert any(widget.label == "live: output" for widget in surface.widgets)
    assert surface.window_availability[-1] is True


def test_history_controller_moves_directly_to_oldest_retained_window() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    current = _messages("assistant", "current", TUI_TRANSCRIPT_WINDOW_SIZE)
    older = _messages("user", "older", TUI_TRANSCRIPT_WINDOW_SHIFT * 2)

    controller.replace_entries(current, session_label="Windowed")
    controller.prepend_entries(older)

    assert controller.show_oldest()
    assert surface.history_labels[0] == "user: older 0"
    assert len(surface.history_labels) == TUI_TRANSCRIPT_WINDOW_SIZE
    assert surface.window_availability[-1] is False
    assert not controller.show_oldest()


def test_history_controller_reaches_oldest_entries_beyond_retention_limit() -> None:
    surface = _HistorySurface(at_top=True)
    controller = TextualHistoryController(surface)
    controller.replace_entries(
        _messages("assistant", "message", TUI_HISTORY_PAGE_LIMIT),
        session_label="Windowed",
    )

    next_newest = 1_925
    while next_newest > 0:
        page_start = max(0, next_newest - TUI_HISTORY_PAGE_LIMIT)
        controller.prepend_entries(
            tuple(
                HistoricalTranscriptMessage(
                    role="assistant",
                    content=f"older {index}",
                )
                for index in range(page_start, next_newest)
            )
        )
        controller.show_oldest()
        next_newest = page_start

    assert surface.history_labels[0] == "assistant: older 0"
    assert controller.retained_entry_count == 1_200
    assert not controller.show_oldest()


def test_history_controller_mounts_session_marker_before_restored_history() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)

    controller.replace_entries(
        (HistoricalTranscriptMessage(role="assistant", content="restored"),),
        session_label="Resumed",
    )

    assert len(surface.marker_boundaries) == 1
    assert surface.marker_boundaries[0] is surface.widgets[1]
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


def test_history_controller_pairs_split_process_call_before_grouping() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    result = HistoricalToolCard(
        card_id="history:result",
        name="bash",
        arguments={},
        output="Process proc-1 completed with exit code 0\nstdout:\ndone\n",
        is_error=False,
        status="done",
        tool_call_id="poll-1",
        call_missing=True,
    )
    missing_call = HistoricalToolCard(
        card_id="history:missing:poll-1",
        name="bash",
        arguments={"operation": "poll", "process_id": "proc-1"},
        output="No persisted tool result.",
        is_error=True,
        tool_call_id="poll-1",
        status="cancelled",
        missing_result=True,
    )

    controller.replace_entries((result,), session_label="First")
    controller.prepend_entries((missing_call,))

    process_widgets = [widget for widget in surface.widgets if widget.label == "process: proc-1"]
    assert len(process_widgets) == 1
    assert process_widgets[0].status == "completed"
    assert process_widgets[0].detail == "done"
    assert not [widget for widget in surface.widgets if widget.name == "bash"]


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [("poll", "poll_denied"), ("cancel", "cancel_denied")],
)
def test_history_controller_replays_denied_process_operation_with_reason(
    operation: str,
    expected_status: str,
) -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    denied = HistoricalToolCard(
        card_id="history:denied",
        name="bash",
        arguments={"operation": operation, "process_id": "proc-1"},
        output="not now",
        is_error=True,
        status="denied",
        tool_call_id="process-1",
    )

    controller.replace_entries((denied,), session_label="Denied")

    process_widget = next(widget for widget in surface.widgets if widget.label == "process: proc-1")
    assert process_widget.status == expected_status
    assert process_widget.detail == "not now"


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [("poll", "poll_interrupted"), ("cancel", "cancel_interrupted")],
)
def test_history_controller_replays_interrupted_process_operation(
    operation: str,
    expected_status: str,
) -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    interrupted = HistoricalToolCard(
        card_id="history:interrupted",
        name="bash",
        arguments={"operation": operation, "process_id": "proc-1"},
        output=INTERRUPTED_TOOL_RESULT_TEXT,
        is_error=True,
        status="cancelled",
        tool_call_id="process-1",
    )

    controller.replace_entries((interrupted,), session_label="Interrupted")

    process_widget = next(widget for widget in surface.widgets if widget.label == "process: proc-1")
    assert process_widget.status == expected_status


def test_history_controller_coalesces_process_polls_across_a_prepended_page() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    newer = HistoricalToolCard(
        card_id="history:newer",
        name="bash",
        arguments={"operation": "poll", "process_id": "proc-1"},
        output="Process proc-1 completed with exit code 0\nstdout:\nnewer\n",
        is_error=False,
        status="done",
        tool_call_id="poll-2",
    )
    older = HistoricalToolCard(
        card_id="history:older",
        name="bash",
        arguments={"operation": "poll", "process_id": "proc-1"},
        output="Process proc-1 is still running\nstdout:\nolder\n",
        is_error=False,
        tool_call_id="poll-1",
    )

    controller.render_entries((newer,))
    controller.prepend_entries((older,))

    process_widgets = [widget for widget in surface.widgets if widget.label == "process: proc-1"]
    assert len(process_widgets) == 1
    assert process_widgets[0].status == "completed"
    assert process_widgets[0].detail == "older\nnewer"


def test_history_controller_replays_grep_summary_with_match_evidence() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    output = "a.py:1:TODO\nb.py:2:TODO\n"

    controller.replace_entries(
        (
            HistoricalToolCard(
                card_id="history:grep",
                name="grep",
                arguments={"pattern": "TODO", "path": "src"},
                output=output,
                is_error=False,
                summary="grep: 2 matches",
            ),
        ),
        session_label="Search",
    )

    card = next(widget for widget in surface.widgets if widget.name == "grep")
    assert card.arguments == {"pattern": "TODO", "path": "src"}
    assert card.detail == "grep: 2 matches\na.py:1:TODO\nb.py:2:TODO"


def test_history_controller_finishes_a_render_batch_when_mounting_fails() -> None:
    surface = _HistorySurface(fail_line_mount=True)
    controller = TextualHistoryController(surface)

    with pytest.raises(RuntimeError, match="mount failed"):
        controller.render_entries((HistoricalTranscriptMessage(role="user", content="prompt"),))

    assert surface.render_starts == surface.render_finishes == 1
    assert surface.prepend_starts == surface.prepend_finishes == 0


def test_history_controller_reloads_latest_after_older_paging_evicts_it() -> None:
    surface = _HistorySurface(at_top=True)
    controller = TextualHistoryController(
        surface,
        retained_capacity=TUI_TRANSCRIPT_WINDOW_SIZE,
    )
    current = _messages("assistant", "current", TUI_TRANSCRIPT_WINDOW_SIZE)
    older = _messages("user", "older", TUI_TRANSCRIPT_WINDOW_SHIFT)

    controller.replace_entries(current, session_label="Windowed")
    controller.prepend_entries(older)

    assert controller.show_latest()
    assert surface.latest_history_requests == 1
    assert surface.history_labels[0] == "user: older 0"

    reloaded = _messages("assistant", "latest", TUI_HISTORY_PAGE_LIMIT)
    follow_requests_before_reload = surface.follow_requests
    controller.replace_latest_entries(reloaded)

    first_visible = TUI_HISTORY_PAGE_LIMIT - TUI_TRANSCRIPT_WINDOW_SIZE
    assert surface.history_labels == [
        f"assistant: latest {index}" for index in range(first_visible, TUI_HISTORY_PAGE_LIMIT)
    ]
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


def test_history_controller_matches_a_fully_clipped_live_skill_invocation() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    prompt = "/skill:review focus on safety"
    controller.record_live_message("user", prompt)
    controller.record_live_skill_invocation("message-1", prompt)

    controller.replace_latest_entries(
        (
            HistoricalSkillInvocation(
                entry_id="message-1",
                name="review",
                original_content="",
                original_content_truncated=True,
                request="",
                request_truncated=True,
                instructions_truncated=False,
            ),
        )
    )

    assert surface.history_labels == []


def test_history_controller_keeps_a_different_fully_clipped_skill_invocation() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    live_prompt = "/skill:review bbb"
    controller.record_live_message("user", live_prompt)
    controller.capture_latest_reload_live_entries()
    controller.record_live_skill_invocation("message-2", live_prompt)

    controller.replace_latest_entries(
        (
            HistoricalSkillInvocation(
                entry_id="message-1",
                name="review",
                original_content="",
                original_content_truncated=True,
                request="",
                request_truncated=True,
                instructions_truncated=False,
            ),
        )
    )

    assert surface.history_labels == ["user: skill /skill:review [request truncated]"]


def test_history_controller_preserves_an_identical_pre_request_snapshot_entry() -> None:
    surface = _HistorySurface()
    controller = TextualHistoryController(surface)
    prompt = "/skill:review focus on safety"
    controller.record_live_message("user", prompt)
    controller.record_live_skill_invocation("message-1", prompt)
    controller.capture_latest_reload_live_entries()
    controller.record_live_message("user", prompt)
    controller.record_live_skill_invocation("message-2", prompt)

    controller.replace_latest_entries(
        (
            HistoricalSkillInvocation(
                entry_id="message-1",
                name="review",
                original_content="",
                original_content_truncated=True,
                request="",
                request_truncated=True,
                instructions_truncated=False,
            ),
        )
    )

    assert surface.history_labels == []


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
    controller = TextualHistoryController(
        surface,
        retained_capacity=TUI_TRANSCRIPT_WINDOW_SIZE,
    )
    controller.replace_entries(
        _messages("assistant", "current", TUI_TRANSCRIPT_WINDOW_SIZE),
        session_label="Windowed",
    )
    controller.prepend_entries(_messages("user", "older", TUI_TRANSCRIPT_WINDOW_SHIFT))
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


def test_history_controller_recovers_evicted_prefix_while_reader_is_browsing() -> None:
    surface = _HistorySurface(following=False, at_top=True)
    controller = TextualHistoryController(surface)
    evicted = cast(Widget, surface._mount("assistant: evicted", before=None))
    surviving = cast(Widget, surface._mount("assistant: surviving", before=None))
    controller.record_live_message("assistant", "evicted", widget=evicted)
    controller.record_live_message("assistant", "surviving", widget=surviving)
    controller.capture_latest_reload_live_entries()
    controller.forget_live_widget(evicted)
    surface.widgets.remove(cast(_HistoryWidget, evicted))

    controller.recover_evicted_entries(
        (
            HistoricalTranscriptMessage(role="assistant", content="evicted"),
            HistoricalTranscriptMessage(role="assistant", content="surviving"),
        )
    )

    assert [widget.label for widget in surface.widgets] == [
        "assistant: evicted",
        "assistant: surviving",
    ]
    assert surface.prepend_starts == surface.prepend_finishes == 1
    assert surface.follow_requests == 0


def test_history_recovery_drops_prefix_already_retained_before_live_entries() -> None:
    surface = _HistorySurface(following=False, at_top=True)
    controller = TextualHistoryController(surface)
    retained = HistoricalTranscriptMessage(
        role="assistant",
        content="retained",
        entry_id="retained",
    )
    controller.replace_entries((retained,), session_label="Windowed")
    evicted = cast(Widget, surface._mount("assistant: evicted", before=None))
    surviving = cast(Widget, surface._mount("assistant: surviving", before=None))
    controller.record_live_message("assistant", "evicted", widget=evicted)
    controller.record_live_message("assistant", "surviving", widget=surviving)
    controller.capture_latest_reload_live_entries()
    controller.forget_live_widget(evicted)
    surface.widgets.remove(cast(_HistoryWidget, evicted))

    controller.recover_evicted_entries(
        (
            retained,
            HistoricalTranscriptMessage(
                role="assistant",
                content="evicted",
                entry_id="evicted",
            ),
            HistoricalTranscriptMessage(
                role="assistant",
                content="surviving",
                entry_id="surviving",
            ),
        )
    )

    assert [widget.label for widget in surface.widgets] == [
        "resumed session: Windowed",
        "assistant: retained",
        "assistant: evicted",
        "assistant: surviving",
    ]


def test_history_recovery_keeps_distinct_repeated_message_after_retained_history() -> None:
    surface = _HistorySurface(following=False, at_top=True)
    controller = TextualHistoryController(surface)
    retained = HistoricalTranscriptMessage(
        role="assistant",
        content="repeated",
        entry_id="retained",
    )
    controller.replace_entries((retained,), session_label="Windowed")
    evicted = cast(Widget, surface._mount("assistant: repeated", before=None))
    surviving = cast(Widget, surface._mount("assistant: surviving", before=None))
    controller.record_live_message("assistant", "repeated", widget=evicted)
    controller.record_live_message("assistant", "surviving", widget=surviving)
    controller.capture_latest_reload_live_entries()
    controller.forget_live_widget(evicted)
    surface.widgets.remove(cast(_HistoryWidget, evicted))

    controller.recover_evicted_entries(
        (
            HistoricalTranscriptMessage(
                role="assistant",
                content="repeated",
                entry_id="evicted",
            ),
            HistoricalTranscriptMessage(
                role="assistant",
                content="surviving",
                entry_id="surviving",
            ),
        )
    )

    assert [widget.label for widget in surface.widgets] == [
        "resumed session: Windowed",
        "assistant: repeated",
        "assistant: repeated",
        "assistant: surviving",
    ]


def test_history_recovery_does_not_evict_the_oldest_full_retained_window() -> None:
    surface = _HistorySurface(following=False, at_top=True)
    controller = TextualHistoryController(
        surface,
        retained_capacity=TUI_TRANSCRIPT_WINDOW_SIZE,
    )
    retained = _messages("assistant", "retained", TUI_TRANSCRIPT_WINDOW_SIZE)
    controller.replace_entries(retained, session_label="Windowed")
    evicted = cast(Widget, surface._mount("assistant: evicted", before=None))
    surviving = cast(Widget, surface._mount("assistant: surviving", before=None))
    controller.record_live_message("assistant", "evicted", widget=evicted)
    controller.record_live_message("assistant", "surviving", widget=surviving)
    controller.capture_latest_reload_live_entries()
    controller.forget_live_widget(evicted)
    surface.widgets.remove(cast(_HistoryWidget, evicted))

    recovered = controller.recover_evicted_entries(
        (
            *retained,
            HistoricalTranscriptMessage(role="assistant", content="evicted"),
            HistoricalTranscriptMessage(role="assistant", content="surviving"),
        )
    )

    assert not recovered
    assert surface.history_labels[:-1] == [f"assistant: retained {index}" for index in range(60)]
    assert surface.widgets[-1].label == "assistant: surviving"


def test_history_recovery_defers_entries_outside_the_mounted_slice() -> None:
    surface = _HistorySurface(following=False, at_top=True)
    controller = TextualHistoryController(surface)
    retained = _messages("assistant", "retained", TUI_TRANSCRIPT_WINDOW_SIZE)
    controller.replace_entries(retained, session_label="Windowed")
    evicted = cast(Widget, surface._mount("assistant: evicted", before=None))
    surviving = cast(Widget, surface._mount("assistant: surviving", before=None))
    controller.record_live_message("assistant", "evicted", widget=evicted)
    controller.record_live_message("assistant", "surviving", widget=surviving)
    controller.capture_latest_reload_live_entries()
    controller.forget_live_widget(evicted)
    surface.widgets.remove(cast(_HistoryWidget, evicted))

    recovered = controller.recover_evicted_entries(
        (
            *retained,
            HistoricalTranscriptMessage(role="assistant", content="evicted"),
            HistoricalTranscriptMessage(role="assistant", content="surviving"),
        )
    )

    assert not recovered
    assert surface.history_labels[:-1] == [
        f"assistant: retained {index}" for index in range(TUI_TRANSCRIPT_WINDOW_SIZE)
    ]
    assert surface.widgets[-1].label == "assistant: surviving"
