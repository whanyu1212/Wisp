"""Adapt connected MCP tools to Wisp's local tool contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as McpToolDefinition

from wisp.mcp.config import McpServerConfig
from wisp.tool_types import ToolSafety
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text

_MAX_PROVIDER_TOOL_NAME_CHARS = 64
_MAX_SCHEMA_BYTES = 65_536
_MAX_DESCRIPTION_BYTES = 4_096
_MAX_DESCRIPTION_LINES = 40
_PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSUPPORTED_TOOL_CONTENT = "MCP tool returned unsupported non-text content"


class McpToolDefinitionError(ValueError):
    """Raised when a remote tool cannot be exposed safely to providers."""


class McpToolClient(Protocol):
    """Connected subset of the official MCP client used by an adapted tool."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult: ...


@dataclass(frozen=True, slots=True)
class McpTool:
    """One remote MCP tool exposed through Wisp's tool interface."""

    name: str
    description: str
    input_schema: ToolInputSchema
    safety: ToolSafety
    remote_name: str
    server_name: str
    _client: McpToolClient = field(repr=False, compare=False)

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        try:
            result = await self._client.call_tool(self.remote_name, dict(arguments))
        except Exception:  # noqa: BLE001 - transport details must not reach the model
            raise ToolError("MCP tool call failed") from None

        text_parts: list[str] = []
        for block in result.content:
            if not isinstance(block, TextContent):
                raise ToolError(_UNSUPPORTED_TOOL_CONTENT)
            text_parts.append(block.text)

        bounded = truncate_text(
            "\n".join(text_parts),
            max_bytes=max(0, context.max_output_bytes),
            max_lines=max(0, context.max_output_lines),
        )
        if result.is_error:
            raise ToolError(bounded.text or "MCP tool reported an error")
        return ToolResult(text=bounded.text, truncated=bounded.truncated)


def adapt_mcp_tool(
    *,
    server: McpServerConfig,
    tool: McpToolDefinition,
    client: McpToolClient,
) -> McpTool:
    """Validate and adapt one remote definition without opening a connection."""

    if not tool.name:
        raise _definition_error(server.name, "unnamed")
    input_schema = _copy_input_schema(server.name, tool.name, tool.input_schema)
    description_source = tool.description or tool.title
    if not description_source:
        description_source = f"MCP tool {tool.name}"
    description = truncate_text(
        f"MCP server {server.name}: {description_source}",
        max_bytes=_MAX_DESCRIPTION_BYTES,
        max_lines=_MAX_DESCRIPTION_LINES,
    ).text
    safety = dict(server.tool_safety).get(tool.name, "command")
    return McpTool(
        name=mcp_tool_name(server.name, tool.name),
        description=description,
        input_schema=input_schema,
        safety=safety,
        remote_name=tool.name,
        server_name=server.name,
        _client=client,
    )


def mcp_tool_name(server_name: str, remote_name: str) -> str:
    """Return a deterministic, provider-safe name for one remote tool."""

    prefix = f"mcp__{server_name}__"
    candidate = f"{prefix}{remote_name}"
    if (
        len(candidate) <= _MAX_PROVIDER_TOOL_NAME_CHARS
        and _PROVIDER_TOOL_NAME.fullmatch(candidate) is not None
    ):
        return candidate

    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", remote_name).strip("_-") or "tool"
    digest = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:10]
    suffix = f"__{digest}"
    readable_chars = _MAX_PROVIDER_TOOL_NAME_CHARS - len(prefix) - len(suffix)
    return f"{prefix}{readable[:readable_chars]}{suffix}"


def _copy_input_schema(
    server_name: str,
    remote_name: str,
    schema: Mapping[str, object],
) -> dict[str, object]:
    try:
        serialized = json.dumps(
            schema,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (RecursionError, TypeError, ValueError):
        raise _definition_error(server_name, remote_name) from None
    if len(serialized.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise _definition_error(server_name, remote_name)
    copied = json.loads(serialized)
    if not isinstance(copied, dict):
        raise _definition_error(server_name, remote_name)
    schema_type = copied.get("type")
    if schema_type is None:
        copied["type"] = "object"
    elif schema_type != "object":
        raise _definition_error(server_name, remote_name)
    return copied


def _definition_error(server_name: str, remote_name: str) -> McpToolDefinitionError:
    return McpToolDefinitionError(f"Invalid MCP tool definition for {server_name}/{remote_name}")


__all__ = [
    "McpTool",
    "McpToolClient",
    "McpToolDefinitionError",
    "adapt_mcp_tool",
    "mcp_tool_name",
]
