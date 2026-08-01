"""Shared tool-exposure and initial-session selection policy.

Frontends decide which registered tools are exposed to a run here; execution and
approval remain owned by :class:`~wisp.coding.CodingSession` and its tool policy.
"""

from __future__ import annotations

from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy


def tool_approval_policy(approve_unsafe_tools: bool) -> ToolApprovalPolicy:
    """Return the explicit approval policy selected by an embedding frontend."""

    if approve_unsafe_tools:
        return ToolApprovalPolicy.approve_all()
    return ToolApprovalPolicy.require_approval()


def select_session(
    sessions: JsonlSessionStore,
    *,
    resume: str | None,
    continue_latest: bool,
) -> JsonlSession | None:
    """Resolve an optional initial persisted session without creating a new one."""

    if resume is not None:
        return sessions.load(resume)
    if continue_latest:
        return sessions.latest()
    return None


def select_tools(
    tools: ToolRegistry,
    *,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
) -> ToolRegistry:
    """Filter registered tools without weakening their approval requirements.

    A tool is exposed when all tools are selected, it is explicitly named, or it
    is read-only and ``allow_read_tools`` is set.  Named unknown tools fail before
    a run begins.  Exposing a mutating or command tool never auto-approves it.
    """

    allowed_names = set(allowed_tools)
    for name in allowed_names:
        tools.get(name)

    filtered = ToolRegistry()
    for tool in tools.all():
        if all_tools or tool.name in allowed_names or (allow_read_tools and tool.safety == "read"):
            filtered.register(tool)
    return filtered
