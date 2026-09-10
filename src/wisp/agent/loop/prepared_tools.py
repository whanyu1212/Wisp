"""Prepared-executor scheduling and cancellation settlement for one tool batch."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    PreparedToolExecutor,
    ToolExecutionProtocolError,
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
from wisp.providers.events import ToolCall

from .stream_cleanup import closing_stream

if TYPE_CHECKING:
    from .tool_execution import ToolExecutionLifecycle

type ToolBatchEvent = (
    ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolExecutionEnded
    | ToolResultReady
)

type CancellationCheck = Callable[[], bool]


_MAX_PARALLEL_TOOL_EXECUTIONS = 8


@dataclass(slots=True)
class _PreparedCallState:
    """Keep a requested call, its validated lifecycle, and deferred execution together."""

    tool_call: ToolCall
    lifecycle: ToolExecutionLifecycle
    execution: PreparedToolExecution


@dataclass(slots=True)
class _PreparedBatchStatus:
    """Share cancellation state between the prepared scheduler and its consumer."""

    cancelled: bool = False


@dataclass(slots=True)
class _PreparedRunOutcome:
    """Store one concurrent runner's terminal event or raised exception in source order."""

    terminal: ToolExecutionEnded | None = None
    error: Exception | None = None


async def _run_prepared_chunk(
    calls: Sequence[_PreparedCallState],
) -> tuple[tuple[_PreparedRunOutcome, ...], bool]:
    """Execute one prepared chunk concurrently while retaining input order.

    Args:
        calls (Sequence[_PreparedCallState]): Prepared calls selected by the batch scheduler.

    Returns:
        tuple[tuple[_PreparedRunOutcome, ...], bool]: Results or exceptions in call order,
            plus whether the task group was cancelled. An interrupted call may have
            neither a terminal result nor a stored exception.
    """
    outcomes = [_PreparedRunOutcome() for _ in calls]

    async def run_one(index: int, call: _PreparedCallState) -> None:
        """Record a runner's result or exception in its assigned output slot.

        Args:
            index (int): Source-order index in the enclosing outcome list.
            call (_PreparedCallState): Prepared execution to invoke.
        """
        try:
            outcomes[index].terminal = await call.execution.run()
        except Exception as exc:  # noqa: BLE001 - preserve the original fatal error
            outcomes[index].error = exc

    cancelled = False
    try:
        async with anyio.create_task_group() as task_group:
            for index, call in enumerate(calls):
                task_group.start_soon(run_one, index, call)
    except anyio.get_cancelled_exc_class():
        cancelled = True
    return tuple(outcomes), cancelled


def _interrupted_tool_execution(tool_call: ToolCall) -> ToolExecutionEnded:
    """Build a terminal record for an interrupted call whose effects may be uncertain.

    Args:
        tool_call (ToolCall): Requested call to settle.

    Returns:
        ToolExecutionEnded: Cancelled error result advising retry only when effects
            can safely be repeated. This function does not execute the tool.
    """
    return ToolExecutionEnded(
        call_id=tool_call.call_id,
        name=tool_call.name,
        output=INTERRUPTED_TOOL_RESULT_TEXT,
        is_error=True,
        failure_code="internal_error",
        retryable=True,
        recovery_hint="Retry the tool call if its effects can be safely repeated.",
        process_state="cancelled",
    )


async def _prepared_tool_batch_events(
    executor: PreparedToolExecutor,
    tool_calls: Sequence[ToolCall],
    status: _PreparedBatchStatus,
    *,
    is_cancelled: CancellationCheck,
) -> AsyncIterator[ToolBatchEvent]:
    """Prepare a batch and execute deferred calls with bounded concurrency.

    All calls are prepared before execution begins. Prepared calls run concurrently
    only if every prepared execution is marked parallel_safe; otherwise they run one
    at a time. Successful batches publish results in source order even when runners
    finish out of order. Cancellation retains completed chunk results first, then
    settles the remaining requested calls in source order.

    Args:
        executor (PreparedToolExecutor): Executor separating approval from side effects.
        tool_calls (Sequence[ToolCall]): Requested calls in model order.
        status (_PreparedBatchStatus): Shared cancellation flag updated while consuming.
        is_cancelled (CancellationCheck): Cooperative cancellation callback.

    Yields:
        ToolBatchEvent: Request, start, approval, and result events. Cancellation preserves
            completed results and synthesizes interrupted results for unsettled calls.

    Examples:
        For three prepared calls A, B, and C, all marked parallel_safe, their runners
        may finish C, A, B. Without cancellation or failures, terminal/result pairs
        are published A, B, C. If cancellation interrupts A after B has completed,
        B's completed result can be published before A's synthetic interrupted result.
        If any prepared call is not parallel_safe, runners execute sequentially instead.

    Raises:
        ToolExecutionProtocolError: Preparation or execution violates lifecycle rules.
        Exception: Executor, runner, or cancellation-check errors propagate. A chunk's
            successful results can be published before its stored runner error is raised.
    """
    from .tool_execution import ToolExecutionLifecycle  # noqa: PLC0415

    prepared_calls: list[_PreparedCallState] = []
    lifecycles: dict[str, ToolExecutionLifecycle] = {}
    requested_call_ids: set[str] = set()
    result_call_ids: set[str] = set()

    async def finalize_interrupted() -> AsyncIterator[ToolBatchEvent]:
        """Settle requested calls that have no published result yet.

        Yields:
            ToolBatchEvent: Missing request events, unresolved approval denials, and
                interrupted terminal/result pairs in source order.
        """
        for tool_call in tool_calls:
            if tool_call.call_id in result_call_ids:
                continue
            if tool_call.call_id not in requested_call_ids:
                requested_call_ids.add(tool_call.call_id)
                yield ToolCallRequested(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=deepcopy(dict(tool_call.arguments)),
                )
            lifecycle = lifecycles.get(tool_call.call_id)
            if (
                lifecycle is not None
                and lifecycle.approval_requested
                and not lifecycle.approval_resolved
            ):
                approval = ToolApprovalResolved(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    approved=False,
                    reason="Agent run cancelled",
                )
                lifecycle.accept(approval)
                yield approval
            terminal = _interrupted_tool_execution(tool_call)
            if lifecycle is not None:
                lifecycle.accept(terminal)
                terminal = lifecycle.finish()
            result = ToolResultReady.from_execution_ended(terminal)
            result_call_ids.add(tool_call.call_id)
            yield terminal
            yield result

    for tool_call in tool_calls:
        requested_call_ids.add(tool_call.call_id)
        yield ToolCallRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=deepcopy(dict(tool_call.arguments)),
        )
        if is_cancelled():
            status.cancelled = True
            break

        yield ToolExecutionStarted(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=deepcopy(dict(tool_call.arguments)),
        )
        if is_cancelled():
            status.cancelled = True
            break

        lifecycle = ToolExecutionLifecycle(tool_call)
        lifecycles[tool_call.call_id] = lifecycle
        prepared: PreparedToolExecution | None = None
        async with closing_stream(executor.prepare(tool_call)) as preparation:
            try:
                async for raw_event in preparation:
                    if isinstance(raw_event, PreparedToolExecution):
                        if prepared is not None:
                            raise ToolExecutionProtocolError(
                                "Tool executor prepared more than one execution for "
                                f"{tool_call.call_id}"
                            )
                        lifecycle.accept_prepared(raw_event)
                        prepared = raw_event
                        if is_cancelled():
                            status.cancelled = True
                            break
                        continue
                    if prepared is not None:
                        raise ToolExecutionProtocolError(
                            f"Tool executor emitted an event after preparing {tool_call.call_id}"
                        )
                    event = lifecycle.accept(raw_event)
                    if isinstance(event, ToolExecutionEnded):
                        raise ToolExecutionProtocolError(
                            "Prepared tool executor emitted a terminal result "
                            "during preparation for "
                            f"{tool_call.call_id}"
                        )
                    yield event.model_copy(deep=True)
                    if is_cancelled():
                        status.cancelled = True
                        break
            except anyio.get_cancelled_exc_class():
                status.cancelled = True
        if status.cancelled:
            break
        if prepared is None:
            raise ToolExecutionProtocolError(
                f"Tool executor ended preparation without a result for {tool_call.call_id}"
            )
        prepared_calls.append(
            _PreparedCallState(
                tool_call=tool_call,
                lifecycle=lifecycle,
                execution=prepared,
            )
        )

    if status.cancelled:
        async for interrupted_event in finalize_interrupted():
            yield interrupted_event
        return

    batch_is_parallel = all(call.execution.parallel_safe for call in prepared_calls)
    chunk_size = _MAX_PARALLEL_TOOL_EXECUTIONS if batch_is_parallel else 1
    for chunk_start in range(0, len(prepared_calls), chunk_size):
        if is_cancelled():
            status.cancelled = True
            async for interrupted_event in finalize_interrupted():
                yield interrupted_event
            return
        chunk = prepared_calls[chunk_start : chunk_start + chunk_size]
        outcomes, chunk_cancelled = await _run_prepared_chunk(chunk)
        status.cancelled = status.cancelled or chunk_cancelled or is_cancelled()
        fatal_error: Exception | None = None
        for call, outcome in zip(chunk, outcomes, strict=True):
            if outcome.error is not None:
                if fatal_error is None:
                    fatal_error = outcome.error
                continue
            if outcome.terminal is None:
                if not status.cancelled and fatal_error is None:
                    fatal_error = ToolExecutionProtocolError(
                        "Prepared tool execution ended without a result for "
                        f"{call.tool_call.call_id}"
                    )
                continue
            try:
                call.lifecycle.accept(outcome.terminal)
                terminal = call.lifecycle.finish()
                result = ToolResultReady.from_execution_ended(terminal)
            except Exception as exc:
                if fatal_error is None:
                    fatal_error = exc
                continue
            result_call_ids.add(call.tool_call.call_id)
            yield terminal
            yield result
            status.cancelled = status.cancelled or is_cancelled()
        if status.cancelled:
            async for interrupted_event in finalize_interrupted():
                yield interrupted_event
            return
        if fatal_error is not None:
            raise fatal_error
