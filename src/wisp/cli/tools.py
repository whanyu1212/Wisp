"""Helpers for non-interactive CLI tool/session policy."""

from __future__ import annotations

from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy


def _print_mode_tool_approval_policy(approve_unsafe_tools: bool) -> ToolApprovalPolicy:
    if approve_unsafe_tools:
        return ToolApprovalPolicy.approve_all()
    return ToolApprovalPolicy.require_approval()


def _session_for_print_run(
    sessions: JsonlSessionStore,
    *,
    resume: str | None,
    continue_latest: bool,
) -> JsonlSession | None:
    if resume is not None:
        return sessions.load(resume)
    if continue_latest:
        return sessions.latest()
    return None


def _print_mode_tool_registry(
    tools: ToolRegistry,
    *,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
) -> ToolRegistry:
    """Return the tools an agent may use, filtered from the full registry.

    Availability and approval are separate axes: this decides which tools the
    agent can *see*; the approval policy decides whether calls to unsafe ones
    (mutating/command) need confirmation. A tool is admitted when any of:

    - ``all_tools`` is set (the whole registry — used by the interactive TUI,
      where unsafe calls are still gated by the approval prompt);
    - it is named in ``allowed_tools``;
    - ``allow_read_tools`` is set and the tool is read-only.
    """

    allowed_names = set(allowed_tools)
    for name in allowed_names:
        tools.get(name)

    filtered = ToolRegistry()
    for tool in tools.all():
        if all_tools or tool.name in allowed_names or (allow_read_tools and tool.safety == "read"):
            filtered.register(tool)
    return filtered
