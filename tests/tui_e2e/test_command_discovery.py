from __future__ import annotations

import errno
import fcntl
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
    complete_logged_commands,
    frame_cursor,
    reap,
    rendered_without_spacing,
    welcome_ready,
)
from wisp import __version__


@pytest.mark.process
def test_rust_tui_command_discovery_and_modes_over_pty(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    command_log = tmp_path / "commands.jsonl"
    backend = tmp_path / "commands_backend.py"
    backend.write_text(
        CONTEXT_STATS_BACKEND
        + """
import json
import sys
import time
from pathlib import Path
from wisp.events import (
    RpcCommandDescriptor, RpcCommandFinished, RpcCommandsReported,
    RpcMessagesReported, RpcStateReported, RpcStateSnapshot,
    RpcConnectionCatalogReported, RpcConnectionCatalogSnapshot,
    CompactionStarted, CompactionCompleted, ContextEstimated, SkillInvoked,
    MessageStarted, MessageDelta, ErrorEvent,
    RpcMcpStatusReported, RpcMcpStatusSnapshot, RpcMcpServerSnapshot,
)
from wisp.runtime.builtin_commands import builtin_command_descriptors
from wisp.skills.models import SkillInvocationEvidence

log_path = Path(sys.argv[1])
mode = "plan"
active = None
auto_compaction = True
web_search = False
refresh_after_cancel = False
refresh_after_auto_configure = False
command_catalog_requests = 0

def emit(event):
    print(event.model_dump_json(), flush=True)

def finish(command):
    emit(RpcCommandFinished(command_id=command["id"], command_type=command["type"], ok=True))

request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    with log_path.open("a") as log:
        log.write(json.dumps(command) + "\\n")
    kind = command["type"]
    if kind == "get_session_stats":
        if refresh_after_cancel:
            # Expose the interval between cancellation and input readiness.
            time.sleep(0.1)
        report_stats(command, "context-session", auto_compaction)
        if refresh_after_auto_configure:
            # This diagnostic is emitted only after the refreshed snapshot and
            # command completion have reached the frontend event stream.
            emit(ErrorEvent(message="AUTOREADY"))
            refresh_after_auto_configure = False
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
        if refresh_after_cancel:
            time.sleep(0.1)
        emit(RpcMessagesReported(command_id=command["id"], session_id="context-session",
                                 session_path=Path("/context-session.jsonl"),
                                 active_leaf_id="leaf"))
        finish(command)
        if refresh_after_cancel:
            # A diagnostic painted after report + finish is a frontend-consumed
            # barrier, not merely proof that the backend received the request.
            emit(ErrorEvent(message="SYNCREADY"))
            refresh_after_cancel = False
        continue
    elif kind == "get_connection_catalog":
        emit(RpcConnectionCatalogReported(command_id=command["id"],
                                         catalog=RpcConnectionCatalogSnapshot()))
    elif kind == "get_commands":
        command_catalog_requests += 1
        emit(RpcCommandsReported(command_id=command["id"], commands=tuple(
            RpcCommandDescriptor(
                name=d.name, title=d.title, description=d.description, category=d.category,
                slash_command=d.slash_command, slash_aliases=d.slash_aliases, order=d.order,
            ) for d in builtin_command_descriptors()
        )))
        finish(command)
        if command_catalog_requests > 1:
            # Cached/fallback help rows may already be visible. Only this
            # post-completion diagnostic proves the refresh was consumed.
            emit(ErrorEvent(message="HELPCATALOGREADY"))
        continue
    elif kind == "get_state":
        emit(RpcStateReported(command_id=command["id"], state=RpcStateSnapshot(
            provider="fake", mode=mode, auto_compaction_enabled=True, web_search=web_search,
            steering_mode="one_at_a_time", follow_up_mode="one_at_a_time",
            pending_steering_count=0, pending_follow_up_count=0,
        )))
    elif kind == "configure":
        web_search = command.get("web_search", web_search)
        mode = command.get("mode", mode)
        auto_compaction = command.get("auto_compaction_enabled", auto_compaction)
        refresh_after_auto_configure = "auto_compaction_enabled" in command
    elif kind == "compact":
        active = command
        emit(CompactionStarted(session_id="context-session", source_entry_count=4))
        continue
    elif kind == "cancel":
        refresh_after_cancel = True
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
            commands = complete_logged_commands(command_log)
            if phase == "startup" and welcome_ready(output) and b"Plan" in output:
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
                and b"/queue" in output
                and b"/compact" in output
                and b"HELPCATALOGREADY"
                in b"".join(re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output).split())
            ):
                # Paging before refresh completion can be rejected as stale input.
                # The queue command adds a row; permissions is now on the next help page.
                os.write(terminal_fd, b"\x1b[6~")
                phase = "help scrolled"
            elif (
                phase == "help scrolled"
                and b"/permissions" in output
                and b"/compact" in output
                and b"Esc close" in output
            ):
                assert b"/context" in output
                assert b"/compact" in output
                # Send Escape on its own and resize only after the popup closes.
                # crossterm's input sources are edge-triggered, so a resize signal
                # ready in the same wake-up can leave a key unread until the next
                # keypress. Wait for the complete frame to return the cursor to
                # the composer; unchanged tagline cells need not be repainted.
                os.write(terminal_fd, b"\x1b")
                phase = "closing help"
                output.clear()
            elif phase == "closing help" and frame_cursor(output) == (20, 6):
                # Closing a popup leaves the background's unchanged cells out of
                # differential writes. Resize to inspect the full restored frame.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 102, 0, 0))
                phase = "closed"
                output.clear()
            elif phase == "closed" and welcome_ready(output):
                os.write(terminal_fd, b"/search on\r")
                phase = "search on"
                output.clear()
            elif phase == "search on" and b"websearchon" in rendered_without_spacing(output):
                os.write(terminal_fd, b"/search off\r")
                phase = "search off"
                output.clear()
            elif phase == "search off" and any(
                command.get("web_search") is False for command in commands
            ):
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 104, 0, 0))
                phase = "search off confirmed"
                output.clear()
            elif phase == "search off confirmed" and b"websearchoff" in rendered_without_spacing(
                output
            ):
                os.write(terminal_fd, b"/build\r")
                phase = "build"
                output.clear()
            elif phase == "build" and b"build mode enabled." in output:
                # The context total can remain unchanged and be absent from a
                # differential write. Inspect it in a fresh frame after applying.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 101, 0, 0))
                phase = "build ready"
                output.clear()
            elif phase == "build ready" and b"30%" in output and welcome_ready(output):
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
                phase = "closing context"
                output.clear()
            elif phase == "closing context" and b"staysinsync" in rendered_without_spacing(output):
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 104, 0, 0))
                phase = "context closed"
                output.clear()
            elif phase == "context closed" and welcome_ready(output):
                os.write(terminal_fd, b"/context auto off\r")
                phase = "auto disabled"
                output.clear()
            elif phase == "auto disabled" and any(
                command["type"] == "configure" and command.get("auto_compaction_enabled") is False
                for command in commands
            ):
                phase = "auto disabled ready"
            elif phase == "auto disabled ready" and b"AUTOREADY" in b"".join(
                re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output).split()
            ):
                os.write(terminal_fd, b"/compact Keep the constraints\r")
                phase = "compacting"
                output.clear()
            # The status line names compaction while it runs.
            elif phase == "compacting" and b"Compacting" in output:
                # Escape exercises the same cancellation action without relying
                # on platform PTY signal handling for the Ctrl-C byte.
                os.write(terminal_fd, b"\x1b")
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
            elif (
                phase == "cancel confirmed"
                and b"cancelled" in output
                # The marker follows both history report and completion. A welcome
                # alone can appear while post-cancellation hydration is in flight.
                and b"SYNCREADY"
                in b"".join(re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output).split())
            ):
                os.write(terminal_fd, b"/skill:review keep running\r")
                phase = "running"
                output.clear()
            # The composer names the follow-up key only while a run takes input.
            elif phase == "running" and b"follow-up" in output:
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
                phase = "closing cached context"
                output.clear()
            elif phase == "closing cached context" and b"keeprunning" in rendered_without_spacing(
                output
            ):
                # The overlay covered this transcript text; repainting it shows Escape
                # was handled. Request a full frame for the transcript checks below.
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 106, 0, 0))
                phase = "cached closed"
                output.clear()
            elif phase == "cached closed" and b"interrupt" in output and b"/skill:review" in output:
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
                phase = "closing mcp"
                output.clear()
            elif phase == "closing mcp" and b"keeprunning" in rendered_without_spacing(output):
                # The overlay covered this transcript text; repainting it shows the key
                # was handled before the resize below.
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
            # The backend log tells a dropped key (no command sent) from a slow reply.
            recent_commands = [command["type"] for command in complete_logged_commands(command_log)]
            pytest.fail(
                f"Rust TUI timed out in {phase!r}; recent commands={recent_commands[-8:]!r}; "
                f"output={bytes(output)!r}"
            )
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert phase == "quit", (phase, bytes(output))
        assert termios.tcgetattr(terminal_fd) == initial_terminal
    finally:
        if status is None:
            reap(child_pid, terminal_fd)
        else:
            os.close(terminal_fd)
    commands = complete_logged_commands(command_log)
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
