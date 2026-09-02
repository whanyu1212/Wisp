from __future__ import annotations

import errno
import fcntl
import json
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
    "protocol_version": 4,
    "event_schema_version": 36,
    "min_protocol_version": 4,
    "max_protocol_version": 4,
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
    assert b"safe" in output and b"tail" in output
    assert b"safe\xef\xbf\xbd[2Jtail" in output
    assert b"safe\x1b[2Jtail" not in output
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
    backend = tmp_path / "queue_backend.py"
    command_log = tmp_path / "commands.jsonl"
    backend.write_text(
        """
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
    "protocol_version": 4,
    "event_schema_version": 36,
    "min_protocol_version": 4,
    "max_protocol_version": 4,
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
    if command_type == "get_messages":
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
    restore_output_offset: int | None = None
    queue_update_output_offset: int | None = None
    idle_output_offset: int | None = None
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
            commands = (
                [json.loads(line) for line in command_log.read_text().splitlines()]
                if command_log.exists()
                else []
            )
            command_types = [command["type"] for command in commands]
            if (
                phase == "startup"
                and b"Type a prompt below to start." in output
                and command_types[:2] == ["get_messages", "get_queue_state"]
            ):
                os.write(terminal_fd, b"prompt-kept-running\r")
                phase = "prompt sent"
            elif phase == "prompt sent" and "prompt" in command_types:
                os.write(terminal_fd, b"steer-via-enter\r")
                phase = "steer sent"
            elif phase == "steer sent" and "steer" in command_types:
                os.write(terminal_fd, b"follow-up-via-alt-enter\x1b\r")
                phase = "follow-up sent"
            elif phase == "follow-up sent" and "follow_up" in command_types:
                restore_output_offset = len(output)
                os.write(terminal_fd, b"\x1b[1;3A")
                phase = "restore sent"
            elif (
                phase == "restore sent"
                and "pop_queue" in command_types
                and restore_output_offset is not None
                and b"later:0" in output[restore_output_offset:]
            ):
                queue_update_output_offset = output.rfind(b"later:0") + len(b"later:0")
                phase = "queue updated"
            elif (
                phase == "queue updated"
                and queue_update_output_offset is not None
                and all(
                    marker in output[queue_update_output_offset:]
                    for marker in (b"follow-up", b"via-alt-enter")
                )
            ):
                idle_output_offset = len(output)
                os.write(terminal_fd, b"\x03")
                phase = "cancel sent"
            elif (
                phase == "cancel sent"
                and "cancel" in command_types
                and idle_output_offset is not None
                and b"idle" in output[idle_output_offset:]
            ):
                os.write(terminal_fd, b"\x03")
                phase = "shutdown sent"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust TUI queue lifecycle test timed out; output={bytes(output)!r}")
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
    command_types = [command["type"] for command in commands]
    lifecycle_commands = [
        command
        for command in commands
        if command["type"] in {"prompt", "steer", "follow_up", "pop_queue", "cancel", "shutdown"}
    ]
    assert phase == "shutdown sent", bytes(output)
    assert command_types[:2] == ["get_messages", "get_queue_state"]
    assert [command["type"] for command in lifecycle_commands] == [
        "prompt",
        "steer",
        "follow_up",
        "pop_queue",
        "cancel",
        "shutdown",
    ]
    prompt, steer, follow_up, pop_queue, cancel, _shutdown = lifecycle_commands
    assert prompt["prompt"] == "prompt-kept-running"
    assert steer["content"] == "steer-via-enter"
    assert follow_up["content"] == "follow-up-via-alt-enter"
    assert pop_queue["kind"] == "follow_up"
    assert cancel["target_id"] == prompt["id"]
    assert restore_output_offset is not None
    assert queue_update_output_offset is not None
    assert b"later:0" in output[restore_output_offset:queue_update_output_offset]
    assert b"follow-up" in output[queue_update_output_offset:]
    assert b"via-alt-enter" in output[queue_update_output_offset:]
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
        """
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from wisp.events import (
    ProjectConfigApplied,
    QueueUpdated,
    RpcCommandFinished,
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
    "protocol_version": 4,
    "event_schema_version": 36,
    "min_protocol_version": 4,
    "max_protocol_version": 4,
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
    if command_type == "get_messages":
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
            commands = (
                [json.loads(line) for line in command_log.read_text().splitlines()]
                if command_log.exists()
                else []
            )
            command_types = [command["type"] for command in commands]
            now = time.monotonic()
            if (
                phase == "startup"
                and command_types[:2] == ["get_messages", "get_queue_state"]
                and b"Type a prompt below to start." in output
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
