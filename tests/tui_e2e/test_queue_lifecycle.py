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
    complete_logged_commands,
    frame_cursor,
    reap,
    screen_rows,
    screen_shows,
    welcome_ready,
)
from wisp import __version__


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
        CONTEXT_STATS_BACKEND
        + """
import json
import sys
from pathlib import Path

from wisp.events import QueueItemsRemoved, QueueUpdated, RpcCommandFinished, RpcMessagesReported


command_log = Path(sys.argv[1])
active_prompt_id = None
steering_content = "steer-via-alt-enter"
follow_up_content = "follow-up-via-enter"
queue_revision = 0
queue_token = None


def emit(event):
    global queue_revision, queue_token
    if isinstance(event, QueueUpdated):
        queue_revision += 1
        queue_token = f"queue-{queue_revision}"
        event = event.model_copy(update={"token": queue_token})
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
        assert command["expected_token"] == queue_token
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
    composer_start: tuple[int, int] | None = None
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
            commands = complete_logged_commands(command_log)
            command_types = [command["type"] for command in commands]
            if (
                phase == "startup"
                and welcome_ready(output)
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
                # The composer names the follow-up key once the run takes input.
                and b"follow-up" in output[phase_output_offset:]
                and (cursor := frame_cursor(output)) is not None
            ):
                # Typed text can be painted across several frames with other cells in
                # between, so wait for the cursor to reach the end of the text before
                # sending Alt+Enter.
                composer_start = cursor
                phase_output_offset = len(output)
                os.write(terminal_fd, b"steer-via-alt-enter")
                phase = "steer typed"
            elif (
                phase == "steer typed"
                # The status line can appear after the cursor was read and move the
                # composer up a row, so only the column marks the end of the text.
                and (cursor := frame_cursor(output)) is not None
                and composer_start is not None
                and cursor[1] == composer_start[1] + len("steer-via-alt-enter")
                and screen_shows(output, "steer-via-alt-enter")
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\x1b\r")
                phase = "steer sent"
            elif phase == "steer sent" and "steer" in command_types:
                # The queue count can reuse cells from an earlier frame. Request a
                # complete frame before checking text in differential PTY output.
                phase_output_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 102, 0, 0))
                phase = "steer frame"
            elif phase == "steer frame" and b"1 queued" in output[phase_output_offset:]:
                # The status line counts the queued steer; Enter queues a follow-up.
                phase_output_offset = len(output)
                os.write(terminal_fd, b"follow-up-via-enter\r")
                phase = "follow-up sent"
            elif phase == "follow-up sent" and "follow_up" in command_types:
                # Logging the command precedes its QueueUpdated response. Wait
                # until the TUI has applied and painted both queued messages
                # before asking it to restore the newest one.
                phase_output_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 104, 0, 0))
                phase = "follow-up frame"
            elif phase == "follow-up frame" and screen_shows(output, "2 queued"):
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
                # The steer-frame resize painted the running footer, so idle is redrawn.
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
                # The query can paint before the filtered selection is marked as
                # rendered. Force a complete frame and wait for both before Enter.
                phase_output_offset = len(output)
                fcntl.ioctl(
                    terminal_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 103, 0, 0),
                )
                phase = "history filtered frame"
            elif phase == "history filtered frame" and all(
                marker in output[phase_output_offset:]
                for marker in (
                    b"Prompt history",
                    b"> needle",
                    b"history exact first needle second",
                    b"1 matches",
                )
            ):
                phase_output_offset = len(output)
                os.write(terminal_fd, b"\r")
                phase = "history restored"
            elif (
                phase == "history restored"
                and screen_shows(output, "› history exact first")
                and not screen_shows(output, "Enter restore")
            ):
                # Differential writes skip cells the popup already held, so read the
                # restored draft from the replayed screen. The transcript and the
                # popup's selected entry show the same words before Enter, so wait for
                # the popup to close with the draft in the composer.
                # The footer remains visible under history, so closing it need not
                # repaint the input hint. Wait until restore has consumed Enter before
                # resizing, which invalidates the popup's activation readiness.
                phase_output_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 101, 0, 0))
                phase = "history restored frame"
            elif phase == "history restored frame" and all(
                marker in output[phase_output_offset:]
                # The footer draws "Ctrl+G" as a keycap, so its action follows a style change.
                for marker in (b"history", b"exact", b"first", b"needle", b"second", b" help")
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
                os.write(terminal_fd, b"\x03")
                phase = "second cancel sent"
            elif phase == "second cancel sent" and command_types.count("cancel") == 2:
                # The cancel can finish before the running footer is painted for this run, and
                # an unchanged idle footer is never redrawn. A resize forces a full frame.
                phase_output_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 104, 0, 0))
                phase = "second cancel frame"
            elif phase == "second cancel frame" and b"idle" in output[phase_output_offset:]:
                os.write(terminal_fd, b"\x03")
                phase = "shutdown sent"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            # A tail of differential frames rarely shows the composer, so replay the
            # screen and report the cursor the typing phases compare against.
            screen = "\n".join(screen_rows(complete_frames(output)))
            pytest.fail(
                f"Rust TUI queue lifecycle timed out in phase {phase!r}; "
                f"composer start={composer_start!r}; cursor={frame_cursor(output)!r}; "
                f"recent commands={commands[-8:]!r}; "
                f"screen=\n{screen}\n"
                f"terminal tail={bytes(output[-6000:])!r}"
            )
    finally:
        if status is None:
            reap(child_pid, terminal_fd)

    commands = complete_logged_commands(command_log)
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
    assert steer["content"] == "steer-via-alt-enter"
    assert follow_up["content"] == "follow-up-via-enter"
    assert pop_queue["kind"] == "follow_up"
    assert cancel["target_id"] == prompt["id"]
    assert restore_output_offset is not None
    assert queue_update_output_offset is not None
    assert b"follow-up" in output
    assert b"via-alt-enter" in output
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
    assert termios.tcgetattr(terminal_fd) == initial_terminal
