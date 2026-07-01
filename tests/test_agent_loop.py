from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio

from wisp.agent.loop import Agent
from wisp.agent.messages import Message
from wisp.events import AssistantMessage, SessionSaved, TokenDelta, ToolResultReady
from wisp.providers.base import ToolCall, ToolCallResult, ToolSpec
from wisp.providers.fake import FakeProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


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
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[object]:
        self.seen_tools = tools
        yield "done"


class ToolLoopProvider:
    name = "tool-loop"
    default_model: str | None = "default"

    def __init__(self, turns: Sequence[Sequence[object]]) -> None:
        self.turns = list(turns)
        self.calls: list[tuple[Sequence[ToolCallResult], str | None]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[object]:
        self.calls.append((tool_results, previous_response_id))
        turn = self.turns.pop(0)
        for event in turn:
            yield event


class EchoTool:
    name = "echo"
    description = "Echo input text."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text=f"echo: {arguments['text']}")


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


def test_agent_executes_tool_calls_and_continues_to_final_response(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [
                ToolCall(
                    call_id="call-1",
                    name="echo",
                    arguments={"text": "hello"},
                    response_id="response-1",
                )
            ],
            ["final answer"],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    emitted_event_types: list[str] = []

    async def run_agent() -> list[object]:
        event_bus = EventBus()
        event_bus.on("*", lambda event: emitted_event_types.append(event.type))
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            events=event_bus,
            tool_registry=tools,
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1] == (
        (ToolCallResult(call_id="call-1", output="echo: hello"),),
        "response-1",
    )
    assert any(
        isinstance(event, AssistantMessage) and event.content == "final answer" for event in events
    )
    tool_result = next(event for event in events if isinstance(event, ToolResultReady))
    assert tool_result.output == "echo: hello"
    assert tool_result.is_error is False
    assert emitted_event_types == [
        "agent.started",
        "tool.call",
        "tool.execution.started",
        "tool.execution.ended",
        "tool.result",
        "token.delta",
        "assistant.message",
        "session.saved",
    ]

    saved = next(event for event in events if isinstance(event, SessionSaved))
    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == ["user", "tool", "assistant"]
    assert records[1]["message"]["tool_call_id"] == "call-1"
    assert records[1]["message"]["tool_name"] == "echo"
    assert records[1]["message"]["content"] == "echo: hello"


def test_agent_returns_error_result_for_unknown_tool(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="missing", arguments={}, response_id="response-1")],
            ["recovered"],
        ]
    )

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=ToolRegistry(),
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1][0] == (
        ToolCallResult(call_id="call-1", output="Unknown tool: missing", is_error=True),
    )
    assert any(
        isinstance(event, AssistantMessage) and event.content == "recovered" for event in events
    )


def test_agent_enforces_max_tool_iterations(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="echo", arguments={"text": "hello"})],
            [ToolCall(call_id="call-2", name="echo", arguments={"text": "again"})],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            max_tool_iterations=1,
        )
        return [event async for event in agent.run("hello")]

    try:
        anyio.run(run_agent)
    except RuntimeError as exc:
        assert str(exc) == "Maximum tool iterations exceeded: 1"
    else:
        raise AssertionError("Expected max tool iteration guard to raise")


def test_agent_returns_error_result_for_invalid_tool_arguments(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [
                ToolCall(
                    call_id="call-1",
                    name="echo",
                    arguments={},
                    parse_error="Invalid JSON arguments for tool echo: Expecting value",
                    response_id="response-1",
                )
            ],
            ["recovered"],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1][0] == (
        ToolCallResult(
            call_id="call-1",
            output="Invalid JSON arguments for tool echo: Expecting value",
            is_error=True,
        ),
    )
    assert any(
        isinstance(event, AssistantMessage) and event.content == "recovered" for event in events
    )
