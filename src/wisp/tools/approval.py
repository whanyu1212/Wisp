"""Tool approval policy for non-read tool execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from wisp.tools.base import Tool, ToolSafety
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError

if TYPE_CHECKING:
    from wisp.events import PermissionMode

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
    rule_id: str | None = None


@dataclass
class ToolApprovalPolicy:
    """Approval state for tools that are allowed by policy but require confirmation."""

    approved_tools: frozenset[str] = field(default_factory=frozenset)
    approved_safety: frozenset[ToolSafety] = field(default_factory=frozenset)
    # Elevated modes are session-only rather than saved in broad folders.
    # Keep the existing wire-facing name for compatibility.
    session_only_yolo: bool = field(default=False, kw_only=True)
    # Auto remains edit-only; auto_commands requires separate explicit consent.
    mode: PermissionMode = field(default="ask", kw_only=True)
    auto_command_read_dir: Path | None = field(default=None, kw_only=True)
    # Per-project saved bash command prefixes allowed without re-prompting.
    command_allow_rules: tuple[str, ...] = field(default=(), kw_only=True)
    # Revokes an in-flight command assessment when consent or project changes.
    generation: int = field(default=0, init=False, repr=False)

    @classmethod
    def require_approval(cls) -> ToolApprovalPolicy:
        """Require approval for mutating and command tools."""

        return cls()

    @classmethod
    def approve_all(cls) -> ToolApprovalPolicy:
        """Approve every tool safety class for non-interactive execution."""

        return cls(approved_safety=frozenset({"read", "mutating", "command"}), mode="yolo")

    @classmethod
    def approve_tool_names(cls, names: set[str] | frozenset[str]) -> ToolApprovalPolicy:
        """Approve an explicit set of tool names."""

        return cls(approved_tools=frozenset(names))

    @classmethod
    def auto_edits(cls) -> ToolApprovalPolicy:
        """Automatically approve eligible built-in workspace edits, and nothing else."""

        return cls(mode="auto")

    @classmethod
    def auto_commands(cls) -> ToolApprovalPolicy:
        """Opt into macOS sandboxed commands in addition to workspace edits.

        The executor requires Seatbelt for every automatic command. Supported
        test/build forms bypass model review; other eligible commands require the
        coding session's independent reviewer. A bare policy grants no commands.
        """

        return cls(mode="auto_commands")

    def suggested_command_prefix(self, tool: Tool, arguments: Mapping[str, object]) -> str | None:
        """Return a safe saveable prefix for a pending ``bash`` call, or None."""

        if (
            self.session_only_yolo
            or confirms_every_call(tool)
            or tool.name != "bash"
            or arguments.get("operation", "run") != "run"
        ):
            return None
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        from wisp.trust.permissions import suggest_command_prefix

        return suggest_command_prefix(command)

    def _evaluate_command_allow_rule(
        self, tool: Tool, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolApprovalDecision | None:
        if (
            not self.command_allow_rules
            or self.session_only_yolo
            or confirms_every_call(tool)
            or tool.name != "bash"
            or arguments.get("operation", "run") != "run"
        ):
            return None
        command = arguments.get("command")
        if not isinstance(command, str):
            return None
        from wisp.tools.shell.working_directory import command_context
        from wisp.trust.permissions import command_matches_allow_rule
        from wisp.trust.scope import is_broad_root

        try:
            boundary = context.boundary()
            if is_broad_root(boundary):
                return None
            resolved_ctx = command_context(arguments, context)
            cwd = resolved_ctx.cwd.resolve(strict=False)
            if is_broad_root(cwd) or not cwd.is_relative_to(boundary):
                return None
        except (OSError, ValueError, RuntimeError, ToolError):
            return None
        for rule in self.command_allow_rules:
            if command_matches_allow_rule(command, rule):
                return ToolApprovalDecision(
                    approved=True,
                    reason=f"Auto-approved by saved rule: {rule}",
                    rule_id="command_allow_rule_v1",
                )
        return None

    async def evaluate_call(
        self, tool: Tool, *, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolApprovalDecision | None:
        """Evaluate saved command rules and edit-only Auto before prompting.

        Args:
            tool (Tool): Resolved tool implementation.
            arguments (Mapping[str, object]): Exact proposed arguments.
            context (ToolContext): Execution-time workspace and path restrictions.

        Returns:
            ToolApprovalDecision | None: An automatic grant or path denial; None
                when the ordinary approval flow must decide. Never creates grants.
        """

        rule_decision = self._evaluate_command_allow_rule(tool, arguments, context)
        if rule_decision is not None:
            return rule_decision
        if self.mode not in ("auto", "auto_commands") or confirms_every_call(tool):
            return None
        # Local import keeps file-tool dependencies out of approval policy startup.
        from wisp.tools.auto_edits import eligible_workspace_edit

        try:
            eligible = await anyio.to_thread.run_sync(
                lambda: eligible_workspace_edit(tool, arguments, context)
            )
        except ToolError as exc:
            return ToolApprovalDecision(approved=False, reason=str(exc))
        except (OSError, ValueError):
            # An unreadable or unresolvable destination is not an automatic grant.
            return None
        # An explicit mode change while preflight runs revokes the pending grant.
        if not eligible or self.mode not in ("auto", "auto_commands"):
            return None
        return ToolApprovalDecision(
            approved=True,
            reason="Auto-approved: workspace edit",
            rule_id="workspace_edit_v1",
        )

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
