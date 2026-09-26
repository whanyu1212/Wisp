from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from tests.coding.compaction_support import (
    VALID_COMPACTION_SUMMARY,
    GatedPreflightSummaryProvider,
    _append_turn,
    build_model_registry,
)
from wisp.agent.context_budget import build_context_budget, estimate_context
from wisp.agent.messages import Message
from wisp.coding.compaction import (
    exceeds_provider_auto_compaction_limit,
    matches_provider_suffix,
    provider_auto_compaction_excess_tokens,
)
from wisp.coding.session import CodingSession
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ErrorEvent,
    MessageCompleted,
    QueueMessageInjected,
    ToolCallSnapshot,
    ToolExecutionEnded,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)
from wisp.providers.base import ContextOverflowError
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
)
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


@pytest.mark.parametrize(
    ("history_chars", "prompt_chars", "history_exceeds_limit"),
    [
        pytest.param(1_600, 0, True, id="resumed-history"),
        pytest.param(1_500, 300, False, id="prompt-inclusive-history"),
    ],
)
def test_coding_session_compacts_before_provider_limit_request(
    tmp_path: Path,
    history_chars: int,
    prompt_chars: int,
    history_exceeds_limit: bool,
) -> None:
    context_window = 2_000
    compaction_limit = 1_600
    first_user = Message(role="user", content="question one " + "a" * history_chars)
    first_assistant = Message(
        role="assistant",
        content="answer one " + "b" * history_chars,
        finish_reason="stop",
    )
    second_user = Message(role="user", content="question two " + "c" * history_chars)
    second_assistant = Message(
        role="assistant",
        content="answer two " + "d" * history_chars,
        finish_reason="stop",
    )
    prompt = "question three " + "e" * prompt_chars
    prompt_messages = (Message(role="system", content="system"),)
    history = (first_user, first_assistant, second_user, second_assistant)
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="answer three",
                    usage=ProviderUsage(input_tokens=900, output_tokens=100, total_tokens=1_000),
                ),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], tuple[str, str], int]:
        first = await session.append_message(first_user)
        second = await session.append_message(first_assistant)
        await session.append_message(second_user)
        await session.append_message(second_assistant)
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=context_window,
                auto_compact_token_limit=compaction_limit,
            ),
            prompt_messages=prompt_messages,
            context_reserve_tokens=100,
        )
        events = [
            event
            async for event in agent.run(
                prompt,
                session=session,
                history=history,
                operation_id="prompt-1",
            )
        ]
        return events, (first.id, second.id), entry_start

    events, first_turn_ids, entry_start = anyio.run(run)

    history_budget = estimate_context((*prompt_messages, *history))
    assert (history_budget.total_tokens > compaction_limit) is history_exceeds_limit
    started_index = next(
        index for index, event in enumerate(events) if isinstance(event, CompactionStarted)
    )
    turn_started_index = next(
        index for index, event in enumerate(events) if isinstance(event, TurnStarted)
    )
    assert started_index < turn_started_index
    started = events[started_index]
    assert isinstance(started, CompactionStarted)
    assert started.reason == "threshold"
    assert started.trigger_budget is not None
    assert started.trigger_budget.estimate.total_tokens > compaction_limit
    assert started.trigger_budget.reserve_tokens == context_window - compaction_limit
    record = next(
        entry.compaction
        for entry in session.read_entries()
        if isinstance(entry, CompactionSessionEntry)
    )
    assert record.replaced_entry_ids == first_turn_ids
    assert all(entry.operation_id == "prompt-1" for entry in session.read_entries()[entry_start:])
    assert len(provider.calls) == 2
    assert provider.calls[0].messages[0].content.startswith("Create a concise")
    request = provider.calls[1]
    assert request.messages[-1].content == prompt
    assert first_user.content not in {message.content for message in request.messages}
    assert not any(isinstance(event, ErrorEvent) for event in events)


def test_coding_session_preflight_compacts_one_completed_turn(tmp_path: Path) -> None:
    context_window = 4_000
    compaction_limit = 1_600
    first_user = Message(role="user", content="question one " + "a" * 3_500)
    first_assistant = Message(
        role="assistant",
        content="answer one " + "b" * 3_500,
        finish_reason="stop",
    )
    history = (first_user, first_assistant)
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer two"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> tuple[list[WispEvent], tuple[str, str]]:
        user_entry = await session.append_message(first_user)
        assistant_entry = await session.append_message(first_assistant)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=context_window,
                auto_compact_token_limit=compaction_limit,
            ),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        events = [
            event async for event in agent.run("question two", session=session, history=history)
        ]
        return events, (user_entry.id, assistant_entry.id)

    events, first_turn_ids = anyio.run(run)

    started = next(event for event in events if isinstance(event, CompactionStarted))
    assert started.trigger_budget is not None
    assert started.trigger_budget.estimate.total_tokens > compaction_limit
    record = next(
        entry.compaction
        for entry in session.read_entries()
        if isinstance(entry, CompactionSessionEntry)
    )
    assert record.replaced_entry_ids == first_turn_ids
    assert len(provider.calls) == 2
    assert provider.calls[1].messages[-1].content == "question two"


def test_coding_session_rechecks_limit_after_preflight_steering(tmp_path: Path) -> None:
    summary_started = anyio.Event()
    release_summary = anyio.Event()
    provider = GatedPreflightSummaryProvider(summary_started, release_summary)
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    history = (
        Message(role="user", content="question one " + "a" * 3_500),
        Message(
            role="assistant",
            content="answer one " + "b" * 3_500,
            finish_reason="stop",
        ),
    )

    async def run() -> None:
        for message in history:
            await session.append_message(message)
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=4_000,
                auto_compact_token_limit=1_600,
            ),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )

        events: list[WispEvent] = []

        async def consume() -> None:
            with pytest.raises(ContextOverflowError, match="prompt and steering exceed"):
                async for event in agent.run(
                    "question two",
                    session=session,
                    history=history,
                    operation_id="prompt-1",
                ):
                    events.append(event)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(consume)
            await summary_started.wait()
            await agent.steer("x" * 8_000)
            release_summary.set()

        injected = next(event for event in events if isinstance(event, QueueMessageInjected))
        persisted = next(
            entry
            for entry in session.read_entries()
            if isinstance(entry, MessageSessionEntry) and entry.id == injected.message_entry_id
        )
        assert persisted.message.content == "x" * 8_000
        assert session.read_context_messages() == history
        assert all(
            entry.operation_id == "prompt-1" for entry in session.read_entries()[entry_start:]
        )

    anyio.run(run)

    # The only provider call is the initial compaction summary. The oversized
    # steering is rejected after injection and before the agent turn begins.
    assert len(provider.calls) == 1
    assert provider.calls[0][0].content.startswith("Create a concise")


def test_coding_session_rejects_oversized_fresh_prompt_before_provider(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([], default_model="model")
    store = JsonlSessionStore(tmp_path)
    agent = CodingSession(
        provider=provider,
        sessions=store,
        model="model",
        models=build_model_registry(
            context_window=2_000,
            auto_compact_token_limit=1_600,
        ),
        prompt_messages=(Message(role="system", content="system"),),
        context_reserve_tokens=100,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("x" * 8_000)]

    with pytest.raises(ContextOverflowError, match="Active prompt exceeds"):
        anyio.run(run)
    assert provider.calls == []
    assert store.latest().read_context_messages() == ()


def test_coding_session_rejects_provider_reserve_that_consumes_window(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([], default_model="model")
    store = JsonlSessionStore(tmp_path)
    agent = CodingSession(
        provider=provider,
        sessions=store,
        model="model",
        models=build_model_registry(
            context_window=2_000,
            auto_compact_token_limit=1_600,
        ),
        prompt_messages=(Message(role="system", content="system"),),
        context_reserve_tokens=2_000,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("small prompt")]

    with pytest.raises(ContextOverflowError, match="Active prompt exceeds"):
        anyio.run(run)
    assert provider.calls == []
    assert store.latest().read_context_messages() == ()


def test_coding_session_stops_when_prompt_remains_over_provider_limit(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ]
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    history = (
        Message(role="user", content="question one"),
        Message(role="assistant", content="answer one", finish_reason="stop"),
    )

    async def run() -> list[WispEvent]:
        for message in history:
            await session.append_message(message)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=2_000,
                auto_compact_token_limit=1_600,
            ),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        return [
            event
            async for event in agent.run(
                "question two " + "x" * 8_000,
                session=session,
                history=history,
            )
        ]

    with pytest.raises(ContextOverflowError, match="Active prompt exceeds"):
        anyio.run(run)
    assert len(provider.calls) == 1
    assert session.read_context_messages() == history


def test_coding_session_recovers_a_session_resumed_after_a_crashed_oversized_tool_turn(
    tmp_path: Path,
) -> None:
    """A crash mid-turn persists the full, untruncated tool result to disk before
    any in-memory recovery can run, and leaves that turn without a closing
    assistant message — so it can never be summarized away by compaction (which
    only replaces *complete* turns). Resuming with a new prompt must not
    immediately re-hit the same overflow: the preflight budget check has to
    truncate that stuck turn's tool result the same way the tool-round path
    truncates an oversized active-turn result, or every future prompt in the
    session fails identically, forever.
    """

    call = ToolCallSnapshot(call_id="call-1", name="huge_read", arguments={})
    crashed_turn = (
        Message(role="user", content="read the huge file"),
        Message(role="assistant", content="", tool_calls=(call,), finish_reason="tool_calls"),
        Message(
            role="tool",
            content="x" * 8_000,
            tool_name="huge_read",
            tool_call_id="call-1",
        ),
        # No closing assistant message: the process crashed here.
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer after resume"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def run() -> list[WispEvent]:
        for message in crashed_turn:
            await session.append_message(message)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=2_000,
                auto_compact_token_limit=1_600,
            ),
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        return [
            event
            async for event in agent.run(
                "continue",
                session=session,
                history=crashed_turn,
            )
        ]

    events = anyio.run(run)

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(provider.calls) == 1
    assert any(
        isinstance(event, MessageCompleted) and event.content == "answer after resume"
        for event in events
    )


def test_matches_provider_suffix_ignores_persistence_only_metadata() -> None:
    call = ToolCallSnapshot(call_id="call-1", name="read", arguments={"path": "a.py"})
    persisted = (
        Message(role="user", content="hello"),
        Message(role="assistant", content="", tool_calls=(call,)),
        Message(role="tool", content="ok", tool_call_id="call-1", tool_name="read"),
    )
    live_call = ToolCallSnapshot(
        call_id="call-1", name="read", arguments={"path": "a.py"}, provider_call_id="p-1"
    )
    live = (
        Message(role="assistant", content="", tool_calls=(live_call,)),
        Message(role="tool", content="ok", tool_call_id="call-1", tool_name="read"),
    )

    assert matches_provider_suffix(persisted, live)
    assert matches_provider_suffix(persisted, ())
    assert not matches_provider_suffix(persisted[:1], live)
    assert not matches_provider_suffix(
        persisted,
        (live[0], Message(role="tool", content="changed", tool_call_id="call-1", tool_name="read")),
    )


def test_provider_auto_compaction_limit_helpers_distinguish_unfixable_reserve() -> None:
    estimate = estimate_context((Message(role="user", content="x" * 4_000),))
    tokens = estimate.total_tokens
    over = build_context_budget(estimate, context_window=tokens + 100, reserve_tokens=200)
    under = build_context_budget(estimate, context_window=tokens + 300, reserve_tokens=200)
    unfixable = build_context_budget(estimate, context_window=1000, reserve_tokens=1000)
    unknown = build_context_budget(estimate, context_window=None, reserve_tokens=200)

    assert exceeds_provider_auto_compaction_limit(over)
    assert not exceeds_provider_auto_compaction_limit(under)
    # An unfixable reserve counts as over the limit but has no truncatable excess.
    assert exceeds_provider_auto_compaction_limit(unfixable)
    assert not exceeds_provider_auto_compaction_limit(unknown)

    assert provider_auto_compaction_excess_tokens(over) == 100
    assert provider_auto_compaction_excess_tokens(under) is None
    assert provider_auto_compaction_excess_tokens(unfixable) is None
    assert provider_auto_compaction_excess_tokens(unknown) is None


def test_coding_session_rechecks_provider_limit_after_tool_round(tmp_path: Path) -> None:
    class LargeReadTool:
        name = "large_read"
        safety = "read"
        description = "Return a large deterministic result."
        input_schema = {"type": "object", "properties": {}}

        async def run(self, arguments: object, context: ToolContext) -> ToolResult:
            del arguments, context
            return ToolResult(text="x" * 3_000)

    call = ToolCall(call_id="call-1", name="large_read", arguments={})
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
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer after tool"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    registry = ToolRegistry()
    registry.register(LargeReadTool())
    first_user = Message(role="user", content="question one " + "a" * 2_000)
    first_assistant = Message(
        role="assistant",
        content="answer one " + "b" * 2_000,
        finish_reason="stop",
    )
    history = (first_user, first_assistant)

    async def run() -> list[WispEvent]:
        await session.append_message(first_user)
        await session.append_message(first_assistant)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=2_000,
                auto_compact_token_limit=1_600,
            ),
            tool_registry=registry,
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        return [
            event async for event in agent.run("question two", session=session, history=history)
        ]

    events = anyio.run(run)

    tool_end = next(
        index for index, event in enumerate(events) if isinstance(event, ToolExecutionEnded)
    )
    compaction_start = next(
        index for index, event in enumerate(events) if isinstance(event, CompactionStarted)
    )
    assert tool_end < compaction_start
    assert len(provider.calls) == 3
    assert provider.calls[1].messages[0].content.startswith("Create a concise")
    final_request = provider.calls[2]
    # The tool call/result pair belongs to the turn still running when compaction
    # fired, so it must survive the mid-turn transcript rebuild as the model's own
    # structured tool call and paired result — not be narrated as historical
    # observation text the model has no record of having produced.
    assert final_request.messages[-1].role == "tool"
    assert final_request.messages[-1].content == "x" * 3_000
    assert not any(isinstance(event, ErrorEvent) for event in events)


def test_coding_session_does_not_segment_tool_round_when_auto_compaction_disabled(
    tmp_path: Path,
) -> None:
    class ReadTool:
        name = "read_value"
        safety = "read"
        description = "Return a value."
        input_schema = {"type": "object", "properties": {}}

        async def run(self, arguments: object, context: ToolContext) -> ToolResult:
            del arguments, context
            return ToolResult(text="value")

    call = ToolCall(call_id="call-1", name="read_value", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="", tool_calls=(call,), finish_reason="tool_calls"
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="done"),
            ],
        ],
        default_model="model",
    )
    registry = ToolRegistry()
    registry.register(ReadTool())
    store = JsonlSessionStore(tmp_path)

    async def run() -> list[WispEvent]:
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(auto_compact_token_limit=80),
            tool_registry=registry,
            auto_compaction_enabled=False,
        )
        return [event async for event in agent.run("question")]

    events = anyio.run(run)

    assert len(provider.calls) == 2
    assert provider.calls[1].tool_results[0].output == "value"
    assert not any(isinstance(event, CompactionStarted) for event in events)


@pytest.mark.parametrize("operation_id", [None, "prompt-1"])
def test_coding_session_stops_when_truncation_cannot_shrink_active_turn_further(
    tmp_path: Path,
    operation_id: str | None,
) -> None:
    """When every tool result in the active turn is already at (or below) the
    truncation floor, ``_recover_via_tool_result_truncation`` can make no further
    progress and the terminal ``ContextOverflowError`` is the only remaining option.

    This exercises the same shape as
    ``test_coding_session_recovers_when_active_tool_turn_remains_over_provider_limit``
    but with a reserve so tight that no single tool result carries enough
    reclaimable content — a config genuinely irreducible by truncation, distinct
    from the ``reserve_tokens >= context_window`` case covered by
    ``test_coding_session_rejects_provider_reserve_that_consumes_window``.
    """

    class TinyReadTool:
        name = "tiny_read"
        safety = "read"
        description = "Return a small result that cannot be truncated further."
        input_schema = {"type": "object", "properties": {}}

        async def run(self, arguments: object, context: ToolContext) -> ToolResult:
            del arguments, context
            return ToolResult(text="x" * 50)

    call = ToolCall(call_id="call-1", name="tiny_read", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="", tool_calls=(call,), finish_reason="tool_calls"
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="recovered"),
            ],
        ],
        default_model="model",
    )
    registry = ToolRegistry()
    registry.register(TinyReadTool())
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    agent = CodingSession(
        provider=provider,
        sessions=store,
        model="model",
        models=build_model_registry(
            context_window=100,
            auto_compact_token_limit=80,
        ),
        tool_registry=registry,
        prompt_messages=(Message(role="system", content="system"),),
        context_reserve_tokens=1,
    )
    first_events: list[WispEvent] = []

    async def run_first() -> None:
        async for event in agent.run("question", session=session, operation_id=operation_id):
            first_events.append(event)

    async def run_retry() -> list[WispEvent]:
        return [
            event
            async for event in agent.run(
                "retry",
                session=session,
                operation_id="retry-1" if operation_id is not None else None,
            )
        ]

    with pytest.raises(ContextOverflowError, match="Active tool result exceeds"):
        anyio.run(run_first)

    # The overflow is raised at the request boundary after the tool turn
    # completed, so it must not publish a conflicting failed terminal event.
    assert [
        (event.turn, event.outcome) for event in first_events if isinstance(event, TurnCompleted)
    ] == [(1, "completed")]
    assert not session.read_context_messages()
    # The rollback runs before the loop publishes the overflow, so its records land
    # on the restored branch that later prompts continue from.
    active_event_types = [
        entry.event.payload["type"]
        for entry in session.read_active_path()
        if isinstance(entry, EventSessionEntry)
    ]
    assert active_event_types[-2:] == ["context.overflow", "error"]

    retry_events = anyio.run(run_retry)
    assert len(provider.calls) == 2
    assert provider.calls[1].messages[-1].content == "retry"
    assert not any(isinstance(event, ErrorEvent) for event in retry_events)


def test_threshold_compaction_rebases_tool_state_with_injected_steering(
    tmp_path: Path,
) -> None:
    """Opaque replay providers retain tool state and append steering once."""

    class BlockingReadTool:
        name = "blocking_read"
        safety = "read"
        description = "Return a large result after steering has been queued."
        input_schema = {"type": "object", "properties": {}}

        def __init__(self, started: anyio.Event, release: anyio.Event) -> None:
            self._started = started
            self._release = release

        async def run(self, arguments: object, context: ToolContext) -> ToolResult:
            del arguments, context
            self._started.set()
            await self._release.wait()
            return ToolResult(text="x" * 6_000)

    class OpaqueRebaseProvider(ScriptedProvider):
        def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
            del effort
            return False

    started = anyio.Event()
    release = anyio.Event()
    call = ToolCall(
        call_id="call-1",
        name="blocking_read",
        arguments={},
        provider_call_id="opaque-provider-call-1",
    )
    provider = OpaqueRebaseProvider(
        [
            [
                ProviderResponseStarted(model="model", response_id="tool-response"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    response_id="tool-response",
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer after steering"),
            ],
        ],
        default_model="model",
    )
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    registry = ToolRegistry()
    registry.register(BlockingReadTool(started, release))
    agent = CodingSession(
        provider=provider,
        sessions=store,
        model="model",
        models=build_model_registry(context_window=10_000, auto_compact_token_limit=1_000),
        tool_registry=registry,
        prompt_messages=(Message(role="system", content="system"),),
        context_reserve_tokens=100,
    )
    events: list[WispEvent] = []

    async def consume() -> None:
        events.extend([event async for event in agent.run("question two", session=session)])

    async def run() -> None:
        await _append_turn(session, "one")
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(consume)
            await started.wait()
            await agent.steer("change direction")
            release.set()

    anyio.run(run)

    assert any(
        isinstance(event, CompactionCompleted)
        and event.reason == "threshold"
        and event.outcome == "completed"
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert provider.calls[2].previous_response_id == "tool-response"
    assert provider.calls[2].tool_results[0].call_id == "call-1"
    assert [message.content for message in provider.calls[2].extra_messages] == ["change direction"]


def test_coding_session_recovers_when_active_tool_turn_remains_over_provider_limit(
    tmp_path: Path,
) -> None:
    """Once history is fully compacted, an active-turn tool result that still exceeds
    the provider's auto-compaction limit must be truncated and the turn allowed to
    continue — not rolled back and raised as a terminal ``ContextOverflowError``.

    This exercises the same "irreducibly large single tool result" shape as
    ``test_coding_session_stops_when_active_tool_turn_remains_over_provider_limit``
    above, but asserts the fixed (recoverable) outcome. Currently fails: the session
    still rolls back and raises.
    """

    class HugeReadTool:
        name = "huge_read"
        safety = "read"
        description = "Return an irreducibly large result."
        input_schema = {"type": "object", "properties": {}}

        async def run(self, arguments: object, context: ToolContext) -> ToolResult:
            del arguments, context
            return ToolResult(text="x" * 8_000)

    call = ToolCall(call_id="call-1", name="huge_read", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="", tool_calls=(call,), finish_reason="tool_calls"
                ),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
            ],
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(content="answer after truncation"),
            ],
        ],
        default_model="model",
    )
    registry = ToolRegistry()
    registry.register(HugeReadTool())
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    history = (
        Message(role="user", content="question one " + "a" * 2_000),
        Message(
            role="assistant",
            content="answer one " + "b" * 2_000,
            finish_reason="stop",
        ),
    )

    async def run() -> list[WispEvent]:
        for message in history:
            await session.append_message(message)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=build_model_registry(
                context_window=2_000,
                auto_compact_token_limit=1_600,
            ),
            tool_registry=registry,
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        return [
            event async for event in agent.run("question two", session=session, history=history)
        ]

    events = anyio.run(run)

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(provider.calls) == 3
    assert any(
        isinstance(event, MessageCompleted) and event.content == "answer after truncation"
        for event in events
    )
