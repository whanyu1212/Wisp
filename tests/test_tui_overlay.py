from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import anyio

from wisp.events import ToolApprovalRequested
from wisp.tui import TuiViewSnapshot
from wisp.tui.overlay import (
    OverlayKind,
    OverlayOperation,
    TextualOverlayController,
    TranscriptViewportState,
)
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import CommandPalette, DecisionPanel, PromptEditor


@dataclass
class _Composer:
    display: bool = True
    focus_count: int = 0

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
    transcript: _Transcript = field(default_factory=_Transcript)
    decision: _Overlay = field(default_factory=_Overlay)
    model: _Overlay = field(default_factory=_Overlay)
    session: _Overlay = field(default_factory=_Overlay)
    palette: _Overlay = field(default_factory=_Overlay)
    history: _Overlay = field(default_factory=_Overlay)
    deferred: list[object] = field(default_factory=list)

    def controller(self, *, clock: Callable[[], float] | None = None) -> TextualOverlayController:
        selected_clock = clock or (lambda: 10.0)
        return TextualOverlayController(
            composer=self.composer,
            suggestion=self.suggestion,
            transcript=self.transcript,
            overlays={
                OverlayKind.decision: self.decision,
                OverlayKind.model_picker: self.model,
                OverlayKind.session_picker: self.session,
                OverlayKind.command_palette: self.palette,
                OverlayKind.prompt_history: self.history,
            },
            defer_after_refresh=self.deferred.append,
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


def test_palette_close_restores_composer_and_viewport_after_refresh() -> None:
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.command_palette, preserve_viewport=True)
    harness.palette.open = True
    assert controller.close(OverlayKind.command_palette)

    assert harness.palette.hide_count == 1
    assert harness.composer.display is True
    assert harness.composer.focus_count == 1
    assert harness.transcript.snapshots == 1
    assert harness.transcript.restored == []
    assert len(harness.deferred) == 1

    callback = harness.deferred.pop()
    assert callable(callback)
    callback()
    assert harness.transcript.restored == [harness.transcript.state]


def test_displaced_overlay_cannot_restore_composer_or_old_viewport() -> None:
    harness = _Harness()
    controller = harness.controller()

    controller.open(OverlayKind.command_palette, preserve_viewport=True)
    harness.palette.open = True
    controller.open(OverlayKind.model_picker)
    harness.model.open = True

    assert not controller.close(OverlayKind.command_palette)
    assert harness.composer.display is False
    assert harness.composer.focus_count == 0
    assert harness.deferred == []
    assert controller.active_overlay is OverlayKind.model_picker


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


def test_real_app_replaces_palette_with_decision_and_restores_draft_focus() -> None:
    async def scenario() -> tuple[OverlayKind | None, bool, bool, bool, str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft survives overlay replacement"
            app.action_open_command_palette()
            await pilot.pause()

            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="call-1",
                    name="write",
                    arguments={"path": "file.txt", "content": "content"},
                    safety="mutating",
                )
            )
            await pilot.pause()

            assert app._overlay_controller is not None
            active = app._overlay_controller.active_overlay
            palette_open = app.query_one("#command-palette", CommandPalette).is_open
            decision_open = app.query_one("#decision-panel", DecisionPanel).is_open
            hidden = not editor.display

            renderer.view_updated(
                TuiViewSnapshot(status="idle", input_hint="wisp> ", input_mode="idle")
            )
            await pilot.pause()
            return (
                active,
                palette_open,
                decision_open,
                hidden,
                editor.value,
                editor.has_focus,
            )

    active, palette_open, decision_open, hidden, draft, focused = anyio.run(scenario)
    assert active is OverlayKind.decision
    assert not palette_open
    assert decision_open
    assert hidden
    assert draft == "draft survives overlay replacement"
    assert focused
