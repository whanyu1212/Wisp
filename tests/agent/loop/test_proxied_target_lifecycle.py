"""A proxy call's `target` stays the same on every event, cancellation included."""

from __future__ import annotations

import pytest

from wisp.agent.loop.tool_lifecycle import ToolBatchSettlement
from wisp.agent.tool_contracts import ToolExecutionProtocolError
from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall

_CALL = ToolCall(call_id="c", name="mcp_call", arguments={"name": "mcp__db__drop", "arguments": {}})


def test_cancelled_approval_keeps_target() -> None:
    settlement = ToolBatchSettlement((_CALL,))
    settlement.request(_CALL)
    lifecycle = settlement.begin_lifecycle(_CALL, target="mcp__db__drop")
    lifecycle.accept(
        ToolApprovalRequested(
            call_id="c",
            name="mcp_call",
            target="mcp__db__drop",
            arguments=dict(_CALL.arguments),
            safety="command",
        )
    )

    events = list(settlement.settle_unfinished())

    resolved = [event for event in events if isinstance(event, ToolApprovalResolved)]
    ended = [event for event in events if isinstance(event, ToolExecutionEnded)]
    assert [event.target for event in (*resolved, *ended)] == ["mcp__db__drop", "mcp__db__drop"]
    assert resolved[0].approved is False


def test_lifecycle_rejects_changed_target() -> None:
    lifecycle = ToolBatchSettlement((_CALL,)).begin_lifecycle(_CALL, target="mcp__db__drop")

    with pytest.raises(ToolExecutionProtocolError, match="announced with"):
        lifecycle.accept(
            ToolExecutionEnded(
                call_id="c", name="mcp_call", target="mcp__db__other", output="", is_error=False
            )
        )
