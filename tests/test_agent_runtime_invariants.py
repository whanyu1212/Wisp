from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from tests.agent_runtime import (
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_terminals,
)
from wisp.agent.execution import PreparedToolExecution, ToolExecutionEvent, ToolPreparationEvent
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    MessageDelta,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


def _completed_turn(turn: int = 1) -> tuple[object, ...]:
    return (
        TurnStarted(turn=turn),
        TurnCompleted(turn=turn, outcome="completed", finish_reason="stop"),
    )


def _ended(call_id: str = "call-1") -> ToolExecutionEnded:
    return ToolExecutionEnded(
        call_id=call_id,
        name="lookup",
        output=f"output-{call_id}",
        is_error=False,
    )


def _ready(call_id: str = "call-1") -> ToolResultReady:
    return ToolResultReady.from_execution_ended(_ended(call_id))


def test_assert_turn_terminals_accepts_matched_starts_and_completes() -> None:
    assert_turn_terminals(_completed_turn())
    assert_turn_terminals((*_completed_turn(1), *_completed_turn(2)))


def test_assert_turn_terminals_accepts_unstarted_failure() -> None:
    assert_turn_terminals((ErrorEvent(message="boom"),))


def test_assert_turn_terminals_rejects_missing_terminal() -> None:
    with pytest.raises(AssertionError, match="started without a terminal"):
        assert_turn_terminals((TurnStarted(turn=1),))


def test_assert_turn_terminals_rejects_unstarted_complete() -> None:
    with pytest.raises(AssertionError, match="without a matching TurnStarted"):
        assert_turn_terminals((TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),))


def test_assert_turn_terminals_rejects_double_complete() -> None:
    with pytest.raises(AssertionError, match="completed more than once"):
        assert_turn_terminals(
            (
                *_completed_turn(),
                TurnCompleted(turn=1, outcome="failed", finish_reason="error"),
            )
        )


def test_assert_tool_result_pairing_accepts_adjacent_ended_and_ready() -> None:
    events = (*_completed_turn(), _ended(), _ready())
    assert_tool_result_pairing(events)
    assert_settled_tool_calls(events, ("call-1",))


def test_assert_tool_result_pairing_allows_requested_call_without_terminal() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
        ToolExecutionStarted(call_id="call-1", name="lookup", arguments={}),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)


def test_assert_tool_result_pairing_rejects_ready_without_ended() -> None:
    with pytest.raises(AssertionError, match="without ToolExecutionEnded"):
        assert_tool_result_pairing((_ready(),))


def test_assert_tool_result_pairing_rejects_ended_without_ready() -> None:
    with pytest.raises(AssertionError, match="without ToolResultReady"):
        assert_tool_result_pairing((_ended(),))


def test_assert_tool_result_pairing_allows_reused_call_id_across_rounds() -> None:
    events = (
        TurnStarted(turn=1),
        _ended("call-lookup-0"),
        _ready("call-lookup-0"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        _ended("call-lookup-0"),
        _ready("call-lookup-0"),
        TurnCompleted(turn=2, outcome="completed", finish_reason="tool_calls"),
    )
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)
    assert_settled_tool_calls(events, ("call-lookup-0",))


def test_assert_tool_result_pairing_rejects_duplicate_ended() -> None:
    with pytest.raises(AssertionError, match="appeared more than once"):
        assert_tool_result_pairing((_ended(), _ended(), _ready()))


def test_assert_tool_result_pairing_rejects_nonadjacent_projection() -> None:
    with pytest.raises(AssertionError, match="must immediately follow"):
        assert_tool_result_pairing((_ended(), ErrorEvent(message="gap"), _ready()))


def test_assert_settled_tool_calls_rejects_missing_listed_call() -> None:
    events = (*_completed_turn(), _ended("call-1"), _ready("call-1"))
    with pytest.raises(AssertionError, match="missing terminal tool results"):
        assert_settled_tool_calls(events, ("call-1", "call-2"))


def test_assert_settled_tool_calls_counts_reused_call_id_occurrences() -> None:
    events = (*_completed_turn(), _ended("call-lookup-0"), _ready("call-lookup-0"))
    with pytest.raises(AssertionError, match="missing terminal tool results"):
        assert_settled_tool_calls(events, ("call-lookup-0", "call-lookup-0"))


def test_assert_settled_tool_calls_accepts_matching_reused_occurrences() -> None:
    events = (
        TurnStarted(turn=1),
        _ended("call-lookup-0"),
        _ready("call-lookup-0"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        _ended("call-lookup-0"),
        _ready("call-lookup-0"),
        TurnCompleted(turn=2, outcome="completed", finish_reason="tool_calls"),
    )
    assert_settled_tool_calls(events, ("call-lookup-0", "call-lookup-0"))


class _NeverToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class _RecordingToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="tool output",
            is_error=False,
        )


class _BlockingPreparedExecutor:
    def __init__(self, all_started: anyio.Event) -> None:
        self._all_started = all_started
        self.started_call_ids: set[str] = set()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            self.started_call_ids.add(tool_call.call_id)
            if len(self.started_call_ids) == 2:
                self._all_started.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=run,
        )

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        async for event in self.prepare(tool_call):
            if isinstance(event, PreparedToolExecution):
                yield await event.run()
            else:
                yield event


class _BlockingProvider:
    name = "blocking"
    default_model: str | None = "blocking"

    def __init__(self, *, waiting: anyio.Event, release: anyio.Event) -> None:
        self.waiting = waiting
        self.release = release
        self.closed = False

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
        try:
            yield ProviderResponseStarted(model=model or self.default_model or self.name)
            yield ProviderTextDelta(delta="first")
            self.waiting.set()
            await self.release.wait()
        finally:
            self.closed = True
        yield ProviderResponseCompleted(content="too late")


def test_live_clean_turn_satisfies_turn_terminals() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=_NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)


def test_live_startless_provider_failure_settles_the_started_turn() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderRetrying(
                    attempt=2,
                    max_attempts=2,
                    delay_seconds=0.0,
                    reason="network",
                ),
                ProviderResponseFailed(message="request never opened"),
            ]
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=_NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)
    assert not any(event.type == "message.started" for event in events)
    assert [event.type for event in events[-2:]] == ["error", "turn.completed"]


def test_live_sequential_tool_round_pairs_ended_and_ready() -> None:
    call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(
                    content="done",
                    finish_reason="stop",
                    response_id="response-2",
                ),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=_RecordingToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)
    assert_settled_tool_calls(events, ("call-1",))


def test_live_harness_cancel_settles_the_started_turn() -> None:
    async def run() -> list[object]:
        provider = _BlockingProvider(waiting=anyio.Event(), release=anyio.Event())
        harness = AgentHarness(
            AgentHarnessConfig(provider=provider, tool_executor=_NeverToolExecutor())
        )
        events: list[object] = []
        with anyio.fail_after(1):
            async for event in harness.prompt("stop"):
                events.append(event)
                if isinstance(event, MessageDelta):
                    assert harness.cancel()
        return events

    events = anyio.run(run)
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)


def test_live_prepared_batch_cancel_settles_each_requested_call() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=calls,
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ]
        ]
    )

    async def run() -> list[object]:
        all_started = anyio.Event()
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=provider,
                tool_executor=_BlockingPreparedExecutor(all_started),
            )
        )
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await all_started.wait()
                assert harness.cancel()
        return events

    events = anyio.run(run)
    assert_turn_terminals(events)
    assert_settled_tool_calls(events, ("call-1", "call-2"))
