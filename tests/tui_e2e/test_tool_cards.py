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

from tests.tui_e2e.support import (
    CONTEXT_STATS_BACKEND,
    rendered_without_spacing,
    welcome_ready,
)
from wisp import __version__


@pytest.mark.process
def test_rust_tui_renders_bounded_tool_and_process_cards(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "tool_backend.py"
    backend.write_text(
        CONTEXT_STATS_BACKEND
        + """
import json
import sys

from wisp.events import (
    RpcCommandFinished, RpcConnectionCatalogReported, RpcConnectionCatalogSnapshot,
    RpcMessagesReported, ToolCallRequested, ToolResultReady,
)


def emit(event):
    print(event.model_dump_json(), flush=True)


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
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
    elif command_type == "get_connection_catalog":
        emit(RpcConnectionCatalogReported(
            command_id=command_id, catalog=RpcConnectionCatalogSnapshot(),
        ))
        emit(RpcCommandFinished(
            command_id=command_id, command_type=command_type, ok=True,
        ))
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
    startup_redraw_offset: int | None = None
    cards_redraw_offset: int | None = None
    process_expand_offset: int | None = None
    edit_select_offset: int | None = None
    detail_requested = False
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
            if startup_redraw_offset is None and b"3.0k" in output:
                startup_redraw_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 101, 0, 0))
                continue
            if (
                not prompt_sent
                and startup_redraw_offset is not None
                and welcome_ready(output[startup_redraw_offset:])
            ):
                os.write(terminal_fd, b"tools\r")
                prompt_sent = True
            # A slow backend can paint "Running process" before the completed card;
            # the shared blank cell then never reaches the PTY stream.
            if cards_redraw_offset is None and all(
                marker in rendered_without_spacing(output)
                for marker in (b"README.md", b"Processcompleted", b"demo.txt")
            ):
                cards_redraw_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 102, 0, 0))
                continue
            if (
                # The cards resize forces a full frame, so an idle footer appears after it.
                cards_redraw_offset is not None
                and not browse_sent
                and b"idle" in output[cards_redraw_offset:]
                and b"completed prompt did not report" in output[cards_redraw_offset:]
            ):
                # This stub has no persisted session. Its metadata warning marks
                # post-prompt hydration as settled, so it cannot invalidate keys
                # sent to inspect the live cards. Observe each changed selection.
                process_expand_offset = len(output)
                os.write(terminal_fd, b"\x1b[17~\x1b[Z\x1b[C")
                browse_sent = True
            if (
                process_expand_offset is not None
                and edit_select_offset is None
                and b"safe\xef\xbf\xbd[2J" in output[process_expand_offset:]
            ):
                edit_select_offset = len(output)
                os.write(terminal_fd, b"\t")
            if (
                edit_select_offset is not None
                and not detail_requested
                and b"demo.txt" in output[edit_select_offset:]
            ):
                os.write(terminal_fd, b"\r")
                detail_requested = True
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
