"""Unified discovery describes the current run without granting execution permission."""

import json
from pathlib import Path

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from tests.coding.test_codemode import _done, _script, _Tool, _Tools
from wisp.coding.session import CodingSession
from wisp.events import ToolResultReady, WispEvent
from wisp.mcp.config import McpToolExposure
from wisp.providers.base import ToolSpec
from wisp.providers.events import ProviderEvent, ToolCall
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolPromptMetadata
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy


def _call(tool_name: str, **arguments: object) -> list[ProviderEvent]:
    return tool_call_stream(
        ToolCall(call_id=f"call-{tool_name}", name=tool_name, arguments=arguments)
    )


def _results(events: list[WispEvent]) -> list[ToolResultReady]:
    return [event for event in events if isinstance(event, ToolResultReady)]


@pytest.mark.parametrize("exposure", ["direct", "on_demand"])
def test_catalog_includes_both_sources(tmp_path: Path, exposure: McpToolExposure) -> None:
    tools = _Tools()
    registry = tools.registry()
    registry.register(tools.lookup, prompt=ToolPromptMetadata(guidelines=("Use current tables.",)))
    provider = ScriptedProvider(
        [
            _call("tool_search", query=""),
            _call("tool_describe", name="lookup"),
            _script("await mcp__db__query(table='current')"),
            _done(),
        ]
    )
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        codemode=True,
        mcp_tool_exposure=exposure,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("discover and query")]

    search, description, called = _results(anyio.run(run))
    assert not any(result.is_error for result in (search, description, called))
    entries = {entry["name"]: entry for entry in json.loads(search.output)["tools"]}
    assert entries["lookup"]["namespace"] == "builtin"
    assert entries["mcp__db__query"]["namespace"] == "mcp:db"
    assert entries["mcp__db__drop"]["requires_confirmation"] is True
    assert tools.drop.calls == []
    detail = json.loads(description.output)
    assert detail["guidelines"] == ["Use current tables."]
    assert detail["input_schema"]["required"] == ["table"]
    assert tools.query.calls == [{"table": "current"}]
    offered = {tool.name for tool in provider.calls[0].tools}
    assert {"codemode", "tool_search", "tool_describe"} <= offered
    assert "tool_search" not in registry.names()  # Per-run readers do not leak into startup state.


@pytest.mark.parametrize("restricted", ["policy", "operation"])
def test_catalog_respects_run_scope(tmp_path: Path, restricted: str) -> None:
    tools = _Tools()
    registry = tools.registry()
    provider = ScriptedProvider([_call("tool_search", query=""), _done()])
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tools=registry.specs(),  # Explicit specs must not bypass the session policy.
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=(
            ToolPolicy.allow_tool_names({"lookup"})
            if restricted == "policy"
            else ToolPolicy.allow_all_tools()
        ),
        codemode=True,
        mcp_tool_exposure="on_demand",
    )

    async def run() -> list[WispEvent]:
        return [
            event
            async for event in agent.run(
                "discover",
                operation_tool_names=frozenset({"lookup"}) if restricted == "operation" else None,
            )
        ]

    (result,) = _results(anyio.run(run))
    assert not result.is_error
    assert [entry["name"] for entry in json.loads(result.output)["tools"]] == ["lookup"]
    assert tools.query.calls == []
    if restricted == "policy":
        assert agent.scriptable_mcp_tools() == (0, 0)


def test_discovery_does_not_approve(tmp_path: Path) -> None:
    tools = _Tools()
    tools.lookup.confirm_every_call = True
    provider = ScriptedProvider(
        [_call("tool_describe", name="lookup"), _script("await lookup(table='private')"), _done()]
    )
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_approval_policy=ToolApprovalPolicy.approve_all(),
        codemode=True,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("describe then call")]

    description, denied = _results(anyio.run(run))
    assert not description.is_error
    assert json.loads(description.output)["requires_confirmation"] is True
    assert denied.is_error
    assert tools.lookup.calls == []


def test_existing_name_is_preserved(tmp_path: Path) -> None:
    tools = _Tools()
    tools.lookup.name = "tool_search"
    provider = ScriptedProvider([_done()])
    registry = tools.registry()
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        codemode=True,
    )

    async def run() -> None:
        async for _ in agent.run("hello"):
            pass

    anyio.run(run)
    offered = {tool.name: tool for tool in provider.calls[0].tools}
    assert offered["tool_search"] == ToolSpec.from_tool(tools.lookup)
    assert "tool_describe" not in offered


def test_discovery_returns_script_data(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            _script(
                "matches = await tool_search(query='lookup')\n"
                "detail = await tool_describe(name=matches['tools'][0]['name'])\n"
                "print(detail['input_schema']['required'])"
            ),
            _done(),
        ]
    )
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        codemode=True,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("discover in a script")]

    (result,) = _results(anyio.run(run))
    assert not result.is_error
    assert "['table']" in result.output


@pytest.mark.parametrize("name", ["mcp_search", "mcp_describe", "mcp_call"])
def test_custom_mcp_names_are_discoverable(tmp_path: Path, name: str) -> None:
    tools = _Tools()
    tools.lookup.name = name
    provider = ScriptedProvider([_call("tool_search", query=""), _done()])
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        codemode=True,
        mcp_tool_exposure="on_demand",
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("discover")]

    (result,) = _results(anyio.run(run))
    assert not result.is_error
    entries = json.loads(result.output)["tools"]
    assert any(entry["name"] == name and entry["namespace"] == "builtin" for entry in entries)


def test_proxy_entries_keep_return_contract(tmp_path: Path) -> None:
    tools = _Tools()
    tools.query.name = "mcp__db__query-with-dash"
    tools.query.output_schema = {"type": "object", "properties": {"id": {"type": "integer"}}}
    tools.query.structured = {"id": 42}
    provider = ScriptedProvider(
        [
            _call("tool_describe", name=tools.query.name),
            _script(
                "detail = await tool_describe(name='mcp__db__query-with-dash')\n"
                "reply = await mcp_call(name=detail['name'], arguments={'table': 'current'})\n"
                "print(reply)"
            ),
            _done(),
        ]
    )
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        codemode=True,
        mcp_tool_exposure="on_demand",
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("inspect and call")]

    described, called = _results(anyio.run(run))
    assert not described.is_error and not called.is_error
    detail = json.loads(described.output)
    assert detail["invocation"] == "mcp_call"
    assert detail["callable_name"] == "mcp_call"
    assert detail["result_type"] == "text"
    assert detail["output_schema"] == tools.query.output_schema
    assert "mcp__db__query-with-dash:current" in called.output
    assert tools.query.calls == [{"table": "current"}]


def test_large_catalog_keeps_prompt_bounded(tmp_path: Path) -> None:
    registry = ToolRegistry()
    for index in range(1000):
        registry.register(_Tool(f"lookup_{index}"))
    provider = ScriptedProvider([_done()])
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        codemode=True,
    )

    async def run() -> None:
        async for _ in agent.run("hello"):
            pass

    anyio.run(run)
    offered = {tool.name: tool for tool in provider.calls[0].tools}
    assert len(offered["codemode"].description.encode()) < 5000
    assert "lookup_999" not in offered["codemode"].description


@pytest.mark.parametrize("context", ["fresh", "fork"])
def test_subagent_discovery_follows_context(tmp_path: Path, context: str) -> None:
    child = [_call("tool_search", query="lookup"), _done()] if context == "fork" else [_done()]
    provider = ScriptedProvider(
        [_call("spawn_agent", tasks=["inspect available tools"], context=context), *child, _done()]
    )
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        codemode=True,
        subagents=True,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("delegate inspection")]

    results = _results(anyio.run(run))
    assert results and all(not result.is_error for result in results)
    child_tools = {tool.name for tool in provider.calls[1].tools}
    assert ("tool_search" in child_tools) is (context == "fork")
    if context == "fork":
        assert provider.calls[1].tools == provider.calls[0].tools
