"""Runtime-owned MCP stdio connections and tool discovery."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import Tool as McpToolDefinition

from wisp.mcp.config import McpServerConfig
from wisp.mcp.tool import adapt_mcp_tool
from wisp.mcp.transport import bounded_stdio_client
from wisp.runtime.api import ExtensionAPI
from wisp.tools.base import Tool

MCP_STARTUP_TIMEOUT_SECONDS = 10.0
MAX_MCP_DISCOVERY_PAGES = 64
MAX_MCP_TOOLS_PER_SERVER = 64
MAX_MCP_TOOL_DEFINITION_BYTES_PER_SERVER = 1_048_576
MAX_MCP_TOOLS = 256
MAX_MCP_TOOL_DEFINITION_BYTES = 4_194_304

type McpDiagnosticCode = Literal[
    "missing-environment",
    "unavailable",
    "timeout",
    "invalid-discovery",
    "invalid-tool",
    "resource-limit",
    "name-collision",
]


@dataclass(frozen=True, slots=True)
class McpStartupDiagnostic:
    """One sanitized MCP startup problem safe for frontend delivery."""

    server_name: str
    code: McpDiagnosticCode

    @property
    def message(self) -> str:
        reasons: dict[McpDiagnosticCode, str] = {
            "missing-environment": "a configured environment variable is unavailable",
            "unavailable": "the server could not be started or initialized",
            "timeout": "startup or tool discovery timed out",
            "invalid-discovery": "the server returned an invalid tool catalog",
            "invalid-tool": "the server returned an invalid tool definition",
            "resource-limit": "the discovered tool catalog exceeded Wisp's limits",
            "name-collision": "a discovered tool name conflicts with another tool",
        }
        return f"MCP server {self.server_name} is unavailable: {reasons[self.code]}"


@dataclass(frozen=True, slots=True)
class _ConnectedServer:
    server: McpServerConfig
    client: Client
    definitions: tuple[McpToolDefinition, ...]
    definition_bytes: int
    release: anyio.Event


@dataclass(frozen=True, slots=True)
class _FailedServer:
    diagnostic: McpStartupDiagnostic


type _ServerResult = _ConnectedServer | _FailedServer


class _DiscoveryRejected(Exception):
    def __init__(self, code: McpDiagnosticCode) -> None:
        self.code = code


class McpRuntime:
    """Own connected MCP clients until the containing Wisp runtime closes."""

    def __init__(
        self,
        servers: tuple[McpServerConfig, ...],
        api: ExtensionAPI,
        existing_tool_names: tuple[str, ...],
    ) -> None:
        self._servers = servers
        self._api = api
        self._existing_tool_names = existing_tool_names
        self._close_requested = anyio.Event()
        self._started = anyio.Event()
        self._owner_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._start_error: BaseException | None = None
        self._close_error: BaseException | None = None
        self._diagnostics: tuple[McpStartupDiagnostic, ...] = ()

    @classmethod
    async def start(
        cls,
        servers: tuple[McpServerConfig, ...],
        *,
        api: ExtensionAPI,
        existing_tool_names: tuple[str, ...],
    ) -> McpRuntime:
        """Connect configured servers and register every accepted catalog."""

        runtime = cls(servers, api, existing_tool_names)
        runtime._owner_task = asyncio.create_task(runtime._run_owner())
        try:
            await runtime._started.wait()
        except BaseException:
            runtime._owner_task.cancel()
            with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
                await runtime._owner_task
            raise
        if runtime._start_error is not None:
            raise RuntimeError("Failed to initialize MCP runtime") from None
        return runtime

    @property
    def diagnostics(self) -> tuple[McpStartupDiagnostic, ...]:
        """Return sanitized startup diagnostics in server-name order."""

        return self._diagnostics

    async def aclose(self) -> None:
        """Close all MCP clients safely; repeated and concurrent calls share cleanup."""

        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close())
            close_task = self._close_task
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await asyncio.shield(close_task)
            raise

    async def _close(self) -> None:
        self._close_requested.set()
        owner_task = self._owner_task
        if owner_task is not None:
            await owner_task
        if self._close_error is not None:
            raise RuntimeError("Failed to close MCP runtime") from None

    async def _run_owner(self) -> None:
        connected: list[_ConnectedServer] = []
        try:
            send, receive = anyio.create_memory_object_stream[_ServerResult](len(self._servers))
            async with send, receive, anyio.create_task_group() as task_group:
                for server in self._servers:
                    task_group.start_soon(self._run_server, server, send.clone())
                results = [await receive.receive() for _ in self._servers]
                connected = [result for result in results if isinstance(result, _ConnectedServer)]
                self._diagnostics = self._register_results(results)
                self._started.set()
                if any(not result.release.is_set() for result in connected):
                    await self._close_requested.wait()
                for result in connected:
                    result.release.set()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._started.is_set():
                self._close_error = exc
            else:
                self._start_error = exc
            self._started.set()
            for result in connected:
                result.release.set()

    async def _run_server(
        self,
        server: McpServerConfig,
        send: anyio.abc.ObjectSendStream[_ServerResult],
    ) -> None:
        try:
            environment = _server_environment(server)
        except _DiscoveryRejected as exc:
            await send.send(_FailedServer(McpStartupDiagnostic(server.name, exc.code)))
            await send.aclose()
            return

        release = anyio.Event()
        stage: Literal["connect", "discover"] = "connect"
        try:
            async with AsyncExitStack() as stack:
                errlog = stack.enter_context(Path(os.devnull).open("w", encoding="utf-8"))
                parameters = StdioServerParameters(
                    command=server.command,
                    args=list(server.args),
                    env=environment,
                    cwd=_safe_server_cwd(),
                )
                async with asyncio.timeout(MCP_STARTUP_TIMEOUT_SECONDS):
                    client = await stack.enter_async_context(
                        Client(
                            bounded_stdio_client(parameters, errlog=errlog),
                            cache=None,
                        )
                    )
                    stage = "discover"
                    definitions, definition_bytes = await _discover_tools(client)
                await send.send(
                    _ConnectedServer(
                        server=server,
                        client=client,
                        definitions=definitions,
                        definition_bytes=definition_bytes,
                        release=release,
                    )
                )
                await send.aclose()
                await release.wait()
        except TimeoutError:
            await send.send(_FailedServer(McpStartupDiagnostic(server.name, "timeout")))
            await send.aclose()
        except _DiscoveryRejected as exc:
            await send.send(_FailedServer(McpStartupDiagnostic(server.name, exc.code)))
            await send.aclose()
        except Exception:  # noqa: BLE001 - transport and server details are untrusted
            code: McpDiagnosticCode = "unavailable" if stage == "connect" else "invalid-discovery"
            await send.send(_FailedServer(McpStartupDiagnostic(server.name, code)))
            await send.aclose()

    def _register_results(
        self,
        results: list[_ServerResult],
    ) -> tuple[McpStartupDiagnostic, ...]:
        diagnostics: list[McpStartupDiagnostic] = []
        registered_names = set(self._existing_tool_names)
        registered_tools = 0
        registered_definition_bytes = 0

        for result in sorted(
            results,
            key=lambda item: (
                item.server.name
                if isinstance(item, _ConnectedServer)
                else item.diagnostic.server_name
            ),
        ):
            if isinstance(result, _FailedServer):
                diagnostics.append(result.diagnostic)
                continue
            if not result.definitions:
                result.release.set()
                continue
            if (
                registered_tools + len(result.definitions) > MAX_MCP_TOOLS
                or registered_definition_bytes + result.definition_bytes
                > MAX_MCP_TOOL_DEFINITION_BYTES
            ):
                diagnostics.append(McpStartupDiagnostic(result.server.name, "resource-limit"))
                result.release.set()
                continue
            try:
                tools = tuple(
                    adapt_mcp_tool(server=result.server, tool=definition, client=result.client)
                    for definition in result.definitions
                )
            except Exception:  # noqa: BLE001 - remote definitions are untrusted
                diagnostics.append(McpStartupDiagnostic(result.server.name, "invalid-tool"))
                result.release.set()
                continue
            names = [tool.name for tool in tools]
            if len(names) != len(set(names)) or any(name in registered_names for name in names):
                diagnostics.append(McpStartupDiagnostic(result.server.name, "name-collision"))
                result.release.set()
                continue
            for tool in tools:
                self._api.register_tool(cast(Tool, tool), replace=False)
            registered_names.update(names)
            registered_tools += len(tools)
            registered_definition_bytes += result.definition_bytes
        return tuple(diagnostics)


async def _discover_tools(client: Client) -> tuple[tuple[McpToolDefinition, ...], int]:
    definitions: list[McpToolDefinition] = []
    names: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    definition_bytes = 0

    for _ in range(MAX_MCP_DISCOVERY_PAGES):
        page = await client.list_tools(cursor=cursor)
        if len(definitions) + len(page.tools) > MAX_MCP_TOOLS_PER_SERVER:
            raise _DiscoveryRejected("resource-limit")
        for definition in page.tools:
            if definition.name in names:
                raise _DiscoveryRejected("invalid-discovery")
            names.add(definition.name)
            try:
                serialized = definition.model_dump_json(by_alias=True, exclude_none=True)
                definition_bytes += len(serialized.encode("utf-8"))
            except (RecursionError, TypeError, UnicodeError, ValueError):
                raise _DiscoveryRejected("invalid-discovery") from None
            if definition_bytes > MAX_MCP_TOOL_DEFINITION_BYTES_PER_SERVER:
                raise _DiscoveryRejected("resource-limit")
            definitions.append(definition)
        next_cursor = page.next_cursor
        if next_cursor is None:
            return tuple(definitions), definition_bytes
        if next_cursor in seen_cursors:
            raise _DiscoveryRejected("invalid-discovery")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise _DiscoveryRejected("resource-limit")


def _server_environment(server: McpServerConfig) -> dict[str, str]:
    environment = {name: value.get_secret_value() for name, value in server.env}
    for name in server.env_from:
        value = os.environ.get(name)
        if value is None:
            raise _DiscoveryRejected("missing-environment")
        environment[name] = value
    return environment


def _safe_server_cwd() -> Path:
    return Path.home().expanduser().resolve(strict=False)


__all__ = ["McpRuntime", "McpStartupDiagnostic"]
