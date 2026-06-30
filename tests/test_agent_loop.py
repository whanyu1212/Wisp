from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio

from wisp.agent.loop import Agent
from wisp.agent.messages import Message
from wisp.events import AssistantMessage, SessionSaved, TokenDelta
from wisp.providers.base import ToolSpec
from wisp.providers.fake import FakeProvider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.jsonl import JsonlSessionStore


class CapturingProvider:
    name = "capturing"
    default_model: str | None = "default"

    def __init__(self) -> None:
        self.seen_tools: Sequence[ToolSpec] | None = None

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[str]:
        self.seen_tools = tools
        yield "done"


def test_agent_streams_fake_response_and_saves_session(tmp_path: Path) -> None:
    emitted_event_types: list[str] = []

    async def run_agent() -> list[object]:
        event_bus = EventBus()
        event_bus.on("*", lambda event: emitted_event_types.append(event.type))
        agent = Agent(
            provider=FakeProvider(), sessions=JsonlSessionStore(tmp_path), events=event_bus
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)
    deltas = [event.delta for event in events if isinstance(event, TokenDelta)]

    assert "".join(deltas) == "fake response to: hello"
    assert any(
        isinstance(event, AssistantMessage) and event.content == "fake response to: hello"
        for event in events
    )

    saved = next(event for event in events if isinstance(event, SessionSaved))
    assert saved.path.exists()

    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == ["user", "assistant"]
    assert [record["message"]["content"] for record in records] == [
        "hello",
        "fake response to: hello",
    ]
    assert emitted_event_types == [
        "agent.started",
        "token.delta",
        "token.delta",
        "token.delta",
        "token.delta",
        "assistant.message",
        "session.saved",
    ]


def test_agent_passes_tool_specs_to_provider(tmp_path: Path) -> None:
    provider = CapturingProvider()
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tools=[tool],
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.seen_tools == (tool,)
    assert any(isinstance(event, AssistantMessage) and event.content == "done" for event in events)
