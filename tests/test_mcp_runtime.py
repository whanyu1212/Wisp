"""Tests for MCP stdio startup, discovery, registration, and cleanup."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.types import ListToolsResult
from mcp.types import Tool as McpToolDefinition
from pytest import MonkeyPatch

import wisp.mcp.runtime as mcp_runtime_module
from wisp import cli as cli_module
from wisp.config import WispConfig
from wisp.events import ErrorEvent
from wisp.mcp.config import McpServerConfig
from wisp.mcp.runtime import McpRuntime, _discover_tools
from wisp.rpc.host import InProcessOptions
from wisp.runtime.api import ExtensionAPI
from wisp.runtime.event_bus import EventBus
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.sdk import InProcessWisp
from wisp.tui.launch import TuiOptions, _preflight_tui_options


def _fixture_server(
    tmp_path: Path,
    *,
    name: str = "fixture",
    env: dict[str, str] | None = None,
    env_from: tuple[str, ...] = (),
) -> McpServerConfig:
    fixture = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
    values = {
        "WISP_MCP_TEST_CLOSED_FILE": str(tmp_path / f"{name}-closed"),
        **(env or {}),
    }
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=("-u", str(fixture)),
        env=values,
        env_from=env_from,
    )


def _api() -> tuple[ExtensionAPI, ToolRegistry]:
    tools = ToolRegistry()
    return (
        ExtensionAPI(providers=ProviderRegistry(), tools=tools, events=EventBus()),
        tools,
    )


def _definition(name: str = "search", *, schema_type: str = "object") -> McpToolDefinition:
    return McpToolDefinition(
        name=name,
        description="Search records",
        inputSchema={"type": schema_type},
    )


def test_missing_forwarded_environment_skips_server_without_naming_variable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    variable = "WISP_MCP_TEST_MISSING_SECRET"
    monkeypatch.delenv(variable, raising=False)
    server = _fixture_server(tmp_path, env_from=(variable,))

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        runtime = await build_runtime(mcp_servers=(server,))
        try:
            return runtime.tools.names(), tuple(event.message for event in runtime.startup_events)
        finally:
            await runtime.aclose()

    names, messages = anyio.run(scenario)

    assert not any(name.startswith("mcp__fixture__") for name in names)
    assert messages == (
        "MCP server fixture is unavailable: a configured environment variable is unavailable",
    )
    assert variable not in messages[0]


class _PagedClient:
    def __init__(self, pages: dict[str | None, ListToolsResult]) -> None:
        self.pages = pages
        self.cursors: list[str | None] = []

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        self.cursors.append(cursor)
        return self.pages[cursor]


def test_discovery_follows_pagination() -> None:
    client = _PagedClient(
        {
            None: ListToolsResult(tools=[_definition("first")], nextCursor="page-2"),
            "page-2": ListToolsResult(tools=[_definition("second")]),
        }
    )

    definitions, definition_bytes = anyio.run(_discover_tools, client)  # type: ignore[arg-type]

    assert [definition.name for definition in definitions] == ["first", "second"]
    assert definition_bytes > 0
    assert client.cursors == [None, "page-2"]


@pytest.mark.parametrize(
    "pages",
    [
        {
            None: ListToolsResult(tools=[_definition("first")], nextCursor="repeat"),
            "repeat": ListToolsResult(tools=[_definition("second")], nextCursor="repeat"),
        },
        {
            None: ListToolsResult(tools=[_definition("duplicate")], nextCursor="next"),
            "next": ListToolsResult(tools=[_definition("duplicate")]),
        },
    ],
)
def test_discovery_rejects_repeated_cursors_and_duplicate_names(
    pages: dict[str | None, ListToolsResult],
) -> None:
    with pytest.raises(mcp_runtime_module._DiscoveryRejected):
        anyio.run(_discover_tools, _PagedClient(pages))  # type: ignore[arg-type]


class _RecordingClient:
    definitions = (_definition(),)
    instances: list[_RecordingClient] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.enter_task: asyncio.Task[Any] | None = None
        self.exit_task: asyncio.Task[Any] | None = None
        self.read_timeout_seconds = _kwargs.get("read_timeout_seconds")
        self.__class__.instances.append(self)

    async def __aenter__(self) -> _RecordingClient:
        self.enter_task = asyncio.current_task()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exit_task = asyncio.current_task()

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        assert cursor is None
        return ListToolsResult(tools=list(self.definitions))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> object:
        raise AssertionError((name, arguments))


def test_client_context_closes_in_its_owner_task_from_cross_task_shutdown(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _RecordingClient.instances.clear()
    monkeypatch.setattr(mcp_runtime_module, "Client", _RecordingClient)
    monkeypatch.setattr(
        mcp_runtime_module, "bounded_stdio_client", lambda *_args, **_kwargs: object()
    )
    api, tools = _api()

    async def scenario() -> None:
        runtime = await McpRuntime.start(
            (_fixture_server(tmp_path),),
            api=api,
            existing_tool_names=(),
        )
        await asyncio.create_task(runtime.aclose())

    anyio.run(scenario)

    client = _RecordingClient.instances[0]
    assert client.enter_task is client.exit_task
    assert client.read_timeout_seconds is None
    assert "mcp__fixture__search" in tools.names()


def test_one_invalid_definition_rejects_the_whole_server_catalog(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class InvalidCatalogClient(_RecordingClient):
        definitions = (_definition("valid"), _definition("invalid", schema_type="string"))

    InvalidCatalogClient.instances.clear()
    monkeypatch.setattr(mcp_runtime_module, "Client", InvalidCatalogClient)
    monkeypatch.setattr(
        mcp_runtime_module, "bounded_stdio_client", lambda *_args, **_kwargs: object()
    )
    api, tools = _api()

    async def scenario() -> tuple[str, ...]:
        runtime = await McpRuntime.start(
            (_fixture_server(tmp_path),),
            api=api,
            existing_tool_names=(),
        )
        try:
            return tuple(diagnostic.code for diagnostic in runtime.diagnostics)
        finally:
            await runtime.aclose()

    diagnostics = anyio.run(scenario)

    assert diagnostics == ("invalid-tool",)
    assert tools.names() == ()


def test_catalog_collision_rejects_the_whole_server(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _RecordingClient.instances.clear()
    monkeypatch.setattr(mcp_runtime_module, "Client", _RecordingClient)
    monkeypatch.setattr(
        mcp_runtime_module, "bounded_stdio_client", lambda *_args, **_kwargs: object()
    )
    api, tools = _api()

    async def scenario() -> tuple[str, ...]:
        runtime = await McpRuntime.start(
            (_fixture_server(tmp_path),),
            api=api,
            existing_tool_names=("mcp__fixture__search",),
        )
        try:
            return tuple(diagnostic.code for diagnostic in runtime.diagnostics)
        finally:
            await runtime.aclose()

    diagnostics = anyio.run(scenario)

    assert diagnostics == ("name-collision",)
    assert tools.names() == ()


def test_aggregate_limit_rejects_later_server_atomically(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _RecordingClient.instances.clear()
    monkeypatch.setattr(mcp_runtime_module, "Client", _RecordingClient)
    monkeypatch.setattr(
        mcp_runtime_module, "bounded_stdio_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(mcp_runtime_module, "MAX_MCP_TOOLS", 1)
    api, tools = _api()

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        runtime = await McpRuntime.start(
            (
                _fixture_server(tmp_path, name="alpha"),
                _fixture_server(tmp_path, name="beta"),
            ),
            api=api,
            existing_tool_names=(),
        )
        try:
            return tools.names(), tuple(
                f"{diagnostic.server_name}:{diagnostic.code}" for diagnostic in runtime.diagnostics
            )
        finally:
            await asyncio.gather(runtime.aclose(), runtime.aclose())

    names, diagnostics = anyio.run(scenario)

    assert names == ("mcp__alpha__search",)
    assert diagnostics == ("beta:resource-limit",)
    assert all(client.enter_task is client.exit_task for client in _RecordingClient.instances)


def test_timed_out_server_is_isolated_from_concurrent_healthy_server(
    monkeypatch: MonkeyPatch,
) -> None:
    class SelectiveClient(_RecordingClient):
        def __init__(self, transport: object, **kwargs: object) -> None:
            super().__init__(transport, **kwargs)
            self.command = transport.command  # type: ignore[attr-defined]

        async def __aenter__(self) -> SelectiveClient:
            if self.command == "hanging":
                await anyio.sleep_forever()
            await super().__aenter__()
            return self

    SelectiveClient.instances.clear()
    monkeypatch.setattr(mcp_runtime_module, "Client", SelectiveClient)
    monkeypatch.setattr(
        mcp_runtime_module,
        "bounded_stdio_client",
        lambda parameters, **_kwargs: parameters,
    )
    monkeypatch.setattr(mcp_runtime_module, "MCP_STARTUP_TIMEOUT_SECONDS", 0.01)
    api, tools = _api()

    async def scenario() -> tuple[str, ...]:
        runtime = await McpRuntime.start(
            (
                McpServerConfig(name="healthy", command="healthy"),
                McpServerConfig(name="slow", command="hanging"),
            ),
            api=api,
            existing_tool_names=(),
        )
        try:
            return tuple(
                f"{diagnostic.server_name}:{diagnostic.code}" for diagnostic in runtime.diagnostics
            )
        finally:
            await runtime.aclose()

    diagnostics = anyio.run(scenario)

    assert tools.names() == ("mcp__healthy__search",)
    assert diagnostics == ("slow:timeout",)


def test_shutdown_failure_is_generic_and_redacted(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    secret = "transport-secret"

    class FailingExitClient(_RecordingClient):
        async def __aexit__(self, *_args: object) -> None:
            await super().__aexit__(*_args)
            raise RuntimeError(secret)

    FailingExitClient.instances.clear()
    monkeypatch.setattr(mcp_runtime_module, "Client", FailingExitClient)
    monkeypatch.setattr(
        mcp_runtime_module, "bounded_stdio_client", lambda *_args, **_kwargs: object()
    )
    api, _tools = _api()

    async def scenario() -> RuntimeError:
        runtime = await McpRuntime.start(
            (_fixture_server(tmp_path),),
            api=api,
            existing_tool_names=(),
        )
        with pytest.raises(RuntimeError) as captured:
            await runtime.aclose()
        return captured.value

    error = anyio.run(scenario)

    assert str(error) == "Failed to close MCP runtime"
    assert secret not in str(error)
    assert error.__cause__ is None


def test_sdk_receives_nonfatal_startup_diagnostic(tmp_path: Path) -> None:
    server = McpServerConfig(name="broken", command=str(tmp_path / "missing-server"))
    config = WispConfig(provider="fake", session_dir=tmp_path, mcp_servers=(server,))

    async def scenario() -> ErrorEvent:
        controller = await InProcessWisp.start(
            config,
            options=InProcessOptions(
                startup_trusted=True,
                allowed_tools=("mcp__broken__search",),
            ),
        )
        try:
            event = await anext(controller.events())
            assert isinstance(event, ErrorEvent)
            return event
        finally:
            await controller.aclose()

    event = anyio.run(scenario)

    assert event.message == (
        "MCP server broken is unavailable: the server could not be started or initialized"
    )


def test_print_mode_renders_startup_diagnostic_without_failing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = McpServerConfig(name="broken", command=str(tmp_path / "missing-server"))
    config = WispConfig(provider="fake", session_dir=tmp_path, mcp_servers=(server,))

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=config.mcp_servers)
        try:
            await cli_module._run_print_with_runtime(
                "hello",
                config,
                runtime,
                allowed_tools=("mcp__broken__search",),
                trusted=True,
            )
        finally:
            await runtime.aclose()

    anyio.run(scenario)

    captured = capsys.readouterr()
    assert "MCP server broken is unavailable" in captured.err
    assert captured.out


def test_tui_preflight_accepts_configured_mcp_tool_without_starting_server(
    tmp_path: Path,
) -> None:
    server = McpServerConfig(name="docs", command=str(tmp_path / "must-not-start"))
    options = TuiOptions(
        config=WispConfig(provider="fake", session_dir=tmp_path, mcp_servers=(server,)),
        allowed_tools=("mcp__docs__search",),
    )

    anyio.run(_preflight_tui_options, options)

    assert not (tmp_path / "must-not-start").exists()
