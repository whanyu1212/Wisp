"""Tool-execution and request-boundary contracts consumed by the pure agent loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from wisp.agent.messages import Message
from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded
from wisp.providers.events import ToolCall

type ToolExecutionEvent = ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded


class ToolExecutionProtocolError(RuntimeError):
    """Raised when an executor emits an invalid event sequence."""


class RequestBoundaryUnsupportedError(RuntimeError):
    """Raised when a `RequestBoundaryHook` returns a decision the loop cannot apply.

    Currently: `messages`/`extra_messages` immediately after a tool round;
    any decision other than `stop=True` (including a plain, unmodified
    continuation) at a later no-tool-calls boundary once the run has had a
    tool round earlier; or `messages`/`extra_messages` containing a
    tool-shaped message (an assistant message with `tool_calls`, or a
    `role="tool"` message) at any boundary. See `RequestBoundaryDecision`.
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
    continuation_messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class RequestBoundaryDecision:
    """What the loop should do before its next provider sample, if anything.

    `messages`, when not `None`, replaces the loop's base message history
    (e.g. after compaction) -- everything accumulated so far is discarded in
    favor of this new base. `extra_messages` are appended after the base/
    replacement history and before the next provider request (e.g. steering
    or follow-up injection) -- everything accumulated so far is kept.
    `stop`, when `True`, ends the run at this boundary through the loop's
    normal clean-completion path.

    A non-empty `messages`/`extra_messages` resets the provider's native
    continuation state (`previous_response_id`, pending tool results) for the
    next request: every provider only ever appends new content on top of
    what it already remembers, so there is no cross-provider-safe way to
    splice caller-supplied content into an active continuation chain -- the
    next request is rebuilt as a fresh, self-contained turn instead. A hook
    that wants a plain, unmodified continuation should return an empty
    decision (`RequestBoundaryDecision()`) rather than repeat what the loop
    already has.

    `messages`/`extra_messages` are only supported while this run has never
    had a tool round. Immediately after one, rebuilding the continuation
    would mean replaying accumulated assistant tool-call/tool-result
    messages through each provider's plain-message converter, which
    flattens them to ordinary text instead of the structured pairs a
    provider expects -- corrupting history rather than fixing it. At a
    *later* no-tool-calls boundary that followed a tool round earlier in the
    run, not even a plain, unmodified continuation (an empty decision) is
    possible: the provider-native replay that would carry the tool round
    forward is only loaded when this boundary's `tool_results` is non-empty,
    which it never is here, so continuing at all -- injected content or
    not -- would silently drop the tool round from what the provider sees.
    `run_agent_loop` raises `RequestBoundaryUnsupportedError` for any
    decision other than `stop=True` once a no-tool-calls boundary has tool
    history behind it, and for `messages`/`extra_messages` immediately after
    a tool round; `stop` is always honored regardless of what else a
    decision carries.

    Independent of any of that, `messages`/`extra_messages` must never
    themselves contain a tool-shaped message (an assistant message with
    `tool_calls`, or a `role="tool"` message) at *any* boundary, even one
    with no loop-generated tool history at all -- the same plain-message-
    converter flattening applies regardless of where the tool-shaped
    content came from.
    """

    messages: Sequence[Message] | None = None
    extra_messages: Sequence[Message] = ()
    stop: bool = False


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


__all__ = [
    "RequestBoundaryDecision",
    "RequestBoundaryHook",
    "RequestBoundarySnapshot",
    "RequestBoundaryUnsupportedError",
    "ToolExecutionEvent",
    "ToolExecutionProtocolError",
    "ToolExecutor",
    "ToolResultProcessingError",
]
