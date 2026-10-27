from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.tui_e2e.support import (
    CONTEXT_STATS_BACKEND,
    complete_logged_commands,
    reap,
    screen_shows,
    welcome_ready,
)
from wisp import __version__


@pytest.mark.process
def test_rust_tui_retains_complete_live_history_over_pty(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "history_backend.py"
    command_log = tmp_path / "history_commands.jsonl"
    backend.write_text(
        CONTEXT_STATS_BACKEND
        + """
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from wisp.events import (
    MessageStarted, MessageCompleted, RpcCommandFinished,
    RpcConnectionCatalogReported, RpcConnectionCatalogSnapshot,
    RpcMessagesReported, RpcMessageSnapshot,
)

log_path = Path(sys.argv[1])
def emit(event):
    print(event.model_dump_json(), flush=True)
def finish(command):
    emit(RpcCommandFinished(command_id=command["id"], command_type=command["type"], ok=True))
request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type":"rpc.handshake.accepted", "backend_package_version":request["frontend_version"],
    "capabilities":[],
    "limits":{"max_client_frame_bytes":67108864,"max_server_frame_bytes":67108864},
}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    with log_path.open("a") as log:
        log.write(json.dumps(command) + "\\n")
    kind = command["type"]
    if kind == "get_session_stats":
        report_stats(command)
    elif kind == "get_skills":
        report_skills(command)
    elif kind == "get_connection_catalog":
        emit(RpcConnectionCatalogReported(
            command_id=command["id"], catalog=RpcConnectionCatalogSnapshot()))
        finish(command)
    elif kind == "prompt":
        for index in range(1205):
            emit(MessageStarted(turn=index))
            emit(MessageCompleted(turn=index, content=f"durable row {index:04d}",
                message_entry_id=f"row-{index}", finish_reason="stop"))
        finish(command)
    elif kind == "get_messages":
        selected = command.get("entry_ids")
        before = command.get("before_entry_id")
        if selected:
            indices = [int(selected[0].split("-")[1])]
        elif before:
            end = int(before.split("-")[1])
            indices = list(range(max(0, end - 75), end))
        else:
            indices = []
        messages = tuple(RpcMessageSnapshot(
            entry_id=f"row-{index}", created_at=datetime.now(UTC), role="assistant",
            content=f"durable row {index:04d}", content_original_bytes=16,
        ) for index in indices)
        emit(RpcMessagesReported(command_id=command["id"], messages=messages,
            next_before_entry_id=f"row-{indices[0]}"
                if before and indices and indices[0] else None))
        finish(command)
    else:
        finish(command)
        if kind == "shutdown":
            break
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
    # Phases clear `output`; the screen is replayed from everything painted.
    painted = bytearray()
    status: int | None = None
    phase = "startup"
    resubmit_at = 0.0
    recovered = False
    next_input = 0.0
    redraw_width = 103
    deadline = time.monotonic() + 40
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    painted.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if phase == "startup" and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 101, 0, 0))
                output.clear()
                phase = "ready"
            elif phase == "ready" and welcome_ready(output):
                os.write(terminal_fd, b"long history\r")
                output.clear()
                phase = "streaming"
                resubmit_at = time.monotonic() + 1
            elif phase == "streaming":
                commands = complete_logged_commands(command_log)
                # A prompt submitted while the startup context refresh is still
                # running is refused and stays in the composer; send it again.
                if (
                    not any(item["type"] == "prompt" for item in commands)
                    and time.monotonic() >= resubmit_at
                    and screen_shows(painted, "› long history")
                ):
                    os.write(terminal_fd, b"\r")
                    resubmit_at = time.monotonic() + 1
                prompt_index = next(
                    (index for index, item in enumerate(commands) if item["type"] == "prompt"),
                    len(commands),
                )
                # The post-prompt read follows all live events through the real
                # frontend. Differential output need not repeat the final number.
                if any(item["type"] == "get_messages" for item in commands[prompt_index + 1 :]):
                    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 102, 0, 0))
                    output.clear()
                    phase = "settling"
            elif (
                # The resize above forces a full frame, so an idle footer appears after it.
                phase == "settling"
                and b"completed prompt did not report" in output
                and b"Ask Wisp anything" in output
                and b"idle" in output
            ):
                os.write(terminal_fd, b"preserve draft")
                output.clear()
                phase = "drafting"
            elif phase in {"drafting", "recovering"}:
                if phase == "drafting" and b"preserve draft" in output:
                    os.write(terminal_fd, b"\x1b[1;5H")
                    output.clear()
                    phase = "recovering"
                elif (
                    phase == "recovering"
                    and b"durable row 0000" in output
                    and b"preserve draft" in output
                ):
                    recovered = True
                    phase = "quitting"
                elif time.monotonic() >= next_input:
                    # Typed text can be split by cursor updates even in one write.
                    # Inspect complete frames in both phases, without history fetches.
                    fcntl.ioctl(
                        terminal_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, redraw_width, 0, 0),
                    )
                    redraw_width = 104 if redraw_width == 103 else 103
                    next_input = time.monotonic() + 0.2
            if phase == "quitting":
                # Terminal settings return before backend shutdown finishes.
                # Repeated Ctrl+C can then become SIGINT instead of TUI input.
                os.write(terminal_fd, b"\x03")
                phase = "waiting for exit"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        assert status is not None, f"history recovery timed out in {phase}: {bytes(output)!r}"
        assert recovered, bytes(output)
        requests = [
            item for item in complete_logged_commands(command_log) if item["type"] == "get_messages"
        ]
        exact = [item for item in requests if item.get("entry_ids")]
        older = [item for item in requests if item.get("before_entry_id")]
        assert exact == [], requests
        assert older == [], requests
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert termios.tcgetattr(terminal_fd) == initial_terminal
    finally:
        if status is None:
            reap(child_pid, terminal_fd)
        else:
            os.close(terminal_fd)
