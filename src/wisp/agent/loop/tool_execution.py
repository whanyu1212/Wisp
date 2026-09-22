"""Execute one model-requested tool batch and retain provider-facing results."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass

from wisp.agent.tool_contracts import (
    PreparedToolExecutor,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.events import (
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
)
from wisp.providers.base import ToolCallResult
from wisp.providers.events import ToolCall

from .prepared_tools import _PreparedToolBatch
from .stream_cleanup import closing_stream
from .tool_lifecycle import (
    CancellationCheck,
    ToolBatchEvent,
    ToolExecutionLifecycle,
)


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
            prepared_batch = _PreparedToolBatch(
                self.tool_executor,
                self.tool_calls,
                is_cancelled=self.is_cancelled,
            )
            async with closing_stream(prepared_batch.events()) as prepared_events:
                async for event in prepared_events:
                    yield event
                    if isinstance(event, ToolResultReady):
                        results.append(_provider_result(event))
                        self.on_result(event)
            outcome = (
                CancelledToolBatch(tuple(results))
                if prepared_batch.cancelled
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
