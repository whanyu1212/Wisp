"""Typed tool-executor contracts shared by the loop and coding layer."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolExecutionEnded,
)
from wisp.providers.events import ToolCall

type ToolExecutionEvent = ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded


@dataclass(frozen=True, slots=True)
class PreparedToolExecution:
    """One approved call whose side effects have not started yet."""

    call_id: str
    name: str
    parallel_safe: bool
    runner: Callable[[], Awaitable[ToolExecutionEnded]] = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.call_id) is not str or not self.call_id:
            raise TypeError("Prepared tool call_id must be a non-empty string")
        if type(self.name) is not str or not self.name:
            raise TypeError("Prepared tool name must be a non-empty string")
        if type(self.parallel_safe) is not bool:
            raise TypeError("Prepared tool parallel_safe must be a boolean")
        if not callable(self.runner):
            raise TypeError("Prepared tool runner must be callable")

    async def run(self) -> ToolExecutionEnded:
        """Execute the prepared call and return its only terminal event."""

        return await self.runner()


type ToolPreparationEvent = ToolApprovalRequested | ToolApprovalResolved | PreparedToolExecution


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


@runtime_checkable
class PreparedToolExecutor(ToolExecutor, Protocol):
    """Optional two-phase capability for schedulers that can overlap safe calls."""

    def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        """Resolve policy and approval without starting tool side effects."""
        ...
