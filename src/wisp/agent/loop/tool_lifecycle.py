"""Shared lifecycle validation and event contracts for tool batches."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from types import TracebackType

import anyio

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
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


class RequestedCancellation:
    """Absorb a scope cancellation only when the run requested it through its token.

    `AgentHarness.cancel()` sets the run's cancellation token and then cancels the
    scope advancing the loop, so a tool batch can settle unfinished calls before the
    turn ends. Any other cancellation, such as a caller's timeout, task
    cancellation, or an RPC command scope, belongs to the caller and propagates
    unchanged instead of being turned into a cancelled turn.

    Wrap the await that may be cancelled, then read `absorbed` after the block exits.

    Attributes:
        absorbed (bool): Whether a requested cancellation was caught by the block.

    Examples:
        Stop a batch only when the run asked for it::

            with RequestedCancellation(is_cancelled) as cancellation:
                async with anyio.create_task_group() as task_group:
                    ...
            if cancellation.absorbed:
                ...  # settle unfinished calls
    """

    __slots__ = ("_is_cancelled", "absorbed")

    def __init__(self, is_cancelled: CancellationCheck) -> None:
        """Create a guard for one cancellable block.

        Args:
            is_cancelled (CancellationCheck): The run's cancellation-token check.
        """
        self._is_cancelled = is_cancelled
        self.absorbed = False

    def __enter__(self) -> RequestedCancellation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Suppress the block's cancellation only when the run's token requested it.

        Args:
            exc_type (type[BaseException] | None): Exception type raised by the block.
            exc (BaseException | None): Exception raised by the block.
            traceback (TracebackType | None): Traceback of that exception.

        Returns:
            bool: True to suppress a requested cancellation; False lets every other
                exception, including an unrequested cancellation, propagate.

        Raises:
            Exception: An error from the cancellation check replaces the cancellation.
        """
        if exc_type is None or not issubclass(exc_type, anyio.get_cancelled_exc_class()):
            return False
        if not self._is_cancelled():
            return False
        self.absorbed = True
        return True


def tool_call_requested(tool_call: ToolCall) -> ToolCallRequested:
    """Build the public request event for a model-requested call.

    Args:
        tool_call (ToolCall): Requested call to publish.

    Returns:
        ToolCallRequested: Event whose arguments are detached from the provider's call.
    """
    return ToolCallRequested(
        call_id=tool_call.call_id,
        name=tool_call.name,
        arguments=deepcopy(dict(tool_call.arguments)),
    )


def tool_execution_started(tool_call: ToolCall) -> ToolExecutionStarted:
    """Build the public start event for a call about to reach its executor.

    Args:
        tool_call (ToolCall): Requested call to publish.

    Returns:
        ToolExecutionStarted: Event whose arguments are detached from the provider's call.
    """
    return ToolExecutionStarted(
        call_id=tool_call.call_id,
        name=tool_call.name,
        arguments=deepcopy(dict(tool_call.arguments)),
    )


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
class ToolBatchSettlement:
    """Guarantee that every call in one batch ends with exactly one published result.

    Execution paths record the calls they request, each call's lifecycle, and the
    results they publish. When a batch stops early, `settle_unfinished()` closes
    whatever they left open, so the guarantee holds however the calls were run.

    Calls are tracked by call ID. A provider may repeat an ID within one response;
    such a repeat is settled once, and a later occurrence's lifecycle replaces the
    earlier one.

    Args:
        tool_calls (Sequence[ToolCall]): Requested calls in model order.
    """

    tool_calls: Sequence[ToolCall]
    _requested: set[str] = field(default_factory=set, init=False)
    _published: set[str] = field(default_factory=set, init=False)
    _lifecycles: dict[str, ToolExecutionLifecycle] = field(default_factory=dict, init=False)

    def request(self, tool_call: ToolCall) -> ToolCallRequested:
        """Record a call as requested and build its public request event.

        Args:
            tool_call (ToolCall): Call about to be published.

        Returns:
            ToolCallRequested: Event whose arguments are detached from the provider's call.
        """
        self._requested.add(tool_call.call_id)
        return tool_call_requested(tool_call)

    def begin_lifecycle(self, tool_call: ToolCall) -> ToolExecutionLifecycle:
        """Start validating one occurrence of a call, visible to settlement.

        Args:
            tool_call (ToolCall): Call whose executor events will be validated.

        Returns:
            ToolExecutionLifecycle: A new lifecycle that replaces any earlier occurrence's.
        """
        lifecycle = ToolExecutionLifecycle(tool_call)
        self._lifecycles[tool_call.call_id] = lifecycle
        return lifecycle

    def publish_result(
        self, terminal: ToolExecutionEnded
    ) -> tuple[ToolExecutionEnded, ToolResultReady]:
        """Build a call's provider-facing result and record the pair as published.

        Args:
            terminal (ToolExecutionEnded): Validated terminal event for the call.

        Returns:
            tuple[ToolExecutionEnded, ToolResultReady]: The terminal and its result, to
                be published adjacently in that order.
        """
        result = ToolResultReady.from_execution_ended(terminal)
        self._published.add(terminal.call_id)
        return terminal, result

    def settle_unfinished(self) -> Iterator[ToolBatchEvent]:
        """Close every call that has no published result, in source order.

        This is a plain generator with no awaits, so a batch can settle after it
        absorbed a requested cancellation without reaching another checkpoint.

        Yields:
            ToolBatchEvent: For each unfinished call: its request event if it was never
                requested, a denial if its approval is still pending, then an
                interrupted terminal/result pair.

        Examples:
            >>> calls = (
            ...     ToolCall(call_id="a", name="read", arguments={}),
            ...     ToolCall(call_id="b", name="read", arguments={}),
            ... )
            >>> settlement = ToolBatchSettlement(calls)
            >>> _ = settlement.request(calls[0])
            >>> for event in settlement.settle_unfinished():
            ...     print(event.type, event.call_id)
            tool.execution.ended a
            tool.result a
            tool.call b
            tool.execution.ended b
            tool.result b
        """
        for tool_call in self.tool_calls:
            if tool_call.call_id in self._published:
                continue
            if tool_call.call_id not in self._requested:
                yield self.request(tool_call)
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
            yield from self.publish_result(terminal)


__all__ = [
    "CancellationCheck",
    "RequestedCancellation",
    "ToolBatchEvent",
    "ToolBatchSettlement",
    "ToolExecutionLifecycle",
    "tool_call_requested",
    "tool_execution_started",
]
