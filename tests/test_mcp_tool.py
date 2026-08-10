"""Tests for adapting connected MCP tools to Wisp tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import (
    CallToolResult,
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from mcp.types import (
    Tool as McpToolDefinition,
)

from wisp.mcp.config import McpServerConfig
from wisp.mcp.tool import (
    McpToolDefinitionError,
    adapt_mcp_tool,
    mcp_tool_name,
)
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError


class ScriptedMcpClient:
    def __init__(
        self,
        result: CallToolResult | None = None,
        *,
        error: Exception | None = None,
        checkpoint: bool = False,
    ) -> None:
        self.result = result or CallToolResult(content=[])
        self.error = error
        self.checkpoint = checkpoint
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if self.checkpoint:
            await anyio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.result


def _definition(
    name: str = "search",
    *,
    description: str | None = "Search remote records",
    input_schema: dict[str, object] | None = None,
    annotations: ToolAnnotations | None = None,
) -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        description=description,
        inputSchema=(
            input_schema if input_schema is not None else {"type": "object", "properties": {}}
        ),
        annotations=annotations,
    )


def _server(**values: object) -> McpServerConfig:
    return McpServerConfig(name="docs", command="server", **values)


def test_adapt_mcp_tool_exposes_provider_spec_and_default_safety() -> None:
    client = ScriptedMcpClient()
    adapted = adapt_mcp_tool(server=_server(), tool=_definition(), client=client)
    registry = ToolRegistry()
    registry.register(adapted)

    assert adapted.name == "mcp__docs__search"
    assert adapted.remote_name == "search"
    assert adapted.server_name == "docs"
    assert adapted.description == "MCP server docs: Search remote records"
    assert adapted.safety == "command"
    assert registry.specs()[0].name == adapted.name
    assert registry.specs()[0].input_schema == adapted.input_schema


@pytest.mark.parametrize("safety", ["read", "mutating", "command"])
def test_user_safety_override_uses_exact_remote_name(safety: str) -> None:
    server = _server(tool_safety={"search": safety, "other": "read"})

    adapted = adapt_mcp_tool(
        server=server,
        tool=_definition(annotations=ToolAnnotations(readOnlyHint=True)),
        client=ScriptedMcpClient(),
    )

    assert adapted.safety == safety


def test_server_read_only_annotation_cannot_weaken_default_safety() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(annotations=ToolAnnotations(readOnlyHint=True)),
        client=ScriptedMcpClient(),
    )

    assert adapted.safety == "command"


def test_existing_policy_and_approval_use_adapted_safety() -> None:
    default_tool = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(),
    )
    read_tool = adapt_mcp_tool(
        server=_server(tool_safety={"search": "read"}),
        tool=_definition(),
        client=ScriptedMcpClient(),
    )
    approval = ToolApprovalPolicy.require_approval()
    read_policy = ToolPolicy.allow_read_tools()

    assert approval.requires_approval(default_tool) is True
    assert approval.requires_approval(read_tool) is False
    assert read_policy.allows(default_tool) is False
    assert read_policy.allows(read_tool) is True


def test_description_fallback_and_bound() -> None:
    fallback = adapt_mcp_tool(
        server=_server(),
        tool=_definition(description=None),
        client=ScriptedMcpClient(),
    )
    bounded = adapt_mcp_tool(
        server=_server(),
        tool=_definition(description="x" * 5_000),
        client=ScriptedMcpClient(),
    )

    assert fallback.description == "MCP server docs: MCP tool search"
    assert len(bounded.description.encode("utf-8")) <= 4_096
    assert bounded.description.endswith("[truncated]")


def test_non_utf8_name_and_description_are_rejected() -> None:
    for definition in (
        _definition(name="\ud800"),
        _definition(description="\ud800"),
    ):
        with pytest.raises(McpToolDefinitionError):
            adapt_mcp_tool(
                server=_server(),
                tool=definition,
                client=ScriptedMcpClient(),
            )


def test_tool_name_is_readable_when_already_provider_safe() -> None:
    assert mcp_tool_name("github", "get_issue") == "mcp__github__get_issue"


def test_tool_name_hashes_invalid_and_oversized_names_deterministically() -> None:
    dotted = mcp_tool_name("github", "tools.search/v2")
    same = mcp_tool_name("github", "tools.search/v2")
    normalized_collision = mcp_tool_name("github", "tools:search/v2")
    oversized = mcp_tool_name("a" * 32, "x" * 128)

    assert dotted == same
    assert dotted != normalized_collision
    assert _provider_safe(dotted)
    assert _provider_safe(oversized)


def test_normalized_tool_name_cannot_overlap_direct_name_namespace() -> None:
    normalized = mcp_tool_name("docs", "foo.")
    direct_lookalike = normalized.removeprefix("mcp__docs__")

    assert mcp_tool_name("docs", direct_lookalike) != normalized


def test_tool_names_are_namespaced_by_server() -> None:
    assert mcp_tool_name("github", "search") != mcp_tool_name("gitlab", "search")


def test_schema_is_deeply_copied_and_defaults_to_object() -> None:
    properties: dict[str, object] = {"query": {"type": "string"}}
    schema = {"properties": properties}
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(input_schema=schema),
        client=ScriptedMcpClient(),
    )

    properties["query"] = {"type": "integer"}

    assert adapted.input_schema == {
        "properties": {"query": {"type": "string"}},
        "type": "object",
    }


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"type": "object", "default": float("nan")},
        {"type": "object", "secret-schema-value": {1, 2}},
        {"type": "object", "description": "\ud800"},
        {"type": "object", "required": "query"},
        {"type": "object", "$schema": {}},
        {"type": "object", "$schema": "https://invalid.example/schema"},
        {"type": "object", "description": "x" * 65_536},
    ],
)
def test_invalid_schemas_are_rejected_without_echoing_content(
    schema: dict[str, object],
) -> None:
    with pytest.raises(McpToolDefinitionError) as captured:
        adapt_mcp_tool(
            server=_server(),
            tool=_definition(input_schema=schema),
            client=ScriptedMcpClient(),
        )

    assert "secret-schema-value" not in str(captured.value)
    assert str(captured.value) == "Invalid MCP tool definition for docs/search"


def test_recursive_schema_is_rejected() -> None:
    schema: dict[str, object] = {"type": "object"}
    schema["self"] = schema

    with pytest.raises(McpToolDefinitionError):
        adapt_mcp_tool(
            server=_server(),
            tool=_definition(input_schema=schema),
            client=ScriptedMcpClient(),
        )


def test_invocation_uses_exact_remote_name_and_preserves_text_order() -> None:
    client = ScriptedMcpClient(
        CallToolResult(
            content=[TextContent(text="first"), TextContent(text="second")],
        )
    )
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(name="remote.search"),
        client=client,
    )

    result = anyio.run(
        adapted.run,
        {"query": "wisp"},
        ToolContext(cwd=_test_cwd()),
    )

    assert client.calls == [("remote.search", {"query": "wisp"})]
    assert result.text == "first\nsecond"
    assert result.truncated is False


def test_successful_output_is_bounded() -> None:
    client = ScriptedMcpClient(CallToolResult(content=[TextContent(text="abcdef")]))
    adapted = adapt_mcp_tool(server=_server(), tool=_definition(), client=client)

    result = anyio.run(
        adapted.run,
        {},
        ToolContext(cwd=_test_cwd(), max_output_bytes=5, max_output_lines=10),
    )

    assert result.text == "[trun"
    assert result.truncated is True


def test_remote_error_becomes_bounded_tool_error() -> None:
    client = ScriptedMcpClient(
        CallToolResult(content=[TextContent(text="sensitive-detail" * 10)], isError=True)
    )
    adapted = adapt_mcp_tool(server=_server(), tool=_definition(), client=client)

    with pytest.raises(ToolError) as captured:
        anyio.run(
            adapted.run,
            {},
            ToolContext(cwd=_test_cwd(), max_output_bytes=20, max_output_lines=10),
        )

    assert len(str(captured.value).encode("utf-8")) <= 20
    assert "sensitive-detail" not in str(captured.value)


def test_empty_remote_error_uses_generic_message() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(CallToolResult(content=[], isError=True)),
    )

    with pytest.raises(ToolError, match="MCP tool reported an error"):
        anyio.run(adapted.run, {}, ToolContext(cwd=_test_cwd()))


def test_structured_only_result_is_rejected() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(
            CallToolResult(content=[], structuredContent={"secret": "structured-result"})
        ),
    )

    with pytest.raises(ToolError, match="unsupported non-text content") as captured:
        anyio.run(adapted.run, {}, ToolContext(cwd=_test_cwd()))

    assert "structured-result" not in str(captured.value)


def test_structured_result_with_empty_compatibility_text_is_rejected() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(
            CallToolResult(
                content=[TextContent(text="")],
                structuredContent={"secret": "structured-result"},
            )
        ),
    )

    with pytest.raises(ToolError, match="unsupported non-text content") as captured:
        anyio.run(adapted.run, {}, ToolContext(cwd=_test_cwd()))

    assert "structured-result" not in str(captured.value)


def test_non_utf8_text_result_is_rejected() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(CallToolResult(content=[TextContent(text="\ud800")])),
    )

    with pytest.raises(ToolError, match="invalid text content") as captured:
        anyio.run(adapted.run, {}, ToolContext(cwd=_test_cwd()))

    assert "\ud800" not in str(captured.value)


def test_unsupported_content_does_not_expose_payload() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(
            CallToolResult(
                content=[TextContent(text="safe"), ImageContent(data="secret-data", mimeType="x")]
            )
        ),
    )

    with pytest.raises(ToolError, match="unsupported non-text content") as captured:
        anyio.run(adapted.run, {}, ToolContext(cwd=_test_cwd()))

    assert "secret-data" not in str(captured.value)


def test_client_exception_does_not_expose_transport_details() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(error=RuntimeError("token=super-secret")),
    )

    with pytest.raises(ToolError) as captured:
        anyio.run(adapted.run, {}, ToolContext(cwd=_test_cwd()))

    assert str(captured.value) == "MCP tool call failed"
    assert captured.value.__cause__ is None


def test_cancellation_propagates() -> None:
    adapted = adapt_mcp_tool(
        server=_server(),
        tool=_definition(),
        client=ScriptedMcpClient(checkpoint=True),
    )

    async def run() -> bool:
        completed = False
        with anyio.CancelScope() as scope:
            scope.cancel()
            await adapted.run({}, ToolContext(cwd=_test_cwd()))
            completed = True
        return completed

    assert anyio.run(run) is False


def test_official_sdk_in_memory_tool_round_trip() -> None:
    server = MCPServer("wisp-test")

    @server.tool()
    def add(left: int, right: int) -> str:
        return f"sum={left + right}"

    async def run() -> tuple[str, str, object]:
        async with Client(server, cache=None) as client:
            definitions = await client.list_tools()
            adapted = adapt_mcp_tool(
                server=_server(),
                tool=definitions.tools[0],
                client=client,
            )
            result = await adapted.run(
                {"left": 2, "right": 3},
                ToolContext(cwd=_test_cwd()),
            )
            return adapted.remote_name, result.text, adapted.input_schema

    remote_name, text, schema = anyio.run(run)

    assert remote_name == "add"
    assert text == "sum=5"
    assert isinstance(schema, dict)
    assert schema["type"] == "object"


def _provider_safe(name: str) -> bool:
    return len(name) <= 64 and all(character.isalnum() or character in "_-" for character in name)


def _test_cwd() -> Path:
    return Path(__file__).parent
