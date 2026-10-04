"""Helpers shared by the built-in tool tests."""

from __future__ import annotations

import anyio

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


def run_tool(tool: object, arguments: dict[str, object], context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        result = await tool.run(arguments, context)  # type: ignore[attr-defined]
        assert isinstance(result, ToolResult)
        return result

    return anyio.run(run)
