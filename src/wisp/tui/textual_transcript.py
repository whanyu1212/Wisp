"""Transient live-transcript presentation state for the Textual frontend.

Import direction is intentionally one-way::

    textual_app -> textual_transcript -> widgets / diff_presentation

This controller owns only live transcript presentation state: tool-card identity,
working indicators, unseen-output tracking, and the follow intent associated with
a focused tool card. It does not import the app, renderer, shell, RPC, sessions,
providers, persisted-history controller, or Markdown stream controller.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from textual.content import Content
from textual.widget import Widget

from wisp.tui.diff_presentation import DiffPresentation
from wisp.tui.history import TUI_HISTORY_PAGE_LIMIT
from wisp.tui.process_lifecycle import ProcessLifecyclePresentation
from wisp.tui.tool_call import ToolActionStatus
from wisp.tui.widgets import ProcessCard, ToolCard, WorkingIndicator

# Keep an evicted widget inside the next durable latest-page reload, whose first
# entry falls immediately before this bounded visible live suffix.
TUI_SETTLED_LIVE_WIDGET_LIMIT = TUI_HISTORY_PAGE_LIMIT - 1
# A settled ToolCard represents both its call and result. Keep enough spare
# history-page capacity that resolving one cannot evict an unrecoverable row.
TUI_SETTLED_LIVE_DURABLE_ENTRY_LIMIT = TUI_HISTORY_PAGE_LIMIT - 2


class TextualTranscriptSurface(Protocol):
    """Minimal Textual effects needed by :class:`TextualTranscriptController`."""

    def transcript_available(self) -> bool:
        """Return whether a transcript widget is currently mounted."""

    def mount_live_transcript_widget(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> None:
        """Mount one live widget into the transcript."""

    def remove_live_transcript_widget(self, widget: Widget) -> None:
        """Remove one live widget without propagating a stale unmount failure."""

    def move_live_transcript_widget(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> None:
        """Move an existing lifecycle card to a newly discovered history boundary."""

    def transcript_is_following(self) -> bool:
        """Return whether the reader currently follows the transcript tail."""

    def follow_transcript_tail_after_refresh(self) -> None:
        """Schedule tail following after the current Textual refresh."""

    def record_live_transcript_update(self, widget: Widget) -> None:
        """Record a live update after app-owned history-prepend gating."""

    def return_transcript_to_latest(self) -> None:
        """Return the reader to the latest transcript content."""

    def is_newest_transcript_widget(self, widget: Widget) -> bool:
        """Return whether ``widget`` is the current transcript tail."""

    def show_unseen_output(self, count: int) -> None:
        """Show the jump-to-latest affordance for ``count`` distinct updates."""

    def hide_unseen_output(self) -> None:
        """Hide the jump-to-latest affordance."""

    def live_transcript_widget_evicted(self, widget: Widget) -> None:
        """Forget a durable live identity after its widget leaves the transcript."""


class TextualTranscriptController:
    """Own transient live transcript cards, activity, and follow presentation.

    Persisted history remains with :class:`TextualHistoryController`; native
    Markdown streaming remains with :class:`MarkdownStreamController`. This class
    deliberately owns no event/RPC interpretation: callers provide already-derived
    tool-card state and route Textual events into its narrow lifecycle methods.
    """

    def __init__(
        self,
        surface: TextualTranscriptSurface,
        *,
        settled_capacity: int = TUI_SETTLED_LIVE_WIDGET_LIMIT,
        durable_entry_capacity: int = TUI_SETTLED_LIVE_DURABLE_ENTRY_LIMIT,
    ) -> None:
        if settled_capacity < 1:
            raise ValueError("settled_capacity must be positive")
        if durable_entry_capacity < 1:
            raise ValueError("durable_entry_capacity must be positive")
        self._surface = surface
        self._settled_capacity = settled_capacity
        self._durable_entry_capacity = durable_entry_capacity
        self._settled_widgets: deque[tuple[Widget, int]] = deque()
        self._settled_durable_entry_count = 0
        self._unseen_output: set[Widget] = set()
        self._tool_cards: dict[str, ToolCard] = {}
        self._process_cards: dict[str, ProcessCard] = {}
        self._live_process_cards: set[ProcessCard] = set()
        self._historical_process_cards: dict[str, ProcessCard] = {}
        self._historical_tool_cards: dict[str, ToolCard] = {}
        self._historical_widgets: set[ToolCard] = set()
        self._working_indicator: WorkingIndicator | None = None
        self._working_indicator_generation = 0
        self._card_focus_was_following = False

    @property
    def unseen_output_count(self) -> int:
        """Return the number of distinct transcript widgets unseen by the reader."""

        return len(self._unseen_output)

    @property
    def pending_tool_count(self) -> int:
        """Return the live tool-card registry size."""

        return len(self._tool_cards)

    @property
    def working_indicator(self) -> WorkingIndicator | None:
        """Return the transient activity widget, if one is currently mounted."""

        return self._working_indicator

    @property
    def working_indicator_identity(self) -> tuple[WorkingIndicator, int] | None:
        """Return the current indicator and its logical turn-ownership generation."""

        indicator = self._working_indicator
        if indicator is None:
            return None
        return indicator, self._working_indicator_generation

    @property
    def settled_widget_count(self) -> int:
        """Return the bounded count of completed live transcript widgets."""

        return len(self._settled_widgets)

    def note_update(self, widget: Widget) -> None:
        """Mark one distinct widget unseen unless the reader follows the tail."""

        if self._surface.transcript_is_following():
            return
        self._unseen_output.add(widget)
        self._surface.show_unseen_output(len(self._unseen_output))

    def discard_unseen_output(self, widget: Widget) -> None:
        """Forget one retired widget and reconcile the jump-to-latest badge."""

        if widget not in self._unseen_output:
            return
        self._unseen_output.discard(widget)
        if self._unseen_output:
            self._surface.show_unseen_output(len(self._unseen_output))
        else:
            self._surface.hide_unseen_output()

    def clear_unseen_output(self) -> None:
        """Clear all unseen-output state after the reader returns to the tail."""

        self._unseen_output.clear()
        self._surface.hide_unseen_output()

    def show_working_indicator(self) -> None:
        """Show or refresh the transcript's ordinary working indicator."""

        if not self._surface.transcript_available():
            return
        indicator = self._working_indicator
        if indicator is not None:
            indicator.show_working()
            return
        indicator = WorkingIndicator()
        indicator.restart_working()
        self._mount_working_indicator(indicator)

    def renew_working_indicator(self) -> None:
        """Give an existing heartbeat to a newer model turn without remounting it."""

        self._working_indicator_generation += 1
        self.show_working_indicator()

    def show_retry_indicator(self, label: str) -> None:
        """Show or refresh the transcript's provider-retry indicator."""

        self.show_activity_indicator(label, show_elapsed=False)

    def show_activity_indicator(self, label: str, *, show_elapsed: bool = True) -> None:
        """Show or relabel command activity without resetting its elapsed time."""

        if not self._surface.transcript_available():
            return
        indicator = self._working_indicator
        if indicator is not None:
            indicator.show_activity(label, show_elapsed=show_elapsed)
            return
        indicator = WorkingIndicator()
        indicator.show_activity(label, show_elapsed=show_elapsed)
        self._mount_working_indicator(indicator)

    def restart_working_indicator(self) -> None:
        """Replace any prior activity row for a newly submitted prompt."""

        if not self._surface.transcript_available():
            return
        self.hide_working_indicator()
        self._working_indicator_generation += 1
        indicator = WorkingIndicator()
        indicator.restart_working()
        self._mount_working_indicator(indicator)

    def hide_working_indicator(self) -> None:
        """Remove the transient activity row and its unseen-output identity."""

        indicator = self._working_indicator
        if indicator is not None:
            self.hide_working_indicator_if_current(indicator)

    def hide_working_indicator_if_current(
        self,
        indicator: WorkingIndicator,
        *,
        generation: int | None = None,
    ) -> None:
        """Remove ``indicator`` only if it still belongs to the captured model turn."""

        if self._working_indicator is not indicator:
            return
        if generation is not None and generation != self._working_indicator_generation:
            return
        self._working_indicator = None
        self.discard_unseen_output(indicator)
        self._surface.remove_live_transcript_widget(indicator)

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: object,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        arguments_available: bool = True,
        before: Widget | None = None,
    ) -> ToolCard | None:
        """Mount and register one evolving tool card when a transcript is available."""

        if not self._surface.transcript_available():
            return None
        card = ToolCard(name, arguments, arguments_available=arguments_available)
        self._tool_cards[call_id] = card
        if historical_card_id is not None:
            self._historical_tool_cards[historical_card_id] = card
        if historical:
            # Persisted history owns this card's retention. Classifying it as settled
            # live output can evict it and recursively request the same latest page.
            self._historical_widgets.add(card)
        self._surface.mount_live_transcript_widget(card, before=before)
        self._surface.record_live_transcript_update(card)
        self._surface.follow_transcript_tail_after_refresh()
        return card

    def mount_process_card(
        self,
        process_id: str,
        *,
        historical: bool = False,
        before: Widget | None = None,
        reposition: bool = False,
    ) -> ProcessCard | None:
        """Mount or recover one stable card for a resumable process lifecycle."""

        card = self._process_cards.get(process_id)
        if historical and card in self._live_process_cards:
            # Older pages need their own stable replay card once the originally
            # shared card has transferred to live ownership. This keeps paged output
            # visible without mutating the evolving live lifecycle at the tail.
            historical_card = self._historical_process_cards.get(process_id)
            if historical_card is not None:
                if (reposition or before is not None) and before is not historical_card:
                    self._surface.move_live_transcript_widget(historical_card, before=before)
                return historical_card
            historical_card = ProcessCard(process_id, track_elapsed=False)
            self._historical_process_cards[process_id] = historical_card
            self._historical_widgets.add(historical_card)
            self._surface.mount_live_transcript_widget(historical_card, before=before)
            self._surface.record_live_transcript_update(historical_card)
            self._surface.follow_transcript_tail_after_refresh()
            return historical_card
        if card is not None:
            if historical:
                self._historical_process_cards[process_id] = card
                self._historical_widgets.add(card)
            else:
                self._historical_widgets.discard(card)
                if self._historical_process_cards.get(process_id) is card:
                    del self._historical_process_cards[process_id]
                self._live_process_cards.add(card)
                self._unsettle_widget(card)
                card.start_live_updates()
            if (reposition or before is not None) and before is not card:
                self._surface.move_live_transcript_widget(card, before=before)
            return card
        if not self._surface.transcript_available():
            return None
        card = ProcessCard(process_id, track_elapsed=not historical)
        self._process_cards[process_id] = card
        if historical:
            self._historical_process_cards[process_id] = card
            self._historical_widgets.add(card)
        else:
            self._live_process_cards.add(card)
        self._surface.mount_live_transcript_widget(card, before=before)
        self._surface.record_live_transcript_update(card)
        self._surface.follow_transcript_tail_after_refresh()
        return card

    def mount_process_call(self, call_id: str, process_id: str) -> ProcessCard | None:
        """Alias one audited call ID to its process's shared live card."""

        card = self.mount_process_card(process_id)
        if card is not None:
            self._tool_cards[call_id] = card
        return card

    def update_historical_process_card(
        self,
        card: Widget,
        presentation: ProcessLifecyclePresentation,
    ) -> ProcessCard | None:
        """Update one retained replay card without touching a live card of the same process."""

        if not isinstance(card, ProcessCard):
            return None
        card.set_lifecycle(presentation)
        self._surface.record_live_transcript_update(card)
        self._surface.follow_transcript_tail_after_refresh()
        return card

    def update_process_card(
        self,
        presentation: ProcessLifecyclePresentation,
        *,
        elapsed: float | None = None,
        settle_terminal: bool = False,
    ) -> ProcessCard | None:
        """Replace one process card snapshot and optionally settle its live lifecycle."""

        card = self._process_cards.get(presentation.process_id)
        if card is None:
            return None
        card.set_lifecycle(presentation, elapsed=elapsed)
        self._surface.record_live_transcript_update(card)
        if settle_terminal and presentation.operation_settled:
            if card not in self._historical_widgets:
                # Presentation retention counts widgets, while durable-entry capacity
                # must account for every represented audited call/result pair.
                self.settle_widget(
                    card,
                    durable_entry_count=2 * presentation.call_count,
                )
        self._surface.follow_transcript_tail_after_refresh()
        return card

    def resolve_process_call(
        self,
        call_id: str,
        presentation: ProcessLifecyclePresentation,
        *,
        elapsed: float | None = None,
    ) -> ProcessCard | None:
        """Finish one poll call while preserving its process-level presentation."""

        card = self._tool_cards.pop(call_id, None)
        if not isinstance(card, ProcessCard):
            return None
        has_pending_alias = any(candidate is card for candidate in self._tool_cards.values())
        updated = self.update_process_card(
            presentation,
            elapsed=elapsed,
            settle_terminal=not has_pending_alias,
        )
        if has_pending_alias and presentation.operation_settled:
            # The latest observation may itself be terminal, denied, or failed,
            # while another audited call against the process is still unresolved.
            # Keep elapsed tracking active and defer eviction eligibility until the
            # final alias resolves.
            card.start_live_updates()
        return updated

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: object,
        *,
        status: ToolActionStatus,
        detail: str | Content | DiffPresentation,
        full_output: str,
        truncated: bool,
    ) -> bool:
        """Enrich a retained result card when its paired historical call arrives."""

        card = self._historical_tool_cards.get(card_id)
        if card is None:
            return False
        card.update_call(name, arguments)
        card.set_state(
            status,
            detail=detail,
            full_output=full_output,
            truncated=truncated,
        )
        self._surface.record_live_transcript_update(card)
        self._surface.follow_transcript_tail_after_refresh()
        return True

    def resolve_tool_call(
        self,
        call_id: str,
        status: ToolActionStatus,
        *,
        detail: str | Content | DiffPresentation = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> ToolCard | None:
        """Resolve a registered card in place and retire terminal registry entries."""

        card = self._tool_cards.get(call_id)
        if card is None:
            return None
        card.set_state(
            status,
            detail=detail,
            elapsed=elapsed,
            full_output=full_output,
            truncated=truncated,
        )
        self._surface.record_live_transcript_update(card)
        if status != "pending":
            del self._tool_cards[call_id]
            if card not in self._historical_widgets:
                self.settle_widget(card, durable_entry_count=2)
        self._surface.follow_transcript_tail_after_refresh()
        return card

    def fail_pending_tool_calls(self, detail: str = "cancelled") -> None:
        """Cancel all unresolved cards so no timer or lookup leaks into a later turn."""

        for card in set(self._tool_cards.values()):
            if isinstance(card, ProcessCard):
                # Losing one poll result does not establish that the underlying
                # resumable process was cancelled or otherwise terminal.
                continue
            card.set_state("cancelled", detail=detail)
            self._surface.record_live_transcript_update(card)
            self.settle_widget(card, durable_entry_count=2)
        self._tool_cards.clear()

    def historical_tool_card(self, card_id: str) -> ToolCard | None:
        """Return a mounted historical tool card for page-boundary reconciliation."""

        return self._historical_tool_cards.get(card_id)

    def release_historical_widget(self, widget: Widget) -> bool:
        """Release history ownership unless a resumed live process owns the card."""

        if isinstance(widget, ProcessCard) and widget in self._live_process_cards:
            return False
        self.forget_widget(widget)
        return True

    def forget_widget(self, widget: Widget) -> None:
        """Discard every controller-owned reference to an evicted transcript widget."""

        self._historical_tool_cards = {
            card_id: card
            for card_id, card in self._historical_tool_cards.items()
            if card is not widget
        }
        if isinstance(widget, ToolCard):
            self._historical_widgets.discard(widget)
        if isinstance(widget, ProcessCard):
            self._live_process_cards.discard(widget)
            self._historical_process_cards = {
                process_id: card
                for process_id, card in self._historical_process_cards.items()
                if card is not widget
            }
            self._process_cards = {
                process_id: card
                for process_id, card in self._process_cards.items()
                if card is not widget
            }
        retained_settled = deque(
            (candidate, entry_count)
            for candidate, entry_count in self._settled_widgets
            if candidate is not widget
        )
        self._settled_widgets = retained_settled
        self._settled_durable_entry_count = sum(
            entry_count for _candidate, entry_count in retained_settled
        )
        self._tool_cards = {
            call_id: card for call_id, card in self._tool_cards.items() if card is not widget
        }
        if self._working_indicator is widget:
            self._working_indicator = None
        self.discard_unseen_output(widget)

    def reset(self) -> None:
        """Drop all transient live-transcript state during a session replacement."""

        self.hide_working_indicator()
        self._tool_cards.clear()
        self._process_cards.clear()
        self._live_process_cards.clear()
        self._historical_process_cards.clear()
        self._historical_tool_cards.clear()
        self._historical_widgets.clear()
        self._settled_widgets.clear()
        self._settled_durable_entry_count = 0
        self._card_focus_was_following = False
        self.clear_unseen_output()

    def tool_card_focused(self, card: ToolCard) -> None:
        """Capture follow intent before Textual's deferred card focus scroll."""

        del card
        self._card_focus_was_following = self._surface.transcript_is_following()

    def user_scrolled(self) -> None:
        """Cancel focus-time re-pin intent after a deliberate reader scroll."""

        self._card_focus_was_following = False

    def tool_card_toggled(self, card: ToolCard) -> None:
        """Re-pin only the newest card when focus began while following the tail."""

        if self._card_focus_was_following and self._surface.is_newest_transcript_widget(card):
            self._surface.return_transcript_to_latest()

    def _unsettle_widget(self, widget: Widget) -> None:
        """Remove a reused process card from settled retention while its call is active."""

        retained = deque(
            (candidate, entry_count)
            for candidate, entry_count in self._settled_widgets
            if candidate is not widget
        )
        if len(retained) == len(self._settled_widgets):
            return
        self._settled_widgets = retained
        self._settled_durable_entry_count = sum(entry_count for _candidate, entry_count in retained)

    def settle_widget(self, widget: Widget, *, durable_entry_count: int = 0) -> None:
        """Retain a completed live widget until the bounded transcript window fills."""

        if durable_entry_count < 0:
            raise ValueError("durable_entry_count cannot be negative")
        if any(candidate is widget for candidate, _entry_count in self._settled_widgets):
            return
        self._settled_widgets.append((widget, durable_entry_count))
        self._settled_durable_entry_count += durable_entry_count
        while (
            len(self._settled_widgets) > self._settled_capacity
            or self._settled_durable_entry_count > self._durable_entry_capacity
        ):
            evicted, evicted_entry_count = self._settled_widgets.popleft()
            self._settled_durable_entry_count -= evicted_entry_count
            self.forget_widget(evicted)
            self._surface.remove_live_transcript_widget(evicted)
            self._surface.live_transcript_widget_evicted(evicted)

    def _mount_working_indicator(self, indicator: WorkingIndicator) -> None:
        if not self._surface.transcript_available():
            return
        self._working_indicator = indicator
        self._surface.mount_live_transcript_widget(indicator)
        self._surface.record_live_transcript_update(indicator)
        self._surface.follow_transcript_tail_after_refresh()


__all__ = [
    "TUI_SETTLED_LIVE_WIDGET_LIMIT",
    "TUI_SETTLED_LIVE_DURABLE_ENTRY_LIMIT",
    "TextualTranscriptController",
    "TextualTranscriptSurface",
]
