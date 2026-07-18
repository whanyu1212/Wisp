from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import pytest

from wisp.agent.execution import ToolExecutionEvent
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.coding.session import PERSISTED_SESSION_EVENT_TYPES
from wisp.events import ContextOverflow, ContextPressure, ErrorEvent, wisp_event_from_json
from wisp.providers.base import ContextOverflowError
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


class NeverToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover


class OverflowingProvider:
    name = "test"
    default_model: str | None = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: object,
        *,
        model: str | None = None,
        tools: object = (),
        tool_results: object = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[object]:
        self.calls += 1
        raise ContextOverflowError("maximum context length exceeded")
        yield  # pragma: no cover


def _run_loop(config: AgentLoopConfig) -> list[object]:
    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                config,
                messages=(Message(role="user", content="hello"),),
            )
        ]

    return anyio.run(run)


def test_context_pressure_emits_after_completed_message_at_threshold() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test-model"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(
                        input_tokens=75,
                        output_tokens=5,
                        total_tokens=80,
                    ),
                ),
            ]
        ]
    )

    events = _run_loop(
        AgentLoopConfig(
            provider=provider,
            tool_executor=NeverToolExecutor(),
            model="test-model",
            context_window=100,
        )
    )

    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "message.completed",
        "context.pressure",
        "turn.completed",
    ]
    pressure = next(event for event in events if isinstance(event, ContextPressure))
    assert pressure.provider == "scripted"
    assert pressure.model == "test-model"
    assert pressure.context_window == 100
    assert pressure.observed_tokens == 80
    assert pressure.remaining_tokens == 20
    assert pressure.pressure_ratio == 0.8


@pytest.mark.parametrize(
    ("context_window", "total_tokens"),
    [
        (None, 90),
        (100, 79),
    ],
)
def test_context_pressure_is_not_emitted_without_a_known_crossed_limit(
    context_window: int | None,
    total_tokens: int,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test-model"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(
                        input_tokens=total_tokens,
                        output_tokens=0,
                        total_tokens=total_tokens,
                    ),
                ),
            ]
        ]
    )

    events = _run_loop(
        AgentLoopConfig(
            provider=provider,
            tool_executor=NeverToolExecutor(),
            context_window=context_window,
        )
    )

    assert not any(isinstance(event, ContextPressure) for event in events)


def test_terminal_context_overflow_emits_structured_event_and_does_not_retry() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test-model"),
                ProviderResponseFailed(message="context_length_exceeded"),
            ],
            [
                ProviderResponseStarted(model="test-model"),
                ProviderResponseCompleted(content="unexpected retry"),
            ],
        ]
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ContextOverflowError):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    model="test-model",
                    context_window=100,
                ),
                messages=(Message(role="user", content="hello"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "context.overflow",
        "error",
        "turn.completed",
    ]
    assert len(provider.calls) == 1


def test_raised_context_overflow_emits_structured_event_before_error() -> None:
    provider = OverflowingProvider()

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ContextOverflowError):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    context_window=100,
                ),
                messages=(Message(role="user", content="hello"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "context.overflow",
        "error",
        "turn.completed",
    ]
    overflow = next(event for event in events if isinstance(event, ContextOverflow))
    assert overflow.provider == "test"
    assert overflow.model == "test-model"
    assert overflow.context_window == 100
    assert isinstance(events[2], ErrorEvent)
    assert provider.calls == 1


def test_context_events_round_trip_on_schema_v7() -> None:
    pressure = ContextPressure(
        turn=1,
        provider="test",
        model="test-model",
        context_window=100,
        observed_tokens=80,
        remaining_tokens=20,
        pressure_ratio=0.8,
    )
    overflow = ContextOverflow(
        turn=2,
        provider="test",
        model="test-model",
        context_window=100,
        message="maximum context length exceeded",
    )

    assert pressure.schema_version == 7
    assert overflow.schema_version == 7
    assert wisp_event_from_json(pressure.model_dump_json()) == pressure
    assert wisp_event_from_json(overflow.model_dump_json()) == overflow


def test_model_registry_resolves_effective_context_window() -> None:
    registry = ModelRegistry(
        ModelCatalog(
            schema_version=1,
            providers=(
                ModelCatalogProviderEntry(
                    name="test",
                    display_name="Test",
                    default_model="default",
                    docs_url="https://example.com",
                    models=("default", "other"),
                    context_windows={"default": 100, "other": 200},
                ),
            ),
        )
    )

    assert registry.context_window("test", None, default_model="default") == 100
    assert registry.context_window("test", "other", default_model="default") == 200
    assert registry.context_window("test", "unknown", default_model="default") is None
    assert registry.context_window("unknown", "default") is None


def test_context_events_are_registered_for_session_persistence() -> None:
    assert "context.pressure" in PERSISTED_SESSION_EVENT_TYPES
    assert "context.overflow" in PERSISTED_SESSION_EVENT_TYPES
