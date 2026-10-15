"""Extension activation helpers.

This module intentionally supports only built-in/static extension factories for
now. Loading arbitrary *project-local* extensions must be gated on project trust
(see :mod:`wisp.trust` and ``CodingSession.trusted``): an untrusted project's code must not
be loaded. That gate now exists; wiring project-local extension discovery behind it
is future work (roadmap ``Extension System``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from wisp.auth.storage import JsonAuthStore
from wisp.config.runtime import default_auth_path
from wisp.events import ErrorEvent
from wisp.extensions import builtin
from wisp.mcp.config import McpServerConfig
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.providers.catalog import ModelRegistry, effective_catalog
from wisp.retry import RetryPolicy
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.commands import CommandRegistry
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry

if TYPE_CHECKING:
    from wisp.mcp.runtime import McpRuntime
    from wisp.providers.anthropic import AnthropicCacheTtl
from wisp.tools.shell.supervisor import ProcessSupervisor

type ExtensionFactory = Callable[[ExtensionAPI], Awaitable[None] | None]


async def build_runtime(
    *,
    auth_path: Path | None = None,
    retry_policy: RetryPolicy | None = None,
    mcp_servers: tuple[McpServerConfig, ...] = (),
    openai_compatible: OpenAICompatibleSettings | None = None,
    anthropic_cache_ttl: AnthropicCacheTtl = "5m",
) -> WispRuntime:
    """Create runtime state and activate built-in extensions."""

    providers = ProviderRegistry()
    tools = ToolRegistry()
    commands = CommandRegistry()
    events = EventBus()
    process_supervisor = ProcessSupervisor()
    api = ExtensionAPI(
        providers=providers,
        tools=tools,
        commands=commands,
        events=events,
        process_supervisor=process_supervisor,
    )
    models = ModelRegistry(
        await anyio.to_thread.run_sync(effective_catalog, abandon_on_cancel=True)
    )
    mcp_runtime: McpRuntime | None = None
    auth_store = JsonAuthStore(auth_path or default_auth_path())
    openai_compatible_provider = (
        openai_compatible.provider_name if openai_compatible is not None else None
    )
    try:
        await activate_builtin_extensions(
            api,
            auth_store=auth_store,
            retry_policy=retry_policy,
            process_supervisor=process_supervisor,
            openai_compatible=openai_compatible,
            anthropic_cache_ttl=anthropic_cache_ttl,
        )
        enabled_servers = tuple(server for server in mcp_servers if server.enabled)
        if enabled_servers:
            # `mcp` costs ~430 ms to import and is only reachable through this
            # branch, which already runs solely when servers are configured. Import
            # it here so a session without MCP servers never pays for it.
            from wisp.mcp.runtime import McpRuntime

            mcp_runtime = await McpRuntime.start(
                enabled_servers,
                api=api,
                existing_tool_names=tools.names(),
            )
    except BaseException:
        try:
            if mcp_runtime is not None:
                await mcp_runtime.aclose()
        finally:
            await process_supervisor.aclose()
        raise
    return WispRuntime(
        providers=providers,
        tools=tools,
        commands=commands,
        events=events,
        api=api,
        models=models,
        process_supervisor=process_supervisor,
        mcp_runtime=mcp_runtime,
        auth_store=auth_store,
        openai_compatible_provider=openai_compatible_provider,
        openai_compatible_requires_api_key=(
            openai_compatible.requires_api_key if openai_compatible is not None else True
        ),
        startup_events=(
            tuple(ErrorEvent(message=diagnostic.message) for diagnostic in mcp_runtime.diagnostics)
            if mcp_runtime is not None
            else ()
        ),
        disabled_mcp_servers=tuple(server.name for server in mcp_servers if not server.enabled),
        unavailable_tool_prefixes=_unavailable_tool_prefixes(mcp_servers, mcp_runtime),
    )


async def restart_mcp_servers(
    runtime: WispRuntime,
    servers: tuple[McpServerConfig, ...],
) -> None:
    """Replace the runtime's MCP connections with ``servers`` and their tools.

    Every current server is stopped and its tools unregistered before the enabled
    servers in ``servers`` start, so two copies of one server never run at once and
    a renamed or removed server's tools cannot linger. Servers that fail to start
    are reported through ``runtime.mcp_runtime.diagnostics``, as at startup. The
    runtime's MCP state and tools are updated even when starting fails or is
    cancelled, so neither refers to closed connections.

    The caller must ensure no agent run is using the runtime's tools.

    Args:
        runtime (WispRuntime): Runtime whose MCP connections are replaced.
        servers (tuple[McpServerConfig, ...]): Every configured server, including
            disabled ones.

    Raises:
        RuntimeError: If the MCP runtime could not start or the previous one could
            not close cleanly. The runtime is then left without MCP tools, and its
            enabled servers are listed in ``runtime.failed_mcp_servers``.
    """

    previous = runtime.mcp_runtime
    started: McpRuntime | None = None
    enabled_servers = tuple(server for server in servers if server.enabled)
    tools_before_start = frozenset(runtime.tools.names())
    try:
        if previous is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await previous.aclose()
                finally:
                    runtime.tools.unregister(
                        name
                        for server_name in previous.server_names
                        for name in previous.tool_names_for(server_name)
                    )
                    runtime.replace_mcp_state(
                        None,
                        disabled_mcp_servers=(),
                        failed_mcp_servers=(),
                        unavailable_tool_prefixes=_unavailable_tool_prefixes(servers, None),
                    )
                    tools_before_start = frozenset(runtime.tools.names())
        if enabled_servers:
            from wisp.mcp.runtime import McpRuntime

            started = await McpRuntime.start(
                enabled_servers,
                api=runtime.api,
                existing_tool_names=runtime.tools.names(),
            )
    finally:
        if started is None:
            # A start that failed or was cancelled after registering a catalog closes
            # its clients but leaves their tools behind; they would shadow the server
            # as a name collision on every later restart.
            runtime.tools.unregister(
                name for name in runtime.tools.names() if name not in tools_before_start
            )
        runtime.replace_mcp_state(
            started,
            disabled_mcp_servers=tuple(server.name for server in servers if not server.enabled),
            failed_mcp_servers=(
                tuple(server.name for server in enabled_servers) if started is None else ()
            ),
            unavailable_tool_prefixes=_unavailable_tool_prefixes(servers, started),
        )


def _unavailable_tool_prefixes(
    mcp_servers: tuple[McpServerConfig, ...], mcp_runtime: McpRuntime | None
) -> tuple[str, ...]:
    """Name the MCP tool prefixes that are withheld this run rather than unknown.

    Servers that are disabled, failed to start, or are not running register no
    tools, but selecting one of their tools with ``--allow-tool`` must not abort
    startup or a reconnect.
    """

    return tuple(
        f"mcp__{server.name}__"
        for server in mcp_servers
        if mcp_runtime is None or not mcp_runtime.tool_names_for(server.name)
    )


async def activate_builtin_extensions(
    api: ExtensionAPI,
    *,
    auth_store: JsonAuthStore | None = None,
    retry_policy: RetryPolicy | None = None,
    process_supervisor: ProcessSupervisor | None = None,
    openai_compatible: OpenAICompatibleSettings | None = None,
    anthropic_cache_ttl: AnthropicCacheTtl = "5m",
) -> None:
    """Activate extensions that ship with Wisp."""

    supervisor = process_supervisor if process_supervisor is not None else api.process_supervisor
    builtin.activate(
        api,
        auth_store=auth_store,
        retry_policy=retry_policy,
        process_supervisor=supervisor,
        openai_compatible=openai_compatible,
        anthropic_cache_ttl=anthropic_cache_ttl,
    )


async def activate_extensions(api: ExtensionAPI, extensions: Sequence[ExtensionFactory]) -> None:
    """Activate extension factories in order."""

    for extension in extensions:
        result = extension(api)
        if isawaitable(result):
            await result
