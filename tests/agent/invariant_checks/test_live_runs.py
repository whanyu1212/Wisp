"""Real loop and harness runs that must satisfy every runtime invariant check."""

from __future__ import annotations

import anyio

from tests.agent.harness.support import RecordingToolExecutor
from tests.agent.loop.support import NeverToolExecutor, completed_stream, tool_call_stream
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
from wisp.messages import Message
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


def test_startless_failure_settles_turn() -> None:
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
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)
    assert not any(event.type == "message.started" for event in events)
    assert [event.type for event in events[-2:]] == ["error", "turn.completed"]


def test_multi_turn_continuation_satisfies_invariants() -> None:
    call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            tool_call_stream(call, response_id="resp-1"),
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
                AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
                messages=(Message(role="user", content="lookup something"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_tool_result_pairing(events)
    assert_continuation_invariants(events)
    assert_settled_tool_calls(events, ("call-1",))


def test_queue_drain_satisfies_ordering_invariants() -> None:
    provider = ScriptedProvider(
        [
            completed_stream("first response", response_id=None),
            completed_stream("steered response", response_id=None),
            completed_stream("follow-up response", response_id=None),
        ]
    )
    harness = AgentHarness(AgentHarnessConfig(provider=provider, tool_executor=NeverToolExecutor()))

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


def test_aborted_provider_satisfies_cancellation_invariants() -> None:
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
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)
    assert_turn_invariants(events)
    assert_cancellation_settled(events)
