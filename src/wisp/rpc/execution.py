"""Transport-independent command execution for the RPC frontend."""

from __future__ import annotations

import json
import stat
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.agent.mode import AgentMode, is_agent_mode
from wisp.agent.prompt import resolve_project_context_root
from wisp.coding import CodingSession
from wisp.events import (
    AgentStarted,
    CodingSessionState,
    ErrorEvent,
    MessageCompleted,
    ModelProviderAutoSwitched,
    QueueItemsRemoved,
    QueueKind,
    QueueMode,
    RpcCommandArgument,
    RpcCommandDescriptor,
    RpcCommandFinished,
    RpcCommandsReported,
    RpcCommandStarted,
    RpcMcpServerSnapshot,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
    RpcMessagesReported,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSessionSummary,
    RpcSessionTreeNavigated,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
    RpcSessionTreeUnreverted,
    RpcSkillCatalogEntry,
    RpcSkillCatalogSnapshot,
    RpcSkillDiagnostic,
    RpcSkillsReported,
    RpcStateReported,
    RpcStateSnapshot,
    SessionStatsReported,
    ToolExecutionEnded,
    WispEvent,
)
from wisp.providers.base import Provider
from wisp.providers.catalog import AmbiguousModelError, UnknownModelError
from wisp.rpc.commands import QUEUE_RPC_COMMAND_TYPES, ApprovalScope
from wisp.runtime.api import WispRuntime
from wisp.runtime.commands import CommandDescriptor
from wisp.runtime.registry import UnknownProviderError
from wisp.sessions.entries import MessageSessionEntry, SessionEntry, SessionInfoSessionEntry
from wisp.sessions.errors import SessionNavigationCancelledError
from wisp.sessions.jsonl import (
    DEFAULT_SESSION_MESSAGE_PAGE_LIMIT,
    DEFAULT_SESSION_TREE_PAGE_LIMIT,
    MAX_SESSION_MESSAGE_PAGE_LIMIT,
    MAX_SESSION_TREE_PAGE_LIMIT,
    JsonlSession,
    JsonlSessionStore,
    SessionError,
    SessionMessagePage,
    SessionSummary,
    SessionTreeNodeSummary,
    SessionTreePage,
)
from wisp.sessions.replay import replay_session_entries, resolve_session_tree
from wisp.tools.context import ToolContext

from .configuration import _RpcConfigureOverrides
from .coordinator import (
    RpcCoordinator,
    _RpcCancelResult,
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcDispatchResult,
    _RpcPromptReady,
    _RpcRunningCommand,
    _RpcSessionState,
)
from .errors import RpcOutputAlreadyReportedError

type RpcEventWriter = Callable[[WispEvent], None]
type RpcEventRenderer = Callable[[AsyncIterator[WispEvent]], Awaitable[None]]
type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]

DEFAULT_RPC_SESSION_CATALOG_LIMIT = 50
MAX_RPC_SESSION_CATALOG_LIMIT = 200
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

    def dispatch(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        self.coordinator.running_command = running_command
        command_type = rpc_command_type(command)
        if command_type == "prompt":
            return self._dispatch_prompt(command)
        if command_type == "init":
            return self._dispatch_init(command)
        if command_type == "compact":
            return self._dispatch_compact(command)
        if command_type == "get_session_stats":
            return self._dispatch_session_stats(command)
        if command_type == "get_messages":
            return self._dispatch_messages(command)
        if command_type == "get_sessions":
            return self._dispatch_sessions(command)
        if command_type == "new_session":
            return self._dispatch_new_session(command, running_command)
        if command_type == "select_session":
            return self._dispatch_select_session(command)
        if command_type == "clone_session":
            return self._dispatch_clone_session(command)
        if command_type == "fork_session":
            return self._dispatch_fork_session(command)
        if command_type == "get_session_tree":
            return self._dispatch_session_tree(command)
        if command_type == "navigate_session_tree":
            return self._dispatch_navigate_session_tree(command)
        if command_type == "unrevert_session_tree":
            return self._dispatch_unrevert_session_tree(command)
        if command_type == "set_session_name":
            return self._dispatch_set_session_name(command)
        if command_type == "get_state":
            return self._dispatch_state(command, running_command)
        if command_type == "get_commands":
            return self._dispatch_commands(command, running_command)
        if command_type == "get_skills":
            return self._dispatch_skills(command, running_command)
        if command_type == "get_mcp_status":
            return self._dispatch_mcp_status(command, running_command)
        if command_type in QUEUE_RPC_COMMAND_TYPES:
            raise RuntimeError("Queue RPC commands require asynchronous dispatch")
        return self._dispatch_control(command, running_command)

    async def dispatch_async(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        """Dispatch commands while allowing queue preparation to perform safe I/O."""

        if rpc_command_type(command) in QUEUE_RPC_COMMAND_TYPES:
            self.coordinator.running_command = running_command
            return await self._dispatch_queue(command, running_command)
        return self.dispatch(command, running_command)

    def _dispatch_prompt(self, command: dict[str, object]) -> _RpcDispatchResult:
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

    def _dispatch_init(self, command: dict[str, object]) -> _RpcDispatchResult:
        try:
            instructions, tool_context, target = _project_init_request(self.agent)
        except ValueError as exc:
            self.reject(command, str(exc))
            return _RpcDispatchResult(
                running_command=None,
                selected_session=self.session_state.session,
            )
        completion = _ProjectInitCompletion(
            target,
            conflicting_paths=(target.with_name("AGENTS.MD"),),
        )
        new_running_command, new_session = start_rpc_prompt_command(
            {**command, "prompt": "/init"},
            agent=self.agent,
            sessions=self.sessions,
            session_state=self.session_state,
            task_group=self.task_group,
            send=self.send,
            trust_gate=self.trust_gate,
            write_event=self.write_event,
            render_events=self.render_events,
            tool_context=tool_context,
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

    def _dispatch_compact(self, command: dict[str, object]) -> _RpcDispatchResult:
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

    def _dispatch_session_stats(self, command: dict[str, object]) -> _RpcDispatchResult:
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

    def _dispatch_messages(self, command: dict[str, object]) -> _RpcDispatchResult:
        return _RpcDispatchResult(
            running_command=start_rpc_messages_command(
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

    def _dispatch_sessions(self, command: dict[str, object]) -> _RpcDispatchResult:
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
        command: dict[str, object],
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

    def _dispatch_select_session(self, command: dict[str, object]) -> _RpcDispatchResult:
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

    def _dispatch_clone_session(self, command: dict[str, object]) -> _RpcDispatchResult:
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

    def _dispatch_fork_session(self, command: dict[str, object]) -> _RpcDispatchResult:
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

    def _dispatch_session_tree(self, command: dict[str, object]) -> _RpcDispatchResult:
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
        command: dict[str, object],
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
        command: dict[str, object],
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
        command: dict[str, object],
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
        command: dict[str, object],
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
        command: dict[str, object],
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
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_commands_command(
            command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_skills(
        self,
        command: dict[str, object],
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
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        handle_rpc_mcp_status_command(
            command,
            runtime=self.runtime,
            write_event=self.write_event,
        )
        return _RpcDispatchResult(running_command=running_command)

    def _dispatch_control(
        self,
        command: dict[str, object],
        running_command: _RpcRunningCommand | None,
    ) -> _RpcDispatchResult:
        should_shutdown = handle_rpc_control_command(
            command,
            running_command=running_command,
            approval_policy=self.approval_policy,
            agent=self.agent,
            runtime=self.runtime,
            trust_gate=self.trust_gate,
            configure_overrides=self.configure_overrides,
            coordinator=self.coordinator,
            write_event=self.write_event,
            defer_until_after_flush=self.defer_until_after_flush,
        )
        return _RpcDispatchResult(
            running_command=running_command,
            should_shutdown=should_shutdown,
        )

    def reject(self, command: dict[str, object], message: str) -> None:
        reject_rpc_command(command, message=message, write_event=self.write_event)


def handle_rpc_new_session_command(
    command: dict[str, object],
    *,
    running_command: _RpcRunningCommand | None,
    write_event: RpcEventWriter,
) -> bool:
    """Validate and synchronously reset the selected-session state."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return False
    if running_command is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="Cannot start a new session while another RPC operation is active",
            write_event=write_event,
        )
        return False
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
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


def _project_init_request(agent: CodingSession) -> tuple[str, ToolContext, Path]:
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
    return (
        prompt,
        replace(
            agent.tool_context,
            cwd=project_root,
            allowed_write_paths=(target,),
            conflicting_write_paths=(target.with_name("AGENTS.MD"),),
            require_create_only_writes=True,
            require_non_empty_writes=True,
        ),
        target,
    )


@dataclass(slots=True)
class _ProjectInitCompletion:
    target: Path
    conflicting_paths: tuple[Path, ...]
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
            try:
                info = self.target.lstat()
            except OSError:
                return
            self.created_file_id = (info.st_dev, info.st_ino)

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
    command: dict[str, object],
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
    operation_instructions: str | None = None,
    operation_tool_names: frozenset[str] | None = None,
    event_observer: Callable[[WispEvent], None] | None = None,
    completion_error: Callable[[], str | None] | None = None,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> tuple[_RpcRunningCommand | None, JsonlSession | None]:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None, session_state.session

    prompt = command.get("prompt")
    if not isinstance(prompt, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC prompt command requires string field: prompt",
            write_event=write_event,
        )
        return None, session_state.session

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
    command: dict[str, object],
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
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    raw_instructions = command.get("instructions")
    if raw_instructions is not None and not isinstance(raw_instructions, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC compact command field instructions must be a string",
            write_event=write_event,
        )
        return None
    instructions = raw_instructions.strip() or None if isinstance(raw_instructions, str) else None

    session = session_state.session
    if session is None or not session.path.is_file():
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC compact command requires an existing persisted session",
            write_event=write_event,
        )
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
    command: dict[str, object],
    *,
    agent: CodingSession,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

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


def start_rpc_messages_command(
    command: dict[str, object],
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    limit = _rpc_message_limit(command)
    if isinstance(limit, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=limit,
            write_event=write_event,
        )
        return None
    session_id = _optional_non_empty_string(command, "session_id", command_type)
    if isinstance(session_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(session_id),
            write_event=write_event,
        )
        return None
    before_entry_id = _optional_non_empty_string(command, "before_entry_id", command_type)
    if isinstance(before_entry_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(before_entry_id),
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_messages_command,
        sessions,
        session_state.session,
        session_state.entry_count,
        session_id,
        limit,
        before_entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_messages",
        cancel_scope=cancel_scope,
    )


def start_rpc_sessions_command(
    command: dict[str, object],
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    limit = _rpc_session_catalog_limit(command)
    if isinstance(limit, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=limit,
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_sessions_command,
        sessions,
        session_state.session,
        session_state.entry_count,
        limit,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_sessions",
        cancel_scope=cancel_scope,
    )


def start_rpc_select_session_command(
    command: dict[str, object],
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    session_id = _required_non_empty_string(command, "session_id", command_type)
    if isinstance(session_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(session_id),
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_select_session_command,
        sessions,
        session_state.entry_count,
        session_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="select_session",
        cancel_scope=cancel_scope,
    )


def start_rpc_clone_session_command(
    command: dict[str, object],
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None
    if session_state.session is None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC clone_session command requires a selected session",
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_clone_session_command,
        sessions,
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
        command_type="clone_session",
        cancel_scope=cancel_scope,
    )


def start_rpc_fork_session_command(
    command: dict[str, object],
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None
    entry_id = _required_non_empty_string(command, "entry_id", command_type)
    if isinstance(entry_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(entry_id),
            write_event=write_event,
        )
        return None
    if session_state.session is None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC fork_session command requires a selected session",
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_fork_session_command,
        sessions,
        session_state.session,
        session_state.entry_count,
        entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="fork_session",
        cancel_scope=cancel_scope,
    )


def start_rpc_session_tree_command(
    command: dict[str, object],
    *,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None

    limit = _rpc_session_tree_limit(command)
    if isinstance(limit, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=limit,
            write_event=write_event,
        )
        return None
    after_entry_id = _optional_non_empty_string(command, "after_entry_id", command_type)
    if isinstance(after_entry_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(after_entry_id),
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_session_tree_command,
        session_state.session,
        session_state.entry_count,
        limit,
        after_entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="get_session_tree",
        cancel_scope=cancel_scope,
    )


def start_rpc_navigate_session_tree_command(
    command: dict[str, object],
    *,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None
    entry_id = _required_non_empty_string(command, "entry_id", command_type)
    if isinstance(entry_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(entry_id),
            write_event=write_event,
        )
        return None
    session = session_state.session
    if session is None or not session.path.is_file():
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=("RPC navigate_session_tree command requires an existing persisted session"),
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_navigate_session_tree_command,
        session,
        session_state.entry_count,
        entry_id,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="navigate_session_tree",
        cancel_scope=cancel_scope,
    )


def start_rpc_unrevert_session_tree_command(
    command: dict[str, object],
    *,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None
    session = session_state.session
    if session is None or not session.path.is_file():
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=("RPC unrevert_session_tree command requires an existing persisted session"),
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_unrevert_session_tree_command,
        session,
        session_state.entry_count,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="unrevert_session_tree",
        cancel_scope=cancel_scope,
    )


def start_rpc_set_session_name_command(
    command: dict[str, object],
    *,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return None
    name = command.get("name")
    if not isinstance(name, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC set_session_name command requires string field: name",
            write_event=write_event,
        )
        return None
    session_id = _optional_non_empty_string(command, "session_id", command_type)
    if isinstance(session_id, ValueError):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(session_id),
            write_event=write_event,
        )
        return None
    selected_session = session_state.session
    if session_id is None and selected_session is None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC set_session_name command requires a selected session or session_id",
            write_event=write_event,
        )
        return None

    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_set_session_name_command,
        sessions,
        selected_session,
        session_state.entry_count,
        session_id,
        name,
        command_id,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="set_session_name",
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


async def run_rpc_messages_command(
    sessions: JsonlSessionStore,
    selected_session: JsonlSession | None,
    selected_entry_count: int,
    session_id: str | None,
    limit: int,
    before_entry_id: str | None,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    try:
        with cancel_scope:
            selected_read = session_id is None
            if session_id is None:
                session = selected_session
            else:
                session = await _run_abandonable_session_read(sessions.load, session_id)

            if session is None:
                page = SessionMessagePage(
                    session_id=None,
                    path=None,
                    active_leaf_id=None,
                    messages=(),
                    truncated=False,
                    next_before_entry_id=None,
                )
            elif not session.path.is_file():
                page = SessionMessagePage(
                    session_id=session.session_id,
                    path=session.path,
                    active_leaf_id=None,
                    messages=(),
                    truncated=False,
                    next_before_entry_id=None,
                )
            else:
                page = await _run_abandonable_session_read(
                    partial(
                        session.read_message_page,
                        limit=limit,
                        before_entry_id=before_entry_id,
                    )
                )

            if selected_read and session is not None:
                refreshed_entry_count, refreshed_history = await _run_abandonable_session_read(
                    updated_rpc_session_state,
                    session,
                    (),
                    selected_entry_count,
                )
            if cancel_scope.cancel_called:
                error = "RPC get_messages command cancelled"
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
            else:
                write_event(
                    RpcMessagesReported(
                        command_id=command_id,
                        session_id=page.session_id,
                        session_path=page.path,
                        active_leaf_id=page.active_leaf_id,
                        messages=page.messages,
                        truncated=page.truncated,
                        next_before_entry_id=page.next_before_entry_id,
                    )
                )
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC get_messages command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_messages command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_messages",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_messages",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_sessions_command(
    sessions: JsonlSessionStore,
    selected_session: JsonlSession | None,
    selected_entry_count: int,
    limit: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    try:
        with cancel_scope:
            summaries = await _run_abandonable_session_read(
                partial(sessions.summaries, limit=limit)
            )
            selected_session_name = (
                await _run_abandonable_session_read(selected_session.read_name)
                if selected_session is not None
                else None
            )
            if cancel_scope.cancel_called:
                error = "RPC get_sessions command cancelled"
            else:
                write_event(
                    RpcSessionsReported(
                        command_id=command_id,
                        sessions=tuple(_rpc_session_summary(summary) for summary in summaries),
                        selected_session_id=(
                            selected_session.session_id if selected_session is not None else None
                        ),
                        selected_session_path=(
                            selected_session.path if selected_session is not None else None
                        ),
                        selected_session_name=selected_session_name,
                    )
                )
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC get_sessions command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_sessions command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_sessions",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_sessions",
                ok=ok,
                history=None,
                entry_count=selected_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_select_session_command(
    sessions: JsonlSessionStore,
    selected_entry_count: int,
    session_id: str,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    selected_session: JsonlSession | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    active_leaf_id: str | None = None
    refreshed_name: str | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            loaded_session = await _run_abandonable_session_read(sessions.load, session_id)
            (
                refreshed_entry_count,
                refreshed_history,
                active_leaf_id,
                refreshed_name,
            ) = await _run_abandonable_session_read(rpc_selected_session_state, loaded_session)
            if cancel_scope.cancel_called:
                error = "RPC select_session command cancelled"
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
            else:
                selected = RpcSessionSelected(
                    command_id=command_id,
                    session_id=loaded_session.session_id,
                    session_path=loaded_session.path,
                    active_leaf_id=active_leaf_id,
                    entry_count=refreshed_entry_count,
                    session_name=refreshed_name,
                )
                selected_session = loaded_session
                post_apply_events = (
                    selected,
                    RpcCommandFinished(
                        command_id=command_id,
                        command_type="select_session",
                        ok=True,
                    ),
                )
                finish_in_worker = False
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC select_session command cancelled"
    except BaseException as exc:
        selected_session = None
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC select_session command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="select_session",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="select_session",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                selected_session=selected_session,
                session_name=refreshed_name,
                session_name_updated=ok,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_clone_session_command(
    sessions: JsonlSessionStore,
    source_session: JsonlSession,
    selected_entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    selected_session: JsonlSession | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    refreshed_name: str | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            if not source_session.path.is_file():
                raise SessionError("Cannot clone an empty session")
            source_active_leaf_id = await _run_abandonable_session_read(
                source_session.read_active_leaf_id
            )
            await anyio.sleep(0)
            # Publishing a derived session is the durable commit boundary. Once
            # entered, cancellation must not leave a created target reported as
            # failed or silently detached from the coordinator.
            with anyio.CancelScope(shield=True):
                cloned_session = await sessions.clone(
                    source_session,
                    expected_active_leaf_id=source_active_leaf_id,
                )
                (
                    refreshed_entry_count,
                    refreshed_history,
                    active_leaf_id,
                    refreshed_name,
                ) = await anyio.to_thread.run_sync(
                    rpc_selected_session_state,
                    cloned_session,
                )
            cloned = RpcSessionCloned(
                command_id=command_id,
                source_session_id=source_session.session_id,
                source_session_path=source_session.path,
                source_active_leaf_id=source_active_leaf_id,
                source_session_name=refreshed_name,
                session_id=cloned_session.session_id,
                session_path=cloned_session.path,
                active_leaf_id=active_leaf_id,
                session_name=refreshed_name,
                entry_count=refreshed_entry_count,
            )
            selected_session = cloned_session
            post_apply_events = (
                cloned,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="clone_session",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC clone_session command cancelled"
    except BaseException as exc:
        selected_session = None
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC clone_session command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="clone_session",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="clone_session",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                selected_session=selected_session,
                session_name=refreshed_name,
                session_name_updated=ok,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_fork_session_command(
    sessions: JsonlSessionStore,
    source_session: JsonlSession,
    selected_entry_count: int,
    entry_id: str,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    selected_session: JsonlSession | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    refreshed_name: str | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            if not source_session.path.is_file():
                raise SessionError("Cannot fork an empty session")
            source_active_leaf_id = await _run_abandonable_session_read(
                source_session.read_active_leaf_id
            )
            await anyio.sleep(0)
            with anyio.CancelScope(shield=True):
                fork_result = await sessions.fork_from_user_message(
                    source_session,
                    entry_id,
                    expected_active_leaf_id=source_active_leaf_id,
                )
                (
                    refreshed_entry_count,
                    refreshed_history,
                    active_leaf_id,
                    refreshed_name,
                ) = await anyio.to_thread.run_sync(
                    rpc_derived_session_state,
                    fork_result.session,
                )
            forked = RpcSessionForked(
                command_id=command_id,
                source_session_id=fork_result.source_session_id,
                source_session_path=source_session.path,
                source_active_leaf_id=fork_result.source_active_leaf_id,
                source_session_name=fork_result.source_session_name,
                session_id=fork_result.session.session_id,
                session_path=fork_result.session.path,
                active_leaf_id=active_leaf_id,
                session_name=refreshed_name,
                entry_count=refreshed_entry_count,
                selected_entry_id=fork_result.selected_entry_id,
                selected_prompt=fork_result.selected_prompt,
            )
            selected_session = fork_result.session
            post_apply_events = (
                forked,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="fork_session",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC fork_session command cancelled"
    except BaseException as exc:
        selected_session = None
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC fork_session command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="fork_session",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="fork_session",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                selected_session=selected_session,
                session_name=refreshed_name,
                session_name_updated=ok,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_session_tree_command(
    session: JsonlSession | None,
    selected_entry_count: int,
    limit: int,
    after_entry_id: str | None,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    try:
        with cancel_scope:
            if session is None:
                if after_entry_id is not None:
                    raise SessionError(f"Session tree cursor not found: {after_entry_id}")
                page = SessionTreePage(
                    session_id=None,
                    path=None,
                    active_leaf_id=None,
                    total_node_count=0,
                    nodes=(),
                    truncated=False,
                    next_after_entry_id=None,
                )
            else:
                page = await _run_abandonable_session_read(
                    partial(
                        session.read_tree_page,
                        limit=limit,
                        after_entry_id=after_entry_id,
                    )
                )
                if session.path.is_file():
                    (
                        refreshed_entry_count,
                        refreshed_history,
                        _,
                        _name,
                    ) = await _run_abandonable_session_read(
                        rpc_selected_session_state,
                        session,
                    )
            if cancel_scope.cancel_called:
                error = "RPC get_session_tree command cancelled"
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
            else:
                write_event(
                    RpcSessionTreeReported(
                        command_id=command_id,
                        session_id=page.session_id,
                        session_path=page.path,
                        active_leaf_id=page.active_leaf_id,
                        total_node_count=page.total_node_count,
                        nodes=tuple(_rpc_session_tree_node(node) for node in page.nodes),
                        truncated=page.truncated,
                        next_after_entry_id=page.next_after_entry_id,
                    )
                )
                ok = True
        if cancel_scope.cancel_called and error is None:
            error = "RPC get_session_tree command cancelled"
    except BaseException as exc:
        if isinstance(exc, anyio.get_cancelled_exc_class()):
            error = "RPC get_session_tree command cancelled"
        else:
            error = str(exc)
    finally:
        write_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_session_tree",
                ok=ok,
                error=error,
            )
        )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="get_session_tree",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
            )
        )
        await send.aclose()


async def run_rpc_navigate_session_tree_command(
    session: JsonlSession,
    selected_entry_count: int,
    entry_id: str,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            expected_active_leaf_id = await _run_abandonable_session_read(
                session.read_active_leaf_id
            )
            await anyio.sleep(0)
            navigation = await session.navigate_tree(
                entry_id,
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=command_id,
                cancel_requested=lambda: cancel_scope.cancel_called,
            )
            # A changed navigation has crossed its durable commit boundary.
            # Replay and coordinator publication must then complete even if
            # cancellation arrives after the active-leaf append.
            with anyio.CancelScope(shield=navigation.changed):
                (
                    refreshed_entry_count,
                    refreshed_history,
                    active_leaf_id,
                    _name,
                ) = await anyio.to_thread.run_sync(
                    rpc_selected_session_state,
                    session,
                )
            if cancel_scope.cancel_called and not navigation.changed:
                refreshed_history = None
                refreshed_entry_count = selected_entry_count
                error = "RPC navigate_session_tree command cancelled"
                return
            navigated = RpcSessionTreeNavigated(
                command_id=command_id,
                session_id=session.session_id,
                session_path=session.path,
                selected_entry_id=navigation.selected_entry_id,
                previous_active_leaf_id=navigation.previous_active_leaf_id,
                active_leaf_id=active_leaf_id,
                editor_text=navigation.editor_text,
                changed=navigation.changed,
                entry_count=refreshed_entry_count,
            )
            post_apply_events = (
                navigated,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="navigate_session_tree",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC navigate_session_tree command cancelled"
    except BaseException as exc:
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(
            exc,
            (anyio.get_cancelled_exc_class(), SessionNavigationCancelledError),
        ):
            error = "RPC navigate_session_tree command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="navigate_session_tree",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="navigate_session_tree",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_unrevert_session_tree_command(
    session: JsonlSession,
    selected_entry_count: int,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            expected_active_leaf_id = await _run_abandonable_session_read(
                session.read_active_leaf_id
            )
            await anyio.sleep(0)
            unrevert = await session.unrevert_tree(
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=command_id,
                cancel_requested=lambda: cancel_scope.cancel_called,
            )
            # The active-leaf append is the durable commit boundary. Publish
            # refreshed coordinator state even if cancellation arrives afterward.
            with anyio.CancelScope(shield=True):
                (
                    refreshed_entry_count,
                    refreshed_history,
                    _active_leaf_id,
                    _name,
                ) = await anyio.to_thread.run_sync(rpc_selected_session_state, session)
            event = RpcSessionTreeUnreverted(
                command_id=command_id,
                session_id=session.session_id,
                session_path=session.path,
                source_transition_id=unrevert.source_transition_id,
                previous_active_leaf_id=unrevert.previous_active_leaf_id,
                active_leaf_id=unrevert.active_leaf_id,
                entry_count=refreshed_entry_count,
            )
            post_apply_events = (
                event,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="unrevert_session_tree",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC unrevert_session_tree command cancelled"
    except BaseException as exc:
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(
            exc,
            (anyio.get_cancelled_exc_class(), SessionNavigationCancelledError),
        ):
            error = "RPC unrevert_session_tree command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="unrevert_session_tree",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="unrevert_session_tree",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                post_apply_events=post_apply_events,
            )
        )
        await send.aclose()


async def run_rpc_set_session_name_command(
    sessions: JsonlSessionStore,
    selected_session: JsonlSession | None,
    selected_entry_count: int,
    session_id: str | None,
    name: str,
    command_id: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    ok = False
    error: str | None = None
    refreshed_history: tuple[Message, ...] | None = None
    refreshed_entry_count = selected_entry_count
    refreshed_name: str | None = None
    selected_target = False
    post_apply_events: tuple[WispEvent, ...] = ()
    finish_in_worker = True
    try:
        with cancel_scope:
            await anyio.sleep(0)
            if session_id is None:
                if selected_session is None:
                    raise SessionError(
                        "RPC set_session_name command requires a selected session or session_id"
                    )
                target = selected_session
                selected_target = True
            else:
                target = await _run_abandonable_session_read(sessions.load, session_id)
                selected_target = (
                    selected_session is not None
                    and target.session_id == selected_session.session_id
                    and _normalized_session_path(target)
                    == _normalized_session_path(selected_session)
                )
            await anyio.sleep(0)
            with anyio.CancelScope(shield=True):
                change = await target.set_name(
                    name,
                    operation_id=command_id,
                    cancel_requested=lambda: cancel_scope.cancel_called,
                )
                if selected_target:
                    (
                        refreshed_entry_count,
                        refreshed_history,
                        _active_leaf_id,
                        refreshed_name,
                    ) = await anyio.to_thread.run_sync(
                        rpc_selected_session_state,
                        target,
                    )
                else:
                    refreshed_entry_count = selected_entry_count
                    refreshed_name = None
            changed = RpcSessionNameChanged(
                command_id=command_id,
                session_id=change.session_id,
                session_path=change.path,
                previous_name=change.previous_name,
                name=change.name,
                entry_count=change.entry_count,
            )
            post_apply_events = (
                changed,
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="set_session_name",
                    ok=True,
                ),
            )
            finish_in_worker = False
            ok = True
        if cancel_scope.cancel_called and not ok:
            error = "RPC set_session_name command cancelled"
    except BaseException as exc:
        refreshed_history = None
        refreshed_entry_count = selected_entry_count
        refreshed_name = None
        selected_target = False
        post_apply_events = ()
        finish_in_worker = True
        if isinstance(
            exc,
            (anyio.get_cancelled_exc_class(), SessionNavigationCancelledError),
        ):
            error = "RPC set_session_name command cancelled"
        else:
            error = str(exc)
    finally:
        if finish_in_worker:
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="set_session_name",
                    ok=ok,
                    error=error,
                )
            )
        await send.send(
            command_completed_factory(
                command_id=command_id,
                command_type="set_session_name",
                ok=ok,
                history=refreshed_history,
                entry_count=refreshed_entry_count,
                session_name=refreshed_name,
                session_name_updated=ok and selected_target,
                post_apply_events=post_apply_events,
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
    operation_instructions: str | None = None,
    operation_tool_names: frozenset[str] | None = None,
    event_observer: Callable[[WispEvent], None] | None = None,
    completion_error: Callable[[], str | None] | None = None,
) -> None:
    error: str | None = None
    run_entry_start = entry_start
    run_active_leaf_id: str | None = None
    run_start_captured = False

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
                with anyio.CancelScope(shield=True):
                    await send.send(_RpcPromptReady(command_id=command_id))
                continue
            yield event

    with cancel_scope:
        try:
            agent.trusted = await trust_gate.resolve()
            agent_events = (
                agent.run(
                    prompt,
                    session=session,
                    history=committed_history,
                    operation_id=command_id,
                    operation_instructions=operation_instructions,
                    operation_tool_names=operation_tool_names,
                )
                if tool_context is None
                else agent.run(
                    prompt,
                    session=session,
                    history=committed_history,
                    operation_id=command_id,
                    tool_context=tool_context,
                    operation_instructions=operation_instructions,
                    operation_tool_names=operation_tool_names,
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


def updated_rpc_session_state(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[int, tuple[Message, ...]]:
    if not session.path.is_file():
        return entry_start, committed_history
    entries = session.read_entry_snapshot()
    replay = replay_session_entries(entries)
    return len(entries), replay.messages


def rpc_selected_session_state(
    session: JsonlSession,
) -> tuple[int, tuple[Message, ...], str | None, str | None]:
    entries = session.read_entries()
    replay = replay_session_entries(entries)
    return len(entries), replay.messages, replay.active_leaf_id, _session_name_from_entries(entries)


def _normalized_session_path(session: JsonlSession) -> Path:
    return session.path.expanduser().resolve(strict=False)


def _session_name_from_entries(entries: tuple[SessionEntry, ...]) -> str | None:
    name: str | None = None
    for entry in entries:
        if isinstance(entry, SessionInfoSessionEntry):
            name = entry.name
    return name


def rpc_derived_session_state(
    session: JsonlSession,
) -> tuple[int, tuple[Message, ...], str | None, str | None]:
    """Read a derived target, including a reserved empty first-message fork."""

    if not session.path.is_file():
        return 0, (), None, None
    return rpc_selected_session_state(session)


def _rpc_session_summary(summary: SessionSummary) -> RpcSessionSummary:
    return RpcSessionSummary(
        session_id=summary.session_id,
        session_path=summary.path,
        updated_at=summary.updated_at,
        entry_count=summary.entry_count,
        active_leaf_id=summary.active_leaf_id,
        name=summary.name,
    )


def _rpc_session_tree_node(summary: SessionTreeNodeSummary) -> RpcSessionTreeNode:
    return RpcSessionTreeNode(
        entry_id=summary.entry_id,
        parent_id=summary.parent_id,
        operation_id=summary.operation_id,
        created_at=summary.created_at,
        kind=summary.kind,
        role=summary.role,
        preview=summary.preview,
        preview_truncated=summary.preview_truncated,
    )


def reject_rpc_command(
    command: dict[str, object],
    *,
    message: str,
    write_event: RpcEventWriter,
) -> None:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=id_error or message,
        write_event=write_event,
    )


async def handle_rpc_queue_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    write_event: RpcEventWriter,
) -> None:
    """Execute one ordered queue command through the shared session facade."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

    removed: QueueItemsRemoved | None = None
    try:
        if command_type == "get_queue_state":
            state = agent.queue_state(session)
        elif command_type in {"steer", "follow_up"}:
            content = command.get("content")
            if not isinstance(content, str):
                write_rpc_command_error(
                    command_id=command_id,
                    command_type=command_type,
                    message=f"RPC {command_type} command requires string field: content",
                    write_event=write_event,
                )
                return
            state = (
                await agent.steer(content)
                if command_type == "steer"
                else await agent.follow_up(content)
            )
        elif command_type == "set_queue_mode":
            kind = require_rpc_queue_kind(command, command_type=command_type)
            mode = require_rpc_queue_mode(command)
            state = agent.set_queue_mode(kind, mode)
        elif command_type == "pop_queue":
            kind = require_rpc_queue_kind(command, command_type=command_type)
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
        elif command_type == "clear_queue":
            clear_kind = optional_rpc_queue_kind(command, command_type=command_type)
            cleared, state = agent.clear_queue(clear_kind)
            removed = QueueItemsRemoved(
                command_id=command_id,
                operation="clear",
                kind=clear_kind,
                steering=tuple(message.user_visible_content for message in cleared.steering),
                follow_up=tuple(message.user_visible_content for message in cleared.follow_up),
            )
        else:  # pragma: no cover - dispatch owns the closed command set
            raise AssertionError(f"Unsupported queue RPC command: {command_type}")
    except (RuntimeError, ValueError) as exc:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(exc),
            write_event=write_event,
        )
        return

    if removed is not None:
        write_event(removed)
    write_event(state)
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_state_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    session: JsonlSession | None,
    session_name: str | None,
    running_command: _RpcRunningCommand | None,
    pending_prompt_queue_commands: tuple[dict[str, object], ...] = (),
    write_event: RpcEventWriter,
) -> None:
    """Return one coherent in-memory state snapshot without becoming active."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

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
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(exc),
            write_event=write_event,
        )
        return

    write_event(RpcStateReported(command_id=command_id, state=state))
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_commands_command(
    command: dict[str, object],
    *,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Return one coherent in-memory command registry snapshot without becoming active."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

    try:
        commands = tuple(
            _rpc_command_descriptor(descriptor) for descriptor in runtime.commands.all()
        )
    except Exception as exc:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(exc),
            write_event=write_event,
        )
        return

    write_event(RpcCommandsReported(command_id=command_id, commands=commands))
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_skills_command(
    command: dict[str, object],
    *,
    agent: CodingSession,
    write_event: RpcEventWriter,
) -> None:
    """Return the active immutable skill catalog without performing discovery."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

    write_event(RpcSkillsReported(command_id=command_id, catalog=rpc_skill_catalog_snapshot(agent)))
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def handle_rpc_mcp_status_command(
    command: dict[str, object],
    *,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Return sanitized startup status without reconnecting MCP servers."""

    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return

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
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


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
    commands: tuple[dict[str, object], ...],
) -> CodingSessionState:
    """Project prompt-startup queue commands without mutating coordinator/session state."""

    if not commands:
        return state
    steering_mode = state.steering_mode
    follow_up_mode = state.follow_up_mode
    steering_count = state.pending_steering_count
    follow_up_count = state.pending_follow_up_count
    for command in commands:
        command_type = rpc_command_type(command)
        _, id_error = rpc_command_id(command)
        if id_error is not None:
            continue
        try:
            if command_type == "steer":
                if isinstance(command.get("content"), str):
                    steering_count += 1
            elif command_type == "follow_up":
                if isinstance(command.get("content"), str):
                    follow_up_count += 1
            elif command_type == "set_queue_mode":
                kind = require_rpc_queue_kind(command, command_type=command_type)
                mode = require_rpc_queue_mode(command)
                if kind == "steering":
                    steering_mode = mode
                else:
                    follow_up_mode = mode
            elif command_type == "pop_queue":
                kind = require_rpc_queue_kind(command, command_type=command_type)
                if kind == "steering":
                    steering_count = max(0, steering_count - 1)
                else:
                    follow_up_count = max(0, follow_up_count - 1)
            elif command_type == "clear_queue":
                clear_kind = optional_rpc_queue_kind(command, command_type=command_type)
                if clear_kind is None:
                    steering_count = 0
                    follow_up_count = 0
                elif clear_kind == "steering":
                    steering_count = 0
                else:
                    follow_up_count = 0
        except ValueError:
            continue

    return state.model_copy(
        update={
            "steering_mode": steering_mode,
            "follow_up_mode": follow_up_mode,
            "pending_steering_count": steering_count,
            "pending_follow_up_count": follow_up_count,
        }
    )


def _rpc_message_limit(command: dict[str, object]) -> int | str:
    limit = command.get("limit", DEFAULT_SESSION_MESSAGE_PAGE_LIMIT)
    if type(limit) is not int:
        return "RPC get_messages command field limit must be an integer"
    if limit < 1 or limit > MAX_SESSION_MESSAGE_PAGE_LIMIT:
        return (
            "RPC get_messages command field limit must be between "
            f"1 and {MAX_SESSION_MESSAGE_PAGE_LIMIT}"
        )
    return limit


def _rpc_session_catalog_limit(command: dict[str, object]) -> int | str:
    limit = command.get("limit", DEFAULT_RPC_SESSION_CATALOG_LIMIT)
    if type(limit) is not int:
        return "RPC get_sessions command field limit must be an integer"
    if limit < 0 or limit > MAX_RPC_SESSION_CATALOG_LIMIT:
        return (
            "RPC get_sessions command field limit must be between "
            f"0 and {MAX_RPC_SESSION_CATALOG_LIMIT}"
        )
    return limit


def _rpc_session_tree_limit(command: dict[str, object]) -> int | str:
    limit = command.get("limit", DEFAULT_SESSION_TREE_PAGE_LIMIT)
    if type(limit) is not int:
        return "RPC get_session_tree command field limit must be an integer"
    if limit < 1 or limit > MAX_SESSION_TREE_PAGE_LIMIT:
        return (
            "RPC get_session_tree command field limit must be between "
            f"1 and {MAX_SESSION_TREE_PAGE_LIMIT}"
        )
    return limit


def _required_non_empty_string(
    command: dict[str, object],
    field: str,
    command_type: str,
) -> str | ValueError:
    value = command.get(field)
    if isinstance(value, str) and value:
        return value
    return ValueError(f"RPC {command_type} command field {field} must be a non-empty string")


def _optional_non_empty_string(
    command: dict[str, object],
    field: str,
    command_type: str,
) -> str | None | ValueError:
    value = command.get(field)
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    return ValueError(f"RPC {command_type} command field {field} must be a non-empty string")


def require_rpc_queue_kind(
    command: dict[str, object],
    *,
    command_type: str,
) -> QueueKind:
    kind = optional_rpc_queue_kind(command, command_type=command_type)
    if kind is None:
        raise ValueError(f"RPC {command_type} command field kind must be 'steering' or 'follow_up'")
    return kind


def optional_rpc_queue_kind(
    command: dict[str, object],
    *,
    command_type: str,
) -> QueueKind | None:
    kind = command.get("kind")
    if kind is None:
        return None
    if isinstance(kind, str) and kind in {"steering", "follow_up"}:
        return cast(QueueKind, kind)
    raise ValueError(f"RPC {command_type} command field kind must be 'steering' or 'follow_up'")


def require_rpc_queue_mode(command: dict[str, object]) -> QueueMode:
    mode = command.get("mode")
    if isinstance(mode, str) and mode in {"one_at_a_time", "all"}:
        return cast(QueueMode, mode)
    raise ValueError("RPC set_queue_mode command field mode must be 'one_at_a_time' or 'all'")


def handle_rpc_control_command(
    command: dict[str, object],
    *,
    running_command: _RpcRunningCommand | None,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    agent: CodingSession | None = None,
    runtime: WispRuntime | None = None,
    trust_gate: RpcTrustResolver | None = None,
    configure_overrides: _RpcConfigureOverrides | None = None,
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
    defer_until_after_flush: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    command_type, command_id, id_error = rpc_command_identity(command)
    write_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
            write_event=write_event,
        )
        return False
    if command_type == "shutdown":
        write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
        return True
    if command_type == "cancel":
        handle_rpc_cancel_command(
            command,
            command_id=command_id,
            command_type=command_type,
            running_command=running_command,
            coordinator=coordinator,
            queued_commands=queued_commands,
            write_event=write_event,
            defer_cancellation=defer_until_after_flush,
        )
        return False
    if command_type == "approval":
        handle_rpc_approval_command(
            command,
            command_id=command_id,
            command_type=command_type,
            approval_policy=approval_policy,
            write_event=write_event,
            defer_resolution=defer_until_after_flush,
        )
        return False
    if command_type == "trust":
        if trust_gate is None:
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC trust command requires an active trust gate",
                write_event=write_event,
            )
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
    if command_type == "configure":
        if running_command is not None:
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="Cannot configure while another RPC operation is active",
                write_event=write_event,
            )
            return False
        if agent is None or runtime is None:
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC configure command requires an active agent runtime",
                write_event=write_event,
            )
            return False
        handle_rpc_configure_command(
            command,
            command_id=command_id,
            command_type=command_type,
            agent=agent,
            runtime=runtime,
            configure_overrides=configure_overrides,
            write_event=write_event,
        )
        return False
    message = f"Unknown RPC command: {command_type}"
    write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=message,
        write_event=write_event,
    )
    return False


def handle_rpc_configure_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    agent: CodingSession,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> None:
    provider = command.get("provider")
    model = command.get("model")
    effort = command.get("effort")
    auto_compaction_enabled = command.get("auto_compaction_enabled")
    mode = command.get("mode")
    clear_effort = command.get("clear_effort") is True
    has_provider = "provider" in command
    has_model = "model" in command
    has_effort = "effort" in command or clear_effort
    has_auto_compaction_enabled = "auto_compaction_enabled" in command
    has_mode = "mode" in command
    if (
        not has_provider
        and not has_model
        and not has_effort
        and not has_auto_compaction_enabled
        and not has_mode
    ):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=(
                "RPC configure command requires provider, model, effort, "
                "auto_compaction_enabled, or mode"
            ),
            write_event=write_event,
        )
        return
    if provider is not None and not isinstance(provider, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field provider must be a string",
            write_event=write_event,
        )
        return
    if model is not None and not isinstance(model, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field model must be a string",
            write_event=write_event,
        )
        return
    if effort is not None and not isinstance(effort, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field effort must be a string",
            write_event=write_event,
        )
        return
    if has_mode and not is_agent_mode(mode):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field mode must be 'build' or 'plan'",
            write_event=write_event,
        )
        return
    if has_auto_compaction_enabled and not isinstance(auto_compaction_enabled, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field auto_compaction_enabled must be a boolean",
            write_event=write_event,
        )
        return
    configuration = agent.configuration
    selected_provider = configuration.provider
    selected_model = configuration.model
    selected_effort = configuration.effort
    selected_auto_compaction_enabled = configuration.auto_compaction_enabled
    if has_auto_compaction_enabled:
        selected_auto_compaction_enabled = cast(bool, auto_compaction_enabled)
    if isinstance(provider, str):
        try:
            selected_provider = runtime.providers.get(provider)
        except UnknownProviderError as exc:
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message=str(exc),
                write_event=write_event,
            )
            return
        if configure_overrides is not None:
            configure_overrides.provider = provider
        if not has_model:
            selected_model = None
            if configure_overrides is not None:
                configure_overrides.model = None
                configure_overrides.has_model = True
        if not has_effort:
            selected_effort = None
            if configure_overrides is not None:
                configure_overrides.effort = None
                configure_overrides.has_effort = True
    if has_model and provider is None and isinstance(model, str):
        selected_provider = auto_switch_provider_for_model(
            model,
            command_id=command_id,
            current_provider=selected_provider,
            runtime=runtime,
            configure_overrides=configure_overrides,
            write_event=write_event,
        )
        if not has_effort:
            selected_effort = None
            if configure_overrides is not None:
                configure_overrides.effort = None
                configure_overrides.has_effort = True
    if has_model:
        selected_model = model
        if configure_overrides is not None:
            configure_overrides.model = model
            configure_overrides.has_model = True
    if has_effort:
        selected_effort = None if clear_effort else effort
        if configure_overrides is not None:
            configure_overrides.effort = None if clear_effort else effort
            configure_overrides.has_effort = True
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
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=str(exc),
            write_event=write_event,
        )
        return
    if has_mode:
        agent.set_mode(cast(AgentMode, mode))
    if configure_overrides is not None and has_auto_compaction_enabled:
        configure_overrides.auto_compaction_enabled = selected_auto_compaction_enabled
        configure_overrides.has_auto_compaction_enabled = True
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def auto_switch_provider_for_model(
    model: str,
    *,
    command_id: str,
    current_provider: Provider,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None,
    write_event: RpcEventWriter,
) -> Provider:
    try:
        resolved_provider, _entry = runtime.models.resolve(model, prefer=current_provider.name)
    except (UnknownModelError, AmbiguousModelError):
        return current_provider
    if resolved_provider == current_provider.name:
        return current_provider
    try:
        new_provider = runtime.providers.get(resolved_provider)
    except UnknownProviderError:
        return current_provider
    write_event(
        ModelProviderAutoSwitched(command_id=command_id, provider=resolved_provider, model=model)
    )
    if configure_overrides is not None:
        configure_overrides.provider = resolved_provider
    return new_provider


def handle_rpc_approval_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    defer_resolution: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    call_id = command.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires string field: call_id",
            write_event=write_event,
        )
        return
    approved = command.get("approved")
    if not isinstance(approved, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires boolean field: approved",
            write_event=write_event,
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command field reason must be a string",
            write_event=write_event,
        )
        return
    raw_scope = command.get("scope")
    scope: ApprovalScope
    if raw_scope is None:
        scope = "once"
    elif isinstance(raw_scope, str) and raw_scope in {
        "once",
        "tool_session",
        "all_session",
    }:
        scope = cast(ApprovalScope, raw_scope)
    else:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=(
                "RPC approval command field scope must be one of: once, tool_session, all_session"
            ),
            write_event=write_event,
        )
        return
    if not approved and scope != "once":
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval scope is only valid for approved requests",
            write_event=write_event,
        )
        return
    if not approval_policy.has_pending_approval(call_id=call_id):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending tool approval with call_id: {call_id}",
            write_event=write_event,
        )
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
            write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message=f"No pending tool approval with call_id: {call_id}",
                write_event=write_event,
            )
            return
        write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
        return

    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))

    def resolve_after_flush() -> None:
        resolve()

    defer_resolution(resolve_after_flush)


def handle_rpc_trust_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    defer_resolution: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    request_id = command.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires string field: request_id",
            write_event=write_event,
        )
        return
    trusted = command.get("trusted")
    if not isinstance(trusted, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires boolean field: trusted",
            write_event=write_event,
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field reason must be a string",
            write_event=write_event,
        )
        return
    transient = command.get("transient")
    if transient is not None and not isinstance(transient, bool):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field transient must be a boolean",
            write_event=write_event,
        )
        return
    defer_release = defer_resolution is not None
    if not trust_gate.resolve_request(
        request_id=request_id,
        trusted=trusted,
        reason=reason,
        transient=transient is True,
        release=not defer_release,
    ):
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending trust request with request_id: {request_id}",
            write_event=write_event,
        )
        return
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
    if defer_resolution is not None:
        defer_resolution(partial(trust_gate.release_request, request_id=request_id))


def handle_rpc_cancel_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    running_command: _RpcRunningCommand | None,
    write_event: RpcEventWriter,
    coordinator: RpcCoordinator | None = None,
    queued_commands: deque[dict[str, object]] | None = None,
    defer_cancellation: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    target_id = command.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC cancel command requires string field: target_id",
            write_event=write_event,
        )
        return
    if (
        running_command is not None
        and running_command.command_id == target_id
        and defer_cancellation is not None
    ):
        write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
        defer_cancellation(running_command.cancel_scope.cancel)
        return
    if coordinator is not None:
        result = coordinator.cancel(target_id)
    else:
        result = legacy_rpc_cancel(
            target_id,
            running_command=running_command,
            queued_commands=queued_commands,
        )
    if result.outcome == "running":
        write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))
        return
    queued_target = result.command
    if queued_target is None:
        write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No running or queued RPC command with id: {target_id}",
            write_event=write_event,
        )
        return
    target_type = rpc_command_type(queued_target)
    write_event(RpcCommandStarted(command_id=target_id, command_type=target_type))
    write_event(
        RpcCommandFinished(
            command_id=target_id,
            command_type=target_type,
            ok=False,
            error=f"RPC command cancelled: {target_id}",
        )
    )
    write_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def legacy_rpc_cancel(
    target_id: str,
    *,
    running_command: _RpcRunningCommand | None,
    queued_commands: deque[dict[str, object]] | None,
) -> _RpcCancelResult:
    if running_command is not None and running_command.command_id == target_id:
        running_command.cancel_scope.cancel()
        return _RpcCancelResult("running")
    queued_target = next(
        (queued for queued in queued_commands or () if queued.get("id") == target_id),
        None,
    )
    if queued_target is None:
        return _RpcCancelResult("missing")
    assert queued_commands is not None
    queued_commands.remove(queued_target)
    return _RpcCancelResult("queued", command=queued_target)


def write_rpc_command_error(
    *,
    command_id: str,
    command_type: str,
    message: str,
    write_event: RpcEventWriter,
) -> None:
    write_event(ErrorEvent(message=message))
    write_event(
        RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=False,
            error=message,
        )
    )


def rpc_command_identity(command: dict[str, object]) -> tuple[str, str, str | None]:
    command_type = rpc_command_type(command)
    command_id, id_error = rpc_command_id(command)
    return command_type, command_id, id_error


def rpc_command_type(command: dict[str, object]) -> str:
    command_type = command.get("type")
    return command_type if isinstance(command_type, str) and command_type else "unknown"


def rpc_command_id(command: dict[str, object]) -> tuple[str, str | None]:
    command_id = command.get("id")
    if command_id is None:
        return uuid4().hex, None
    if isinstance(command_id, str) and command_id:
        return command_id, None
    return uuid4().hex, "RPC command id must be a non-empty string"


__all__ = ["RpcCommandExecutor"]
