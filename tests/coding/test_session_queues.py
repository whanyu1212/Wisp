from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

import anyio
import pytest

from wisp.agent.messages import Message
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentStarted,
    QueueKind,
    QueueMessageInjected,
    QueueMode,
    WispEvent,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
)
from wisp.providers.fake import FakeProvider, ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.entries import (
    SessionEntry,
)
from wisp.sessions.jsonl import JsonlSessionStore


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
