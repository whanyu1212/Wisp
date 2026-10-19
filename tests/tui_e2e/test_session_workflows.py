from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.tui_e2e.support import (
    CONTEXT_STATS_BACKEND,
    reap,
    welcome_ready,
)
from wisp import __version__


@pytest.mark.process
def test_rust_tui_session_workflows_over_pty(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "session_backend.py"
    command_log = tmp_path / "session_commands.jsonl"
    backend.write_text(
        CONTEXT_STATS_BACKEND
        + """
import json
import sys
import time
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
        # Exceed the test's old 250ms settling delay so metadata readiness must
        # come from a rendered state, even on fast development machines.
        time.sleep(0.35)
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
    phase_output_offset = 0
    context_redraw_offset: int | None = None
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
            # Observe a fresh ready frame after each operation. Backend receipt
            # of get_messages does not mean the frontend has finished hydration.
            emitted = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output[phase_output_offset:])
            ready = welcome_ready(output[phase_output_offset:]) and any(
                marker in emitted for marker in (b"3.0k", b"10.0k", b"30%")
            )
            commands = []
            if command_log.exists():
                for line in command_log.read_text().splitlines():
                    try:
                        commands.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            command_types = [command["type"] for command in commands]
            if context_redraw_offset is None and b"3.0k" in output:
                context_redraw_offset = len(output)
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 111, 0, 0))
                continue
            if (
                phase == "startup"
                and context_redraw_offset is not None
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
                # A fresh welcome after the context result excludes the earlier
                # editable frame while metadata hydration was still pending.
                and welcome_ready(output[context_redraw_offset:])
            ):
                os.write(terminal_fd, b"/name client-requested\r")
                phase = "name"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "name"
                and "set_session_name" in command_types
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"/clone\r")
                phase = "clone"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "clone"
                and "clone_session" in command_types
                and command_types.count("get_messages") >= 2
                and ready
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"/tree\r")
                phase = "first tree"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "first tree"
                and command_types.count("get_session_tree") >= 1
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\r")
                phase = "navigate"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "navigate"
                and "navigate_session_tree" in command_types
                and command_types.count("get_messages") >= 3
                and ready
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\x7f" * len("nav-restored-z"))
                os.write(terminal_fd, b"/tree\r")
                phase = "second tree"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "second tree"
                and command_types.count("get_session_tree") >= 2
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"f")
                phase = "fork"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "fork"
                and "fork_session" in command_types
                and command_types.count("get_messages") >= 4
                and ready
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\x7f" * len("fork-restored-z"))
                os.write(terminal_fd, b"/unrevert\r")
                phase = "unrevert"
                phase_started = now
                phase_output_offset = len(output)
            elif (
                phase == "unrevert"
                and "unrevert_session_tree" in command_types
                and command_types.count("get_messages") >= 5
                and ready
                and now - phase_started > 0.25
            ):
                os.write(terminal_fd, b"\x03")
                phase = "shutdown"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(
                f"Rust TUI session workflow timed out in {phase!r}; output={bytes(output)!r}"
            )
    finally:
        if status is None:
            reap(child_pid, terminal_fd)

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
