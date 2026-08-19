"""Transient overlay, composer-focus, and viewport coordination for Textual.

Import direction is intentionally one-way:

``textual_app -> overlay -> structural surface protocols``

The controller owns presentation-transition state only. It does not import
widgets, the RPC shell, providers, sessions, or approval policy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OverlayKind(StrEnum):
    """Visible Textual surfaces that temporarily replace the composer."""

    decision = "decision"
    connect = "connect"
    model_picker = "model_picker"
    session_picker = "session_picker"
    prompt_history = "prompt_history"
    theme_picker = "theme_picker"
    context_status = "context_status"
    diff_viewer = "diff_viewer"
    update_prompt = "update_prompt"
    operation_indicator = "operation_indicator"


class OverlayOperation(StrEnum):
    """Non-visual operations that keep the composer temporarily unavailable."""

    history_hydration = "history_hydration"
    session_catalog = "session_catalog"
    session_switch = "session_switch"
    update = "update"


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

    def hide(self) -> None: ...

    def show(self) -> None: ...

    def focus(self, scroll_visible: bool = True) -> object: ...


class SuggestionSurface(Protocol):
    """Inline suggestion-menu operation needed during transitions.

    Implemented by every menu anchored to the composer — the slash-command menu and
    the ``@``-file picker — since all of them must vanish when the composer does.
    """

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
        suggestions: Sequence[SuggestionSurface],
        transcript: TranscriptViewport,
        overlays: Mapping[OverlayKind, OverlaySurface],
        defer_after_refresh: Callable[[Callable[[], None]], None],
        on_overlay_displaced: Callable[[OverlayKind], None] | None = None,
        on_transition_finished: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._composer = composer
        # A sequence, not a single surface: every composer-anchored menu must be
        # torn down together. This was a scalar while the slash menu was the only
        # one, which silently left the later `@`-file picker floating over
        # overlays and stealing Escape. Adding a menu must mean registering it.
        self._suggestions = tuple(suggestions)
        self._transcript = transcript
        self._overlays = dict(overlays)
        self._defer_after_refresh = defer_after_refresh
        self._on_overlay_displaced = on_overlay_displaced
        self._on_transition_finished = on_transition_finished
        self._clock = clock
        self._active_overlay: OverlayKind | None = None
        self._active_operation: OverlayOperation | None = None
        self._stale_event_barrier = 0.0
        self._viewport_owner: OverlayKind | None = None
        self._viewport_state: TranscriptViewportState | None = None
        # Bumped on every transition that could race a still-pending deferred
        # restore (see _restore_viewport_for). The deferred closure captures its
        # generation and no-ops if a later transition has since moved on.
        self._viewport_generation = 0

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
        self._notify_displaced_overlay(replacement=kind)
        self._hide_all_overlays()
        self._clear_viewport_state()
        if preserve_viewport:
            self._viewport_owner = kind
            self._viewport_state = self._transcript.viewport_state()
        self._active_operation = None
        self._active_overlay = kind
        self._composer.hide()

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
        if self._on_transition_finished is not None:
            self._on_transition_finished()
        return True

    def start_operation(self, operation: OverlayOperation) -> None:
        """Hide transient UI while a sequential session operation is pending."""

        self._begin_transition()
        self._notify_displaced_overlay()
        self._hide_all_overlays()
        self._clear_viewport_state()
        self._active_overlay = None
        self._active_operation = operation
        self._composer.hide()

    def finish_operation(self, operation: OverlayOperation) -> bool:
        """Finish only the matching operation, ignoring stale completions."""

        if self._active_operation is not operation:
            return False
        self._active_operation = None
        if self._active_overlay is None:
            self._restore_composer()
        if self._on_transition_finished is not None:
            self._on_transition_finished()
        return True

    def consume_cancel(self) -> bool:
        """Dismiss the nearest presentation layer, or guard an active transition.

        Decision panels intentionally remain widget-owned because Escape must post
        their conservative deny/cancel answer rather than merely hide the panel.
        """

        if self._active_overlay is not None and self._active_overlay is not OverlayKind.decision:
            self.close(self._active_overlay)
            return True
        return self._active_operation is not None

    def consume_interrupt(self) -> bool:
        """Backward-compatible alias for the former presentation interrupt hook."""

        return self.consume_cancel()

    def _begin_transition(self) -> None:
        # This must happen before hiding or moving focus. Input already read by
        # Textual's driver then remains older than the new barrier.
        self._stale_event_barrier = self._clock()
        for suggestion in self._suggestions:
            suggestion.hide()
        # Invalidate any deferred viewport restore still queued from a prior
        # close() (see _restore_viewport_for): once a new transition begins,
        # that snapshot no longer describes the surface the reader is looking
        # at, so it must not be allowed to apply when it finally fires.
        self._viewport_generation += 1

    def _hide_all_overlays(self) -> None:
        for surface in self._overlays.values():
            if surface.is_open:
                surface.hide()

    def _notify_displaced_overlay(self, *, replacement: OverlayKind | None = None) -> None:
        active = self._active_overlay
        if active is None or active is replacement or self._on_overlay_displaced is None:
            return
        self._on_overlay_displaced(active)

    def _restore_composer(self) -> None:
        self._composer.show()
        self._composer.focus()

    def _restore_viewport_for(self, kind: OverlayKind) -> None:
        if self._viewport_owner is not kind or self._viewport_state is None:
            return
        state = self._viewport_state
        self._clear_viewport_state()
        generation = self._viewport_generation
        self._defer_after_refresh(lambda: self._apply_deferred_restore(generation, state))

    def _apply_deferred_restore(self, generation: int, state: TranscriptViewportState) -> None:
        # A later open()/start_operation() has since begun a new transition —
        # this snapshot describes a surface the reader has already left.
        # Applying it now would silently overwrite whatever scroll state that
        # later transition (or the reader) has since established.
        if generation != self._viewport_generation:
            return
        self._transcript.restore_viewport_state(state)

    def _clear_viewport_state(self) -> None:
        self._viewport_owner = None
        self._viewport_state = None
