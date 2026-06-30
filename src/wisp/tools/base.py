"""Tool protocol for local capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult

ToolArguments = Mapping[str, object]
ToolInputSchema = Mapping[str, object]


class Tool(Protocol):
    """Protocol implemented by tools registered with the runtime."""

    name: str
    description: str
    input_schema: ToolInputSchema

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        """Execute the tool with JSON-like arguments."""
        ...
