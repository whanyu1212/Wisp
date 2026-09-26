from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import anyio
import pytest

from tests.support.agent_runtime import assert_settled_tool_calls
from wisp.agent.loop.tool_execution import (
    CancelledToolBatch,
    CompletedToolBatch,
    ToolBatch,
)
from wisp.agent.loop.tool_lifecycle import ToolExecutionLifecycle
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolPreparationEvent,
)
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
)
from wisp.providers.base import ToolCallResult
from wisp.providers.events import ToolCall


def _call(call_id: str = "call-1", *, arguments: dict[str, object] | None = None) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="lookup",
        arguments=arguments or {"query": "wisp"},
    )


def _approval(
    call: ToolCall,
    *,
    arguments: dict[str, object] | None = None,
) -> ToolApprovalRequested:
    return ToolApprovalRequested(
        call_id=call.call_id,
        name=call.name,
        arguments=arguments or dict(call.arguments),
        safety="read",
    )


def _resolution(call: ToolCall, *, approved: bool = True) -> ToolApprovalResolved:
    return ToolApprovalResolved(
        call_id=call.call_id,
        name=call.name,
        approved=approved,
    )


def _ended(
    call: ToolCall,
    *,
    output: str = "done",
    is_error: bool = False,
) -> ToolExecutionEnded:
    return ToolExecutionEnded(
        call_id=call.call_id,
        name=call.name,
        output=output,
        is_error=is_error,
    )


class _ScriptedExecutor:
    def __init__(self, events: Sequence[object]) -> None:
        self.events = events
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        for event in self.events:
            yield event  # type: ignore[misc]


class _NeverExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        raise AssertionError(f"Unexpected tool execution: {tool_call.call_id}")
        yield  # pragma: no cover - makes this an async generator


class _PreparedExecutor:
    def __init__(
        self,
        runner: Callable[[ToolCall], Awaitable[ToolExecutionEnded]],
        *,
        before_prepare: Callable[[ToolCall], None] | None = None,
    ) -> None:
        self._runner = runner
        self._before_prepare = before_prepare

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        if self._before_prepare is not None:
            self._before_prepare(tool_call)

        async def run() -> ToolExecutionEnded:
            return await self._runner(tool_call)

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


def test_tool_execution_lifecycle_accepts_valid_approval_sequence() -> None:
    call = _call()
    lifecycle = ToolExecutionLifecycle(call)

    assert lifecycle.accept(_approval(call)).type == "tool.approval.requested"
    assert lifecycle.accept(_resolution(call)).type == "tool.approval.resolved"
    terminal = _ended(call)
    assert lifecycle.accept(terminal) is terminal
    assert lifecycle.finish() is terminal


@pytest.mark.parametrize(
    ("events", "error"),
    [
        ((object(),), "unsupported event type"),
        ((_resolution(_call()),), "resolved approval before requesting"),
        ((_approval(_call()), _approval(_call())), "requested approval more than once"),
        (
            (_approval(_call()), _resolution(_call()), _resolution(_call())),
            "resolved approval more than once",
        ),
        ((_approval(_call()),), "unresolved approval"),
        (
            (_approval(_call()), _resolution(_call(), approved=False), _ended(_call())),
            "reported success after approval was denied",
        ),
        ((_ended(_call()), _ended(_call())), "event after the result"),
        (
            (
                _approval(
                    _call(arguments={"options": [1]}),
                    arguments={"options": [True]},
                ),
            ),
            "approval arguments do not match",
        ),
    ],
)
def test_tool_execution_lifecycle_rejects_malformed_sequences(
    events: tuple[object, ...],
    error: str,
) -> None:
    lifecycle = ToolExecutionLifecycle(
        _call(arguments={"options": [1]}) if "arguments do not match" in error else _call()
    )

    with pytest.raises(ToolExecutionProtocolError, match=error):
        for event in events:
            lifecycle.accept(event)
        lifecycle.finish()


def test_sequential_batch_streams_events_and_returns_provider_results() -> None:
    call = _call()
    executor = _ScriptedExecutor((_approval(call), _resolution(call), _ended(call)))
    recorded: list[ToolResultReady] = []
    batch = ToolBatch(
        tool_executor=executor,
        tool_calls=(call,),
        truncated=False,
        is_cancelled=lambda: False,
        on_result=recorded.append,
    )

    async def run() -> list[object]:
        return [event async for event in batch.events()]

    events = anyio.run(run)

    assert [type(event) for event in events] == [
        ToolCallRequested,
        ToolExecutionStarted,
        ToolApprovalRequested,
        ToolApprovalResolved,
        ToolExecutionEnded,
        ToolResultReady,
    ]
    assert isinstance(batch.outcome, CompletedToolBatch)
    assert batch.outcome.results == (
        ToolCallResult(call_id=call.call_id, output="done", is_error=False),
    )
    assert len(recorded) == 1
    assert recorded[0] is events[-1]


def test_truncated_batch_synthesizes_results_without_execution() -> None:
    call = _call()
    executor = _NeverExecutor()
    recorded: list[ToolResultReady] = []
    batch = ToolBatch(
        tool_executor=executor,
        tool_calls=(call,),
        truncated=True,
        is_cancelled=lambda: False,
        on_result=recorded.append,
    )

    async def run() -> list[object]:
        return [event async for event in batch.events()]

    events = anyio.run(run)

    assert [type(event) for event in events] == [
        ToolCallRequested,
        ToolExecutionEnded,
        ToolResultReady,
    ]
    assert executor.calls == []
    assert isinstance(batch.outcome, CompletedToolBatch)
    assert batch.outcome.results[0].is_error is True
    assert recorded[0].failure_code == "invalid_arguments"


def test_prepared_batch_publishes_source_order_after_reverse_completion() -> None:
    calls = (_call("call-1"), _call("call-2"))
    first_started = anyio.Event()
    release_first = anyio.Event()
    completion_order: list[str] = []

    async def runner(call: ToolCall) -> ToolExecutionEnded:
        if call.call_id == "call-1":
            first_started.set()
            await release_first.wait()
        else:
            await first_started.wait()
            completion_order.append(call.call_id)
            release_first.set()
        if call.call_id == "call-1":
            completion_order.append(call.call_id)
        return _ended(call, output=f"output-{call.call_id}")

    batch = ToolBatch(
        tool_executor=_PreparedExecutor(runner),
        tool_calls=calls,
        truncated=False,
        is_cancelled=lambda: False,
        on_result=lambda _result: None,
    )

    async def run() -> list[object]:
        with anyio.fail_after(2):
            return [event async for event in batch.events()]

    events = anyio.run(run)

    assert completion_order == ["call-2", "call-1"]
    assert [event.call_id for event in events if isinstance(event, ToolResultReady)] == [
        "call-1",
        "call-2",
    ]
    assert isinstance(batch.outcome, CompletedToolBatch)
    assert [result.call_id for result in batch.outcome.results] == ["call-1", "call-2"]


def test_prepared_cancellation_settles_requested_calls() -> None:
    calls = (_call("call-1"), _call("call-2"))
    cancelled = False
    runner_calls = 0

    def cancel_during_prepare(_call: ToolCall) -> None:
        nonlocal cancelled
        cancelled = True

    async def runner(call: ToolCall) -> ToolExecutionEnded:
        nonlocal runner_calls
        runner_calls += 1
        return _ended(call)

    recorded: list[ToolResultReady] = []
    batch = ToolBatch(
        tool_executor=_PreparedExecutor(runner, before_prepare=cancel_during_prepare),
        tool_calls=calls,
        truncated=False,
        is_cancelled=lambda: cancelled,
        on_result=recorded.append,
    )

    async def run() -> list[object]:
        return [event async for event in batch.events()]

    events = anyio.run(run)

    assert runner_calls == 0
    assert isinstance(batch.outcome, CancelledToolBatch)
    assert [event.call_id for event in events if isinstance(event, ToolResultReady)] == [
        "call-1",
        "call-2",
    ]
    assert [result.process_state for result in recorded] == ["cancelled", "cancelled"]
    assert [result.call_id for result in batch.outcome.results] == ["call-1", "call-2"]


def test_cancellation_check_errors_propagate() -> None:
    def raise_cancelled() -> bool:
        raise RuntimeError("cancellation check failed")

    batch = ToolBatch(
        tool_executor=_NeverExecutor(),
        tool_calls=(_call(),),
        truncated=False,
        is_cancelled=raise_cancelled,
        on_result=lambda _result: None,
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="cancellation check failed"):
            async for _event in batch.events():
                pass

    anyio.run(run)


class _ApprovingExecutor:
    """Request approval, receive it, then finish each call; records started calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call.call_id)
        yield _approval(tool_call)
        yield _resolution(tool_call)
        yield _ended(tool_call, output=f"{tool_call.call_id} output")


# (event that triggers cancellation, calls the executor started, calls that finished)
_SEQUENTIAL_CANCEL_POINTS = [
    (("tool.call", "call-1"), [], []),
    (("tool.execution.started", "call-1"), [], []),
    (("tool.approval.requested", "call-1"), ["call-1"], []),
    (("tool.approval.resolved", "call-1"), ["call-1"], []),
    (("tool.execution.ended", "call-1"), ["call-1"], ["call-1"]),
    (("tool.result", "call-1"), ["call-1"], ["call-1"]),
    (("tool.call", "call-2"), ["call-1"], ["call-1"]),
    (("tool.result", "call-2"), ["call-1", "call-2"], ["call-1", "call-2"]),
]


@pytest.mark.parametrize(
    ("cancel_at", "executed", "finished"),
    _SEQUENTIAL_CANCEL_POINTS,
    ids=[f"{call_id}:{event_type}" for (event_type, call_id), _, _ in _SEQUENTIAL_CANCEL_POINTS],
)
def test_sequential_cancellation_settles_every_requested_call(
    cancel_at: tuple[str, str], executed: list[str], finished: list[str]
) -> None:
    calls = (_call("call-1"), _call("call-2"))
    executor = _ApprovingExecutor()
    cancelled = False
    recorded: list[ToolResultReady] = []
    batch = ToolBatch(
        tool_executor=executor,
        tool_calls=calls,
        truncated=False,
        is_cancelled=lambda: cancelled,
        on_result=recorded.append,
    )

    async def run() -> list[object]:
        nonlocal cancelled
        events: list[object] = []
        async for event in batch.events():
            events.append(event)
            if (event.type, event.call_id) == cancel_at:
                cancelled = True
        return events

    events = anyio.run(run)

    assert_settled_tool_calls(events, ("call-1", "call-2"))
    assert executor.calls == executed
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [result.call_id for result in results] == ["call-1", "call-2"]
    assert [result.output for result in results] == [
        f"{result.call_id} output" if result.call_id in finished else INTERRUPTED_TOOL_RESULT_TEXT
        for result in results
    ]
    # Every published result is retained, in publication order.
    assert recorded == results
    assert isinstance(batch.outcome, CancelledToolBatch)
    assert [result.call_id for result in batch.outcome.results] == ["call-1", "call-2"]
    if cancel_at == ("tool.approval.requested", "call-1"):
        # The pending approval is denied before the interrupted result.
        call_1_types = [
            event.type for event in events if getattr(event, "call_id", None) == "call-1"
        ]
        assert call_1_types[-3:] == [
            "tool.approval.resolved",
            "tool.execution.ended",
            "tool.result",
        ]
        denial = [event for event in events if isinstance(event, ToolApprovalResolved)][-1]
        assert (denial.approved, denial.reason) == (False, "Agent run cancelled")


def test_truncated_batch_rejects_every_call_even_when_cancelled_midway() -> None:
    calls = (_call("call-1"), _call("call-2"))
    executor = _NeverExecutor()
    cancelled = False
    batch = ToolBatch(
        tool_executor=executor,
        tool_calls=calls,
        truncated=True,
        is_cancelled=lambda: cancelled,
        on_result=lambda _result: None,
    )

    async def run() -> list[object]:
        nonlocal cancelled
        events: list[object] = []
        async for event in batch.events():
            events.append(event)
            if isinstance(event, ToolResultReady):
                cancelled = True
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [(result.call_id, result.failure_code) for result in results] == [
        ("call-1", "invalid_arguments"),
        ("call-2", "invalid_arguments"),
    ]
    assert executor.calls == []
    assert isinstance(batch.outcome, CancelledToolBatch)
    assert [result.call_id for result in batch.outcome.results] == ["call-1", "call-2"]


def test_sequential_batch_propagates_an_unrequested_cancellation() -> None:
    blocked = anyio.Event()

    class BlockingExecutor:
        async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
            blocked.set()
            await anyio.sleep_forever()
            yield _ended(tool_call)  # pragma: no cover - never reached

    batch = ToolBatch(
        tool_executor=BlockingExecutor(),
        tool_calls=(_call("call-1"), _call("call-2")),
        truncated=False,
        is_cancelled=lambda: False,
        on_result=lambda _result: None,
    )

    async def run() -> tuple[bool, list[object]]:
        events: list[object] = []
        scope = anyio.CancelScope()

        async def consume() -> None:
            with scope:
                async for event in batch.events():
                    events.append(event)

        with anyio.fail_after(5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(consume)
                await blocked.wait()
                scope.cancel()
        return scope.cancelled_caught, events

    cancelled_caught, events = anyio.run(run)

    # The caller's own cancellation is not the run's: nothing is settled.
    assert cancelled_caught
    assert [getattr(event, "type", None) for event in events] == [
        "tool.call",
        "tool.execution.started",
    ]
    assert batch.outcome is None
