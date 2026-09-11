"""Transport-independent command execution for the RPC frontend."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import assert_never

from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.coding import CodingSession
from wisp.events import WispEvent
from wisp.rpc.commands import (
    ApprovalCommand,
    BeginDeviceCodeCommand,
    CancelCommand,
    ClearQueueCommand,
    CloneSessionCommand,
    CompactCommand,
    ConfigureCommand,
    DisconnectProviderCommand,
    FollowUpCommand,
    ForkSessionCommand,
    GetCommandsCommand,
    GetConnectionCatalogCommand,
    GetMcpStatusCommand,
    GetMessagesCommand,
    GetModelCatalogCommand,
    GetProjectFilesCommand,
    GetQueueStateCommand,
    GetSessionsCommand,
    GetSessionStatsCommand,
    GetSessionTreeCommand,
    GetSkillsCommand,
    GetStateCommand,
    InitCommand,
    NavigateSessionTreeCommand,
    NewSessionCommand,
    ParsedRpcCommand,
    PopQueueCommand,
    PromptCommand,
    SelectSessionCommand,
    SetQueueModeCommand,
    SetSessionNameCommand,
    ShutdownCommand,
    SteerCommand,
    StoreApiKeyCommand,
    TrustCommand,
    UnrevertSessionTreeCommand,
)
from wisp.rpc.project_files import ProjectFilesPublisher, RpcProjectFiles
from wisp.runtime.api import WispRuntime
from wisp.sessions.jsonl import JsonlSessionStore

from .configuration import _RpcConfigureOverrides
from .configure import handle_rpc_configure_command
from .connections import (
    handle_rpc_disconnect_provider_command,
    handle_rpc_store_api_key_command,
    start_rpc_device_code_command,
)
from .control import (
    RpcApprovalResolver,
    RpcTrustResolver,
    _RpcControlCommand,
    handle_rpc_control_command,
)
from .coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcDispatchResult,
    _RpcRunningCommand,
    _RpcSessionState,
)
from .inspection import (
    handle_rpc_commands_command,
    handle_rpc_connection_catalog_command,
    handle_rpc_mcp_status_command,
    handle_rpc_model_catalog_command,
    handle_rpc_skills_command,
    handle_rpc_state_command,
)
from .lifecycle import RpcCommandLifecycle, RpcEventWriter
from .session_mutation import (
    start_rpc_clone_session_command,
    start_rpc_fork_session_command,
    start_rpc_navigate_session_tree_command,
    start_rpc_select_session_command,
    start_rpc_set_session_name_command,
    start_rpc_unrevert_session_tree_command,
)
from .session_queue import _RpcQueueCommand, handle_rpc_queue_command
from .session_read import (
    start_rpc_messages_command,
    start_rpc_session_tree_command,
    start_rpc_sessions_command,
)
from .session_run import (
    handle_rpc_new_session_command,
    rpc_has_durable_completion,
    rpc_session_entry_count,
    rpc_session_run_start,
    run_rpc_compact_command,
    run_rpc_prompt_command,
    run_rpc_session_stats_command,
    start_rpc_compact_command,
    start_rpc_init_command,
    start_rpc_prompt_command,
    start_rpc_session_stats_command,
    updated_rpc_history,
)
from .session_state import rpc_session_state, updated_rpc_session_state

type RpcEventRenderer = Callable[[AsyncIterator[WispEvent]], Awaitable[None]]
type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]


class RpcCommandExecutor:
    """Validate, launch, and report RPC commands independently from stdin."""

    def __init__(
        self,
        *,
        agent: CodingSession,
        runtime: WispRuntime,
        sessions: JsonlSessionStore,
        session_state: _RpcSessionState,
        task_group: TaskGroup,
        send: MemoryObjectSendStream[_RpcControlEvent],
        approval_policy: RpcApprovalResolver,
        trust_gate: RpcTrustResolver,
        configure_overrides: _RpcConfigureOverrides,
        coordinator: RpcCoordinator,
        write_event: RpcEventWriter,
        render_events: RpcEventRenderer,
        running_command_factory: RunningCommandFactory = _RpcRunningCommand,
        command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
        defer_until_after_flush: Callable[[Callable[[], None]], None] | None = None,
        project_files: RpcProjectFiles | None = None,
        publish_project_files: ProjectFilesPublisher | None = None,
    ) -> None:
        self.project_files = project_files
        self.publish_project_files = publish_project_files
        self.agent = agent
        self.runtime = runtime
        self.sessions = sessions
        self.session_state = session_state
        self.task_group = task_group
        self.send = send
        self.approval_policy = approval_policy
        self.trust_gate = trust_gate
        self.configure_overrides = configure_overrides
        self.coordinator = coordinator
        self.write_event = write_event
        self.render_events = render_events
        self.running_command_factory = running_command_factory
        self.command_completed_factory = command_completed_factory
        self.defer_until_after_flush = defer_until_after_flush

    async def dispatch_parsed(
        self,
        command: ParsedRpcCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        known = command.known
        if isinstance(known, ConfigureCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_configure(
                known,
                provided_fields=command.provided_fields,
                running_command=running_command,
            )
        if isinstance(known, GetMessagesCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_messages(
                known,
                provided_fields=command.provided_fields,
            )
        if isinstance(known, GetSessionsCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_sessions(known)
        if isinstance(known, GetSessionTreeCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_session_tree(known)
        if isinstance(known, SelectSessionCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_select_session(known)
        if isinstance(known, CloneSessionCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_clone_session(known)
        if isinstance(known, ForkSessionCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_fork_session(known)
        if isinstance(known, NavigateSessionTreeCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_navigate_session_tree(known)
        if isinstance(known, UnrevertSessionTreeCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_unrevert_session_tree(known)
        if isinstance(known, SetSessionNameCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_set_session_name(known)
        if isinstance(known, NewSessionCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_new_session(known, running_command)
        if isinstance(known, GetStateCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_state(known, running_command)
        if isinstance(known, GetCommandsCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_commands(known, running_command)
        if isinstance(known, GetModelCatalogCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_model_catalog(known, running_command)
        if isinstance(known, GetConnectionCatalogCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_connection_catalog(known, running_command)
        if isinstance(known, GetProjectFilesCommand):
            if self.project_files is None or self.publish_project_files is None:
                RpcCommandLifecycle.for_command(known, write_event=self.write_event).fail(
                    "Project file discovery unavailable"
                )
                return _RpcDispatchResult(running_command=None)
            return _RpcDispatchResult(
                running_command=self.project_files.start(
                    known,
                    task_group=self.task_group,
                    send=self.send,
                    write_event=self.write_event,
                    publish=self.publish_project_files,
                )
            )
        if isinstance(known, GetSkillsCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_skills(known, running_command)
        if isinstance(known, GetMcpStatusCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_mcp_status(known, running_command)
        if isinstance(known, StoreApiKeyCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_store_api_key(known, running_command)
        if isinstance(known, DisconnectProviderCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_disconnect_provider(known, running_command)
        if isinstance(known, BeginDeviceCodeCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_begin_device_code(known, running_command)
        if isinstance(
            known,
            (
                SteerCommand,
                FollowUpCommand,
                GetQueueStateCommand,
                SetQueueModeCommand,
                PopQueueCommand,
                ClearQueueCommand,
            ),
        ):
            self.coordinator.running_command = running_command
            return await self._dispatch_queue(known, running_command)
        if isinstance(known, (CancelCommand, ApprovalCommand, TrustCommand, ShutdownCommand)):
            self.coordinator.running_command = running_command
            return self._dispatch_control(known, running_command)
        if isinstance(known, PromptCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_prompt(known)
        if isinstance(known, InitCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_init(known)
        if isinstance(known, CompactCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_compact(known)
        if isinstance(known, GetSessionStatsCommand):
            self.coordinator.running_command = running_command
            return self._dispatch_session_stats(known)
        if known is not None:
            assert_never(known)
        self.coordinator.running_command = running_command
        self.reject_parsed(command, f"Unknown RPC command: {command.command_type}")
        return _RpcDispatchResult(running_command=running_command)

    def reject_parsed(self, command: ParsedRpcCommand, message: str) -> None:
        id_error = command.command_id_error
        lifecycle = RpcCommandLifecycle.start(
            command_id=command.command_id if id_error is None else None,
            command_type=command.command_type,
            write_event=self.write_event,
        )
        lifecycle.fail(id_error or message)

    def _dispatch_prompt(self, command: PromptCommand) -> _RpcDispatchResult:
        new_running_command, new_session = start_rpc_prompt_command(
            command,
            agent=self.agent,
            sessions=self.sessions,
            session_state=self.session_state,
            task_group=self.task_group,
            send=self.send,
            trust_gate=self.trust_gate,
            write_event=self.write_event,
            render_events=self.render_events,
            running_command_factory=self.running_command_factory,
            command_completed_factory=self.command_completed_factory,
        )
        return _RpcDispatchResult(
            running_command=new_running_command,
            selected_session=new_session,
        )

    def _dispatch_init(self, command: InitCommand) -> _RpcDispatchResult:
        new_running_command, new_session = start_rpc_init_command(
            command,
            agent=self.agent,
            sessions=self.sessions,
            session_state=self.session_state,
            task_group=self.task_group,
            send=self.send,
            trust_gate=self.trust_gate,
            write_event=self.write_event,
            render_events=self.render_events,
            running_command_factory=self.running_command_factory,
            command_completed_factory=self.command_completed_factory,
        )
        return _RpcDispatchResult(
            running_command=new_running_command,
            selected_session=new_session,
        )

    def _dispatch_compact(self, command: CompactCommand) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_compact_command(
                command,
                agent=self.agent,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                trust_gate=self.trust_gate,
                write_event=self.write_event,
                render_events=self.render_events,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_session_stats(self, command: GetSessionStatsCommand) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_session_stats_command(
                command,
                agent=self.agent,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_messages(
        self,
        command: GetMessagesCommand,
        *,
        provided_fields: frozenset[str],
    ) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_messages_command(
                command,
                provided_fields=provided_fields,
                sessions=self.sessions,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_sessions(self, command: GetSessionsCommand) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_sessions_command(
                command,
                sessions=self.sessions,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_new_session(
        self,
        command: NewSessionCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        reset_session = handle_rpc_new_session_command(
            command,
            running_command=running_command,
            write_event=self.write_event,
        )
        if reset_session:
            self.agent.reset_session_state()
        return _RpcDispatchResult(
            running_command=running_command,
            reset_session=reset_session,
        )

    def _dispatch_select_session(self, command: SelectSessionCommand) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_select_session_command(
                command,
                sessions=self.sessions,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_clone_session(self, command: CloneSessionCommand) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_clone_session_command(
                command,
                sessions=self.sessions,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_fork_session(self, command: ForkSessionCommand) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_fork_session_command(
                command,
                sessions=self.sessions,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_session_tree(
        self,
        command: GetSessionTreeCommand,
    ) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_session_tree_command(
                command,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_navigate_session_tree(
        self,
        command: NavigateSessionTreeCommand,
    ) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_navigate_session_tree_command(
                command,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_unrevert_session_tree(
        self,
        command: UnrevertSessionTreeCommand,
    ) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_unrevert_session_tree_command(
                command,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    def _dispatch_set_session_name(
        self,
        command: SetSessionNameCommand,
    ) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_set_session_name_command(
                command,
                sessions=self.sessions,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
        )

    async def _dispatch_queue(
        self,
        command: _RpcQueueCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        await handle_rpc_queue_command(
            command,
            agent=self.agent,
            session=self.session_state.session,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_state(
        self,
        command: GetStateCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_state_command(
            command,
            agent=self.agent,
            session=self.session_state.session,
            session_name=self.session_state.name,
            running_command=running_command,
            pending_prompt_queue_commands=tuple(self.coordinator.pending_prompt_queue_commands),
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_commands(
        self,
        command: GetCommandsCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_commands_command(
            command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_model_catalog(
        self,
        command: GetModelCatalogCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_model_catalog_command(
            command,
            agent=self.agent,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_connection_catalog(
        self,
        command: GetConnectionCatalogCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_connection_catalog_command(
            command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_store_api_key(
        self,
        command: StoreApiKeyCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_store_api_key_command(
            command,
            running_command=running_command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_disconnect_provider(
        self,
        command: DisconnectProviderCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_disconnect_provider_command(
            command,
            running_command=running_command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_begin_device_code(
        self,
        command: BeginDeviceCodeCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_device_code_command(
                command,
                running_command=running_command,
                runtime=self.runtime,
                session_state=self.session_state,
                task_group=self.task_group,
                send=self.send,
                write_event=self.write_event,
                running_command_factory=self.running_command_factory,
                command_completed_factory=self.command_completed_factory,
            )
            or running_command
        )

    def _dispatch_skills(
        self,
        command: GetSkillsCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_skills_command(
            command,
            agent=self.agent,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_mcp_status(
        self,
        command: GetMcpStatusCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_mcp_status_command(
            command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_configure(
        self,
        command: ConfigureCommand,
        *,
        provided_fields: frozenset[str],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        lifecycle = RpcCommandLifecycle.for_command(command, write_event=self.write_event)
        if running_command is not None:
            lifecycle.fail("Cannot configure while another RPC operation is active")
            return _RpcDispatchResult(running_command=running_command)
        handle_rpc_configure_command(
            command,
            command_id=lifecycle.command_id,
            provided_fields=provided_fields,
            agent=self.agent,
            runtime=self.runtime,
            configure_overrides=self.configure_overrides,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_control(
        self,
        command: _RpcControlCommand,
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        should_shutdown = handle_rpc_control_command(
            command,
            running_command=running_command,
            approval_policy=self.approval_policy,
            trust_gate=self.trust_gate,
            coordinator=self.coordinator,
            write_event=self.write_event,
            defer_until_after_flush=self.defer_until_after_flush,
        )
        return _RpcDispatchResult(
            running_command=running_command,
            should_shutdown=should_shutdown,
        )


__all__ = [
    "RpcCommandExecutor",
    "handle_rpc_new_session_command",
    "rpc_has_durable_completion",
    "rpc_session_entry_count",
    "rpc_session_run_start",
    "rpc_session_state",
    "run_rpc_compact_command",
    "run_rpc_prompt_command",
    "run_rpc_session_stats_command",
    "start_rpc_compact_command",
    "start_rpc_init_command",
    "start_rpc_prompt_command",
    "start_rpc_session_stats_command",
    "updated_rpc_history",
    "updated_rpc_session_state",
]
