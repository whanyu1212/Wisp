from __future__ import annotations

import asyncio
import builtins
import io
import sys
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from rich.console import Console
from typer.testing import CliRunner

from wisp import tui as tui_module
from wisp.cli import app
from wisp.config import WispConfig
from wisp.events import (
    AssistantMessage,
    ErrorEvent,
    KnownWispEvent,
    RpcCommandFinished,
    SessionSaved,
    TokenDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
)
from wisp.tui import (
    FullscreenTuiRenderer,
    LineTuiRenderer,
    TuiInteractionState,
    TuiOptions,
    TuiRendererKind,
    TuiShell,
    TuiStatus,
    create_tui_renderer,
)
from wisp.tui.app import _default_prompt_reader, _InputLine, _InputMode, _rpc_command

type EventBatch = list[KnownWispEvent]
type ScriptedBatch = EventBatch | tuple[float, EventBatch]


class ScriptedController:
    def __init__(
        self,
        prompt_events: list[ScriptedBatch] | None = None,
        *,
        approval_events: list[ScriptedBatch] | None = None,
        cancel_events: list[ScriptedBatch] | None = None,
        shutdown_events: list[ScriptedBatch] | None = None,
        close_after_prompt: bool = False,
    ) -> None:
        self.prompt_events = deque(prompt_events or [])
        self.approval_events = deque(approval_events or [])
        self.cancel_events = deque(cancel_events or [])
        self.shutdown_events = deque(shutdown_events or [])
        self.close_after_prompt = close_after_prompt
        self.prompts: list[str] = []
        self.approvals: list[tuple[str, bool, str | None]] = []
        self.cancelled: list[str] = []
        self.shutdown_count = 0
        self.closed = False
        self._send, self._receive = anyio.create_memory_object_stream[KnownWispEvent](100)

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
        self.prompts.append(prompt)
        selected_id = command_id or f"prompt-{len(self.prompts)}"
        await self._emit_scripted(
            self.prompt_events,
            default=[RpcCommandFinished(command_id=selected_id, command_type="prompt", ok=True)],
        )
        if self.close_after_prompt:
            await self._send.aclose()
        return selected_id

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str:
        self.cancelled.append(target_id)
        await self._emit_scripted(self.cancel_events, default=[])
        return command_id or f"cancel-{len(self.cancelled)}"

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> str:
        self.approvals.append((call_id, approved, reason))
        await self._emit_scripted(self.approval_events, default=[])
        return command_id or f"approval-{len(self.approvals)}"

    async def shutdown(self, *, command_id: str | None = None) -> str:
        self.shutdown_count += 1
        selected_id = command_id or f"shutdown-{self.shutdown_count}"
        await self._emit_scripted(
            self.shutdown_events,
            default=[RpcCommandFinished(command_id=selected_id, command_type="shutdown", ok=True)],
        )
        return selected_id

    def events(self) -> AsyncIterator[KnownWispEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[KnownWispEvent]:
        async with self._receive.clone() as receive:
            async for event in receive:
                yield event

    async def close(self) -> None:
        self.closed = True
        await self._send.aclose()

    async def emit(self, events: EventBatch) -> None:
        await self._emit(events)

    async def _emit_scripted(
        self,
        batches: deque[ScriptedBatch],
        *,
        default: EventBatch,
    ) -> None:
        batch = batches.popleft() if batches else default
        if isinstance(batch, tuple):
            delay, events = batch
            asyncio.create_task(self._emit_after(delay, events))
            return
        await self._emit(batch)

    async def _emit_after(self, delay: float, events: EventBatch) -> None:
        await anyio.sleep(delay)
        await self._emit(events)

    async def _emit(self, events: EventBatch) -> None:
        for event in events:
            await self._send.send(event)


async def _reader_from(inputs: list[str]) -> object:
    values = deque(inputs)

    async def read(_prompt: str) -> str:
        if not values:
            raise EOFError
        return values.popleft()

    return read


def _console() -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_tui_shell_uses_injected_renderer() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.calls: list[str] = []

        def __bool__(self) -> bool:
            return False

        def startup(self) -> None:
            self.calls.append("startup")

        def running(self) -> None:
            self.calls.append("running")

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert renderer.calls == ["startup", "running"]
        assert controller.prompts == ["hello"]

    anyio.run(run)


def test_fullscreen_tui_renderer_renders_layout_regions(tmp_path: Path) -> None:
    console, output = _console()
    renderer = FullscreenTuiRenderer(console, clear_screen=False)

    renderer.startup()
    renderer.running()
    renderer.token_delta("hello")
    renderer.end_token_stream()
    renderer.event(SessionSaved(session_id="session", path=tmp_path / "session.jsonl"))

    rendered = output.getvalue()
    assert "Transcript" in rendered
    assert "Status" in rendered
    assert "Input" in rendered
    assert "hello" in rendered
    assert "session saved: session.jsonl" in rendered
    assert renderer.state.last_session == "session.jsonl"


def test_fullscreen_tui_renderer_does_not_clear_terminal_by_default() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=80)
    renderer = FullscreenTuiRenderer(console)

    renderer.startup()

    assert renderer.clear_screen is False
    assert "\x1b[2J" not in output.getvalue()


def test_fullscreen_tui_renderer_coalesces_streaming_token_redraws() -> None:
    console, output = _console()
    renderer = FullscreenTuiRenderer(console, clear_screen=False)

    renderer.startup()
    renderer.running()
    before_tokens = output.getvalue()

    renderer.token_delta("hel")
    renderer.token_delta("lo")

    assert renderer.state.streaming_text == "hello"
    assert output.getvalue() == before_tokens

    renderer.end_token_stream()

    assert renderer.state.streaming_text == ""
    assert "hello" in output.getvalue()


def test_fullscreen_tui_renderer_restores_idle_footer_after_cancellation() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.running()
    renderer.queued_follow_up(1)
    renderer.cancelled()

    assert renderer.state.status == "idle"
    assert renderer.state.input_hint == "wisp> "
    assert renderer.state.queued_follow_ups == 0


def test_fullscreen_tui_renderer_clears_discarded_follow_ups_on_eof() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.running()
    renderer.queued_follow_up(1)
    renderer.input_closed_finishing_prompt()

    assert renderer.state.queued_follow_ups == 0


def test_fullscreen_tui_renderer_clears_follow_ups_on_failed_prompt() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.running()
    renderer.queued_follow_up(1)
    renderer.event(
        RpcCommandFinished(
            command_id="prompt-1",
            command_type="prompt",
            ok=False,
            error="failed",
        )
    )

    assert renderer.state.status == "error"
    assert renderer.state.input_hint == "wisp> "
    assert renderer.state.queued_follow_ups == 0


def test_fullscreen_tui_renderer_does_not_idle_on_approval_completion() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.running()
    renderer.approval_request(
        ToolApprovalRequested(
            call_id="call-1",
            name="bash",
            arguments={"command": "echo hi"},
            safety="command",
        )
    )
    renderer.queued_follow_up(1)
    renderer.event(ToolApprovalResolved(call_id="call-1", name="bash", approved=True))
    renderer.event(RpcCommandFinished(command_id="approval-1", command_type="approval", ok=True))

    assert renderer.state.status == "running"
    assert renderer.state.input_hint == "wisp(running)> "
    assert renderer.state.queued_follow_ups == 1


def test_create_tui_renderer_selects_fullscreen_renderer() -> None:
    renderer = create_tui_renderer(TuiRendererKind.fullscreen, _console()[0])

    assert isinstance(renderer, FullscreenTuiRenderer)


def test_tui_shell_records_submitted_prompt_for_fullscreen_renderer() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    AssistantMessage(content="answer"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["what <now>?"]),
        )

        await shell.run()

        assert any(
            entry.role == "user" and entry.content == "what <now>?"
            for entry in renderer.state.transcript
        )

    anyio.run(run)


def test_tui_shell_runs_with_fullscreen_renderer() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    AssistantMessage(content="fullscreen response"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            renderer=FullscreenTuiRenderer(console, clear_screen=False),
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert "Transcript" in output.getvalue()
        assert "fullscreen response" in output.getvalue()

    anyio.run(run)


def test_tui_shell_runs_prompt_then_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    TokenDelta(delta="hello"),
                    AssistantMessage(content="hello"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert controller.shutdown_count == 1
        assert "Wisp TUI MVP" in output.getvalue()
        assert "hello" in output.getvalue()

    anyio.run(run)


def test_tui_shell_help_renders_approval_hint_literally() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/help", "/quit"]),
        )

        await shell.run()

        assert "approve? [y/N]" in output.getvalue()

    anyio.run(run)


def test_default_prompt_reader_hides_prompts_for_non_tty(monkeypatch: object) -> None:
    prompts: list[str] = []

    class NonTtyStdin:
        def isatty(self) -> bool:
            return False

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "hello"

    monkeypatch.setattr(sys, "stdin", NonTtyStdin())
    monkeypatch.setattr(builtins, "input", fake_input)

    result = anyio.run(_default_prompt_reader, "wisp> ")

    assert result == "hello"
    assert prompts == [""]


def test_tui_shell_quit_then_eof_sends_one_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(
            controller,
            console=_console()[0],
            prompt_reader=await _reader_from(["/quit"]),
        )

        await shell.run()

        assert controller.shutdown_count == 1

    anyio.run(run)


def test_tui_shell_queues_follow_up_while_running() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                ),
                [RpcCommandFinished(command_id="prompt-2", command_type="prompt", ok=True)],
            ]
        )
        inputs = deque(["first", "second"])

        async def read(_prompt: str) -> str:
            if inputs:
                return inputs.popleft()
            await anyio.sleep(0.1)
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.prompts == ["first", "second"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "queued follow-up #1" in rendered
        assert "running queued follow-up" in rendered

    anyio.run(run)


def test_tui_shell_preserves_remaining_fullscreen_follow_up_count() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                ),
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-2", command_type="prompt", ok=True)],
                ),
                [RpcCommandFinished(command_id="prompt-3", command_type="prompt", ok=True)],
            ]
        )
        inputs = deque(["first", "second", "third"])

        async def read(_prompt: str) -> str:
            if inputs:
                return inputs.popleft()
            await anyio.sleep(0.2)
            raise EOFError

        class RecordingFullscreenRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.running_follow_up_counts: list[int] = []

            def running_queued_follow_up(self, count: int) -> None:
                self.running_follow_up_counts.append(count)
                super().running_queued_follow_up(count)

        renderer = RecordingFullscreenRenderer()
        shell = TuiShell(controller, renderer=renderer, prompt_reader=read)

        await shell.run()

        assert controller.prompts == ["first", "second", "third"]
        assert renderer.running_follow_up_counts == [1, 0]

    anyio.run(run)


def test_tui_shell_discards_queued_follow_ups_after_input_eof() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                )
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["first", "second"]),
        )

        await shell.run()

        assert controller.prompts == ["first"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "queued follow-up #1" in rendered
        assert "running queued follow-up" not in rendered
        assert "input closed; finishing current prompt" in rendered
        assert "waiting for current prompt" not in rendered

    anyio.run(run)


def test_tui_shell_clears_queued_follow_ups_after_failed_prompt() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [
                        RpcCommandFinished(
                            command_id="prompt-1",
                            command_type="prompt",
                            ok=False,
                            error="failed",
                        )
                    ],
                )
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["first", "second"]),
        )

        await shell.run()

        assert controller.prompts == ["first"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "queued follow-up #1" in rendered
        assert "running queued follow-up" not in rendered

    anyio.run(run)


def test_tui_shell_interrupt_cancels_running_prompt() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [
                        ErrorEvent(message="RPC command cancelled: prompt-1"),
                        RpcCommandFinished(
                            command_id="prompt-1",
                            command_type="prompt",
                            ok=False,
                            error="RPC command cancelled: prompt-1",
                        ),
                    ],
                )
            ]
        )

        interrupted = False

        async def read(prompt: str) -> str:
            nonlocal interrupted
            if prompt == "wisp> ":
                return "hello"
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.cancelled == ["prompt-1"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "Cancelling current prompt" in rendered
        assert "cancelled" in rendered
        assert "command failed" not in rendered
        assert "error: RPC command cancelled" not in rendered

    anyio.run(run)


def test_tui_shell_ignores_repeated_cancel_for_same_prompt() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(controller, console=console)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running

        first_exit = await shell._cancel_current("Cancelling current prompt...")
        second_exit = await shell._cancel_current("Cancelling current prompt...")

        assert first_exit is False
        assert second_exit is False
        assert controller.cancelled == ["prompt-1"]
        assert "cancel already requested" in output.getvalue()

    anyio.run(run)


def test_tui_shell_separates_streamed_text_from_tool_events() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    TokenDelta(delta="partial"),
                    ToolCallRequested(call_id="call-1", name="read", arguments={"path": "x"}),
                    ToolResultReady(
                        call_id="call-1",
                        name="read",
                        output="ok",
                        is_error=False,
                    ),
                    AssistantMessage(content="partial"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["use tool"]),
        )

        await shell.run()

        rendered = output.getvalue()
        assert "partial\n→ tool" in rendered
        assert rendered.count("partial") == 1

    anyio.run(run)


def test_tui_shell_denies_tool_approval() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_line(_InputLine(text="n", mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI")]
        assert shell.state.pending_approval is None

    anyio.run(run)


def test_tui_shell_does_not_approve_from_stale_running_input() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_line(
            _InputLine(text="yes", mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.approvals == []
        assert list(shell.state.queued_prompts) == ["yes"]

    anyio.run(run)


def test_tui_shell_denies_tool_approval_on_blank_answer() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_line(_InputLine(text="", mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI")]
        assert list(shell.state.queued_prompts) == []

    anyio.run(run)


def test_tui_shell_denies_approval_on_input_eof_then_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    ToolApprovalRequested(
                        call_id="call-1",
                        name="danger",
                        arguments={"path": "file.txt"},
                        safety="mutating",
                    )
                ]
            ],
            approval_events=[
                [
                    ToolApprovalResolved(
                        call_id="call-1",
                        name="danger",
                        approved=False,
                        reason="Denied from TUI: input closed",
                    ),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ],
        )

        async def read(prompt: str) -> str:
            if prompt == "wisp> ":
                return "use tool"
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.approvals == [("call-1", False, "Denied from TUI: input closed")]
        assert controller.shutdown_count == 1
        assert "Approval input closed" in output.getvalue()

    anyio.run(run)


def test_tui_shell_reports_prompt_send_failure() -> None:
    class FailingPromptController(ScriptedController):
        async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
            self.prompts.append(prompt)
            raise RuntimeError("[red]closed pipe[/red]")

    async def run() -> None:
        controller = FailingPromptController([])
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        rendered = output.getvalue()
        assert "failed to send prompt" in rendered
        assert "[red]closed pipe[/red]" in rendered

    anyio.run(run)


def test_tui_shell_reports_shutdown_failure_with_original_wording() -> None:
    class FailingShutdownController(ScriptedController):
        async def shutdown(self, *, command_id: str | None = None) -> str:
            self.shutdown_count += 1
            raise RuntimeError("closed")

    async def run() -> None:
        controller = FailingShutdownController()
        console, output = _console()
        shell = TuiShell(controller, console=console)

        should_exit = await shell._request_shutdown()

        assert should_exit is True
        assert "shutdown failed: closed" in output.getvalue()

    anyio.run(run)


def test_tui_shell_reports_rpc_eof_before_prompt_finished() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [[ErrorEvent(message="Unknown provider")]],
            close_after_prompt=True,
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert controller.shutdown_count == 0
        rendered = output.getvalue()
        assert "Unknown provider" in rendered
        assert "RPC event stream ended before command completed: prompt-1" in rendered

    anyio.run(run)


def test_tui_shell_escapes_approval_markup() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    ToolApprovalRequested(
                        call_id="call-1",
                        name="[red]write[/red]",
                        arguments={"path": "[black]hidden[/black]"},
                        safety="mutating",
                    ),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["use tool", "y"]),
        )

        await shell.run()

        rendered = output.getvalue()
        assert "[red]write[/red]" in rendered
        assert "[black]hidden[/black]" in rendered

    anyio.run(run)


def test_tui_shell_compacts_session_saved_path(tmp_path: Path) -> None:
    console, output = _console()
    shell = TuiShell(ScriptedController(), console=console)

    shell._render_event(
        SessionSaved(
            session_id="session-1",
            path=tmp_path / "20260703-123456-abcdef12.jsonl",
        )
    )

    rendered = output.getvalue()
    assert "session saved: 20260703-123456-abcdef12.jsonl" in rendered
    assert str(tmp_path) not in rendered


def test_tui_interaction_state_tracks_status() -> None:
    state = TuiInteractionState()

    assert state.status is TuiStatus.idle
    state.status = TuiStatus.running
    assert state.status is TuiStatus.running


def test_tui_rpc_command_includes_runtime_flags(tmp_path: Path) -> None:
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", model="model-x", session_dir=tmp_path),
            allow_read_tools=True,
            allowed_tools=("bash",),
            resume="session-123",
            approve_unsafe_tools=True,
            max_tool_iterations=3,
        )
    )

    assert command[:4] == (command[0], "-m", "wisp", "--mode")
    assert "rpc" in command
    assert ("--provider", "fake") == (
        command[command.index("--provider")],
        command[command.index("--provider") + 1],
    )
    assert ("--model", "model-x") == (
        command[command.index("--model")],
        command[command.index("--model") + 1],
    )
    assert ("--session-dir", str(tmp_path)) == (
        command[command.index("--session-dir")],
        command[command.index("--session-dir") + 1],
    )
    assert ("--resume", "session-123") == (
        command[command.index("--resume")],
        command[command.index("--resume") + 1],
    )
    assert "--allow-read-tools" in command
    assert ("--allow-tool", "bash") == (
        command[command.index("--allow-tool")],
        command[command.index("--allow-tool") + 1],
    )
    assert "--yes" in command
    assert ("--max-tool-iterations", "3") == (
        command[command.index("--max-tool-iterations")],
        command[command.index("--max-tool-iterations") + 1],
    )


def test_tui_rpc_command_includes_continue_latest(tmp_path: Path) -> None:
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", session_dir=tmp_path),
            continue_latest=True,
        )
    )

    assert "--continue" in command


def test_cli_tui_mode_validates_provider_before_prompting() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--mode", "tui", "--provider", "missing"])

    assert result.exit_code == 1
    assert "Unknown provider: missing" in result.output
    assert "Wisp TUI MVP" not in result.output


def test_cli_tui_mode_validates_continue_before_prompting(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui", "--provider", "fake", "--session-dir", str(tmp_path), "--continue"],
    )

    assert result.exit_code == 1
    assert "No sessions found" in result.output
    assert str(tmp_path.name) in result.output
    assert "Wisp TUI MVP" not in result.output


def test_cli_tui_mode_invokes_tui_runner(tmp_path: Path, monkeypatch: object) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--mode",
            "tui",
            "--provider",
            "fake",
            "--session-dir",
            str(tmp_path),
            "--continue",
            "--tui-renderer",
            "fullscreen",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].config.provider == "fake"
    assert captured[0].config.session_dir == tmp_path
    assert captured[0].continue_latest is True
    assert captured[0].renderer is TuiRendererKind.fullscreen
