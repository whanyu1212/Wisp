from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from tests.support.agent_runtime import (
    assert_cancellation_settled,
    assert_continuation_invariants,
    assert_queue_ordering_invariants,
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_invariants,
    assert_turn_terminals,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolPreparationEvent,
)
from wisp.events import (
    MessageDelta,
    ToolExecutionEnded,
    TurnCompleted,
)
from wisp.messages import Message
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


def test_live_cancel_after_completed_turn_settles_cleanly() -> None:
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


def test_live_cancel_after_tool_finishes_settles_cleanly() -> None:
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


def test_live_truncated_tool_limit_keeps_continuation_valid() -> None:
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
