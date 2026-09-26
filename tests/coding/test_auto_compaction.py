from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio
import pytest

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
    ContextEstimated,
    ErrorEvent,
    SessionSaved,
    SessionStats,
    TokenUsage,
    WispEvent,
)
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import (
    CompactionSessionEntry,
    SessionEntry,
)
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.sessions.replay import (
    HISTORICAL_CONTEXT_SUMMARY_LABEL,
    SessionReplay,
)
from wisp.tools.builtin import ReadTool
from wisp.tools.context import ToolContext


class BlockingAutoCompactionProvider:
    name = "scripted"
    default_model: str | None = "model"

    def __init__(self, summary_started: anyio.Event) -> None:
        self.summary_started = summary_started
        self.calls = 0

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
        self.calls += 1
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if self.calls == 1:
            yield ProviderResponseCompleted(
                content="answer two",
                usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
            )
            return
        self.summary_started.set()
        await anyio.sleep_forever()


class BlockingOverflowRecoveryProvider:
    name = "scripted"
    default_model: str | None = "model"

    def __init__(self, summary_started: anyio.Event) -> None:
        self.summary_started = summary_started
        self.calls = 0

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
        self.calls += 1
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if self.calls == 1:
            yield ProviderResponseFailed(message="maximum context length exceeded")
            return
        self.summary_started.set()
        await anyio.sleep_forever()


def test_auto_compacts_after_completed_turn(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=81, output_tokens=11, total_tokens=92),
                ),
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

    async def run() -> list[WispEvent]:
        first_ids = await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(auto_compact_token_limit=80),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=10,
        )
        events = [event async for event in agent.run("question two", session=session)]
        record = next(
            entry.compaction
            for entry in session.read_entries()
            if isinstance(entry, CompactionSessionEntry)
        )
        assert record.replaced_entry_ids == first_ids
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.completed",
        "context.pressure",
        "turn.completed",
        "compaction.started",
        "session.saved",
        "compaction.completed",
        "agent.completed",
    ]
    started = next(event for event in events if isinstance(event, CompactionStarted))
    assert started.reason == "threshold"
    assert started.trigger_budget is not None
    assert started.trigger_budget.observed_tokens == 81
    assert started.trigger_budget.context_window == 100
    assert started.trigger_budget.reserve_tokens == 20
    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.reason == "threshold"
    assert completed.outcome == "completed"
    assert sum(isinstance(event, SessionSaved) for event in events) == 1
    assert len(provider.calls) == 2
    entries = session.read_entries()
    record = next(
        entry.compaction for entry in entries if isinstance(entry, CompactionSessionEntry)
    )
    assert record.schema_version == 4
    assert record.reason == "threshold"
    assert record.trigger_budget == started.trigger_budget
    assert [message.content for message in session.read_context_messages()] == [
        f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\n{VALID_COMPACTION_SUMMARY}",
        "question two",
        "answer two",
    ]


def test_does_not_auto_compact_at_provider_limit(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=60, output_tokens=10, total_tokens=70),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(auto_compact_token_limit=80),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=10,
        )
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    estimated = next(event for event in events if isinstance(event, ContextEstimated))
    assert estimated.budget.reserve_tokens == 20
    assert not any(isinstance(event, CompactionStarted) for event in events)
    assert len(provider.calls) == 1
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_failure_preserves_prompt_success(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
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

    async def run() -> tuple[list[WispEvent], SessionStats]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        with anyio.fail_after(2):
            events = [event async for event in agent.run("question two", session=session)]
        return events, await agent.get_session_stats(session)

    events, stats = anyio.run(run)

    assert not any(isinstance(event, ErrorEvent) for event in events)
    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.reason == "threshold"
    assert completed.outcome == "failed"
    assert "blank" in (completed.error or "")
    assert completed.usage is not None
    assert completed.usage.total_tokens == 81
    assert completed.cost is not None
    assert any(
        event["type"] == "compaction.completed" and event.get("cost") is not None
        for event in session.read_events()
    )
    assert stats.cost.complete is False
    assert stats.cost.unpriced_record_count == 3
    assert events[-1].type == "agent.completed"
    assert events[-1].outcome == "completed"
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
    assert [message.content for message in session.read_context_messages()] == [
        "question one",
        "answer one",
        "question two",
        "answer two",
    ]


def test_failure_ignores_listener_failure(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ],
            [ProviderResponseStarted(model="model"), ProviderResponseCompleted(content=" ")],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    bus = EventBus()

    def fail_completion_publication(_event: WispEvent) -> None:
        raise RuntimeError("listener failed")

    bus.on("compaction.completed", fail_completion_publication)

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.outcome == "failed"
    assert "blank" in (completed.error or "")
    assert events[-1].type == "agent.completed"
    assert events[-1].outcome == "completed"


def test_failure_ignores_accounting_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content=VALID_COMPACTION_SUMMARY,
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        original_append_event = session.append_event

        async def fail_compaction_append(
            _entry: SessionEntry,
            *,
            expected_context_entry_ids: Sequence[str],
        ) -> SessionEntry:
            del expected_context_entry_ids
            raise OSError("compaction storage unavailable")

        async def fail_accounting_append(
            event: WispEvent,
            *,
            operation_id: str | None = None,
        ) -> SessionEntry:
            if isinstance(event, CompactionCompleted):
                raise OSError("accounting storage unavailable")
            return await original_append_event(event, operation_id=operation_id)

        monkeypatch.setattr(session, "append_compaction_entry", fail_compaction_append)
        monkeypatch.setattr(session, "append_event", fail_accounting_append)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.reason == "threshold"
    assert completed.outcome == "failed"
    assert completed.error == "compaction storage unavailable"
    assert completed.usage == TokenUsage(input_tokens=70, output_tokens=11, total_tokens=81)
    assert completed.cost is not None
    assert events[-1].type == "agent.completed"
    assert events[-1].outcome == "completed"
    assert not any(entry.kind == "compaction" for entry in session.read_entries())


def test_prepare_failure_preserves_prompt_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], int]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )

        async def fail_prepare(_session: JsonlSession) -> SessionReplay:
            agent._queue_message(
                session,
                Message(role="tool", content="interrupted", tool_call_id="call-1"),
            )

            async def fail_pending_flush() -> None:
                raise OSError("pending repair flush failed")

            monkeypatch.setattr(agent, "_flush_pending_entries", fail_pending_flush)
            raise OSError("pending repair flush failed")

        monkeypatch.setattr(agent, "_prepare_compaction_replay", fail_prepare)
        events = [event async for event in agent.run("question two", session=session)]
        return events, len(agent._pending_entries)

    events, pending_count = anyio.run(run)

    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.reason == "threshold"
    assert completed.outcome == "failed"
    assert completed.error == "pending repair flush failed"
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, SessionSaved) for event in events)
    assert pending_count == 1
    assert events[-1].type == "agent.completed"
    assert events[-1].outcome == "completed"


def test_read_failure_keeps_final_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )

        async def fail_prepare(_session: JsonlSession) -> SessionReplay:
            raise OSError("context replay failed")

        monkeypatch.setattr(agent, "_prepare_compaction_replay", fail_prepare)
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    failed_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, CompactionCompleted) and event.outcome == "failed"
    )
    saved_indexes = [index for index, event in enumerate(events) if isinstance(event, SessionSaved)]
    assert saved_indexes == [failed_index + 1]
    assert events[-1].type == "agent.completed"
    assert events[-1].outcome == "completed"


def test_reports_post_commit_publication_failure(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
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
    bus = EventBus()
    published: list[WispEvent] = []

    def fail_saved_publication(event: WispEvent) -> None:
        published.append(event)
        if isinstance(event, SessionSaved):
            raise RuntimeError("listener failed")

    bus.on("*", fail_saved_publication)

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, CompactionCompleted))
    assert completed.outcome == "completed"
    assert completed.error == "Event publication failed: listener failed"
    published_completion = next(
        event for event in published if isinstance(event, CompactionCompleted)
    )
    assert published_completion == completed
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert sum(entry.kind == "compaction" for entry in session.read_entries()) == 1
    assert events[-1].type == "agent.completed"
    assert events[-1].outcome == "completed"


@pytest.mark.parametrize(
    ("enabled", "context_window", "reserve_tokens"),
    [(False, 100, 20), (True, None, 20), (True, 1_000, 16_384)],
)
def test_skips_unusable_policy(
    tmp_path: Path,
    enabled: bool,
    context_window: int | None,
    reserve_tokens: int,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(context_window=context_window) if context_window else None,
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=reserve_tokens,
            auto_compaction_enabled=enabled,
        )
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    assert not any(isinstance(event, CompactionStarted | CompactionCompleted) for event in events)
    assert len(provider.calls) == 1


def test_skips_without_compactable_prefix(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer one",
                    usage=ProviderUsage(input_tokens=70, output_tokens=11, total_tokens=81),
                ),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    agent = CodingSession(
        provider=provider,
        sessions=store,
        model="model",
        models=build_model_registry(),
        prompt_messages=(Message(role="system", content="system"),),
        context_reserve_tokens=20,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("question one")]

    events = anyio.run(run)

    assert not any(isinstance(event, CompactionStarted | CompactionCompleted) for event in events)
    assert len(provider.calls) == 1


def test_falls_back_to_estimate(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer two " + "y" * 200),
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

    async def run() -> list[WispEvent]:
        await session.append_message(Message(role="user", content="question one " + "x" * 200))
        await session.append_message(
            Message(role="assistant", content="answer one " + "x" * 200, finish_reason="stop")
        )
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        return [event async for event in agent.run("question two " + "y" * 200, session=session)]

    events = anyio.run(run)

    started = next(event for event in events if isinstance(event, CompactionStarted))
    assert started.trigger_budget is not None
    assert started.trigger_budget.observed_tokens is None
    assert started.trigger_budget.estimate.total_tokens > 80


def test_uses_estimate_after_tool_round(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.txt"
    source.write_text("tool output", encoding="utf-8")
    call = ToolCall(call_id="call-1", name="read", arguments={"path": source.name})
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
                ProviderResponseCompleted(
                    content="answer two",
                    usage=ProviderUsage(input_tokens=890, output_tokens=11, total_tokens=901),
                ),
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
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            # The estimate now mirrors the actual structured active exchange,
            # including call arguments, rather than the old lossy narration.
            models=build_model_registry(context_window=1_200),
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        return [event async for event in agent.run("question two", session=session)]

    events = anyio.run(run)

    assert not any(isinstance(event, CompactionStarted | CompactionCompleted) for event in events)
    assert len(provider.calls) == 2


def test_cancellation_preserves_completed_turn(
    tmp_path: Path,
) -> None:
    summary_started = anyio.Event()
    provider = BlockingAutoCompactionProvider(summary_started)
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        emitted: list[WispEvent] = []
        bus = EventBus()
        bus.on("*", emitted.append)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        scope = anyio.CancelScope()

        async def consume() -> None:
            with scope:
                _events = [event async for event in agent.run("question two", session=session)]

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(consume)
            await summary_started.wait()
            scope.cancel()
        return emitted

    emitted = anyio.run(run)

    terminal = next(
        event
        for event in emitted
        if isinstance(event, CompactionCompleted) and event.reason == "threshold"
    )
    assert terminal.outcome == "cancelled"
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
    assert [message.content for message in session.read_context_messages()] == [
        "question one",
        "answer one",
        "question two",
        "answer two",
    ]


def test_overflow_recovery_cancellation_does_not_retry(tmp_path: Path) -> None:
    summary_started = anyio.Event()
    provider = BlockingOverflowRecoveryProvider(summary_started)
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        await _append_turn(session, "one")
        await _append_turn(session, "two")
        emitted: list[WispEvent] = []
        bus = EventBus()
        bus.on("*", emitted.append)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            events=bus,
            model="model",
            models=build_model_registry(),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=20,
        )
        scope = anyio.CancelScope()

        async def consume() -> None:
            with scope:
                _events = [event async for event in agent.run("question three", session=session)]

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(consume)
            await summary_started.wait()
            scope.cancel()
        return emitted

    emitted = anyio.run(run)

    terminal = next(
        event
        for event in emitted
        if isinstance(event, CompactionCompleted) and event.reason == "overflow"
    )
    assert terminal.outcome == "cancelled"
    assert terminal.will_retry is False
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
    assert provider.calls == 2
