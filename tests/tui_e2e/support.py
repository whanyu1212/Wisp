"""Inline backend code and screen and command-log readers shared by the TUI PTY tests."""

from __future__ import annotations

import json
import os
import re
import signal
import time
import unicodedata
from pathlib import Path

# Shared by the deliberately small inline backends in the PTY tests; use real event
# models so the Rust projection is checked against Python's complete live wire contract.
CONTEXT_STATS_BACKEND = """
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


def rendered_without_spacing(output: bytes | bytearray) -> bytes:
    """Return painted text without escapes or whitespace.

    Incremental frames skip cells that already hold the same content, so a phrase
    that replaces similar text can arrive as words separated by cursor moves.
    """
    emitted = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output)
    return b"".join(emitted.split())


# The TUI wraps every frame in a synchronized update (`terminal.rs`). Output up to
# the last end marker holds only complete frames.
_FRAME_END = b"\x1b[?2026l"


def complete_frames(output: bytes | bytearray) -> bytes:
    """Return the output up to the end of the last complete frame.

    A PTY read can stop mid-frame. A frame that paints a response and still shows
    the run as ``working`` may then be seen before its ``working`` cells arrive,
    which makes a still-running TUI look idle.
    """
    end = output.rfind(_FRAME_END)
    return bytes(output[: end + len(_FRAME_END)]) if end >= 0 else b""


_CURSOR_MOVE = re.compile(rb"\x1b\[(\d+);(\d+)H")
_FRAME_START = b"\x1b[?2026h"


def frame_cursor(output: bytes | bytearray) -> tuple[int, int] | None:
    """Return where the last complete frame left the cursor, as 1-based (row, column).

    Each frame ends by placing the cursor, so a frame whose cursor sits where the
    composer's text ends proves the TUI has processed and painted every keystroke
    up to that point. Unlike matching painted text, this does not depend on which
    cells a frame happened to repaint.

    Args:
        output (bytes | bytearray): Raw PTY output so far.

    Returns:
        tuple[int, int] | None: The cursor position, or None if no frame was received
        whole (start and end marker) or the frame placed no cursor.
    """
    frames = complete_frames(output)
    start = frames.rfind(_FRAME_START)
    if start < 0:
        # The frame began before the buffer was last cleared; its cursor may
        # predate input the test has sent since.
        return None
    moves = _CURSOR_MOVE.findall(frames[start:])
    if not moves:
        return None
    row, column = moves[-1]
    return int(row), int(column)


def display_width(text: str) -> int:
    """Return the terminal cells ``text`` occupies, counting wide East Asian characters as two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def reap(child_pid: int, terminal_fd: int, *, timeout: float = 10.0) -> None:
    """Kill a PTY child's process group, close the PTY, and reap the child within a deadline.

    Closing the controlling PTY matters on macOS: a dying process can block while
    its terminal output drains, so an unbounded ``waitpid`` after SIGKILL can hang
    until the per-test timeout and hide the original failure.

    Args:
        child_pid (int): Child process from ``pty.fork``.
        terminal_fd (int): The child's controlling PTY.
        timeout (float, optional): Seconds to wait for the child to exit. Defaults to 10.

    Raises:
        AssertionError: The child did not exit within ``timeout``.
    """
    try:
        foreground_group = os.tcgetpgrp(terminal_fd)
    except OSError:
        foreground_group = None
    # Right after the fork the PTY reports 0, and killpg(0) would signal the test's
    # own process group; only signal a real group other than ours.
    if foreground_group is not None and 0 < foreground_group != os.getpgrp():
        try:
            os.killpg(foreground_group, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.close(terminal_fd)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited_pid, _status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == child_pid:
            return
        time.sleep(0.05)
    raise AssertionError(f"PTY child {child_pid} did not exit within {timeout}s of SIGKILL")


def run_settled(output: bytes | bytearray, offset: int = 0, *fragments: bytes) -> bool:
    """Return whether complete frames show the fragments and the run back at idle.

    Ctrl+C cancels while a command runs and quits only when idle, so a test must
    see a complete idle frame before it sends Ctrl+C to quit.

    Args:
        output (bytes | bytearray): Raw PTY output so far.
        offset (int, optional): Position from which the fragments must appear.
            Defaults to 0.
        *fragments (bytes): Byte strings that must appear after ``offset``.

    Returns:
        bool: True when every fragment is in a complete frame after ``offset`` and
        the last ``idle`` comes after the last ``working``.
    """
    frames = complete_frames(output)
    phase = frames[offset:]
    if not all(fragment in phase for fragment in fragments):
        return False
    return frames.rfind(b"idle") > frames.rfind(b"working")


def welcome_ready(output: bytes | bytearray) -> bool:
    """Recognize the hydrated welcome screen with or without a provider."""
    rendered = rendered_without_spacing(output)
    return b"Typeapromptor/forcommands." in rendered or b"/connecttoaddaprovider" in rendered


def complete_logged_commands(path: Path) -> list[dict[str, object]]:
    """Ignore a final record until the backend finishes writing its newline."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1]]
