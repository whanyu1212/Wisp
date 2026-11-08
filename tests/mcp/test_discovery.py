"""The on-demand MCP catalog and its search and describe tools (`wisp.mcp.discovery`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest

from wisp.mcp.discovery import (
    McpCatalog,
    McpDescribeTool,
    McpSearchTool,
    catalog_guidance,
    check_proxied_arguments,
)
from wisp.tool_types import ToolSafety
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult


@dataclass
class _Tool:
    remote_name: str
    summary: str
    server_name: str = "inventory"
    safety: ToolSafety = "read"
    input_schema: ToolInputSchema = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "count": {"type": ["integer", "null"]}},
            "required": ["sku"],
        }
    )

    @property
    def name(self) -> str:
        return f"mcp__{self.server_name}__{self.remote_name}"

    @property
    def description(self) -> str:
        return f"MCP server {self.server_name}: {self.summary}\nMore detail."

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text="ran")


def _catalog(*, connected: bool = True) -> McpCatalog:
    return McpCatalog.from_tools(
        [
            _Tool("get_stock_level", "Units on hand right now."),
            _Tool("get_reorder_policy", "The reorder point that triggers a purchase order."),
            _Tool("get_invoice", "Look up an invoice.", server_name="billing"),
        ],
        connected=lambda server: connected or server != "billing",
    )


def _search(catalog: McpCatalog, tmp_path: Path, **arguments: object) -> str:
    result = anyio.run(lambda: McpSearchTool(catalog).run(arguments, ToolContext(cwd=tmp_path)))
    return result.text


def test_search_ranks_name_matches_first(tmp_path: Path) -> None:
    text = _search(_catalog(), tmp_path, query="reorder point")

    lines = text.splitlines()
    assert lines[1].startswith("- mcp__inventory__get_reorder_policy (inventory): The reorder")
    assert "Parameters: sku*, count" in lines[1]
    assert "More detail" not in text and "MCP server" not in text


def test_search_lists_servers_for_empty_query(tmp_path: Path) -> None:
    assert _search(_catalog(), tmp_path, query=" ") == (
        "MCP servers in this run:\n- inventory: 2 tools\n- billing: 1 tool"
    )


def test_search_flags_disconnected_server(tmp_path: Path) -> None:
    assert "[server disconnected]" in _search(_catalog(connected=False), tmp_path, query="invoice")


@pytest.mark.parametrize("limit", [0, 21, True, "3"])
def test_search_rejects_bad_limit(tmp_path: Path, limit: object) -> None:
    with pytest.raises(ToolError, match="limit"):
        _search(_catalog(), tmp_path, query="stock", limit=limit)


def test_describe_returns_schema(tmp_path: Path) -> None:
    describe = McpDescribeTool(_catalog())
    context = ToolContext(cwd=tmp_path)

    text = anyio.run(lambda: describe.run({"name": "mcp__billing__get_invoice"}, context)).text

    assert text.startswith("mcp__billing__get_invoice (server billing)")
    assert '"required":["sku"]' in text
    with pytest.raises(ToolError) as error:
        anyio.run(lambda: describe.run({"name": "bash"}, context))
    assert error.value.failure_code == "unknown_tool"


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"sku": "A-1"}, None),
        ({"sku": "A-1", "count": None}, None),
        ({"sku": "A-1", "count": 2.0}, None),
        ({"sku": "A-1", "extra": object()}, None),
        ({}, "needs sku"),
        ({"sku": 1}, "sku must be string"),
        ({"sku": "A", "count": True}, "count must be integer or null"),
        (["A-1"], "must be an object"),
    ],
    ids=[
        "ok",
        "nullable",
        "whole-float",
        "unknown-key",
        "missing",
        "wrong-type",
        "bool-not-int",
        "not-object",
    ],
)
def test_proxied_arguments_checked_lightly(arguments: object, problem: str | None) -> None:
    entry = _catalog().entries[0]

    found = check_proxied_arguments(entry, arguments)

    assert found == problem if problem is None else problem in (found or "")


def test_guidance_names_servers() -> None:
    assert catalog_guidance(_catalog()) == (
        "MCP servers, found with mcp_search and run with mcp_call: "
        "inventory (2 tools), billing (1 tool)."
    )
    assert catalog_guidance(McpCatalog()) == ""


def test_search_matches_hyphenated_server(tmp_path: Path) -> None:
    catalog = McpCatalog.from_tools(
        [_Tool("list", "List rows.", server_name="my-db"), _Tool("list", "List items.")]
    )

    assert (
        _search(catalog, tmp_path, query="db list")
        .splitlines()[1]
        .startswith("- mcp__my-db__list (my-db)")
    )
