"""Compatibility helpers for non-interactive CLI tool/session policy.

Shared policy lives in :mod:`wisp.tools.selection` so the in-process SDK uses
exactly the same exposure and approval rules.
"""

from wisp.tools.selection import select_session, select_tools, tool_approval_policy

_print_mode_tool_approval_policy = tool_approval_policy
_print_mode_tool_registry = select_tools
_session_for_print_run = select_session

__all__ = [
    "_print_mode_tool_approval_policy",
    "_print_mode_tool_registry",
    "_session_for_print_run",
]
