from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

import wisp.coding.tool_execution as tool_execution
from tests.coding.session_support import (
    CapturingProvider,
    EchoTool,
    ToolLoopProvider,
)
from wisp.agent.tool_contracts import ToolResultProcessingError
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.coding.session import CodingSession, _tool_result_status
from wisp.events import (
    AgentCompleted,
    ErrorEvent,
    ManagedProcessState,
    MessageCompleted,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
)
from wisp.providers.base import (
    ToolCall,
    ToolCallResult,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import (
    MessageSessionEntry,
    ToolResultPresentationSnapshot,
)
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import (
    ToolArguments,
    ToolExecutionMetadata,
    ToolInputSchema,
    ToolPromptMetadata,
)
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult


class _RaisingTextResult:
    @property
    def text(self) -> str:
        raise ValueError("could not read tool result text")


class MalformedResultTool:
    name = "malformed"
    safety = "read"
    description = "Returns an invalid result object."
    input_schema: ToolInputSchema = {"type": "object", "properties": {}}

    async def run(self, arguments: ToolArguments, context: ToolContext) -> Any:
        return _RaisingTextResult()


class BlockingTool:
    name = "blocking"
    safety = "read"
    description = "Blocks until released."
    input_schema: ToolInputSchema = {"type": "object", "properties": {}}

    def __init__(
        self,
        *,
        release: anyio.Event,
        log: list[str],
        started: anyio.Event | None = None,
    ) -> None:
        self.release = release
        self.log = log
        self.started = started

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.log.append("run-started")
        if self.started is not None:
            self.started.set()
        await self.release.wait()
        return ToolResult(text="released")


class MutatingTool:
    name = "mutate"
    safety = "mutating"
    description = "Pretend to mutate state."
    input_schema: ToolInputSchema = {"type": "object", "properties": {}}

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text="mutated")


def test_coding_session_operation_tool_context_overrides_tool_root(tmp_path: Path) -> None:
    class CwdTool:
        name = "cwd"
        safety = "read"
        description = "Report the tool working directory."
        input_schema: ToolInputSchema = {"type": "object", "properties": {}}

        async def run(
            self,
            arguments: ToolArguments,
            context: ToolContext,
        ) -> ToolResult:
            return ToolResult(text=str(context.cwd))

    tool_call = ToolCall(
        call_id="call-1",
        name="cwd",
        arguments={},
        response_id="response-1",
    )
    provider = ToolLoopProvider([[tool_call], ["done"]])
    registry = ToolRegistry()
    registry.register(CwdTool())
    launch_directory = tmp_path / "project" / "src"
    project_root = tmp_path / "project"
    launch_directory.mkdir(parents=True)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=launch_directory),
    )

    async def run_agent() -> None:
        events = agent.run(
            "inspect",
            tool_context=ToolContext(cwd=project_root),
            operation_tool_names=frozenset({"cwd"}),
        )
        _ = [event async for event in events]

    anyio.run(run_agent)

    assert provider.calls[1][0] == (ToolCallResult(call_id="call-1", output=str(project_root)),)
    assert agent.tool_context.cwd == launch_directory


def test_coding_session_operation_tool_names_block_other_registry_tools(tmp_path: Path) -> None:
    class HiddenTool:
        name = "hidden"
        safety = "read"
        description = "A tool excluded from this operation."
        input_schema: ToolInputSchema = {"type": "object", "properties": {}}

        async def run(
            self,
            arguments: ToolArguments,
            context: ToolContext,
        ) -> ToolResult:
            return ToolResult(text="unexpected")

    tool_call = ToolCall(call_id="call-1", name="hidden", arguments={})
    provider = ToolLoopProvider([[tool_call], ["done"]])
    registry = ToolRegistry()
    registry.register(HiddenTool())
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
    )

    async def run_agent() -> None:
        _ = [
            event
            async for event in agent.run(
                "inspect",
                operation_tool_names=frozenset(),
            )
        ]

    anyio.run(run_agent)

    assert provider.calls[1][0] == (
        ToolCallResult(
            call_id="call-1",
            output="Tool hidden is blocked by policy",
            is_error=True,
        ),
    )


def test_coding_session_keeps_operation_instructions_out_of_user_prompt(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
    )
    sessions = JsonlSessionStore(tmp_path / "sessions")
    session = sessions.create()
    agent = CodingSession(provider=provider, sessions=sessions)

    async def run_agent() -> None:
        _ = [
            event
            async for event in agent.run(
                "/init",
                session=session,
                operation_instructions="Inspect and initialize the repository.",
            )
        ]

    anyio.run(run_agent)

    provider_messages = provider.calls[0].messages
    assert [(message.role, message.content) for message in provider_messages[-2:]] == [
        ("system", "Inspect and initialize the repository."),
        ("user", "/init"),
    ]
    persisted = session.read_context_messages()
    assert [message.content for message in persisted if message.role == "user"] == ["/init"]


def test_coding_session_executes_tool_calls_and_continues_to_final_response(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [
                "checking ",
                ToolCall(
                    call_id="call-1",
                    name="echo",
                    arguments={"text": "hello"},
                    response_id="response-1",
                ),
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
        agent = CodingSession(
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
        isinstance(event, MessageCompleted) and event.content == "final answer" for event in events
    )
    tool_result = next(event for event in events if isinstance(event, ToolResultReady))
    assert tool_result.output == "echo: hello"
    assert tool_result.is_error is False
    assert emitted_event_types == [
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "tool.call",
        "tool.execution.started",
        "tool.execution.ended",
        "tool.result",
        "turn.completed",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
    ]

    saved = next(event for event in events if isinstance(event, SessionSaved))
    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    message_records = [record for record in records if record["kind"] == "message"]
    event_records = [record for record in records if record["kind"] == "event"]
    assert [record["message"]["role"] for record in message_records] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [record["event"]["payload"]["type"] for record in event_records] == [
        "tool.call",
        "tool.execution.started",
        "tool.execution.ended",
    ]
    assert event_records[1]["event"]["payload"]["call_id"] == "call-1"
    assert event_records[1]["event"]["payload"]["arguments"] == {"text": "hello"}
    assert event_records[2]["event"]["payload"]["output"] == "echo: hello"
    assert event_records[2]["event"]["payload"]["is_error"] is False
    tool_call_message = message_records[4]["message"]
    assert tool_call_message["content"] == "checking "
    assert tool_call_message["response_id"] == "response-1"
    assert tool_call_message["finish_reason"] == "tool_calls"
    assert tool_call_message["tool_calls"] == [
        {
            "call_id": "call-1",
            "name": "echo",
            "arguments": {"text": "hello"},
        }
    ]
    tool_message = message_records[5]["message"]
    assert tool_message["tool_call_id"] == "call-1"
    assert tool_message["tool_name"] == "echo"
    assert tool_message["content"] == "echo: hello"
    assert tool_message["is_error"] is False
    assert message_records[5]["tool_result"] == {
        "status": "done",
        "created": False,
        "output_has_exit_status": False,
        "truncated": False,
    }
    loaded_tool_entry = next(
        entry
        for entry in JsonlSessionStore(tmp_path).load(saved.path).read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "tool"
    )
    assert loaded_tool_entry.tool_result == ToolResultPresentationSnapshot(status="done")
    final_message = message_records[6]["message"]
    assert final_message["content"] == "final answer"
    assert final_message["finish_reason"] == "stop"
    assert final_message["tool_calls"] == []


@pytest.mark.parametrize(
    ("process_state", "expected"),
    [
        ("timed_out", "error"),
        ("failed", "error"),
        ("cancelled", "cancelled"),
        ("completed", "done"),
        ("running", "done"),
    ],
)
def test_tool_result_status_uses_managed_process_state(
    process_state: ManagedProcessState,
    expected: str,
) -> None:
    event = ToolExecutionEnded(
        call_id="call-1",
        name="bash",
        output=f"Process proc-1 {process_state}",
        is_error=False,
        process_state=process_state,
    )

    assert _tool_result_status(event) == expected


def test_coding_session_returns_error_result_when_tool_result_text_raises(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="malformed", arguments={})],
            ["recovered"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MalformedResultTool())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == "Tool returned an invalid result"
    assert result.is_error is True
    assert any(
        isinstance(event, MessageCompleted) and event.content == "recovered" for event in events
    )
    tool_message = next(
        message
        for message in JsonlSessionStore(tmp_path).latest().read_messages()
        if message.role == "tool"
    )
    assert tool_message.is_error is True


def test_coding_session_does_not_turn_internal_result_failure_into_tool_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "internal api-key=secret"

    def fail_summary(
        name: str,
        data: Mapping[str, object],
        *,
        truncated: bool = False,
    ) -> str | None:
        del name, data, truncated
        raise RuntimeError(secret)

    monkeypatch.setattr(tool_execution, "summarize_tool_result", fail_summary)
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="echo", arguments={"text": "hello"})],
            ["must not run"],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())

    async def run_agent() -> tuple[list[object], ToolResultProcessingError]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        events: list[object] = []
        with pytest.raises(ToolResultProcessingError) as raised:
            async for event in agent.run("hello"):
                events.append(event)
        return events, raised.value

    events, error = anyio.run(run_agent)

    assert error.call_id == "call-1"
    assert error.tool_name == "echo"
    assert len(provider.calls) == 1
    assert not any(isinstance(event, ToolExecutionEnded | ToolResultReady) for event in events)
    assert [type(event) for event in events[-3:]] == [ErrorEvent, TurnCompleted, AgentCompleted]
    assert cast(ErrorEvent, events[-3]).message == "Internal error while processing a tool result"
    assert cast(TurnCompleted, events[-2]).outcome == "failed"
    assert cast(AgentCompleted, events[-1]).outcome == "failed"
    messages = JsonlSessionStore(tmp_path).latest().read_messages()
    assert all(secret not in message.content for message in messages)


def test_coding_session_filters_provider_tool_specs_by_policy(tmp_path: Path) -> None:
    provider = CapturingProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    tools.register(MutatingTool())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_read_tools(),
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_tools is not None
    assert [tool.name for tool in provider.seen_tools] == ["echo"]


def test_coding_session_persists_concurrent_batch_results_in_source_order(
    tmp_path: Path,
) -> None:
    calls = (
        ToolCall(call_id="call-1", name="echo", arguments={"text": "one"}),
        ToolCall(call_id="call-2", name="echo", arguments={"text": "two"}),
    )
    provider = ToolLoopProvider([list(calls), ["done"]])
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    tools = ToolRegistry()
    tools.register(
        EchoTool(),
        execution=ToolExecutionMetadata(parallel_safe=True),
    )

    async def run_agent() -> None:
        agent = CodingSession(
            provider=provider,
            sessions=store,
            tool_registry=tools,
        )
        _ = [event async for event in agent.run("hello", session=session)]

    anyio.run(run_agent)

    tool_messages = [message for message in session.read_messages() if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["call-1", "call-2"]
    assert [message.content for message in tool_messages] == ["echo: one", "echo: two"]
    assert [result.call_id for result in provider.calls[1][0]] == [
        "call-1",
        "call-2",
    ]


def test_coding_session_operation_registry_preserves_execution_metadata(tmp_path: Path) -> None:
    tools = ToolRegistry()
    execution = ToolExecutionMetadata(parallel_safe=True)
    tools.register(EchoTool(), execution=execution)
    agent = CodingSession(
        provider=CapturingProvider(),
        sessions=JsonlSessionStore(tmp_path),
        tool_registry=tools,
    )

    operation_registry = agent._operation_tool_registry()  # noqa: SLF001

    assert operation_registry is not None
    assert operation_registry.execution_metadata_for("echo") is execution


def test_coding_session_filters_tool_prompt_metadata_by_policy(tmp_path: Path) -> None:
    provider = CapturingProvider()
    tools = ToolRegistry()
    tools.register(
        EchoTool(),
        prompt=ToolPromptMetadata(prompt_snippet="Visible read guidance."),
    )
    tools.register(
        MutatingTool(),
        prompt=ToolPromptMetadata(prompt_snippet="Blocked mutation guidance."),
    )

    async def run_agent() -> None:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_read_tools(),
        )
        _ = [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    prompt = "\n".join(message.content for message in provider.seen_messages)
    assert "Visible read guidance." in prompt
    assert "Blocked mutation guidance." not in prompt


def test_coding_session_plan_mode_exposes_only_read_tools_and_restores_build_tools(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    tools = ToolRegistry()
    tools.register(EchoTool())
    tools.register(MutatingTool())
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path),
        tool_registry=tools,
    )

    async def run_agent() -> tuple[list[str], list[str], list[str]]:
        agent.set_mode("plan")
        _ = [event async for event in agent.run("plan this")]
        plan_tools = [tool.name for tool in provider.seen_tools or ()]
        plan_messages = [message.content for message in provider.seen_messages or ()]
        agent.set_mode("build")
        _ = [event async for event in agent.run("build this")]
        build_tools = [tool.name for tool in provider.seen_tools or ()]
        return plan_tools, plan_messages, build_tools

    plan_tools, plan_messages, build_tools = anyio.run(run_agent)

    assert plan_tools == ["echo"]
    assert any("You are in plan mode" in message for message in plan_messages)
    assert build_tools == ["echo", "mutate"]


def test_coding_session_plan_mode_blocks_fabricated_mutating_tool_call(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="mutate", arguments={}, response_id="response-1")],
            ["recovered"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())

    async def run_agent() -> None:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            mode="plan",
        )
        _ = [event async for event in agent.run("plan this")]

    anyio.run(run_agent)

    assert provider.calls[1][0] == (
        ToolCallResult(call_id="call-1", output="Tool mutate is blocked by policy", is_error=True),
    )


def test_coding_session_returns_error_result_for_policy_blocked_tool(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="mutate", arguments={}, response_id="response-1")],
            ["recovered"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())

    async def run_agent() -> list[object]:
        agent = CodingSession(
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
        isinstance(event, MessageCompleted) and event.content == "recovered" for event in events
    )


def test_coding_session_blocks_approval_required_tool_without_override(tmp_path: Path) -> None:
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
        agent = CodingSession(
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
    assert [event.type for event in emitted_events[:7]] == [
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.completed",
        "tool.call",
        "tool.execution.started",
    ]
    assert any(
        isinstance(event, MessageCompleted) and event.content == "recovered" for event in events
    )
    saved = next(event for event in events if isinstance(event, SessionSaved))
    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    event_records = [record for record in records if record["kind"] == "event"]
    assert [record["event"]["payload"]["type"] for record in event_records] == [
        "tool.call",
        "tool.execution.started",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
    ]
    assert event_records[3]["event"]["payload"]["approved"] is False
    assert "requires approval" in event_records[3]["event"]["payload"]["reason"]
    tool_entry = next(
        entry
        for entry in JsonlSessionStore(tmp_path).load(saved.path).read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "tool"
    )
    assert tool_entry.tool_result is not None
    assert tool_entry.tool_result.status == "denied"


def test_coding_session_approves_required_tool_with_override(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="mutate", arguments={}, response_id="response-1")],
            ["done"],
        ]
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_policy=ToolPolicy.allow_tool_names({"mutate"}),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1][0] == (ToolCallResult(call_id="call-1", output="mutated"),)
    assert not any(
        isinstance(event, ToolApprovalRequested | ToolApprovalResolved) for event in events
    )


def test_coding_session_updates_previous_response_id_for_chained_tool_calls(tmp_path: Path) -> None:
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
        agent = CodingSession(
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


def test_coding_session_falls_back_to_tool_call_response_id(tmp_path: Path) -> None:
    tool_call = ToolCall(
        call_id="call-1",
        name="echo",
        arguments={"text": "first"},
        response_id="response-1",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1].previous_response_id == "response-1"
    first_completion = next(event for event in events if isinstance(event, MessageCompleted))
    assert first_completion.response_id == "response-1"


def test_coding_session_yields_tool_lifecycle_before_tool_runs(tmp_path: Path) -> None:
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
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        events = agent.run("hello")

        first_event = await anext(events)
        assert first_event.type == "agent.started"
        assert (await anext(events)).type == "turn.started"
        assert (await anext(events)).type == "context.estimated"
        assert (await anext(events)).type == "message.started"
        assert (await anext(events)).type == "message.completed"
        call_event = await anext(events)
        start_event = await anext(events)

        assert isinstance(call_event, ToolCallRequested)
        assert isinstance(start_event, ToolExecutionStarted)
        assert log == []

        release.set()
        remaining_events = [event async for event in events]
        assert log == ["run-started"]
        assert any(isinstance(event, MessageCompleted) for event in remaining_events)

    anyio.run(run_agent)


def test_coding_session_cancellation_during_tool_keeps_completed_assistant(
    tmp_path: Path,
) -> None:
    provider = ToolLoopProvider(
        [
            [
                ToolCall(
                    call_id="call-1",
                    name="blocking",
                    arguments={},
                    response_id="response-1",
                )
            ],
            ["unused"],
        ]
    )
    session = JsonlSessionStore(tmp_path).create()

    async def run_agent() -> None:
        release = anyio.Event()
        started = anyio.Event()
        tools = ToolRegistry()
        tools.register(BlockingTool(release=release, log=[], started=started))
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        events = agent.run("hello", session=session)
        while True:
            event = await anext(events)
            if isinstance(event, ToolExecutionStarted):
                break

        scope = anyio.CancelScope()

        async def wait_for_tool_result() -> None:
            with scope:
                await anext(events)

        with anyio.fail_after(1):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(wait_for_tool_result)
                await started.wait()
                scope.cancel()
        await events.aclose()

    anyio.run(run_agent)

    completed_messages = [
        message for message in session.read_messages() if message.role in {"assistant", "tool"}
    ]
    assert [message.role for message in completed_messages] == ["assistant", "tool"]
    assert completed_messages[0].tool_calls is not None
    assert [call.call_id for call in completed_messages[0].tool_calls] == ["call-1"]
    assert completed_messages[1].tool_call_id == "call-1"
    assert completed_messages[1].tool_name == "blocking"
    assert completed_messages[1].content == INTERRUPTED_TOOL_RESULT_TEXT
    assert completed_messages[1].is_error is True

    resumed_provider = CapturingProvider()

    async def resume_agent() -> None:
        agent = CodingSession(
            provider=resumed_provider,
            sessions=JsonlSessionStore(tmp_path),
        )
        _events = [
            event
            async for event in agent.run(
                "what happened?",
                session=session,
                history=session.read_messages(),
            )
        ]

    anyio.run(resume_agent)

    assert resumed_provider.seen_messages is not None
    repaired_history = next(
        message
        for message in resumed_provider.seen_messages
        if message.role == "assistant" and INTERRUPTED_TOOL_RESULT_TEXT in message.content
    )
    assert json.loads(repaired_history.content)["calls"][0]["result"]["is_error"] is True
    assert (
        len(
            [
                message
                for message in session.read_messages()
                if message.role == "tool" and message.tool_call_id == "call-1"
            ]
        )
        == 1
    )


def test_coding_session_returns_error_result_for_unknown_tool(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="missing", arguments={}, response_id="response-1")],
            ["recovered"],
        ]
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(
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
        isinstance(event, MessageCompleted) and event.content == "recovered" for event in events
    )


def test_coding_session_defaults_to_uncapped_tool_iterations(tmp_path: Path) -> None:
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
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert len(provider.calls) == 11
    assert any(isinstance(event, MessageCompleted) and event.content == "done" for event in events)


def test_coding_session_enforces_configured_max_tool_iterations(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        [
            [ToolCall(call_id="call-1", name="echo", arguments={"text": "hello"})],
            [ToolCall(call_id="call-2", name="echo", arguments={"text": "again"})],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())

    async def run_agent() -> list[object]:
        agent = CodingSession(
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


def test_coding_session_returns_error_result_for_invalid_tool_arguments(tmp_path: Path) -> None:
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
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.calls[1][0] == (
        ToolCallResult(
            call_id="call-1",
            output=(
                "Invalid JSON arguments for tool echo: Expecting value\n"
                "Recovery: Retry with arguments that match the tool's input schema."
            ),
            is_error=True,
        ),
    )
    assert any(
        isinstance(event, MessageCompleted) and event.content == "recovered" for event in events
    )
