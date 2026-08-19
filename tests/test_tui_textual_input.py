"""Focused tests for Textual's process-local input-state controller."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
import pytest

from wisp.tui.input_types import TuiSubmission
from wisp.tui.textual_input import TextualInputController


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

    def hook() -> None:
        with pytest.raises(anyio.WouldBlock):
            controller.receive_stream.receive_nowait()
        hook_calls.append("before enqueue")

    controller.set_submit_hook(hook)

    assert controller.submit_line("typed prompt", clear_editor=True)
    controller.set_submit_hook(lambda: hook_calls.append("decision submit"))
    assert controller.submit_line("decision answer", clear_editor=False)
    assert surface.clear_count == 1
    assert hook_calls == ["before enqueue", "decision submit"]

    async def receive() -> tuple[TuiSubmission, TuiSubmission]:
        first = await controller.receive()
        second = await controller.receive()
        assert isinstance(first, TuiSubmission)
        assert isinstance(second, TuiSubmission)
        return first, second

    first, second = anyio.run(receive)
    assert (first.content, second.content) == ("typed prompt", "decision answer")
    assert surface.buffered == [first]


def test_full_submission_queue_reports_error_after_running_submit_hook() -> None:
    surface = _Surface()
    controller = TextualInputController(surface, queue_capacity=1)
    hooks: list[None] = []
    controller.set_submit_hook(lambda: hooks.append(None))

    assert controller.submit_line("first", clear_editor=True)
    assert not controller.submit_line("dropped", clear_editor=True)

    assert hooks == [None, None]
    assert surface.clear_count == 1
    assert len(surface.buffered) == 1
    assert surface.buffered[0].content == "first"
    assert surface.errors == ["input buffer full; command dropped"]


def test_signal_clears_editor_only_after_queueing_and_receive_reraises() -> None:
    surface = _Surface()
    controller = TextualInputController(surface, queue_capacity=1)

    assert controller.submit_line("queued", clear_editor=True)
    assert not controller.signal(KeyboardInterrupt(), action="interrupt")
    assert surface.clear_count == 1
    assert surface.errors == ["input buffer full; interrupt ignored"]

    async def receive() -> None:
        queued = await controller.receive()
        assert isinstance(queued, TuiSubmission)
        assert queued.content == "queued"
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


def test_prompt_history_remains_owned_by_input_controller() -> None:
    surface = _Surface()
    controller = TextualInputController(surface)

    controller.record_prompt("first accepted")
    controller.record_prompt("second accepted")
    controller.record_prompt(" ")
    assert tuple(entry.prompt for entry in controller.prompt_history_entries) == (
        "second accepted",
        "first accepted",
    )
