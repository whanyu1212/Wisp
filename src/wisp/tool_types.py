"""Shared tool value types without runtime dependencies."""

from typing import Literal

ToolSafety = Literal["read", "mutating", "command"]
ToolFailureCode = Literal[
    "approval_denied",
    "internal_error",
    "invalid_arguments",
    "invalid_pattern",
    "invalid_result",
    "not_found",
    "path_outside_workspace",
    "policy_denied",
    "stale_input",
    "timeout",
    "unknown_tool",
]
