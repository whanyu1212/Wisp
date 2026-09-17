"""Synchronous inspection and catalog projection for the RPC frontend."""

from __future__ import annotations

from typing import Literal

from wisp.auth.connections import connection_catalog
from wisp.auth.storage import AuthStorageError
from wisp.coding.session import CodingSession
from wisp.events import (
    CodingSessionState,
    RpcCommandArgument,
    RpcCommandDescriptor,
    RpcCommandsReported,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcConnectionMethodSnapshot,
    RpcConnectionProviderSnapshot,
    RpcMcpServerSnapshot,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
    RpcModelCatalogEntry,
    RpcModelCatalogReported,
    RpcModelCatalogSnapshot,
    RpcModelProviderSnapshot,
    RpcModelSelectionSnapshot,
    RpcSkillCatalogEntry,
    RpcSkillCatalogSnapshot,
    RpcSkillDiagnostic,
    RpcSkillsReported,
    RpcStateReported,
    RpcStateSnapshot,
)
from wisp.providers.base import Provider
from wisp.rpc.commands import (
    ClearQueueCommand,
    FollowUpCommand,
    GetCommandsCommand,
    GetConnectionCatalogCommand,
    GetMcpStatusCommand,
    GetModelCatalogCommand,
    GetSkillsCommand,
    GetStateCommand,
    ParsedRpcCommand,
    PopQueueCommand,
    SetQueueModeCommand,
    SteerCommand,
)
from wisp.rpc.coordinator import _RpcRunningCommand
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime
from wisp.runtime.commands import CommandDescriptor
from wisp.sessions.jsonl import JsonlSession


def _sanitized_auth_error(exc: BaseException) -> str:
    if isinstance(exc, AuthStorageError):
        return str(exc)
    return "Provider connection failed"


def _rpc_command_descriptor(descriptor: CommandDescriptor) -> RpcCommandDescriptor:
    """Convert a runtime command descriptor into its RPC wire shape."""

    return RpcCommandDescriptor(
        name=descriptor.name,
        title=descriptor.title,
        description=descriptor.description,
        category=str(descriptor.category),
        aliases=descriptor.aliases,
        slash_command=descriptor.slash_command,
        slash_aliases=descriptor.slash_aliases,
        arguments=tuple(
            RpcCommandArgument(
                name=argument.name,
                description=argument.description,
                required=argument.required,
            )
            for argument in descriptor.arguments
        ),
        accepts_arguments=descriptor.accepts_arguments,
        prefill_on_partial_enter=descriptor.prefill_on_partial_enter,
        order=descriptor.order,
    )


def _project_buffered_prompt_queue_commands(
    state: CodingSessionState,
    commands: tuple[ParsedRpcCommand, ...],
) -> CodingSessionState:
    """Project prompt-startup queue commands without mutating coordinator/session state."""

    if not commands:
        return state
    steering_mode = state.steering_mode
    follow_up_mode = state.follow_up_mode
    steering_count = state.pending_steering_count
    follow_up_count = state.pending_follow_up_count
    for parsed in commands:
        command = parsed.known
        if isinstance(command, SteerCommand):
            steering_count += 1
        elif isinstance(command, FollowUpCommand):
            follow_up_count += 1
        elif isinstance(command, SetQueueModeCommand):
            if command.kind == "steering":
                steering_mode = command.mode
            else:
                follow_up_mode = command.mode
        elif isinstance(command, PopQueueCommand):
            if command.kind == "steering":
                steering_count = max(0, steering_count - 1)
            else:
                follow_up_count = max(0, follow_up_count - 1)
        elif isinstance(command, ClearQueueCommand):
            if command.kind is None:
                steering_count = 0
                follow_up_count = 0
            elif command.kind == "steering":
                steering_count = 0
            else:
                follow_up_count = 0

    return state.model_copy(
        update={
            "steering_mode": steering_mode,
            "follow_up_mode": follow_up_mode,
            "pending_steering_count": steering_count,
            "pending_follow_up_count": follow_up_count,
        }
    )


def rpc_model_catalog_snapshot(
    *,
    runtime: WispRuntime,
    provider: Provider,
    model: str | None,
    effort: str | None,
) -> RpcModelCatalogSnapshot:
    """Project the effective catalog without constructing deferred providers."""

    providers = tuple(
        RpcModelProviderSnapshot(
            name=entry.name,
            display_name=entry.display_name,
            default_model=entry.default_model,
            available=runtime.providers.is_registered(entry.name),
            models=tuple(
                RpcModelCatalogEntry(
                    id=model_id,
                    lifecycle=entry.model_lifecycle.get(model_id),
                    effort_levels=entry.effort_levels.get(model_id, ()),
                )
                for model_id in entry.models
            ),
        )
        for entry in runtime.models.providers()
    )
    effective_model = model or provider.default_model
    return RpcModelCatalogSnapshot(
        selection=RpcModelSelectionSnapshot(
            provider=provider.name,
            model=model,
            effective_model=effective_model,
            catalog_model=(
                runtime.models.canonical_model(provider.name, effective_model)
                if effective_model is not None
                else None
            ),
            effort=effort,
        ),
        providers=providers,
    )


def rpc_connection_catalog_snapshot(runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
    """Project the sanitized connection catalog without revealing secrets."""

    store = runtime.auth_store
    if store is None:
        raise AuthStorageError("RPC connection catalog requires an auth store")
    configured_provider = runtime.openai_compatible_provider
    openai_compatible_provider = (
        configured_provider
        if (
            configured_provider is not None
            and runtime.openai_compatible_requires_api_key
            and runtime.providers.is_registered(configured_provider)
        )
        else None
    )
    catalog = connection_catalog(
        store,
        openai_compatible_provider=openai_compatible_provider,
    )
    return RpcConnectionCatalogSnapshot(
        providers=tuple(
            RpcConnectionProviderSnapshot(
                id=family.id,
                label=family.label,
                methods=tuple(
                    RpcConnectionMethodSnapshot(
                        provider=method.provider,
                        label=method.label,
                        kind=method.kind,
                        source=method.source,
                        environment_variable=method.environment_variable,
                        oauth_expires_at=method.oauth_expires_at,
                        has_stored_credential=method.has_stored_credential,
                    )
                    for method in family.methods
                ),
            )
            for family in catalog
        )
    )


def rpc_skill_catalog_snapshot(agent: CodingSession) -> RpcSkillCatalogSnapshot:
    """Project one session's current catalog into its bounded RPC shape."""

    catalog = agent.skill_catalog
    return RpcSkillCatalogSnapshot(
        entries=tuple(
            RpcSkillCatalogEntry(
                name=entry.name,
                description=entry.description,
                source=entry.source,
            )
            for entry in catalog.entries
        ),
        diagnostics=tuple(
            RpcSkillDiagnostic(
                code=diagnostic.code,
                severity=diagnostic.severity,
                message=diagnostic.message,
                source=diagnostic.source,
                path=diagnostic.path,
            )
            for diagnostic in catalog.diagnostics
        ),
        project_trusted=agent.trusted,
    )


def handle_rpc_state_command(
    command: GetStateCommand,
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    session_name: str | None,
    running_command: _RpcRunningCommand | None,
    pending_prompt_queue_commands: tuple[ParsedRpcCommand, ...] = (),
    write_event: RpcEventWriter,
) -> None:
    """Return one coherent in-memory state snapshot without becoming active."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)

    try:
        core_state = _project_buffered_prompt_queue_commands(
            agent.state_snapshot(session),
            pending_prompt_queue_commands,
        )
        state = RpcStateSnapshot(
            **core_state.model_dump(),
            session_id=session.session_id if session is not None else None,
            session_path=session.path if session is not None else None,
            session_name=session_name if session is not None else None,
            active_command_id=(running_command.command_id if running_command is not None else None),
            active_command_type=(
                running_command.command_type if running_command is not None else None
            ),
            cancel_requested=(
                running_command.cancel_scope.cancel_called if running_command is not None else False
            ),
        )
    except Exception as exc:
        lifecycle.fail(str(exc))
        return

    write_event(RpcStateReported(command_id=lifecycle.command_id, state=state))
    lifecycle.finish()


def handle_rpc_commands_command(
    command: GetCommandsCommand,
    *,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Return one coherent in-memory command registry snapshot without becoming active."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)

    try:
        commands = tuple(
            _rpc_command_descriptor(descriptor) for descriptor in runtime.commands.all()
        )
    except Exception as exc:
        lifecycle.fail(str(exc))
        return

    write_event(RpcCommandsReported(command_id=lifecycle.command_id, commands=commands))
    lifecycle.finish()


def handle_rpc_model_catalog_command(
    command: GetModelCatalogCommand,
    *,
    agent: CodingSession,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Return one coherent effective model catalog without becoming active."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    try:
        catalog = rpc_model_catalog_snapshot(
            runtime=runtime,
            provider=agent.provider,
            model=agent.model,
            effort=agent.effort,
        )
    except Exception as exc:
        lifecycle.fail(str(exc))
        return
    write_event(RpcModelCatalogReported(command_id=lifecycle.command_id, catalog=catalog))
    lifecycle.finish()


def handle_rpc_connection_catalog_command(
    command: GetConnectionCatalogCommand,
    *,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Return one sanitized connection catalog without becoming active."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    try:
        catalog = rpc_connection_catalog_snapshot(runtime)
    except Exception as exc:
        lifecycle.fail(_sanitized_auth_error(exc))
        return
    write_event(RpcConnectionCatalogReported(command_id=lifecycle.command_id, catalog=catalog))
    lifecycle.finish()


def handle_rpc_skills_command(
    command: GetSkillsCommand,
    *,
    agent: CodingSession,
    write_event: RpcEventWriter,
) -> None:
    """Return the active immutable skill catalog without performing discovery."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    write_event(
        RpcSkillsReported(
            command_id=lifecycle.command_id, catalog=rpc_skill_catalog_snapshot(agent)
        )
    )
    lifecycle.finish()


def handle_rpc_mcp_status_command(
    command: GetMcpStatusCommand,
    *,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Return sanitized startup status without reconnecting MCP servers."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id

    mcp_runtime = runtime.mcp_runtime
    servers: tuple[RpcMcpServerSnapshot, ...] = ()
    if mcp_runtime is not None:
        diagnostics = {item.server_name: item for item in mcp_runtime.diagnostics}
        snapshots: list[RpcMcpServerSnapshot] = []
        for name in mcp_runtime.server_names:
            registered_tools = mcp_runtime.tool_names_for(name)
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
                    tool_names=registered_tools,
                    error=diagnostic.message if diagnostic is not None else None,
                )
            )
        servers = tuple(snapshots)

    write_event(
        RpcMcpStatusReported(command_id=command_id, status=RpcMcpStatusSnapshot(servers=servers))
    )
    lifecycle.finish()
