from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

import wisp.coding.session as session_module
import wisp.coding.tool_execution as tool_execution
from wisp.agent.execution import ToolResultProcessingError
from wisp.agent.messages import Message
from wisp.agent.prompt import build_prompt_messages
from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.coding.session import CodingSession, _prompt_cache_key, _tool_result_status
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    ErrorEvent,
    ManagedProcessState,
    MessageCompleted,
    MessageDelta,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    WispEvent,
)
from wisp.providers.base import (
    ProviderProtocolError,
    ToolCall,
    ToolCallResult,
    ToolSpec,
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
from wisp.providers.fake import FakeProvider, ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import (
    MessageSessionEntry,
    SessionEntry,
    ToolResultPresentationSnapshot,
)
from wisp.sessions.errors import StaleSessionWriterError
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


class CapturingProvider:
    name = "capturing"
    default_model: str | None = "default"
    supports_prompt_cache_key = True

    def __init__(self) -> None:
        self.seen_messages: Sequence[Message] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_effort: str | None = None
        self.seen_prompt_cache_keys: list[str | None] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_effort = effort
        self.seen_prompt_cache_keys.append(prompt_cache_key)
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderTextDelta(delta="done")
        yield ProviderResponseCompleted(content="done")


def test_coding_session_builds_trusted_prompt_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = threading.Event()
    worker_release = threading.Event()
    worker_threads: list[int] = []
    original_build = build_prompt_messages

    def blocking_build(**kwargs: object) -> tuple[Message, ...]:
        worker_threads.append(threading.get_ident())
        worker_started.set()
        worker_release.wait(timeout=2)
        return original_build(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(session_module, "build_prompt_messages", blocking_build)

    async def scenario() -> tuple[bool, int]:
        agent = CodingSession(
            provider=CapturingProvider(),
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tool_context=ToolContext(cwd=tmp_path),
            trusted=True,
        )
        completed = anyio.Event()

        async def run_agent() -> None:
            _ = [event async for event in agent.run("hello")]
            completed.set()

        event_loop_thread = threading.get_ident()
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_agent)
            assert await anyio.to_thread.run_sync(worker_started.wait, 1)
            await anyio.sleep(0)
            responsive = not completed.is_set()
            worker_release.set()
        return responsive, event_loop_thread

    responsive, event_loop_thread = anyio.run(scenario)

    assert responsive
    assert worker_threads
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)


def test_coding_session_prompt_construction_is_abandoned_on_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = threading.Event()
    worker_release = threading.Event()

    def blocking_build(**_kwargs: object) -> tuple[Message, ...]:
        worker_started.set()
        worker_release.wait(timeout=2)
        return ()

    monkeypatch.setattr(session_module, "build_prompt_messages", blocking_build)

    async def scenario() -> None:
        agent = CodingSession(
            provider=CapturingProvider(),
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tool_context=ToolContext(cwd=tmp_path),
            trusted=True,
        )

        async def run_agent() -> None:
            _ = [event async for event in agent.run("hello")]

        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(run_agent)
                assert await anyio.to_thread.run_sync(worker_started.wait, 1)
                task_group.cancel_scope.cancel()
        worker_release.set()

    anyio.run(scenario)


class BlockingCapturingProvider(CapturingProvider):
    def __init__(self, *, started: anyio.Event, release: anyio.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.seen_messages = messages
        self.started.set()
        await self.release.wait()
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderResponseCompleted(content="stale answer")


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
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append((tool_results, previous_response_id))
        turn = self.turns.pop(0)
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        for item in turn:
            if isinstance(item, str):
                chunks.append(item)
                yield ProviderTextDelta(delta=item)
            elif isinstance(item, ToolCall):
                tool_calls.append(item)
                yield ProviderToolCallCompleted(
                    tool_call=item,
                    content_index=len(tool_calls) - 1,
                )
            else:
                raise TypeError(f"Unsupported test provider event: {item!r}")
        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=next(
                (call.response_id for call in reversed(tool_calls) if call.response_id),
                None,
            ),
            finish_reason="tool_calls" if tool_calls else "stop",
        )


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


def test_concurrent_coding_session_rejects_stale_provider_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        seed = await session.append_message(Message(role="user", content="seed"))
        started = anyio.Event()
        release = anyio.Event()
        stale_provider = BlockingCapturingProvider(started=started, release=release)
        stale_agent = CodingSession(provider=stale_provider, sessions=store)
        winner_provider = CapturingProvider()
        winner_agent = CodingSession(provider=winner_provider, sessions=store)
        stale_error: BaseException | None = None

        async def stale_run() -> None:
            nonlocal stale_error
            try:
                _events = [
                    event
                    async for event in stale_agent.run(
                        "left",
                        session=store.load(session.path),
                        history=session.read_context_messages(),
                    )
                ]
            except BaseException as exc:
                stale_error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(stale_run)
            await started.wait()
            _events = [
                event
                async for event in winner_agent.run(
                    "right",
                    session=store.load(session.path),
                    history=session.read_context_messages(),
                )
            ]
            release.set()

        assert isinstance(stale_error, StaleSessionWriterError)
        assert stale_provider.seen_messages is not None
        assert winner_provider.seen_messages is not None
        assert [message.content for message in stale_provider.seen_messages[-2:]] == [
            "seed",
            "left",
        ]
        assert [message.content for message in winner_provider.seen_messages[-3:]] == [
            "seed",
            "left",
            "right",
        ]
        assert [message.content for message in session.read_context_messages()] == [
            "seed",
            "left",
            "right",
            "done",
        ]
        assert session.read_entries()[0].id == seed.id

    anyio.run(scenario)


def test_coding_session_streams_fake_response_and_saves_session(tmp_path: Path) -> None:
    emitted_event_types: list[str] = []

    async def run_agent() -> list[object]:
        event_bus = EventBus()
        event_bus.on("*", lambda event: emitted_event_types.append(event.type))
        agent = CodingSession(
            provider=FakeProvider(), sessions=JsonlSessionStore(tmp_path), events=event_bus
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)
    deltas = [event.delta for event in events if isinstance(event, MessageDelta)]

    assert "".join(deltas) == "fake response to: hello"
    assert any(
        isinstance(event, MessageCompleted) and event.content == "fake response to: hello"
        for event in events
    )

    saved = next(event for event in events if isinstance(event, SessionSaved))
    assert saved.path.exists()

    records = [json.loads(line) for line in saved.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert "You are Wisp" in records[0]["message"]["content"]
    assert "[WISP PROJECT CONTEXT]" in records[1]["message"]["content"]
    assert records[2]["message"]["content"].startswith("[WISP TRUST BOUNDARY]")
    assert [record["message"]["content"] for record in records[3:]] == [
        "hello",
        "fake response to: hello",
    ]
    assert emitted_event_types == [
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
    ]


def test_coding_session_persists_follow_up_at_injection_boundary(tmp_path: Path) -> None:
    async def run_agent() -> tuple[list[WispEvent], tuple[Message, ...], CodingSession]:
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
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        event_bus = EventBus()
        agent = CodingSession(provider=provider, sessions=store, events=event_bus)
        queued = False
        persisted_at_injection = False

        async def queue_once(event: WispEvent) -> None:
            nonlocal queued
            if event.type == "agent.started" and not queued:
                queued = True
                update = await agent.follow_up("continue")
                assert update.follow_up == ("continue",)

        def observe_first_completion(event: WispEvent) -> None:
            assert isinstance(event, MessageCompleted)
            if event.content == "first answer":
                assert all(
                    message.content != "continue" for message in session.read_context_messages()
                )

        def observe_injection(event: WispEvent) -> None:
            nonlocal persisted_at_injection
            assert isinstance(event, QueueMessageInjected)
            persisted_at_injection = any(
                message.role == "user" and message.content == "continue"
                for message in session.read_context_messages()
            )

        event_bus.on("agent.started", queue_once)
        event_bus.on("message.completed", observe_first_completion)
        event_bus.on("queue.message.injected", observe_injection)
        events = [event async for event in agent.run("initial", session=session)]
        assert persisted_at_injection
        with pytest.raises(RuntimeError, match="no active agent run"):
            await agent.follow_up("too late")
        return events, session.read_context_messages(), agent

    events, messages, agent = anyio.run(run_agent)

    conversation = [
        (message.role, message.content) for message in messages if message.role != "system"
    ]
    assert conversation == [
        ("user", "initial"),
        ("assistant", "first answer"),
        ("user", "continue"),
        ("assistant", "second answer"),
    ]
    injected = next(event for event in events if isinstance(event, QueueMessageInjected))
    assert injected.kind == "follow_up"
    assert injected.content == "continue"
    assert any(isinstance(event, QueueUpdated) and event.follow_up == () for event in events)
    assert messages[-2].created_at == injected.timestamp


def test_coding_session_accepts_and_persists_steering_from_agent_start(tmp_path: Path) -> None:
    async def run_agent() -> tuple[list[WispEvent], tuple[Message, ...], CodingSession]:
        provider = ScriptedProvider(
            [
                [
                    ProviderResponseStarted(model="test"),
                    ProviderResponseCompleted(content="first answer"),
                ],
                [
                    ProviderResponseStarted(model="test"),
                    ProviderResponseCompleted(content="steered answer"),
                ],
            ]
        )
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        event_bus = EventBus()
        agent = CodingSession(provider=provider, sessions=store, events=event_bus)

        async def queue_at_start(event: WispEvent) -> None:
            assert event.type == "agent.started"
            update = await agent.steer("change direction")
            assert update.steering == ("change direction",)

        event_bus.on("agent.started", queue_at_start)
        events = [event async for event in agent.run("initial", session=session)]
        with pytest.raises(RuntimeError, match="no active agent run"):
            await agent.steer("too late")
        return events, session.read_context_messages(), agent

    events, messages, agent = anyio.run(run_agent)

    assert [
        (message.role, message.content) for message in messages if message.role != "system"
    ] == [
        ("user", "initial"),
        ("assistant", "first answer"),
        ("user", "change direction"),
        ("assistant", "steered answer"),
    ]
    assert any(
        isinstance(event, QueueMessageInjected)
        and event.kind == "steering"
        and event.content == "change direction"
        for event in events
    )


def test_coding_session_queue_state_is_safe_while_idle(tmp_path: Path) -> None:
    agent = CodingSession(provider=FakeProvider(), sessions=JsonlSessionStore(tmp_path))

    state = agent.queue_state()

    assert state.steering == ()
    assert state.follow_up == ()
    assert state.steering_mode == "one_at_a_time"
    assert state.follow_up_mode == "one_at_a_time"
    with pytest.raises(RuntimeError, match="no active agent run"):
        agent.set_queue_mode("steering", "all")
    with pytest.raises(RuntimeError, match="no active agent run"):
        agent.pop_queue("steering")
    with pytest.raises(RuntimeError, match="no active agent run"):
        agent.clear_queue()


def test_coding_session_state_snapshot_uses_effective_configuration_without_io(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    default_agent = CodingSession(provider=FakeProvider(), sessions=store)
    configured_agent = CodingSession(
        provider=FakeProvider(),
        sessions=store,
        model="configured-model",
        effort="high",
        auto_compaction_enabled=False,
    )

    assert default_agent.state_snapshot().model_dump() == {
        "provider": "fake",
        "model": "fake",
        "mode": "build",
        "effort": None,
        "auto_compaction_enabled": True,
        "steering_mode": "one_at_a_time",
        "follow_up_mode": "one_at_a_time",
        "pending_steering_count": 0,
        "pending_follow_up_count": 0,
    }
    assert configured_agent.state_snapshot().model_dump() == {
        "provider": "fake",
        "model": "configured-model",
        "mode": "build",
        "effort": "high",
        "auto_compaction_enabled": False,
        "steering_mode": "one_at_a_time",
        "follow_up_mode": "one_at_a_time",
        "pending_steering_count": 0,
        "pending_follow_up_count": 0,
    }
    assert tuple(tmp_path.iterdir()) == ()


def test_cancelled_session_stats_releases_operation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        await session.append_message(Message(role="user", content="previous"))
        agent = CodingSession(provider=FakeProvider(), sessions=store)
        started = threading.Event()
        release = threading.Event()
        original_read_entries = session.read_entries

        def slow_read_entries() -> tuple[SessionEntry, ...]:
            started.set()
            release.wait(timeout=5)
            return original_read_entries()

        monkeypatch.setattr(session, "read_entries", slow_read_entries)
        cancelled = anyio.Event()
        cancel_scope = anyio.CancelScope()

        async def read_stats() -> None:
            try:
                with cancel_scope:
                    await agent.get_session_stats(session)
            finally:
                cancelled.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(read_stats)
            await anyio.to_thread.run_sync(started.wait)
            cancel_scope.cancel()
            try:
                with anyio.fail_after(1):
                    await cancelled.wait()
                    await agent.get_session_stats()
            finally:
                release.set()

    anyio.run(scenario)


def test_coding_session_queue_facade_delegates_to_active_harness(tmp_path: Path) -> None:
    async def run_agent() -> None:
        provider = ScriptedProvider(
            [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
        )
        store = JsonlSessionStore(tmp_path)
        event_bus = EventBus()
        agent = CodingSession(provider=provider, sessions=store, events=event_bus)

        async def exercise_queue_facade(event: WispEvent) -> None:
            if event.type != "agent.started":
                return
            assert agent.queue_state().steering == ()
            assert agent.set_queue_mode("steering", "all").steering_mode == "all"
            assert agent.set_queue_mode("follow_up", "all").follow_up_mode == "all"
            assert (await agent.steer("steer one")).steering == ("steer one",)
            assert (await agent.follow_up("follow one")).follow_up == ("follow one",)
            snapshot = agent.state_snapshot()
            assert snapshot.steering_mode == "all"
            assert snapshot.follow_up_mode == "all"
            assert snapshot.pending_steering_count == 1
            assert snapshot.pending_follow_up_count == 1
            assert agent.queue_state().steering == ("steer one",)
            assert agent.queue_state().follow_up == ("follow one",)
            popped, pop_state = agent.pop_queue("steering")
            assert popped is not None
            assert popped.content == "steer one"
            assert pop_state.steering == ()
            assert (await agent.steer("steer two")).steering == ("steer two",)
            cleared_follow_up, follow_up_state = agent.clear_queue("follow_up")
            assert [message.content for message in cleared_follow_up.follow_up] == ["follow one"]
            assert follow_up_state.follow_up == ()
            cleared_all, final_state = agent.clear_queue()
            assert [message.content for message in cleared_all.steering] == ["steer two"]
            assert cleared_all.follow_up == ()
            assert final_state.steering == ()
            assert final_state.follow_up == ()
            assert final_state.steering_mode == "all"
            assert final_state.follow_up_mode == "all"

        event_bus.on("agent.started", exercise_queue_facade)
        _events = [event async for event in agent.run("initial")]

    anyio.run(run_agent)


def test_coding_session_queue_facade_rejects_invalid_kind_and_mode(
    tmp_path: Path,
) -> None:
    async def run_agent() -> None:
        provider = ScriptedProvider(
            [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
        )
        event_bus = EventBus()
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            events=event_bus,
        )

        def exercise_invalid_inputs(event: WispEvent) -> None:
            if event.type != "agent.started":
                return
            with pytest.raises(ValueError, match="Unsupported queue kind"):
                agent.set_queue_mode(cast(QueueKind, "unknown"), "all")
            with pytest.raises(ValueError, match="Unsupported queue kind"):
                agent.pop_queue(cast(QueueKind, "unknown"))
            with pytest.raises(ValueError, match="Unsupported queue kind"):
                agent.clear_queue(cast(QueueKind, "unknown"))
            with pytest.raises(ValueError, match="Unsupported queue mode"):
                agent.set_queue_mode("steering", cast(QueueMode, "invalid"))

        event_bus.on("agent.started", exercise_invalid_inputs)
        _events = [event async for event in agent.run("initial")]

    anyio.run(run_agent)


def test_coding_session_retains_unconsumed_queues_for_same_session_retry(
    tmp_path: Path,
) -> None:
    async def run_agent() -> tuple[list[WispEvent], tuple[Message, ...]]:
        provider = ScriptedProvider(
            [
                [ProviderResponseStarted(model="test"), RuntimeError("provider failed")],
                [
                    ProviderResponseStarted(model="test"),
                    ProviderResponseCompleted(content="other session answer"),
                ],
                [
                    ProviderResponseStarted(model="test"),
                    ProviderResponseCompleted(content="retry answer"),
                ],
                [
                    ProviderResponseStarted(model="test"),
                    ProviderResponseCompleted(content="steered answer"),
                ],
                [
                    ProviderResponseStarted(model="test"),
                    ProviderResponseCompleted(content="follow-up answer"),
                ],
            ]
        )
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        event_bus = EventBus()
        agent = CodingSession(provider=provider, sessions=store, events=event_bus)
        queued = False

        async def queue_once(event: WispEvent) -> None:
            nonlocal queued
            if not queued:
                queued = True
                agent.set_queue_mode("steering", "all")
                agent.set_queue_mode("follow_up", "all")
                await agent.steer("retained steering one")
                await agent.steer("retained steering two")
                await agent.follow_up("retained follow-up one")
                await agent.follow_up("retained follow-up two")

        event_bus.on("agent.started", queue_once)
        with pytest.raises(RuntimeError, match="provider failed"):
            _failed_events = [event async for event in agent.run("first", session=session)]

        assert agent.queue_state().steering == ("retained steering one", "retained steering two")
        assert agent.queue_state().steering_mode == "all"
        assert agent.queue_state(session).follow_up == (
            "retained follow-up one",
            "retained follow-up two",
        )
        assert agent.queue_state(session).follow_up_mode == "all"
        entry_count_before_snapshot = len(session.read_entries())
        retained_snapshot = agent.state_snapshot(session)
        assert retained_snapshot.steering_mode == "all"
        assert retained_snapshot.follow_up_mode == "all"
        assert retained_snapshot.pending_steering_count == 2
        assert retained_snapshot.pending_follow_up_count == 2
        assert len(session.read_entries()) == entry_count_before_snapshot
        assert agent.queue_state(session).steering == (
            "retained steering one",
            "retained steering two",
        )
        assert agent.queue_state(session).follow_up == (
            "retained follow-up one",
            "retained follow-up two",
        )
        assert all(
            message.content
            not in {
                "retained steering one",
                "retained steering two",
                "retained follow-up one",
                "retained follow-up two",
            }
            for message in session.read_context_messages()
        )

        other_session = store.create()
        checked_other_active_state = False

        def assert_explicit_session_state_while_other_session_runs(event: WispEvent) -> None:
            nonlocal checked_other_active_state
            if not isinstance(event, AgentStarted) or event.session_id != other_session.session_id:
                return
            checked_other_active_state = True
            assert agent.queue_state().steering == ()
            assert agent.queue_state(session).steering == (
                "retained steering one",
                "retained steering two",
            )
            assert agent.queue_state(session).steering_mode == "all"
            assert agent.queue_state(session).follow_up == (
                "retained follow-up one",
                "retained follow-up two",
            )
            assert agent.queue_state(session).follow_up_mode == "all"

        event_bus.on("agent.started", assert_explicit_session_state_while_other_session_runs)
        other_events = [event async for event in agent.run("other", session=other_session)]
        assert checked_other_active_state
        assert not any(isinstance(event, QueueMessageInjected) for event in other_events)

        events = [
            event
            async for event in agent.run(
                "retry",
                session=session,
                history=session.read_context_messages(),
            )
        ]
        return events, session.read_context_messages()

    events, messages = anyio.run(run_agent)

    assert [
        (event.kind, event.content) for event in events if isinstance(event, QueueMessageInjected)
    ] == [
        ("steering", "retained steering one"),
        ("steering", "retained steering two"),
        ("follow_up", "retained follow-up one"),
        ("follow_up", "retained follow-up two"),
    ]
    conversation = [
        (message.role, message.content) for message in messages if message.role != "system"
    ]
    assert conversation[-8:] == [
        ("user", "retry"),
        ("assistant", "retry answer"),
        ("user", "retained steering one"),
        ("user", "retained steering two"),
        ("assistant", "steered answer"),
        ("user", "retained follow-up one"),
        ("user", "retained follow-up two"),
        ("assistant", "follow-up answer"),
    ]


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


def test_coding_session_persists_completion_before_exposing_it(
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
                assert persisted.content == "checking"
                assert persisted.response_id == "response-1"
                assert persisted.finish_reason == "tool_calls"
                assert persisted.tool_calls is not None
                assert [call.call_id for call in persisted.tool_calls] == ["call-1"]
                assert persisted.created_at == event.timestamp
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


def test_coding_session_does_not_persist_partial_assistant_on_generator_close(
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


def test_coding_session_persists_tool_output_before_exposing_execution_end(
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


def test_coding_session_persists_truncated_tool_errors_without_running_tools(
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


def test_coding_session_preserves_provider_text_content_index(tmp_path: Path) -> None:
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


def test_coding_session_maps_pre_start_provider_retry_progress(tmp_path: Path) -> None:
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
def test_coding_session_rejects_malformed_provider_lifecycle(
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


def test_coding_session_maps_provider_failed_terminal_to_failed_lifecycle(tmp_path: Path) -> None:
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


def test_coding_session_does_not_persist_empty_failed_completion(tmp_path: Path) -> None:
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


def test_coding_session_retries_uncertain_completion_write_without_duplicate(
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


def test_coding_session_flushes_prior_completion_before_next_provider_request(
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


def test_coding_session_repairs_loaded_tool_call_before_provider_request(
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


def test_coding_session_retries_uncertain_repair_write_without_duplicate(
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


def test_coding_session_retries_uncertain_finalizer_repair_without_duplicate(
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


def test_coding_session_continues_with_history_and_labeled_tool_observations(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    session = JsonlSessionStore(tmp_path).create()
    history = [
        Message(role="system", content="old instructions"),
        Message(role="user", content="previous question"),
        Message(
            role="assistant",
            content="",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="call-1",
                    name="read",
                    arguments={"path": "README.md"},
                ),
            ),
            response_id="response-1",
            finish_reason="tool_calls",
        ),
        Message(
            role="tool",
            content="raw tool output must not be replayed as user text",
            tool_call_id="call-1",
            tool_name="read",
        ),
        Message(role="assistant", content="previous answer"),
    ]

    async def run_agent() -> list[object]:
        agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
        return [
            event async for event in agent.run("next question", session=session, history=history)
        ]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [message.role for message in provider.seen_messages] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "assistant",
        "user",
    ]
    assert "You are Wisp" in provider.seen_messages[0].content
    assert provider.seen_messages[1].content.startswith("[WISP PROJECT CONTEXT]")
    assert provider.seen_messages[2].content.startswith("[WISP TRUST BOUNDARY]")
    assert provider.seen_messages[3].content == "previous question"
    payload = json.loads(provider.seen_messages[4].content)
    assert payload["type"] == "wisp.portable_tool_exchange"
    assert payload["calls"][0]["arguments"] == {"path": "README.md"}
    assert payload["calls"][0]["result"]["output"] == (
        "raw tool output must not be replayed as user text"
    )
    assert [message.content for message in provider.seen_messages[5:]] == [
        "previous answer",
        "next question",
    ]
    assert not any(
        message.role == "assistant" and not message.content for message in provider.seen_messages
    )

    records = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert records[3]["message"]["content"] == "next question"
    assert records[4]["message"]["content"] == "done"


def test_coding_session_passes_tool_specs_to_provider(tmp_path: Path) -> None:
    provider = CapturingProvider()
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tools=[tool],
        )
        return [event async for event in agent.run("hello")]

    events = anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [message.role for message in provider.seen_messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert "You are Wisp" in provider.seen_messages[0].content
    assert "allowed tools:\n  - lookup: Look something up." in provider.seen_messages[1].content
    assert provider.seen_messages[2].content.startswith("[WISP TRUST BOUNDARY]")
    assert provider.seen_messages[3].content == "hello"
    assert provider.seen_tools == (tool,)
    assert any(isinstance(event, MessageCompleted) and event.content == "done" for event in events)


def test_coding_session_custom_prompt_messages_remain_full_replacement(tmp_path: Path) -> None:
    provider = CapturingProvider()
    custom_prompt = Message(role="system", content="Custom application policy.")

    async def run_agent() -> None:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            prompt_messages=(custom_prompt,),
        )
        _ = [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_messages is not None
    assert [(message.role, message.content) for message in provider.seen_messages] == [
        ("system", "Custom application policy."),
        ("user", "hello"),
    ]


def test_coding_session_passes_effort_to_provider(tmp_path: Path) -> None:
    provider = CapturingProvider()

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            effort="high",
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_effort == "high"


def test_coding_session_defaults_effort_to_none(tmp_path: Path) -> None:
    provider = CapturingProvider()

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
        )
        return [event async for event in agent.run("hello")]

    anyio.run(run_agent)

    assert provider.seen_effort is None


def test_coding_session_reuses_cache_key_for_session_and_isolates_sessions(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    sessions = JsonlSessionStore(tmp_path)
    first = sessions.create()
    second = sessions.create()

    async def run_agent() -> None:
        agent = CodingSession(provider=provider, sessions=sessions)
        _ = [event async for event in agent.run("first", session=first)]
        _ = [
            event
            async for event in agent.run(
                "resume",
                session=first,
                history=first.read_context_messages(),
            )
        ]
        _ = [event async for event in agent.run("other", session=second)]

    anyio.run(run_agent)

    assert provider.seen_prompt_cache_keys == [
        _prompt_cache_key(first.session_id),
        _prompt_cache_key(first.session_id),
        _prompt_cache_key(second.session_id),
    ]
    assert provider.seen_prompt_cache_keys[0] != provider.seen_prompt_cache_keys[2]


def test_coding_session_skips_project_context_when_untrusted(tmp_path: Path) -> None:
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
        agent = CodingSession(
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


def test_coding_session_includes_project_context_when_trusted(tmp_path: Path) -> None:
    provider = CapturingProvider()
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Trusted agent rules.\n", encoding="utf-8")

    async def run_agent() -> list[object]:
        agent = CodingSession(
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
