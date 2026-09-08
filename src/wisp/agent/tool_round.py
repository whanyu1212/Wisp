"""One tool-round lifecycle, scheduling, and cancellation settlement."""

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
type ToolRoundEvent = (
    ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | ToolExecutionEnded
    | ToolResultReady
)


def _json_payloads_match(left: object, right: object) -> bool:
    """Compare JSON payloads canonically without conflating booleans and numbers."""

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
    tool_call: ToolCall
    lifecycle: ToolExecutionLifecycle
    execution: PreparedToolExecution


@dataclass(slots=True)
class _PreparedBatchStatus:
    cancelled: bool = False


@dataclass(slots=True)
class _PreparedRunOutcome:
    terminal: ToolExecutionEnded | None = None
    error: Exception | None = None


async def _run_prepared_chunk(
    calls: Sequence[_PreparedCallState],
) -> tuple[tuple[_PreparedRunOutcome, ...], bool]:
    outcomes = [_PreparedRunOutcome() for _ in calls]

    async def run_one(index: int, call: _PreparedCallState) -> None:
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
    """Reject one call from an incomplete model response without invoking tools."""

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
) -> AsyncIterator[ToolRoundEvent]:
    prepared_calls: list[_PreparedCallState] = []
    lifecycles: dict[str, ToolExecutionLifecycle] = {}
    requested_call_ids: set[str] = set()
    result_call_ids: set[str] = set()

    async def finalize_interrupted() -> AsyncIterator[ToolRoundEvent]:
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
class CompletedToolRound:
    results: tuple[ToolCallResult, ...]


@dataclass(frozen=True, slots=True)
class CancelledToolRound:
    results: tuple[ToolCallResult, ...]


type ToolRoundOutcome = CompletedToolRound | CancelledToolRound


@dataclass(slots=True)
class ToolRound:
    """Stream one requested tool round and record its provider-facing result."""

    tool_executor: ToolExecutor
    tool_calls: Sequence[ToolCall]
    truncated: bool
    is_cancelled: CancellationCheck
    on_result: Callable[[ToolResultReady], None]
    outcome: ToolRoundOutcome | None = None

    async def events(self) -> AsyncIterator[ToolRoundEvent]:
        results: list[ToolCallResult] = []

        if self.truncated:
            for tool_call in self.tool_calls:
                if self.is_cancelled():
                    self.outcome = CancelledToolRound(tuple(results))
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
                    self.outcome = CancelledToolRound(tuple(results))
                    return
            self.outcome = CompletedToolRound(tuple(results))
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
                CancelledToolRound(tuple(results))
                if status.cancelled
                else CompletedToolRound(tuple(results))
            )
            self.outcome = outcome
            return

        for tool_call in self.tool_calls:
            if self.is_cancelled():
                self.outcome = CancelledToolRound(tuple(results))
                return
            arguments = dict(tool_call.arguments)
            yield ToolCallRequested(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=arguments,
            )
            if self.is_cancelled():
                self.outcome = CancelledToolRound(tuple(results))
                return
            yield ToolExecutionStarted(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=arguments,
            )
            if self.is_cancelled():
                self.outcome = CancelledToolRound(tuple(results))
                return

            result_event: ToolResultReady | None = None
            async for event in _execute_tool_call(self.tool_executor, tool_call):
                yield event
                if isinstance(event, ToolResultReady):
                    result_event = event
                if self.is_cancelled() and not isinstance(event, ToolExecutionEnded):
                    self.outcome = CancelledToolRound(tuple(results))
                    return
            if result_event is None:
                raise ToolExecutionProtocolError(
                    f"Tool executor produced no provider result for {tool_call.call_id}"
                )
            results.append(_provider_result(result_event))
            self.on_result(result_event)

        self.outcome = CompletedToolRound(tuple(results))
