"""Exercise each emitted boundary of small, deterministic harness workflows."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import anyio
import pytest

from tests.agent_runtime import (
    assert_cancellation_settled,
    assert_continuation_invariants,
    assert_queue_ordering_invariants,
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_invariants,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig, AgentHarnessEvent, QueuedMessages
from wisp.agent.harness.boundaries import HarnessBoundaryContext, HarnessBoundaryPreparer
from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    RequestBoundaryDecision,
    RequestBoundaryUnsupportedError,
    RequestContextRebase,
)
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolPreparationEvent,
)
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    QueueMessageInjected,
    ToolCallRequested,
    ToolExecutionEnded,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider

type Scenario = Literal["stream", "sequential", "parallel", "queues", "replacement", "rebase"]
_SCENARIOS: tuple[Scenario, ...] = (
    "stream",
    "sequential",
    "parallel",
    "queues",
    "replacement",
    "rebase",
)
_TOOL = ToolSpec(name="read", description="Fixture read", input_schema={"type": "object"})
_CALL_IDS = ("call-1", "call-2")


class _ReplayProvider(ScriptedProvider):
    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        return True


def _response(text: str, *, calls: Sequence[ToolCall] = ()) -> tuple[ProviderEvent, ...]:
    return (
        ProviderResponseStarted(model="fixture", response_id=text),
        ProviderTextDelta(delta=text),
        ProviderTextDelta(delta="."),
        *(ProviderToolCallCompleted(tool_call=call) for call in calls),
        ProviderResponseCompleted(
            content=f"{text}.",
            response_id=text,
            tool_calls=tuple(calls),
            finish_reason="tool_calls" if calls else "stop",
        ),
    )


class _SequentialExecutor:
    def __init__(self) -> None:
        self.finished: list[str] = []

    def result(self, call: ToolCall) -> ToolExecutionEnded:
        self.finished.append(call.call_id)
        return ToolExecutionEnded(
            call_id=call.call_id, name=call.name, output=f"read {call.call_id}", is_error=False
        )

    async def execute(self, call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield self.result(call)


class _ParallelExecutor(_SequentialExecutor):
    """Require overlap and finish in reverse order without depending on timing."""

    def __init__(self) -> None:
        super().__init__()
        self.started: set[str] = set()
        self.both_started = anyio.Event()
        self.second_finished = anyio.Event()

    async def prepare(self, call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            self.started.add(call.call_id)
            if len(self.started) == 2:
                self.both_started.set()
            await self.both_started.wait()
            if call.call_id == "call-1":
                await self.second_finished.wait()
            result = self.result(call)
            if call.call_id == "call-2":
                self.second_finished.set()
            return result

        yield PreparedToolExecution(
            call_id=call.call_id, name=call.name, parallel_safe=True, runner=run
        )


@dataclass
class _ContextTransition:
    mode: Literal["replacement", "rebase"]

    async def prepare_boundary(
        self, *, context: HarnessBoundaryContext
    ) -> RequestBoundaryDecision | None:
        if context.snapshot.turn != 1:
            return None
        base = (Message(role="system", content="compacted base"),)
        if self.mode == "replacement":
            return RequestBoundaryDecision(messages=base)
        return RequestBoundaryDecision(
            context_rebase=RequestContextRebase(
                base_messages=base,
                expected_continuation_messages=context.snapshot.continuation_messages,
            )
        )


@dataclass
class _Workflow:
    harness: AgentHarness
    provider: _ReplayProvider
    executor: _SequentialExecutor
    boundary: HarnessBoundaryPreparer | None
    initial_queues: QueuedMessages

    def start(self) -> AsyncGenerator[AgentHarnessEvent, None]:
        return self.harness.prompt("task", boundary_preparer=self.boundary)


@dataclass(frozen=True)
class _Checkpoint:
    event: AgentHarnessEvent
    messages: tuple[Message, ...]
    queues: QueuedMessages


def _workflow(scenario: Scenario) -> _Workflow:
    executor = _ParallelExecutor() if scenario == "parallel" else _SequentialExecutor()
    boundary = _ContextTransition(scenario) if scenario in ("replacement", "rebase") else None
    if scenario in ("sequential", "parallel"):
        calls = tuple(ToolCall(call_id=call_id, name="read", arguments={}) for call_id in _CALL_IDS)
        streams = [_response("tools", calls=calls), _response("answer")]
    else:
        turns = 3 if scenario == "queues" else 2 if boundary is not None else 1
        streams = [_response(f"answer-{index}") for index in range(turns)]
    provider = _ReplayProvider(streams)
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, tool_executor=executor, tools=(_TOOL,)),
        messages=(Message(role="system", content="initial base"),),
    )
    if scenario == "queues":
        harness.set_steering_mode("all")
        harness.set_follow_up_mode("all")
        for text in ("steer-1", "steer-2"):
            harness.steer(text)
        for text in ("follow-1", "follow-2"):
            harness.follow_up(text)
    return _Workflow(harness, provider, executor, boundary, harness.queued_messages)


def _message_payloads(messages: Sequence[Message]) -> list[dict[str, object]]:
    # Timestamps differ between fresh runs; all other retained message data must agree.
    return [message.model_dump(exclude={"created_at"}) for message in messages]


def _assert_queues(workflow: _Workflow, events: Sequence[AgentHarnessEvent]) -> None:
    initial = workflow.initial_queues
    remaining = workflow.harness.queued_messages
    injected = [event for event in events if isinstance(event, QueueMessageInjected)]
    expected_steering = tuple(message.content for message in initial.steering)
    expected_follow_up = tuple(message.content for message in initial.follow_up)
    steering = tuple(event.content for event in injected if event.kind == "steering")
    follow_up = tuple(event.content for event in injected if event.kind == "follow_up")
    assert steering + tuple(message.content for message in remaining.steering) == expected_steering
    assert (
        follow_up + tuple(message.content for message in remaining.follow_up) == expected_follow_up
    )
    assert_queue_ordering_invariants(
        events,
        initial_steering_count=len(expected_steering),
        initial_follow_up_count=len(expected_follow_up),
        expected_steering=expected_steering[: len(steering)],
        expected_follow_up=expected_follow_up[: len(follow_up)],
    )
    users = Counter(
        message.content for message in workflow.harness.messages if message.role == "user"
    )
    assert all(users[event.content] == 1 for event in injected)


def _assert_assistant_retention(
    harness: AgentHarness, events: Sequence[AgentHarnessEvent], *, scenario: Scenario
) -> None:
    completed = [event for event in events if isinstance(event, MessageCompleted)]
    if scenario == "replacement" and any(
        isinstance(event, TurnStarted) and event.turn == 2 for event in events
    ):
        # Only an accepted replacement may discard the first completed response.
        completed = [event for event in completed if event.turn != 1]
    assert [
        (message.content, message.tool_calls or ())
        for message in harness.messages
        if message.role == "assistant"
    ] == [(event.content, event.tool_calls) for event in completed]


def _assert_visible_results_retained(
    harness: AgentHarness, events: Sequence[AgentHarnessEvent]
) -> None:
    ended = [event for event in events if isinstance(event, ToolExecutionEnded)]
    retained = [message for message in harness.messages if message.role == "tool"]
    assert [message.tool_call_id for message in retained] == [event.call_id for event in ended]
    for message, event in zip(retained, ended, strict=True):
        assert (message.content, message.tool_name, message.is_error) == (
            event.output,
            event.name,
            event.is_error,
        )


async def _baseline(scenario: Scenario) -> tuple[_Checkpoint, ...]:
    workflow = _workflow(scenario)
    checkpoints = []
    async for event in workflow.start():
        checkpoints.append(
            _Checkpoint(event, workflow.harness.messages, workflow.harness.queued_messages)
        )
    events = [checkpoint.event for checkpoint in checkpoints]
    assert events and isinstance(events[-1], TurnCompleted)
    assert_turn_invariants(events)
    assert_continuation_invariants(events)
    assert_tool_result_pairing(events)
    assert not workflow.harness.is_running
    assert not workflow.harness.has_queued_messages()
    assert all(event.outcome == "completed" for event in events if isinstance(event, TurnCompleted))
    _assert_queues(workflow, events)
    _assert_visible_results_retained(workflow.harness, events)
    _assert_assistant_retention(workflow.harness, events, scenario=scenario)
    if scenario in ("sequential", "parallel"):
        assert_settled_tool_calls(events, _CALL_IDS)
        expected_finish_order = (
            list(reversed(_CALL_IDS)) if scenario == "parallel" else list(_CALL_IDS)
        )
        assert workflow.executor.finished == expected_finish_order
    if scenario in ("replacement", "rebase"):
        assert workflow.provider.calls[1].messages[0].content == "compacted base"
        assert workflow.harness.messages[0].content == "compacted base"
    return tuple(checkpoints)


async def _assert_reusable(workflow: _Workflow) -> None:
    harness = workflow.harness
    assert not harness.is_running
    assert not harness.cancel()
    before = harness.messages
    queued = harness.queued_messages
    calls = [call for message in before for call in message.tool_calls or ()]
    results = {message.tool_call_id: message for message in before if message.role == "tool"}
    missing = [call.call_id for call in calls if call.call_id not in results]

    # A new provider script prevents leftover fixture responses from accidentally
    # replaying tools. Reuse the harness itself and its actual remaining queues.
    provider = _ReplayProvider([_response(f"resume-{index}") for index in range(queued.count + 1)])
    harness.replace_config(replace(harness.config, provider=provider))
    resumed = [event async for event in harness.continue_()]
    assert_turn_invariants(resumed)
    assert_continuation_invariants(resumed)
    assert_queue_ordering_invariants(
        resumed,
        expected_steering=tuple(message.content for message in queued.steering),
        expected_follow_up=tuple(message.content for message in queued.follow_up),
    )
    assert not harness.is_running
    assert not harness.cancel()
    assert not harness.has_queued_messages()
    retained = [message for message in harness.messages if message.role == "tool"]
    assert Counter(message.tool_call_id for message in retained) == Counter(
        call.call_id for call in calls
    )
    for message in retained:
        if message.tool_call_id in results:
            assert message == results[message.tool_call_id]
        else:
            assert message.tool_call_id in missing
            assert message.content == INTERRUPTED_TOOL_RESULT_TEXT
            assert message.is_error
    replay = provider.calls[0].messages
    for message in retained:
        assert message in replay
        assistant_index = next(
            index
            for index, row in enumerate(replay)
            if any(call.call_id == message.tool_call_id for call in row.tool_calls or ())
        )
        assert replay.index(message) > assistant_index
    repaired = harness.messages
    assert harness.repair_interrupted_tool_calls() == ()
    assert harness.messages == repaired


@pytest.mark.parametrize("scenario", _SCENARIOS)
@pytest.mark.parametrize("action", ["cancel", "close"])
def test_harness_interruption_at_every_emitted_boundary(
    scenario: Scenario, action: Literal["cancel", "close"]
) -> None:
    async def run() -> None:
        with anyio.fail_after(5):
            baseline = await _baseline(scenario)
        occurrences: Counter[str] = Counter()
        for index, checkpoint in enumerate(baseline):
            occurrences[checkpoint.event.type] += 1
            boundary = f"{checkpoint.event.type}[{occurrences[checkpoint.event.type]}]"
            label = f"{scenario}/{action}/after-{boundary}"
            observed: list[AgentHarnessEvent] = []
            try:
                with anyio.fail_after(5):
                    workflow = _workflow(scenario)
                    stream = workflow.start()
                    try:
                        for _ in range(index + 1):
                            observed.append(await anext(stream))
                        assert [event.type for event in observed] == [
                            item.event.type for item in baseline[: index + 1]
                        ]
                        assert _message_payloads(workflow.harness.messages) == _message_payloads(
                            checkpoint.messages
                        )
                        assert _message_payloads(
                            workflow.harness.queued_messages.steering
                        ) == _message_payloads(checkpoint.queues.steering)
                        assert _message_payloads(
                            workflow.harness.queued_messages.follow_up
                        ) == _message_payloads(checkpoint.queues.follow_up)
                        if action == "cancel":
                            assert workflow.harness.cancel()
                            observed.extend([event async for event in stream])
                            assert_cancellation_settled(observed)
                            if scenario == "parallel":
                                requested = [
                                    event.call_id
                                    for event in observed
                                    if isinstance(event, ToolCallRequested)
                                ]
                                assert_settled_tool_calls(observed, requested)
                        else:
                            await stream.aclose()
                            # Closed generators cannot publish settlement events.
                            assert _message_payloads(
                                workflow.harness.messages
                            ) == _message_payloads(checkpoint.messages)
                        assert _message_payloads(
                            workflow.harness.messages[: len(checkpoint.messages)]
                        ) == _message_payloads(checkpoint.messages)
                        assert not workflow.harness.is_running
                        assert not workflow.harness.cancel()
                        _assert_visible_results_retained(workflow.harness, observed)
                        _assert_queues(workflow, observed)
                        _assert_assistant_retention(workflow.harness, observed, scenario=scenario)
                        await _assert_reusable(workflow)
                    finally:
                        await stream.aclose()
            except Exception as exc:
                exc.add_note(
                    f"Interruption checkpoint: {label}; "
                    f"observed: {[event.type for event in observed]}"
                )
                raise

    anyio.run(run)


class _FailingExecutor(_SequentialExecutor):
    async def execute(self, call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        if call.call_id == "call-2":
            raise RuntimeError("tool fixture failed")
        yield self.result(call)


@dataclass
class _FailingBoundary:
    stale_rebase: bool

    async def prepare_boundary(self, *, context: HarnessBoundaryContext) -> RequestBoundaryDecision:
        if not self.stale_rebase:
            raise RuntimeError("boundary fixture failed")
        return RequestBoundaryDecision(
            context_rebase=RequestContextRebase(
                base_messages=(Message(role="system", content="rejected base"),),
                expected_continuation_messages=(),
            )
        )


@pytest.mark.parametrize("fault", ["provider", "tool", "boundary", "stale-rebase"])
def test_harness_retains_accepted_state_and_recovers_after_execution_failure(
    fault: Literal["provider", "tool", "boundary", "stale-rebase"],
) -> None:
    async def run() -> None:
        workflow = _workflow("sequential" if fault == "tool" else "stream")
        harness = workflow.harness
        expected_error: type[Exception] = RuntimeError
        expected_message = f"{fault} fixture failed"
        if fault == "provider":
            workflow.provider = _ReplayProvider(
                [
                    (
                        ProviderResponseStarted(model="fixture"),
                        ProviderTextDelta(delta="unfinished"),
                        RuntimeError(expected_message),
                    )
                ]
            )
            harness.replace_config(replace(harness.config, provider=workflow.provider))
        elif fault == "tool":
            workflow.executor = _FailingExecutor()
            harness.replace_config(replace(harness.config, tool_executor=workflow.executor))
        else:
            workflow.boundary = _FailingBoundary(stale_rebase=fault == "stale-rebase")
            if fault == "stale-rebase":
                expected_error = RequestBoundaryUnsupportedError
                expected_message = (
                    "RequestContextRebase expected continuation does not match live state"
                )

        events: list[AgentHarnessEvent] = []
        with anyio.fail_after(5):
            with pytest.raises(expected_error, match=expected_message):
                async for event in workflow.start():
                    events.append(event)
                    if isinstance(event, TurnStarted):
                        # Pending follow-up is ineligible during tools or failure.
                        # For boundary faults it is accepted before preparation fails.
                        harness.follow_up("pending follow-up")
                        workflow.initial_queues = harness.queued_messages

            assert_turn_invariants(events)
            assert_tool_result_pairing(events)
            assert len(workflow.provider.calls) == 1
            assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
                expected_message
            ]
            terminals = [event for event in events if isinstance(event, TurnCompleted)]
            assert len(terminals) == 1
            assert terminals[0].outcome == (
                "completed" if fault in ("boundary", "stale-rebase") else "failed"
            )
            assert harness.messages[0].content == "initial base"
            assert not any(message.content == "rejected base" for message in harness.messages)
            _assert_visible_results_retained(harness, events)
            _assert_assistant_retention(harness, events, scenario="stream")
            _assert_queues(workflow, events)
            if fault == "tool":
                assert workflow.executor.finished == ["call-1"]
            if fault == "provider":
                assert not any(message.role == "assistant" for message in harness.messages)
            await _assert_reusable(workflow)

    anyio.run(run)
