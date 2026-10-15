"""RPC handlers that change MCP servers and restart them without restarting Wisp.

Each command first changes the user settings file (``reconnect_mcp`` changes
nothing), then reloads every configured server from it, restarts the runtime's MCP
connections, and gives the session a tool registry selected the same way as at
startup. The reply is the resulting ``rpc.mcp`` status. The commands run as
sequential RPC commands, so they wait for an active prompt to finish instead of
changing its tools mid-run. Once started they cannot be cancelled: stopping after
the settings changed would leave the running servers out of step with the file, and
MCP startup is already bounded by its own timeout.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.coding.session import CodingSession
from wisp.config.mcp_servers import (
    McpSettingsError,
    add_user_mcp_servers,
    load_user_mcp_servers,
    remove_user_mcp_server,
    set_user_mcp_server_enabled,
    validated_mcp_server,
)
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcMcpServerSnapshot,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
)
from wisp.mcp.config import McpServerConfig
from wisp.rpc.commands import (
    AddMcpServerCommand,
    ReconnectMcpCommand,
    RemoveMcpServerCommand,
    SetMcpServerEnabledCommand,
)
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import restart_mcp_servers
from wisp.runtime.registry import ToolRegistry
from wisp.tools.selection import select_tools

type McpServerCommand = (
    AddMcpServerCommand | RemoveMcpServerCommand | SetMcpServerEnabledCommand | ReconnectMcpCommand
)
type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]

# MCP tool names all start with this; see `wisp.mcp.tool`.
_MCP_TOOL_PREFIX = "mcp__"


@dataclass(frozen=True)
class RpcToolSelection:
    """The startup tool-exposure options, re-applied after MCP servers restart."""

    all_tools: bool
    allow_read_tools: bool
    allowed_tools: tuple[str, ...]

    def select(self, runtime: WispRuntime) -> ToolRegistry:
        """Select tools from the runtime's current registry.

        An explicitly allowed MCP tool that no longer exists is skipped rather than
        rejected: its server was removed, disabled, or failed after startup, and
        built-in names were already checked when Wisp started.

        Args:
            runtime (WispRuntime): Runtime whose registered tools are selected.

        Returns:
            ToolRegistry: The tools a session may expose.
        """

        return select_tools(
            runtime.tools,
            all_tools=self.all_tools,
            allow_read_tools=self.allow_read_tools,
            allowed_tools=self.allowed_tools,
            ignored_unknown_prefixes=(_MCP_TOOL_PREFIX,),
        )


class RpcMcpServers:
    """Change and restart one host's MCP servers between agent runs."""

    def __init__(
        self,
        *,
        runtime: WispRuntime,
        agent: CodingSession,
        tool_selection: RpcToolSelection,
        revoke_session_tool_grants: Callable[[str], None],
    ) -> None:
        self._runtime = runtime
        self._agent = agent
        self._tool_selection = tool_selection
        self._revoke_session_tool_grants = revoke_session_tool_grants

    def start(
        self,
        command: McpServerCommand,
        *,
        running_command: _RpcRunningCommand | None,
        session_state: _RpcSessionState,
        task_group: TaskGroup,
        send: MemoryObjectSendStream[_RpcControlEvent],
        write_event: RpcEventWriter,
        running_command_factory: RunningCommandFactory = _RpcRunningCommand,
        command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
    ) -> _RpcRunningCommand | None:
        """Start one MCP command as the host's running command.

        Returns:
            _RpcRunningCommand | None: The new running command, or the existing one
                unchanged when the command is rejected because another is active.
        """

        lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
        if running_command is not None:
            lifecycle.fail("Change MCP servers after the current operation finishes")
            return running_command
        cancel_scope = anyio.CancelScope()
        task_group.start_soon(
            self._run,
            command,
            lifecycle.command_id,
            session_state.history,
            session_state.entry_count,
            cancel_scope,
            send.clone(),
            write_event,
            command_completed_factory,
        )
        return running_command_factory(
            command_id=lifecycle.command_id,
            command_type=command.type,
            cancel_scope=cancel_scope,
        )

    async def _run(
        self,
        command: McpServerCommand,
        command_id: str,
        history: tuple[Message, ...],
        entry_count: int,
        cancel_scope: anyio.CancelScope,
        send: MemoryObjectSendStream[_RpcControlEvent],
        write_event: RpcEventWriter,
        command_completed_factory: CommandCompletedFactory,
    ) -> None:
        error: str | None = None
        # `cancel_scope` belongs to the running-command contract; shielding inside
        # it makes a cancel request a no-op (see the module docstring).
        try:
            with cancel_scope, anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(_change_settings, command)
                    servers = await anyio.to_thread.run_sync(load_user_mcp_servers)
                except McpSettingsError as exc:
                    error = str(exc)
                else:
                    error = await self._restart(servers)
                    write_event(
                        RpcMcpStatusReported(
                            command_id=command_id, status=rpc_mcp_status_snapshot(self._runtime)
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - command failures must not stop RPC
            error = f"MCP servers could not be changed: {type(exc).__name__}"
        finally:
            async with send:
                if error is not None:
                    write_event(ErrorEvent(message=error))
                write_event(
                    RpcCommandFinished(
                        command_id=command_id,
                        command_type=command.type,
                        ok=error is None,
                        error=error,
                    )
                )
                await send.send(
                    command_completed_factory(
                        command_id=command_id,
                        command_type=command.type,
                        ok=error is None,
                        history=history,
                        entry_count=entry_count,
                    )
                )

    async def _restart(self, servers: tuple[McpServerConfig, ...]) -> str | None:
        """Restart MCP servers and hand the session the resulting tools.

        Per-tool session grants for MCP tools are revoked first: a server may now
        run a different command under the same name, so "allow this tool for the
        session" answers given to the old one must not carry over. Grants that are
        not tied to a tool, such as the permission mode, are kept.

        Returns:
            str | None: A sanitized error when the MCP runtime failed as a whole;
                per-server problems are reported in the status instead.
        """

        self._revoke_session_tool_grants(_MCP_TOOL_PREFIX)
        error: str | None = None
        try:
            await restart_mcp_servers(self._runtime, servers)
        except RuntimeError:
            error = "MCP servers could not be restarted; their tools are unavailable"
        finally:
            # The old registry refers to closed connections, so the session must
            # switch even when starting failed.
            self._agent.replace_tool_registry(self._tool_selection.select(self._runtime))
        return error


def _change_settings(command: McpServerCommand) -> None:
    """Apply one command's change to the user settings file.

    Raises:
        McpSettingsError: If the change is invalid or the file cannot be updated.
    """

    if isinstance(command, AddMcpServerCommand):
        entry: dict[str, object] = {
            "command": command.command,
            "args": command.args,
            "url": command.url,
            "env": command.env,
            "env_from": command.env_from,
        }
        server = validated_mcp_server(
            command.name, {key: value for key, value in entry.items() if value}
        )
        add_user_mcp_servers((server,), replace=command.replace)
    elif isinstance(command, RemoveMcpServerCommand):
        remove_user_mcp_server(command.name)
    elif isinstance(command, SetMcpServerEnabledCommand):
        # Unlike `wisp mcp enable`, this cannot check the entry against what the
        # user was shown: RPC has no command that shows a server's launch settings
        # yet. HY-258 adds one, and the check, before the TUI offers enabling.
        set_user_mcp_server_enabled(command.name, command.enabled)


def rpc_mcp_status_snapshot(runtime: WispRuntime) -> RpcMcpStatusSnapshot:
    """Return sanitized status for every configured MCP server, sorted by name.

    Args:
        runtime (WispRuntime): Runtime whose MCP connections are reported.

    Returns:
        RpcMcpStatusSnapshot: Status without commands, URLs, or environment values.
    """

    snapshots = [
        RpcMcpServerSnapshot(name=name, status="disabled") for name in runtime.disabled_mcp_servers
    ]
    snapshots.extend(
        RpcMcpServerSnapshot(
            name=name,
            status="unavailable",
            error=f"MCP server {name} is unavailable: the MCP runtime could not start",
        )
        for name in runtime.failed_mcp_servers
    )
    mcp_runtime = runtime.mcp_runtime
    if mcp_runtime is not None:
        diagnostics = {item.server_name: item for item in mcp_runtime.diagnostics}
        for name in mcp_runtime.server_names:
            diagnostic = diagnostics.get(name)
            status: Literal["connected", "disconnected", "unavailable"]
            if diagnostic is not None:
                status = "unavailable"
            elif mcp_runtime.is_connected(name):
                status = "connected"
            else:
                status = "disconnected"
            snapshots.append(
                RpcMcpServerSnapshot(
                    name=name,
                    status=status,
                    tool_names=mcp_runtime.tool_names_for(name),
                    error=diagnostic.message if diagnostic is not None else None,
                )
            )
    return RpcMcpStatusSnapshot(servers=tuple(sorted(snapshots, key=lambda item: item.name)))
