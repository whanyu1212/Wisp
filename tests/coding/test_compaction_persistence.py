from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio
import pytest

from tests.coding.compaction_support import (
    VALID_COMPACTION_SUMMARY,
    CacheAwareScriptedProvider,
    GatedPreflightSummaryProvider,
    _append_turn,
    build_model_registry,
)
from wisp.agent.messages import Message
from wisp.coding.compaction import (
    CompactionSummaryError,
)
from wisp.coding.session import CodingSession
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ErrorEvent,
    SessionSaved,
    SessionStats,
    TokenUsage,
    ToolCallSnapshot,
    WispEvent,
)
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntry,
)
from wisp.sessions.errors import StaleSessionWriterError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.sessions.replay import (
    HISTORICAL_CONTEXT_SUMMARY_LABEL,
    StaleCompactionError,
)


class BlockingSummaryProvider:
    name = "blocking-summary"
    default_model: str | None = "blocking-model"

    def __init__(self, started: anyio.Event) -> None:
        self.started = started

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
        del messages, tools, tool_results, previous_response_id, effort
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        self.started.set()
        await anyio.sleep_forever()


def test_coding_session_rejects_stale_compaction_without_writing_failure_event(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[JsonlSession, BaseException | None]:
        summary_started = anyio.Event()
        release_summary = anyio.Event()
        provider = GatedPreflightSummaryProvider(summary_started, release_summary)
        store = JsonlSessionStore(tmp_path)
        session = store.create()
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(provider=provider, sessions=store)
        error: BaseException | None = None

        async def compact() -> None:
            nonlocal error
            try:
                _events = [event async for event in agent.compact(session)]
            except BaseException as exc:  # noqa: BLE001 - asserted below
                error = exc

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(compact)
            await summary_started.wait()
            await session.append_message(Message(role="user", content="competing prompt"))
            release_summary.set()

        return session, error

    session, error = anyio.run(run)

    assert isinstance(error, StaleSessionWriterError)
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
    assert not any(event["type"] == "compaction.completed" for event in session.read_events())
    assert session.read_context_messages()[-1].content == "competing prompt"


def test_manual_compaction_snapshots_cursor_after_pending_entry_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
        ]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    agent = CodingSession(provider=provider, sessions=store)
    append_if_current = session.append_entry_if_current
    reject_completion = True

    async def fail_completion_once(
        entry: SessionEntry,
        *,
        expected_active_leaf_id: str | None,
    ) -> SessionEntry:
        if (
            reject_completion
            and isinstance(entry, MessageSessionEntry)
            and entry.message.role == "assistant"
        ):
            raise OSError("completion storage unavailable")
        return await append_if_current(
            entry,
            expected_active_leaf_id=expected_active_leaf_id,
        )

    monkeypatch.setattr(session, "append_entry_if_current", fail_completion_once)

    async def run() -> list[WispEvent]:
        nonlocal reject_completion
        await _append_turn(session, "seed")
        with pytest.raises(OSError, match="completion storage unavailable"):
            _events = [event async for event in agent.run("first", session=session)]
        reject_completion = False
        return [event async for event in agent.compact(session)]

    events = anyio.run(run)

    assert any(
        isinstance(event, CompactionCompleted) and event.outcome == "completed" for event in events
    )
    assert [message.content for message in session.read_messages()][-1] == "first answer"
    assert sum(entry.kind == "compaction" for entry in session.read_entries()) == 1


def test_coding_session_reconciles_append_that_commits_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ]
        ]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        original_append = session.append_compaction_entry
        calls = 0

        async def uncertain_append(
            entry: SessionEntry,
            *,
            expected_context_entry_ids: Sequence[str],
        ) -> SessionEntry:
            nonlocal calls
            calls += 1
            result = await original_append(
                entry,
                expected_context_entry_ids=expected_context_entry_ids,
            )
            if calls == 1:
                raise OSError("post-append validation failed")
            return result

        monkeypatch.setattr(session, "append_compaction_entry", uncertain_append)
        agent = CodingSession(provider=provider, sessions=store)
        events = [event async for event in agent.compact(session)]
        assert calls == 2
        return events

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.outcome == "completed"
    assert sum(entry.kind == "compaction" for entry in session.read_entries()) == 1


def test_coding_session_compaction_is_durable_and_next_run_uses_active_context(
    tmp_path: Path,
) -> None:
    usage = ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25)
    provider = CacheAwareScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY, usage=usage),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="next answer"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], list[WispEvent]]:
        first_ids = await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(provider=provider, sessions=store, model="model", effort="low")
        compact_events = [
            event async for event in agent.compact(session, instructions="Keep exact paths")
        ]
        record = session.read_entries()[-1].compaction
        assert record is not None
        assert record.replaced_entry_ids == first_ids
        run_events = [event async for event in agent.run("question three", session=session)]
        return compact_events, run_events

    compact_events, run_events = anyio.run(run)

    assert [event.type for event in compact_events] == [
        "compaction.started",
        "session.saved",
        "compaction.completed",
    ]
    assert isinstance(compact_events[0], CompactionStarted)
    assert compact_events[0].source_entry_count == 4
    assert isinstance(compact_events[1], SessionSaved)
    completed = compact_events[2]
    assert isinstance(completed, CompactionCompleted)
    assert completed.outcome == "completed"
    assert completed.replaced_entry_count == 2
    assert completed.retained_entry_count == 2
    assert completed.provider == "scripted"
    assert completed.model == "model"
    assert completed.usage == TokenUsage(input_tokens=20, output_tokens=5, total_tokens=25)
    persisted_compaction = next(
        entry for entry in session.read_entries() if entry.kind == "compaction"
    )
    assert completed.compaction_id == persisted_compaction.id
    assert any(event.type == "agent.completed" for event in run_events)
    assert [call.prompt_cache_key for call in provider.calls] == [
        f"wisp:{session.session_id}",
        f"wisp:{session.session_id}",
    ]

    replay = session.read_context()
    assert [row.message.role for row in replay.rows] == [
        "user",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert replay.rows[0].message.content == (
        f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\n{VALID_COMPACTION_SUMMARY}"
    )
    assert [message.content for message in provider.calls[1].messages[-4:]] == [
        replay.rows[0].message.content,
        "question two",
        "answer two",
        "question three",
    ]
    compaction_entry = persisted_compaction
    assert isinstance(compaction_entry, CompactionSessionEntry)
    assert compaction_entry.compaction.instructions == "Keep exact paths"
    assert compaction_entry.compaction.usage == completed.usage


def test_coding_session_repairs_interrupted_tools_before_compaction(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ]
        ]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    call = ToolCallSnapshot(call_id="call-1", name="read", arguments={"path": "a.py"})

    async def run() -> None:
        await _append_turn(session, "zero")
        await session.append_message(Message(role="user", content="interrupted"))
        await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(call,),
                finish_reason="tool_calls",
            )
        )
        await _append_turn(session, "two")
        await _append_turn(session, "three")
        agent = CodingSession(provider=provider, sessions=store)
        _events = [event async for event in agent.compact(session)]

    anyio.run(run)

    entries = session.read_entries()
    repair = next(
        entry
        for entry in entries
        if isinstance(entry, MessageSessionEntry)
        and entry.message.role == "tool"
        and entry.message.tool_call_id == "call-1"
    )
    compaction = next(entry for entry in entries if isinstance(entry, CompactionSessionEntry))
    assert entries.index(repair) < entries.index(compaction)
    assert repair.id in compaction.compaction.replaced_entry_ids
    assert [message.content for message in session.read_context_messages()[-2:]] == [
        "question three",
        "answer three",
    ]


def test_coding_session_summary_failure_emits_failure_and_appends_no_compaction(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="model"), ProviderResponseCompleted(content="  ")]]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(provider=provider, sessions=store)
        events: list[WispEvent] = []
        with pytest.raises(CompactionSummaryError, match="was blank"):
            async for event in agent.compact(session):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "compaction.started",
        "error",
        "compaction.completed",
    ]
    assert isinstance(events[1], ErrorEvent)
    failed = events[2]
    assert isinstance(failed, CompactionCompleted)
    assert failed.outcome == "failed"
    assert failed.compaction_id is None
    assert failed.error == "Compaction summary was blank"
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_summary_tool_call_failure_persists_accounting(tmp_path: Path) -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="calling",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                    usage=ProviderUsage(input_tokens=40, output_tokens=10, total_tokens=50),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], SessionStats]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(provider=provider, sessions=store, models=build_model_registry())
        events: list[WispEvent] = []
        with pytest.raises(CompactionSummaryError, match="forbidden tool call"):
            async for event in agent.compact(session):
                events.append(event)
        return events, await agent.get_session_stats(session)

    events, stats = anyio.run(run)

    failed = events[-1]
    assert isinstance(failed, CompactionCompleted)
    assert failed.outcome == "failed"
    assert failed.usage == TokenUsage(input_tokens=40, output_tokens=10, total_tokens=50)
    assert failed.cost is not None
    assert any(
        event["type"] == "compaction.completed"
        and event.get("usage") is not None
        and event.get("cost") is not None
        for event in session.read_events()
    )
    assert stats.cost.unpriced_record_count == 3
    assert stats.usage_record_count == 1
    assert stats.usage == TokenUsage(input_tokens=40, output_tokens=10, total_tokens=50)
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_summary_commit_failure_persists_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content=VALID_COMPACTION_SUMMARY,
                    usage=ProviderUsage(input_tokens=40, output_tokens=10, total_tokens=50),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], SessionStats]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")

        async def stale_append(
            _entry: SessionEntry,
            *,
            expected_context_entry_ids: Sequence[str],
        ) -> SessionEntry:
            del expected_context_entry_ids
            raise StaleCompactionError("Compaction plan is stale")

        monkeypatch.setattr(session, "append_compaction_entry", stale_append)
        agent = CodingSession(provider=provider, sessions=store, models=build_model_registry())
        events: list[WispEvent] = []
        with pytest.raises(StaleCompactionError, match="stale"):
            async for event in agent.compact(session):
                events.append(event)
        return events, await agent.get_session_stats(session)

    events, stats = anyio.run(run)

    failed = events[-1]
    assert isinstance(failed, CompactionCompleted)
    assert failed.outcome == "failed"
    assert failed.usage == TokenUsage(input_tokens=40, output_tokens=10, total_tokens=50)
    assert failed.cost is not None
    assert stats.usage_record_count == 1
    assert stats.usage == TokenUsage(input_tokens=40, output_tokens=10, total_tokens=50)
    assert stats.cost.unpriced_record_count == 3
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_summary_cancellation_appends_no_compaction(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        started = anyio.Event()
        emitted: list[WispEvent] = []
        bus = EventBus()
        bus.on("*", emitted.append)
        agent = CodingSession(
            provider=BlockingSummaryProvider(started),
            sessions=store,
            events=bus,
        )
        scope = anyio.CancelScope()

        async def consume() -> None:
            with scope:
                _events = [event async for event in agent.compact(session)]

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(consume)
            await started.wait()
            scope.cancel()
        return emitted

    emitted = anyio.run(run)

    assert [event.type for event in emitted] == [
        "compaction.started",
        "compaction.completed",
    ]
    cancelled = emitted[-1]
    assert isinstance(cancelled, CompactionCompleted)
    assert cancelled.outcome == "cancelled"
    assert cancelled.compaction_id is None
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_cancellation_after_started_emits_cancelled_terminal(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        provider_started = anyio.Event()
        emitted: list[WispEvent] = []
        bus = EventBus()
        bus.on("*", emitted.append)
        agent = CodingSession(
            provider=BlockingSummaryProvider(provider_started),
            sessions=store,
            events=bus,
        )
        events = agent.compact(session)
        first = await anext(events)
        assert isinstance(first, CompactionStarted)
        scope = anyio.CancelScope()

        async def advance() -> None:
            with scope:
                scope.cancel()
                await anext(events)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(advance)
        await events.aclose()
        return emitted

    emitted = anyio.run(run)

    assert [event.type for event in emitted] == [
        "compaction.started",
        "compaction.completed",
    ]
    terminal = emitted[-1]
    assert isinstance(terminal, CompactionCompleted)
    assert terminal.outcome == "cancelled"
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_cancellation_after_append_starts_finishes_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ]
        ]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        append_started = anyio.Event()
        release_append = anyio.Event()
        original_append = session.append_compaction_entry

        async def blocking_append(
            entry: SessionEntry,
            *,
            expected_context_entry_ids: Sequence[str],
        ) -> SessionEntry:
            append_started.set()
            await release_append.wait()
            return await original_append(
                entry,
                expected_context_entry_ids=expected_context_entry_ids,
            )

        monkeypatch.setattr(session, "append_compaction_entry", blocking_append)
        emitted: list[WispEvent] = []
        bus = EventBus()
        bus.on("*", emitted.append)
        agent = CodingSession(provider=provider, sessions=store, events=bus)
        scope = anyio.CancelScope()

        async def consume() -> None:
            with scope:
                _events = [event async for event in agent.compact(session)]

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(consume)
            await append_started.wait()
            scope.cancel()
            release_append.set()
        return emitted

    emitted = anyio.run(run)

    assert [event.type for event in emitted] == [
        "compaction.started",
        "session.saved",
        "compaction.completed",
    ]
    completed = emitted[-1]
    assert isinstance(completed, CompactionCompleted)
    assert completed.outcome == "completed"
    assert any(entry.kind == "compaction" for entry in session.read_entries())


def test_coding_session_post_commit_event_failure_reports_warning_not_failed_compaction(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ]
        ]
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    bus = EventBus()

    def fail_session_saved(event: WispEvent) -> None:
        if isinstance(event, SessionSaved):
            raise RuntimeError("extension hook failed")

    bus.on("*", fail_session_saved)

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        agent = CodingSession(provider=provider, sessions=store, events=bus)
        return [event async for event in agent.compact(session)]

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "compaction.started",
        "session.saved",
        "compaction.completed",
        "error",
    ]
    completed = events[2]
    assert isinstance(completed, CompactionCompleted)
    assert completed.outcome == "completed"
    warning = events[3]
    assert isinstance(warning, ErrorEvent)
    assert "Compaction committed" in warning.message
    assert "extension hook failed" in warning.message
    assert any(entry.kind == "compaction" for entry in session.read_entries())
