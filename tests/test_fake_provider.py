from __future__ import annotations

from dataclasses import FrozenInstanceError

import anyio
import pytest

from wisp.agent.messages import Message
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.fake import ProviderRequest, ScriptedProvider


def test_scripted_provider_replays_events_and_records_request() -> None:
    scripted_events = [
        ProviderResponseStarted(model="test"),
        ProviderTextDelta(delta="done"),
        ProviderResponseCompleted(content="done"),
    ]
    provider = ScriptedProvider([scripted_events])
    messages = [Message(role="user", content="hello")]
    tools = [
        ToolSpec(
            name="read",
            description="Read a file.",
            input_schema={"type": "object", "properties": {}},
        )
    ]
    tool_results = [ToolCallResult(call_id="call-1", output="result")]
    extra_messages = [Message(role="user", content="steered")]

    async def consume() -> list[object]:
        return [
            event
            async for event in provider.stream(
                messages,
                model="model-1",
                tools=tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id="response-1",
            )
        ]

    events = anyio.run(consume)

    assert events == scripted_events
    assert provider.calls == [
        ProviderRequest(
            messages=tuple(messages),
            model="model-1",
            tools=tuple(tools),
            tool_results=tuple(tool_results),
            previous_response_id="response-1",
            extra_messages=tuple(extra_messages),
        )
    ]
    with pytest.raises(FrozenInstanceError):
        provider.calls[0].model = "changed"  # type: ignore[misc]


def test_scripted_provider_raises_scripted_exception() -> None:
    provider = ScriptedProvider([[RuntimeError("script failed")]])

    async def consume() -> None:
        with pytest.raises(RuntimeError, match="script failed"):
            async for _event in provider.stream([]):
                pass

    anyio.run(consume)


def test_scripted_provider_rejects_exhausted_scripts() -> None:
    provider = ScriptedProvider([[]])

    async def consume() -> None:
        assert [event async for event in provider.stream([])] == []
        with pytest.raises(RuntimeError, match="no response stream remaining"):
            async for _event in provider.stream([]):
                pass

    anyio.run(consume)
