"""Tool approval policy for non-read tool execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from wisp.tools.base import Tool, ToolSafety

APPROVAL_REQUIRED_SAFETY: frozenset[ToolSafety] = frozenset({"mutating", "command"})


def confirms_every_call(tool: Tool) -> bool:
    """Return whether every call of ``tool`` needs the user's own confirmation.

    Such a tool sets ``confirm_every_call = True``. No grant approves it: not
    ``--yes``, a saved YOLO mode, nor an "allow for this session" answer.

    Args:
        tool (Tool): Tool about to run.

    Returns:
        bool: True when only an explicit, one-time confirmation may approve it.
    """

    return getattr(tool, "confirm_every_call", False) is True


@dataclass(frozen=True)
class ToolApprovalDecision:
    """Decision for a tool call that requires approval."""

    approved: bool
    reason: str | None = None


@dataclass
class ToolApprovalPolicy:
    """Approval state for tools that are allowed by policy but require confirmation."""

    approved_tools: frozenset[str] = field(default_factory=frozenset)
    approved_safety: frozenset[ToolSafety] = field(default_factory=frozenset)
    # Set when choosing YOLO applies for the session only rather than being saved
    # for the project, as in the home folder.
    session_only_yolo: bool = field(default=False, kw_only=True)

    @classmethod
    def require_approval(cls) -> ToolApprovalPolicy:
        """Require approval for mutating and command tools."""

        return cls()

    @classmethod
    def approve_all(cls) -> ToolApprovalPolicy:
        """Approve every tool safety class for non-interactive execution."""

        return cls(approved_safety=frozenset({"read", "mutating", "command"}))

    @classmethod
    def approve_tool_names(cls, names: set[str] | frozenset[str]) -> ToolApprovalPolicy:
        """Approve an explicit set of tool names."""

        return cls(approved_tools=frozenset(names))

    def requires_approval(self, tool: Tool) -> bool:
        """Return whether this tool safety class requires approval."""

        return tool.safety in APPROVAL_REQUIRED_SAFETY or confirms_every_call(tool)

    def approves(self, tool: Tool) -> bool:
        """Return whether this tool is approved for execution."""

        if confirms_every_call(tool):
            return False
        return (
            not self.requires_approval(tool)
            or tool.name in self.approved_tools
            or tool.safety in self.approved_safety
        )

    def prepare_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> None:
        """Prepare to answer an approval request before the request is emitted."""

    async def await_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalDecision:
        """Return the approval decision for a tool call."""

        approved = self.approves(tool)
        return ToolApprovalDecision(
            approved=approved,
            reason=None if approved else self.block_reason(tool),
        )

    def cancel_approval(self, *, call_id: str, reason: str) -> bool:
        """Cancel one pending interactive decision, if this policy owns it."""

        del call_id, reason
        return False

    def block_reason(self, tool: Tool) -> str:
        """Return a model-visible reason for approval denial."""

        if confirms_every_call(tool):
            return (
                f"Tool {tool.name} needs the user's confirmation in the interactive TUI or "
                "an RPC client. --yes and saved permissions never approve it."
            )
        return (
            f"Tool {tool.name} requires approval before execution. "
            "In non-interactive print mode, rerun with --yes to approve mutating "
            "and command tools."
        )
