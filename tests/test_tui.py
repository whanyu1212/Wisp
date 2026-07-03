from __future__ import annotations

import io
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
    KnownWispEvent,
    RpcCommandFinished,
    TokenDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolResultReady,
)
from wisp.tui import TuiOptions, TuiShell
from wisp.tui.app import _rpc_command


class ScriptedController:
    def __init__(self, event_batches: list[list[KnownWispEvent]]) -> None:
        self.event_batches = deque(event_batches)
        self.prompts: list[str] = []
        self.approvals: list[tuple[str, bool, str | None]] = []
        self.cancelled: list[str] = []
        self.shutdown_count = 0
        self.closed = False

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
        self.prompts.append(prompt)
        return command_id or "prompt-1"

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str:
        self.cancelled.append(target_id)
        return command_id or "cancel-1"

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        command_id: str | None = None,
    ) -> str:
        self.approvals.append((call_id, approved, reason))
        return command_id or "approval-1"

    async def shutdown(self, *, command_id: str | None = None) -> str:
        self.shutdown_count += 1
        return command_id or "shutdown-1"

    def events(self) -> AsyncIterator[KnownWispEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[KnownWispEvent]:
        batch = self.event_batches.popleft()
        for event in batch:
            yield event

    async def close(self) -> None:
        self.closed = True


async def _reader_from(inputs: list[str]) -> object:
    values = deque(inputs)

    async def read(_prompt: str) -> str:
        return values.popleft()

    return read


def _console() -> tuple[Console, io.StringIO]:
    output = io.StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_tui_shell_runs_prompt_then_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    TokenDelta(delta="hello"),
                    AssistantMessage(content="hello"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ],
                [RpcCommandFinished(command_id="shutdown-1", command_type="shutdown", ok=True)],
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello", "/quit"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert controller.shutdown_count == 1
        assert "Wisp TUI MVP" in output.getvalue()
        assert "hello" in output.getvalue()

    anyio.run(run)


def test_tui_shell_denies_tool_approval() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    ToolApprovalRequested(
                        call_id="call-1",
                        name="danger",
                        arguments={"path": "file.txt"},
                        safety="mutating",
                    ),
                    ToolApprovalResolved(
                        call_id="call-1",
                        name="danger",
                        approved=False,
                        reason="Denied from TUI",
                    ),
                    ToolResultReady(
                        call_id="call-1",
                        name="danger",
                        output="Denied from TUI",
                        is_error=True,
                    ),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ],
                [RpcCommandFinished(command_id="shutdown-1", command_type="shutdown", ok=True)],
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["use tool", "n", "/quit"]),
        )

        await shell.run()

        assert controller.approvals == [("call-1", False, "Denied from TUI")]
        assert "approval required" in output.getvalue()
        assert "denied" in output.getvalue()

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
                ],
                [RpcCommandFinished(command_id="shutdown-1", command_type="shutdown", ok=True)],
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["use tool", "y", "/quit"]),
        )

        await shell.run()

        rendered = output.getvalue()
        assert "[red]write[/red]" in rendered
        assert "[black]hidden[/black]" in rendered

    anyio.run(run)


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
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].config.provider == "fake"
    assert captured[0].config.session_dir == tmp_path
    assert captured[0].continue_latest is True
