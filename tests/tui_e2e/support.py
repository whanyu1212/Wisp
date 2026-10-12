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


def welcome_ready(output: bytes | bytearray) -> bool:
    """Recognize the hydrated welcome screen with or without a provider."""
    rendered = rendered_without_spacing(output)
    return b"Typeapromptor/forcommands." in rendered or b"/connecttoaddaprovider" in rendered


def complete_logged_commands(path: Path) -> list[dict[str, object]]:
    """Ignore a final record until the backend finishes writing its newline."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1]]
