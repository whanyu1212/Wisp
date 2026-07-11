"""Tool-execution contract consumed by the pure agent loop."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall

type ToolExecutionEvent = ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded


class ToolExecutionProtocolError(RuntimeError):
    """Raised when an executor emits an invalid event sequence."""


class ToolExecutor(Protocol):
    """Execute one provider-neutral tool call as a typed event stream."""

    def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        """Yield optional approval events and exactly one terminal result."""
        ...


__all__ = [
    "ToolExecutionEvent",
    "ToolExecutionProtocolError",
    "ToolExecutor",
]
