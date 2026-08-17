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
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from textual.content import Content
from textual.widget import Widget

from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import JsonObject
from wisp.tui.diff_presentation import DiffPresentation
from wisp.tui.history import (
    HistoricalSkillInvocation,
    HistoricalToolCard,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    historical_tool_status,
)
from wisp.tui.process_lifecycle import (
    ProcessLifecycle,
    ProcessLifecyclePresentation,
    historical_process_observation,
    process_call_identity,
)
from wisp.tui.skills import format_skill_invocation
from wisp.tui.tool_call import ToolActionStatus
from wisp.tui.tool_output import full_tool_result_for_display, render_tool_result
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

    def mount_process_card(
        self,
        process_id: str,
        *,
        historical: bool = False,
        before: Widget | None = None,
        reposition: bool = False,
    ) -> Widget | None: ...

    def update_historical_process_card(
        self,
        card: Widget,
        presentation: ProcessLifecyclePresentation,
    ) -> Widget | None: ...

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
    ) -> Widget | None: ...

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: JsonObject,
        *,
        status: ToolActionStatus,
        detail: str | Content | DiffPresentation,
        full_output: str,
        truncated: bool,
    ) -> bool: ...

    def resolve_tool_call(
        self,
        call_id: str,
        status: ToolActionStatus,
        *,
        detail: str | Content | DiffPresentation = "",
        full_output: str = "",
        truncated: bool = False,
    ) -> Widget | None: ...

    def historical_tool_card(self, card_id: str) -> Widget | None: ...


@dataclass(frozen=True)
class _RetainedHistoryEntry:
    """One UI-local identity for a persisted transcript entry."""

    id: int
    entry: HistoricalTranscriptEntry


@dataclass(frozen=True)
class _BoundaryToolCall:
    """Call metadata retained while its paired result remains pageable."""

    name: str
    arguments: JsonObject


@dataclass(frozen=True)
class _HistoricalProcessGroup:
    """One process lifecycle projected from visible audited history entries."""

    first_entry_id: int
    member_entry_ids: frozenset[int]
    presentation: ProcessLifecyclePresentation


@dataclass(frozen=True)
class _LiveHistoryEntry:
    """One persisted entry already represented by a live transcript widget."""

    kind: Literal["message", "tool"]
    role: Literal["user", "assistant"] | None = None
    content: str | None = None
    message_entry_id: str | None = None
    tool_call_id: str | None = None
    widget: Widget | None = None


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
        self._boundary_tool_calls: dict[str, _BoundaryToolCall] = {}
        self._window = TranscriptWindow[_RetainedHistoryEntry](retained_capacity=retained_capacity)
        self._widgets: dict[int, Widget] = {}
        self._transferred_history_entry_ids: dict[Widget, set[int]] = {}
        self._transferred_history_entries: dict[Widget, list[HistoricalTranscriptEntry]] = {}
        self._next_entry_id = 0
        self._live_entries: list[_LiveHistoryEntry] = []
        self._latest_reload_live_entries: tuple[_LiveHistoryEntry, ...] | None = None

    @property
    def retained_entry_count(self) -> int:
        """Return the number of history entries retained by the UI."""

        return self._window.retained_count

    def render_entries(self, entries: Iterable[HistoricalTranscriptEntry]) -> None:
        """Append persisted entries received with the current history page."""

        self._append_entries(tuple(entries))

    def record_live_message(
        self,
        role: Literal["user", "assistant"],
        content: str,
        *,
        widget: Widget | None = None,
    ) -> None:
        """Remember a live persisted message so a durable reload does not duplicate it."""

        if content:
            self._append_live_entry(
                _LiveHistoryEntry(kind="message", role=role, content=content, widget=widget)
            )

    def discard_live_message(self, role: Literal["user", "assistant"], content: str) -> None:
        """Forget a live message that did not become a durable session entry."""

        for index in range(len(self._live_entries) - 1, -1, -1):
            entry = self._live_entries[index]
            if entry.kind == "message" and entry.role == role and entry.content == content:
                del self._live_entries[index]
                return

    def record_live_skill_invocation(self, message_entry_id: str, original_content: str) -> None:
        """Attach persisted identity to the newest matching live skill prompt."""

        matched_entry = None
        updated_entry = None
        for index in range(len(self._live_entries) - 1, -1, -1):
            entry = self._live_entries[index]
            if (
                entry.kind == "message"
                and entry.role == "user"
                and entry.content == original_content
            ):
                matched_entry = entry
                updated_entry = replace(entry, message_entry_id=message_entry_id)
                self._live_entries[index] = updated_entry
                break

        snapshot = self._latest_reload_live_entries
        if snapshot is not None and matched_entry is not None and updated_entry is not None:
            self._latest_reload_live_entries = tuple(
                updated_entry if entry is matched_entry else entry for entry in snapshot
            )

    def record_live_tool_call(self, tool_call_id: str, *, widget: Widget | None = None) -> None:
        """Remember a pending live tool card as the durable history page would render it."""

        self._append_live_entry(
            _LiveHistoryEntry(kind="tool", tool_call_id=tool_call_id, widget=widget)
        )

    def record_live_tool_result(self, tool_call_id: str, *, widget: Widget | None = None) -> None:
        """Replace a pending live tool card identity with its persisted result identity."""

        prior_widget = next(
            (
                entry.widget
                for entry in reversed(self._live_entries)
                if entry.kind == "tool" and entry.tool_call_id == tool_call_id
            ),
            None,
        )
        self._live_entries = [
            entry
            for entry in self._live_entries
            if not (entry.kind == "tool" and entry.tool_call_id == tool_call_id)
        ]
        self._append_live_entry(
            _LiveHistoryEntry(
                kind="tool",
                tool_call_id=tool_call_id,
                widget=widget or prior_widget,
            )
        )

    def transfer_widget_to_live(self, widget: Widget) -> None:
        """Detach historical aliases when a resumed process card becomes live-owned."""

        transferred_entry_ids = {
            entry_id for entry_id, candidate in self._widgets.items() if candidate is widget
        }
        if transferred_entry_ids:
            self._transferred_history_entry_ids.setdefault(widget, set()).update(
                transferred_entry_ids
            )
            retained_by_id = {item.id: item.entry for item in self._window.entries}
            transferred_entries = self._transferred_history_entries.setdefault(widget, [])
            transferred_entries.extend(
                retained_by_id[entry_id]
                for entry_id in sorted(transferred_entry_ids)
                if entry_id in retained_by_id
            )
        self._widgets = {
            entry_id: candidate
            for entry_id, candidate in self._widgets.items()
            if candidate is not widget
        }

    def forget_live_widget(self, widget: Widget) -> None:
        """Allow an evicted live entry to reappear through durable history paging."""

        self._live_entries = [entry for entry in self._live_entries if entry.widget is not widget]
        self._transferred_history_entry_ids.pop(widget, None)
        self._transferred_history_entries.pop(widget, None)
        snapshot = self._latest_reload_live_entries
        if snapshot is not None:
            self._latest_reload_live_entries = tuple(
                entry for entry in snapshot if entry.widget is not widget
            )

    def clear_entries(self) -> None:
        """Clear retained and live transcript state for a fresh session."""

        self._clear()
        self._surface.replace_transcript()

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

    def show_oldest(self) -> bool:
        """Move the mounted window to the oldest retained history."""

        if not self._window.show_oldest():
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

    def replace_latest_entries(self, entries: Iterable[HistoricalTranscriptEntry]) -> bool:
        """Replace evicted history with a newly loaded durable latest page.

        Return ``False`` when the reader left the tail while the page was in
        flight, so the caller can defer replacement until they return.
        """

        if not self._surface.history_is_following():
            self._latest_reload_live_entries = None
            return False

        snapshot = self._latest_reload_live_entries
        live_entries = snapshot if snapshot is not None else tuple(self._live_entries)
        self._latest_reload_live_entries = None
        reloaded_entries = self._exclude_live_tail(tuple(entries), live_entries=live_entries)
        self._surface.begin_history_render()
        try:
            self._remove_historical_widgets()
            self._clear(clear_live=False)
            retained = self._retain(reloaded_entries)
            self._remap_transferred_history_entry_ids(retained)
            self._window.replace(retained)
            self._reconcile()
            self._surface.follow_transcript_tail_after_refresh()
        finally:
            self._surface.finish_history_render()
        return True

    def recover_evicted_entries(self, entries: Iterable[HistoricalTranscriptEntry]) -> bool:
        """Insert the durable prefix missing from a bounded live transcript.

        A tail reload replaces the retained window only while the reader follows
        live output. When the reader has moved upward, doing that would yank the
        viewport back to the tail. Instead retain just the durable entries that
        precede the surviving live suffix and mount them before that suffix.
        """

        snapshot = self._latest_reload_live_entries
        live_entries = snapshot if snapshot is not None else tuple(self._live_entries)
        self._latest_reload_live_entries = None
        recovered = self._exclude_live_tail(tuple(entries), live_entries=live_entries)
        recovered = self._exclude_retained_overlap(recovered)
        if not recovered:
            return True
        if len(recovered) > self._window.visible_append_capacity:
            # Keep the reader's mounted slice stable. Entries appended beyond it
            # would remain invisible while falsely completing recovery; returning
            # to live output will instead perform the deferred latest-page reload.
            return False
        if (
            self._window.is_at_oldest
            and len(self._window.entries) + len(recovered) > self._window.retained_capacity
        ):
            # The reader is viewing the oldest retained entries. Appending a
            # latest-page prefix here would evict that exact edge and replace the
            # anchor under their viewport; defer tail reconciliation instead.
            return False
        self._surface.begin_history_prepend()
        self._surface.begin_history_render()
        try:
            self._discard_entries(self._window.append(self._retain(recovered), follow_tail=False))
            self._reconcile()
        finally:
            self._surface.finish_history_render()
            self._surface.finish_history_prepend()
        return True

    def capture_latest_reload_live_entries(self) -> None:
        """Capture live output at the point the durable latest-page request starts."""

        self._latest_reload_live_entries = tuple(self._live_entries)

    def _clear(self, *, clear_live: bool = True) -> None:
        self._historical_tool_results.clear()
        self._resolved_boundary_results.clear()
        self._boundary_result_calls.clear()
        self._boundary_tool_calls.clear()
        self._window.clear()
        self._widgets.clear()
        self._next_entry_id = 0
        if clear_live:
            self._live_entries.clear()
            self._transferred_history_entry_ids.clear()
            self._transferred_history_entries.clear()
        else:
            self._transferred_history_entry_ids.clear()
        self._latest_reload_live_entries = None

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

    def _remap_transferred_history_entry_ids(
        self,
        retained: tuple[_RetainedHistoryEntry, ...],
    ) -> None:
        """Remap live-owned records after a latest-page rebuild resets local ids."""

        for widget, transferred_entries in self._transferred_history_entries.items():
            available = list(retained)
            remapped: set[int] = set()
            for transferred in reversed(transferred_entries):
                for index in range(len(available) - 1, -1, -1):
                    candidate = available[index]
                    if not _same_durable_history_entry(candidate.entry, transferred):
                        continue
                    remapped.add(candidate.id)
                    del available[index]
                    break
            if remapped:
                self._transferred_history_entry_ids[widget] = remapped

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
        *,
        live_entries: tuple[_LiveHistoryEntry, ...],
    ) -> tuple[HistoricalTranscriptEntry, ...]:
        """Drop the durable suffix already visible as live transcript output."""

        end = len(entries)
        live_end = len(live_entries)
        while end and live_end:
            if not _history_entry_matches_live(entries[end - 1], live_entries[live_end - 1]):
                break
            end -= 1
            live_end -= 1
        return entries[:end]

    def _exclude_retained_overlap(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
    ) -> tuple[HistoricalTranscriptEntry, ...]:
        """Drop the recovered prefix already contiguous with retained history."""

        retained = tuple(item.entry for item in self._window.entries)
        for overlap in range(min(len(entries), len(retained)), 0, -1):
            if all(
                _history_entry_id(left) is not None
                and _history_entry_id(left) == _history_entry_id(right)
                for left, right in zip(retained[-overlap:], entries[:overlap], strict=True)
            ):
                return entries[overlap:]
        return entries

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
        # The call can age out before its newer result. Keep the pairing snapshot
        # until the result itself leaves retention so a later remount preserves the
        # semantic header and argument-dependent diff.
        self._boundary_result_calls = {
            result_card_id: call_card_id
            for result_card_id, call_card_id in self._boundary_result_calls.items()
            if result_card_id not in card_ids
        }
        self._boundary_tool_calls = {
            result_card_id: tool_call
            for result_card_id, tool_call in self._boundary_tool_calls.items()
            if result_card_id not in card_ids
        }

    def _reconcile(self) -> None:
        """Apply only the changed edges of the retained history window."""

        visible = self._window.visible
        live_owned_history_entry_ids = (
            set().union(*self._transferred_history_entry_ids.values())
            if self._transferred_history_entry_ids
            else set()
        )
        process_groups = self._historical_process_groups(
            visible,
            excluded_entry_ids=live_owned_history_entry_ids,
        )
        visible_ids = {item.id for item in visible}
        reposition_widgets: set[Widget] = set()
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
                    del self._widgets[item_id]
                    reposition_widgets.add(widget)
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
            if item.id in live_owned_history_entry_ids:
                continue
            process_group = process_groups.get(item.id)
            if item.id in self._widgets:
                if process_group is not None and item.id == process_group.first_entry_id:
                    widget = self._widgets[item.id]
                    if widget in reposition_widgets:
                        before = next(
                            (
                                self._widgets[later.id]
                                for later in visible[index + 1 :]
                                if later.id in self._widgets
                                and self._widgets[later.id] is not widget
                            ),
                            None,
                        )
                        if before is None:
                            before = self._surface.history_insertion_boundary(
                                set(self._widgets.values())
                            )
                        self._surface.mount_process_card(
                            process_group.presentation.process_id,
                            historical=True,
                            before=before,
                            reposition=True,
                        )
                    self._surface.update_historical_process_card(
                        widget,
                        process_group.presentation,
                    )
                continue
            if process_group is not None and item.id != process_group.first_entry_id:
                first_widget = self._widgets.get(process_group.first_entry_id)
                if first_widget is not None:
                    self._widgets[item.id] = first_widget
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
            if process_group is None:
                mounted = self._mount_entry(item.entry, before=before)
            else:
                mounted = self._surface.mount_process_card(
                    process_group.presentation.process_id,
                    historical=True,
                    before=before,
                )
                if mounted is not None:
                    self._surface.update_historical_process_card(
                        mounted,
                        process_group.presentation,
                    )
                    superseded = {
                        self._widgets[member_id]
                        for member_id in process_group.member_entry_ids
                        if member_id in self._widgets and self._widgets[member_id] is not mounted
                    }
                    for widget in superseded:
                        self._surface.remove_historical_widget(widget)
                    if superseded:
                        self._widgets = {
                            entry_id: widget
                            for entry_id, widget in self._widgets.items()
                            if widget not in superseded
                        }
            if mounted is not None:
                self._widgets[item.id] = mounted
                if process_group is not None:
                    for member_id in process_group.member_entry_ids:
                        self._widgets[member_id] = mounted

    @staticmethod
    def _historical_process_groups(
        visible: tuple[_RetainedHistoryEntry, ...],
        *,
        excluded_entry_ids: set[int] | None = None,
    ) -> dict[int, _HistoricalProcessGroup]:
        """Project visible poll/cancel records into stable process-level groups."""

        excluded_entry_ids = excluded_entry_ids or set()
        split_results: dict[str, deque[_RetainedHistoryEntry]] = {}
        for item in visible:
            entry = item.entry
            if item.id in excluded_entry_ids:
                continue
            if (
                isinstance(entry, HistoricalToolCard)
                and entry.call_missing
                and entry.tool_call_id is not None
            ):
                split_results.setdefault(entry.tool_call_id, deque()).append(item)
        paired_results: dict[int, _RetainedHistoryEntry] = {}
        paired_result_ids: set[int] = set()
        for item in visible:
            entry = item.entry
            if item.id in excluded_entry_ids:
                continue
            if (
                isinstance(entry, HistoricalToolCard)
                and entry.missing_result
                and entry.tool_call_id is not None
                and (results := split_results.get(entry.tool_call_id))
            ):
                result = results.popleft()
                paired_results[item.id] = result
                paired_result_ids.add(result.id)
        lifecycles: dict[str, ProcessLifecycle] = {}
        first_ids: dict[str, int] = {}
        member_ids: dict[str, set[int]] = {}
        for item in visible:
            entry = item.entry
            if (
                not isinstance(entry, HistoricalToolCard)
                or item.id in excluded_entry_ids
                or item.id in paired_result_ids
            ):
                continue
            observation = entry
            observation_member_ids = {item.id}
            if entry.missing_result:
                paired_result = paired_results.get(item.id)
                if paired_result is not None and isinstance(
                    paired_result.entry,
                    HistoricalToolCard,
                ):
                    observation = paired_result.entry
                    observation_member_ids.add(paired_result.id)
            identity = process_call_identity(entry.name, entry.arguments)
            if identity is None:
                continue
            lifecycle = lifecycles.setdefault(
                identity.process_id,
                ProcessLifecycle(identity.process_id),
            )
            first_ids.setdefault(identity.process_id, item.id)
            member_ids.setdefault(identity.process_id, set()).update(observation_member_ids)
            lifecycle.begin(identity.operation)
            historical_status = historical_tool_status(observation)
            if historical_status == "denied":
                lifecycle.deny(identity.operation, observation.output or "denied")
            elif historical_status == "cancelled" and (
                observation.missing_result or observation.output == INTERRUPTED_TOOL_RESULT_TEXT
            ):
                lifecycle.interrupt(identity.operation)
            else:
                state, output = historical_process_observation(
                    identity.process_id,
                    observation.output,
                )
                lifecycle.observe(
                    operation=identity.operation,
                    state=state,
                    fallback_output=output,
                    source_truncated=observation.truncated,
                    failed=historical_status == "error",
                )

        by_entry_id: dict[int, _HistoricalProcessGroup] = {}
        for process_id, lifecycle in lifecycles.items():
            group_members = frozenset(member_ids[process_id])
            group = _HistoricalProcessGroup(
                first_entry_id=first_ids[process_id],
                member_entry_ids=group_members,
                presentation=lifecycle.presentation(),
            )
            for member_id in group_members:
                by_entry_id[member_id] = group
        return by_entry_id

    def _mount_entry(
        self,
        entry: HistoricalTranscriptEntry,
        *,
        before: Widget | None,
    ) -> Widget | None:
        if isinstance(entry, HistoricalTranscriptMessage):
            role = "user" if entry.role == "user" else "assistant"
            return self._surface.mount_historical_line(role, entry.content, before=before)
        if isinstance(entry, HistoricalSkillInvocation):
            return self._surface.mount_historical_line(
                "user",
                format_skill_invocation(
                    entry.name,
                    entry.request,
                    request_truncated=entry.request_truncated,
                    instructions_truncated=entry.instructions_truncated,
                ),
                before=before,
            )
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
            paired_call = self._boundary_tool_calls.get(entry.card_id)
            resolved_name = paired_call.name if paired_call is not None else entry.name
            resolved_arguments = (
                paired_call.arguments if paired_call is not None else entry.arguments
            )
            card = self._surface.mount_tool_call(
                entry.card_id,
                resolved_name,
                resolved_arguments,
                historical_card_id=entry.card_id,
                historical=True,
                arguments_available=paired_call is not None or not entry.call_missing,
                before=before,
            )
            self._apply_tool_result(
                entry.card_id,
                entry,
                name=resolved_name,
                arguments=resolved_arguments,
            )
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
                        arguments_available=not entry.call_missing,
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
                    self._remember_boundary_call(result, entry)
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
                    self._remember_boundary_call(result, entry)
                    return self._surface.historical_tool_card(result_card_id)

        card = self._surface.mount_tool_call(
            entry.card_id,
            entry.name,
            entry.arguments,
            historical_card_id=entry.card_id if entry.call_missing else None,
            historical=True,
            arguments_available=not entry.call_missing,
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

    def _remember_boundary_call(
        self,
        result: HistoricalToolCard,
        call: HistoricalToolCard,
    ) -> None:
        self._boundary_result_calls[result.card_id] = call.card_id
        self._boundary_tool_calls[result.card_id] = _BoundaryToolCall(
            name=call.name,
            arguments=dict(call.arguments),
        )

    def _apply_tool_result(
        self,
        card_id: str,
        entry: HistoricalToolCard,
        *,
        name: str | None = None,
        arguments: JsonObject | None = None,
    ) -> None:
        status, detail, full_output, truncated = self._tool_presentation(
            entry,
            name=name,
            arguments=arguments,
        )
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
    ) -> tuple[ToolActionStatus, str | Content | DiffPresentation, str, bool]:
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
            full_tool_result_for_display(
                resolved_name,
                entry.output,
                entry.exit_code,
                output_has_exit_status=entry.output_has_exit_status,
                summary=entry.summary if status == "done" else None,
            ),
            entry.truncated,
        )


def _history_entry_id(entry: HistoricalTranscriptEntry) -> str | None:
    if isinstance(entry, HistoricalTranscriptMessage):
        return entry.entry_id
    if isinstance(entry, HistoricalSkillInvocation):
        return entry.entry_id
    return entry.card_id


def _same_durable_history_entry(
    left: HistoricalTranscriptEntry,
    right: HistoricalTranscriptEntry,
) -> bool:
    """Match one persisted projection across destructive latest-page reloads."""

    if type(left) is not type(right):
        return False
    if isinstance(left, HistoricalTranscriptMessage) and isinstance(
        right, HistoricalTranscriptMessage
    ):
        if left.entry_id is not None or right.entry_id is not None:
            return left.entry_id == right.entry_id
        return left == right
    if isinstance(left, HistoricalSkillInvocation) and isinstance(right, HistoricalSkillInvocation):
        return left.entry_id == right.entry_id
    if isinstance(left, HistoricalToolCard) and isinstance(right, HistoricalToolCard):
        if left.missing_result or right.missing_result:
            return left == right and left.tool_call_id == right.tool_call_id
        return left.card_id == right.card_id
    return False


def _history_entry_matches_live(
    entry: HistoricalTranscriptEntry,
    live: _LiveHistoryEntry,
) -> bool:
    if isinstance(entry, HistoricalToolCard):
        return live.kind == "tool" and entry.tool_call_id == live.tool_call_id
    if isinstance(entry, HistoricalSkillInvocation):
        if live.kind != "message" or live.role != "user" or live.content is None:
            return False
        if live.message_entry_id is not None:
            return entry.entry_id == live.message_entry_id
        if not entry.original_content_truncated:
            return entry.original_content == live.content
        return bool(entry.original_content) and live.content.startswith(entry.original_content)
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
