"""Execute one model-requested tool batch and retain provider-facing results."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    PreparedToolExecutor,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
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

from .prepared_tools import _prepared_tool_batch_events, _PreparedBatchStatus
from .stream_cleanup import closing_stream

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
    async with closing_stream(executor.execute(tool_call)) as execution:
        async for raw_event in execution:
            event = lifecycle.accept(raw_event)
            if not isinstance(event, ToolExecutionEnded):
                # Approval arguments may alias the executor's pending inputs.
                yield event.model_copy(deep=True)

    terminal = lifecycle.finish()
    yield terminal
    yield ToolResultReady.from_execution_ended(terminal)


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
                    arguments=deepcopy(dict(tool_call.arguments)),
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
            async with closing_stream(
                _prepared_tool_batch_events(
                    self.tool_executor,
                    self.tool_calls,
                    status,
                    is_cancelled=self.is_cancelled,
                )
            ) as prepared_events:
                async for event in prepared_events:
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
            yield ToolCallRequested(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=deepcopy(dict(tool_call.arguments)),
            )
            if self.is_cancelled():
                self.outcome = CancelledToolBatch(tuple(results))
                return
            yield ToolExecutionStarted(
                call_id=tool_call.call_id,
                name=tool_call.name,
                arguments=deepcopy(dict(tool_call.arguments)),
            )
            if self.is_cancelled():
                self.outcome = CancelledToolBatch(tuple(results))
                return

            result_event: ToolResultReady | None = None
            async with closing_stream(
                _execute_tool_call(self.tool_executor, tool_call)
            ) as execution_events:
                async for event in execution_events:
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
