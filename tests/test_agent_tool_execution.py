from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

import anyio
import pytest

from wisp.agent.execution import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolPreparationEvent,
)
from wisp.agent.loop.tool_execution import (
    CancelledToolBatch,
    CompletedToolBatch,
    ToolBatch,
    ToolExecutionLifecycle,
)
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
