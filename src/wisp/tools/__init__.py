"""Tool contracts, policies, and built-in capability packages.

Built-in implementations are grouped under ``files``, ``search``, and ``shell``;
the package root remains the public surface for shared tool contracts.
"""

from .approval import ToolApprovalPolicy
from .base import Tool, ToolArguments, ToolInputSchema, ToolPromptMetadata, ToolSafety
from .context import ToolContext
from .policy import ToolPolicy
from .result import ToolError, ToolResult

__all__ = [
    "Tool",
    "ToolApprovalPolicy",
    "ToolArguments",
    "ToolContext",
    "ToolError",
    "ToolInputSchema",
    "ToolPolicy",
    "ToolPromptMetadata",
    "ToolResult",
    "ToolSafety",
]
