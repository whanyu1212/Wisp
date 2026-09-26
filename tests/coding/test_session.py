from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import anyio
import pytest

import wisp.coding.session as session_module
from tests.coding.session_support import (
    CapturingProvider,
)
from wisp.agent.messages import Message
from wisp.agent.prompt import build_prompt_messages
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    QueueMessageInjected,
    QueueUpdated,
    SessionSaved,
    TurnCompleted,
    WispEvent,
)
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
)
from wisp.providers.fake import FakeProvider, ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.entries import (
    MessageSessionEntry,
)
from wisp.sessions.errors import StaleSessionWriterError
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext


def test_builds_trusted_prompt_off_event_loop(
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


def test_prompt_construction_is_abandoned_on_cancel(
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


def test_concurrent_session_rejects_stale_provider_result(tmp_path: Path) -> None:
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


def test_streams_fake_response_and_saves_session(tmp_path: Path) -> None:
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


def test_persists_follow_up_at_injection_boundary(tmp_path: Path) -> None:
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
        entries = {
            entry.id: entry
            for entry in session.read_entries()
            if isinstance(entry, MessageSessionEntry)
        }
        started = next(event for event in events if isinstance(event, AgentStarted))
        assert started.message_entry_id is not None
        assert entries[started.message_entry_id].message.content == "initial"
        injected = next(event for event in events if isinstance(event, QueueMessageInjected))
        assert injected.message_entry_id is not None
        assert entries[injected.message_entry_id].message.content == "continue"
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


def test_failure_after_completed_turn_does_not_complete_it_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first answer"),
            ],
        ]
    )
    store = JsonlSessionStore(tmp_path)
    event_bus = EventBus()
    agent = CodingSession(provider=provider, sessions=store, events=event_bus)
    queue_message = agent._queue_message

    def fail_follow_up(session: Any, message: Message, **kwargs: Any) -> str:
        if message.content == "continue":
            raise RuntimeError("follow-up persistence failed")
        return queue_message(session, message, **kwargs)

    monkeypatch.setattr(agent, "_queue_message", fail_follow_up)

    async def queue_follow_up(event: WispEvent) -> None:
        await agent.follow_up("continue")

    event_bus.on("agent.started", queue_follow_up)

    async def run_agent() -> list[WispEvent]:
        events: list[WispEvent] = []
        with pytest.raises(RuntimeError, match="follow-up persistence failed"):
            async for event in agent.run("initial", session=store.create()):
                events.append(event)
        return events

    events = anyio.run(run_agent)

    # The loop completed turn 1 before the session failed to persist the follow-up,
    # so the failure publishes an error without a second terminal for that turn.
    assert [
        (event.turn, event.outcome) for event in events if isinstance(event, TurnCompleted)
    ] == [(1, "completed")]
    errors = [event.message for event in events if isinstance(event, ErrorEvent)]
    assert errors == ["follow-up persistence failed"]
    completed = events[-1]
    assert isinstance(completed, AgentCompleted)
    assert (completed.turns, completed.outcome) == (1, "failed")


class _OtherProvider(ScriptedProvider):
    name = "other-scripted"
