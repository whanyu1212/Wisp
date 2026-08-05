"""Transient overlay, composer-focus, and viewport coordination for Textual.

Import direction is intentionally one-way:

``textual_app -> overlay -> structural surface protocols``

The controller owns presentation-transition state only. It does not import
widgets, the RPC shell, providers, sessions, or approval policy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OverlayKind(StrEnum):
    """Visible Textual surfaces that temporarily replace the composer."""

    decision = "decision"
    model_picker = "model_picker"
    session_picker = "session_picker"
    command_palette = "command_palette"
    prompt_history = "prompt_history"
    operation_indicator = "operation_indicator"


class OverlayOperation(StrEnum):
    """Non-visual operations that keep the composer temporarily unavailable."""

    session_catalog = "session_catalog"
    session_switch = "session_switch"


@dataclass(frozen=True)
class TranscriptViewportState:
    """Restorable transcript scroll position and follow intent."""

    scroll_y: float
    following: bool


class OverlaySurface(Protocol):
    """Minimal contract shared by Textual's transient overlay widgets."""

    @property
    def is_open(self) -> bool: ...

    def hide(self) -> None: ...


class ComposerSurface(Protocol):
    """Prompt editor operations needed during overlay transitions."""

    display: bool

    def focus(self, scroll_visible: bool = True) -> object: ...


class SuggestionSurface(Protocol):
    """Slash-command suggestion operation needed during transitions."""

    def hide(self) -> None: ...


class TranscriptViewport(Protocol):
    """Transcript operations needed to preserve a reader's position."""

    def viewport_state(self) -> TranscriptViewportState: ...

    def restore_viewport_state(self, state: TranscriptViewportState) -> None: ...


class TextualOverlayController:
    """Own transient overlay visibility, focus, stale-input, and viewport state."""

    def __init__(
        self,
        *,
        composer: ComposerSurface,
        suggestion: SuggestionSurface,
        transcript: TranscriptViewport,
        overlays: Mapping[OverlayKind, OverlaySurface],
        defer_after_refresh: Callable[[Callable[[], None]], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._composer = composer
        self._suggestion = suggestion
        self._transcript = transcript
        self._overlays = dict(overlays)
        self._defer_after_refresh = defer_after_refresh
        self._clock = clock
        self._active_overlay: OverlayKind | None = None
        self._active_operation: OverlayOperation | None = None
        self._stale_event_barrier = 0.0
        self._viewport_owner: OverlayKind | None = None
        self._viewport_state: TranscriptViewportState | None = None

    @property
    def active_overlay(self) -> OverlayKind | None:
        return self._active_overlay

    @property
    def active_operation(self) -> OverlayOperation | None:
        return self._active_operation

    @property
    def stale_event_barrier(self) -> float:
        return self._stale_event_barrier

    def event_is_stale(self, timestamp: float) -> bool:
        """Return whether input predates the latest visibility/focus transition."""

        return timestamp < self._stale_event_barrier

    def open(self, kind: OverlayKind, *, preserve_viewport: bool = False) -> None:
        """Prepare one visible overlay and make it the sole transition owner."""

        self._begin_transition()
        self._hide_all_overlays()
        self._clear_viewport_state()
        if preserve_viewport:
            self._viewport_owner = kind
            self._viewport_state = self._transcript.viewport_state()
        self._active_operation = None
        self._active_overlay = kind
        self._composer.display = False

    def close(self, kind: OverlayKind, *, restore_composer: bool = True) -> bool:
        """Close ``kind`` and restore its owned presentation state.

        Returns ``True`` only when ``kind`` owned the active transition. A late
        close for a displaced overlay may hide that surface, but cannot reveal
        the composer or consume another overlay's viewport snapshot.
        """

        surface = self._overlays[kind]
        if surface.is_open:
            surface.hide()
        if self._active_overlay is not kind:
            return False

        self._active_overlay = None
        if restore_composer and self._active_operation is None:
            self._restore_composer()
        self._restore_viewport_for(kind)
        return True

    def start_operation(self, operation: OverlayOperation) -> None:
        """Hide transient UI while a sequential session operation is pending."""

        self._begin_transition()
        self._hide_all_overlays()
        self._clear_viewport_state()
        self._active_overlay = None
        self._active_operation = operation
        self._composer.display = False

    def finish_operation(self, operation: OverlayOperation) -> bool:
        """Finish only the matching operation, ignoring stale completions."""

        if self._active_operation is not operation:
            return False
        self._active_operation = None
        if self._active_overlay is None:
            self._restore_composer()
        return True

    def consume_interrupt(self) -> bool:
        """Handle interrupts owned by transient session presentation."""

        if self._active_overlay in {
            OverlayKind.session_picker,
            OverlayKind.prompt_history,
        }:
            self.close(self._active_overlay)
            return True
        return self._active_operation is not None

    def _begin_transition(self) -> None:
        # This must happen before hiding or moving focus. Input already read by
        # Textual's driver then remains older than the new barrier.
        self._stale_event_barrier = self._clock()
        self._suggestion.hide()

    def _hide_all_overlays(self) -> None:
        for surface in self._overlays.values():
            if surface.is_open:
                surface.hide()

    def _restore_composer(self) -> None:
        self._composer.display = True
        self._composer.focus()

    def _restore_viewport_for(self, kind: OverlayKind) -> None:
        if self._viewport_owner is not kind or self._viewport_state is None:
            return
        state = self._viewport_state
        self._clear_viewport_state()
        self._defer_after_refresh(lambda: self._transcript.restore_viewport_state(state))

    def _clear_viewport_state(self) -> None:
        self._viewport_owner = None
        self._viewport_state = None
