"""Continuation state and request-boundary transitions for the agent loop."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wisp.agent.execution import (
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
)
from wisp.agent.messages import Message
from wisp.events import ContextBudget, ToolResultReady
from wisp.providers.base import (
    Provider,
    ToolCallResult,
    structured_tool_replacement_support,
)

if TYPE_CHECKING:
    from wisp.agent.loop import AgentLoopConfig


@dataclass(slots=True)
class ContinuationState:
    """Provider cursor, pending request data, and the live continuation transcript."""

    pending_tool_results: tuple[ToolCallResult, ...] = ()
    pending_extra_messages: tuple[Message, ...] = ()
    previous_response_id: str | None = None
    continuation_messages: list[Message] = field(default_factory=list)

    def record_response(self, message: Message, *, response_id: str | None) -> None:
        # Public response IDs remain upstream-observed values. Stateless
        # adapters may safely retain their existing local replay key when a
        # later clean response has no new upstream ID, so do not erase a
        # usable cursor in that case.
        if response_id is not None:
            self.previous_response_id = response_id
        self.pending_extra_messages = ()
        self.continuation_messages.append(message)

    def record_tool_result(self, result_event: ToolResultReady) -> None:
        self.continuation_messages.append(
            Message(
                role="tool",
                content=result_event.output,
                tool_call_id=result_event.call_id,
                tool_name=result_event.name,
                is_error=result_event.is_error,
            )
        )

    def complete_tool_round(self, results: Sequence[ToolCallResult]) -> None:
        self.pending_tool_results = tuple(results)

    def consume_pending_tool_results(self) -> None:
        """Clear tool results once the request carrying them has been sent.

        Without this, a completed round's results stay in
        `pending_tool_results` and leak into a later, unrelated request --
        e.g. once the loop continues past a turn that had no tool calls of
        its own. `previous_response_id` is left untouched: it still points
        at the just-completed turn, and every provider natively continues
        from it with an empty `tool_results` -- no `messages` rebuild needed.
        """

        self.pending_tool_results = ()

    def queue_extra_messages(self, messages: Sequence[Message]) -> None:
        """Queue user messages for exactly the next continued request."""

        queued = tuple(messages)
        self.continuation_messages.extend(queued)
        self.pending_extra_messages = queued

    def clear_native_continuation(self) -> None:
        """Discard the provider cursor and data not yet consumed by a request."""

        self.previous_response_id = None
        self.pending_tool_results = ()
        self.pending_extra_messages = ()

    def replace_context(self) -> None:
        """Atomically discard all state made obsolete by a base replacement."""

        self.clear_native_continuation()
        self.continuation_messages.clear()

    def fold_clean(self, messages: Sequence[Message]) -> Sequence[Message]:
        """Fold the just-finished turn's own answer into `messages` and reset.

        Only called when nothing in `continuation_messages` is tool-shaped
        (see `at_request_boundary`) -- folding a plain completed-turn answer and
        resetting `previous_response_id` is always safe there, since every
        provider's plain-message converter round-trips ordinary text messages
        correctly.
        """

        folded = (*messages, *self.continuation_messages)
        self.continuation_messages.clear()
        self.clear_native_continuation()
        return folded

    def snapshot(self) -> tuple[Message, ...]:
        """Return a deep, immutable-facing view of live continuation state."""

        # `Message`/`ToolCallSnapshot` are frozen, but a `ToolCallSnapshot`'s
        # arguments contain a mutable dict. Never expose the loop's live state.
        return tuple(message.model_copy(deep=True) for message in self.continuation_messages)


def is_tool_shaped(message: Message) -> bool:
    """Return whether a row belongs to a structured assistant/tool exchange."""

    return bool(message.tool_calls) or message.role == "tool"


def has_valid_replacement_tool_order(messages: Sequence[Message]) -> bool:
    """Require each native tool result in a fresh replacement to be paired.

    A replacement may retain the active exchange that compaction cannot
    safely summarize. It must still be self-contained: accepting an orphaned
    tool row would make adapter-specific error handling decide whether raw
    tool output is trusted context.
    """

    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            return False
        if message.role != "assistant" or not message.tool_calls:
            index += 1
            continue

        expected_names = {tool_call.call_id: tool_call.name for tool_call in message.tool_calls}
        if len(expected_names) != len(message.tool_calls):
            return False
        index += 1
        while expected_names:
            if index >= len(messages):
                return False
            tool_result = messages[index]
            if tool_result.role != "tool" or tool_result.tool_call_id is None:
                return False
            expected_name = expected_names.pop(tool_result.tool_call_id, None)
            if expected_name is None or tool_result.tool_name != expected_name:
                return False
            index += 1
    return True


def validate_replacement_messages(
    config: AgentLoopConfig, messages: Sequence[Message]
) -> tuple[Message, ...]:
    """Validate a caller-owned portable base before a fresh/rebased request."""

    replacement = tuple(messages)
    if not has_valid_replacement_tool_order(replacement):
        raise RequestBoundaryUnsupportedError(
            "RequestBoundaryDecision.messages contains an unpaired structured tool exchange"
        )
    if any(is_tool_shaped(message) for message in replacement) and (
        structured_tool_replacement_support(config.provider, effort=config.effort) is False
    ):
        raise RequestBoundaryUnsupportedError(
            "The provider cannot fresh-replay a structured tool exchange for this effort"
        )
    return replacement


def provider_supports_continuation_messages(provider: Provider) -> bool:
    return getattr(provider, "supports_continuation_messages", False) is True


def provider_supports_context_rebase(provider: Provider) -> bool:
    return getattr(provider, "supports_context_rebase", False) is True


def apply_request_boundary_decision(
    config: AgentLoopConfig,
    state: ContinuationState,
    *,
    messages: Sequence[Message],
    had_tool_calls: bool,
    decision: RequestBoundaryDecision,
    allow_extra_messages: bool,
) -> tuple[Sequence[Message], bool]:
    """Validate and atomically apply one caller-supplied loop transition."""

    # No provider request follows a stop, so unused content must not make a
    # completed turn fail or mutate its logical continuation.
    if decision.stop:
        return messages, True
    if decision.messages is not None and decision.context_rebase is not None:
        raise RequestBoundaryUnsupportedError(
            "RequestBoundaryDecision.messages and context_rebase are mutually exclusive"
        )

    extra_messages = tuple(decision.extra_messages)
    if not allow_extra_messages and extra_messages:
        raise RequestBoundaryUnsupportedError(
            "Context-overflow recovery cannot append extra messages"
        )
    if any(message.role != "user" or is_tool_shaped(message) for message in extra_messages):
        raise RequestBoundaryUnsupportedError(
            "RequestBoundaryDecision.extra_messages must contain only plain user messages"
        )

    if decision.messages is not None:
        replacement = validate_replacement_messages(config, decision.messages)
        # A replacement is caller-owned, self-contained context. It may retain
        # the active structured tool pair; each adapter is responsible for
        # encoding that fresh context natively. Extras become part of the fresh
        # base, so this transition never depends on an optional capability.
        state.replace_context()
        return (*replacement, *extra_messages), False

    rebase = decision.context_rebase
    if rebase is not None:
        if not provider_supports_context_rebase(config.provider):
            raise RequestBoundaryUnsupportedError(
                "The provider cannot rebase portable context beneath its continuation"
            )
        if state.previous_response_id is None:
            raise RequestBoundaryUnsupportedError(
                "Cannot rebase context without a usable provider continuation"
            )
        expected = tuple(rebase.expected_continuation_messages)
        if expected != tuple(state.continuation_messages):
            raise RequestBoundaryUnsupportedError(
                "RequestContextRebase expected continuation does not match live state"
            )
        replacement = validate_replacement_messages(config, rebase.base_messages)
        # Do not call `replace_context`: rebase deliberately keeps the live
        # provider cursor and opaque replay tail. Tool results remain pending
        # only at the immediate post-tool boundary; a clean response has
        # already consumed them.
        if not had_tool_calls:
            state.consume_pending_tool_results()
        if extra_messages:
            state.queue_extra_messages(extra_messages)
        return replacement, False

    has_tool_history = had_tool_calls or any(
        is_tool_shaped(message) for message in state.continuation_messages
    )
    supports_continuation_messages = provider_supports_continuation_messages(config.provider)
    if not had_tool_calls:
        # The preceding provider request already consumed these outputs. Clear
        # them only when another request will actually be made.
        state.consume_pending_tool_results()

    if extra_messages:
        if supports_continuation_messages and state.previous_response_id is not None:
            state.queue_extra_messages(extra_messages)
            return messages, False
        if has_tool_history:
            raise RequestBoundaryUnsupportedError(
                "Cannot append messages without a usable provider continuation after a tool round"
            )
        # A cursor-less clean response has portable assistant text only. Fold
        # that history and make this a fresh request rather than inventing a
        # provider response ID.
        return (*state.fold_clean(messages), *extra_messages), False

    # The current tool results themselves make the immediate post-tool
    # request a valid continuation for every legacy adapter. A cursor becomes
    # necessary only after a later clean response has consumed those results.
    if had_tool_calls:
        return messages, False
    if supports_continuation_messages and state.previous_response_id is not None:
        return messages, False
    if has_tool_history:
        raise RequestBoundaryUnsupportedError(
            "Cannot continue after a tool round without a usable provider continuation"
        )
    return state.fold_clean(messages), False


async def at_request_boundary(
    config: AgentLoopConfig,
    state: ContinuationState,
    *,
    turn: int,
    tool_iterations: int,
    messages: Sequence[Message],
    had_tool_calls: bool,
    stop_by_default: bool,
) -> tuple[Sequence[Message], bool]:
    """Apply a typed transition between a completed turn and the next request."""

    if config.request_boundary_hook is None:
        return messages, stop_by_default
    snapshot = RequestBoundarySnapshot(
        turn=turn,
        tool_iterations=tool_iterations,
        had_tool_calls=had_tool_calls,
        can_append_user_messages=(
            provider_supports_continuation_messages(config.provider)
            and state.previous_response_id is not None
        ),
        continuation_messages=state.snapshot(),
    )
    decision = await config.request_boundary_hook.before_next_request(snapshot=snapshot)
    return apply_request_boundary_decision(
        config,
        state,
        messages=messages,
        had_tool_calls=had_tool_calls,
        decision=decision,
        allow_extra_messages=True,
    )


async def at_context_overflow(
    config: AgentLoopConfig,
    state: ContinuationState,
    *,
    turn: int,
    tool_iterations: int,
    messages: Sequence[Message],
    context_budget: ContextBudget,
    had_streamed_delta: bool,
    message: str,
) -> tuple[Sequence[Message], bool]:
    """Ask the optional hook whether this rejected request can retry safely."""

    if config.context_overflow_hook is None:
        return messages, False
    snapshot = ContextOverflowSnapshot(
        turn=turn,
        tool_iterations=tool_iterations,
        continuation_messages=state.snapshot(),
        has_native_continuation=state.previous_response_id is not None,
        context_budget=context_budget,
        had_streamed_delta=had_streamed_delta,
        message=message,
    )
    decision = await config.context_overflow_hook.recover_context_overflow(snapshot=snapshot)
    if decision is None or decision.stop:
        return messages, False
    if decision.messages is None and decision.context_rebase is None:
        raise RequestBoundaryUnsupportedError(
            "Context-overflow recovery must provide a fresh replacement or context rebase"
        )
    rebased_messages, stop = apply_request_boundary_decision(
        config,
        state,
        messages=messages,
        had_tool_calls=any(is_tool_shaped(item) for item in state.continuation_messages),
        decision=decision,
        allow_extra_messages=False,
    )
    return rebased_messages, not stop
