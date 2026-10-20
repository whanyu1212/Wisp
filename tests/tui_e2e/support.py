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


# A frame that shows the cursor ends by placing it: show, move, end of frame.
_FRAME_CURSOR = re.compile(rb"\x1b\[\?25h\x1b\[(\d+);(\d+)H\x1b\[\?2026l$")
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
        tuple[int, int] | None: The cursor position, or None if the last frame was
        not received whole (start and end marker) or did not show the cursor.
    """
    frames = complete_frames(output)
    start = frames.rfind(_FRAME_START)
    if start < 0:
        # The frame began before the buffer was last cleared; its cursor may
        # predate input the test has sent since.
        return None
    placed = _FRAME_CURSOR.search(frames[start:])
    if placed is None:
        # Frames that hide the cursor (loading, the animated welcome logo) contain
        # only drawing moves, which say nothing about the composer.
        return None
    return int(placed.group(1)), int(placed.group(2))


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


_CSI = re.compile(rb"\x1b\[([0-?]*)[ -/]*([@-~])")


def screen_rows(output: bytes | bytearray) -> list[str]:
    """Replay painted output onto a grid and return its rows as text.

    Incremental frames rewrite only the cells that change, so a word that replaces
    similar text, such as ``idle`` after ``working``, may never appear whole in the
    byte stream. Replaying cursor moves and text recovers what the screen shows.
    Only the sequences the TUI paints with matter: cursor position, clearing the
    screen or the rest of a line, and text; styles and modes are skipped.

    Args:
        output (bytes | bytearray): Raw PTY output.

    Returns:
        list[str]: Screen rows from the top, right-trimmed.
    """
    grid: dict[tuple[int, int], str] = {}
    row = column = 1
    position = 0
    while position < len(output):
        match = _CSI.match(output, position)
        if match is not None:
            params, final = match.group(1), match.group(2)
            if final == b"H":
                values = [int(value or 1) for value in params.split(b";")] if params else [1, 1]
                row, column = values[0], values[1] if len(values) > 1 else 1
            elif final == b"J" and params == b"2":
                grid.clear()
            elif final == b"K":
                for key in [key for key in grid if key[0] == row and key[1] >= column]:
                    del grid[key]
            position = match.end()
            continue
        if output[position] == 0x1B:
            position += 1
            continue
        end = position + 1
        while end < len(output) and (output[end] & 0xC0) == 0x80:
            end += 1
        character = bytes(output[position:end]).decode("utf-8", errors="replace")
        position = end
        if character in ("\r", "\n"):
            continue
        grid[(row, column)] = character
        column += display_width(character) or 1
    if not grid:
        return []
    rows: list[str] = []
    for current in range(1, max(key[0] for key in grid) + 1):
        cells = [(key[1], value) for key, value in grid.items() if key[0] == current]
        text = ""
        for cell_column, value in sorted(cells):
            text = text.ljust(cell_column - 1) + value
        rows.append(text.rstrip())
    return rows


# The label starts a word and is followed by its separator or the end of the row.
def screen_shows(output: bytes | bytearray, text: str) -> bool:
    """Return whether the last complete frame's screen shows ``text`` on one row.

    Typed characters read in separate wake-ups paint in separate frames, so a
    phrase may never appear whole in the byte stream even though it is on screen.

    Args:
        output (bytes | bytearray): Raw PTY output so far.
        text (str): Text that must appear within a single screen row.

    Returns:
        bool: True when a row of the replayed screen contains ``text``.
    """
    return any(text in row for row in screen_rows(complete_frames(output)))


_STATUS_LABEL = re.compile(r"(?:^|\s)(idle|working)(?=\s·|\s*$)")


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
        the first status word on the last complete frame's footer is ``idle``.
    """
    frames = complete_frames(output)
    phase = frames[offset:]
    if not all(fragment in phase for fragment in fragments):
        return False
    footer = next((row for row in reversed(screen_rows(frames)) if row.strip()), "")
    # The status label is the footer's first status word; a branch or folder name
    # later on the row must not count.
    label = _STATUS_LABEL.search(footer)
    return label is not None and label.group(1) == "idle"


def welcome_ready(output: bytes | bytearray) -> bool:
    """Recognize the hydrated welcome screen with or without a provider."""
    rendered = rendered_without_spacing(output)
    return b"Typeapromptor/forcommands." in rendered or b"/connecttoaddaprovider" in rendered


def complete_logged_commands(path: Path) -> list[dict[str, object]]:
    """Ignore a final record until the backend finishes writing its newline."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1]]
