"""Transport-independent command execution for the RPC frontend."""

from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Protocol, assert_never, cast

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.agent.prompt import resolve_project_context_root
from wisp.coding import CodingSession
from wisp.events import (
    AgentStarted,
    ErrorEvent,
    MessageCompleted,
    ModelProviderAutoSwitched,
    QueueItemsRemoved,
    RpcCommandFinished,
    RpcModelCatalogReported,
    RpcModelCatalogSnapshot,
    SessionStatsReported,
    ToolExecutionEnded,
    WispEvent,
)
from wisp.providers.base import Provider
from wisp.providers.catalog import AmbiguousModelError, UnknownModelError, startup_effort
from wisp.rpc.commands import (
    ApprovalCommand,
    ApprovalScope,
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
from wisp.runtime.api import WispRuntime
from wisp.runtime.registry import UnknownProviderError
from wisp.sessions.entries import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.sessions.replay import resolve_session_tree
from wisp.tools.context import ToolContext
from wisp.tools.file_ops import CreateOnlyWriteReceipt

from .configuration import _RpcConfigureOverrides
from .connections import (
    handle_rpc_disconnect_provider_command,
    handle_rpc_store_api_key_command,
    start_rpc_device_code_command,
)
from .coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcDispatchResult,
    _RpcPromptReady,
    _RpcRunningCommand,
    _RpcSessionState,
)
from .errors import RpcOutputAlreadyReportedError
from .inspection import (
    handle_rpc_commands_command,
    handle_rpc_connection_catalog_command,
    handle_rpc_mcp_status_command,
    handle_rpc_model_catalog_command,
    handle_rpc_skills_command,
    handle_rpc_state_command,
    rpc_model_catalog_snapshot,
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
from .session_read import (
    start_rpc_messages_command,
    start_rpc_session_tree_command,
    start_rpc_sessions_command,
)
from .session_state import updated_rpc_session_state

type _RpcControlCommand = CancelCommand | ApprovalCommand | TrustCommand | ShutdownCommand

type _RpcQueueCommand = (
    SteerCommand
    | FollowUpCommand
    | GetQueueStateCommand
    | SetQueueModeCommand
    | PopQueueCommand
    | ClearQueueCommand
)

type RpcEventRenderer = Callable[[AsyncIterator[WispEvent]], Awaitable[None]]
type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]

_PROJECT_INIT_TOOL_NAMES = frozenset({"read", "grep", "find", "ls", "write"})


async def _run_abandonable_session_read[T](func: Callable[..., T], *args: object) -> T:
    return await anyio.to_thread.run_sync(func, *args, abandon_on_cancel=True)


class RpcApprovalResolver(Protocol):
    def has_pending_approval(self, *, call_id: str) -> bool: ...

    def resolve_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: str | None = None,
        scope: ApprovalScope = "once",
    ) -> bool: ...


class RpcTrustResolver(Protocol):
    async def resolve(self) -> bool: ...

    def resolve_request(
        self,
        *,
        request_id: str,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        release: bool = True,
    ) -> bool: ...

    def release_request(self, *, request_id: str) -> None: ...


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
    ) -> None:
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
        try:
            instructions, target = _project_init_request(self.agent)
        except ValueError as exc:
            lifecycle = RpcCommandLifecycle.for_command(command, write_event=self.write_event)
            lifecycle.fail(str(exc))
            return _RpcDispatchResult(
                running_command=None,
                selected_session=self.session_state.session,
            )
        receipt = CreateOnlyWriteReceipt()
        completion = _ProjectInitCompletion(
            target,
            conflicting_paths=(target.with_name("AGENTS.MD"),),
            receipt=receipt,
        )
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
            tool_context_factory=partial(
                _project_init_tool_context,
                self.agent,
                target,
                receipt,
            ),
            operation_instructions=instructions,
            operation_tool_names=_PROJECT_INIT_TOOL_NAMES,
            event_observer=completion.observe,
            completion_error=completion.error,
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


def handle_rpc_new_session_command(
    command: NewSessionCommand,
    *,
    running_command: _RpcRunningCommand | None,
    write_event: RpcEventWriter,
) -> bool:
    """Synchronously reset the selected-session state."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    if running_command is not None:
        lifecycle.fail("Cannot start a new session while another RPC operation is active")
        return False
    lifecycle.finish()
    return True


def rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    return _RpcSessionState(
        session=session,
        history=session.read_context_messages(),
        entry_count=len(session.read_entries()),
        name=session.read_name(),
    )


def _project_init_request(agent: CodingSession) -> tuple[str, Path]:
    if agent.mode != "build":
        raise ValueError("Project initialization requires build mode. Run /build first.")

    project_root = (
        agent.project_context_root or resolve_project_context_root(agent.tool_context.cwd)
    ).resolve(strict=False)
    for filename in ("AGENTS.md", "AGENTS.MD"):
        candidate = project_root / filename
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"Could not inspect project guidance path: {candidate}") from exc
        raise ValueError(f"Project guidance already exists: {candidate}")

    if "write" not in {tool.name for tool in agent.tools}:
        raise ValueError("Project initialization requires the write tool.")

    target = project_root / "AGENTS.md"
    encoded_target = json.dumps(str(target), ensure_ascii=False)
    prompt = f"""Initialize this repository for future coding agents.

Inspect the repository using read-only tools before writing anything. Base the guidance on verified
README files, manifests, lockfiles, CI configuration, source layout, and existing contributor
instructions. Do not run project code or copy secrets.

Create concise, repository-specific guidance at the exact absolute path
{encoded_target}. Include:
- a short project overview and mission;
- verified setup, build, format, lint, type-check, and test commands;
- the important architecture and module boundaries;
- repository-specific development conventions and safety rules;
- a practical definition of done.

Avoid generic advice and do not invent commands. Modify no other file. Immediately before writing,
check that neither AGENTS.md nor AGENTS.MD exists at the project root. If either exists, stop
without changing it. Create the target with the write tool using overwrite=false so the operation
remains create-only if the filesystem changes during your inspection."""
    return prompt, target


def _project_init_tool_context(
    agent: CodingSession,
    target: Path,
    receipt: CreateOnlyWriteReceipt,
) -> ToolContext:
    return replace(
        agent.tool_context,
        cwd=target.parent,
        allowed_write_paths=(target,),
        conflicting_write_paths=(target.with_name("AGENTS.MD"),),
        require_create_only_writes=True,
        require_non_empty_writes=True,
        create_only_write_receipt=receipt,
    )


@dataclass(slots=True)
class _ProjectInitCompletion:
    target: Path
    conflicting_paths: tuple[Path, ...]
    receipt: CreateOnlyWriteReceipt
    matching_call_ids: set[str] = field(default_factory=set)
    created_file_id: tuple[int, int] | None = None

    def observe(self, event: WispEvent) -> None:
        if isinstance(event, MessageCompleted):
            for call in event.tool_calls:
                # The operation policy and ToolContext permit a successful write only
                # to ``target`` with create-only semantics, regardless of path spelling.
                if (
                    call.name == "write"
                    and call.parse_error is None
                    and call.arguments.get("overwrite") is False
                ):
                    self.matching_call_ids.add(call.call_id)
            return
        if (
            isinstance(event, ToolExecutionEnded)
            and event.call_id in self.matching_call_ids
            and event.name == "write"
            and not event.is_error
            and event.created
        ):
            if self.receipt.path == self.target:
                self.created_file_id = self.receipt.file_id

    def error(self) -> str | None:
        if self.created_file_id is None:
            return (
                "Project initialization completed without a successful create-only write to "
                f"{self.target}"
            )
        try:
            info = self.target.lstat()
        except FileNotFoundError:
            return f"Project initialization completed without creating {self.target}"
        except OSError as exc:
            return f"Could not inspect generated project guidance: {exc}"
        if not stat.S_ISREG(info.st_mode):
            return f"Project initialization did not create a regular file: {self.target}"
        if (info.st_dev, info.st_ino) != self.created_file_id:
            return f"Generated project guidance was replaced before completion: {self.target}"
        if info.st_size == 0:
            return f"Project initialization created an empty file: {self.target}"
        for conflict in self.conflicting_paths:
            try:
                conflict_info = conflict.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                return f"Could not inspect generated project guidance: {exc}"
            if (conflict_info.st_dev, conflict_info.st_ino) == self.created_file_id:
                continue
            return f"Conflicting project guidance appeared during initialization: {conflict}"
        return None


def start_rpc_prompt_command(
    command: PromptCommand | InitCommand,
    *,
    agent: CodingSession,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    tool_context: ToolContext | None = None,
    tool_context_factory: Callable[[], ToolContext] | None = None,
    operation_instructions: str | None = None,
    operation_tool_names: frozenset[str] | None = None,
    event_observer: Callable[[WispEvent], None] | None = None,
    completion_error: Callable[[], str | None] | None = None,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> tuple[_RpcRunningCommand, JsonlSession]:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_type = command.type
    command_id = lifecycle.command_id
    prompt = command.prompt if isinstance(command, PromptCommand) else "/init"

    selected_session = session_state.session or sessions.create()
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_prompt_command,
        agent,
        selected_session,
        session_state.history,
        session_state.entry_count,
        prompt,
        command_id,
        command_type,
        cancel_scope,
        send.clone(),
        trust_gate,
        write_event,
        render_events,
        command_completed_factory,
        tool_context,
        tool_context_factory,
        operation_instructions,
        operation_tool_names,
        event_observer,
        completion_error,
    )
    return (
        running_command_factory(
            command_id=command_id,
            command_type=command_type,
            cancel_scope=cancel_scope,
        ),
        selected_session,
    )


def start_rpc_compact_command(
    command: CompactCommand,
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    instructions = (
        command.instructions.strip() or None if command.instructions is not None else None
    )

    session = session_state.session
    if session is None or not session.path.is_file():
        lifecycle.fail("RPC compact command requires an existing persisted session")
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_compact_command,
        agent,
        session,
        session_state.history,
        session_state.entry_count,
        instructions,
        command_id,
        cancel_scope,
        send.clone(),
        trust_gate,
        write_event,
        render_events,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="compact",
        cancel_scope=cancel_scope,
    )


def start_rpc_session_stats_command(
    command: GetSessionStatsCommand,
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_session_stats_command,
        agent,
        session_state.session,
        session_state.entry_count,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_session_stats",
        cancel_scope=cancel_scope,
    )


async def run_rpc_session_stats_command(
    agent: CodingSession,
    session: JsonlSession | None,
    entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = entry_count
    try:
        with cancel_scope:
            stats = await agent.get_session_stats(session)
            if session is not None:
                refreshed_entry_count, refreshed_history = await _run_abandonable_session_read(
                    updated_rpc_session_state,
                    session,
                    (),
                    entry_count,
                )
            write_event(SessionStatsReported(command_id=command_id, stats=stats))
            ok = True
        if cancel_scope.cancel_called:
            error = "RPC get_session_stats command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_session_stats command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_session_stats",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_session_stats",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_prompt_command(
    agent: CodingSession,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    prompt: str,
    command_id: str,
    command_type: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
    tool_context: ToolContext | None = None,
    tool_context_factory: Callable[[], ToolContext] | None = None,
    operation_instructions: str | None = None,
    operation_tool_names: frozenset[str] | None = None,
    event_observer: Callable[[WispEvent], None] | None = None,
    completion_error: Callable[[], str | None] | None = None,
) -> None:
    error: str | None = None
    run_entry_start = entry_start
    run_active_leaf_id: str | None = None
    run_start_captured = False

    async def mark_prompt_ready() -> None:
        with anyio.CancelScope(shield=True):
            await send.send(_RpcPromptReady(command_id=command_id))

    async def track_run_start(events: AsyncIterator[WispEvent]) -> AsyncIterator[WispEvent]:
        nonlocal run_active_leaf_id, run_entry_start, run_start_captured
        async for event in events:
            if event_observer is not None:
                event_observer(event)
            if isinstance(event, AgentStarted):
                run_entry_start, run_active_leaf_id = await anyio.to_thread.run_sync(
                    rpc_session_run_start,
                    session,
                    entry_start,
                    abandon_on_cancel=True,
                )
                run_start_captured = True
                yield event
                continue
            yield event

    with cancel_scope:
        try:
            agent.trusted = await trust_gate.resolve()
            operation_context = (
                tool_context_factory() if tool_context_factory is not None else tool_context
            )
            agent_events = (
                agent.run(
                    prompt,
                    session=session,
                    history=committed_history,
                    operation_id=command_id,
                    operation_instructions=operation_instructions,
                    operation_tool_names=operation_tool_names,
                    operation_ready=mark_prompt_ready,
                )
                if operation_context is None
                else agent.run(
                    prompt,
                    session=session,
                    history=committed_history,
                    operation_id=command_id,
                    tool_context=operation_context,
                    operation_instructions=operation_instructions,
                    operation_tool_names=operation_tool_names,
                    operation_ready=mark_prompt_ready,
                )
            )
            await render_events(track_run_start(agent_events))
            if completion_error is not None:
                error = await anyio.to_thread.run_sync(completion_error)
        except RpcOutputAlreadyReportedError as exc:
            error = str(exc)
        except anyio.get_cancelled_exc_class():
            error = f"RPC command cancelled: {command_id}"
        except Exception as exc:  # noqa: BLE001 - command failures must not stop RPC
            error = str(exc)

    # Process-level BaseException values deliberately skip this terminal lifecycle and
    # continue unwinding the host. Ordinary failures always reach the coordinator below.
    cancelled = error is not None and error.startswith("RPC command cancelled:")
    if cancelled:
        crossed_completion_boundary = await _run_abandonable_session_read(
            rpc_has_durable_completion,
            session,
            run_entry_start,
            command_id,
        )
        if not crossed_completion_boundary and run_start_captured:
            rolled_back = await session.restore_active_leaf_for_operation(
                run_entry_start,
                run_active_leaf_id,
                operation_id=command_id,
            )
            if not rolled_back and session.path.is_file():
                entries = await _run_abandonable_session_read(session.read_entries)
                if any(entry.operation_id == command_id for entry in entries[run_entry_start:]):
                    error = (
                        f"RPC command cancelled: {command_id}; prompt entries were retained "
                        "because another writer appended to the session"
                    )
    try:
        entry_count, updated_history = await _run_abandonable_session_read(
            updated_rpc_session_state,
            session,
            committed_history,
            entry_start,
        )
    except Exception as exc:  # noqa: BLE001 - preserve a usable RPC coordinator
        entry_count = entry_start
        updated_history = committed_history
        if error is None:
            error = str(exc)
    async with send:
        if cancelled:
            assert error is not None
            write_event(ErrorEvent(message=error))
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type=command_type,
                ok=error is None,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type=command_type,
                ok=error is None,
                history=updated_history,
                entry_count=entry_count,
            )
        )


async def run_rpc_compact_command(
    agent: CodingSession,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    instructions: str | None,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    render_events: RpcEventRenderer,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    error: str | None = None
    error_rendered = False

    async def track_errors(events: AsyncIterator[WispEvent]) -> AsyncIterator[WispEvent]:
        nonlocal error_rendered
        async for event in events:
            if isinstance(event, ErrorEvent):
                error_rendered = True
            yield event

    try:
        with cancel_scope:
            try:
                agent.trusted = await trust_gate.resolve()
                await render_events(track_errors(agent.compact(session, instructions=instructions)))
            except RpcOutputAlreadyReportedError as exc:
                error = str(exc)
                error_rendered = True
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
            except Exception as exc:  # noqa: BLE001 - command failures must not stop RPC
                error = str(exc)
    finally:
        try:
            entry_count, updated_history = await _run_abandonable_session_read(
                updated_rpc_session_state,
                session,
                committed_history,
                entry_start,
            )
        except Exception as exc:  # noqa: BLE001 - preserve a usable RPC coordinator
            entry_count = entry_start
            updated_history = committed_history
            if error is None:
                error = str(exc)
        async with send:
            if error is not None and not error_rendered:
                write_event(ErrorEvent(message=error))
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="compact",
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                command_completed_factory(
                    command_id=command_id,
                    command_type="compact",
                    ok=error is None,
                    history=updated_history,
                    entry_count=entry_count,
                )
            )


def updated_rpc_history(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[Message, ...]:
    return updated_rpc_session_state(session, committed_history, entry_start)[1]


def rpc_session_entry_count(session: JsonlSession, fallback: int) -> int:
    if not session.path.is_file():
        return fallback
    return len(session.read_entries())


def rpc_session_run_start(
    session: JsonlSession,
    fallback: int,
) -> tuple[int, str | None]:
    """Snapshot the audit offset and active leaf before a prompt starts writing."""

    if not session.path.is_file():
        return fallback, None
    entries = session.read_entries()
    return len(entries), resolve_session_tree(entries).active_leaf_id


def rpc_has_durable_completion(
    session: JsonlSession,
    entry_start: int,
    operation_id: str,
) -> bool:
    if not session.path.is_file():
        return False
    for entry in session.read_entries()[entry_start:]:
        if entry.operation_id != operation_id:
            continue
        if not isinstance(entry, MessageSessionEntry):
            continue
        message = entry.message
        if message.role == "assistant" and message.finish_reason is not None:
            return True
        if message.role == "tool" and message.tool_call_id is not None:
            return True
    return False


async def handle_rpc_queue_command(
    command: _RpcQueueCommand,
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    write_event: RpcEventWriter,
) -> None:
    """Execute one ordered queue command through the shared session facade."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id

    removed: QueueItemsRemoved | None = None
    try:
        if isinstance(command, GetQueueStateCommand):
            state = agent.queue_state(session)
        elif isinstance(command, SteerCommand):
            state = await agent.steer(command.content)
        elif isinstance(command, FollowUpCommand):
            state = await agent.follow_up(command.content)
        elif isinstance(command, SetQueueModeCommand):
            kind = command.kind
            mode = command.mode
            state = agent.set_queue_mode(kind, mode)
        elif isinstance(command, PopQueueCommand):
            kind = command.kind
            popped, state = agent.pop_queue(kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="pop",
                kind=kind,
                steering=(popped.user_visible_content,)
                if popped is not None and kind == "steering"
                else (),
                follow_up=(popped.user_visible_content,)
                if popped is not None and kind == "follow_up"
                else (),
            )
        elif isinstance(command, ClearQueueCommand):
            clear_kind = command.kind
            cleared, state = agent.clear_queue(clear_kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="clear",
                kind=clear_kind,
                steering=tuple(message.user_visible_content for message in cleared.steering),
                follow_up=tuple(message.user_visible_content for message in cleared.follow_up),
            )
        else:  # pragma: no cover - dispatch owns the closed command set
            assert_never(command)
    except (RuntimeError, ValueError) as exc:
        lifecycle.fail(str(exc))
        return

    if removed is not None:
        write_event(removed)
    write_event(state)
    lifecycle.finish()


def handle_rpc_control_command(
    command: _RpcControlCommand,
    *,
    running_command: _RpcRunningCommand | None,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    trust_gate: RpcTrustResolver | None = None,
    coordinator: RpcCoordinator | None = None,
    defer_until_after_flush: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    command_type = command.type
    if isinstance(command, ShutdownCommand):
        lifecycle.finish()
        return True
    if isinstance(command, CancelCommand):
        handle_rpc_cancel_command(
            command,
            command_id=command_id,
            command_type=command_type,
            running_command=running_command,
            coordinator=coordinator,
            write_event=write_event,
            defer_cancellation=defer_until_after_flush,
        )
        return False
    if isinstance(command, ApprovalCommand):
        handle_rpc_approval_command(
            command,
            command_id=command_id,
            command_type=command_type,
            approval_policy=approval_policy,
            write_event=write_event,
            defer_resolution=defer_until_after_flush,
        )
        return False
    if isinstance(command, TrustCommand):
        if trust_gate is None:
            lifecycle.fail("RPC trust command requires an active trust gate")
            return False
        handle_rpc_trust_command(
            command,
            command_id=command_id,
            command_type=command_type,
            trust_gate=trust_gate,
            write_event=write_event,
            defer_resolution=defer_until_after_flush,
        )
        return False
    assert_never(command)


def handle_rpc_configure_command(
    command: ConfigureCommand,
    *,
    command_id: str,
    provided_fields: frozenset[str],
    agent: CodingSession,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type="configure",
        write_event=write_event,
    )
    provider = command.provider
    model = command.model
    effort = command.effort
    auto_compaction_enabled = command.auto_compaction_enabled
    mode = command.mode
    clear_effort = command.clear_effort
    has_provider = "provider" in provided_fields
    has_model = "model" in provided_fields
    has_effort = "effort" in provided_fields or clear_effort
    has_auto_compaction_enabled = "auto_compaction_enabled" in provided_fields
    has_mode = "mode" in provided_fields
    if has_mode and mode is None:
        lifecycle.fail("RPC configure command field mode must be 'build' or 'plan'")
        return
    if has_auto_compaction_enabled and auto_compaction_enabled is None:
        lifecycle.fail("RPC configure command field auto_compaction_enabled must be a boolean")
        return
    configuration = agent.configuration
    selected_provider = configuration.provider
    selected_model = configuration.model
    selected_effort = configuration.effort
    selected_auto_compaction_enabled = configuration.auto_compaction_enabled
    auto_switched_provider: str | None = None
    if auto_compaction_enabled is not None:
        selected_auto_compaction_enabled = auto_compaction_enabled
    if provider is not None:
        try:
            selected_provider = runtime.providers.get(provider)
        except UnknownProviderError as exc:
            lifecycle.fail(str(exc))
            return
        if not has_model:
            selected_model = None
        if not has_effort:
            selected_effort = None
    if has_model and provider is None and model is not None:
        try:
            selected_provider = auto_switch_provider_for_model(
                model,
                current_provider=selected_provider,
                runtime=runtime,
            )
            if selected_provider.name != configuration.provider.name:
                auto_switched_provider = selected_provider.name
        except AmbiguousModelError as exc:
            lifecycle.fail(f"{exc}; specify provider explicitly")
            return
        except UnknownProviderError as exc:
            lifecycle.fail(
                f"Model {model!r} resolves to provider {exc.name!r}, which is not available"
            )
            return
        if not has_effort:
            selected_effort = None
    if has_model:
        selected_model = model
    if has_effort:
        selected_effort = None if clear_effort else effort
    selected_effort = startup_effort(
        runtime.models,
        provider_name=selected_provider.name,
        model=selected_model,
        default_model=selected_provider.default_model,
        effort=selected_effort,
    )
    selection_changed = (
        has_provider or has_model or has_effort or (selected_effort != configuration.effort)
    )
    model_catalog: RpcModelCatalogSnapshot | None = None
    model_catalog_error: str | None = None
    if selection_changed:
        try:
            model_catalog = rpc_model_catalog_snapshot(
                runtime=runtime,
                provider=selected_provider,
                model=selected_model,
                effort=selected_effort,
            )
        except Exception as exc:
            # Catalog bounds protect RPC consumers, not provider configuration.
            model_catalog_error = str(exc)
    try:
        agent.reconfigure(
            replace(
                configuration,
                provider=selected_provider,
                model=selected_model,
                effort=selected_effort,
                models=runtime.models,
                auto_compaction_enabled=selected_auto_compaction_enabled,
            )
        )
    except RuntimeError as exc:
        lifecycle.fail(str(exc))
        return
    if mode is not None:
        agent.set_mode(mode)
    if auto_switched_provider is not None:
        write_event(
            ModelProviderAutoSwitched(
                command_id=command_id,
                provider=auto_switched_provider,
                model=cast(str, model),
            )
        )
    if configure_overrides is not None:
        if has_provider or selected_provider.name != configuration.provider.name:
            configure_overrides.provider = selected_provider.name
        if has_model or has_provider:
            configure_overrides.model = selected_model
            configure_overrides.has_model = True
        if has_effort or selected_effort != configuration.effort:
            configure_overrides.effort = selected_effort
            configure_overrides.has_effort = True
        if has_auto_compaction_enabled:
            configure_overrides.auto_compaction_enabled = selected_auto_compaction_enabled
            configure_overrides.has_auto_compaction_enabled = True
    if model_catalog is not None:
        write_event(RpcModelCatalogReported(command_id=command_id, catalog=model_catalog))
    elif model_catalog_error is not None:
        write_event(
            ErrorEvent(
                message=f"Configuration applied; model catalog unavailable: {model_catalog_error}"
            )
        )
    lifecycle.finish()


def auto_switch_provider_for_model(
    model: str,
    *,
    current_provider: Provider,
    runtime: WispRuntime,
) -> Provider:
    try:
        resolved_provider, _entry = runtime.models.resolve(model, prefer=current_provider.name)
    except UnknownModelError:
        return current_provider
    if resolved_provider == current_provider.name:
        return current_provider
    return runtime.providers.get(resolved_provider)


def handle_rpc_approval_command(
    command: ApprovalCommand,
    *,
    command_id: str,
    command_type: str,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    defer_resolution: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type=command_type,
        write_event=write_event,
    )
    call_id = command.call_id
    approved = command.approved
    reason = command.reason
    scope = command.scope or "once"
    if not approval_policy.has_pending_approval(call_id=call_id):
        lifecycle.fail(f"No pending tool approval with call_id: {call_id}")
        return
    resolve = partial(
        approval_policy.resolve_approval,
        call_id=call_id,
        approved=approved,
        reason=reason,
        scope=scope,
    )
    if defer_resolution is None:
        if not resolve():
            lifecycle.fail(f"No pending tool approval with call_id: {call_id}")
            return
        lifecycle.finish()
        return

    lifecycle.finish()

    def resolve_after_flush() -> None:
        resolve()

    defer_resolution(resolve_after_flush)


def handle_rpc_trust_command(
    command: TrustCommand,
    *,
    command_id: str,
    command_type: str,
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    defer_resolution: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type=command_type,
        write_event=write_event,
    )
    request_id = command.request_id
    trusted = command.trusted
    reason = command.reason
    transient = command.transient
    defer_release = defer_resolution is not None
    if not trust_gate.resolve_request(
        request_id=request_id,
        trusted=trusted,
        reason=reason,
        transient=transient is True,
        release=not defer_release,
    ):
        lifecycle.fail(f"No pending trust request with request_id: {request_id}")
        return
    lifecycle.finish()
    if defer_resolution is not None:
        defer_resolution(partial(trust_gate.release_request, request_id=request_id))


def handle_rpc_cancel_command(
    command: CancelCommand,
    *,
    command_id: str,
    command_type: str,
    running_command: _RpcRunningCommand | None,
    write_event: RpcEventWriter,
    coordinator: RpcCoordinator | None = None,
    defer_cancellation: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type=command_type,
        write_event=write_event,
    )
    target_id = command.target_id
    if (
        running_command is not None
        and running_command.command_id == target_id
        and defer_cancellation is not None
    ):
        lifecycle.finish()
        defer_cancellation(running_command.cancel_scope.cancel)
        return
    if coordinator is None:
        raise RuntimeError("RPC cancellation requires the shared coordinator")
    result = coordinator.cancel(target_id)
    if result.outcome == "running":
        lifecycle.finish()
        return
    queued_target = result.command
    if queued_target is None:
        lifecycle.fail(f"No running or queued RPC command with id: {target_id}")
        return
    target = RpcCommandLifecycle.start(
        command_id=target_id,
        command_type=queued_target.command_type,
        write_event=write_event,
    )
    target.finish(ok=False, error=f"RPC command cancelled: {target_id}")
    lifecycle.finish()


__all__ = ["RpcCommandExecutor"]
