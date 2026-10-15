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

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.coding.session import CodingSession
from wisp.config.mcp_servers import (
    McpServerExistsError,
    McpSettingsError,
    add_user_mcp_servers,
    load_user_mcp_servers,
    parse_mcp_snippet,
    remove_user_mcp_server,
    set_user_mcp_server_enabled,
    validated_mcp_server,
)
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcMcpHeaderFrom,
    RpcMcpServerLaunch,
    RpcMcpServerPreview,
    RpcMcpServerSnapshot,
    RpcMcpSnippetPreviewed,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
)
from wisp.mcp.config import McpServerConfig, header_template_variable
from wisp.rpc.commands import (
    AddMcpServerCommand,
    PreviewMcpSnippetCommand,
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
from wisp.tools.mcp_servers import (
    PROPOSE_MCP_SERVER,
    PROPOSE_MCP_SERVER_CHANGE,
    control_characters,
    proposed_change,
    proposed_server,
    secret_like_argument,
)
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
        # A confirmed proposal saved settings during a run; servers restart once
        # the run ends, because a run's tools cannot change while it uses them.
        self._restart_after_run = False

    def confirm_proposal(self, tool_name: str, arguments: Mapping[str, object]) -> str | None:
        """Apply a proposal the user confirmed, from the arguments they were shown.

        The settings change is saved now; servers restart when the current run
        ends (see :meth:`restart_after_run`).

        Args:
            tool_name (str): `propose_mcp_server` or `propose_mcp_server_change`.
            arguments (Mapping[str, object]): The confirmed tool call's arguments.

        Returns:
            str | None: Why the proposal could not be applied, or None once saved.
        """

        try:
            if tool_name == PROPOSE_MCP_SERVER:
                server = proposed_server(arguments)
                # Checked again here: the confirmation must never save a secret or
                # a command whose display could have been disguised.
                if control_characters(server):
                    return "The proposal contains control characters; it was not saved."
                if secret_like_argument(server) is not None:
                    return "The proposal seems to contain a secret value; it was not saved."
                add_user_mcp_servers((server,), replace=arguments.get("replace") is True)
            elif tool_name == PROPOSE_MCP_SERVER_CHANGE:
                name, action = proposed_change(arguments)
                if action == "remove":
                    remove_user_mcp_server(name)
                else:
                    set_user_mcp_server_enabled(name, False)
            else:
                return f"Unknown MCP proposal tool {tool_name}"
        except McpServerExistsError as exc:
            return f"{exc}. Propose it again with replace set to true to overwrite it."
        except McpSettingsError as exc:
            return str(exc)
        self._restart_after_run = True
        return None

    async def restart_after_run(self) -> str | None:
        """Restart servers if a confirmed proposal changed them during the run.

        Call only once the run has ended and before the next one starts.

        Returns:
            str | None: A sanitized error if the servers could not be restarted.
        """

        if not self._restart_after_run:
            return None
        if self._agent.busy:
            # The run did not release the session; keep the change pending so the
            # session never ends up with tools from closed connections.
            return "MCP servers restart after the next reply, once the session is idle."
        self._restart_after_run = False
        try:
            servers = await anyio.to_thread.run_sync(load_user_mcp_servers)
        except McpSettingsError as exc:
            return f"MCP servers were not restarted: {exc}"
        error = await self._restart(servers)
        if error is not None:
            return error
        # No status request is waiting for this restart, so a server that failed
        # to start would otherwise go unnoticed after the user confirmed it.
        failures = [
            server.error
            for server in rpc_mcp_status_snapshot(self._runtime).servers
            if server.status == "unavailable" and server.error is not None
        ]
        return "; ".join(failures) or None

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


def handle_rpc_mcp_snippet_preview_command(
    command: PreviewMcpSnippetCommand,
    *,
    write_event: RpcEventWriter,
) -> None:
    """Report the servers a pasted snippet describes, without saving them.

    Errors name the problem without echoing snippet values, and the reply names
    environment variables only.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    try:
        servers = parse_mcp_snippet(command.snippet)
    except McpSettingsError as exc:
        lifecycle.fail(str(exc))
        return
    except Exception:  # noqa: BLE001 - a malformed snippet must not stop the host
        lifecycle.fail("the snippet could not be parsed")
        return
    write_event(
        RpcMcpSnippetPreviewed(
            command_id=lifecycle.command_id,
            servers=tuple(
                RpcMcpServerPreview(name=server.name, launch=rpc_mcp_server_launch(server))
                for server in servers
            ),
        )
    )
    lifecycle.finish()


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
            "headers": command.headers,
            "headers_from": command.headers_from,
        }
        server = validated_mcp_server(
            command.name, {key: value for key, value in entry.items() if value}
        )
        add_user_mcp_servers((server,), replace=command.replace)
    elif isinstance(command, RemoveMcpServerCommand):
        remove_user_mcp_server(command.name)
    elif isinstance(command, SetMcpServerEnabledCommand):
        expected = command.expected
        set_user_mcp_server_enabled(
            command.name,
            command.enabled,
            confirmed=(
                None if expected is None else lambda saved: rpc_mcp_server_launch(saved) == expected
            ),
        )


def rpc_mcp_server_launch(server: McpServerConfig) -> RpcMcpServerLaunch:
    """Return what ``server`` runs or connects to, naming environment variables only.

    Literal header values are reduced to their header names; a forwarded header
    names the variable whose value it sends.

    Args:
        server (McpServerConfig): Configured server.

    Returns:
        RpcMcpServerLaunch: The settings a user must see to confirm the server.
    """

    return RpcMcpServerLaunch(
        command=server.command,
        args=server.args,
        url=server.url,
        env_names=tuple(name for name, _value in server.env),
        env_from=server.env_from,
        header_names=tuple(name for name, _value in server.headers),
        headers_from=tuple(
            RpcMcpHeaderFrom(header=header, variable=variable, scheme=scheme)
            for header, (scheme, variable) in (
                (header, header_template_variable(template))
                for header, template in server.headers_from
            )
        ),
    )


def rpc_mcp_status_snapshot(runtime: WispRuntime) -> RpcMcpStatusSnapshot:
    """Return sanitized status for every configured MCP server, sorted by name.

    Args:
        runtime (WispRuntime): Runtime whose MCP connections are reported.

    Returns:
        RpcMcpStatusSnapshot: Status and launch settings, without environment values.
    """

    configs = {server.name: server for server in runtime.mcp_server_configs}
    mcp_runtime = runtime.mcp_runtime
    running = mcp_runtime.server_names if mcp_runtime is not None else ()
    diagnostics = (
        {item.server_name: item for item in mcp_runtime.diagnostics}
        if mcp_runtime is not None
        else {}
    )
    names = sorted({*configs, *running, *runtime.disabled_mcp_servers, *runtime.failed_mcp_servers})
    snapshots: list[RpcMcpServerSnapshot] = []
    for name in names:
        config = configs.get(name)
        launch = rpc_mcp_server_launch(config) if config is not None else None
        diagnostic = diagnostics.get(name)
        if name in runtime.disabled_mcp_servers:
            snapshot = RpcMcpServerSnapshot(name=name, status="disabled", launch=launch)
        elif name in runtime.failed_mcp_servers:
            snapshot = RpcMcpServerSnapshot(
                name=name,
                status="unavailable",
                error=f"MCP server {name} is unavailable: the MCP runtime could not start",
                launch=launch,
            )
        elif mcp_runtime is None or name not in running:
            continue
        elif diagnostic is not None:
            snapshot = RpcMcpServerSnapshot(
                name=name, status="unavailable", error=diagnostic.message, launch=launch
            )
        else:
            snapshot = RpcMcpServerSnapshot(
                name=name,
                status="connected" if mcp_runtime.is_connected(name) else "disconnected",
                tool_names=mcp_runtime.tool_names_for(name),
                launch=launch,
            )
        snapshots.append(snapshot)
    return RpcMcpStatusSnapshot(servers=tuple(snapshots))
