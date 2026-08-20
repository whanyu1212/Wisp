from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from wisp.tui.overlay import (
    OverlayKind,
    OverlayOperation,
    TextualOverlayController,
    TranscriptViewportState,
)

pytestmark = pytest.mark.tui


@dataclass
class _Composer:
    display: bool = True
    focus_count: int = 0

    def hide(self) -> None:
        self.display = False

    def show(self) -> None:
        self.display = True

    def focus(self, scroll_visible: bool = True) -> object:
        self.focus_count += 1
        return self


@dataclass
class _Suggestion:
    hide_count: int = 0

    def hide(self) -> None:
        self.hide_count += 1


@dataclass
class _Overlay:
    open: bool = False
    hide_count: int = 0

    @property
    def is_open(self) -> bool:
        return self.open

    def hide(self) -> None:
        self.open = False
        self.hide_count += 1


@dataclass
class _Transcript:
    state: TranscriptViewportState = TranscriptViewportState(scroll_y=17.0, following=False)
    snapshots: int = 0
    restored: list[TranscriptViewportState] = field(default_factory=list)

    def viewport_state(self) -> TranscriptViewportState:
        self.snapshots += 1
        return self.state

    def restore_viewport_state(self, state: TranscriptViewportState) -> None:
        self.restored.append(state)


@dataclass
class _Harness:
    composer: _Composer = field(default_factory=_Composer)
    suggestion: _Suggestion = field(default_factory=_Suggestion)
    # The `@`-file picker: a second composer-anchored menu that must be torn down
    # by the same transitions as the slash menu.
    file_suggestion: _Suggestion = field(default_factory=_Suggestion)
    transcript: _Transcript = field(default_factory=_Transcript)
    decision: _Overlay = field(default_factory=_Overlay)
    model: _Overlay = field(default_factory=_Overlay)
    session: _Overlay = field(default_factory=_Overlay)
    history: _Overlay = field(default_factory=_Overlay)
    deferred: list[object] = field(default_factory=list)
    displaced: list[OverlayKind] = field(default_factory=list)

    def controller(self, *, clock: Callable[[], float] | None = None) -> TextualOverlayController:
        selected_clock = clock or (lambda: 10.0)
        return TextualOverlayController(
            composer=self.composer,
            suggestions=(self.suggestion, self.file_suggestion),
            transcript=self.transcript,
            overlays={
                OverlayKind.decision: self.decision,
                OverlayKind.model_picker: self.model,
                OverlayKind.session_picker: self.session,
                OverlayKind.prompt_history: self.history,
            },
            defer_after_refresh=self.deferred.append,
            on_overlay_displaced=self.displaced.append,
            clock=selected_clock,
        )


def test_open_raises_barrier_before_hiding_competing_surfaces() -> None:
    harness = _Harness()
    harness.decision.open = True
    observed: list[tuple[bool, bool, int]] = []

    def clock() -> float:
        observed.append(
            (harness.composer.display, harness.decision.is_open, harness.suggestion.hide_count)
        )
        return 42.0

    controller = harness.controller(clock=clock)
    controller.open(OverlayKind.model_picker)

    assert observed == [(True, True, 0)]
    assert controller.stale_event_barrier == 42.0
    assert controller.active_overlay is OverlayKind.model_picker
    assert controller.event_is_stale(41.0)
    assert not controller.event_is_stale(42.0)
    assert harness.decision.is_open is False
    assert harness.decision.hide_count == 1
    assert harness.suggestion.hide_count == 1
    assert harness.composer.display is False


def test_opening_an_overlay_hides_every_suggestion_menu() -> None:
    """Both composer-anchored menus must go, not just the slash menu.

    A surviving `@` picker floats over the overlay and wins Escape/navigation keys
    that belong to the active workflow.
    """

    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.decision)

    assert harness.suggestion.hide_count == 1
    assert harness.file_suggestion.hide_count == 1


def test_replacing_an_overlay_notifies_its_owner_before_hiding() -> None:
    harness = _Harness()
    controller = harness.controller()
    controller.open(OverlayKind.prompt_history)
    harness.history.open = True

    controller.open(OverlayKind.decision)

    assert harness.displaced == [OverlayKind.prompt_history]
    assert not harness.history.is_open


def test_starting_an_operation_notifies_the_displaced_overlay() -> None:
    harness = _Harness()
    controller = harness.controller()
    controller.open(OverlayKind.prompt_history)
    harness.history.open = True

    controller.start_operation(OverlayOperation.session_switch)

    assert harness.displaced == [OverlayKind.prompt_history]
    assert not harness.history.is_open


def test_starting_an_operation_hides_every_suggestion_menu() -> None:
    """Non-visual operations hide the composer too, so its menus must follow."""

    harness = _Harness()
    controller = harness.controller()

    controller.start_operation(OverlayOperation.session_switch)

    assert harness.suggestion.hide_count == 1
    assert harness.file_suggestion.hide_count == 1


def test_only_matching_operation_completion_restores_composer() -> None:
    harness = _Harness()
    controller = harness.controller()

    controller.start_operation(OverlayOperation.session_catalog)
    controller.start_operation(OverlayOperation.session_switch)

    assert not controller.finish_operation(OverlayOperation.session_catalog)
    assert harness.composer.display is False
    assert harness.composer.focus_count == 0
    assert controller.consume_interrupt()

    assert controller.finish_operation(OverlayOperation.session_switch)
    assert harness.composer.display is True
    assert harness.composer.focus_count == 1
    assert not controller.consume_interrupt()


def test_preparing_operation_finish_exposes_layout_but_retains_input_guard() -> None:
    harness = _Harness()
    controller = harness.controller()

    controller.start_operation(OverlayOperation.session_switch)

    assert not controller.prepare_operation_finish(OverlayOperation.session_catalog)
    assert harness.composer.display is False
    assert harness.composer.focus_count == 0

    assert controller.prepare_operation_finish(OverlayOperation.session_switch)
    assert harness.composer.display is True
    assert harness.composer.focus_count == 0
    assert controller.active_operation is OverlayOperation.session_switch
    assert controller.consume_interrupt()

    assert controller.finish_operation(OverlayOperation.session_switch)
    assert harness.composer.focus_count == 1
    assert controller.active_operation is None
    assert not controller.consume_interrupt()


def test_session_picker_interrupt_closes_picker_and_preserves_other_interrupts() -> None:
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.session_picker)
    harness.session.open = True

    assert controller.consume_interrupt()
    assert harness.session.is_open is False
    assert harness.composer.display is True
    assert harness.composer.focus_count == 1
    assert not controller.consume_interrupt()


def test_prompt_history_interrupt_closes_overlay_and_restores_viewport() -> None:
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.prompt_history, preserve_viewport=True)
    harness.history.open = True

    assert controller.consume_interrupt()
    assert not harness.history.is_open
    assert harness.composer.display is True
    assert len(harness.deferred) == 1


def test_deferred_restore_applies_when_nothing_else_transitions_first() -> None:
    # Baseline for the regression below: with no intervening transition, the
    # deferred restore queued by close() must still apply when it finally runs.
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.prompt_history, preserve_viewport=True)
    harness.history.open = True
    controller.close(OverlayKind.prompt_history)

    assert len(harness.deferred) == 1
    harness.deferred[0]()
    assert harness.transcript.restored == [harness.transcript.state]


def test_deferred_restore_is_dropped_by_a_later_overlay_transition() -> None:
    # Regression: a restore queued by close() must not apply once a later
    # open()/start_operation() has begun a new transition. Without the
    # generation guard, the stale closure would silently overwrite whatever
    # scroll state the second overlay's own lifecycle has since established —
    # the same "deferred async work forgets the state that scheduled it" bug
    # class already fixed in Transcript's follow-intent tracking.
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.prompt_history, preserve_viewport=True)
    harness.history.open = True
    controller.close(OverlayKind.prompt_history)
    assert len(harness.deferred) == 1
    pending_restore = harness.deferred[0]

    # A second, unrelated overlay opens before the first restore's deferred
    # callback has run — e.g. an approval prompt arriving right after the
    # reader dismissed the prompt-history picker.
    controller.open(OverlayKind.decision)

    pending_restore()
    assert harness.transcript.restored == []


def test_deferred_restore_is_dropped_by_a_later_operation() -> None:
    # Same guarantee, but the intervening transition is a non-visual operation
    # (e.g. a session switch) rather than another overlay.
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.prompt_history, preserve_viewport=True)
    harness.history.open = True
    controller.close(OverlayKind.prompt_history)
    assert len(harness.deferred) == 1
    pending_restore = harness.deferred[0]

    controller.start_operation(OverlayOperation.session_switch)

    pending_restore()
    assert harness.transcript.restored == []
