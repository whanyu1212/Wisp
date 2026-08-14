"""Tool-execution contract consumed by the pure agent loop."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall

type ToolExecutionEvent = ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded


class ToolExecutionProtocolError(RuntimeError):
    """Raised when an executor emits an invalid event sequence."""


class ToolResultProcessingError(RuntimeError):
    """Raised when Wisp cannot normalize an otherwise returned tool result."""

    def __init__(self, *, call_id: str, tool_name: str) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        super().__init__("Internal error while processing a tool result")


class ToolExecutor(Protocol):
    """Execute one provider-neutral tool call as a typed event stream."""

    def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        """Yield an optional ordered approval pair, then exactly one terminal result.

        An approval request must be followed by one resolution before the result. A
        denied approval must terminate with an error result.
        """
        ...


__all__ = [
    "ToolExecutionEvent",
    "ToolExecutionProtocolError",
    "ToolExecutor",
    "ToolResultProcessingError",
]
