from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import wisp.agent.harness.runner as agent_harness_module
from tests.coding.compaction_support import (
    VALID_COMPACTION_SUMMARY,
    _append_turn,
    build_model_registry,
)
from wisp.agent.messages import Message
from wisp.coding.session import CodingSession
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ErrorEvent,
    MessageCompleted,
    SessionStats,
    ToolExecutionEnded,
    TurnCompleted,
    WispEvent,
)
from wisp.providers.base import ContextOverflowError
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
)
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.builtin import ReadTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


class MutatingRecoveryTool:
    name = "mutate"
    safety = "mutating"
    description = "Record a mutable side effect."
    input_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, arguments: object, context: ToolContext) -> ToolResult:
        del arguments, context
        self.calls += 1
        return ToolResult(text="mutated")


def test_coding_session_recovers_one_overflow_with_compaction_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer three"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    real_run_agent_loop = agent_harness_module.run_agent_loop
    primary_loop_calls = 0

    def recording_run_agent_loop(*args: object, **kwargs: object) -> object:
        nonlocal primary_loop_calls
        primary_loop_calls += 1
        return real_run_agent_loop(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_harness_module, "run_agent_loop", recording_run_agent_loop)

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [
            event
            async for event in agent.run(
                "question three",
                session=session,
                operation_id="prompt-1",
            )
        ]

    events = anyio.run(run)

    overflow = next(event for event in events if event.type == "context.overflow")
    failed_turn = next(event for event in events if isinstance(event, TurnCompleted))
    completed = next(
        event
        for event in events
        if isinstance(event, CompactionCompleted) and event.reason == "overflow"
    )
    retry_turn = next(
        event
        for event in events
        if isinstance(event, MessageCompleted) and event.content == "answer three"
    )
    assert overflow.turn == 1
    assert failed_turn.turn == 1
    assert failed_turn.outcome == "failed"
    assert completed.outcome == "completed"
    assert completed.will_retry is True
    assert completed.compaction_id is not None
    assert retry_turn.turn == 2
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, CompactionStarted) and event.reason == "threshold" for event in events
    )
    assert events[-1].type == "agent.completed"
    assert events[-1].turns == 2
    assert events[-1].outcome == "completed"
    assert len(provider.calls) == 3
    # The isolated compaction summarizer is intentionally not counted here.
    assert primary_loop_calls == 1
    entries = session.read_entries()
    overflow_record = next(
        entry.compaction
        for entry in entries
        if isinstance(entry, CompactionSessionEntry) and entry.compaction.reason == "overflow"
    )
    assert overflow_record is not None
    assert overflow_record.schema_version == 4
    user_messages = [
        entry.message.content
        for entry in entries
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
    ]
    assert user_messages == [
        "question one",
        "question two",
        "question three",
    ]
    assert all(entry.operation_id == "prompt-1" for entry in entries[-3:])


def test_coding_session_does_not_retry_a_second_overflow(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    emitted: list[WispEvent] = []
    bus = EventBus()
    bus.on("*", emitted.append)

    async def run() -> None:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        with pytest.raises(ContextOverflowError, match="maximum context length exceeded"):
            _events = [event async for event in agent.run("question three", session=session)]

    anyio.run(run)

    assert sum(event.type == "context.overflow" for event in emitted) == 2
    assert (
        sum(
            isinstance(event, CompactionStarted) and event.reason == "overflow" for event in emitted
        )
        == 1
    )
    assert (
        sum(
            isinstance(event, CompactionCompleted) and event.reason == "overflow"
            for event in emitted
        )
        == 1
    )
    assert emitted[-3].type == "error"
    assert emitted[-1].type == "agent.completed"
    assert emitted[-1].outcome == "failed"


def test_coding_session_overflow_without_compactable_prefix_is_terminal(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    emitted: list[WispEvent] = []
    bus = EventBus()
    bus.on("*", emitted.append)

    async def run() -> None:
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        with pytest.raises(ContextOverflowError):
            _events = [event async for event in agent.run("question one")]

    anyio.run(run)

    assert any(event.type == "context.overflow" for event in emitted)
    assert not any(isinstance(event, CompactionStarted | CompactionCompleted) for event in emitted)
    assert emitted[-3].type == "error"
    assert emitted[-1].type == "agent.completed"


def test_coding_session_overflow_summary_failure_is_terminal(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content=" ",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    emitted: list[WispEvent] = []
    bus = EventBus()
    bus.on("*", emitted.append)

    async def run() -> None:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        with pytest.raises(ContextOverflowError, match="Context overflow recovery failed"):
            _events = [event async for event in agent.run("question three", session=session)]

    anyio.run(run)

    completed = next(
        event
        for event in emitted
        if isinstance(event, CompactionCompleted) and event.reason == "overflow"
    )
    assert completed.outcome == "failed"
    assert completed.will_retry is False
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
    assert len(provider.calls) == 2
    # The loop ends the rejected turn once, using the session's recovery-failure message.
    errors = [event.message for event in emitted if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].startswith("Context overflow recovery failed")
    assert [
        (event.turn, event.outcome) for event in emitted if isinstance(event, TurnCompleted)
    ] == [(1, "failed")]
    assert [event.type for event in emitted][-3:] == ["error", "turn.completed", "agent.completed"]


def test_coding_session_overflow_retry_setup_failure_does_not_claim_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    emitted: list[WispEvent] = []
    bus = EventBus()
    bus.on("*", emitted.append)

    async def run() -> None:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )

        def fail_rehydrate() -> tuple[Message, ...]:
            raise OSError()

        monkeypatch.setattr(session, "read_context_messages", fail_rehydrate)
        with pytest.raises(ContextOverflowError, match="Context overflow recovery failed"):
            _events = [event async for event in agent.run("question three", session=session)]

    anyio.run(run)

    completed = next(
        event
        for event in emitted
        if isinstance(event, CompactionCompleted) and event.reason == "overflow"
    )
    assert completed.outcome == "completed"
    assert completed.will_retry is False
    assert completed.error == "OSError"
    types = [event.type for event in emitted]
    assert types[-3:] == ["error", "turn.completed", "agent.completed"]
    assert any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_overflow_recovery_allows_unknown_context_window(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="recovered"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], SessionStats]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            prompt_messages=(Message(role="system", content="system"),),
        )
        return [event async for event in agent.run("question three", session=session)]

    events = anyio.run(run)

    completed = next(
        event
        for event in events
        if isinstance(event, CompactionCompleted) and event.reason == "overflow"
    )
    assert completed.outcome == "completed"
    assert completed.will_retry is True
    assert len(provider.calls) == 3


@pytest.mark.parametrize("enabled", [False, True])
def test_coding_session_skips_overflow_retry_when_ineligible(
    tmp_path: Path,
    enabled: bool,
) -> None:
    overflow_stream: list[ProviderEvent] = [ProviderResponseStarted(model="model")]
    if enabled:
        overflow_stream.append(ProviderTextDelta(delta="partial"))
    overflow_stream.append(ProviderResponseFailed(message="maximum context length exceeded"))
    provider = ScriptedProvider(
        [overflow_stream],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> None:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
            auto_compaction_enabled=enabled,
        )
        _events = [event async for event in agent.run("question three", session=session)]

    with pytest.raises(ContextOverflowError):
        anyio.run(run)

    assert len(provider.calls) == 1
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_overflow_retry_reuses_completed_tool_round(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.txt"
    source.write_text("tool output", encoding="utf-8")
    call = ToolCall(call_id="call-1", name="read", arguments={"path": source.name})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderTextDelta(delta="checking"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer after tool"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    registry = ToolRegistry()
    registry.register(ReadTool())

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [event async for event in agent.run("question three", session=session)]

    events = anyio.run(run)

    assert len(provider.calls) == 4
    assert sum(isinstance(event, ToolExecutionEnded) for event in events) == 1
    assert (
        sum(
            isinstance(event, CompactionCompleted)
            and event.reason == "overflow"
            and event.outcome == "completed"
            for event in events
        )
        == 1
    )
    assert [
        message.content
        for message in session.read_context_messages()
        if message.role == "tool" and message.tool_call_id == "call-1"
    ] == ["tool output"]


def test_overflow_retry_preserves_the_prompt_tool_iteration_limit(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("tool output", encoding="utf-8")
    read_call = ToolCall(call_id="call-1", name="read", arguments={"path": source.name})
    mutate_call = ToolCall(call_id="call-2", name="mutate", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=read_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(read_call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=mutate_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(mutate_call,),
                    finish_reason="tool_calls",
                ),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    registry = ToolRegistry()
    registry.register(ReadTool())
    mutating_tool = MutatingRecoveryTool()
    registry.register(mutating_tool)

    async def run() -> None:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
            max_tool_iterations=1,
        )
        with pytest.raises(RuntimeError, match="Maximum tool iterations exceeded: 1"):
            _events = [event async for event in agent.run("question three", session=session)]

    anyio.run(run)

    assert len(provider.calls) == 4
    assert mutating_tool.calls == 0


def test_coding_session_retries_overflow_after_rejected_truncated_mutating_call(
    tmp_path: Path,
) -> None:
    call = ToolCall(call_id="call-1", name="mutate", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="length",
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer after retry"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    registry = ToolRegistry()
    tool = MutatingRecoveryTool()
    registry.register(tool)

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [event async for event in agent.run("question three", session=session)]

    events = anyio.run(run)

    assert tool.calls == 0
    assert len(provider.calls) == 4
    assert any(
        isinstance(event, CompactionCompleted)
        and event.reason == "overflow"
        and event.outcome == "completed"
        and event.will_retry
        for event in events
    )
    assert any(
        isinstance(event, MessageCompleted) and event.content == "answer after retry"
        for event in events
    )


def test_coding_session_overflow_does_not_retry_after_mutating_tool(tmp_path: Path) -> None:
    call = ToolCall(call_id="call-1", name="mutate", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseFailed(message="maximum context length exceeded"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    registry = ToolRegistry()
    tool = MutatingRecoveryTool()
    registry.register(tool)

    async def run() -> None:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        with pytest.raises(ContextOverflowError):
            _events = [event async for event in agent.run("question three", session=session)]

    anyio.run(run)

    assert tool.calls == 1
    assert len(provider.calls) == 2
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
