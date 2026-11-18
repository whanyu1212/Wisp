"""Shared tool presentation-status helpers."""

from __future__ import annotations

from typing import Final

from wisp.events import ManagedProcessState, ToolPresentationStatus
from wisp.tool_types import ToolFailureCode

_FAILED_PROCESS_STATES: Final = frozenset({"failed", "timed_out"})
_FAILURE_REASONS: Final[dict[ToolFailureCode, str]] = {
    "approval_denied": "Approval denied",
    "internal_error": "Tool execution error",
    "invalid_arguments": "Invalid arguments",
    "invalid_pattern": "Invalid search pattern",
    "invalid_result": "Invalid tool result",
    "not_found": "Path or resource not found",
    "path_outside_workspace": "Path outside workspace",
    "policy_denied": "Blocked by policy",
    "stale_input": "Stale input",
    "timeout": "Timed out",
    "unknown_tool": "Unknown tool",
}


def tool_result_status(
    is_error: bool,
    exit_code: int | None,
    *,
    process_state: ManagedProcessState | None = None,
) -> ToolPresentationStatus:
    """Map promoted execution facts to the shared tool status vocabulary."""

    if process_state == "cancelled":
        return "cancelled"
    if tool_result_failed(is_error, exit_code, process_state=process_state):
        return "error"
    return "done"


def tool_failure_reason(
    is_error: bool,
    exit_code: int | None,
    *,
    failure_code: ToolFailureCode | None = None,
    process_state: ManagedProcessState | None = None,
) -> str | None:
    """Explain a failed result from typed facts, without interpreting command output.

    Args:
        is_error (bool): Whether the tool reported an error.
        exit_code (int | None): Promoted process exit status, when available.
        failure_code (ToolFailureCode | None): The tool's classified failure, if any.
        process_state (ManagedProcessState | None): Managed process lifecycle state.

    Returns:
        str | None: A short reason for an error status; None for success or
        cancellation. An explicit failure code takes precedence over exit status.
    """
    if tool_result_status(is_error, exit_code, process_state=process_state) != "error":
        return None
    if failure_code is not None:
        return _FAILURE_REASONS[failure_code]
    if process_state == "timed_out":
        return "Timed out"
    if exit_code is not None and exit_code != 0:
        return f"Command exited {exit_code}"
    if process_state == "failed":
        return "Process failed"
    return "Tool reported an error"


def tool_result_failed(
    is_error: bool,
    exit_code: int | None,
    *,
    process_state: ManagedProcessState | None = None,
) -> bool:
    """Whether a tool result should be presented as a failure."""

    if is_error:
        return True
    if process_state in _FAILED_PROCESS_STATES:
        return True
    return exit_code is not None and exit_code != 0
