"""Retained transcript-history coordination for the Textual frontend.

Import direction is intentionally one-way::

    textual_renderer -> textual_history -> renderer-neutral history/tool helpers

This controller owns only persisted-history retention, bounded-window reconciliation,
and page-boundary tool-card pairing. It talks to Textual through a narrow structural
surface; it does not import the app, shell, RPC, sessions, providers, or live-stream
state. The app remains responsible for widget lifecycle and viewport restoration.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from textual.content import Content
from textual.widget import Widget

from wisp.events import JsonObject
from wisp.tui.diff_presentation import DiffPresentation
from wisp.tui.history import (
    HistoricalToolCard,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    historical_tool_status,
)
from wisp.tui.tool_output import full_tool_output_for_display, render_tool_result
from wisp.tui.transcript_window import TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT, TranscriptWindow


class TextualHistorySurface(Protocol):
    """Textual operations needed to mount and reconcile retained history."""

    def replace_transcript(self) -> None: ...

    def mount_history_marker(self, message: str, *, before: Widget | None) -> None: ...

    def history_is_at_top(self) -> bool: ...

    def history_is_following(self) -> bool: ...

    def begin_history_prepend(self) -> None: ...

    def finish_history_prepend(self) -> None: ...

    def begin_history_render(self) -> None: ...

    def finish_history_render(self) -> None: ...

    def follow_transcript_tail_after_refresh(self) -> None: ...

    def request_latest_history(self) -> bool: ...

    def set_history_window_available(self, *, has_older: bool) -> None: ...

    def history_insertion_boundary(self, history_widgets: set[Widget]) -> Widget | None: ...

    def remove_historical_widget(self, widget: Widget) -> None: ...

    def mount_historical_line(
        self,
        role: str,
        message: str,
        *,
        before: Widget | None = None,
    ) -> Widget | None: ...

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: JsonObject,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        before: Widget | None = None,
    ) -> Widget | None: ...

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: JsonObject,
        *,
        status: str,
        detail: str | Content | DiffPresentation,
        full_output: str,
        truncated: bool,
    ) -> bool: ...

    def resolve_tool_call(
        self,
        call_id: str,
        status: str,
        *,
        detail: str | Content | DiffPresentation = "",
        full_output: str = "",
        truncated: bool = False,
    ) -> None: ...

    def historical_tool_card(self, card_id: str) -> Widget | None: ...


@dataclass(frozen=True)
class _RetainedHistoryEntry:
    """One UI-local identity for a persisted transcript entry."""

    id: int
    entry: HistoricalTranscriptEntry


@dataclass(frozen=True)
class _LiveHistoryEntry:
    """One persisted entry already represented by a live transcript widget."""

    kind: Literal["message", "tool"]
    role: Literal["user", "assistant"] | None = None
    content: str | None = None
    tool_call_id: str | None = None


class TextualHistoryController:
    """Own bounded retained-history state for one Textual transcript.

    Entries are retained only for the lifetime of this controller. Durable paging,
    Textual layout, and live transcript output deliberately remain outside this
    class. The controller's public methods match history lifecycle transitions
    rather than exposing its mutable window or widget maps.
    """

    def __init__(
        self,
        surface: TextualHistorySurface,
        *,
        retained_capacity: int = TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT,
    ) -> None:
        self._surface = surface
        self._historical_tool_results: dict[str, deque[tuple[str, HistoricalToolCard]]] = {}
        self._resolved_boundary_results: dict[str, HistoricalToolCard] = {}
        self._boundary_result_calls: dict[str, str] = {}
        self._window = TranscriptWindow[_RetainedHistoryEntry](retained_capacity=retained_capacity)
        self._widgets: dict[int, Widget] = {}
        self._next_entry_id = 0
        self._live_entries: list[_LiveHistoryEntry] = []

    @property
    def retained_entry_count(self) -> int:
        """Return the number of history entries retained by the UI."""

        return self._window.retained_count

    def render_entries(self, entries: Iterable[HistoricalTranscriptEntry]) -> None:
        """Append persisted entries received with the current history page."""

        self._append_entries(tuple(entries))

    def record_live_message(self, role: Literal["user", "assistant"], content: str) -> None:
        """Remember a live persisted message so a durable reload does not duplicate it."""

        if content:
            self._append_live_entry(_LiveHistoryEntry(kind="message", role=role, content=content))

    def record_live_tool_call(self, tool_call_id: str) -> None:
        """Remember a pending live tool card as the durable history page would render it."""

        self._append_live_entry(_LiveHistoryEntry(kind="tool", tool_call_id=tool_call_id))

    def record_live_tool_result(self, tool_call_id: str) -> None:
        """Replace a pending live tool card identity with its persisted result identity."""

        self._live_entries = [
            entry
            for entry in self._live_entries
            if not (entry.kind == "tool" and entry.tool_call_id == tool_call_id)
        ]
        self._append_live_entry(_LiveHistoryEntry(kind="tool", tool_call_id=tool_call_id))

    def replace_entries(
        self,
        entries: Iterable[HistoricalTranscriptEntry],
        *,
        session_label: str,
    ) -> None:
        """Replace all retained history for a newly selected session."""

        self._clear()
        self._surface.replace_transcript()
        self._append_entries(tuple(entries))
        self._surface.mount_history_marker(
            f"resumed session: {session_label}",
            before=next(iter(self._widgets.values()), None),
        )

    def prepend_entries(self, entries: Iterable[HistoricalTranscriptEntry]) -> None:
        """Prepend one durable older-history page and preserve its viewport anchor."""

        retained = self._retain(entries)
        self._surface.begin_history_prepend()
        self._surface.begin_history_render()
        try:
            self._discard_entries(self._window.prepend(retained))
            # A durable page arrived because the reader is already at the top.
            # Reveal its leading slice now so an exhausted page cursor never hides
            # fetched entries behind Transcript's durable-page request gate.
            if self._surface.history_is_at_top():
                self._window.shift_older()
            self._reconcile()
        finally:
            self._surface.finish_history_render()
            self._surface.finish_history_prepend()

    def shift_older(self) -> bool:
        """Move the mounted window toward older retained history."""

        if not self._window.shift_older():
            return False
        self._surface.begin_history_prepend()
        self._surface.begin_history_render()
        try:
            self._reconcile()
        finally:
            self._surface.finish_history_render()
            self._surface.finish_history_prepend()
        return True

    def show_latest(self) -> bool:
        """Move the mounted window to the newest retained history."""

        if not self._window.latest_is_retained:
            if self._surface.request_latest_history():
                return True
        if not self._window.show_latest():
            return False
        self._surface.begin_history_render()
        try:
            self._reconcile()
            self._surface.follow_transcript_tail_after_refresh()
        finally:
            self._surface.finish_history_render()
        return True

    def replace_latest_entries(self, entries: Iterable[HistoricalTranscriptEntry]) -> None:
        """Replace evicted history with a newly loaded durable latest page."""

        reloaded_entries = self._exclude_live_tail(tuple(entries))
        self._surface.begin_history_render()
        try:
            self._remove_historical_widgets()
            self._clear(clear_live=False)
            self._window.replace(self._retain(reloaded_entries))
            self._reconcile()
            self._surface.follow_transcript_tail_after_refresh()
        finally:
            self._surface.finish_history_render()

    def _clear(self, *, clear_live: bool = True) -> None:
        self._historical_tool_results.clear()
        self._resolved_boundary_results.clear()
        self._boundary_result_calls.clear()
        self._window.clear()
        self._widgets.clear()
        self._next_entry_id = 0
        if clear_live:
            self._live_entries.clear()

    def _append_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
        self._surface.begin_history_render()
        try:
            following = self._surface.history_is_following()
            self._discard_entries(self._window.append(self._retain(entries), follow_tail=following))
            self._reconcile()
            if following:
                self._surface.follow_transcript_tail_after_refresh()
        finally:
            self._surface.finish_history_render()

    def _retain(
        self, entries: Iterable[HistoricalTranscriptEntry]
    ) -> tuple[_RetainedHistoryEntry, ...]:
        retained = tuple(
            _RetainedHistoryEntry(id=self._next_entry_id + index, entry=entry)
            for index, entry in enumerate(entries)
        )
        self._next_entry_id += len(retained)
        return retained

    def _remove_historical_widgets(self) -> None:
        for widget in set(self._widgets.values()):
            self._surface.remove_historical_widget(widget)

    def _append_live_entry(self, entry: _LiveHistoryEntry) -> None:
        self._live_entries.append(entry)
        overflow = len(self._live_entries) - TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT
        if overflow > 0:
            del self._live_entries[:overflow]

    def _exclude_live_tail(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
    ) -> tuple[HistoricalTranscriptEntry, ...]:
        """Drop the durable suffix already visible as live transcript output."""

        end = len(entries)
        live_end = len(self._live_entries)
        while end and live_end:
            if not _history_entry_matches_live(entries[end - 1], self._live_entries[live_end - 1]):
                break
            end -= 1
            live_end -= 1
        return entries[:end]

    def _discard_entries(self, entries: Iterable[_RetainedHistoryEntry]) -> None:
        """Release pairing state that only referenced evicted history entries."""

        card_ids = {
            item.entry.card_id for item in entries if isinstance(item.entry, HistoricalToolCard)
        }
        if not card_ids:
            return
        for tool_call_id, results in tuple(self._historical_tool_results.items()):
            retained = deque(
                (card_id, result) for card_id, result in results if card_id not in card_ids
            )
            if retained:
                self._historical_tool_results[tool_call_id] = retained
            else:
                del self._historical_tool_results[tool_call_id]
        self._resolved_boundary_results = {
            card_id: result
            for card_id, result in self._resolved_boundary_results.items()
            if card_id not in card_ids and result.card_id not in card_ids
        }
        self._boundary_result_calls = {
            result_card_id: call_card_id
            for result_card_id, call_card_id in self._boundary_result_calls.items()
            if result_card_id not in card_ids and call_card_id not in card_ids
        }

    def _reconcile(self) -> None:
        """Apply only the changed edges of the retained history window."""

        visible = self._window.visible
        visible_ids = {item.id for item in visible}
        self._surface.set_history_window_available(has_older=not self._window.is_at_oldest)
        for item_id, widget in tuple(self._widgets.items()):
            if item_id not in self._widgets:
                continue
            if item_id not in visible_ids:
                aliases = [
                    other_id
                    for other_id, other_widget in self._widgets.items()
                    if other_widget is widget
                ]
                if any(other_id in visible_ids for other_id in aliases):
                    continue
                if len(aliases) > 1:
                    if item_id != min(aliases):
                        del self._widgets[item_id]
                        continue
                    for alias in aliases:
                        del self._widgets[alias]
                else:
                    del self._widgets[item_id]
                self._surface.remove_historical_widget(widget)

        for index, item in enumerate(visible):
            if item.id in self._widgets:
                continue
            before = next(
                (
                    self._widgets[later.id]
                    for later in visible[index + 1 :]
                    if later.id in self._widgets
                ),
                None,
            )
            if before is None:
                before = self._surface.history_insertion_boundary(set(self._widgets.values()))
            mounted = self._mount_entry(item.entry, before=before)
            if mounted is not None:
                self._widgets[item.id] = mounted

    def _mount_entry(
        self,
        entry: HistoricalTranscriptEntry,
        *,
        before: Widget | None,
    ) -> Widget | None:
        if isinstance(entry, HistoricalTranscriptMessage):
            role = "user" if entry.role == "user" else "assistant"
            return self._surface.mount_historical_line(role, entry.content, before=before)
        return self._mount_tool_card(entry, before=before)

    def _mount_tool_card(
        self,
        entry: HistoricalToolCard,
        *,
        before: Widget | None = None,
    ) -> Widget | None:
        paired_call_id = self._boundary_result_calls.get(entry.card_id)
        if paired_call_id is not None:
            card = self._surface.historical_tool_card(paired_call_id)
            if card is not None:
                return card
            card = self._surface.mount_tool_call(
                entry.card_id,
                entry.name,
                entry.arguments,
                historical_card_id=entry.card_id,
                historical=True,
                before=before,
            )
            self._apply_tool_result(entry.card_id, entry)
            return card

        tool_call_id = entry.tool_call_id
        if entry.missing_result and tool_call_id is not None:
            result = self._resolved_boundary_results.get(entry.card_id)
            if result is not None:
                card = self._surface.historical_tool_card(result.card_id)
                if card is not None:
                    self._enrich_tool_result(
                        result.card_id,
                        result,
                        name=entry.name,
                        arguments=entry.arguments,
                    )
                else:
                    card = self._surface.mount_tool_call(
                        entry.card_id,
                        entry.name,
                        entry.arguments,
                        historical_card_id=entry.card_id,
                        historical=True,
                        before=before,
                    )
                    if card is not None:
                        status, detail, full_output, truncated = self._tool_presentation(
                            result,
                            name=entry.name,
                            arguments=entry.arguments,
                        )
                        self._surface.resolve_tool_call(
                            entry.card_id,
                            status,
                            detail=detail,
                            full_output=full_output,
                            truncated=truncated,
                        )
                if card is not None:
                    self._boundary_result_calls[result.card_id] = entry.card_id
                return card

            results = self._historical_tool_results.get(tool_call_id)
            if results:
                result_card_id, result = results[0]
                if self._enrich_tool_result(
                    result_card_id,
                    result,
                    name=entry.name,
                    arguments=entry.arguments,
                ):
                    results.popleft()
                    if not results:
                        del self._historical_tool_results[tool_call_id]
                    self._resolved_boundary_results[entry.card_id] = result
                    self._boundary_result_calls[result.card_id] = entry.card_id
                    return self._surface.historical_tool_card(result_card_id)

        card = self._surface.mount_tool_call(
            entry.card_id,
            entry.name,
            entry.arguments,
            historical_card_id=entry.card_id if entry.call_missing else None,
            historical=True,
            before=before,
        )
        if entry.call_missing and tool_call_id is not None:
            self._historical_tool_results.setdefault(tool_call_id, deque()).append(
                (entry.card_id, entry)
            )
        self._apply_tool_result(entry.card_id, entry)
        return card

    def _enrich_tool_result(
        self,
        card_id: str,
        result: HistoricalToolCard,
        *,
        name: str,
        arguments: JsonObject,
    ) -> bool:
        status, detail, full_output, truncated = self._tool_presentation(
            result,
            name=name,
            arguments=arguments,
        )
        return self._surface.enrich_historical_tool_call(
            card_id,
            name,
            arguments,
            status=status,
            detail=detail,
            full_output=full_output,
            truncated=truncated,
        )

    def _apply_tool_result(self, card_id: str, entry: HistoricalToolCard) -> None:
        status, detail, full_output, truncated = self._tool_presentation(entry)
        self._surface.resolve_tool_call(
            card_id,
            status,
            detail=detail,
            full_output=full_output,
            truncated=truncated,
        )

    def _tool_presentation(
        self,
        entry: HistoricalToolCard,
        *,
        name: str | None = None,
        arguments: JsonObject | None = None,
    ) -> tuple[str, str | Content | DiffPresentation, str, bool]:
        status = historical_tool_status(entry)
        if status in {"cancelled", "denied"}:
            return status, entry.output, "", False
        resolved_name = name or entry.name
        resolved_arguments = entry.arguments if arguments is None else arguments
        return (
            status,
            render_tool_result(
                resolved_name,
                resolved_arguments,
                entry.output,
                is_error=entry.is_error,
                exit_code=entry.exit_code,
                output_has_exit_status=entry.output_has_exit_status,
                before_text=entry.before_text,
                created=entry.created,
                summary=entry.summary,
            ),
            full_tool_output_for_display(
                entry.output,
                entry.exit_code,
                output_has_exit_status=entry.output_has_exit_status,
            ),
            entry.truncated,
        )


def _history_entry_matches_live(
    entry: HistoricalTranscriptEntry,
    live: _LiveHistoryEntry,
) -> bool:
    if isinstance(entry, HistoricalToolCard):
        return live.kind == "tool" and entry.tool_call_id == live.tool_call_id
    if live.kind != "message" or entry.role != live.role or live.content is None:
        return False
    if entry.content == live.content:
        return True
    truncated_suffix = "[content truncated]"
    if not entry.content.endswith(truncated_suffix):
        return False
    content_prefix = entry.content[: -len(truncated_suffix)].rstrip("\n")
    return live.content.startswith(content_prefix)


__all__ = ["TextualHistoryController", "TextualHistorySurface"]
