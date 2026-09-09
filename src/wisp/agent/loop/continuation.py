"""Continuation state and request-boundary transitions for the agent loop."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
)
from wisp.events import ContextBudget, ToolResultReady
from wisp.providers.base import (
    Provider,
    ToolCallResult,
    structured_tool_replacement_support,
)

if TYPE_CHECKING:
    from .config import AgentLoopConfig


@dataclass(slots=True)
class ContinuationState:
    """Provider cursor, pending request data, and the live continuation transcript."""

    pending_tool_results: tuple[ToolCallResult, ...] = ()
    pending_extra_messages: tuple[Message, ...] = ()
    previous_response_id: str | None = None
    continuation_messages: list[Message] = field(default_factory=list)

    def record_response(self, message: Message, *, response_id: str | None) -> None:
        """Append a successful response and consume queued extra messages.

        Args:
            message (Message): Assistant transcript row with isolated tool snapshots.
            response_id (str | None): New provider cursor. None retains any prior cursor.
        """
        # Public response IDs remain upstream-observed values. Stateless
        # adapters may safely retain their existing local replay key when a
        # later clean response has no new upstream ID, so do not erase a
        # usable cursor in that case.
        if response_id is not None:
            self.previous_response_id = response_id
        self.pending_extra_messages = ()
        self.continuation_messages.append(message)

    def record_tool_result(self, result_event: ToolResultReady) -> None:
        """Append a tool's provider-visible output to the continuation transcript.

        Args:
            result_event (ToolResultReady): Result containing the call identity and output.
        """
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
        """Store batch results for the next provider request.

        Args:
            results (Sequence[ToolCallResult]): Ordered results, including any partial
                results collected before cancellation.
        """
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
        """Queue user messages for exactly the next continued request.

        Appends to the transcript and replaces the pending extra-message tuple.
        The boundary decision validator is responsible for checking message roles.

        Args:
            messages (Sequence[Message]): Validated plain user messages to inject.
        """

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

        Args:
            messages (Sequence[Message]): Portable base history to extend.

        Returns:
            Sequence[Message]: Base history followed by the live continuation. The
                continuation transcript, cursor, and pending inputs are cleared.

        Examples:
            >>> state = ContinuationState()
            >>> state.record_response(Message(role="assistant", content="Done"), response_id="r1")
            >>> folded = state.fold_clean((Message(role="user", content="Hello"),))
            >>> [message.content for message in folded]
            ['Hello', 'Done']
            >>> state.previous_response_id is None
            True
        """

        folded = (*messages, *self.continuation_messages)
        self.continuation_messages.clear()
        self.clear_native_continuation()
        return folded

    def snapshot(self) -> tuple[Message, ...]:
        """Copy the continuation transcript for a caller or hook.

        Returns:
            tuple[Message, ...]: Deep copies in transcript order. Mutating nested tool
                arguments in a snapshot cannot change the live continuation.
        """

        # `Message`/`ToolCallSnapshot` are frozen, but a `ToolCallSnapshot`'s
        # arguments contain a mutable dict. Never expose the loop's live state.
        return tuple(message.model_copy(deep=True) for message in self.continuation_messages)


def is_tool_shaped(message: Message) -> bool:
    """Check whether a row belongs to a structured assistant/tool exchange.

    Args:
        message (Message): Transcript row to inspect.

    Returns:
        bool: True for a tool result or a message carrying tool-call snapshots.
    """

    return bool(message.tool_calls) or message.role == "tool"


def has_valid_replacement_tool_order(messages: Sequence[Message]) -> bool:
    """Require each native tool result in a fresh replacement to be paired.

    A replacement may retain the active exchange that compaction cannot
    safely summarize. It must still be self-contained: accepting an orphaned
    tool row would make adapter-specific error handling decide whether raw
    tool output is trusted context.

    Args:
        messages (Sequence[Message]): Complete replacement history to inspect.

    Returns:
        bool: True when every assistant call group is immediately followed by exactly
            its matching tool results, in any result order, with no orphan tool rows.

    Examples:
        >>> has_valid_replacement_tool_order((Message(role="user", content="Hello"),))
        True
        >>> orphan = Message(role="tool", content="output", tool_call_id="missing")
        >>> has_valid_replacement_tool_order((orphan,))
        False
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
    """Validate a caller-owned portable base before a fresh or rebased request.

    Args:
        config (AgentLoopConfig): Provider and effort used to check replay support.
        messages (Sequence[Message]): Self-contained replacement history.

    Returns:
        tuple[Message, ...]: Validated history in its original order, without deep copies.

    Raises:
        RequestBoundaryUnsupportedError: Tool exchanges are unpaired or the provider
            explicitly rejects structured replay for the requested effort.
    """

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
    """Check the adapter's explicit opt-in to extra continuation messages.

    Args:
        provider (Provider): Adapter to inspect without requiring a new protocol member.

    Returns:
        bool: True only when supports_continuation_messages is literally True.
    """
    return getattr(provider, "supports_continuation_messages", False) is True


def provider_supports_context_rebase(provider: Provider) -> bool:
    """Check the adapter's explicit opt-in to rebasing beneath a live cursor.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_context_rebase is literally True.
    """
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
    """Apply a caller's stop, replacement, rebase, or continuation decision.

    Stop takes precedence without changing state. Replacement discards live continuation;
    rebase keeps the cursor and replay tail after checking the expected transcript.
    Plain continuation may consume old results or fold a text-only tail into the base.

    Args:
        config (AgentLoopConfig): Provider capabilities and replay settings.
        state (ContinuationState): Live state to update as the decision is applied.
        messages (Sequence[Message]): Current portable base history.
        had_tool_calls (bool): Whether the preceding turn produced a tool batch.
        decision (RequestBoundaryDecision): Caller-supplied transition and optional inputs.
        allow_extra_messages (bool): Whether this boundary permits user-message injection.

    Returns:
        tuple[Sequence[Message], bool]: Updated base history and a stop flag. True means
            no further request should be made.

    Examples:
        Replace context using an existing run configuration::

            decision = RequestBoundaryDecision(
                messages=(Message(role="user", content="Compacted summary"),)
            )
            messages, stop = apply_request_boundary_decision(
                config, state, messages=history, had_tool_calls=True,
                decision=decision, allow_extra_messages=True,
            )
            # stop is False; state now has no cursor or pending tool results.

    Raises:
        RequestBoundaryUnsupportedError: The decision conflicts with itself, contains
            invalid messages, requests unsupported continuation, or has a stale rebase.
            Some continuation paths consume pending results before detecting an error.
    """

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
    """Consult the optional policy hook after a successful turn.

    Args:
        config (AgentLoopConfig): Run configuration with an optional boundary hook.
        state (ContinuationState): Live continuation used to build an isolated snapshot.
        turn (int): Number of the completed turn.
        tool_iterations (int): Tool batches consumed so far, including the offset.
        messages (Sequence[Message]): Current base history.
        had_tool_calls (bool): Whether this turn executed a tool batch.
        stop_by_default (bool): Decision used when no hook is configured.

    Returns:
        tuple[Sequence[Message], bool]: Updated base history and whether to stop.

    Raises:
        RequestBoundaryUnsupportedError: The hook's transition cannot be represented safely.
        Exception: An exception raised by the hook propagates to the loop.
    """

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
    """Ask the optional hook to replace or rebase a rejected request's context.

    Args:
        config (AgentLoopConfig): Run configuration with an optional recovery hook.
        state (ContinuationState): Live continuation to snapshot and potentially update.
        turn (int): Number of the rejected turn.
        tool_iterations (int): Tool batches consumed so far, including the offset.
        messages (Sequence[Message]): Current base history.
        context_budget (ContextBudget): Estimate made before the rejected request.
        had_streamed_delta (bool): Whether text or thinking escaped during that attempt.
        message (str): Provider's overflow description.

    Returns:
        tuple[Sequence[Message], bool]: Updated history and a retry flag. True means retry,
            unlike the stop flag returned by at_request_boundary. False means no hook,
            declined recovery, or a stop decision.

    Examples:
        A recovery hook can supply caller-owned compacted history::

            class ReplaceOverflowContext:
                def __init__(self, compacted_messages):
                    self.compacted_messages = tuple(compacted_messages)

                async def recover_context_overflow(self, *, snapshot):
                    if snapshot.had_streamed_delta:
                        return None
                    return RequestBoundaryDecision(messages=self.compacted_messages)

    Raises:
        RequestBoundaryUnsupportedError: Recovery appends extra messages, omits a
            replacement/rebase, or fails the normal transition validation.
        Exception: An exception raised by the recovery hook propagates to the loop.
    """

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
