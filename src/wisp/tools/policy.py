"""Tool execution policy."""

from __future__ import annotations

from dataclasses import dataclass, field

from wisp.tools.base import Tool, ToolSafety


@dataclass(frozen=True)
class ToolPolicy:
    """Allow/deny policy for model-requested tool execution."""

    allow_all: bool = False
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_safety: frozenset[ToolSafety] = field(default_factory=frozenset)

    @classmethod
    def allow_all_tools(cls) -> ToolPolicy:
        """Allow every registered tool."""

        return cls(allow_all=True)

    @classmethod
    def allow_no_tools(cls) -> ToolPolicy:
        """Deny every tool."""

        return cls()

    @classmethod
    def allow_read_tools(cls) -> ToolPolicy:
        """Allow tools marked as read-only."""

        return cls(allowed_safety=frozenset({"read"}))

    @classmethod
    def allow_tool_names(cls, names: set[str] | frozenset[str]) -> ToolPolicy:
        """Allow an explicit set of tool names."""

        return cls(allowed_tools=frozenset(names))

    def allows(self, tool: Tool) -> bool:
        """Return whether a registered tool may be executed."""

        return (
            self.allow_all or tool.name in self.allowed_tools or tool.safety in self.allowed_safety
        )

    def block_reason(self, tool: Tool) -> str:
        """Return a model-visible reason for blocking a tool."""

        return f"Tool {tool.name} is blocked by policy"
