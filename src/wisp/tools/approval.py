"""Tool approval policy for non-read tool execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from wisp.tools.base import Tool, ToolSafety

APPROVAL_REQUIRED_SAFETY: frozenset[ToolSafety] = frozenset({"mutating", "command"})


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
        """Return whether this tool requires approval before execution."""

        return tool.safety in APPROVAL_REQUIRED_SAFETY

    def approves(self, tool: Tool) -> bool:
        """Return whether this tool is approved for execution."""

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

    def block_reason(self, tool: Tool) -> str:
        """Return a model-visible reason for approval denial."""

        return (
            f"Tool {tool.name} requires approval before execution. "
            "In non-interactive print mode, rerun with --yes to approve mutating "
            "and command tools."
        )
