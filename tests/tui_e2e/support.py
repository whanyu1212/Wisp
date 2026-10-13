"""Inline backend code and screen and command-log readers shared by the TUI PTY tests."""

from __future__ import annotations

import json
import re
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
