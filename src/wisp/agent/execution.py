"""Tool-execution and request-boundary contracts consumed by the pure agent loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from wisp.agent.messages import Message
from wisp.events import (
    ContextBudget,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolExecutionEnded,
)
from wisp.providers.events import ToolCall

type ToolExecutionEvent = ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded


class ToolExecutionProtocolError(RuntimeError):
    """Raised when an executor emits an invalid event sequence."""


class RequestBoundaryUnsupportedError(RuntimeError):
    """Raised when a boundary decision has no safe provider representation.

    This is used when native structured tool history needs a continuation
    cursor that is unavailable, or when appended content is not a plain user
    message. A complete `messages` replacement remains a fresh request.
    """


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


@dataclass(frozen=True, slots=True)
class RequestBoundarySnapshot:
    """Read-only view of loop state offered to a `RequestBoundaryHook`.

    Exposes only what a caller needs to decide what happens next -- never the
    loop's live mutable state -- so a hook cannot reach back into the loop's
    internals.
    """

    turn: int
    tool_iterations: int
    had_tool_calls: bool
    can_append_user_messages: bool
    continuation_messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class RequestContextRebase:
    """Replace portable base context while retaining a live native continuation.

    ``base_messages`` must exclude the loop-owned continuation supplied in
    ``expected_continuation_messages``. The latter is an optimistic guard: it
    must exactly match the loop's immutable snapshot when the decision is
    applied, preventing a stale compaction plan from silently duplicating or
    dropping active tool state.
    """

    base_messages: Sequence[Message]
    expected_continuation_messages: Sequence[Message]


@dataclass(frozen=True, slots=True)
class RequestBoundaryDecision:
    """What the loop should do before its next provider sample, if anything.

    `messages`, when not `None`, is a complete replacement context (for
    example, after compaction). The loop discards every prior logical and
    provider-native continuation value, then sends this replacement as a
    fresh request. It may retain a valid active assistant tool call and its
    matching tool result; provider adapters serialize such fresh context in
    their native wire format.

    `context_rebase`, when not ``None``, replaces only the portable base
    context while retaining the provider cursor, pending tool results, and
    opaque continuation tail. It requires an explicitly capable provider and
    an exact continuation snapshot. It is mutually exclusive with ``messages``.

    `extra_messages` are one or more plain user messages for steering or a
    follow-up. With an active continuation-capable provider, they are sent
    once after current tool results without resetting that continuation. If a
    replacement is also supplied, they are appended to its fresh base
    instead. A cursor-less clean response may be folded into fresh portable
    history; a cursor-less structured tool history is rejected rather than
    flattened.

    `stop=True` ends the run cleanly and takes precedence over unused
    `messages` or `extra_messages`.
    """

    messages: Sequence[Message] | None = None
    context_rebase: RequestContextRebase | None = None
    extra_messages: Sequence[Message] = ()
    stop: bool = False


@dataclass(frozen=True, slots=True)
class ContextOverflowSnapshot:
    """Read-only state for one rejected provider request.

    The loop exposes the current context budget and whether user-visible stream
    content has already escaped so callers can make a safe, bounded retry
    decision without reconstructing lifecycle state from public events.
    """

    turn: int
    tool_iterations: int
    continuation_messages: tuple[Message, ...]
    has_native_continuation: bool
    context_budget: ContextBudget
    had_streamed_delta: bool
    message: str


class RequestBoundaryHook(Protocol):
    """Called once per boundary between a finished turn and the next provider sample.

    Fires after a tool round completes and after a turn completes with no
    tool calls, before the loop would otherwise continue or stop. Lets a
    caller (e.g. `AgentHarness`) inject compaction, steering, or follow-up
    without the pure loop constructing a new run or knowing what any of
    those mean.
    """

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        """Return the decision to apply before the loop's next provider sample."""
        ...


class ContextOverflowHook(Protocol):
    """Optionally recover a rejected context-overflow request in the same loop."""

    async def recover_context_overflow(
        self, *, snapshot: ContextOverflowSnapshot
    ) -> RequestBoundaryDecision | None:
        """Return a fresh/rebased retry decision, or ``None`` to decline recovery."""
        ...


__all__ = [
    "ContextOverflowHook",
    "ContextOverflowSnapshot",
    "RequestBoundaryDecision",
    "RequestContextRebase",
    "RequestBoundaryHook",
    "RequestBoundarySnapshot",
    "RequestBoundaryUnsupportedError",
    "ToolExecutionEvent",
    "ToolExecutionProtocolError",
    "ToolExecutor",
    "ToolResultProcessingError",
]
