"""Adapt connected MCP tools to Wisp's local tool contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpToolDefinition

from wisp.images import MAX_IMAGE_BYTES, ImageRejectedError, inspect_image
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
# Match ``MAX_TOOL_RESULT_IMAGES`` in ``wisp.events.tools`` without importing ``wisp.events``.
_MAX_MCP_RESULT_IMAGES = 6
_MAX_IMAGE_BASE64_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4
# Cap raw whitespace-padded base64 before normalization so adversarial
# payloads cannot allocate unbounded strings.
_MAX_RAW_IMAGE_BASE64_CHARS = MAX_IMAGE_BYTES * 2
_ASCII_WHITESPACE = str.maketrans("", "", " \t\r\n")
_PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSUPPORTED_TOOL_CONTENT = "MCP tool returned unsupported non-text content"
_INVALID_TEXT_CONTENT = "MCP tool returned invalid text content"
_INVALID_IMAGE_CONTENT = "MCP tool returned invalid image content"
_TOO_MANY_IMAGES = f"MCP tool returned more than {_MAX_MCP_RESULT_IMAGES} images"
_NORMALIZED_NAME_MARKER = "_"


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
    # The server's schema for the tool's structured result, when it declares a valid
    # one. Only a codemode script receives that result; the model reads the text.
    output_schema: dict[str, object] | None = None

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        try:
            result = await self._client.call_tool(self.remote_name, dict(arguments))
        except Exception:  # noqa: BLE001 - transport details must not reach the model
            raise ToolError("MCP tool call failed") from None

        text_parts: list[str] = []
        images: list[bytes] = []
        image_descriptions: list[str] = []
        if result.is_error:
            text_parts.extend(
                block.text for block in result.content if isinstance(block, TextContent)
            )
        else:
            for block in result.content:
                if isinstance(block, TextContent):
                    text_parts.append(block.text)
                elif isinstance(block, ImageContent):
                    if len(images) >= _MAX_MCP_RESULT_IMAGES:
                        raise ToolError(_TOO_MANY_IMAGES, failure_code="invalid_result")
                    decoded, description = _decode_image_content(block)
                    images.append(decoded)
                    image_descriptions.append(description)
                else:
                    raise ToolError(_UNSUPPORTED_TOOL_CONTENT)

        text = "\n".join(text_parts)
        if not text.strip() and image_descriptions:
            text = "\n".join(image_descriptions)
        if result.structured_content is not None and not text.strip() and not images:
            raise ToolError(_UNSUPPORTED_TOOL_CONTENT)

        try:
            bounded = truncate_text(
                text,
                max_bytes=max(0, context.max_output_bytes),
                max_lines=max(0, context.max_output_lines),
            )
        except UnicodeEncodeError:
            raise ToolError(_INVALID_TEXT_CONTENT) from None
        if result.is_error:
            raise ToolError(bounded.text or "MCP tool reported an error")
        return ToolResult(
            text=bounded.text,
            truncated=bounded.truncated,
            images=tuple(images),
            structured=result.structured_content,
        )


def adapt_mcp_tool(
    *,
    server: McpServerConfig,
    tool: McpToolDefinition,
    client: McpToolClient,
) -> McpTool:
    """Validate and adapt one remote definition without opening a connection."""

    if not tool.name:
        raise _definition_error(server.name, "unnamed")
    try:
        tool.name.encode("utf-8")
    except UnicodeEncodeError:
        raise _definition_error(server.name, "invalid-name") from None
    input_schema = _copy_input_schema(server.name, tool.name, tool.input_schema)
    # A bad output schema only costs the tool its typed result, not the server its tools.
    output_schema = (
        _copied_object_schema(tool.output_schema) if tool.output_schema is not None else None
    )
    description_source = tool.description or tool.title
    if not description_source:
        description_source = f"MCP tool {tool.name}"
    raw_description = f"MCP server {server.name}: {description_source}"
    try:
        raw_description.encode("utf-8")
    except UnicodeEncodeError:
        raise _definition_error(server.name, tool.name) from None
    description = truncate_text(
        raw_description,
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
        output_schema=output_schema,
    )


def mcp_tool_name(server_name: str, remote_name: str) -> str:
    """Return a deterministic, provider-safe name for one remote tool."""

    prefix = f"mcp__{server_name}__"
    candidate = f"{prefix}{remote_name}"
    if (
        len(candidate) <= _MAX_PROVIDER_TOOL_NAME_CHARS
        and _PROVIDER_TOOL_NAME.fullmatch(candidate) is not None
        and not remote_name.startswith(_NORMALIZED_NAME_MARKER)
    ):
        return candidate

    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", remote_name).strip("_-") or "tool"
    digest = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:10]
    suffix = f"__{digest}"
    readable_chars = (
        _MAX_PROVIDER_TOOL_NAME_CHARS - len(prefix) - len(_NORMALIZED_NAME_MARKER) - len(suffix)
    )
    return f"{prefix}{_NORMALIZED_NAME_MARKER}{readable[:readable_chars]}{suffix}"


def _copy_input_schema(
    server_name: str,
    remote_name: str,
    schema: Mapping[str, object],
) -> dict[str, object]:
    copied = _copied_object_schema(schema)
    if copied is None:
        raise _definition_error(server_name, remote_name)
    return copied


def _copied_object_schema(schema: Mapping[str, object]) -> dict[str, object] | None:
    """Copy a server's object schema through JSON, or None when it is not a valid one.

    A schema with no `type` is an object schema; any other `type` is refused.
    """

    try:
        serialized = json.dumps(
            schema,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        encoded = serialized.encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        return None
    if len(encoded) > _MAX_SCHEMA_BYTES:
        return None
    copied = json.loads(serialized)
    if not isinstance(copied, dict):
        return None
    schema_type = copied.get("type")
    if schema_type is None:
        copied["type"] = "object"
        normalized = json.dumps(copied, ensure_ascii=False, separators=(",", ":"))
        if len(normalized.encode("utf-8")) > _MAX_SCHEMA_BYTES:
            return None
    elif schema_type != "object":
        return None
    try:
        if "$schema" in copied:
            # jsonschema supports None to disable fallback, but types-jsonschema omits it.
            validator = validator_for(copied, default=None)  # type: ignore[arg-type]
        else:
            validator = validator_for(copied)
        if validator is None:
            return None
        validator.check_schema(copied)
    except (SchemaError, TypeError):
        return None
    return copied


def _decode_image_content(block: ImageContent) -> tuple[bytes, str]:
    """Decode and validate one MCP image block within Wisp's size and format bounds."""

    raw = block.data
    if not isinstance(raw, str) or len(raw) > _MAX_RAW_IMAGE_BASE64_CHARS:
        raise ToolError(_INVALID_IMAGE_CONTENT, failure_code="invalid_result")
    compact = raw.translate(_ASCII_WHITESPACE)
    if not compact or len(compact) > _MAX_IMAGE_BASE64_CHARS:
        raise ToolError(_INVALID_IMAGE_CONTENT, failure_code="invalid_result")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ToolError(_INVALID_IMAGE_CONTENT, failure_code="invalid_result") from None
    try:
        part = inspect_image(data)
    except ImageRejectedError as exc:
        raise ToolError(f"{_INVALID_IMAGE_CONTENT}: {exc}", failure_code="invalid_result") from None
    return data, f"Image: {part.media_type}, {part.width}x{part.height} px, {part.byte_size} bytes."


def _definition_error(server_name: str, remote_name: str) -> McpToolDefinitionError:
    return McpToolDefinitionError(f"Invalid MCP tool definition for {server_name}/{remote_name}")


__all__ = [
    "McpTool",
    "McpToolClient",
    "McpToolDefinitionError",
    "adapt_mcp_tool",
    "mcp_tool_name",
]
