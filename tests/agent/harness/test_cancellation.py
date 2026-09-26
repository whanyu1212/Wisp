from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from tests.agent.harness.support import (
    RecordingToolExecutor,
    build_harness,
)
from tests.support.agent_runtime import (
    assert_cancellation_settled,
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_terminals,
)
from wisp.agent.harness import AgentHarness
from wisp.agent.messages import Message
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolPreparationEvent,
)
from wisp.events import (
    ErrorEvent,
    MessageDelta,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
)
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


class BlockingPreparedExecutor:
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


class FailingAndBlockingPreparedExecutor:
    def __init__(self, failed: anyio.Event) -> None:
        self._failed = failed
        self._second_started = anyio.Event()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            if tool_call.call_id == "call-1":
                await self._second_started.wait()
                self._failed.set()
                raise RuntimeError("executor failed during cancellation")
            self._second_started.set()
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


class ImmediatePreparedExecutor:
    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            return ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output=f"output-{tool_call.call_id}",
                is_error=False,
            )

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


class BlockingProvider:
    name = "blocking"
    default_model: str | None = "blocking"

    def __init__(
        self,
        *,
        waiting: anyio.Event,
        release: anyio.Event,
        delta_before_wait: str | None = None,
    ) -> None:
        self.waiting = waiting
        self.release = release
        self.delta_before_wait = delta_before_wait
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
            if self.delta_before_wait is not None:
                yield ProviderTextDelta(delta=self.delta_before_wait)
            self.waiting.set()
            await self.release.wait()
        finally:
            self.closed = True
        yield ProviderResponseCompleted(content="too late")


def test_cancel_stops_at_event_boundary_and_marks_turn_cancelled() -> None:
    async def run() -> tuple[AgentHarness, BlockingProvider, list[object]]:
        provider = BlockingProvider(
            waiting=anyio.Event(),
            release=anyio.Event(),
            delta_before_wait="first",
        )
        harness = build_harness(provider)
        events: list[object] = []

        with anyio.fail_after(1):
            async for event in harness.prompt("stop"):
                events.append(event)
                if isinstance(event, MessageDelta):
                    assert harness.cancel()
        return harness, provider, events

    harness, provider, events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "error",
        "turn.completed",
    ]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert completed.outcome == "cancelled"
    assert [(message.role, message.content) for message in harness.messages] == [("user", "stop")]
    assert not provider.waiting.is_set()
    assert provider.closed
    assert harness.is_running is False
    assert harness.cancel() is False
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)


def test_cancel_interrupts_blocked_provider_stream() -> None:
    async def run() -> tuple[AgentHarness, BlockingProvider, list[object]]:
        provider = BlockingProvider(waiting=anyio.Event(), release=anyio.Event())
        harness = build_harness(provider)
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("stop now")])

        with anyio.fail_after(1):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await provider.waiting.wait()
                assert harness.cancel()
        return harness, provider, events

    harness, provider, events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "error",
        "turn.completed",
    ]
    assert provider.closed
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "stop now")
    ]
    assert harness.is_running is False


def test_close_after_tool_finishes_keeps_tool_output() -> None:
    tool_call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    harness = build_harness(
        provider,
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={}),),
    )

    async def run() -> None:
        events = harness.prompt("initial")
        async for event in events:
            if isinstance(event, ToolExecutionEnded):
                await events.aclose()
                return
        raise AssertionError("missing tool execution end")

    anyio.run(run)

    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "checking"),
        ("tool", "tool output"),
    ]


def test_cancel_drains_prepared_batch_results_in_source_order() -> None:
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

    async def run() -> tuple[AgentHarness, list[object]]:
        all_started = anyio.Event()
        harness = build_harness(
            provider,
            executor=BlockingPreparedExecutor(all_started),
        )
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await all_started.wait()
                assert harness.cancel()
        return harness, events

    harness, events = anyio.run(run)

    terminals = [event for event in events if isinstance(event, ToolExecutionEnded)]
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in terminals] == ["call-1", "call-2"]
    assert [event.call_id for event in results] == ["call-1", "call-2"]
    assert all(event.is_error and event.retryable for event in results)
    assert all(event.process_state == "cancelled" for event in results)
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"
    assert [(message.role, message.tool_call_id) for message in harness.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
        ("tool", "call-2"),
    ]
    assert_turn_terminals(events)
    assert_settled_tool_calls(events, ("call-1", "call-2"))


def test_cancel_settles_blocked_sequential_batch() -> None:
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

    class BlockingExecutor:
        def __init__(self) -> None:
            self.blocked = anyio.Event()
            self.closed = False

        async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
            try:
                self.blocked.set()
                await anyio.sleep_forever()
            finally:
                self.closed = True
            yield  # pragma: no cover - makes this an async generator

    async def run() -> tuple[AgentHarness, BlockingExecutor, list[object]]:
        executor = BlockingExecutor()
        harness = build_harness(provider, executor=executor)
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await executor.blocked.wait()
                assert harness.cancel()
        return harness, executor, events

    harness, executor, events = anyio.run(run)

    assert executor.closed
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [(event.call_id, event.process_state) for event in results] == [
        ("call-1", "cancelled"),
        ("call-2", "cancelled"),
    ]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert [(event.outcome, event.finish_reason) for event in completed] == [
        ("cancelled", "cancelled")
    ]
    # Settlement is retained live, so the next run has nothing left to repair.
    assert [(message.role, message.tool_call_id) for message in harness.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
        ("tool", "call-2"),
    ]
    assert harness.repair_interrupted_tool_calls() == ()
    assert_turn_terminals(events)
    assert_settled_tool_calls(events, ("call-1", "call-2"))


def test_cancel_publishes_result_returned_before_executor_cleanup() -> None:
    tool_call = ToolCall(call_id="call-1", name="read", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ]
        ]
    )

    class SlowCleanupExecutor:
        def __init__(self) -> None:
            self.cleaning_up = anyio.Event()

        async def execute(self, call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
            yield ToolExecutionEnded(
                call_id=call.call_id, name=call.name, output="real output", is_error=False
            )
            # The result is already returned; cancellation arrives while cleaning up.
            self.cleaning_up.set()
            await anyio.sleep_forever()

    async def run() -> list[object]:
        executor = SlowCleanupExecutor()
        harness = build_harness(provider, executor=executor)
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await executor.cleaning_up.wait()
                assert harness.cancel()
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [(event.call_id, event.output, event.process_state) for event in results] == [
        ("call-1", "real output", None)
    ]
    assert [
        (event.outcome, event.finish_reason) for event in events if isinstance(event, TurnCompleted)
    ] == [("cancelled", "cancelled")]
    assert_settled_tool_calls(events, ("call-1",))


def test_cancel_settles_batch_before_sibling_executor_error() -> None:
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
        failed = anyio.Event()
        harness = build_harness(
            provider,
            executor=FailingAndBlockingPreparedExecutor(failed),
        )
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await failed.wait()
                assert harness.cancel()
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in results] == ["call-1", "call-2"]
    assert all(event.process_state == "cancelled" for event in results)
    assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
        "Agent run cancelled"
    ]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"


def test_cancel_after_prepared_result_finishes_batch_then_turn() -> None:
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
    harness = build_harness(provider, executor=ImmediatePreparedExecutor())

    async def run() -> list[object]:
        emitted: list[object] = []
        async for event in harness.prompt("initial"):
            emitted.append(event)
            if isinstance(event, ToolExecutionEnded) and event.call_id == "call-1":
                assert harness.cancel()
        return emitted

    events = anyio.run(run)

    assert [event.call_id for event in events if isinstance(event, ToolExecutionEnded)] == [
        "call-1",
        "call-2",
    ]
    assert [event.call_id for event in events if isinstance(event, ToolResultReady)] == [
        "call-1",
        "call-2",
    ]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"
    assert [(message.role, message.tool_call_id) for message in harness.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
        ("tool", "call-2"),
    ]


def test_cancel_after_tool_finishes_keeps_tool_output() -> None:
    tool_call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    harness = build_harness(
        provider,
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={}),),
    )

    async def run() -> list[object]:
        emitted: list[object] = []
        async for event in harness.prompt("initial"):
            emitted.append(event)
            if isinstance(event, ToolExecutionEnded):
                assert harness.cancel()
        return emitted

    events = anyio.run(run)

    assert [event.type for event in events[-2:]] == ["error", "turn.completed"]
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "checking"),
        ("tool", "tool output"),
    ]
    tool_message = harness.messages[-1]
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.tool_name == "lookup"
    assert tool_message.is_error is False


@pytest.mark.parametrize("prepared", [False, True])
def test_cancel_after_tool_turn_emits_one_boundary_error(prepared: bool) -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="", tool_calls=(call,), finish_reason="tool_calls"
                ),
            ],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="unused")],
        ]
    )
    harness = build_harness(
        provider,
        executor=ImmediatePreparedExecutor() if prepared else RecordingToolExecutor(),
        tools=(ToolSpec(name="read", description="Read", input_schema={"type": "object"}),),
    )

    async def run() -> list[object]:
        events: list[object] = []
        async for event in harness.prompt("initial"):
            events.append(event)
            if isinstance(event, TurnCompleted):
                assert harness.cancel()
        return events

    events = anyio.run(run)
    assert_cancellation_settled(events)
    assert len(provider.calls) == 1
    assert len([event for event in events if isinstance(event, TurnCompleted)]) == 1
    assert not harness.is_running
    assert not harness.cancel()
