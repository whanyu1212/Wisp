"""Focused tests for Textual's process-local input-state controller."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest

from wisp.tui.compact_echo import CompactEchoLog
from wisp.tui.input_types import TuiSubmission
from wisp.tui.textual_input import TextualInputController
from wisp.tui.widgets import PromptEditor


def test_prompt_editor_does_not_pop_retained_queue_while_idle() -> None:
    editor = PromptEditor()
    messages: list[PromptEditor.RestoreQueued] = []
    editor.post_message = messages.append  # type: ignore[method-assign]

    editor.action_restore_queued()

    assert messages == []


@dataclass
class _Surface:
    clear_count: int = 0
    errors: list[str] = field(default_factory=list)
    buffered: list[TuiSubmission] = field(default_factory=list)

    def clear_prompt_editor(self) -> None:
        self.clear_count += 1

    def write_input_error(self, message: str) -> None:
        self.errors.append(message)

    def buffer_submission(self, submission: TuiSubmission) -> None:
        self.buffered.append(submission)


def test_submission_clears_only_ordinary_editor_and_runs_hook_before_enqueue() -> None:
    surface = _Surface()
    controller = TextualInputController(surface)
    hook_calls: list[str] = []

    def hook() -> str:
        with pytest.raises(anyio.WouldBlock):
            controller.receive_stream.receive_nowait()
        hook_calls.append("before enqueue")
        return "running"

    controller.set_submit_hook(hook)

    assert controller.submit_line("typed prompt", clear_editor=True, queue_kind="steering")

    def decision_hook() -> str:
        hook_calls.append("decision submit")
        return "approval"

    controller.set_submit_hook(decision_hook)
    assert controller.submit_line("decision answer", clear_editor=False)
    assert surface.clear_count == 1
    assert hook_calls == ["before enqueue", "decision submit"]

    async def receive() -> tuple[TuiSubmission, TuiSubmission]:
        return await controller.receive(), await controller.receive()

    typed, decision = anyio.run(receive)
    assert (typed.content, typed.input_mode, typed.queue_kind) == (
        "typed prompt",
        "running",
        "steering",
    )
    assert (decision.content, decision.input_mode, decision.queue_kind) == (
        "decision answer",
        "approval",
        "auto",
    )
    assert surface.buffered == [typed]


def test_full_submission_queue_reports_error_after_running_submit_hook() -> None:
    surface = _Surface()
    controller = TextualInputController(surface, queue_capacity=1)
    hooks: list[None] = []
    controller.set_submit_hook(lambda: hooks.append(None))

    assert controller.submit_line("first", clear_editor=True)
    assert not controller.submit_line("dropped", clear_editor=True)

    assert hooks == [None, None]
    assert surface.clear_count == 1
    assert [submission.content for submission in surface.buffered] == ["first"]
    assert surface.errors == ["input buffer full; command dropped"]


def test_signal_clears_editor_only_after_queueing_and_receive_reraises() -> None:
    surface = _Surface()
    controller = TextualInputController(surface, queue_capacity=1)

    assert controller.submit_line("queued", clear_editor=True)
    assert not controller.signal(KeyboardInterrupt(), action="interrupt")
    assert surface.clear_count == 1
    assert surface.errors == ["input buffer full; interrupt ignored"]

    async def receive() -> None:
        assert await controller.receive() == "queued"
        assert controller.signal(EOFError(), action="EOF")
        with pytest.raises(EOFError):
            await controller.receive()

    anyio.run(receive)
    assert surface.clear_count == 2


def test_cancel_signal_can_preserve_editor_after_successful_queueing() -> None:
    surface = _Surface()
    controller = TextualInputController(surface)
    signal = RuntimeError("cancel")

    assert controller.signal(signal, action="cancel", clear_editor=False)
    assert surface.clear_count == 0

    async def receive() -> None:
        with pytest.raises(RuntimeError, match="cancel"):
            await controller.receive()

    anyio.run(receive)


def test_compact_echoes_and_prompt_history_have_one_controller_owner() -> None:
    surface = _Surface()
    controller = TextualInputController(surface, compact_echoes=CompactEchoLog(max_pending=2))

    controller.register_compact_echo("same", "first marker")
    controller.register_compact_echo("same", "second marker")
    controller.register_compact_echo("new", "new marker")

    assert controller.pending_compact_echo_count == 2
    assert controller.compact_echo_order_length == 2
    assert controller.compact_echo_for("same") == "second marker"  # oldest was evicted
    assert controller.compact_echo_for("new") == "new marker"
    controller.clear_compact_echoes()
    assert controller.compact_echo_key_count == 0

    controller.record_prompt("first accepted")
    controller.record_prompt("second accepted")
    controller.record_prompt(" ")
    assert tuple(entry.prompt for entry in controller.prompt_history_entries) == (
        "second accepted",
        "first accepted",
    )
