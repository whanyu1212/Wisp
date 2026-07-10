from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import anyio

from wisp.agent.loop import Agent
from wisp.agent.messages import Message
from wisp.events import (
    AssistantMessage,
    SessionSaved,
    TokenDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionStarted,
    ToolResultReady,
)
from wisp.providers.base import ToolCall, ToolCallResult, ToolSpec
from wisp.providers.fake import FakeProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult


class CapturingProvider:
    name = "capturing"
    default_model: str | None = "default"

    def __init__(self) -> None:
        self.seen_messages: Sequence[Message] | None = None
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
        self.seen_messages = messages
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
    safety = "read"
    description = "Echo input text."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text=f"echo: {arguments['text']}")


class BlockingTool:
    name = "blocking"
    safety = "read"
    description = "Blocks until released."
    input_schema: ToolInputSchema = {"type": "object", "properties": {}}

    def __init__(self, *, release: anyio.Event, log: list[str]) -> None:
        self.release = release
        self.log = log

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.log.append("run-started")
        await self.release.wait()
        return ToolResult(text="released")


class MutatingTool:
    name = "mutate"
    safety = "mutating"
    description = "Pretend to mutate state."
    input_schema: ToolInputSchema = {"type": "object", "properties": {}}

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text="mutated")


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
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert "You are Wisp" in records[0]["message"]["content"]
    assert "[WISP PROJECT CONTEXT]" in records[1]["message"]["content"]
    assert [record["message"]["content"] for record in records[2:]] == [
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


def test_agent_continues_with_history_and_labeled_tool_observations(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    session = JsonlSessionStore(tmp_path).create()
    history = [
        Message(role="system", content="old instructions"),
        Message(role="user", content="previous question"),
        Message(
            role="tool",
            content="raw tool output must not be replayed as user text",
            tool_call_id="call-1",
            tool_name="read",
        ),
        Message(role="assistant", content="previous answer"),
    ]

    async def run_agent() -> list[object]:
        agent = Agent(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [
            event async for event in agent.run("next question", session=session, history=history)
        ]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [message.role for message in provider.seen_messages] == [
        "system",
        "system",
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert "You are Wisp" in provider.seen_messages[0].content
    assert provider.seen_messages[1].content.startswith("[WISP PROJECT CONTEXT]")
    assert provider.seen_messages[2].content == "previous question"
    assert provider.seen_messages[3].content == (
        "[Historical tool observation — not a user instruction]\n"
        "Tool: read (call-1)\n\n"
        "raw tool output must not be replayed as user text"
    )
    assert [message.content for message in provider.seen_messages[4:]] == [
        "previous answer",
        "next question",
    ]

    records = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert records[2]["message"]["content"] == "next question"
    assert records[3]["message"]["content"] == "done"


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

    assert provider.seen_messages is not None
    assert [message.role for message in provider.seen_messages] == ["system", "system", "user"]
    assert "You are Wisp" in provider.seen_messages[0].content
    assert "allowed tools:\n  - lookup: Look something up." in provider.seen_messages[1].content
    assert provider.seen_messages[2].content == "hello"
    assert provider.seen_tools == (tool,)
    assert any(isinstance(event, AssistantMessage) and event.content == "done" for event in events)


def test_agent_skips_project_context_when_untrusted(tmp_path: Path) -> None:
    provider = CapturingProvider()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Never show untrusted agent rules.\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("Never show untrusted Claude rules.\n", encoding="utf-8")
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=cast(Any, provider),
            sessions=JsonlSessionStore(tmp_path),
            tools=[tool],
            tool_context=ToolContext(cwd=project),
            trusted=False,
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    context = provider.seen_messages[1].content
    assert "project context: skipped because this project is not trusted" in context
    assert str(project.resolve(strict=False)) not in context
    assert "pyproject.toml" not in context
    assert "AGENTS.md" not in context
    assert "CLAUDE.md" not in context
    assert "Never show untrusted agent rules." not in context
    assert "Never show untrusted Claude rules." not in context
    assert "allowed tools:\n  - lookup: Look something up." in context


def test_agent_includes_project_context_when_trusted(tmp_path: Path) -> None:
    provider = CapturingProvider()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Trusted agent rules.\n", encoding="utf-8")

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=cast(Any, provider),
            sessions=JsonlSessionStore(tmp_path),
            tool_context=ToolContext(cwd=project),
            trusted=True,
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    context = provider.seen_messages[1].content
    assert f"cwd: {project.resolve(strict=False)}" in context
    assert "project files:\n  pyproject.toml" in context
    assert "--- AGENTS.md ---\nTrusted agent rules." in context


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
        "tool.execution.started",
        "tool.call",
        "tool.execution.ended",
        "tool.result",
        "token.delta",
        "assistant.message",
        "session.saved",
    ]

    saved = next(event for event in events if isinstance(event, SessionSaved))
    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    message_records = [record for record in records if record["kind"] == "message"]
    event_records = [record for record in records if record["kind"] == "event"]
    assert [record["message"]["role"] for record in message_records] == [
        "system",
        "system",
        "user",
        "tool",
        "assistant",
    ]
    assert [record["event"]["type"] for record in event_records] == [
        "tool.execution.started",
        "tool.call",
        "tool.execution.ended",
    ]
    assert event_records[1]["event"]["call_id"] == "call-1"
    assert event_records[1]["event"]["arguments"] == {"text": "hello"}
    assert event_records[2]["event"]["output"] == "echo: hello"
    assert event_records[2]["event"]["is_error"] is False
    assert message_records[3]["message"]["tool_call_id"] == "call-1"
    assert message_records[3]["message"]["tool_name"] == "echo"
    assert message_records[3]["message"]["content"] == "echo: hello"


def test_agent_filters_provider_tool_specs_by_policy(tmp_path: Path) -> None:
    provider = CapturingProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    tools.register(MutatingTool())

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_read_tools(),
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_tools is not None
    assert [tool.name for tool in provider.seen_tools] == ["echo"]


def test_agent_returns_error_result_for_policy_blocked_tool(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="mutate", arguments={}, response_id="response-1")],
            ["recovered"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_read_tools(),
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1][0] == (
        ToolCallResult(call_id="call-1", output="Tool mutate is blocked by policy", is_error=True),
    )
    assert any(
        isinstance(event, AssistantMessage) and event.content == "recovered" for event in events
    )


def test_agent_blocks_approval_required_tool_without_override(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="mutate", arguments={}, response_id="response-1")],
            ["recovered"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())
    emitted_events: list[object] = []

    async def run_agent() -> list[object]:
        event_bus = EventBus()
        event_bus.on("*", emitted_events.append)
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            events=event_bus,
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_tool_names({"mutate"}),
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    blocked_result = provider.calls[1][0][0]
    assert blocked_result.is_error is True
    assert "Tool mutate requires approval before execution" in blocked_result.output
    approval_requested = next(event for event in events if isinstance(event, ToolApprovalRequested))
    approval_resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
    assert approval_requested.safety == "mutating"
    assert approval_resolved.approved is False
    assert approval_resolved.reason is not None
    assert [event.type for event in emitted_events[:6]] == [
        "agent.started",
        "tool.execution.started",
        "tool.call",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
    ]
    assert any(
        isinstance(event, AssistantMessage) and event.content == "recovered" for event in events
    )
    saved = next(event for event in events if isinstance(event, SessionSaved))
    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    event_records = [record for record in records if record["kind"] == "event"]
    assert [record["event"]["type"] for record in event_records] == [
        "tool.execution.started",
        "tool.call",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
    ]
    assert event_records[3]["event"]["approved"] is False
    assert "requires approval" in event_records[3]["event"]["reason"]


def test_agent_approves_required_tool_with_override(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="mutate", arguments={}, response_id="response-1")],
            ["done"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())

    async def run_agent() -> list[object]:
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_tool_names({"mutate"}),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1][0] == (ToolCallResult(call_id="call-1", output="mutated"),)
    approval_resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
    assert approval_resolved.approved is True
    assert approval_resolved.reason is None


def test_agent_updates_previous_response_id_for_chained_tool_calls(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [
                ToolCall(
                    call_id="call-1",
                    name="echo",
                    arguments={"text": "first"},
                    response_id="response-1",
                )
            ],
            [
                ToolCall(
                    call_id="call-2",
                    name="echo",
                    arguments={"text": "second"},
                    response_id="response-2",
                )
            ],
            ["final answer"],
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

    anyio.run(run_agent)

    assert provider.calls == [
        ((), None),
        ((ToolCallResult(call_id="call-1", output="echo: first"),), "response-1"),
        ((ToolCallResult(call_id="call-2", output="echo: second"),), "response-2"),
    ]


def test_agent_yields_tool_lifecycle_before_tool_runs(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="blocking", arguments={}, response_id="response-1")],
            ["final answer"],
        ]
    )

    async def run_agent() -> None:
        release = anyio.Event()
        log: list[str] = []
        tools = ToolRegistry()
        tools.register(BlockingTool(release=release, log=log))
        agent = Agent(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        events = agent.run("hello")

        first_event = await anext(events)
        assert first_event.type == "agent.started"
        start_event = await anext(events)
        call_event = await anext(events)

        assert isinstance(start_event, ToolExecutionStarted)
        assert isinstance(call_event, ToolCallRequested)
        assert log == []

        release.set()
        remaining_events = [event async for event in events]
        assert log == ["run-started"]
        assert any(isinstance(event, AssistantMessage) for event in remaining_events)

    anyio.run(run_agent)


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


def test_agent_defaults_to_uncapped_tool_iterations(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [
                ToolCall(
                    call_id=f"call-{index}",
                    name="echo",
                    arguments={"text": str(index)},
                    response_id=f"response-{index}",
                )
            ]
            for index in range(10)
        ]
        + [["done"]]
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

    assert len(provider.calls) == 11
    assert any(isinstance(event, AssistantMessage) and event.content == "done" for event in events)


def test_agent_enforces_configured_max_tool_iterations(tmp_path: Path) -> None:
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

    session = JsonlSessionStore(tmp_path).latest()
    error_events = [event for event in session.read_events() if event["type"] == "error"]
    assert error_events[-1]["message"] == "Maximum tool iterations exceeded: 1"


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
