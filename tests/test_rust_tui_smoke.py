from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from wisp import __version__

# Shared by the deliberately small inline backends below; use real event models so
# the Rust projection is checked against Python's complete live wire contract.
_CONTEXT_STATS_BACKEND = """
from wisp.events import (
    ContextBudget, ContextEstimate, SessionStats, SessionStatsReported, TokenUsage,
    CompactionPolicyStatus, SessionCostSummary, RpcCommandFinished,
    RpcSkillsReported, RpcSkillCatalogSnapshot,
)

def report_skills(command):
    print(RpcSkillsReported(command_id=command["id"],
        catalog=RpcSkillCatalogSnapshot()).model_dump_json(), flush=True)
    print(RpcCommandFinished(command_id=command["id"],
        command_type="get_skills", ok=True).model_dump_json(), flush=True)

def report_stats(command, session_id=None, enabled=True):
    budget = ContextBudget(estimate=ContextEstimate(system_tokens=100,
        message_tokens=2800, tool_schema_tokens=100, total_tokens=3000),
        context_window=10000, reserve_tokens=1000)
    stats = SessionStats(session_id=session_id, entry_count=4, active_message_count=4,
        compaction_count=0, usage_record_count=1,
        usage=TokenUsage(input_tokens=2900, output_tokens=100, total_tokens=3000),
        context=budget, compaction=CompactionPolicyStatus(auto_compaction_enabled=enabled,
        threshold_eligible=True, threshold_ineligible_reason=None),
        cost=SessionCostSummary(known_usd="0.0123", complete=False,
            priced_record_count=1, unpriced_record_count=1))
    print(SessionStatsReported(command_id=command["id"], stats=stats).model_dump_json(), flush=True)
    print(RpcCommandFinished(command_id=command["id"],
        command_type="get_session_stats", ok=True).model_dump_json(), flush=True)
"""


def _complete_logged_commands(path: Path) -> list[dict[str, object]]:
    """Ignore a final record until the backend finishes writing its newline."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1]]


def test_command_log_ignores_incomplete_records(tmp_path: Path) -> None:
    path = tmp_path / "commands.jsonl"
    assert _complete_logged_commands(path) == []
    path.write_bytes(b'{"type":"prompt"}\n{"type":"follow_up"')
    assert _complete_logged_commands(path) == [{"type": "prompt"}]
    with path.open("ab") as log:
        log.write(b"}\n")
    assert _complete_logged_commands(path) == [{"type": "prompt"}, {"type": "follow_up"}]


@pytest.mark.process
def test_rust_model_selection_survives_restart(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    # conftest gives both execve children the same isolated home and project.
    settings_path = Path.home() / ".wisp" / "settings.json"

    def run_selection(*, restart: bool, picker_only: bool = False) -> None:
        environment = {
            **os.environ,
            "WISP_RUST_TUI_BINARY": str(binary),
            "WISP_TRUST": "1",
        }
        for key in ("WISP_PROVIDER", "WISP_MODEL", "WISP_EFFORT"):
            environment.pop(key, None)
        if not restart:
            environment["WISP_PROVIDER"] = "fake"
        child_pid, terminal_fd = pty.fork()
        if child_pid == 0:
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "wisp",
                    "tui",
                    "--renderer",
                    "rust",
                    "--session-dir",
                    str(tmp_path / ("restart" if restart else "initial")),
                ],
                environment,
            )
        fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        initial_terminal = termios.tcgetattr(terminal_fd)
        output = bytearray()
        status: int | None = None
        submitted = restart
        picker_phase = "startup" if picker_only else "done"
        picker_width = 121
        next_picker_frame = 0.0
        quit_sent = False
        context_redrawn = False
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([terminal_fd], [], [], 0.05)
                if readable:
                    try:
                        output.extend(os.read(terminal_fd, 65536))
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                if not context_redrawn and b"ctx" in output and b"~" in output:
                    # Context readiness replaces a loading hint. Request one full frame;
                    # differential writes can omit letters shared by the two hints.
                    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 121, 0, 0))
                    context_redrawn = True
                # Hydration can show the prompt hint before the startup reads finish.
                ready = context_redrawn and b"Type a prompt or / for commands." in output
                if picker_phase == "startup" and ready and b"fake/fake" in output:
                    os.write(terminal_fd, b"/model\r")
                    picker_phase = "loading"
                    output.clear()
                elif picker_phase == "loading" and b"Current:" in output:
                    # The fake provider is hidden; inspect the first real catalog model
                    # before staging effort. Configuration makes no provider request.
                    picker_phase = "effort"
                    output.clear()
                elif (
                    picker_phase == "effort"
                    and b"Effort:" in output
                    and b"Default" in output
                    and b"apply" in output
                ):
                    os.write(terminal_fd, b"\x1b[C")
                    # Do not race navigation with a resize. If an intervening redraw
                    # rejects the key, the next unchanged frame retries it.
                    next_picker_frame = time.monotonic() + 0.1
                    output.clear()
                elif (
                    picker_phase == "effort"
                    and b"Effort:" in output
                    and b"Default" not in output
                    and b"apply" in output
                ):
                    os.write(terminal_fd, b"\r")
                    picker_phase = "applying"
                    submitted = True
                    output.clear()
                elif picker_phase == "applying" and b"Model selection applied." in output:
                    saved = json.loads(settings_path.read_text())
                    assert saved["provider"] != "fake" and saved["model"]
                    assert saved["effort"]
                    picker_phase = "done"
                elif not submitted and not picker_only and ready and b"fake/fake" in output:
                    os.write(terminal_fd, b"/model fake::custom-model effort\r")
                    submitted = True
                    output.clear()
                applied = (
                    ready and b"fake/custom-model" in output and b"effort" in output
                    if restart
                    else (
                        b"Model selection applied." in output
                        and (picker_only or (b"custom-model" in output and b"effort" in output))
                    )
                )
                if submitted and applied and not quit_sent:
                    os.write(terminal_fd, b"\x03")
                    quit_sent = True
                if picker_phase == "effort" and time.monotonic() >= next_picker_frame:
                    # Observe the staged effort in a full frame before activating it.
                    picker_width = 123 if picker_width == 122 else 122
                    fcntl.ioctl(
                        terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, picker_width, 0, 0)
                    )
                    next_picker_frame = time.monotonic() + 0.1
                    output.clear()
                waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    status = waited_status
                    break
            assert status is not None, (
                f"Rust model selection timed out in {picker_phase!r}: {bytes(output)!r}"
            )
            assert submitted and quit_sent, bytes(output)
            if not picker_only:
                assert b"custom-model" in output and b"effort" in output, bytes(output)
            assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
            assert termios.tcgetattr(terminal_fd) == initial_terminal
        finally:
            if status is None:
                try:
                    os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            os.close(terminal_fd)

    run_selection(restart=False, picker_only=True)
    run_selection(restart=False)
    assert json.loads(settings_path.read_text()) == {
        "provider": "fake",
        "model": "custom-model",
        "effort": "effort",
    }
    # No provider/model/effort environment overrides: prove defaults are restored.
    run_selection(restart=True)


@pytest.mark.process
@pytest.mark.parametrize("mouse_enabled", [False, True], ids=["keyboard", "mouse"])
@pytest.mark.parametrize(
    ("exercise_prompt", "expected_exit"),
    [(True, 0), (False, 1)],
    ids=["prompt-stream-and-quit", "frontend-killed"],
)
def test_rust_tui_cross_language_smoke(
    tmp_path: Path,
    exercise_prompt: bool,
    expected_exit: int,
    mouse_enabled: bool,
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        environment = {
            **os.environ,
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_RUST_TUI_BINARY": str(binary),
            "WISP_TRUST": "1",
            "WISP_TUI_MOUSE": "1" if mouse_enabled else "0",
        }
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--renderer",
                "rust",
                "--session-dir",
                str(tmp_path),
            ],
            environment,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    context_redrawn = False
    deadline = time.monotonic() + 20
    prompt_sent = False
    response_seen = False
    quit_sent = False
    terminal_output = bytearray()
    rust_process_group: int | None = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    terminal_output.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 81, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if (
                context_redrawn
                and not prompt_sent
                and b"Type a prompt or / for commands." in output
            ):
                if not exercise_prompt:
                    rust_process_group = os.tcgetpgrp(terminal_fd)
                    os.kill(rust_process_group, signal.SIGKILL)
                    quit_sent = True
                else:
                    os.write(terminal_fd, b"hello rust\r")
                prompt_sent = True
            # Streaming can draw "fake" and "response" in separate frames with
            # cursor movements between them; the raw PTY bytes need not be adjacent.
            if exercise_prompt and not response_seen and b"response" in output:
                response_seen = True
            if (
                exercise_prompt
                and response_seen
                and not quit_sent
                and output.rfind(b"idle") > output.rfind(b"working")
            ):
                # Ctrl-C while the prompt is still running now cancels rather
                # than quitting. Wait until the header returns to idle first.
                os.write(terminal_fd, b"\x03")
                quit_sent = True
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI smoke test timed out; output={bytes(output)!r}")
    finally:
        if status is None:
            try:
                foreground_pgrp = os.tcgetpgrp(terminal_fd)
                os.killpg(foreground_pgrp, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert prompt_sent, bytes(output)
    assert quit_sent, bytes(output)
    for mode in (1000, 1006):
        assert (f"\x1b[?{mode}h".encode() in terminal_output) is mouse_enabled
        if mouse_enabled:
            assert f"\x1b[?{mode}l".encode() in terminal_output
    assert b"\x1b[?1003h" not in terminal_output, "navigation must not request all-motion reports"
    if exercise_prompt:
        assert response_seen, bytes(output)
        # Ratatui may place cursor-control sequences between adjacent cells, so
        # assert the submitted prompt's words independently in the PTY stream.
        assert b"hello" in output
        assert b"rust" in output
    assert os.waitstatus_to_exitcode(status) == expected_exit, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal
    if rust_process_group is not None:
        with pytest.raises(ProcessLookupError):
            os.killpg(rust_process_group, 0)


@pytest.mark.process
def test_rust_tui_renders_bounded_tool_and_process_cards(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "tool_backend.py"
    backend.write_text(
        _CONTEXT_STATS_BACKEND
        + """
import json
import sys

from wisp.events import RpcCommandFinished, RpcMessagesReported, ToolCallRequested, ToolResultReady


def emit(event):
    print(event.model_dump_json(), flush=True)


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "protocol_version": 6,
    "event_schema_version": 37,
    "min_protocol_version": 6,
    "max_protocol_version": 6,
    "capabilities": [],
    "limits": {
        "max_client_frame_bytes": 67108864,
        "max_server_frame_bytes": 67108864
    }
}), flush=True)

for line in sys.stdin:
    command = json.loads(line)
    command_type = command["type"]
    command_id = command["id"]
    if command_type == "get_session_stats":
        report_stats(command)
    elif command_type == "get_skills":
        report_skills(command)
    elif command_type == "get_messages":
        emit(RpcMessagesReported(
            command_id=command_id,
            session_id=command.get("session_id"),
        ))
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type="get_messages",
            ok=True,
        ))
    elif command_type == "prompt":
        emit(ToolCallRequested(
            call_id="read-1",
            name="read",
            arguments={"path": "README.md"},
        ))
        emit(ToolResultReady(
            call_id="read-1",
            name="read",
            output="contents",
            is_error=False,
            summary="Read README.md",
        ))
        emit(ToolCallRequested(
            call_id="poll-1",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        ))
        emit(ToolResultReady(
            call_id="poll-1",
            name="bash",
            output="running",
            is_error=False,
            process_id="process-1",
            process_state="running",
            stdout="first chunk",
        ))
        emit(ToolCallRequested(
            call_id="poll-2",
            name="bash",
            arguments={"operation": "poll", "process_id": "process-1"},
        ))
        emit(ToolResultReady(
            call_id="poll-2",
            name="bash",
            output="completed",
            is_error=False,
            process_id="process-1",
            process_state="completed",
            stdout="safe\\u001b[2Jtail",
        ))
        emit(ToolCallRequested(
            call_id="edit-1",
            name="edit",
            arguments={
                "path": "demo.txt",
                "edits": [{
                    "oldText": "old\\u001b[2J value\\n",
                    "newText": "new value\\n",
                }],
            },
        ))
        emit(ToolResultReady(
            call_id="edit-1",
            name="edit",
            output="Applied 1 edit",
            is_error=False,
        ))
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type="prompt",
            ok=True,
        ))
    elif command_type == "shutdown":
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type="shutdown",
            ok=True,
        ))
        break
    else:
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=True,
        ))
""",
        encoding="utf-8",
    )

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
            ],
            os.environ,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    prompt_sent = False
    browse_sent = False
    detail_seen = False
    detail_resize_output_offset: int | None = None
    resized_detail_at: float | None = None
    quit_sent = False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not prompt_sent and b"Type a prompt or / for commands." in output:
                os.write(terminal_fd, b"tools\r")
                prompt_sent = True
            cards_visible = all(
                marker in output
                for marker in (
                    b"README.md",
                    b"Process completed",
                    b"demo.txt",
                )
            )
            if (
                cards_visible
                and not browse_sent
                and output.rfind(b"idle") > output.rfind(b"working")
            ):
                # F6 selects the last card (edit), BackTab+Right expands the
                # process card so sanitized stdout is painted, Tab returns to
                # edit, and Enter opens retained detail.
                os.write(terminal_fd, b"\x1b[17~\x1b[Z\x1b[C\t\r")
                browse_sent = True
            if (
                browse_sent
                and not detail_seen
                and b"live retained detail" in output
                and b"Esc close" in output
            ):
                detail_seen = True
                detail_resize_output_offset = len(output)
                fcntl.ioctl(
                    terminal_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 30, 120, 0, 0),
                )
            if (
                detail_resize_output_offset is not None
                and resized_detail_at is None
                and b"Esc close" in output[detail_resize_output_offset:]
            ):
                resized_detail_at = time.monotonic()
            if (
                resized_detail_at is not None
                and not quit_sent
                and time.monotonic() - resized_detail_at >= 0.5
            ):
                os.write(terminal_fd, b"\x03")
                quit_sent = True
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI tool-card test timed out; output={bytes(output)!r}")
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    assert prompt_sent, bytes(output)
    assert browse_sent, bytes(output)
    assert detail_seen, bytes(output)
    assert detail_resize_output_offset is not None, bytes(output)
    assert resized_detail_at is not None, bytes(output)
    assert quit_sent, bytes(output)
    assert b"README.md" in output
    assert b"Process completed" in output
    assert b"safe" in output
    assert b"safe\xef\xbf\xbd[2J" in output
    assert b"safe\x1b[2J" not in output
    assert b"old\xef\xbf\xbd[2J" in output
    assert b"old\x1b[2J" not in output
    assert b"live retained detail" in output
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal


@pytest.mark.process
def test_rust_tui_queue_lifecycle_over_pty(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    project = tmp_path / "project"
    project.mkdir()
    backend = tmp_path / "queue_backend.py"
    command_log = tmp_path / "commands.jsonl"
    backend.write_text(
        _CONTEXT_STATS_BACKEND
        + """
import json
import sys
from pathlib import Path

from wisp.events import QueueItemsRemoved, QueueUpdated, RpcCommandFinished, RpcMessagesReported


command_log = Path(sys.argv[1])
active_prompt_id = None
steering_content = "steer-via-enter"
follow_up_content = "follow-up-via-alt-enter"


def emit(event):
    print(event.model_dump_json(), flush=True)


def finish(command, *, ok=True, error=None):
    emit(RpcCommandFinished(
        command_id=command["id"],
        command_type=command["type"],
        ok=ok,
        error=error,
    ))


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "protocol_version": 6,
    "event_schema_version": 37,
    "min_protocol_version": 6,
    "max_protocol_version": 6,
    "capabilities": [],
    "limits": {
        "max_client_frame_bytes": 67108864,
        "max_server_frame_bytes": 67108864,
    },
}), flush=True)

for line in sys.stdin:
    command = json.loads(line)
    with command_log.open("a", encoding="utf-8") as log:
        log.write(json.dumps(command, sort_keys=True) + "\\n")
    command_type = command["type"]
    if command_type == "get_session_stats":
        report_stats(command)
    elif command_type == "get_skills":
        report_skills(command)
    elif command_type == "get_messages":
        emit(RpcMessagesReported(
            command_id=command["id"],
            session_id=command.get("session_id"),
        ))
        finish(command)
    elif command_type == "get_queue_state":
        emit(QueueUpdated())
        finish(command)
    elif command_type == "prompt":
        active_prompt_id = command["id"]
    elif command_type == "steer":
        emit(QueueUpdated(steering=(command["content"],)))
        finish(command)
    elif command_type == "follow_up":
        emit(QueueUpdated(
            steering=(steering_content,),
            follow_up=(command["content"],),
        ))
        finish(command)
    elif command_type == "pop_queue":
        emit(QueueItemsRemoved(
            command_id=command["id"],
            operation="pop",
            kind="follow_up",
            follow_up=(follow_up_content,),
        ))
        emit(QueueUpdated(steering=(steering_content,)))
        finish(command)
    elif command_type == "cancel":
        finish(command)
        emit(RpcCommandFinished(
            command_id=active_prompt_id,
            command_type="prompt",
            ok=False,
            error="RPC command cancelled: requested by user",
        ))
    elif command_type == "shutdown":
        finish(command)
        break
    else:
        finish(command)
""",
        encoding="utf-8",
    )

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                str(command_log),
            ],
            os.environ,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    phase = "startup"
    phase_output_offset = 0
    commands: list[dict[str, object]] = []
    restore_output_offset: int | None = None
    queue_update_output_offset: int | None = None
    idle_output_offset: int | None = None
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            commands = _complete_logged_commands(command_log)
            command_types = [command["type"] for command in commands]
            if (
                phase == "startup"
                and b"Type a prompt or / for commands." in output
                and command_types[:7]
                == [
                    "get_messages",
                    "get_connection_catalog",
                    "get_model_catalog",
                    "get_commands",
                    "get_state",
                    "get_skills",
                    "get_queue_state",
                ]
            ):
                phase_output_offset = len(output)
                os.write(
                    terminal_fd,
                    b"\x1b[200~history exact first\nneedle second\x1b[201~\r",
                )
                phase = "prompt sent"
            elif (
                phase == "prompt sent"
                and "prompt" in command_types
                and b"s:0/l:0" in output[phase_output_offset:]
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"steer-via-enter\r")
                phase = "steer sent"
            elif (
                phase == "steer sent"
                and "steer" in command_types
                and b"steer: steer-via-enter" in output[phase_output_offset:]
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"follow-up-via-alt-enter")
                phase = "follow-up typed"
            elif (
                phase == "follow-up typed"
                and b"follow-up-via-alt-enter" in output[phase_output_offset:]
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\x1b\r")
                phase = "follow-up sent"
            elif phase == "follow-up sent" and "follow_up" in command_types:
                restore_output_offset = len(output)
                os.write(terminal_fd, b"\x1b[1;3A")
                phase = "restore sent"
            elif (
                phase == "restore sent"
                and "pop_queue" in command_types
                and restore_output_offset is not None
            ):
                queue_update_output_offset = restore_output_offset
                idle_output_offset = len(output)
                os.write(terminal_fd, b"\x03")
                phase = "cancel sent"
            elif (
                phase == "cancel sent"
                and "cancel" in command_types
                and idle_output_offset is not None
                and b"idle" in output[idle_output_offset:]
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\x12")
                phase = "history opened"
            elif phase == "history opened" and all(
                marker in output[phase_output_offset:]
                for marker in (b"Prompt", b"history", b"Enter restore")
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\x1b[200~needle\x1b[201~")
                phase = "history filtered"
            elif phase == "history filtered" and b"needle" in output[phase_output_offset:]:
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\r")
                phase = "history restored"
            elif phase == "history restored" and all(
                marker in output[phase_output_offset:]
                for marker in (b"history exact first", b"needle second")
            ):
                # The footer remains visible under history, so closing it need not
                # repaint the input hint. Wait until restore has consumed Enter before
                # resizing, which invalidates the popup's activation readiness.
                phase_output_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 101, 0, 0))
                phase = "history restored frame"
            elif phase == "history restored frame" and all(
                marker in output[phase_output_offset:]
                for marker in (b"history", b"exact", b"first", b"needle", b"second", b"Enter send")
            ):
                assert command_types.count("prompt") == 1, "history must not submit"
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\x05 edited")
                phase = "restored prompt edited"
            elif phase == "restored prompt edited" and b"edited" in output[phase_output_offset:]:
                assert command_types.count("prompt") == 1, "editing must not submit"
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\r")
                phase = "restored prompt sent"
            elif phase == "restored prompt sent" and command_types.count("prompt") == 2:
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\x03")
                phase = "second cancel sent"
            elif (
                phase == "second cancel sent"
                and command_types.count("cancel") == 2
                and b"idle" in output[phase_output_offset:]
            ):
                os.write(terminal_fd, b"\x03")
                phase = "shutdown sent"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(
                f"Rust TUI queue lifecycle timed out in phase {phase!r}; "
                f"recent commands={commands[-8:]!r}; "
                f"terminal tail={bytes(output[-6000:])!r}"
            )
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    commands = _complete_logged_commands(command_log)
    command_types = [command["type"] for command in commands]
    lifecycle_commands = [
        command
        for command in commands
        if command["type"] in {"prompt", "steer", "follow_up", "pop_queue", "cancel", "shutdown"}
    ]
    assert phase == "shutdown sent", bytes(output)
    assert command_types[:7] == [
        "get_messages",
        "get_connection_catalog",
        "get_model_catalog",
        "get_commands",
        "get_state",
        "get_skills",
        "get_queue_state",
    ]
    assert [command["type"] for command in lifecycle_commands] == [
        "prompt",
        "steer",
        "follow_up",
        "pop_queue",
        "cancel",
        "prompt",
        "cancel",
        "shutdown",
    ]
    prompt, steer, follow_up, pop_queue, cancel, recalled, second_cancel, _shutdown = (
        lifecycle_commands
    )
    assert prompt["prompt"] == "history exact first\nneedle second"
    assert recalled["prompt"] == "history exact first\nneedle second edited"
    assert second_cancel["target_id"] == recalled["id"]
    assert steer["content"] == "steer-via-enter"
    assert follow_up["content"] == "follow-up-via-alt-enter"
    assert pop_queue["kind"] == "follow_up"
    assert cancel["target_id"] == prompt["id"]
    assert restore_output_offset is not None
    assert queue_update_output_offset is not None
    assert b"follow-up" in output
    assert b"via-alt-enter" in output
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal


@pytest.mark.process
def test_rust_tui_session_workflows_over_pty(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "session_backend.py"
    command_log = tmp_path / "session_commands.jsonl"
    backend.write_text(
        _CONTEXT_STATS_BACKEND
        + """
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from wisp.events import (
    ProjectConfigApplied,
    QueueUpdated,
    RpcCommandFinished,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcMessagesReported,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionTreeNavigated,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
    RpcSessionTreeUnreverted,
)


command_log = Path(sys.argv[1])
current_session = "source"
current_name = "source-name"
active_leaf = "entry-user"
unrevert_pending = False


def session_path(session_id):
    return Path(f"/sessions/{session_id}.jsonl")


def emit(event):
    print(event.model_dump_json(), flush=True)


def finish(command):
    emit(RpcCommandFinished(
        command_id=command["id"],
        command_type=command["type"],
        ok=True,
    ))


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "protocol_version": 6,
    "event_schema_version": 37,
    "min_protocol_version": 6,
    "max_protocol_version": 6,
    "capabilities": [],
    "limits": {
        "max_client_frame_bytes": 67108864,
        "max_server_frame_bytes": 67108864,
    },
}), flush=True)

for line in sys.stdin:
    command = json.loads(line)
    with command_log.open("a", encoding="utf-8") as log:
        log.write(json.dumps(command, sort_keys=True) + "\\n")
    command_type = command["type"]
    if command_type == "get_session_stats":
        report_stats(command, current_session)
    elif command_type == "get_connection_catalog":
        emit(RpcConnectionCatalogReported(
            command_id=command["id"], catalog=RpcConnectionCatalogSnapshot(),
        ))
        finish(command)
    elif command_type == "get_skills":
        report_skills(command)
    elif command_type == "get_messages":
        emit(RpcMessagesReported(
            command_id=command["id"],
            session_id=current_session,
            session_path=session_path(current_session),
            active_leaf_id=active_leaf,
        ))
        finish(command)
        if unrevert_pending:
            emit(ProjectConfigApplied(
                provider="unrevert-ready",
                auth_path=Path("/tmp/auth.json"),
            ))
            unrevert_pending = False
    elif command_type == "get_queue_state":
        emit(QueueUpdated())
        finish(command)
    elif command_type == "set_session_name":
        previous_name = current_name
        current_name = "server-confirmed"
        finish(command)
        emit(RpcSessionNameChanged(
            command_id=command["id"],
            session_id=current_session,
            session_path=session_path(current_session),
            previous_name=previous_name,
            name=current_name,
            entry_count=2,
        ))
    elif command_type == "clone_session":
        source_session = current_session
        source_name = current_name
        source_leaf = active_leaf
        current_session = "clone"
        current_name = "cloned-backend"
        emit(RpcSessionCloned(
            command_id=command["id"],
            source_session_id=source_session,
            source_session_path=session_path(source_session),
            source_active_leaf_id=source_leaf,
            source_session_name=source_name,
            session_id=current_session,
            session_path=session_path(current_session),
            active_leaf_id=active_leaf,
            session_name=current_name,
            entry_count=2,
        ))
        finish(command)
    elif command_type == "get_session_tree":
        node = RpcSessionTreeNode(
            entry_id="entry-user",
            parent_id=None,
            operation_id="prompt-1",
            created_at=datetime(2026, 8, 31, tzinfo=UTC),
            kind="message",
            role="user",
            preview="tree-node-preview",
        )
        finish(command)
        emit(RpcSessionTreeReported(
            command_id=command["id"],
            session_id=current_session,
            session_path=session_path(current_session),
            active_leaf_id=active_leaf,
            total_node_count=1,
            nodes=(node,),
        ))
    elif command_type == "navigate_session_tree":
        previous_leaf = active_leaf
        active_leaf = None
        emit(RpcSessionTreeNavigated(
            command_id=command["id"],
            session_id=current_session,
            session_path=session_path(current_session),
            selected_entry_id=command["entry_id"],
            previous_active_leaf_id=previous_leaf,
            active_leaf_id=active_leaf,
            editor_text="nav-restored-z",
            changed=True,
            entry_count=3,
        ))
        finish(command)
    elif command_type == "fork_session":
        source_session = current_session
        source_name = current_name
        source_leaf = active_leaf
        current_session = "fork"
        current_name = "forked-backend"
        finish(command)
        emit(RpcSessionForked(
            command_id=command["id"],
            source_session_id=source_session,
            source_session_path=session_path(source_session),
            source_active_leaf_id=source_leaf,
            source_session_name=source_name,
            session_id=current_session,
            session_path=session_path(current_session),
            active_leaf_id=active_leaf,
            session_name=current_name,
            entry_count=1,
            selected_entry_id=command["entry_id"],
            selected_prompt="fork-restored-z",
        ))
    elif command_type == "unrevert_session_tree":
        previous_leaf = active_leaf
        active_leaf = "entry-user"
        emit(RpcSessionTreeUnreverted(
            command_id=command["id"],
            session_id=current_session,
            session_path=session_path(current_session),
            source_transition_id="transition-1",
            previous_active_leaf_id=previous_leaf,
            active_leaf_id=active_leaf,
            entry_count=2,
        ))
        finish(command)
        unrevert_pending = True
    elif command_type == "shutdown":
        finish(command)
        break
    else:
        finish(command)
""",
        encoding="utf-8",
    )

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                str(command_log),
            ],
            os.environ,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 110, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    phase = "startup"
    phase_started = time.monotonic()
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            now = time.monotonic()
            commands = []
            if command_log.exists():
                for line in command_log.read_text().splitlines():
                    try:
                        commands.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            command_types = [command["type"] for command in commands]
            if (
                phase == "startup"
                and command_types[:7]
                == [
                    "get_messages",
                    "get_connection_catalog",
                    "get_model_catalog",
                    "get_commands",
                    "get_state",
                    "get_skills",
                    "get_queue_state",
                ]
                and b"Type a prompt" in output
            ):
                os.write(terminal_fd, b"/name client-requested\r")
                phase = "name"
                phase_started = now
            elif (
                phase == "name"
                and "set_session_name" in command_types
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"/clone\r")
                phase = "clone"
                phase_started = now
            elif (
                phase == "clone"
                and "clone_session" in command_types
                and command_types.count("get_messages") >= 2
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"/tree\r")
                phase = "first tree"
                phase_started = now
            elif (
                phase == "first tree"
                and command_types.count("get_session_tree") >= 1
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\r")
                phase = "navigate"
                phase_started = now
            elif (
                phase == "navigate"
                and "navigate_session_tree" in command_types
                and command_types.count("get_messages") >= 3
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\x7f" * len("nav-restored-z"))
                os.write(terminal_fd, b"/tree\r")
                phase = "second tree"
                phase_started = now
            elif (
                phase == "second tree"
                and command_types.count("get_session_tree") >= 2
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"f")
                phase = "fork"
                phase_started = now
            elif (
                phase == "fork"
                and "fork_session" in command_types
                and command_types.count("get_messages") >= 4
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\x7f" * len("fork-restored-z"))
                os.write(terminal_fd, b"/unrevert\r")
                phase = "unrevert"
                phase_started = now
            elif (
                phase == "unrevert"
                and "unrevert_session_tree" in command_types
                and command_types.count("get_messages") >= 5
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\x03")
                phase = "shutdown"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI session workflow test timed out; output={bytes(output)!r}")
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)

    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    workflows = [
        command
        for command in commands
        if command["type"]
        in {
            "set_session_name",
            "clone_session",
            "get_session_tree",
            "navigate_session_tree",
            "fork_session",
            "unrevert_session_tree",
        }
    ]
    assert phase == "shutdown", bytes(output)
    assert [command["type"] for command in workflows] == [
        "set_session_name",
        "clone_session",
        "get_session_tree",
        "navigate_session_tree",
        "get_session_tree",
        "fork_session",
        "unrevert_session_tree",
    ]
    assert workflows[0]["name"] == "client-requested"
    assert workflows[3]["entry_id"] == "entry-user"
    assert workflows[5]["entry_id"] == "entry-user"
    assert b"nav-restored-z" in output
    assert b"fork-restored-z" in output
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal


@pytest.mark.process
def test_rust_tui_command_discovery_and_modes_over_pty(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    command_log = tmp_path / "commands.jsonl"
    backend = tmp_path / "commands_backend.py"
    backend.write_text(
        _CONTEXT_STATS_BACKEND
        + """
import json
import sys
from pathlib import Path
from wisp.events import (
    RpcCommandDescriptor, RpcCommandFinished, RpcCommandsReported,
    RpcMessagesReported, RpcStateReported, RpcStateSnapshot,
    RpcConnectionCatalogReported, RpcConnectionCatalogSnapshot,
    CompactionStarted, CompactionCompleted, ContextEstimated, SkillInvoked,
    MessageStarted, MessageDelta,
    RpcMcpStatusReported, RpcMcpStatusSnapshot, RpcMcpServerSnapshot,
)
from wisp.runtime.builtin_commands import builtin_command_descriptors
from wisp.skills.models import SkillInvocationEvidence

log_path = Path(sys.argv[1])
mode = "plan"
active = None
auto_compaction = True

def emit(event):
    print(event.model_dump_json(), flush=True)

def finish(command):
    emit(RpcCommandFinished(command_id=command["id"], command_type=command["type"], ok=True))

request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "protocol_version": 6, "event_schema_version": 37,
    "min_protocol_version": 6, "max_protocol_version": 6,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    with log_path.open("a") as log:
        log.write(json.dumps(command) + "\\n")
    kind = command["type"]
    if kind == "get_session_stats":
        report_stats(command, "context-session", auto_compaction)
        continue
    elif kind == "get_skills":
        report_skills(command)
        continue
    elif kind == "get_mcp_status":
        emit(RpcMcpStatusReported(command_id=command["id"], status=RpcMcpStatusSnapshot(
            servers=(RpcMcpServerSnapshot(name="search", status="connected",
                tool_names=("mcp__search__query",)),))))
        emit(MessageDelta(turn=1, delta="LIVEBG\\n"))
    elif kind == "get_messages":
        emit(RpcMessagesReported(command_id=command["id"], session_id="context-session",
                                 session_path=Path("/context-session.jsonl"),
                                 active_leaf_id="leaf"))
    elif kind == "get_connection_catalog":
        emit(RpcConnectionCatalogReported(command_id=command["id"],
                                         catalog=RpcConnectionCatalogSnapshot()))
    elif kind == "get_commands":
        emit(RpcCommandsReported(command_id=command["id"], commands=tuple(
            RpcCommandDescriptor(
                name=d.name, title=d.title, description=d.description, category=d.category,
                slash_command=d.slash_command, slash_aliases=d.slash_aliases, order=d.order,
            ) for d in builtin_command_descriptors()
        )))
    elif kind == "get_state":
        emit(RpcStateReported(command_id=command["id"], state=RpcStateSnapshot(
            provider="fake", mode=mode, auto_compaction_enabled=True,
            steering_mode="one_at_a_time", follow_up_mode="one_at_a_time",
            pending_steering_count=0, pending_follow_up_count=0,
        )))
    elif kind == "configure":
        mode = command.get("mode", mode)
        auto_compaction = command.get("auto_compaction_enabled", auto_compaction)
    elif kind == "compact":
        active = command
        emit(CompactionStarted(session_id="context-session", source_entry_count=4))
        continue
    elif kind == "cancel":
        finish(command)
        if active:
            emit(CompactionCompleted(session_id="context-session", outcome="cancelled",
                replaced_entry_count=2, retained_entry_count=2, error="Compaction cancelled"))
            emit(RpcCommandFinished(command_id=active["id"], command_type=active["type"],
                ok=False, error="RPC command cancelled: user request"))
            active = None
        continue
    elif kind == "prompt":
        active = command
        emit(MessageStarted(turn=1))
        emit(SkillInvoked(session_id="context-session", message_entry_id="skill-message",
            invocation=SkillInvocationEvidence(name="review", original_content=command["prompt"],
                request="keep running", content_sha256="0" * 64),
            provider_content="EXPANDED_BODY_MUST_STAY_OUT_OF_THE_TRANSCRIPT"))
        emit(ContextEstimated(turn=1, provider="fake", budget=ContextBudget(
            estimate=ContextEstimate(system_tokens=100, message_tokens=3800,
                tool_schema_tokens=100, total_tokens=4000),
            context_window=10000, reserve_tokens=1000)))
        continue
    elif kind == "shutdown":
        if active:
            emit(RpcCommandFinished(command_id=active["id"], command_type="prompt", ok=False,
                                    error="RPC command cancelled: shutdown"))
        finish(command)
        break
    finish(command)
""",
        encoding="utf-8",
    )
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                str(command_log),
            ],
            os.environ,
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    phase = "startup"
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            commands = _complete_logged_commands(command_log)
            if (
                phase == "startup"
                and b"Type a prompt or / for commands." in output
                and b"plan" in output
            ):
                os.write(terminal_fd, b"/he")
                phase = "prefix"
                output.clear()
            elif phase == "prefix" and b"/help" in output:
                os.write(terminal_fd, b"\r")
                phase = "completed"
                output.clear()
            # Differential terminal output may contain only the newly filled suffix.
            elif phase == "completed" and b"lp" in output:
                assert not any(command["type"] == "prompt" for command in commands)
                os.write(terminal_fd, b"\r")
                phase = "help"
                output.clear()
            elif (
                phase == "help"
                and b"/build" in output
                and b"/compact" in output
                and b"Esc close" in output
            ):
                assert b"/context" in output
                assert b"/compact" in output
                os.write(terminal_fd, b"\x1b")
                # Closing a popup leaves the background's unchanged cells out of
                # differential writes. Resize to inspect the full restored frame.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 102, 0, 0))
                phase = "closed"
                output.clear()
            elif phase == "closed" and b"Type a prompt or / for commands." in output:
                os.write(terminal_fd, b"/build\r")
                phase = "build"
                output.clear()
            elif phase == "build" and b"build mode enabled." in output and b"3.0k" in output:
                # Mode changes are rejected while the post-configure stats refresh
                # is in flight. Wait for the snapshot before sending /plan.
                os.write(terminal_fd, b"/plan\r")
                phase = "plan"
                output.clear()
            elif phase == "plan" and any(
                command["type"] == "configure" and command.get("mode") == "plan"
                for command in commands
            ):
                # Resize after the request so the next frame contains the whole confirmed
                # notice, even when only "build" -> "plan" otherwise changes on screen.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 103, 0, 0))
                phase = "plan confirmed"
                output.clear()
            elif phase == "plan confirmed" and b"plan mode enabled." in output:
                os.write(terminal_fd, b"/context\r")
                phase = "context"
                output.clear()
            elif phase == "context" and b"Context" in output and b"0.0123" in output:
                os.write(terminal_fd, b"\x1b")
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 104, 0, 0))
                phase = "context closed"
                output.clear()
            elif phase == "context closed" and b"Type a prompt or / for commands." in output:
                os.write(terminal_fd, b"/context auto off\r")
                phase = "auto disabled"
                output.clear()
            elif phase == "auto disabled" and any(
                command["type"] == "configure" and command.get("auto_compaction_enabled") is False
                for command in commands
            ):
                os.write(terminal_fd, b"/compact Keep the constraints\r")
                phase = "compacting"
                output.clear()
            elif phase == "compacting" and b"compacting" in output:
                os.write(terminal_fd, b"\x03")
                phase = "cancelled"
                output.clear()
            elif (
                phase == "cancelled"
                and sum(command["type"] == "get_messages" for command in commands) >= 2
            ):
                # A full frame avoids depending on terminal diff boundaries inside "cancelled".
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 105, 0, 0))
                phase = "cancel confirmed"
                output.clear()
            elif phase == "cancel confirmed" and b"cancelled" in output:
                os.write(terminal_fd, b"/skill:review keep running\r")
                phase = "running"
                output.clear()
            elif phase == "running" and b"s:0/l:0" in output:
                stats_before_cached_view = sum(
                    command["type"] == "get_session_stats" for command in commands
                )
                os.write(terminal_fd, b"/context\r")
                phase = "cached context"
                output.clear()
            elif phase == "cached context" and b"snapshot" in output:
                assert (
                    sum(command["type"] == "get_session_stats" for command in commands)
                    == stats_before_cached_view
                )
                os.write(terminal_fd, b"\x1b")
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 106, 0, 0))
                phase = "cached closed"
                output.clear()
            elif phase == "cached closed" and b"working" in output and b"/skill:review" in output:
                assert b"EXPANDED_BODY_MUST_STAY_OUT_OF_THE_TRANSCRIPT" not in output
                os.write(terminal_fd, b"/mcp\r")
                phase = "mcp"
                output.clear()
            elif phase == "mcp" and b"MCP servers" in output:
                # The new overlay can reuse individual terminal cells from the
                # transcript. Inspect a full frame, not differential writes.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 107, 0, 0))
                phase = "mcp ready"
                output.clear()
            elif phase == "mcp ready" and b"mcp__search__query" in output and b"LIVEBG" in output:
                # LIVEBG fits in the exposed transcript margin. Both markers must
                # appear in this full frame while the popup still owns input.
                assert b"MCP" in output and b"servers" in output
                assert b"connected" in output
                os.write(terminal_fd, b"r")
                phase = "mcp refresh"
                output.clear()
            elif (
                phase == "mcp refresh"
                and sum(command["type"] == "get_mcp_status" for command in commands) == 2
            ):
                os.write(terminal_fd, b"\x03")
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 108, 0, 0))
                phase = "prompt restored"
                output.clear()
            elif (
                phase == "prompt restored"
                and b"/skill:review" in output
                and b"keep" in output
                and b"LIVEBG" in output
            ):
                # A full styled frame can arrive in several PTY reads. Wait for
                # the streamed row too, rather than asserting on an early chunk.
                assert b"EXPANDED_BODY_MUST_STAY_OUT_OF_THE_TRANSCRIPT" not in output
                os.write(terminal_fd, b"/quit\r")
                phase = "quit"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI timed out in {phase!r}; output={bytes(output)!r}")
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert phase == "quit", (phase, bytes(output))
        assert termios.tcgetattr(terminal_fd) == initial_terminal
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Release the PTY before reaping: macOS can otherwise leave a killed
            # child waiting for terminal teardown after a failed screen assertion.
            os.close(terminal_fd)
            os.waitpid(child_pid, 0)
        else:
            os.close(terminal_fd)
    commands = _complete_logged_commands(command_log)
    configure = [command for command in commands if command["type"] == "configure"]
    assert [command["mode"] for command in configure if "mode" in command] == ["build", "plan"]
    assert [
        command["auto_compaction_enabled"]
        for command in configure
        if "auto_compaction_enabled" in command
    ] == [False]
    compact = next(command for command in commands if command["type"] == "compact")
    assert compact["instructions"] == "Keep the constraints"
    prompt = next(command for command in commands if command["type"] == "prompt")
    assert prompt["prompt"] == "/skill:review keep running"
    cancel = next(command for command in commands if command["type"] == "cancel")
    assert cancel["target_id"] == compact["id"]
    assert all(command["persist_model_selection"] is False for command in configure)
    assert [
        command["type"]
        for command in commands
        if command["type"]
        in {
            "prompt",
            "steer",
            "follow_up",
            "shutdown",
        }
    ] == ["prompt", "shutdown"]


@pytest.mark.process
def test_rust_tui_skill_browser_expands_through_python(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    project = tmp_path / "project"
    skill = project / ".wisp" / "skills" / "0-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        """---
name: 0-review
description: Inspect changes carefully
---
SKILL_EXPANSION_MARKER
""",
        encoding="utf-8",
    )
    session_dir = tmp_path / "sessions"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--renderer",
                "rust",
                "--session-dir",
                str(session_dir),
            ],
            {
                **os.environ,
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
                "WISP_RUST_TUI_BINARY": str(binary),
                "WISP_TRUST": "1",
            },
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    status: int | None = None
    phase = "startup"
    context_redrawn = False
    deadline = time.monotonic() + 25
    next_browser_frame = 0.0
    browser_width = 121
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 121, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if (
                phase == "startup"
                and context_redrawn
                and b"Type a prompt or / for commands." in output
            ):
                os.write(terminal_fd, b"/skills\r")
                phase = "browser"
                output.clear()
            elif phase == "browser" and b"Skills" in output:
                phase = "browser ready"
                output.clear()
            elif (
                phase == "browser ready"
                and b"Inspect changes carefully" in output
                and b"Refreshing skills" not in output
            ):
                assert b"0-review" in output and b"project:wisp" in output
                os.write(terminal_fd, b"\r")
                phase = "inserted"
                output.clear()
            elif phase == "inserted" and b"/skill:0-review" in output:
                # Selecting only inserts the directive. The user supplies and submits
                # the request; the Python backend owns expansion and persistence.
                assert not list(session_dir.glob("*.jsonl"))
                os.write(terminal_fd, b"inspect this change\r")
                phase = "submitted"
                output.clear()
            elif (
                phase == "submitted"
                and b"SKILL_EXPANSION_MARKER" in output
                and output.rfind(b"idle") > output.rfind(b"working")
            ):
                # FakeProvider echoes its expanded input as assistant output.
                os.write(terminal_fd, b"\x03")
                phase = "quit"
            if phase == "browser ready" and time.monotonic() >= next_browser_frame:
                # Cached entries are visible before refresh settles. Require a full
                # frame without the loading line before acting on its selection.
                browser_width += 1
                fcntl.ioctl(
                    terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, browser_width, 0, 0)
                )
                next_browser_frame = time.monotonic() + 0.1
                output.clear()
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI timed out in {phase!r}; output={bytes(output)!r}")
        assert phase == "quit", (phase, bytes(output))
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert termios.tcgetattr(terminal_fd) == initial_terminal
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        os.close(terminal_fd)
    entries = [
        json.loads(line)
        for path in session_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    message = next(
        entry["message"]
        for entry in entries
        if entry.get("kind") == "message" and entry["message"]["role"] == "user"
    )
    assert message["skill_invocation"]["original_content"] == (
        "/skill:0-review inspect this change"
    )
    assert message["content"].startswith("[WISP EXPLICIT SKILL]\nSkill: 0-review")
    assert "SKILL_EXPANSION_MARKER" in message["content"]
    assert message["content"].endswith("[USER REQUEST]\ninspect this change")


@pytest.mark.process
def test_rust_file_picker_uses_python_discovery_and_submits_only_the_reference(
    tmp_path: Path,
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    relative_path = 'src/资料 "example".py'
    (project / relative_path).write_text("FILE_CONTENT_MUST_NOT_BE_INLINED", encoding="utf-8")
    protected_name = "PRIVATE_FILE_SHOULD_NOT_APPEAR.key"
    (project / protected_name).write_text("SECRET_MUST_NOT_APPEAR", encoding="utf-8")
    reference = "@" + json.dumps(relative_path, ensure_ascii=False)
    prompt = f"Explain {reference} "
    session_dir = tmp_path / "sessions"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--renderer",
                "rust",
                "--session-dir",
                str(session_dir),
            ],
            {
                **os.environ,
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
                "WISP_RUST_TUI_BINARY": str(binary),
                "WISP_TRUST": "1",
            },
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    all_output = bytearray()
    status: int | None = None
    phase = "startup"
    context_redrawn = False
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    all_output.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            # Ratatui can position over unchanged blanks and between wide glyphs.
            # Buffer tests assert exact layout; here compare the emitted nonblank
            # text, not an assumed contiguous ANSI write.
            emitted = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output)
            compact = b"".join(emitted.split())
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 121, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if (
                phase == "startup"
                and context_redrawn
                and b"Type a prompt or / for commands." in output
            ):
                os.write(terminal_fd, b"Explain @")
                phase = "all files"
                output.clear()
            elif phase == "all files" and b"".join(relative_path.encode().split()) in compact:
                # The initial unfiltered list would expose this name if discovery
                # crossed the Python protected-path boundary.
                assert protected_name.encode() not in all_output
                os.write(terminal_fd, b"example")
                phase = "filtered"
                output.clear()
            elif (
                phase == "filtered"
                and b"".join(Path(relative_path).name.encode().split()) in compact
            ):
                os.write(terminal_fd, b"\r")
                phase = "inserted"
                output.clear()
            elif phase == "inserted" and b"".join(reference[1:].encode().split()) in compact:
                assert not list(session_dir.glob("*.jsonl")), "selection must not submit"
                os.write(terminal_fd, b"\r")
                phase = "submitted"
                output.clear()
            elif phase == "submitted" and b"idle" in output:
                entries = [
                    entry
                    for path in session_dir.glob("*.jsonl")
                    for entry in _complete_logged_commands(path)
                ]
                if any(
                    entry.get("kind") == "message"
                    and isinstance(message := entry.get("message"), dict)
                    and message.get("role") == "assistant"
                    for entry in entries
                ):
                    os.write(terminal_fd, b"\x03")
                    phase = "quit"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust file picker timed out in {phase!r}; output={bytes(output)!r}")
        assert phase == "quit", (phase, bytes(output))
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert termios.tcgetattr(terminal_fd) == initial_terminal
        assert protected_name.encode() not in all_output
        assert b"SECRET_MUST_NOT_APPEAR" not in all_output
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        os.close(terminal_fd)
    messages = [
        entry["message"]
        for path in session_dir.glob("*.jsonl")
        for entry in _complete_logged_commands(path)
        if entry.get("kind") == "message"
    ]
    user_messages = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    assert len(user_messages) == 1
    assert user_messages[0]["content"] == prompt
    assert "FILE_CONTENT_MUST_NOT_BE_INLINED" not in json.dumps(messages)
    # The autouse fixture's empty parent project must remain untouched.
    assert not (Path.cwd() / ".wisp").exists()
