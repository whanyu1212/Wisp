from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from tests.agent_runtime import (
    assert_cancellation_settled,
    assert_continuation_invariants,
    assert_queue_ordering_invariants,
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_invariants,
    assert_turn_terminals,
)
from wisp.agent.execution import PreparedToolExecution, ToolExecutionEvent, ToolPreparationEvent
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    QueueMessageInjected,
    ToolCallRequested,
    ToolCallSnapshot,
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


def test_assert_tool_result_pairing_rejects_mismatched_payload() -> None:
    ended = _ended()
    ready = ToolResultReady(
        call_id=ended.call_id,
        name=ended.name,
        output="different-output",
        is_error=ended.is_error,
    )
    with pytest.raises(AssertionError, match="does not match"):
        assert_tool_result_pairing((ended, ready))


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


def test_assert_turn_invariants_accepts_clean_turn_sequence() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_non_1_initial_turn() -> None:
    events = (
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="expected turn 1"):
        assert_turn_invariants(events, initial_turn=1)


def test_assert_turn_invariants_rejects_turn_gap() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=3),
        TurnCompleted(turn=3, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="expected turn 2"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_invalid_outcome() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted.model_construct(turn=1, outcome="unknown", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="Invalid TurnCompleted outcome"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_cancelled_with_wrong_finish_reason() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="finish_reason 'cancelled'"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_completed_with_cancelled_finish_reason() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="cancelled"),
    )
    with pytest.raises(AssertionError, match="must not have finish_reason 'cancelled'"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_completed_with_error_finish_reason() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="error"),
    )
    with pytest.raises(AssertionError, match="must not have finish_reason 'error'"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_failed_with_non_error_finish_reason() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="failed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="Failed turn must have finish_reason 'error'"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_turn_activity_after_cancellation() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="appeared after turn 1 was cancelled"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_turn_event_after_final_turn_completed() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        _ended("call-1"),
    )
    with pytest.raises(AssertionError, match="appeared after final TurnCompleted"):
        assert_turn_invariants(events)


def test_assert_cancellation_settled_accepts_clean_cancellation() -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_cancellation_settled(events)


def test_assert_cancellation_settled_accepts_pre_turn_cancellation() -> None:
    events = (ErrorEvent(message="Agent run cancelled"),)
    assert_cancellation_settled(events)


def test_assert_cancellation_settled_accepts_error_after_completed_turn() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        ErrorEvent(message="Agent run cancelled"),
    )
    assert_cancellation_settled(events)


def test_assert_cancellation_settled_accepts_aborted_provider_error_wording() -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="request aborted"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_cancellation_settled(events)


def test_assert_cancellation_settled_allows_ended_without_ready() -> None:
    events = (
        TurnStarted(turn=1),
        _ended("call-1"),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    assert_cancellation_settled(events)


def test_assert_cancellation_settled_rejects_multiple_unpaired_ended() -> None:
    events = (
        TurnStarted(turn=1),
        _ended("call-1"),
        _ended("call-2"),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    with pytest.raises(AssertionError, match="without ToolResultReady"):
        assert_cancellation_settled(events)


def test_assert_turn_invariants_rejects_tool_call_requested_after_final_turn() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        ToolCallRequested(call_id="call-1", name="lookup", arguments={}),
    )
    with pytest.raises(AssertionError, match="appeared after final TurnCompleted"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_tool_event_before_first_turn() -> None:
    events = (
        _ended("call-1"),
        _ready("call-1"),
        *_completed_turn(),
    )
    with pytest.raises(AssertionError, match="appeared outside an active turn"):
        assert_turn_invariants(events)


def test_assert_turn_invariants_rejects_tool_event_between_turns() -> None:
    events = (
        *_completed_turn(1),
        _ended("call-1"),
        _ready("call-1"),
        *_completed_turn(2),
    )
    with pytest.raises(AssertionError, match="appeared outside an active turn"):
        assert_turn_invariants(events)


def test_assert_queue_ordering_invariants_rejects_cross_boundary_priority_reversal() -> None:
    events = (
        QueueMessageInjected(kind="follow_up", content="follow 1"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
        QueueMessageInjected(kind="steering", content="steer 2"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="Expected all 1 initial steering messages"):
        assert_queue_ordering_invariants(
            events,
            initial_steering_count=1,
            initial_follow_up_count=1,
        )


def test_assert_continuation_invariants_recognizes_truncated_tool_calls() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="read", arguments={}),
        _ended("call-1"),
        _ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="length"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_continuation_invariants(events)


def test_assert_continuation_invariants_rejects_dropped_call_in_multi_call_turn() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="read", arguments={}),
        ToolCallRequested(call_id="call-2", name="read", arguments={}),
        _ended("call-1"),
        _ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="missing terminal results for: \\['call-2'\\]"):
        assert_continuation_invariants(events)


def test_assert_continuation_invariants_reconciles_completed_and_requested_calls() -> None:
    events = (
        TurnStarted(turn=1),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="read", arguments={}),
                ToolCallSnapshot(call_id="call-2", name="read", arguments={}),
            ),
        ),
        ToolCallRequested(call_id="call-1", name="read", arguments={}),
        _ended("call-1"),
        _ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="missing terminal results for: \\['call-2'\\]"):
        assert_continuation_invariants(events)


def test_assert_continuation_invariants_allows_failed_tool_bearing_final_turn() -> None:
    events = (
        TurnStarted(turn=1),
        ToolCallRequested(call_id="call-1", name="read", arguments={}),
        _ended("call-1"),
        _ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="length"),
        TurnStarted(turn=2),
        MessageCompleted(
            turn=2,
            content="",
            finish_reason="length",
            tool_calls=(ToolCallSnapshot(call_id="call-2", name="read", arguments={}),),
        ),
        TurnCompleted(turn=2, outcome="failed", finish_reason="error"),
    )
    assert_continuation_invariants(events)


def test_assert_cancellation_settled_rejects_missing_error_event() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="cancelled", finish_reason="cancelled"),
    )
    with pytest.raises(AssertionError, match="ErrorEvent before completion"):
        assert_cancellation_settled(events)


def test_assert_cancellation_settled_rejects_completed_outcome() -> None:
    events = (
        TurnStarted(turn=1),
        ErrorEvent(message="Agent run cancelled"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="outcome 'cancelled'"):
        assert_cancellation_settled(events)


def test_assert_turn_invariants_accepts_nonzero_initial_turn() -> None:
    events = (
        TurnStarted(turn=5),
        TurnCompleted(turn=5, outcome="completed", finish_reason="stop"),
    )
    assert_turn_invariants(events)
    assert_turn_invariants(events, initial_turn=5)


def test_assert_turn_invariants_accepts_context_overflow_retry_after_failed_turn() -> None:
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="failed", finish_reason="error"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_turn_invariants(events)


def test_assert_queue_ordering_invariants_accepts_steering_before_follow_up() -> None:
    events = (
        QueueMessageInjected(kind="steering", content="steer text"),
        QueueMessageInjected(kind="follow_up", content="follow text"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    assert_queue_ordering_invariants(events)


def test_assert_queue_ordering_invariants_rejects_follow_up_before_steering() -> None:
    events = (
        QueueMessageInjected(kind="follow_up", content="follow text"),
        QueueMessageInjected(kind="steering", content="steer text"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="[Ss]teering injection.*appeared after follow-up"):
        assert_queue_ordering_invariants(events)


def test_assert_queue_ordering_invariants_rejects_injection_during_active_turn() -> None:
    events = (
        TurnStarted(turn=1),
        QueueMessageInjected(kind="steering", content="in flight"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="appeared inside an active turn"):
        assert_queue_ordering_invariants(events)


def test_assert_queue_ordering_invariants_rejects_within_kind_reorder() -> None:
    events = (
        QueueMessageInjected(kind="steering", content="steer 2"),
        QueueMessageInjected(kind="steering", content="steer 1"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="expected FIFO order"):
        assert_queue_ordering_invariants(events, expected_steering=("steer 1", "steer 2"))


def test_assert_queue_ordering_invariants_accepts_matching_fifo_snapshot() -> None:
    events = (
        QueueMessageInjected(kind="steering", content="steer 1"),
        QueueMessageInjected(kind="steering", content="steer 2"),
        QueueMessageInjected(kind="follow_up", content="follow 1"),
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="stop"),
    )
    assert_queue_ordering_invariants(
        events,
        expected_steering=("steer 1", "steer 2"),
        expected_follow_up=("follow 1",),
    )


def test_assert_continuation_invariants_accepts_tool_continuation() -> None:
    events = (
        TurnStarted(turn=1),
        _ended("call-1"),
        _ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    assert_continuation_invariants(events)


def test_assert_continuation_invariants_rejects_uncompleted_tool_calls_at_final_turn() -> None:
    events = (
        TurnStarted(turn=1),
        _ended("call-1"),
        _ready("call-1"),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
    )
    with pytest.raises(AssertionError, match="no subsequent continuation turn"):
        assert_continuation_invariants(events)


def test_assert_continuation_invariants_rejects_tool_call_without_results_before_next_turn() -> (
    None
):
    events = (
        TurnStarted(turn=1),
        TurnCompleted(turn=1, outcome="completed", finish_reason="tool_calls"),
        TurnStarted(turn=2),
        TurnCompleted(turn=2, outcome="completed", finish_reason="stop"),
    )
    with pytest.raises(AssertionError, match="had no tool execution within that turn"):
        assert_continuation_invariants(events)


def test_live_multi_turn_continuation_satisfies_all_invariants() -> None:
    call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="resp-1"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                    response_id="resp-1",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="resp-2"),
                ProviderTextDelta(delta="all done"),
                ProviderResponseCompleted(
                    content="all done",
                    finish_reason="stop",
                    response_id="resp-2",
                ),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=_RecordingToolExecutor()),
                messages=(Message(role="user", content="lookup something"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_tool_result_pairing(events)
    assert_continuation_invariants(events)
    assert_settled_tool_calls(events, ("call-1",))


def test_live_cancellation_settlement_satisfies_invariants() -> None:
    provider = _BlockingProvider(waiting=anyio.Event(), release=anyio.Event())
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, tool_executor=_NeverToolExecutor())
    )

    async def run() -> list[object]:
        events: list[object] = []
        with anyio.fail_after(1):
            async for event in harness.prompt("stop"):
                events.append(event)
                if isinstance(event, MessageDelta):
                    assert harness.cancel()
        return events

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_cancellation_settled(events)


def test_live_cancel_after_completed_turn_satisfies_cancellation_invariants() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="answer"),
            ]
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, tool_executor=_NeverToolExecutor())
    )
    harness.steer("do not inject")

    async def run() -> list[object]:
        events: list[object] = []
        async for event in harness.prompt("initial"):
            events.append(event)
            if isinstance(event, TurnCompleted):
                assert harness.cancel()
        return events

    events = anyio.run(run)
    assert events[-1].type == "error"
    assert_turn_invariants(events)
    assert_cancellation_settled(events)


def test_live_queue_drain_satisfies_ordering_invariants() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="first response"),
                ProviderResponseCompleted(content="first response"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="steered response"),
                ProviderResponseCompleted(content="steered response"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="follow-up response"),
                ProviderResponseCompleted(content="follow-up response"),
            ],
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, tool_executor=_NeverToolExecutor())
    )

    async def run() -> list[object]:
        events: list[object] = []
        harness.follow_up("queued follow-up")
        harness.steer("queued steering")
        async for event in harness.prompt("initial prompt"):
            events.append(event)
        return events

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_queue_ordering_invariants(
        events,
        initial_steering_count=1,
        initial_follow_up_count=1,
        expected_steering=("queued steering",),
        expected_follow_up=("queued follow-up",),
    )


def test_live_aborted_provider_satisfies_cancellation_invariants() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseFailed(message="request aborted", failure_kind="aborted"),
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
    assert_turn_invariants(events)
    assert_cancellation_settled(events)


def test_live_cancel_after_tool_execution_end_satisfies_cancellation_invariants() -> None:
    call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, tool_executor=_RecordingToolExecutor())
    )

    async def run() -> list[object]:
        events: list[object] = []
        async for event in harness.prompt("initial"):
            events.append(event)
            if isinstance(event, ToolExecutionEnded):
                assert harness.cancel()
        return events

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_cancellation_settled(events)


def test_live_failed_truncated_tool_limit_satisfies_continuation_invariants() -> None:
    first = ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"})
    second = ToolCall(call_id="call-2", name="read", arguments={"path": "two.txt"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=first),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(first,),
                    finish_reason="length",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=second),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(second,),
                    finish_reason="length",
                ),
            ],
        ]
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="Maximum tool iterations exceeded: 1"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=_NeverToolExecutor(),
                    max_tool_iterations=1,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_continuation_invariants(events)
