"""Smoke-test the Rust TUI through an installed Wisp environment."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import re
import resource
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

_HISTORY_READY = b"RC2 saved history ready"
_ESCAPE_SEQUENCE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
# The context snapshot replaces the footer's "ctx ~?" with a reading: a meter and
# percent such as "ctx ▰▰▰▰▱▱▱▱▱▱ ~42%" or "ctx ~811%" when the window is known,
# otherwise a token count such as "ctx ~2.6k". Frames repaint only changed cells, so
# "ctx ~" may never be resent next to the reading; match the newly painted percent or
# abbreviated count instead. Counts below 1k are not matched: a bare number is too
# ambiguous, and the system prompt alone exceeds 1k tokens.
_CONTEXT_READING = re.compile(rb"[0-9]%|[0-9](?:\.[0-9])?[km](?![A-Za-z])")


def _full_repaint_after(output: bytearray, offset: int) -> bytes | None:
    """Return a complete screen-clearing frame received after a resize request."""
    start = output.find(b"\x1b[?2026h\x1b[2J", offset)
    end = output.find(b"\x1b[?2026l", start) if start >= 0 else -1
    return bytes(output[start:end]) if end >= 0 else None


def _seed_history(session_dir: Path, count: int) -> int:
    """Write a disposable active path with conversation and completed read tools.

    Args:
        session_dir: Disposable session directory supplied by the caller.
        count: Number of fixture messages, in complete four-message turns.

    Returns:
        Fixture file size in bytes, excluding later smoke-test messages.
    """
    from wisp.events import ToolCallSnapshot
    from wisp.messages import Message
    from wisp.sessions import JsonlSessionStore, MessageSessionEntry
    from wisp.sessions.entries import session_entry_to_json

    session_dir.mkdir(parents=True, exist_ok=True)
    session = JsonlSessionStore(session_dir).create()
    parent_id = None
    with session.path.open("x", encoding="utf-8") as stream:
        for index in range(count + 1):
            turn = index // 4
            messages = (
                Message(role="user", content=f"Inspect module {turn}. " + "source context " * 16),
                Message(
                    role="assistant",
                    content="Inspecting the source.",
                    tool_calls=(
                        ToolCallSnapshot(
                            call_id=f"read-{turn}",
                            name="read",
                            arguments={"path": f"module-{turn}.py"},
                        ),
                    ),
                ),
                Message(
                    role="tool",
                    content="def example(): return 42\n" * 20,
                    tool_call_id=f"read-{turn}",
                    tool_name="read",
                ),
                Message(
                    role="assistant", content=f"Module {turn} inspected. " + "Result details. " * 16
                ),
            )
            message = (
                Message(role="assistant", content=_HISTORY_READY.decode())
                if index == count
                else messages[index % 4]
            )
            entry = MessageSessionEntry(
                session_id=session.session_id, parent_id=parent_id, message=message
            )
            stream.write(session_entry_to_json(entry) + "\n")
            parent_id = entry.id
    return session.path.stat().st_size


def run(wisp: Path, session_dir: Path, history_messages: int = 0) -> dict[str, float | int]:
    """Submit one fake-provider prompt through the installed Rust TUI.

    Args:
        wisp: Installed Wisp console script.
        session_dir: Disposable session directory.
        history_messages: Optional saved-history fixture size, divisible by four.

    Returns:
        Ready-frame, total runtime, and child-process peak RSS measurements.

    Raises:
        RuntimeError: If startup, response, exit, or terminal restoration fails.
    """

    if history_messages < 0 or history_messages % 4:
        raise ValueError("history_messages must be nonnegative and divisible by four")
    history_bytes = _seed_history(session_dir, history_messages) if history_messages else 0
    started = time.monotonic()
    ready_seconds: float | None = None
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        environment = {
            **os.environ,
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_TRUST": "1",
            "WISP_TUI_MOUSE": "0",
            "WISP_AUTO_COMPACTION": "0",
        }
        environment.pop("WISP_RUST_TUI_BINARY", None)
        os.execve(
            str(wisp),
            [
                str(wisp),
                "tui",
                "--session-dir",
                str(session_dir),
                *(["--continue"] if history_messages else []),
            ],
            environment,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 81, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    startup_redraw_offset: int | None = None
    prompt_sent = False
    response_seen = False
    settled_redraw_offset: int | None = None
    settled_at: float | None = None
    settled_width = 82
    quit_sent = False
    status: int | None = None
    deadline = time.monotonic() + (120 if history_messages else 30)
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if startup_redraw_offset is None and _CONTEXT_READING.search(
                _ESCAPE_SEQUENCE.sub(b"", output[-65536:])
            ):
                # The empty-transcript hint can appear before startup metadata
                # hydration finishes. Request a complete frame after the context
                # snapshot lands, then submit only from that settled frame.
                startup_redraw_offset = len(output)
                fcntl.ioctl(
                    terminal_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 82, 0, 0),
                )
                continue
            if (
                not prompt_sent
                and startup_redraw_offset is not None
                and (_HISTORY_READY if history_messages else b"Type a prompt or / for commands.")
                in output[startup_redraw_offset:]
            ):
                ready_seconds = time.monotonic() - started
                os.write(terminal_fd, b"installed wheel smoke\r")
                prompt_sent = True
            if prompt_sent and b"response" in output:
                response_seen = True
            if response_seen and not quit_sent:
                # Incremental output may repaint only "dle" when the existing
                # cell already contains "i". Never wait for a fresh literal
                # "idle" before requesting the frame that proves readiness.
                frame = (
                    _full_repaint_after(output, settled_redraw_offset)
                    if settled_redraw_offset is not None
                    else None
                )
                redraw_due = settled_at is None or time.monotonic() - settled_at >= 0.25
                if (
                    frame is not None
                    and b"fake response to: installed wheel smoke" in frame
                    and b"idle" in frame
                    and redraw_due
                ):
                    os.write(terminal_fd, b"\x03")
                    quit_sent = True
                elif redraw_due:
                    # The first repaint can precede the final response/cleanup.
                    # Keep requesting complete frames until the run settles.
                    settled_redraw_offset = len(output)
                    settled_at = time.monotonic()
                    settled_width = 83 if settled_width == 82 else 82
                    fcntl.ioctl(
                        terminal_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, settled_width, 0, 0),
                    )
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            raise RuntimeError(
                "installed Rust TUI smoke timed out "
                f"(prompt_sent={prompt_sent}, response_seen={response_seen}, "
                f"quit_sent={quit_sent}): {bytes(output[-8000:])!r}"
            )
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code != 0 or not response_seen or not quit_sent:
            raise RuntimeError(
                f"installed Rust TUI smoke failed with {exit_code}: {bytes(output[-8000:])!r}"
            )
        if termios.tcgetattr(terminal_fd) != initial_terminal:
            raise RuntimeError("installed Rust TUI did not restore terminal attributes")
        if ready_seconds is None:
            raise RuntimeError("installed Rust TUI never reached a ready frame")
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        rss_bytes = int(rss if sys.platform == "darwin" else rss * 1024)
        return {
            "history_messages": history_messages + 1 if history_messages else 0,
            "history_bytes": history_bytes,
            "binary_bytes": (wisp.parent / "wisp-tui").stat().st_size,
            "ready_frame_seconds": ready_seconds,
            "total_seconds": time.monotonic() - started,
            "max_rss_bytes": rss_bytes,
        }
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


def main() -> None:
    """Run the installed-wheel smoke from the command line."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--wisp", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--history-messages", type=int, default=0)
    arguments = parser.parse_args()
    print(
        json.dumps(
            run(arguments.wisp, arguments.session_dir, arguments.history_messages), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
