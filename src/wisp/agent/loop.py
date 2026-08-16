"""Pure provider and tool-call agent loop."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

import wisp.providers.events as provider_events
from wisp.agent.configuration import (
    validate_agent_runtime_limits,
    validate_non_negative_integer,
)
from wisp.agent.context import (
    build_context_budget,
    estimate_context,
    observe_context,
    trailing_context_estimate,
)
from wisp.agent.execution import (
    ContextOverflowHook,
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundaryHook,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.agent.messages import Message
from wisp.events import (
    ContextBudget,
    ContextEstimated,
    ContextOverflow,
    ContextPressure,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    TokenUsage,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
    UsageCost,
)
from wisp.providers.base import (
    ContextOverflowError,
    ContinuationMessageProvider,
    PromptCacheContinuationMessageProvider,
    PromptCacheKeyProvider,
    Provider,
    ProviderProtocolError,
    StructuredToolReplacementProvider,
    ToolCallResult,
    ToolSpec,
    is_context_overflow_message,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ToolCall,
)

type AgentLoopEvent = (
    TurnStarted
    | ContextEstimated
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ContextPressure
    | ContextOverflow
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | ErrorEvent
)
type UsageCostEstimator = Callable[[str, str | None, str | None, TokenUsage], UsageCost]


class CancellationToken(Protocol):
    """Cooperative cancellation state observed by the pure loop."""

    def is_cancelled(self) -> bool:
        """Return whether the current run should stop."""
        ...


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Dependencies and limits for one provider-neutral loop run."""

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None
    cancellation_token: CancellationToken | None = None
    # Provider-native reasoning-effort tier string (e.g. Anthropic's "high",
    # Google's "MEDIUM", OpenAI's "low") -- not normalized across providers,
    # forwarded to Provider.stream() as-is. None means "use the provider's
    # own default behavior."
    effort: str | None = None
    context_window: int | None = None
    context_reserve_tokens: int = 16_384
    context_pressure_threshold: float = 0.8
    turn_offset: int = 0
    tool_iteration_offset: int = 0
    cost_estimator: UsageCostEstimator | None = None
    defer_context_overflow_errors: bool = False
    prompt_cache_key: str | None = None
    request_boundary_hook: RequestBoundaryHook | None = None
    context_overflow_hook: ContextOverflowHook | None = None

    def __post_init__(self) -> None:
        validate_agent_runtime_limits(
            max_tool_iterations=self.max_tool_iterations,
            context_window=self.context_window,
            context_reserve_tokens=self.context_reserve_tokens,
            context_pressure_threshold=self.context_pressure_threshold,
        )
        validate_non_negative_integer(self.turn_offset, field="turn_offset")
        validate_non_negative_integer(self.tool_iteration_offset, field="tool_iteration_offset")


def _is_cancelled(config: AgentLoopConfig) -> bool:
    token = config.cancellation_token
    return token is not None and token.is_cancelled()


def _is_tool_shaped(message: Message) -> bool:
    """Return whether a row belongs to a structured assistant/tool exchange."""

    return bool(message.tool_calls) or message.role == "tool"


def _has_valid_replacement_tool_order(messages: Sequence[Message]) -> bool:
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


def _fold_clean_continuation(
    state: _AgentLoopState, messages: Sequence[Message]
) -> Sequence[Message]:
    """Fold the just-finished turn's own answer into `messages` and reset.

    Only called when nothing in `state.continuation_messages` is tool-shaped
    (see `_at_request_boundary`) -- folding a plain completed-turn answer and
    resetting `previous_response_id` is always safe there, since every
    provider's plain-message converter round-trips ordinary text messages
    correctly.
    """

    folded = (*messages, *state.continuation_messages)
    state.continuation_messages.clear()
    state.clear_native_continuation()
    return folded


def _continuation_snapshot(state: _AgentLoopState) -> tuple[Message, ...]:
    """Return a deep, immutable-facing view of live continuation state."""

    # `Message`/`ToolCallSnapshot` are frozen, but a `ToolCallSnapshot`'s
    # arguments contain a mutable dict. Never expose the loop's live state.
    return tuple(message.model_copy(deep=True) for message in state.continuation_messages)


def _validate_replacement_messages(
    config: AgentLoopConfig, messages: Sequence[Message]
) -> tuple[Message, ...]:
    """Validate a caller-owned portable base before a fresh/rebased request."""

    replacement = tuple(messages)
    if not _has_valid_replacement_tool_order(replacement):
        raise RequestBoundaryUnsupportedError(
            "RequestBoundaryDecision.messages contains an unpaired structured tool exchange"
        )
    if any(_is_tool_shaped(message) for message in replacement) and not (
        _provider_supports_structured_tool_replacement(config.provider, effort=config.effort)
    ):
        raise RequestBoundaryUnsupportedError(
            "The provider cannot fresh-replay a structured tool exchange for this effort"
        )
    return replacement


def _apply_request_boundary_decision(
    config: AgentLoopConfig,
    state: _AgentLoopState,
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
    if any(message.role != "user" or _is_tool_shaped(message) for message in extra_messages):
        raise RequestBoundaryUnsupportedError(
            "RequestBoundaryDecision.extra_messages must contain only plain user messages"
        )

    if decision.messages is not None:
        replacement = _validate_replacement_messages(config, decision.messages)
        # A replacement is caller-owned, self-contained context. It may retain
        # the active structured tool pair; each adapter is responsible for
        # encoding that fresh context natively. Extras become part of the fresh
        # base, so this transition never depends on an optional capability.
        state.replace_context()
        return (*replacement, *extra_messages), False

    rebase = decision.context_rebase
    if rebase is not None:
        if not _provider_supports_context_rebase(config.provider):
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
        replacement = _validate_replacement_messages(config, rebase.base_messages)
        # Do not call `replace_context`: rebase deliberately keeps the live
        # provider cursor, opaque replay tail, and pending tool results.
        if extra_messages:
            state.queue_extra_messages(extra_messages)
        return replacement, False

    has_tool_history = had_tool_calls or any(
        _is_tool_shaped(message) for message in state.continuation_messages
    )
    supports_continuation_messages = _provider_supports_continuation_messages(config.provider)
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
        return (*_fold_clean_continuation(state, messages), *extra_messages), False

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
    return _fold_clean_continuation(state, messages), False


async def _at_request_boundary(
    config: AgentLoopConfig,
    state: _AgentLoopState,
    *,
    messages: Sequence[Message],
    had_tool_calls: bool,
    stop_by_default: bool,
) -> tuple[Sequence[Message], bool]:
    """Apply a typed transition between a completed turn and the next request."""

    if config.request_boundary_hook is None:
        return messages, stop_by_default
    snapshot = RequestBoundarySnapshot(
        turn=state.turn,
        tool_iterations=state.tool_iterations,
        had_tool_calls=had_tool_calls,
        can_append_user_messages=(
            _provider_supports_continuation_messages(config.provider)
            and state.previous_response_id is not None
        ),
        continuation_messages=_continuation_snapshot(state),
    )
    decision = await config.request_boundary_hook.before_next_request(snapshot=snapshot)
    return _apply_request_boundary_decision(
        config,
        state,
        messages=messages,
        had_tool_calls=had_tool_calls,
        decision=decision,
        allow_extra_messages=True,
    )


async def _at_context_overflow(
    config: AgentLoopConfig,
    state: _AgentLoopState,
    *,
    messages: Sequence[Message],
    context_budget: ContextBudget,
    had_streamed_delta: bool,
    message: str,
) -> tuple[Sequence[Message], bool]:
    """Ask the optional hook whether this rejected request can retry safely."""

    if config.context_overflow_hook is None:
        return messages, False
    snapshot = ContextOverflowSnapshot(
        turn=state.turn,
        tool_iterations=state.tool_iterations,
        continuation_messages=_continuation_snapshot(state),
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
    rebased_messages, stop = _apply_request_boundary_decision(
        config,
        state,
        messages=messages,
        had_tool_calls=any(_is_tool_shaped(item) for item in state.continuation_messages),
        decision=decision,
        allow_extra_messages=False,
    )
    return rebased_messages, not stop


def _provider_supports_continuation_messages(provider: Provider) -> bool:
    return getattr(provider, "supports_continuation_messages", False) is True


def _provider_supports_context_rebase(provider: Provider) -> bool:
    return getattr(provider, "supports_context_rebase", False) is True


def _provider_supports_prompt_cache_key(provider: Provider) -> bool:
    return getattr(provider, "supports_prompt_cache_key", False) is True


def _provider_supports_structured_tool_replacement(
    provider: Provider, *, effort: str | None
) -> bool:
    """Negotiate an optional guard for opaque provider-native replay state."""

    capability = getattr(provider, "supports_structured_tool_replacement", None)
    if not callable(capability):
        return True
    replacement_provider = cast(StructuredToolReplacementProvider, provider)
    return replacement_provider.supports_structured_tool_replacement(effort=effort)


def _provider_stream(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
    tool_results: Sequence[ToolCallResult],
    extra_messages: Sequence[Message],
    previous_response_id: str | None,
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Call one provider without imposing optional keywords on legacy adapters."""

    provider = config.provider
    supports_continuation_messages = _provider_supports_continuation_messages(provider)
    supports_prompt_cache_key = _provider_supports_prompt_cache_key(provider)
    use_prompt_cache_key = config.prompt_cache_key is not None and supports_prompt_cache_key

    if extra_messages and supports_continuation_messages and use_prompt_cache_key:
        combined_provider = cast(PromptCacheContinuationMessageProvider, provider)
        if config.effort is not None:
            return combined_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id=previous_response_id,
                effort=config.effort,
                prompt_cache_key=config.prompt_cache_key,
            )
        return combined_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            extra_messages=extra_messages,
            previous_response_id=previous_response_id,
            prompt_cache_key=config.prompt_cache_key,
        )

    if extra_messages and supports_continuation_messages:
        continuation_provider = cast(ContinuationMessageProvider, provider)
        if config.effort is not None:
            return continuation_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id=previous_response_id,
                effort=config.effort,
            )
        return continuation_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            extra_messages=extra_messages,
            previous_response_id=previous_response_id,
        )

    if use_prompt_cache_key:
        cache_provider = cast(PromptCacheKeyProvider, provider)
        if config.effort is not None:
            return cache_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=config.effort,
                prompt_cache_key=config.prompt_cache_key,
            )
        return cache_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
            prompt_cache_key=config.prompt_cache_key,
        )

    if config.effort is not None:
        return provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
            effort=config.effort,
        )
    return provider.stream(
        messages,
        model=config.model,
        tools=config.tools,
        tool_results=tool_results,
        previous_response_id=previous_response_id,
    )


def _cancelled_turn_events(turn: int) -> tuple[ErrorEvent, TurnCompleted]:
    return (
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=turn, outcome="cancelled", finish_reason="cancelled"),
    )


def _unavailable_cost(
    provider: str,
    requested_model: str | None,
    response_model: str | None,
    *,
    reason: Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"],
) -> UsageCost:
    """Keep optional accounting failures from discarding a completed provider response."""

    return UsageCost(
        provider=provider,
        requested_model=requested_model,
        model=response_model or requested_model,
        unavailable_reason=reason,
    )


def _require_provider_response_started(started: bool) -> None:
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


def _resolve_provider_response_id(
    *,
    started_response_id: str | None,
    terminal_response_id: str | None,
    tool_calls: Sequence[ToolCall],
) -> str | None:
    """Resolve one consistent response id from a provider lifecycle."""

    candidates = [
        ("response_started", started_response_id),
        ("terminal response", terminal_response_id),
        *((f"tool call {tool_call.call_id}", tool_call.response_id) for tool_call in tool_calls),
    ]
    supplied = [
        (source, response_id) for source, response_id in candidates if response_id is not None
    ]
    if not supplied:
        return None

    resolved_source, resolved_id = supplied[0]
    for source, response_id in supplied[1:]:
        if response_id != resolved_id:
            raise ProviderProtocolError(
                "Provider emitted conflicting response ids: "
                f"{resolved_source}={resolved_id!r}, {source}={response_id!r}"
            )
    return resolved_id


@dataclass(frozen=True, slots=True)
class _CompletedProviderResponse:
    """Validated successful provider response assembled from one stream."""

    response: ProviderResponseCompleted
    content: str
    response_id: str | None
    response_model: str | None


@dataclass(frozen=True, slots=True)
class _FailedProviderResponse:
    """Validated terminal provider failure assembled from one stream."""

    response: ProviderResponseFailed
    content: str
    response_id: str | None


@dataclass(slots=True)
class _ProviderResponseLifecycle:
    """Own and validate the state transitions for one provider response."""

    started: bool = False
    started_response_id: str | None = None
    response_model: str | None = None
    terminal: ProviderResponseCompleted | ProviderResponseFailed | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def require_open(self) -> None:
        if self.terminal is not None:
            raise ProviderProtocolError("Provider emitted an event after its terminal response")

    def start(self, event: ProviderResponseStarted) -> None:
        self.require_open()
        if self.started:
            raise ProviderProtocolError("Provider emitted response_started more than once")
        self.started = True
        self.started_response_id = event.response_id
        self.response_model = event.model

    def retry(self) -> None:
        self.require_open()
        if self.started:
            raise ProviderProtocolError("Provider emitted retry progress after response_started")

    def add_text(self, delta: str) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        self.text.append(delta)

    def add_thinking(self, delta: str) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        del delta

    def add_tool_call(self, tool_call: ToolCall) -> None:
        self.require_open()
        _require_provider_response_started(self.started)
        self.tool_calls.append(tool_call)

    def complete(self, event: ProviderResponseCompleted | ProviderResponseFailed) -> None:
        self.require_open()
        if isinstance(event, ProviderResponseCompleted):
            _require_provider_response_started(self.started)
        self.terminal = event

    def finish(self) -> _CompletedProviderResponse | _FailedProviderResponse:
        if self.terminal is None:
            if not self.started:
                raise ProviderProtocolError("Provider stream ended before response_started")
            raise ProviderProtocolError("Provider stream ended without a terminal response")
        if isinstance(self.terminal, ProviderResponseFailed):
            response_id = _resolve_provider_response_id(
                started_response_id=self.started_response_id,
                terminal_response_id=self.terminal.response_id,
                tool_calls=self.tool_calls,
            )
            return _FailedProviderResponse(
                response=self.terminal,
                content=self.terminal.partial_content or "".join(self.text),
                response_id=response_id,
            )
        if not self.started:
            raise ProviderProtocolError("Provider stream ended before response_started")
        if tuple(self.tool_calls) != self.terminal.tool_calls:
            raise ProviderProtocolError(
                "Provider terminal tool calls do not match streamed tool calls"
            )
        response_id = _resolve_provider_response_id(
            started_response_id=self.started_response_id,
            terminal_response_id=self.terminal.response_id,
            tool_calls=self.terminal.tool_calls,
        )
        return _CompletedProviderResponse(
            response=self.terminal,
            content=self.terminal.content or "".join(self.text),
            response_id=response_id,
            response_model=self.response_model,
        )


@dataclass(slots=True)
class _AgentLoopState:
    """Mutable continuation state shared across provider/tool turns."""

    turn: int
    tool_iterations: int
    pending_tool_results: tuple[ToolCallResult, ...] = ()
    pending_extra_messages: tuple[Message, ...] = ()
    previous_response_id: str | None = None
    continuation_messages: list[Message] = field(default_factory=list)

    def begin_turn(self) -> int:
        self.turn += 1
        return self.turn

    def record_response(self, completed: _CompletedProviderResponse, message: Message) -> None:
        # Public response IDs remain upstream-observed values. Stateless
        # adapters may safely retain their existing local replay key when a
        # later clean response has no new upstream ID, so do not erase a
        # usable cursor in that case.
        if completed.response_id is not None:
            self.previous_response_id = completed.response_id
        self.pending_extra_messages = ()
        self.continuation_messages.append(message)

    def begin_tool_round(self, maximum: int | None) -> None:
        if maximum is not None and self.tool_iterations >= maximum:
            raise RuntimeError(f"Maximum tool iterations exceeded: {maximum}")
        self.tool_iterations += 1

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


async def _provider_events(
    stream: AsyncIterator[provider_events.ProviderEvent],
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Normalize context overflows raised while advancing a provider stream."""

    iterator = aiter(stream)
    while True:
        try:
            event = await anext(iterator)
        except StopAsyncIteration:
            return
        except Exception as exc:
            if is_context_overflow_message(str(exc)):
                raise ContextOverflowError(str(exc)) from exc
            raise
        yield event


def _json_payloads_match(left: object, right: object) -> bool:
    """Compare JSON payloads canonically without conflating booleans and numbers."""

    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ) == json.dumps(
            right,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False


@dataclass(slots=True)
class _ToolExecutionLifecycle:
    """Validate one executor stream before its result reaches the provider."""

    tool_call: ToolCall
    approval_requested: bool = False
    approval_resolved: bool = False
    approved: bool | None = None
    terminal: ToolExecutionEnded | None = None

    def accept(self, event: object) -> ToolExecutionEvent:
        if not isinstance(event, ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded):
            raise ToolExecutionProtocolError(
                "Tool executor emitted an unsupported event type for "
                f"{self.tool_call.call_id}: {type(event).__name__}"
            )
        if self.terminal is not None:
            raise ToolExecutionProtocolError(
                f"Tool executor emitted an event after the result for {self.tool_call.call_id}"
            )
        if event.call_id != self.tool_call.call_id or event.name != self.tool_call.name:
            raise ToolExecutionProtocolError(
                "Tool executor event does not match the requested call: "
                f"expected {self.tool_call.name}/{self.tool_call.call_id}, "
                f"got {event.name}/{event.call_id}"
            )

        if isinstance(event, ToolApprovalRequested):
            if self.approval_requested:
                raise ToolExecutionProtocolError(
                    f"Tool executor requested approval more than once for {self.tool_call.call_id}"
                )
            if not _json_payloads_match(event.arguments, self.tool_call.arguments):
                raise ToolExecutionProtocolError(
                    "Tool executor approval arguments do not match the requested call "
                    f"{self.tool_call.call_id}"
                )
            self.approval_requested = True
        elif isinstance(event, ToolApprovalResolved):
            if not self.approval_requested:
                raise ToolExecutionProtocolError(
                    "Tool executor resolved approval before requesting it for "
                    f"{self.tool_call.call_id}"
                )
            if self.approval_resolved:
                raise ToolExecutionProtocolError(
                    f"Tool executor resolved approval more than once for {self.tool_call.call_id}"
                )
            self.approval_resolved = True
            self.approved = event.approved
        else:
            if self.approval_requested and not self.approval_resolved:
                raise ToolExecutionProtocolError(
                    f"Tool executor ended with an unresolved approval for {self.tool_call.call_id}"
                )
            if self.approved is False and not event.is_error:
                raise ToolExecutionProtocolError(
                    "Tool executor reported success after approval was denied for "
                    f"{self.tool_call.call_id}"
                )
            self.terminal = event
        return event

    def finish(self) -> ToolExecutionEnded:
        if self.approval_requested and not self.approval_resolved:
            raise ToolExecutionProtocolError(
                f"Tool executor ended with an unresolved approval for {self.tool_call.call_id}"
            )
        if self.terminal is None:
            raise ToolExecutionProtocolError(
                f"Tool executor ended without a result for {self.tool_call.call_id}"
            )
        return self.terminal


async def _execute_tool_call(
    config: AgentLoopConfig,
    tool_call: ToolCall,
) -> AsyncIterator[ToolExecutionEvent | ToolResultReady]:
    lifecycle = _ToolExecutionLifecycle(tool_call)
    async for raw_event in config.tool_executor.execute(tool_call):
        event = lifecycle.accept(raw_event)
        if not isinstance(event, ToolExecutionEnded):
            yield event

    terminal = lifecycle.finish()
    yield terminal
    yield ToolResultReady.from_execution_ended(terminal)


async def run_agent_loop(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
) -> AsyncGenerator[AgentLoopEvent, None]:
    """Run provider turns and tool cycles without session or frontend dependencies."""

    state = _AgentLoopState(
        turn=config.turn_offset,
        tool_iterations=config.tool_iteration_offset,
    )
    # Bind `turn`/`turn_started` before the loop so the outer `except` below
    # can always reference them, even if an exception (e.g. a raising
    # CancellationToken) fires before the first `state.begin_turn()` call.
    # `turn_started` -- rather than `turn > 0` -- distinguishes "no turn
    # started this invocation" from "a real turn is in flight", since a
    # nonzero `turn_offset` would otherwise make `turn > 0` true even when
    # this call never emitted a matching `TurnStarted`.
    turn = config.turn_offset
    turn_started = False

    try:
        while True:
            turn_started = False
            if _is_cancelled(config):
                yield ErrorEvent(message="Agent run cancelled")
                break
            turn = state.begin_turn()
            turn_started = True
            yield TurnStarted(turn=turn)
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            lifecycle = _ProviderResponseLifecycle()

            request_messages = (*messages, *state.continuation_messages)
            selected_model = config.model or config.provider.default_model
            previous_observation = next(
                (
                    message.context_observation
                    for message in reversed(request_messages)
                    if message.context_observation is not None
                ),
                None,
            )
            estimate = estimate_context(request_messages, config.tools)
            trailing_estimate = (
                trailing_context_estimate(request_messages, config.tools, previous_observation)
                if previous_observation is not None
                and previous_observation.provider == config.provider.name
                and previous_observation.model == selected_model
                else None
            )
            context_budget = build_context_budget(
                estimate,
                context_window=config.context_window,
                reserve_tokens=config.context_reserve_tokens,
                observed_tokens=(
                    previous_observation.input_tokens if previous_observation is not None else None
                ),
                observed_is_current=trailing_estimate is not None,
                trailing_estimated_tokens=(
                    trailing_estimate.total_tokens if trailing_estimate is not None else None
                ),
            )
            yield ContextEstimated(
                turn=turn,
                provider=config.provider.name,
                model=selected_model,
                budget=context_budget,
            )

            attempt_had_streamed_delta = False
            request_overflow_error: ContextOverflowError | None = None
            try:
                provider_stream = _provider_stream(
                    config,
                    messages=messages,
                    tool_results=state.pending_tool_results,
                    extra_messages=state.pending_extra_messages,
                    previous_response_id=state.previous_response_id,
                )
                async for provider_event in _provider_events(provider_stream):
                    if _is_cancelled(config):
                        for event in _cancelled_turn_events(turn):
                            yield event
                        return
                    lifecycle.require_open()
                    if isinstance(provider_event, ProviderResponseStarted):
                        lifecycle.start(provider_event)
                        yield MessageStarted(turn=turn)
                    elif isinstance(provider_event, provider_events.ProviderRetrying):
                        lifecycle.retry()
                        yield ProviderRetrying(
                            turn=turn,
                            provider=config.provider.name,
                            attempt=provider_event.attempt,
                            max_attempts=provider_event.max_attempts,
                            delay_seconds=provider_event.delay_seconds,
                            reason=provider_event.reason,
                            status_code=provider_event.status_code,
                        )
                    elif isinstance(provider_event, ProviderTextDelta):
                        lifecycle.add_text(provider_event.delta)
                        attempt_had_streamed_delta = True
                        yield MessageDelta(
                            turn=turn,
                            delta=provider_event.delta,
                            content_index=provider_event.content_index,
                        )
                    elif isinstance(provider_event, ProviderThinkingDelta):
                        lifecycle.add_thinking(provider_event.delta)
                        attempt_had_streamed_delta = True
                        yield MessageDelta(
                            turn=turn,
                            delta=provider_event.delta,
                            content_index=provider_event.content_index,
                            content_kind="thinking",
                        )
                    elif isinstance(provider_event, ProviderToolCallCompleted):
                        lifecycle.add_tool_call(provider_event.tool_call)
                    elif isinstance(
                        provider_event, ProviderResponseCompleted | ProviderResponseFailed
                    ):
                        lifecycle.complete(provider_event)
                    else:
                        event_type = type(provider_event).__name__
                        raise ProviderProtocolError(
                            f"Provider emitted unsupported event type: {event_type}"
                        )
            except ContextOverflowError as exc:
                request_overflow_error = exc
            except Exception as exc:
                if is_context_overflow_message(str(exc)):
                    request_overflow_error = ContextOverflowError(str(exc))
                else:
                    raise

            if request_overflow_error is not None:
                # Preserve the historical raised-overflow path for callers
                # without an explicit same-loop recovery hook. The outer
                # handler owns its public terminal events and re-raises.
                if config.context_overflow_hook is None:
                    raise request_overflow_error
                yield ContextOverflow(
                    turn=turn,
                    provider=config.provider.name,
                    model=config.model or config.provider.default_model,
                    context_window=config.context_window,
                    message=str(request_overflow_error),
                )
                messages, retry = await _at_context_overflow(
                    config,
                    state,
                    messages=messages,
                    context_budget=context_budget,
                    had_streamed_delta=attempt_had_streamed_delta,
                    message=str(request_overflow_error),
                )
                if retry:
                    yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                    turn_started = False
                    continue
                if config.defer_context_overflow_errors:
                    return
                yield ErrorEvent(message=str(request_overflow_error))
                yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                return

            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            completed = lifecycle.finish()
            if isinstance(completed, _FailedProviderResponse):
                failure = completed.response
                failure_kind = (
                    "context_overflow"
                    if failure.failure_kind == "context_overflow"
                    or is_context_overflow_message(failure.message)
                    else failure.failure_kind
                )
                if lifecycle.started:
                    yield MessageCompleted(
                        turn=turn,
                        content=completed.content,
                        finish_reason="error",
                        response_id=completed.response_id,
                    )
                if failure_kind == "context_overflow":
                    yield ContextOverflow(
                        turn=turn,
                        provider=config.provider.name,
                        model=config.model or config.provider.default_model,
                        context_window=config.context_window,
                        message=failure.message,
                    )
                    messages, retry = await _at_context_overflow(
                        config,
                        state,
                        messages=messages,
                        context_budget=context_budget,
                        had_streamed_delta=attempt_had_streamed_delta,
                        message=failure.message,
                    )
                    if retry:
                        yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
                        turn_started = False
                        continue
                    if config.defer_context_overflow_errors:
                        return
                yield ErrorEvent(message=failure.message)
                yield TurnCompleted(
                    turn=turn,
                    outcome="cancelled" if failure_kind == "aborted" else "failed",
                    finish_reason="cancelled" if failure_kind == "aborted" else "error",
                )
                return
            response = completed.response
            completed_content = completed.content
            tool_calls = response.tool_calls
            response_id = completed.response_id
            usage = (
                TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    cache_read_input_tokens=response.usage.cache_read_input_tokens,
                    cache_write_input_tokens=response.usage.cache_write_input_tokens,
                    reasoning_output_tokens=response.usage.reasoning_output_tokens,
                )
                if response.usage is not None
                else None
            )
            if usage is None:
                cost = _unavailable_cost(
                    config.provider.name,
                    config.model,
                    completed.response_model,
                    reason="usage_incomplete",
                )
            elif config.cost_estimator is None:
                cost = _unavailable_cost(
                    config.provider.name,
                    config.model,
                    completed.response_model,
                    reason="pricing_unavailable",
                )
            else:
                try:
                    cost = config.cost_estimator(
                        config.provider.name,
                        config.model,
                        completed.response_model,
                        usage,
                    )
                except Exception:
                    cost = _unavailable_cost(
                        config.provider.name,
                        config.model,
                        completed.response_model,
                        reason="estimation_failed",
                    )
            tool_call_snapshots = tuple(
                ToolCallSnapshot(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    provider_call_id=tool_call.provider_call_id,
                    parse_error=tool_call.parse_error,
                )
                for tool_call in tool_calls
            )
            # Completion events cross a public yield boundary. Deep-copy the
            # snapshots before yielding so consumer mutation cannot alter provider
            # continuation state.
            continuation_tool_calls = tuple(
                snapshot.model_copy(deep=True) for snapshot in tool_call_snapshots
            )
            context_observation = (
                observe_context(
                    request_messages,
                    config.tools,
                    provider=config.provider.name,
                    model=selected_model,
                    input_tokens=(
                        response.usage.context_input_tokens
                        if response.usage is not None
                        and response.usage.context_input_tokens is not None
                        else usage.input_tokens
                    ),
                )
                if usage is not None
                else None
            )
            yield MessageCompleted(
                turn=turn,
                content=completed_content,
                finish_reason=response.finish_reason,
                response_id=response_id,
                usage=usage,
                cost=cost,
                context_observation=context_observation,
                tool_calls=tool_call_snapshots,
            )
            continuation_message = Message(
                role="assistant",
                content=completed_content,
                response_id=response_id,
                finish_reason=response.finish_reason,
                usage=usage,
                cost=cost,
                context_observation=context_observation,
                tool_calls=continuation_tool_calls,
            )
            state.record_response(completed, continuation_message)
            if usage is not None and config.context_window is not None:
                pressure_ratio = usage.input_tokens / config.context_window
                if pressure_ratio >= config.context_pressure_threshold:
                    yield ContextPressure(
                        turn=turn,
                        provider=config.provider.name,
                        model=config.model or config.provider.default_model,
                        context_window=config.context_window,
                        observed_tokens=usage.input_tokens,
                        remaining_tokens=max(0, config.context_window - usage.input_tokens),
                        pressure_ratio=pressure_ratio,
                    )

            if not tool_calls:
                yield TurnCompleted(
                    turn=turn,
                    outcome="completed",
                    finish_reason=response.finish_reason,
                )
                # This turn has already yielded its one terminal event -- a
                # boundary-hook failure past this point is not a failure *of*
                # `turn` and must not produce a second, contradictory
                # TurnCompleted for it in the except block below.
                turn_started = False
                if not _is_cancelled(config):
                    messages, stop = await _at_request_boundary(
                        config,
                        state,
                        messages=messages,
                        had_tool_calls=False,
                        stop_by_default=True,
                    )
                    if not stop:
                        continue
                break
            if _is_cancelled(config):
                for event in _cancelled_turn_events(turn):
                    yield event
                break
            state.begin_tool_round(config.max_tool_iterations)
            tool_results: list[ToolCallResult] = []
            for tool_call in tool_calls:
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                arguments = dict(tool_call.arguments)
                yield ToolCallRequested(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=arguments,
                )
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                yield ToolExecutionStarted(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=arguments,
                )
                if _is_cancelled(config):
                    for event in _cancelled_turn_events(turn):
                        yield event
                    return
                result_event: ToolResultReady | None = None
                async for execution_event in _execute_tool_call(config, tool_call):
                    yield execution_event
                    if isinstance(execution_event, ToolResultReady):
                        result_event = execution_event
                    if _is_cancelled(config) and not isinstance(
                        execution_event, ToolExecutionEnded
                    ):
                        for event in _cancelled_turn_events(turn):
                            yield event
                        return
                if result_event is None:
                    raise ToolExecutionProtocolError(
                        f"Tool executor produced no provider result for {tool_call.call_id}"
                    )
                tool_results.append(
                    ToolCallResult(
                        call_id=result_event.call_id,
                        output=result_event.output,
                        is_error=result_event.is_error,
                    )
                )
                state.continuation_messages.append(
                    Message(
                        role="tool",
                        content=result_event.output,
                        tool_call_id=result_event.call_id,
                        tool_name=result_event.name,
                        is_error=result_event.is_error,
                    )
                )
            state.complete_tool_round(tool_results)
            yield TurnCompleted(
                turn=turn,
                outcome="completed",
                finish_reason=response.finish_reason,
            )
            # See the no-tool-calls boundary above: this turn's one terminal
            # event has already been yielded.
            turn_started = False
            if not _is_cancelled(config):
                messages, stop = await _at_request_boundary(
                    config,
                    state,
                    messages=messages,
                    had_tool_calls=True,
                    stop_by_default=False,
                )
                if stop:
                    break
    except Exception as exc:
        overflow_error: ContextOverflowError | None = None
        if isinstance(exc, ContextOverflowError):
            overflow_error = exc
        if overflow_error is not None:
            yield ContextOverflow(
                turn=turn,
                provider=config.provider.name,
                model=config.model or config.provider.default_model,
                context_window=config.context_window,
                message=str(overflow_error),
            )
            if config.defer_context_overflow_errors:
                if overflow_error is not exc:
                    raise overflow_error from exc
                raise
        yield ErrorEvent(message=str(exc))
        if turn_started:
            yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
        if overflow_error is not None and overflow_error is not exc:
            raise overflow_error from exc
        raise


__all__ = [
    "AgentLoopConfig",
    "AgentLoopEvent",
    "CancellationToken",
    "run_agent_loop",
]
