from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from wisp.agent.execution import ToolExecutionEvent
from wisp.agent.loop import AgentLoopConfig
from wisp.agent.messages import Message
from wisp.agent.provider_turn import (
    CompletedProviderResponse,
    ProviderResponseLifecycle,
    iter_provider_events,
    open_provider_stream,
    project_usage_and_cost,
    resolve_provider_response_id,
)
from wisp.events import UsageCost
from wisp.providers.base import (
    ContextOverflowError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderUsage,
    ToolCall,
)


class _NeverExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class _LegacyProvider:
    name = "legacy"
    default_model = "legacy"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append(
            {
                "messages": tuple(messages),
                "model": model,
                "tools": tuple(tools),
                "tool_results": tuple(tool_results),
                "previous_response_id": previous_response_id,
            }
        )
        yield ProviderResponseStarted(model=model or self.default_model)
        yield ProviderResponseCompleted(content="ok")


class _OverflowStream:
    def __aiter__(self) -> AsyncIterator[ProviderEvent]:
        return self

    async def __anext__(self) -> ProviderEvent:
        raise RuntimeError("maximum context length exceeded")


def _config(*, provider: object | None = None, **kwargs: object) -> AgentLoopConfig:
    return AgentLoopConfig(
        provider=provider or _LegacyProvider(),
        tool_executor=_NeverExecutor(),
        **kwargs,  # type: ignore[arg-type]
    )


def _call(*, call_id: str = "call-1", response_id: str | None = None) -> ToolCall:
    return ToolCall(call_id=call_id, name="noop", arguments={}, response_id=response_id)


def test_resolve_provider_response_id_agrees_across_sources() -> None:
    assert (
        resolve_provider_response_id(
            started_response_id="resp-1",
            terminal_response_id="resp-1",
            tool_calls=(_call(response_id="resp-1"),),
        )
        == "resp-1"
    )
    assert (
        resolve_provider_response_id(
            started_response_id=None,
            terminal_response_id=None,
            tool_calls=(),
        )
        is None
    )


def test_resolve_provider_response_id_rejects_conflicts() -> None:
    with pytest.raises(ProviderProtocolError, match="conflicting response ids"):
        resolve_provider_response_id(
            started_response_id="started",
            terminal_response_id="terminal",
            tool_calls=(),
        )


def test_lifecycle_rejects_inconsistent_finish_reason_and_tool_calls() -> None:
    call = _call()
    empty = ProviderResponseLifecycle()
    empty.start(ProviderResponseStarted(model="test"))
    empty.complete(ProviderResponseCompleted(content="", finish_reason="tool_calls"))
    with pytest.raises(ProviderProtocolError, match="requires at least one tool call"):
        empty.finish()

    with_calls = ProviderResponseLifecycle()
    with_calls.start(ProviderResponseStarted(model="test"))
    with_calls.add_tool_call(call)
    with_calls.complete(
        ProviderResponseCompleted(content="", tool_calls=(call,), finish_reason="stop")
    )
    with pytest.raises(ProviderProtocolError, match="cannot include tool calls"):
        with_calls.finish()


def test_lifecycle_failed_response_does_not_require_start() -> None:
    lifecycle = ProviderResponseLifecycle()
    lifecycle.complete(ProviderResponseFailed(message="upstream failed"))
    failed = lifecycle.finish()
    assert failed.content == ""
    assert failed.response.message == "upstream failed"


def test_open_provider_stream_omits_optional_keywords_for_legacy_providers() -> None:
    provider = _LegacyProvider()
    config = _config(provider=provider, prompt_cache_key="wisp:session-1")
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[ProviderEvent]:
        return [
            event
            async for event in open_provider_stream(
                config,
                messages=messages,
                tool_results=(),
                extra_messages=(),
                previous_response_id=None,
            )
        ]

    events = anyio.run(run)
    assert [type(event) for event in events] == [ProviderResponseStarted, ProviderResponseCompleted]
    assert provider.calls == [
        {
            "messages": messages,
            "model": None,
            "tools": (),
            "tool_results": (),
            "previous_response_id": None,
        }
    ]


def test_project_usage_and_cost_covers_unavailable_reasons() -> None:
    config = _config()
    missing_usage = CompletedProviderResponse(
        response=ProviderResponseCompleted(content="done"),
        content="done",
        response_id=None,
        response_model="resolved",
    )
    _, incomplete = project_usage_and_cost(config, missing_usage)
    assert incomplete.unavailable_reason == "usage_incomplete"

    with_usage = CompletedProviderResponse(
        response=ProviderResponseCompleted(
            content="done",
            usage=ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        ),
        content="done",
        response_id=None,
        response_model="resolved",
    )
    _, unpriced = project_usage_and_cost(config, with_usage)
    assert unpriced.unavailable_reason == "pricing_unavailable"

    def fail_estimate(*args: object) -> UsageCost:
        del args
        raise RuntimeError("pricing lookup failed")

    priced_config = _config(cost_estimator=fail_estimate)
    _, failed = project_usage_and_cost(priced_config, with_usage)
    assert failed.unavailable_reason == "estimation_failed"


def test_iter_provider_events_promotes_overflow_shaped_exceptions() -> None:
    async def run() -> None:
        with pytest.raises(ContextOverflowError, match="maximum context length exceeded"):
            async for _event in iter_provider_events(_OverflowStream()):
                pass

    anyio.run(run)


def test_lifecycle_records_text_but_not_thinking() -> None:
    lifecycle = ProviderResponseLifecycle()
    lifecycle.start(ProviderResponseStarted(model="test"))
    lifecycle.add_text("hello")
    lifecycle.add_thinking("secret")
    lifecycle.complete(ProviderResponseCompleted(content=""))
    completed = lifecycle.finish()
    assert completed.content == "hello"
    assert "".join(lifecycle.text) == "hello"


def test_lifecycle_rejects_data_before_start() -> None:
    lifecycle = ProviderResponseLifecycle()
    with pytest.raises(ProviderProtocolError, match="before response_started"):
        lifecycle.add_text("nope")
