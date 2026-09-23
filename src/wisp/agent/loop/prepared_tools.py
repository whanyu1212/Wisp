"""Prepared-executor scheduling for one tool batch."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import anyio

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    PreparedToolExecutor,
    ToolExecutionProtocolError,
)
from wisp.events import ToolExecutionEnded
from wisp.providers.events import ToolCall

from .stream_cleanup import closing_stream
from .tool_lifecycle import (
    CancellationCheck,
    RequestedCancellation,
    ToolBatchEvent,
    ToolBatchSettlement,
    ToolExecutionLifecycle,
    tool_execution_started,
)

_MAX_PARALLEL_TOOL_EXECUTIONS = 8


@dataclass(slots=True)
class _PreparedCallState:
    """Keep a requested call, its validated lifecycle, and deferred execution together."""

    tool_call: ToolCall
    lifecycle: ToolExecutionLifecycle
    execution: PreparedToolExecution


@dataclass(slots=True)
class _PreparedRunOutcome:
    """Store one concurrent runner's terminal event or raised exception in source order."""

    terminal: ToolExecutionEnded | None = None
    error: Exception | None = None


async def _run_prepared_chunk(
    calls: Sequence[_PreparedCallState],
    *,
    is_cancelled: CancellationCheck,
) -> tuple[tuple[_PreparedRunOutcome, ...], bool]:
    """Execute one prepared chunk concurrently while retaining input order.

    Args:
        calls (Sequence[_PreparedCallState]): Prepared calls selected by the batch scheduler.
        is_cancelled (CancellationCheck): The run's cancellation-token check.

    Returns:
        tuple[tuple[_PreparedRunOutcome, ...], bool]: Results or exceptions in call order,
            plus whether the run's requested cancellation interrupted the chunk. An
            interrupted call may have neither a terminal result nor a stored exception.

    Raises:
        BaseException: A cancellation the run did not request propagates to the caller.
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

    with RequestedCancellation(is_cancelled) as cancellation:
        async with anyio.create_task_group() as task_group:
            for index, call in enumerate(calls):
                task_group.start_soon(run_one, index, call)
    return tuple(outcomes), cancellation.absorbed


@dataclass(slots=True)
class _PreparedToolBatch:
    """Prepare and execute one two-phase tool batch.

    Each instance is single-use: consume events() once, then read cancelled. When
    cancelled, it stops and leaves unfinished calls to the batch's shared settlement.

    Args:
        executor (PreparedToolExecutor): Executor separating approval from side effects.
        tool_calls (Sequence[ToolCall]): Requested calls in model order.
        is_cancelled (CancellationCheck): Cooperative cancellation callback.
        settlement (ToolBatchSettlement): Batch record of requests, lifecycles, and
            published results.
    """

    executor: PreparedToolExecutor
    tool_calls: Sequence[ToolCall]
    is_cancelled: CancellationCheck
    settlement: ToolBatchSettlement
    # Set while events() runs; ToolBatch reads it after the stream is exhausted.
    cancelled: bool = field(default=False, init=False)
    _prepared_calls: list[_PreparedCallState] = field(default_factory=list, init=False)

    async def events(self) -> AsyncIterator[ToolBatchEvent]:
        """Prepare calls, then execute eligible chunks until done or cancelled.

        All calls are prepared before execution begins. Prepared calls run concurrently
        only if every prepared execution is marked parallel safe; otherwise they run one
        at a time. Successful batches publish results in source order even when runners
        finish out of order.

        Yields:
            ToolBatchEvent: Request, start, approval, and result events. On cancellation
                it stops after publishing completed results; the caller settles the rest.

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

        async with closing_stream(self._prepare_calls()) as preparation_events:
            async for event in preparation_events:
                yield event
        if self.cancelled:
            return
        async with closing_stream(self._run_prepared_calls()) as execution_events:
            async for event in execution_events:
                yield event

    async def _prepare_calls(self) -> AsyncIterator[ToolBatchEvent]:
        """Prepare every requested call before any deferred side effect starts.

        Yields:
            ToolBatchEvent: Request, start, and approval events in source order.
        """

        for tool_call in self.tool_calls:
            yield self.settlement.request(tool_call)
            if self.is_cancelled():
                self.cancelled = True
                return

            yield tool_execution_started(tool_call)
            if self.is_cancelled():
                self.cancelled = True
                return

            lifecycle = self.settlement.begin_lifecycle(tool_call)
            prepared: PreparedToolExecution | None = None
            async with closing_stream(self.executor.prepare(tool_call)) as preparation:
                with RequestedCancellation(self.is_cancelled) as cancellation:
                    async for raw_event in preparation:
                        if isinstance(raw_event, PreparedToolExecution):
                            if prepared is not None:
                                raise ToolExecutionProtocolError(
                                    "Tool executor prepared more than one execution for "
                                    f"{tool_call.call_id}"
                                )
                            lifecycle.accept_prepared(raw_event)
                            prepared = raw_event
                            if self.is_cancelled():
                                self.cancelled = True
                                break
                            continue
                        if prepared is not None:
                            raise ToolExecutionProtocolError(
                                "Tool executor emitted an event after preparing "
                                f"{tool_call.call_id}"
                            )
                        event = lifecycle.accept(raw_event)
                        if isinstance(event, ToolExecutionEnded):
                            raise ToolExecutionProtocolError(
                                "Prepared tool executor emitted a terminal result "
                                f"during preparation for {tool_call.call_id}"
                            )
                        yield event.model_copy(deep=True)
                        if self.is_cancelled():
                            self.cancelled = True
                            break
                if cancellation.absorbed:
                    self.cancelled = True
            if self.cancelled:
                return
            if prepared is None:
                raise ToolExecutionProtocolError(
                    f"Tool executor ended preparation without a result for {tool_call.call_id}"
                )
            self._prepared_calls.append(
                _PreparedCallState(
                    tool_call=tool_call,
                    lifecycle=lifecycle,
                    execution=prepared,
                )
            )

    async def _run_prepared_calls(self) -> AsyncIterator[ToolBatchEvent]:
        """Run prepared calls and publish each completed chunk in source order.

        Yields:
            ToolBatchEvent: Adjacent terminal and provider-result pairs.
        """

        batch_is_parallel = all(call.execution.parallel_safe for call in self._prepared_calls)
        chunk_size = _MAX_PARALLEL_TOOL_EXECUTIONS if batch_is_parallel else 1
        for chunk_start in range(0, len(self._prepared_calls), chunk_size):
            if self.is_cancelled():
                self.cancelled = True
                return
            chunk = self._prepared_calls[chunk_start : chunk_start + chunk_size]
            outcomes, chunk_cancelled = await _run_prepared_chunk(
                chunk, is_cancelled=self.is_cancelled
            )
            self.cancelled = self.cancelled or chunk_cancelled or self.is_cancelled()
            fatal_error: Exception | None = None
            for call, outcome in zip(chunk, outcomes, strict=True):
                if outcome.error is not None:
                    if fatal_error is None:
                        fatal_error = outcome.error
                    continue
                if outcome.terminal is None:
                    if not self.cancelled and fatal_error is None:
                        fatal_error = ToolExecutionProtocolError(
                            "Prepared tool execution ended without a result for "
                            f"{call.tool_call.call_id}"
                        )
                    continue
                try:
                    call.lifecycle.accept(outcome.terminal)
                    terminal, result = self.settlement.publish_result(call.lifecycle.finish())
                except Exception as exc:  # Preserve the first source-order failure.
                    if fatal_error is None:
                        fatal_error = exc
                    continue
                yield terminal
                yield result
                self.cancelled = self.cancelled or self.is_cancelled()
            if self.cancelled:
                # Cancellation takes precedence over a sibling's stored runner error.
                return
            if fatal_error is not None:
                raise fatal_error
