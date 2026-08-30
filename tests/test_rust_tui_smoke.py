from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from wisp import __version__


@pytest.mark.process
@pytest.mark.parametrize(
    ("exercise_prompt", "expected_exit"),
    [(True, 0), (False, 1)],
    ids=["prompt-stream-and-quit", "frontend-killed"],
)
def test_rust_tui_cross_language_smoke(
    tmp_path: Path,
    exercise_prompt: bool,
    expected_exit: int,
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
    deadline = time.monotonic() + 20
    prompt_sent = False
    response_seen = False
    quit_sent = False
    rust_process_group: int | None = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not prompt_sent and b"Type a prompt below to start." in output:
                if not exercise_prompt:
                    rust_process_group = os.tcgetpgrp(terminal_fd)
                    os.kill(rust_process_group, signal.SIGKILL)
                    quit_sent = True
                else:
                    os.write(terminal_fd, b"hello rust\r")
                prompt_sent = True
            if exercise_prompt and not response_seen and b"fake response" in output:
                response_seen = True
            if (
                exercise_prompt
                and response_seen
                and not quit_sent
                and output.rfind(b"idle") > output.rfind(b"running")
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
        """
import json
import sys

from wisp.events import RpcCommandFinished, RpcMessagesReported, ToolCallRequested, ToolResultReady


def emit(event):
    print(event.model_dump_json(), flush=True)


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "protocol_version": 2,
    "event_schema_version": 34,
    "min_protocol_version": 2,
    "max_protocol_version": 2,
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
    if command_type == "get_messages":
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
    detail_close_output_offset: int | None = None
    detail_closed_at: float | None = None
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
            if not prompt_sent and b"Type a prompt below to start." in output:
                os.write(terminal_fd, b"tools\r")
                prompt_sent = True
            cards_visible = all(
                marker in output
                for marker in (
                    b"README.md",
                    b"contents",
                    b"Process completed",
                    b"safe",
                    b"tail",
                    b"demo.txt",
                    b"new",
                    b"value",
                    b"F6",
                    b"browse",
                )
            )
            if (
                cards_visible
                and not browse_sent
                and output.rfind(b"idle") > output.rfind(b"running")
            ):
                # F6 enters visible-card browse mode; Enter opens retained detail.
                os.write(terminal_fd, b"\x1b[17~\r")
                browse_sent = True
            if browse_sent and not detail_seen and b"live retained detail" in output:
                detail_seen = True
                fcntl.ioctl(
                    terminal_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 30, 120, 0, 0),
                )
                # Enter closes detail too and is unambiguous after a resize on Linux;
                # a standalone Escape can remain buffered as a sequence prefix.
                detail_close_output_offset = len(output)
                os.write(terminal_fd, b"\r")
            if (
                detail_close_output_offset is not None
                and detail_closed_at is None
                and b"F6 details" in output[detail_close_output_offset:]
            ):
                detail_closed_at = time.monotonic()
            if (
                detail_closed_at is not None
                and not quit_sent
                and time.monotonic() - detail_closed_at >= 0.5
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
    assert detail_close_output_offset is not None, bytes(output)
    assert detail_closed_at is not None, bytes(output)
    assert quit_sent, bytes(output)
    assert b"README.md" in output
    assert b"Process completed" in output
    assert b"safe" in output and b"tail" in output
    assert b"safe\xef\xbf\xbd[2Jtail" in output
    assert b"safe\x1b[2Jtail" not in output
    assert b"old\xef\xbf\xbd[2J" in output
    assert b"old\x1b[2J" not in output
    assert b"live retained detail" in output
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal
