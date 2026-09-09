"""Execution, scheduling, and cancellation settlement for a batch of tool calls."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import cast

import anyio

from wisp.agent.execution import (
    PreparedToolExecution,
    PreparedToolExecutor,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
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

type CancellationCheck = Callable[[], bool]
type ToolBatchEvent = (
    ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolExecutionEnded
    | ToolResultReady
)


def _json_payloads_match(left: object, right: object) -> bool:
    """Compare JSON payloads without conflating booleans and numbers.

    Args:
        left (object): First JSON-compatible value.
        right (object): Second JSON-compatible value.

    Returns:
        bool: True when canonical JSON encodings match. False if either value cannot
            be encoded, including non-finite numbers.

    Examples:
        >>> _json_payloads_match({"enabled": True}, {"enabled": 1})
        False
        >>> _json_payloads_match({"a": 1, "b": 2}, {"b": 2, "a": 1})
        True
    """

    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ) == json.dumps(
            right,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return False


@dataclass(slots=True)
class ToolExecutionLifecycle:
    """Validate one executor stream before its result reaches the provider."""

    tool_call: ToolCall
    approval_requested: bool = False
    approval_resolved: bool = False
    approved: bool | None = None
    terminal: ToolExecutionEnded | None = None

    def accept(self, event: object) -> ToolExecutionEvent:
        """Validate and record the next event for this tool call.

        Args:
            event (object): Executor event to validate against call identity, approval
                order, original arguments, and terminal-result rules.

        Returns:
            ToolExecutionEvent: The same event after updating lifecycle state.

        Raises:
            ToolExecutionProtocolError: The event has the wrong type or call identity,
                violates approval ordering, follows a terminal result, or reports
                success after approval was denied.
        """
        if not isinstance(event, ToolApprovalRequested | ToolApprovalResolved | ToolExecutionEnded):
            raise ToolExecutionProtocolError(
                "Tool executor emitted an unsupported event type for "
                f"{self.tool_call.call_id}: {type(event).__name__}"
            )
        if self.terminal is not None:
            raise ToolExecutionProtocolError(
                f"Tool executor emitted an event after the result for {self.tool_call.call_id}"
            )
        if event.call_id != self.tool_call.call_id or event.name != self.tool_call.name:
            raise ToolExecutionProtocolError(
                "Tool executor event does not match the requested call: "
                f"expected {self.tool_call.name}/{self.tool_call.call_id}, "
                f"got {event.name}/{event.call_id}"
            )

        if isinstance(event, ToolApprovalRequested):
            if self.approval_requested:
                raise ToolExecutionProtocolError(
                    f"Tool executor requested approval more than once for {self.tool_call.call_id}"
                )
            if not _json_payloads_match(event.arguments, self.tool_call.arguments):
                raise ToolExecutionProtocolError(
                    "Tool executor approval arguments do not match the requested call "
                    f"{self.tool_call.call_id}"
                )
            self.approval_requested = True
        elif isinstance(event, ToolApprovalResolved):
            if not self.approval_requested:
                raise ToolExecutionProtocolError(
                    "Tool executor resolved approval before requesting it for "
                    f"{self.tool_call.call_id}"
                )
            if self.approval_resolved:
                raise ToolExecutionProtocolError(
                    f"Tool executor resolved approval more than once for {self.tool_call.call_id}"
                )
            self.approval_resolved = True
            self.approved = event.approved
        else:
            if self.approval_requested and not self.approval_resolved:
                raise ToolExecutionProtocolError(
                    f"Tool executor ended with an unresolved approval for {self.tool_call.call_id}"
                )
            if self.approved is False and not event.is_error:
                raise ToolExecutionProtocolError(
                    "Tool executor reported success after approval was denied for "
                    f"{self.tool_call.call_id}"
                )
            self.terminal = event
        return event

    def accept_prepared(self, prepared: PreparedToolExecution) -> None:
        """Validate a prepared execution before the scheduler may run it.

        Args:
            prepared (PreparedToolExecution): Deferred runner for this tool call.

        Raises:
            ToolExecutionProtocolError: A result already exists, call identity differs,
                or an earlier approval request remains unresolved.
        """
        if self.terminal is not None:
            raise ToolExecutionProtocolError(
                f"Tool executor prepared a call after the result for {self.tool_call.call_id}"
            )
        if prepared.call_id != self.tool_call.call_id or prepared.name != self.tool_call.name:
            raise ToolExecutionProtocolError(
                "Prepared execution does not match the requested call: "
                f"expected {self.tool_call.name}/{self.tool_call.call_id}, "
                f"got {prepared.name}/{prepared.call_id}"
            )
        if self.approval_requested and not self.approval_resolved:
            raise ToolExecutionProtocolError(
                f"Tool executor prepared {self.tool_call.call_id} with unresolved approval"
            )

    def finish(self) -> ToolExecutionEnded:
        """Require a settled executor lifecycle and retrieve its terminal result.

        Returns:
            ToolExecutionEnded: Previously accepted terminal event.

        Raises:
            ToolExecutionProtocolError: Approval is unresolved or no terminal result exists.
        """
        if self.approval_requested and not self.approval_resolved:
            raise ToolExecutionProtocolError(
                f"Tool executor ended with an unresolved approval for {self.tool_call.call_id}"
            )
        if self.terminal is None:
            raise ToolExecutionProtocolError(
                f"Tool executor ended without a result for {self.tool_call.call_id}"
            )
        return self.terminal


async def _execute_tool_call(
    executor: ToolExecutor,
    tool_call: ToolCall,
) -> AsyncIterator[ToolExecutionEvent | ToolResultReady]:
    """Run one executor stream and publish its validated terminal/result pair.

    Args:
        executor (ToolExecutor): Executor supplying approval and terminal events.
        tool_call (ToolCall): Requested call whose identity and arguments must be preserved.

    Yields:
        ToolExecutionEvent | ToolResultReady: Validated approval events, followed by
            adjacent ToolExecutionEnded and ToolResultReady events after stream exhaustion.

    Raises:
        ToolExecutionProtocolError: The executor's event stream is malformed or incomplete.
        Exception: Executor errors propagate rather than becoming synthetic tool outputs.
    """
    lifecycle = ToolExecutionLifecycle(tool_call)
    async for raw_event in executor.execute(tool_call):
        event = lifecycle.accept(raw_event)
        if not isinstance(event, ToolExecutionEnded):
            yield event

    terminal = lifecycle.finish()
    yield terminal
    yield ToolResultReady.from_execution_ended(terminal)


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


def _truncated_tool_call_events(
    tool_call: ToolCall,
) -> tuple[ToolExecutionEnded, ToolResultReady]:
    """Reject one call from an incomplete model response without invoking tools.

    Args:
        tool_call (ToolCall): Call emitted by a response whose finish reason is length.

    Returns:
        tuple[ToolExecutionEnded, ToolResultReady]: Synthetic invalid-arguments error
            and matching provider-visible result, in publication order.
    """

    terminal = ToolExecutionEnded(
        call_id=tool_call.call_id,
        name=tool_call.name,
        output=(
            "Tool call was not executed because the model response was truncated. "
            "Re-issue the call with complete arguments."
        ),
        is_error=True,
        failure_code="invalid_arguments",
        retryable=True,
        recovery_hint="Re-issue the tool call with complete arguments.",
    )
    return terminal, ToolResultReady.from_execution_ended(terminal)


def _provider_result(result_event: ToolResultReady) -> ToolCallResult:
    """Project a public tool result into the provider's continuation input.

    Args:
        result_event (ToolResultReady): Completed result with public execution metadata.

    Returns:
        ToolCallResult: Call ID, output text, and error status required by the provider.
    """
    return ToolCallResult(
        call_id=result_event.call_id,
        output=result_event.output,
        is_error=result_event.is_error,
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
                    arguments=dict(tool_call.arguments),
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
        arguments = dict(tool_call.arguments)
        requested_call_ids.add(tool_call.call_id)
        yield ToolCallRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=arguments,
        )
        if is_cancelled():
            status.cancelled = True
            break

        yield ToolExecutionStarted(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=arguments,
        )
        if is_cancelled():
            status.cancelled = True
            break

        lifecycle = ToolExecutionLifecycle(tool_call)
        lifecycles[tool_call.call_id] = lifecycle
        prepared: PreparedToolExecution | None = None
        preparation = executor.prepare(tool_call)
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
                        "Prepared tool executor emitted a terminal result during preparation for "
                        f"{tool_call.call_id}"
                    )
                yield event
                if is_cancelled():
                    status.cancelled = True
                    break
        except anyio.get_cancelled_exc_class():
            status.cancelled = True
        finally:
            close_preparation = getattr(preparation, "aclose", None)
            if callable(close_preparation):
                with anyio.CancelScope(shield=True):
                    await cast(Callable[[], Awaitable[None]], close_preparation)()
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


@dataclass(frozen=True, slots=True)
class CompletedToolBatch:
    """Carry ordered provider-facing results after a batch finishes."""

    results: tuple[ToolCallResult, ...]


@dataclass(frozen=True, slots=True)
class CancelledToolBatch:
    """Carry results collected before or during cancellation settlement."""

    results: tuple[ToolCallResult, ...]


type ToolBatchOutcome = CompletedToolBatch | CancelledToolBatch


@dataclass(slots=True)
class ToolBatch:
    """Execute one model-requested tool batch and retain its provider-facing results.

    Args:
        tool_executor (ToolExecutor): Executor, optionally supporting two-phase preparation.
        tool_calls (Sequence[ToolCall]): Calls to execute in source order.
        truncated (bool): Whether the model response was incomplete. If True, synthesize
            error results without executing tools.
        is_cancelled (CancellationCheck): Cooperative stop callback.
        on_result (Callable[[ToolResultReady], None]): Callback recording results retained
            by the batch after their public yield. Ordinary executor cancellation can
            omit the just-yielded result from both this callback and the outcome.
        outcome (ToolBatchOutcome | None, optional): Terminal result, initially None.
            Normally left unset and populated by exhausting events.
    """

    tool_executor: ToolExecutor
    tool_calls: Sequence[ToolCall]
    truncated: bool
    is_cancelled: CancellationCheck
    on_result: Callable[[ToolResultReady], None]
    outcome: ToolBatchOutcome | None = None

    async def events(self) -> AsyncIterator[ToolBatchEvent]:
        """Stream this batch's execution and store its terminal outcome.

        Yields:
            ToolBatchEvent: Tool lifecycle events. Prepared executors may run
                safe calls concurrently; ordinary executors run sequentially. Exhaust
                the iterator before reading outcome. A publicly yielded result is not
                necessarily retained when cancellation intervenes before its callback.

        Examples:
            Run a batch using an application-supplied executor and tool calls::

                async def collect_batch(executor, calls):
                    recorded_results = []
                    batch = ToolBatch(
                        tool_executor=executor, tool_calls=calls, truncated=False,
                        is_cancelled=lambda: False, on_result=recorded_results.append,
                    )
                    events = [event async for event in batch.events()]
                    return events, batch.outcome, recorded_results

        Raises:
            ToolExecutionProtocolError: An executor emits an invalid or incomplete lifecycle.
            Exception: Executor, cancellation-check, or result-callback errors propagate;
                an exception or early consumer exit may leave outcome unset.
        """
        results: list[ToolCallResult] = []

        if self.truncated:
            for tool_call in self.tool_calls:
                if self.is_cancelled():
                    self.outcome = CancelledToolBatch(tuple(results))
                    return
                yield ToolCallRequested(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                )
                terminal, result = _truncated_tool_call_events(tool_call)
                yield terminal
                yield result
                results.append(_provider_result(result))
                self.on_result(result)
                if self.is_cancelled():
                    self.outcome = CancelledToolBatch(tuple(results))
                    return
            self.outcome = CompletedToolBatch(tuple(results))
            return

        if isinstance(self.tool_executor, PreparedToolExecutor):
            status = _PreparedBatchStatus()
            async for event in _prepared_tool_batch_events(
                self.tool_executor,
                self.tool_calls,
                status,
                is_cancelled=self.is_cancelled,
            ):
                yield event
                if isinstance(event, ToolResultReady):
                    results.append(_provider_result(event))
                    self.on_result(event)
            outcome = (
                CancelledToolBatch(tuple(results))
                if status.cancelled
                else CompletedToolBatch(tuple(results))
            )
            self.outcome = outcome
            return

        for tool_call in self.tool_calls:
            if self.is_cancelled():
                self.outcome = CancelledToolBatch(tuple(results))
                return
            arguments = dict(tool_call.arguments)
            yield ToolCallRequested(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=arguments,
            )
            if self.is_cancelled():
                self.outcome = CancelledToolBatch(tuple(results))
                return
            yield ToolExecutionStarted(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=arguments,
            )
            if self.is_cancelled():
                self.outcome = CancelledToolBatch(tuple(results))
                return

            result_event: ToolResultReady | None = None
            async for event in _execute_tool_call(self.tool_executor, tool_call):
                yield event
                if isinstance(event, ToolResultReady):
                    result_event = event
                if self.is_cancelled() and not isinstance(event, ToolExecutionEnded):
                    self.outcome = CancelledToolBatch(tuple(results))
                    return
            if result_event is None:
                raise ToolExecutionProtocolError(
                    f"Tool executor produced no provider result for {tool_call.call_id}"
                )
            results.append(_provider_result(result_event))
            self.on_result(result_event)

        self.outcome = CompletedToolBatch(tuple(results))
