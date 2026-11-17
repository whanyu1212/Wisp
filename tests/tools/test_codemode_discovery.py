"""Unified discovery preserves callable contracts within bounded JSON replies."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.tools.support import run_tool
from wisp.tool_types import ToolSafety
from wisp.tools.base import ToolArguments
from wisp.tools.codemode.discovery import (
    DISCOVERY_TOOL_NAMES,
    MAX_DISCOVERY_BYTES,
    DiscoveryEntry,
    ToolCatalog,
    ToolDescribeTool,
    ToolSearchTool,
    discovery_tools,
)
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult


@dataclass
class CatalogTool:
    name: str
    description: str = "Read project files."
    input_schema: dict[str, object] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    )
    output_schema: dict[str, object] | None = None
    safety: ToolSafety = "read"

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        raise AssertionError("Discovery must never execute a catalog tool")


def _context(tmp_path: Path, budget: int = MAX_DISCOVERY_BYTES) -> ToolContext:
    return ToolContext(cwd=tmp_path, max_output_bytes=budget, max_output_lines=1)


def _payload(tool: object, arguments: dict[str, object], context: ToolContext) -> dict[str, object]:
    result = run_tool(tool, arguments, context)
    payload = json.loads(result.text)
    assert isinstance(payload, dict)
    assert payload == result.structured
    assert len(result.text.encode("utf-8")) <= min(context.max_output_bytes, MAX_DISCOVERY_BYTES)
    assert len(result.text.splitlines()) == 1
    return payload


def test_enumerates_filtered_catalog(tmp_path: Path) -> None:
    catalog = ToolCatalog(
        (
            DiscoveryEntry(CatalogTool("read_file"), "builtin"),
            DiscoveryEntry(
                CatalogTool("mcp_docs_read_file"),
                "mcp:docs",
                requires_confirmation=True,
                connected=False,
            ),
        )
    )
    tools = discovery_tools(catalog)
    assert tuple(tool.name for tool in tools) == DISCOVERY_TOOL_NAMES
    assert all(tool.safety == "read" for tool in tools)
    payload = _payload(tools[0], {"query": ""}, _context(tmp_path))
    assert payload == {
        "tools": [
            {
                "name": "read_file",
                "callable_name": "read_file",
                "invocation": "function",
                "namespace": "builtin",
                "safety": "read",
                "requires_confirmation": False,
                "connected": True,
                "summary": "Read project files.",
            },
            {
                "name": "mcp_docs_read_file",
                "callable_name": "mcp_docs_read_file",
                "invocation": "function",
                "namespace": "mcp:docs",
                "safety": "read",
                "requires_confirmation": True,
                "connected": False,
                "summary": "Read project files.",
            },
        ],
        "total_matches": 2,
        "next_offset": None,
    }
    # A denied tool is absent because only the owner's already filtered entries
    # are accepted. Discovery has no fallback registry to search.
    assert catalog.get("denied_tool") is None
    assert _payload(tools[0], {"query": "denied_tool"}, _context(tmp_path))["tools"] == []


def test_ranks_and_pages_stably(tmp_path: Path) -> None:
    entries = tuple(
        DiscoveryEntry(CatalogTool(f"read_{index}", "Find files"), "builtin")
        for index in range(1000)
    )
    catalog = ToolCatalog((DiscoveryEntry(CatalogTool("other", "read"), "builtin"), *entries))
    search = ToolSearchTool(catalog)
    first = _payload(search, {"query": "read", "limit": 2}, _context(tmp_path))
    second = _payload(search, {"query": "read", "limit": 2, "offset": 2}, _context(tmp_path))
    assert [entry["name"] for entry in first["tools"]] == ["read_0", "read_1"]
    assert [entry["name"] for entry in second["tools"]] == ["read_2", "read_3"]
    assert first["total_matches"] == 1001
    assert first["next_offset"] == 2
    assert "input_schema" not in first["tools"][0]


@pytest.mark.parametrize(
    ("name", "query"),
    [("读取", "读取"), ("读取", "读"), ("résumé", "RÉSUMÉ"), ("straße", "STRASSE")],
)
def test_searches_unicode_names(tmp_path: Path, name: str, query: str) -> None:
    catalog = ToolCatalog((DiscoveryEntry(CatalogTool(name), "builtin"),))
    payload = _payload(ToolSearchTool(catalog), {"query": query}, _context(tmp_path))
    assert [entry["name"] for entry in payload["tools"]] == [name]


def test_duplicate_names_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate discovery tool name: read"):
        ToolCatalog(
            (
                DiscoveryEntry(CatalogTool("read"), "builtin"),
                DiscoveryEntry(CatalogTool("read"), "mcp:docs"),
            )
        )


@pytest.mark.parametrize(
    ("schema", "kind", "receives"),
    [
        (None, "text", "receives its text"),
        (
            {"type": "object", "properties": {"id": {"type": "integer"}}},
            "dict",
            "receives a dict: id",
        ),
        (
            {
                "type": "object",
                "properties": {"result": {"type": "array", "items": {"type": "integer"}}},
                "required": ["result"],
            },
            "unwrapped",
            "receives the `result` value: list[int]",
        ),
        (
            {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
            "text",
            "receives its text",
        ),
    ],
)
def test_describes_script_returns(
    tmp_path: Path, schema: dict[str, object] | None, kind: str, receives: str
) -> None:
    tool = CatalogTool("read", output_schema=schema)
    entry = DiscoveryEntry(tool, "mcp:docs", ("Read before writing.",), True, False)
    payload = _payload(
        ToolDescribeTool(ToolCatalog((entry,))), {"name": "read"}, _context(tmp_path)
    )
    assert payload["input_schema"] == tool.input_schema
    assert payload["callable_name"] == "read"
    assert payload["invocation"] == "function"
    assert payload["output_schema"] == schema
    assert payload["result_type"] == kind
    assert receives in payload["script_receives"]
    assert payload["guidelines"] == ["Read before writing."]
    assert payload["requires_confirmation"] is True
    assert payload["connected"] is False
    assert payload["omitted_fields"] == {}


@pytest.mark.parametrize(
    "output_schema",
    [
        {"type": "object", "properties": {"id": {"type": "integer"}}},
        {
            "type": "object",
            "properties": {"result": {"type": "array", "items": {"type": "integer"}}},
            "required": ["result"],
        },
    ],
)
def test_proxy_contract_returns_text(tmp_path: Path, output_schema: dict[str, object]) -> None:
    tool = CatalogTool("mcp_docs_9-read", output_schema=output_schema)
    entry = DiscoveryEntry(tool, "mcp:docs", invocation="mcp_call")
    catalog = ToolCatalog((entry,))
    search = _payload(ToolSearchTool(catalog), {"query": tool.name}, _context(tmp_path))
    assert search["tools"][0]["name"] == tool.name
    assert search["tools"][0]["callable_name"] == "mcp_call"
    assert search["tools"][0]["invocation"] == "mcp_call"
    describe = _payload(ToolDescribeTool(catalog), {"name": tool.name}, _context(tmp_path))
    assert describe["name"] == tool.name
    assert describe["callable_name"] == "mcp_call"
    assert describe["invocation"] == "mcp_call"
    assert describe["input_schema"] == tool.input_schema
    assert describe["output_schema"] == output_schema
    assert describe["result_type"] == "text"
    assert "mcp_call(name=the exact tool name, arguments={...})" in describe["script_receives"]
    assert "receives the wrapper's text" in describe["script_receives"]
    assert describe["omitted_fields"] == {}


def test_large_fields_are_explicit(tmp_path: Path) -> None:
    schema: dict[str, object] = {"type": "object", "description": "行" * 30_000}
    entry = DiscoveryEntry(CatalogTool("read", "行" * 30_000, schema), "builtin", ("行" * 30_000,))
    describe = ToolDescribeTool(ToolCatalog((entry,)))
    payload = _payload(describe, {"name": "read"}, _context(tmp_path, 1024))
    assert "input_schema" not in payload
    assert payload["omitted_fields"]["input_schema"] == {
        "schema_path": "/input_schema",
        "size_bytes": len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()),
    }
    assert "description" in payload["omitted_fields"]
    assert "guidelines" in payload["omitted_fields"]
    search = _payload(
        ToolSearchTool(ToolCatalog((entry,))), {"query": ""}, _context(tmp_path, 1024)
    )
    assert search["tools"][0]["summary_omitted_chars"] > 0


def test_nested_schema_paging(tmp_path: Path) -> None:
    fields = {
        f"field/{index}~": {"type": "string", "description": "行" * 500} for index in range(8)
    }
    schema: dict[str, object] = {"type": "object", "properties": fields}
    describe = ToolDescribeTool(
        ToolCatalog((DiscoveryEntry(CatalogTool("read", input_schema=schema), "builtin"),))
    )
    context = _context(tmp_path, 900)
    root = _payload(describe, {"name": "read", "schema_path": "/input_schema"}, context)
    assert root["schema_kind"] == "object"
    assert root["schema_items"][1]["path"] == "/input_schema/properties"
    assert root["schema_items"][1]["omitted"] is True
    page = _payload(
        describe, {"name": "read", "schema_path": "/input_schema/properties", "limit": 2}, context
    )
    assert page["next_offset"] == 2
    assert page["total_items"] == 8
    assert [item["path"] for item in page["schema_items"]] == [
        "/input_schema/properties/field~10~0",
        "/input_schema/properties/field~11~0",
    ]
    next_page = _payload(
        describe,
        {"name": "read", "schema_path": "/input_schema/properties", "limit": 2, "offset": 2},
        context,
    )
    assert next_page["schema_items"][0]["path"] == "/input_schema/properties/field~12~0"
    leaf = _payload(
        describe,
        {"name": "read", "schema_path": "/input_schema/properties/field~10~0/type"},
        context,
    )
    assert leaf["schema_value"] == "string"
    scalar = _payload(
        describe,
        {"name": "read", "schema_path": "/input_schema/properties/field~10~0/description"},
        context,
    )
    assert scalar["omitted"] is True
    assert scalar["size_bytes"] == 1502


@pytest.mark.parametrize(
    ("tool_type", "arguments"),
    [
        (ToolSearchTool, {}),
        (ToolSearchTool, {"query": False}),
        (ToolSearchTool, {"query": "", "limit": True}),
        (ToolSearchTool, {"query": "", "limit": 21}),
        (ToolSearchTool, {"query": "", "offset": -1}),
        (ToolSearchTool, {"query": "", "extra": 1}),
        (ToolDescribeTool, {"name": False}),
        (ToolDescribeTool, {"name": "read", "schema_path": 1}),
        (ToolDescribeTool, {"name": "read", "schema_path": "/input_schema/~9"}),
        (ToolDescribeTool, {"name": "read", "schema_path": "/wrong"}),
        (ToolDescribeTool, {"name": "read", "schema_path": "/input_schema/missing"}),
        (ToolDescribeTool, {"name": "read", "offset": 0}),
    ],
)
def test_malformed_arguments_fail(
    tmp_path: Path,
    tool_type: type[ToolSearchTool] | type[ToolDescribeTool],
    arguments: dict[str, object],
) -> None:
    catalog = ToolCatalog((DiscoveryEntry(CatalogTool("read"), "builtin"),))
    with pytest.raises(ToolError) as error:
        run_tool(tool_type(catalog), arguments, _context(tmp_path))
    assert error.value.failure_code == "invalid_arguments"


def test_unknown_name_is_clear(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as error:
        run_tool(ToolDescribeTool(ToolCatalog()), {"name": "missing"}, _context(tmp_path))
    assert error.value.failure_code == "unknown_tool"
    assert error.value.recovery_hint == "Find the callable name with tool_search."


def test_optional_schema_matches_script(tmp_path: Path) -> None:
    tool = CatalogTool("read")
    # Third-party tools may expose unrelated metadata under this optional name.
    tool.output_schema = "not a schema"  # type: ignore[assignment]
    describe = ToolDescribeTool(ToolCatalog((DiscoveryEntry(tool, "builtin"),)))
    payload = _payload(describe, {"name": "read"}, _context(tmp_path))
    assert payload["output_schema"] is None
    assert payload["result_type"] == "text"


def test_unicode_separators_stay_json(tmp_path: Path) -> None:
    schema: dict[str, object] = {"description": "first\u0085second\u2028third\u2029last"}
    describe = ToolDescribeTool(
        ToolCatalog((DiscoveryEntry(CatalogTool("read", input_schema=schema), "builtin"),))
    )
    payload = _payload(describe, {"name": "read"}, _context(tmp_path))
    assert payload["input_schema"] == schema


def test_schema_array_paths(tmp_path: Path) -> None:
    schema: dict[str, object] = {"enum": ["first", "second", "行" * 1000]}
    describe = ToolDescribeTool(
        ToolCatalog((DiscoveryEntry(CatalogTool("read", input_schema=schema), "builtin"),))
    )
    payload = _payload(
        describe,
        {"name": "read", "schema_path": "/input_schema/enum", "limit": 2},
        _context(tmp_path, 600),
    )
    assert payload["schema_kind"] == "array"
    assert payload["schema_items"] == [
        {"path": "/input_schema/enum/0", "value": "first"},
        {"path": "/input_schema/enum/1", "value": "second"},
    ]
    assert payload["next_offset"] == 2
    leaf = _payload(
        describe, {"name": "read", "schema_path": "/input_schema/enum/1"}, _context(tmp_path)
    )
    assert leaf["schema_value"] == "second"
    for invalid_index in ("01", "-1", "9" * 5000):
        with pytest.raises(ToolError) as error:
            run_tool(
                describe,
                {"name": "read", "schema_path": "/input_schema/enum/" + invalid_index},
                _context(tmp_path),
            )
        assert error.value.failure_code == "invalid_arguments"


@pytest.mark.parametrize("budget", [0, 1, 40])
def test_tiny_budget_fails_clearly(tmp_path: Path, budget: int) -> None:
    catalog = ToolCatalog((DiscoveryEntry(CatalogTool("read", "行" * 1000), "builtin"),))
    with pytest.raises(ToolError, match="budget is too small"):
        run_tool(ToolSearchTool(catalog), {"query": ""}, _context(tmp_path, budget))


def test_large_search_pages_fit(tmp_path: Path) -> None:
    catalog = ToolCatalog(
        tuple(
            DiscoveryEntry(CatalogTool(f"tool_{index}", "行" * 2000), "builtin")
            for index in range(200)
        )
    )
    context = _context(tmp_path, 700)
    search = ToolSearchTool(catalog)
    names = []
    offset = 0
    while True:
        payload = _payload(search, {"query": "", "limit": 20, "offset": offset}, context)
        names.extend(entry["name"] for entry in payload["tools"])
        if payload["next_offset"] is None:
            break
        assert payload["next_offset"] > offset
        offset = payload["next_offset"]
    assert names == [f"tool_{index}" for index in range(200)]
