"""Tool execution result types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolResult:
    """Result returned by a Wisp tool invocation."""

    text: str
    data: Mapping[str, object] = field(default_factory=dict)
    truncated: bool = False


class ToolError(RuntimeError):
    """Raised when a tool invocation cannot be completed."""
