"""Shared lifecycle validation and event contracts for tool batches."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
)
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


__all__ = [
    "CancellationCheck",
    "ToolBatchEvent",
    "ToolExecutionLifecycle",
    "tool_call_requested",
    "tool_execution_started",
]
