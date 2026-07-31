"""Shared tool presentation-status helpers."""

from __future__ import annotations

from typing import Final

from wisp.events import ManagedProcessState, ToolPresentationStatus

_FAILED_PROCESS_STATES: Final = frozenset({"failed", "timed_out"})


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
