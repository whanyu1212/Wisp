"""Pure provider and tool-call agent loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any

import wisp.providers.events as provider_events
from wisp.agent.execution import (
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import (
    Provider,
    ProviderError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
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
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | ErrorEvent
)

# The value remains unbound until __getattr__ loads the compatibility adapter.
Agent: Any


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """Dependencies and limits for one provider-neutral loop run."""

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None


def _require_provider_response_started(started: bool) -> None:
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


def _validate_execution_event(event: ToolExecutionEvent, tool_call: ToolCall) -> None:
    if event.call_id != tool_call.call_id or event.name != tool_call.name:
        raise ToolExecutionProtocolError(
            "Tool executor event does not match the requested call: "
            f"expected {tool_call.name}/{tool_call.call_id}, "
            f"got {event.name}/{event.call_id}"
        )


async def _execute_tool_call(
    config: AgentLoopConfig,
    tool_call: ToolCall,
) -> AsyncIterator[ToolExecutionEvent | ToolResultReady]:
    terminal: ToolExecutionEnded | None = None
    async for event in config.tool_executor.execute(tool_call):
        if terminal is not None:
            raise ToolExecutionProtocolError(
                f"Tool executor emitted an event after the result for {tool_call.call_id}"
            )
        _validate_execution_event(event, tool_call)
        if isinstance(event, ToolExecutionEnded):
            terminal = event
        else:
            yield event

    if terminal is None:
        raise ToolExecutionProtocolError(
            f"Tool executor ended without a result for {tool_call.call_id}"
        )
    yield terminal
    yield ToolResultReady(
        call_id=terminal.call_id,
        name=terminal.name,
        output=terminal.output,
        is_error=terminal.is_error,
    )


async def run_agent_loop(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
) -> AsyncIterator[AgentLoopEvent]:
    """Run provider turns and tool cycles without session or frontend dependencies."""

    pending_tool_results: tuple[ToolCallResult, ...] = ()
    previous_response_id: str | None = None
    tool_iterations = 0
    turn = 0

    try:
        while True:
            turn += 1
            yield TurnStarted(turn=turn)
            response_started = False
            terminal_response: ProviderResponseCompleted | ProviderResponseFailed | None = None
            streamed_tool_calls: list[ToolCall] = []

            async for provider_event in config.provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=pending_tool_results,
                previous_response_id=previous_response_id,
            ):
                if terminal_response is not None:
                    raise ProviderProtocolError(
                        "Provider emitted an event after its terminal response"
                    )
                if isinstance(provider_event, ProviderResponseStarted):
                    if response_started:
                        raise ProviderProtocolError(
                            "Provider emitted response_started more than once"
                        )
                    response_started = True
                    yield MessageStarted(turn=turn)
                elif isinstance(provider_event, provider_events.ProviderRetrying):
                    if response_started:
                        raise ProviderProtocolError(
                            "Provider emitted retry progress after response_started"
                        )
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
                    _require_provider_response_started(response_started)
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                    )
                elif isinstance(provider_event, ProviderThinkingDelta):
                    _require_provider_response_started(response_started)
                    yield MessageDelta(
                        turn=turn,
                        delta=provider_event.delta,
                        content_index=provider_event.content_index,
                        content_kind="thinking",
                    )
                elif isinstance(provider_event, ProviderToolCallCompleted):
                    _require_provider_response_started(response_started)
                    streamed_tool_calls.append(provider_event.tool_call)
                elif isinstance(provider_event, ProviderResponseCompleted | ProviderResponseFailed):
                    _require_provider_response_started(response_started)
                    terminal_response = provider_event
                else:
                    raise ProviderProtocolError(
                        f"Provider emitted unsupported event type: {type(provider_event).__name__}"
                    )

            if not response_started:
                raise ProviderProtocolError("Provider stream ended before response_started")
            if terminal_response is None:
                raise ProviderProtocolError("Provider stream ended without a terminal response")
            if isinstance(terminal_response, ProviderResponseFailed):
                raise ProviderError(terminal_response.message)
            if tuple(streamed_tool_calls) != terminal_response.tool_calls:
                raise ProviderProtocolError(
                    "Provider terminal tool calls do not match streamed tool calls"
                )

            response = terminal_response
            tool_calls = response.tool_calls
            response_id = response.response_id
            if response_id is None:
                response_id = next(
                    (
                        tool_call.response_id
                        for tool_call in reversed(tool_calls)
                        if tool_call.response_id is not None
                    ),
                    None,
                )
            previous_response_id = response_id
            yield MessageCompleted(
                turn=turn,
                content=response.content,
                finish_reason=response.finish_reason,
                response_id=response_id,
                tool_calls=tuple(
                    ToolCallSnapshot(
                        call_id=tool_call.call_id,
                        name=tool_call.name,
                        arguments=dict(tool_call.arguments),
                        parse_error=tool_call.parse_error,
                    )
                    for tool_call in tool_calls
                ),
            )

            if not tool_calls:
                yield TurnCompleted(
                    turn=turn,
                    outcome="completed",
                    finish_reason=response.finish_reason,
                )
                break
            if (
                config.max_tool_iterations is not None
                and tool_iterations >= config.max_tool_iterations
            ):
                raise RuntimeError(
                    f"Maximum tool iterations exceeded: {config.max_tool_iterations}"
                )

            tool_iterations += 1
            tool_results: list[ToolCallResult] = []
            for tool_call in tool_calls:
                arguments = dict(tool_call.arguments)
                yield ToolCallRequested(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=arguments,
                )
                yield ToolExecutionStarted(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=arguments,
                )
                result_event: ToolResultReady | None = None
                async for execution_event in _execute_tool_call(config, tool_call):
                    yield execution_event
                    if isinstance(execution_event, ToolResultReady):
                        result_event = execution_event
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
            pending_tool_results = tuple(tool_results)
            yield TurnCompleted(
                turn=turn,
                outcome="completed",
                finish_reason=response.finish_reason,
            )
    except Exception as exc:
        yield ErrorEvent(message=str(exc))
        if turn > 0:
            yield TurnCompleted(turn=turn, outcome="failed", finish_reason="error")
        raise


def __getattr__(name: str) -> Any:
    """Resolve the temporary legacy Agent export without an eager import cycle."""
    if name != "Agent":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    agent = import_module("wisp.agent.compat").Agent
    globals()[name] = agent
    return agent


__all__ = ["Agent", "AgentLoopConfig", "AgentLoopEvent", "run_agent_loop"]
