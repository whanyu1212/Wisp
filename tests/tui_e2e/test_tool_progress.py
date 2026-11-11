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
    complete_frames,
    reap,
    screen_rows,
    screen_shows,
    welcome_ready,
)
from wisp import __version__

_BACKEND = (
    CONTEXT_STATS_BACKEND
    + """
import json
import sys
import time

from wisp.events import (
    RpcCommandFinished, RpcConnectionCatalogReported, RpcConnectionCatalogSnapshot,
    RpcMessagesReported, ToolCallRequested, ToolExecutionProgress, ToolExecutionStarted,
    ToolResultReady,
)


def emit(event):
    print(event.model_dump_json(), flush=True)


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
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
        emit(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
    elif command_type == "get_skills":
        report_skills(command)
    elif command_type == "get_messages":
        emit(RpcMessagesReported(command_id=command_id, session_id=command.get("session_id")))
        emit(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
    elif command_type == "prompt":
        arguments = {"command": "make build"}
        emit(ToolCallRequested(call_id="build-1", name="bash", arguments=arguments))
        emit(ToolExecutionStarted(call_id="build-1", name="bash", arguments=arguments))
        emit(ToolExecutionProgress(
            call_id="build-1", name="bash", sequence=1, elapsed_ms=500,
            stdout_tail="compiling LIVEPROGRESS\\n", stderr_tail="",
            stdout_bytes=23, stderr_bytes=0,
        ))
        # The card must show the progress on its own before the result arrives.
        time.sleep(2)
        emit(ToolResultReady(
            call_id="build-1", name="bash", output="Command exited with code 0: build finished",
            is_error=False, exit_code=0, output_has_exit_status=True,
        ))
        emit(RpcCommandFinished(command_id=command_id, command_type="prompt", ok=True))
    elif command_type == "shutdown":
        emit(RpcCommandFinished(command_id=command_id, command_type="shutdown", ok=True))
        break
    else:
        emit(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
"""
)


@pytest.mark.process
def test_running_bash_card_shows_progress_until_its_result_arrives(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "progress_backend.py"
    backend.write_text(_BACKEND, encoding="utf-8")

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
    output = bytearray()
    status: int | None = None
    prompt_sent = False
    last_submit = 0.0
    progress_seen = False
    result_seen = False
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
            if not prompt_sent and welcome_ready(output):
                os.write(terminal_fd, b"build\r")
                prompt_sent = True
                last_submit = time.monotonic()
            # A session-stats read still in flight refuses the prompt with this notice
            # and keeps it in the composer, so submit it again until the run starts.
            if (
                prompt_sent
                and not progress_seen
                and time.monotonic() - last_submit > 0.5
                and screen_shows(output, "metadata refresh to finish")
            ):
                os.write(terminal_fd, b"\r")
                last_submit = time.monotonic()
            if prompt_sent and not progress_seen and screen_shows(output, "LIVEPROGRESS"):
                progress_seen = True
            # The completed card collapses to its finished action row; the live line
            # beside it must be gone.
            if progress_seen and not result_seen and screen_shows(output, "• Ran"):
                result_seen = True
                assert not screen_shows(output, "LIVEPROGRESS"), bytes(output)
                os.write(terminal_fd, b"\x03")
                quit_sent = True
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            screen = "\n".join(screen_rows(complete_frames(output)))
            pytest.fail(
                f"Rust TUI progress test timed out (prompt_sent={prompt_sent}, "
                f"progress_seen={progress_seen}, result_seen={result_seen}); screen:\n{screen}"
            )
    finally:
        if status is None:
            reap(child_pid, terminal_fd)

    assert progress_seen, bytes(output)
    assert result_seen, bytes(output)
    assert quit_sent, bytes(output)
