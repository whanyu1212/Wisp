"""Prepared-executor scheduling and cancellation settlement for one tool batch."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import anyio

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    PreparedToolExecutor,
    ToolExecutionProtocolError,
)
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    ToolApprovalResolved,
    ToolExecutionEnded,
    ToolResultReady,
)
from wisp.providers.events import ToolCall

from .stream_cleanup import closing_stream
from .tool_lifecycle import (
    CancellationCheck,
    ToolBatchEvent,
    ToolExecutionLifecycle,
    tool_call_requested,
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


@dataclass(slots=True)
class _PreparedToolBatch:
    """Prepare, execute, and settle one two-phase tool batch.

    Each instance is single-use: consume events() once, then read cancelled.

    Args:
        executor (PreparedToolExecutor): Executor separating approval from side effects.
        tool_calls (Sequence[ToolCall]): Requested calls in model order.
        is_cancelled (CancellationCheck): Cooperative cancellation callback.
    """

    executor: PreparedToolExecutor
    tool_calls: Sequence[ToolCall]
    is_cancelled: CancellationCheck
    # Set while events() runs; ToolBatch reads it after the stream is exhausted.
    cancelled: bool = field(default=False, init=False)
    _prepared_calls: list[_PreparedCallState] = field(default_factory=list, init=False)
    _lifecycles: dict[str, ToolExecutionLifecycle] = field(default_factory=dict, init=False)
    _requested_call_ids: set[str] = field(default_factory=set, init=False)
    _result_call_ids: set[str] = field(default_factory=set, init=False)

    async def events(self) -> AsyncIterator[ToolBatchEvent]:
        """Prepare calls, execute eligible chunks, and settle cancellation.

        All calls are prepared before execution begins. Prepared calls run concurrently
        only if every prepared execution is marked parallel safe; otherwise they run one
        at a time. Successful batches publish results in source order even when runners
        finish out of order.

        Yields:
            ToolBatchEvent: Request, start, approval, and result events. Cancellation
                preserves completed results and settles remaining calls in source order.

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
            async with closing_stream(self._finalize_interrupted()) as interrupted_events:
                async for event in interrupted_events:
                    yield event
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
            self._requested_call_ids.add(tool_call.call_id)
            yield tool_call_requested(tool_call)
            if self.is_cancelled():
                self.cancelled = True
                return

            yield tool_execution_started(tool_call)
            if self.is_cancelled():
                self.cancelled = True
                return

            lifecycle = ToolExecutionLifecycle(tool_call)
            self._lifecycles[tool_call.call_id] = lifecycle
            prepared: PreparedToolExecution | None = None
            async with closing_stream(self.executor.prepare(tool_call)) as preparation:
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
                except anyio.get_cancelled_exc_class():
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
                async with closing_stream(self._finalize_interrupted()) as interrupted_events:
                    async for event in interrupted_events:
                        yield event
                return
            chunk = self._prepared_calls[chunk_start : chunk_start + chunk_size]
            outcomes, chunk_cancelled = await _run_prepared_chunk(chunk)
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
                    terminal = call.lifecycle.finish()
                    result = ToolResultReady.from_execution_ended(terminal)
                except Exception as exc:  # Preserve the first source-order failure.
                    if fatal_error is None:
                        fatal_error = exc
                    continue
                self._result_call_ids.add(call.tool_call.call_id)
                yield terminal
                yield result
                self.cancelled = self.cancelled or self.is_cancelled()
            if self.cancelled:
                async with closing_stream(self._finalize_interrupted()) as interrupted_events:
                    async for event in interrupted_events:
                        yield event
                return
            if fatal_error is not None:
                raise fatal_error

    async def _finalize_interrupted(self) -> AsyncIterator[ToolBatchEvent]:
        """Settle requested calls that have no published result yet.

        Yields:
            ToolBatchEvent: Missing request events, unresolved approval denials, and
                interrupted terminal/result pairs in source order.
        """
        for tool_call in self.tool_calls:
            if tool_call.call_id in self._result_call_ids:
                continue
            if tool_call.call_id not in self._requested_call_ids:
                self._requested_call_ids.add(tool_call.call_id)
                yield tool_call_requested(tool_call)
            lifecycle = self._lifecycles.get(tool_call.call_id)
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
            self._result_call_ids.add(tool_call.call_id)
            yield terminal
            yield result
