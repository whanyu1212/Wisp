from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import cast

import anyio
import pytest

import wisp.agent.harness.runner as harness_module
from tests.coding.session_support import (
    CapturingProvider,
    EchoTool,
    ToolLoopProvider,
)
from wisp.agent.harness import AgentHarness
from wisp.agent.loop import AgentLoopConfig, AgentLoopEvent
from wisp.agent.messages import Message
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    QueueMessageInjected,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)
from wisp.providers.base import (
    Provider,
    ProviderProtocolError,
    ToolCall,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import (
    MessageSessionEntry,
    SessionEntry,
    ToolResultPresentationSnapshot,
)
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.base import (
    ToolArguments,
)
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


@pytest.mark.parametrize("phase", ["startup", "cleanup"])
def test_harness_failure_releases_state_and_retains_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    def fail_preparation(
        messages: Sequence[Message],
        *,
        provider: Provider,
        effort: str | None,
        active_from: int | None = None,
    ) -> tuple[Message, ...]:
        raise RuntimeError("startup failed")

    async def fail_cleanup(
        config: AgentLoopConfig, *, messages: Sequence[Message]
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        try:
            yield TurnStarted(turn=1)
        finally:
            raise RuntimeError("cleanup failed")

    async def run() -> None:
        provider = ScriptedProvider(
            [
                [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content=answer)]
                for answer in ("retried", "followed up")
            ]
        )
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        bus = EventBus()
        agent = CodingSession(provider=provider, sessions=store, events=bus)
        captured: list[AgentHarness] = []

        async def queue_once(event: WispEvent) -> None:
            if not captured:
                assert agent._active_harness is not None
                captured.append(agent._active_harness)
                await agent.follow_up("pending follow-up")

        bus.on("agent.started", queue_once)
        observed: list[WispEvent] = []
        with monkeypatch.context() as patch:
            if phase == "startup":
                patch.setattr(harness_module, "prepare_provider_history", fail_preparation)
            else:
                patch.setattr(harness_module, "run_agent_loop", fail_cleanup)
            with pytest.raises(RuntimeError, match=f"{phase} failed"):
                events = agent.run("first", session=session)
                async for event in events:
                    observed.append(event)
                    if phase == "cleanup" and isinstance(event, TurnStarted):
                        await events.aclose()
            assert not captured[0].is_running
            assert not captured[0].cancel()
            assert agent._active_harness is None
            assert not agent._operation_active
        assert provider.calls == []
        assert not any(isinstance(event, QueueMessageInjected) for event in observed)
        assert [
            message.content for message in session.read_messages() if message.role == "user"
        ] == ["first"]
        if phase == "startup":
            assert [event.message for event in observed if isinstance(event, ErrorEvent)] == [
                "startup failed"
            ]
            assert not any(isinstance(event, TurnCompleted) for event in observed)

        with anyio.fail_after(2):
            retried = [
                event
                async for event in agent.run(
                    "retry", session=session, history=session.read_context_messages()
                )
            ]
        assert [event.content for event in retried if isinstance(event, QueueMessageInjected)] == [
            "pending follow-up"
        ]
        assert [
            message.content for message in session.read_messages() if message.role == "user"
        ] == ["first", "retry", "pending follow-up"]
        assert len(provider.calls) == 2
        completed = [event for event in retried if isinstance(event, AgentCompleted)]
        assert len(completed) == 1 and completed[0].outcome == "completed"

    anyio.run(run)


def test_persists_completion_before_exposing_it(
    tmp_path: Path,
) -> None:
    tool_call = ToolCall(
        call_id="call-1",
        name="echo",
        arguments={"text": "hello"},
        response_id="response-1",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(tool_call,),
                    response_id="response-1",
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    session = JsonlSessionStore(tmp_path).create()

    async def run_agent() -> MessageCompleted:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        events = agent.run("hello", session=session)
        while True:
            event = await anext(events)
            if isinstance(event, MessageCompleted):
                persisted = session.read_messages()[-1]
                assert event.message_entry_id == next(
                    entry.id
                    for entry in session.read_entries()
                    if isinstance(entry, MessageSessionEntry) and entry.message == persisted
                )
                assert persisted.content == "checking"
                assert persisted.response_id == "response-1"
                assert persisted.finish_reason == "tool_calls"
                assert persisted.tool_calls is not None
                assert [call.call_id for call in persisted.tool_calls] == ["call-1"]
                assert persisted.created_at == event.timestamp
                assert event.tool_calls is not None
                event.tool_calls[0].arguments["text"] = "changed by observer"
                assert agent._active_harness is not None
                retained = agent._active_harness.messages[-1]
                assert retained.tool_calls is not None
                assert retained.tool_calls[0].arguments == {"text": "hello"}
                await events.aclose()
                return event

    completion = anyio.run(run_agent)

    assert completion.content == "checking"
    assert [message.role for message in session.read_messages()] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "tool",
    ]
    repair = session.read_messages()[-1]
    completed = session.read_messages()[-2]
    assert completed.tool_calls is not None
    assert completed.tool_calls[0].arguments == {"text": "hello"}
    assert repair.tool_call_id == "call-1"
    assert repair.content == INTERRUPTED_TOOL_RESULT_TEXT
    assert repair.is_error is True
    repair_entry = next(
        entry
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry)
        and entry.message.tool_call_id == "call-1"
        and entry.message.content == INTERRUPTED_TOOL_RESULT_TEXT
    )
    assert repair_entry.tool_result == ToolResultPresentationSnapshot(status="cancelled")


def test_does_not_persist_partial_assistant_on_generator_close(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="partial"),
                ProviderResponseCompleted(content="partial"),
            ]
        ]
    )
    session = JsonlSessionStore(tmp_path).create()

    async def run_agent() -> None:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        events = agent.run("hello", session=session)
        while True:
            event = await anext(events)
            if isinstance(event, MessageDelta):
                await events.aclose()
                return

    anyio.run(run_agent)

    assert not any(message.role == "assistant" for message in session.read_messages())


def test_persists_tool_output_before_exposing_execution_end(
    tmp_path: Path,
) -> None:
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
            ["unused"],
        ]
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    session = JsonlSessionStore(tmp_path).create()

    async def run_agent() -> ToolExecutionEnded:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        events = agent.run("echo it", session=session)
        while True:
            event = await anext(events)
            if isinstance(event, ToolExecutionEnded):
                persisted = session.read_messages()[-1]
                assert event.message_entry_id == next(
                    entry.id
                    for entry in session.read_entries()
                    if isinstance(entry, MessageSessionEntry) and entry.message == persisted
                )
                assert persisted.role == "tool"
                assert persisted.tool_call_id == "call-1"
                assert persisted.content == "echo: hello"
                assert persisted.is_error is False
                await events.aclose()
                return event

    terminal = anyio.run(run_agent)

    assert terminal.output == "echo: hello"
    tool_messages = [
        message
        for message in session.read_messages()
        if message.role == "tool" and message.tool_call_id == "call-1"
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == "echo: hello"
    assert tool_messages[0].content != INTERRUPTED_TOOL_RESULT_TEXT


def test_persists_truncated_tool_errors_without_running_tools(
    tmp_path: Path,
) -> None:
    calls = (
        ToolCall(
            call_id="call-1",
            name="echo",
            arguments={"text": "one"},
            response_id="truncated-response",
        ),
        ToolCall(
            call_id="call-2",
            name="echo",
            arguments={"text": "two"},
            response_id="truncated-response",
        ),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="truncated-response"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=calls,
                    response_id="truncated-response",
                    finish_reason="length",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="recovered-response"),
                ProviderResponseCompleted(
                    content="recovered",
                    response_id="recovered-response",
                ),
            ],
        ]
    )
    executions: list[ToolArguments] = []

    class RecordingEchoTool(EchoTool):
        async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
            executions.append(arguments)
            return await super().run(arguments, context)

    tools = ToolRegistry()
    tools.register(RecordingEchoTool())
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run_agent() -> list[WispEvent]:
        agent = CodingSession(provider=provider, sessions=store, tool_registry=tools)
        return [event async for event in agent.run("echo twice", session=session)]

    events = anyio.run(run_agent)

    assert executions == []
    relevant_messages = [
        message for message in session.read_messages() if message.role in {"assistant", "tool"}
    ]
    assert [message.role for message in relevant_messages] == [
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    truncated = relevant_messages[0]
    assert truncated.finish_reason == "length"
    assert truncated.response_id == "truncated-response"
    assert truncated.tool_calls is not None
    assert [call.call_id for call in truncated.tool_calls] == ["call-1", "call-2"]
    tool_messages = relevant_messages[1:3]
    assert [message.tool_call_id for message in tool_messages] == ["call-1", "call-2"]
    assert all(message.is_error for message in tool_messages)
    assert all(message.content != INTERRUPTED_TOOL_RESULT_TEXT for message in tool_messages)
    ended = [event for event in events if isinstance(event, ToolExecutionEnded)]
    assert [event.call_id for event in ended] == ["call-1", "call-2"]
    assert all(event.failure_code == "invalid_arguments" for event in ended)
    assert all(event.retryable for event in ended)
    assert not any(
        isinstance(event, ToolExecutionStarted | ToolApprovalRequested | ToolApprovalResolved)
        for event in events
    )


def test_preserves_provider_text_content_index(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="second part", content_index=1),
                ProviderResponseCompleted(content="second part"),
            ]
        ]
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    delta = next(event for event in events if isinstance(event, MessageDelta))
    assert delta.content_index == 1


def test_maps_pre_start_provider_retry_progress(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderRetrying(
                    attempt=2,
                    max_attempts=3,
                    delay_seconds=0.5,
                    reason="rate_limit",
                    status_code=429,
                ),
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ]
        ]
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)
    retry_index = next(
        index for index, event in enumerate(events) if event.type == "provider.retrying"
    )
    message_start_index = next(
        index for index, event in enumerate(events) if event.type == "message.started"
    )
    retry = events[retry_index]

    assert retry_index < message_start_index
    assert retry.turn == 1
    assert retry.provider == "scripted"
    assert retry.attempt == 2
    assert retry.status_code == 429


@pytest.mark.parametrize(
    ("provider_events", "error_message"),
    [
        (
            [ProviderTextDelta(delta="too early")],
            "Provider emitted response data before response_started",
        ),
        (
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseStarted(model="test"),
            ],
            "Provider emitted response_started more than once",
        ),
        ([], "Provider stream ended before response_started"),
        (
            [ProviderResponseStarted(model="test")],
            "Provider stream ended without a terminal response",
        ),
        (
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
                ProviderTextDelta(delta="too late"),
            ],
            "Provider emitted an event after its terminal response",
        ),
        (
            [
                ProviderResponseStarted(model="test"),
                ProviderRetrying(
                    attempt=2,
                    max_attempts=3,
                    delay_seconds=0.5,
                    reason="network",
                ),
            ],
            "Provider emitted retry progress after response_started",
        ),
        (
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(
                    tool_call=ToolCall(call_id="call-1", name="echo", arguments={})
                ),
                ProviderResponseCompleted(content="", tool_calls=()),
            ],
            "Provider terminal tool calls do not match streamed tool calls",
        ),
        (
            [
                ProviderResponseStarted(model="test"),
                cast(ProviderEvent, object()),
            ],
            "Provider emitted unsupported event type: object",
        ),
    ],
)
def test_rejects_malformed_provider_lifecycle(
    tmp_path: Path,
    provider_events: list[ProviderEvent],
    error_message: str,
) -> None:
    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=ScriptedProvider([provider_events]),
            sessions=JsonlSessionStore(tmp_path),
        )
        events: list[object] = []
        with pytest.raises(ProviderProtocolError, match=error_message):
            async for event in agent.run("hello"):
                events.append(event)
        return events

    events = anyio.run(run_agent)

    assert [event.type for event in events[-3:]] == [
        "error",
        "turn.completed",
        "agent.completed",
    ]
    assert isinstance(events[-2], TurnCompleted)
    assert events[-2].outcome == "failed"
    assert isinstance(events[-1], AgentCompleted)
    assert events[-1].outcome == "failed"


def test_maps_provider_failed_terminal_to_failed_lifecycle(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderTextDelta(delta="partial"),
                ProviderResponseFailed(
                    message="upstream failed",
                    partial_content="partial",
                    response_id="response-1",
                ),
            ]
        ]
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    completion = next(event for event in events if isinstance(event, MessageCompleted))
    assert completion.content == "partial"
    assert completion.finish_reason == "error"
    assert [event.type for event in events[-3:]] == [
        "turn.completed",
        "session.saved",
        "agent.completed",
    ]
    assert cast(AgentCompleted, events[-1]).outcome == "failed"

    session_started = next(event for event in events if isinstance(event, AgentStarted))
    replayed = JsonlSessionStore(tmp_path).load(session_started.session_id).read_context_messages()
    assistant_messages = [message for message in replayed if message.role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content == "partial"
    assert assistant_messages[0].finish_reason == "error"


def test_does_not_persist_empty_failed_completion(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderResponseFailed(message="upstream failed", response_id="response-1"),
            ]
        ]
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    completion = next(event for event in events if isinstance(event, MessageCompleted))
    assert completion.content == ""
    assert completion.finish_reason == "error"
    session_started = next(event for event in events if isinstance(event, AgentStarted))
    replayed = JsonlSessionStore(tmp_path).load(session_started.session_id).read_context_messages()
    assert [(message.role, message.content) for message in replayed] == [("user", "hello")]


def test_retries_uncertain_completion_write_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="next"),
            ],
        ]
    )
    session = JsonlSessionStore(tmp_path).create()
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    append_entry = session.append_entry
    failed = False

    async def append_then_fail(entry: SessionEntry) -> SessionEntry:
        nonlocal failed
        persisted = await append_entry(entry)
        if (
            not failed
            and isinstance(entry, MessageSessionEntry)
            and entry.message.role == "assistant"
        ):
            failed = True
            raise OSError("uncertain completion write")
        return persisted

    monkeypatch.setattr(session, "append_entry", append_then_fail)

    async def run_agent() -> list[object]:
        events: list[object] = []
        with pytest.raises(OSError, match="uncertain completion write"):
            async for event in agent.run("hello", session=session):
                events.append(event)
        _next_events = [event async for event in agent.run("again", session=session, history=())]
        return events

    events = anyio.run(run_agent)

    assert not any(isinstance(event, MessageCompleted) for event in events)
    assistant_entries = [
        entry
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "assistant"
    ]
    assert len(assistant_entries) == 2
    assert assistant_entries[0].message is not None
    assert assistant_entries[0].message.content == "done"
    assert assistant_entries[1].message is not None
    assert assistant_entries[1].message.content == "next"
    assert [(message.role, message.content) for message in provider.calls[1].messages[-3:]] == [
        ("user", "hello"),
        ("assistant", "done"),
        ("user", "again"),
    ]


def test_flushes_prior_completion_before_next_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="second answer"),
            ],
        ]
    )
    session = JsonlSessionStore(tmp_path).create()
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    append_entry = session.append_entry
    fail_completion_writes = True

    async def fail_first_completion(entry: SessionEntry) -> SessionEntry:
        if (
            fail_completion_writes
            and isinstance(entry, MessageSessionEntry)
            and entry.message.role == "assistant"
            and entry.message.content == "first answer"
        ):
            raise OSError("completion storage unavailable")
        return await append_entry(entry)

    monkeypatch.setattr(session, "append_entry", fail_first_completion)

    async def run_agent() -> None:
        nonlocal fail_completion_writes
        with pytest.raises(OSError, match="completion storage unavailable"):
            _events = [event async for event in agent.run("first", session=session)]

        fail_completion_writes = False
        _events = [event async for event in agent.run("second", session=session, history=())]

    anyio.run(run_agent)

    assert [message.role for message in provider.calls[1].messages] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert [(message.role, message.content) for message in provider.calls[1].messages[-3:]] == [
        ("user", "first"),
        ("assistant", "first answer"),
        ("user", "second"),
    ]
    assistant_messages = [
        message.content for message in session.read_messages() if message.role == "assistant"
    ]
    assert assistant_messages == ["first answer", "second answer"]


def test_repairs_loaded_tool_call_before_provider_request(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    interrupted = Message(
        role="assistant",
        content="starting read",
        tool_calls=(
            ToolCallSnapshot(
                call_id="call-1",
                name="read",
                arguments={"path": "README.md"},
            ),
        ),
        finish_reason="tool_calls",
    )

    async def seed_session() -> None:
        await session.append_message(Message(role="user", content="read the file"))
        await session.append_message(interrupted)
        await session.append_message(Message(role="user", content="historical follow-up"))

    anyio.run(seed_session)
    provider = CapturingProvider()

    async def run_agent() -> None:
        agent = CodingSession(provider=provider, sessions=store)
        _events = [
            event
            async for event in agent.run(
                "continue",
                session=session,
                history=session.read_messages(),
            )
        ]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    replayed = [message for message in provider.seen_messages if message.role != "system"]
    assert [message.role for message in replayed] == ["user", "assistant", "user", "user"]
    assert replayed[0].content == "read the file"
    payload = json.loads(replayed[1].content)
    assert payload["assistant_content"] == "starting read"
    assert payload["calls"][0]["result"] == {
        "call_id": "call-1",
        "is_error": True,
        "output": INTERRUPTED_TOOL_RESULT_TEXT,
        "tool_name": "read",
    }
    assert [message.content for message in replayed[2:]] == [
        "historical follow-up",
        "continue",
    ]
    repairs = [
        message
        for message in session.read_messages()
        if message.role == "tool" and message.tool_call_id == "call-1"
    ]
    assert len(repairs) == 1
    assert repairs[0].is_error is True

    reloaded = store.load(session.path)

    async def resume_reloaded() -> None:
        agent = CodingSession(provider=CapturingProvider(), sessions=store)
        _events = [
            event
            async for event in agent.run(
                "after reload",
                session=reloaded,
                history=reloaded.read_messages(),
            )
        ]

    anyio.run(resume_reloaded)

    assert (
        len(
            [
                message
                for message in reloaded.read_messages()
                if message.role == "tool" and message.tool_call_id == "call-1"
            ]
        )
        == 1
    )


def test_retries_uncertain_repair_write_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    interrupted = Message(
        role="assistant",
        content="",
        tool_calls=(
            ToolCallSnapshot(
                call_id="call-1",
                name="bash",
                arguments={"command": "make"},
            ),
        ),
        finish_reason="tool_calls",
    )

    async def seed_session() -> None:
        await session.append_message(Message(role="user", content="build it"))
        await session.append_message(interrupted)

    anyio.run(seed_session)
    provider = CapturingProvider()
    agent = CodingSession(provider=provider, sessions=store)
    append_entry = session.append_entry
    failed = False

    async def append_then_fail(entry: SessionEntry) -> SessionEntry:
        nonlocal failed
        persisted = await append_entry(entry)
        if (
            not failed
            and isinstance(entry, MessageSessionEntry)
            and entry.message.content == INTERRUPTED_TOOL_RESULT_TEXT
        ):
            failed = True
            raise OSError("uncertain repair write")
        return persisted

    monkeypatch.setattr(session, "append_entry", append_then_fail)

    async def run_agent() -> None:
        with pytest.raises(OSError, match="uncertain repair write"):
            _events = [
                event
                async for event in agent.run(
                    "first attempt",
                    session=session,
                    history=session.read_messages(),
                )
            ]
        assert provider.seen_messages is None

        _events = [event async for event in agent.run("retry", session=session, history=())]

    anyio.run(run_agent)

    repairs = [
        entry
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry)
        and entry.message.role == "tool"
        and entry.message.tool_call_id == "call-1"
    ]
    assert len(repairs) == 1
    assert repairs[0].message is not None
    assert repairs[0].message.is_error is True
    assert provider.seen_messages is not None
    repaired_history = next(
        message
        for message in provider.seen_messages
        if message.role == "assistant" and INTERRUPTED_TOOL_RESULT_TEXT in message.content
    )
    assert json.loads(repaired_history.content)["calls"][0]["result"]["is_error"] is True


def test_retries_uncertain_finalizer_repair_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_call = ToolCall(
        call_id="call-1",
        name="read",
        arguments={"path": "README.md"},
        response_id="response-1",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(tool_call,),
                    response_id="response-1",
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(content="recovered", response_id="response-2"),
            ],
        ]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    agent = CodingSession(provider=provider, sessions=store)
    append_entry = session.append_entry
    failed = False

    async def append_then_fail(entry: SessionEntry) -> SessionEntry:
        nonlocal failed
        persisted = await append_entry(entry)
        if (
            not failed
            and isinstance(entry, MessageSessionEntry)
            and entry.message.content == INTERRUPTED_TOOL_RESULT_TEXT
        ):
            failed = True
            raise OSError("uncertain finalizer repair write")
        return persisted

    monkeypatch.setattr(session, "append_entry", append_then_fail)

    async def run_agent() -> None:
        events = agent.run("read it", session=session)
        while True:
            event = await anext(events)
            if isinstance(event, MessageCompleted):
                break
        with pytest.raises(OSError, match="uncertain finalizer repair write"):
            await events.aclose()

        _events = [event async for event in agent.run("retry", session=session, history=())]

    anyio.run(run_agent)

    repair_entries = [
        entry
        for entry in session.read_entries()
        if isinstance(entry, MessageSessionEntry)
        and entry.message.role == "tool"
        and entry.message.tool_call_id == "call-1"
    ]
    assert len(repair_entries) == 1
    assert len(provider.calls) == 2
    repaired_history = next(
        message
        for message in provider.calls[1].messages
        if message.role == "assistant" and INTERRUPTED_TOOL_RESULT_TEXT in message.content
    )
    assert json.loads(repaired_history.content)["calls"][0]["result"]["is_error"] is True
