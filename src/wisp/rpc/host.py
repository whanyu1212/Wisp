"""Transport-independent host for Wisp's long-lived command contract.

Both JSONL-RPC and in-process embeddings submit the same typed commands to this
host.  It owns command scheduling, durable-session selection, approvals, trust,
and configuration transitions; transports only adapt input and events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.coding.prompt import resolve_project_context_root
from wisp.config.runtime import WispConfig
from wisp.events import (
    ErrorEvent,
    PermissionMode,
    PermissionState,
    RpcCommandFinished,
    RpcGitDiffReported,
    RpcProjectFilesReported,
    SkillCatalogUpdated,
    TrustRequested,
    TrustResolved,
    WispEvent,
)
from wisp.rpc.commands import ApprovalScope, ParsedRpcCommand
from wisp.rpc.configuration import RpcProjectConfiguration, _ConfigOverrides, _RpcConfigureOverrides
from wisp.rpc.coordinator import (
    _MAX_QUEUED_RPC_COMMANDS,
    RpcControlReceiver,
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.execution import (
    RpcCommandExecutor,
    rpc_session_state,
)
from wisp.rpc.handlers.inspection import rpc_skill_catalog_snapshot, rpc_workspace_report
from wisp.rpc.handlers.mcp import RpcMcpServers, RpcToolSelection
from wisp.rpc.handlers.project_files import RpcProjectFiles
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.skills.lifecycle import discover_skill_catalog
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy, confirms_every_call
from wisp.tools.base import Tool, ToolSafety
from wisp.tools.mcp_servers import PROPOSE_MCP_SERVER, PROPOSE_MCP_SERVER_CHANGE
from wisp.tools.selection import select_session, select_tools, tool_approval_policy
from wisp.trust.permissions import (
    ignored_broad_root_yolo,
    load_permission_mode,
    save_permission_mode,
)
from wisp.trust.records import (
    ignored_broad_root_trust,
    is_trusted,
    record_trust,
    trust_override_from_env,
)
from wisp.trust.scope import BROAD_FOLDER_DESCRIPTION, is_broad_root

if TYPE_CHECKING:
    from wisp.providers.anthropic import AnthropicCacheTtl

type RpcEventWriter = Callable[[WispEvent], None]
type RpcEventRenderer = Callable[[AsyncIterator[WispEvent]], Awaitable[None]]
type RuntimeBuilder = Callable[[WispConfig], Awaitable[WispRuntime]]


@dataclass(frozen=True, slots=True)
class InProcessOptions:
    """Explicit runtime policy for an in-process Wisp controller.

    Tools are not exposed by default.  ``all_tools`` or ``allowed_tools`` only
    control model visibility; mutating and command tools still require an
    approval response unless ``approve_unsafe_tools`` is explicitly selected.

    ``cwd`` controls built-in tool path resolution and command execution.
    ``project_context_root`` independently controls trust, project settings,
    skills, and instruction discovery. When only a project root is supplied, it
    also becomes the tool working directory.
    """

    all_tools: bool = False
    allow_read_tools: bool = False
    allowed_tools: tuple[str, ...] = ()
    resume: str | None = None
    continue_latest: bool = False
    approve_unsafe_tools: bool = False
    max_tool_iterations: int | None = None
    startup_trusted: bool = False
    project_context_root: Path | None = None
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if self.resume is not None and self.continue_latest:
            raise ValueError("resume and continue_latest cannot both be set")
        if self.max_tool_iterations is not None and self.max_tool_iterations < 0:
            raise ValueError("max_tool_iterations must be non-negative")


@dataclass
class _PendingApproval:
    call_id: str
    tool_name: str
    tool_safety: ToolSafety
    event: anyio.Event
    approved: bool | None = None
    reason: str | None = None
    resolved: bool = False
    # Set for tools that confirm every call: only a one-time confirmation applies,
    # and its arguments are what the confirmation handler acts on.
    confirm_only: bool = False
    arguments: Mapping[str, object] | None = None


type ConfirmationHandler = Callable[[str, Mapping[str, object]], str | None]


def _broad_root_notices(project: Path) -> tuple[str, ...]:
    """Explain at startup that YOLO and trust are not kept for ``project``.

    Args:
        project (Path): The session's project folder.

    Returns:
        tuple[str, ...]: Notices to show, empty for an ordinary project folder.
    """

    if not is_broad_root(project):
        return ()
    notices = [
        f"Working in {project}, {BROAD_FOLDER_DESCRIPTION}: YOLO and trust are not saved "
        "for it and last for this session only. Start Wisp in a project folder to keep them."
    ]
    if ignored_broad_root_yolo(project):
        notices.append(
            f"YOLO saved for {project} by an earlier Wisp no longer applies. Choose Ask in "
            "/permissions to remove that setting."
        )
    if ignored_broad_root_trust(project):
        notices.append(
            f"A trust decision saved for {project} by an earlier Wisp no longer applies. "
            f"Run `wisp trust forget {project}` to remove it."
        )
    return tuple(notices)


class RpcToolApprovalPolicy(ToolApprovalPolicy):
    """Approval policy resolved by typed ``approval`` commands."""

    def __init__(self, fallback: ToolApprovalPolicy, *, project_path: Path | None = None) -> None:
        super().__init__(
            approved_tools=fallback.approved_tools,
            approved_safety=fallback.approved_safety,
        )
        self._pending: dict[str, _PendingApproval] = {}
        self._confirmations: dict[str, ConfirmationHandler] = {}
        self._input_closed_reason: str | None = None
        self._project_path = project_path
        # YOLO is never saved for the home folder or a folder above it, so there it
        # is granted for this session only (see ``wisp.trust.scope``).
        self.session_only_yolo = project_path is not None and is_broad_root(project_path)
        self._saved_mode = load_permission_mode(project_path) if project_path is not None else None
        if self._saved_mode == "yolo":
            self.approved_safety = self.approved_safety | {"mutating", "command"}
        self._baseline_approved_tools = self.approved_tools
        self._baseline_approved_safety = self.approved_safety

    def register_confirmation(self, tool_name: str, handler: ConfirmationHandler) -> None:
        """Apply a confirm-every-call tool's effect when the user confirms it.

        The handler receives the tool name and the exact arguments the user was
        shown, and returns an error to deny the call with, or None. A confirmed
        call of such a tool without a handler is denied, so a tool can never
        report an effect that nothing applied.

        Args:
            tool_name (str): Tool whose confirmation the handler applies.
            handler (ConfirmationHandler): Applies the change; returns an error or None.
        """

        self._confirmations[tool_name] = handler

    def reset_session_grants(self) -> None:
        """Expire temporary grants while retaining the selected project/startup mode."""

        self.approved_tools = self._baseline_approved_tools
        self.approved_safety = self._baseline_approved_safety

    def revoke_session_tool_grants(self, prefix: str) -> None:
        """Expire per-tool session grants for tools whose names start with ``prefix``.

        Startup grants and the permission mode are kept.

        Args:
            prefix (str): Tool-name prefix, such as ``"mcp__"`` for MCP tools.
        """

        self.approved_tools = frozenset(
            name
            for name in self.approved_tools
            if not name.startswith(prefix) or name in self._baseline_approved_tools
        )

    def permissions_snapshot(self) -> PermissionState:
        """Return effective permissions without reading the preference store."""

        return PermissionState(
            mode="yolo" if {"mutating", "command"} <= self.approved_safety else "ask",
            saved_mode=self._saved_mode,
            project_path=self._project_path,
            session_only_yolo=self.session_only_yolo,
        )

    def set_permissions(self, mode: PermissionMode) -> None:
        """Save the new default before applying it and clearing temporary grants.

        In the home folder or a folder above it, YOLO is not saved: it becomes a
        session grant, which a new session clears, and the saved mode is unchanged.
        Saving Ask there still replaces a YOLO record an earlier release saved.

        Args:
            mode (PermissionMode): Explicit user choice, overriding startup defaults
                (including an earlier --yes) for the remainder of this run.

        Raises:
            OSError: Saving fails; current approvals remain unchanged.
        """

        if mode == "yolo" and self.session_only_yolo:
            self.approved_tools = self._baseline_approved_tools
            self.approved_safety = self.approved_safety | {"read", "mutating", "command"}
            return
        if self._project_path is None:
            raise OSError("No project is available for saving permissions")
        save_permission_mode(self._project_path, mode)
        self._saved_mode = mode
        self.approved_tools = frozenset()
        self.approved_safety = (
            frozenset({"read", "mutating", "command"}) if mode == "yolo" else frozenset()
        )
        self._baseline_approved_tools = self.approved_tools
        self._baseline_approved_safety = self.approved_safety

    def prepare_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> None:
        if self.approves(tool):
            return
        confirm_only = confirms_every_call(tool)
        pending = _PendingApproval(
            call_id=call_id,
            tool_name=tool.name,
            tool_safety=tool.safety,
            event=anyio.Event(),
            confirm_only=confirm_only,
            arguments=dict(arguments) if confirm_only else None,
        )
        self._pending[call_id] = pending
        if self._input_closed_reason is not None:
            self._resolve_pending(pending, approved=False, reason=self._input_closed_reason)

    async def await_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalDecision:
        del arguments
        pending = self._pending.get(call_id)
        if pending is None:
            approved = self.approves(tool)
            return ToolApprovalDecision(
                approved=approved, reason=None if approved else self.block_reason(tool)
            )
        try:
            await pending.event.wait()
            approved = pending.approved is True
            if approved and pending.confirm_only:
                # Applied here, in the run's task and off the event loop: the
                # handler takes a file lock and writes settings.
                error = await anyio.to_thread.run_sync(self._apply_confirmation, pending)
                if error is not None:
                    return ToolApprovalDecision(approved=False, reason=error)
            reason = None if approved else pending.reason or "Tool execution was denied"
            return ToolApprovalDecision(approved=approved, reason=reason)
        finally:
            self._pending.pop(call_id, None)

    def has_pending_approval(self, *, call_id: str) -> bool:
        pending = self._pending.get(call_id)
        return pending is not None and not pending.resolved

    def cancel_approval(self, *, call_id: str, reason: str) -> bool:
        return self.resolve_approval(
            call_id=call_id,
            approved=False,
            reason=reason,
        )

    def resolve_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: str | None = None,
        scope: ApprovalScope = "once",
    ) -> bool:
        pending = self._pending.get(call_id)
        if pending is None or pending.resolved:
            return False
        if pending.confirm_only:
            # A grant never covers these tools; any approval is a one-time
            # confirmation, which `await_approval` applies before the call may
            # proceed.
            self._resolve_pending(pending, approved=approved, reason=reason)
            return True
        if approved and scope == "all_project":
            try:
                self.set_permissions("yolo")
            except OSError:
                self._resolve_pending(
                    pending,
                    approved=False,
                    reason="Could not save project permissions. Retry or choose Allow once.",
                )
                return True
        elif approved and scope == "tool_session":
            self.approved_tools = self.approved_tools | {pending.tool_name}
        elif approved and scope == "all_session":
            self.approved_safety = self.approved_safety | {"mutating", "command"}
        self._resolve_pending(pending, approved=approved, reason=reason)
        if approved and scope != "once":
            # A scoped grant also releases matching calls that are already waiting.
            # Explicit denials remain final, regardless of later mode changes.
            for other in self._pending.values():
                if (
                    not other.resolved
                    and not other.confirm_only
                    and (
                        other.tool_name in self.approved_tools
                        or other.tool_safety in self.approved_safety
                    )
                ):
                    self._resolve_pending(other, approved=True, reason=None)
        return True

    def _apply_confirmation(self, pending: _PendingApproval) -> str | None:
        handler = self._confirmations.get(pending.tool_name)
        if handler is None or pending.arguments is None:
            return f"This client cannot apply {pending.tool_name}"
        try:
            return handler(pending.tool_name, pending.arguments)
        except Exception as exc:  # noqa: BLE001 - deny instead of failing the host
            return f"{pending.tool_name} could not be applied: {type(exc).__name__}"

    def deny_pending_on_input_closed(self) -> None:
        reason = "RPC input closed before approval response"
        self._input_closed_reason = reason
        for pending in tuple(self._pending.values()):
            if not pending.resolved:
                self._resolve_pending(pending, approved=False, reason=reason)

    @staticmethod
    def _resolve_pending(
        pending: _PendingApproval,
        *,
        approved: bool,
        reason: str | None,
    ) -> None:
        pending.resolved = True
        pending.approved = approved
        pending.reason = None if approved else reason
        pending.event.set()


@dataclass
class _PendingTrust:
    request_id: str
    event: anyio.Event
    trusted: bool | None = None
    reason: str | None = None
    transient: bool = False
    resolved: bool = False


class RpcTrustGate:
    """Resolve project trust through typed events and commands."""

    def __init__(
        self,
        project_path: Path,
        *,
        write_event: RpcEventWriter,
        on_first_trusted: Callable[[], Awaitable[None]] | None = None,
        initially_trusted: bool = False,
    ) -> None:
        self._project_path = project_path
        self._write_event = write_event
        self._on_first_trusted = on_first_trusted
        self._pending: _PendingTrust | None = None
        self._decision: bool | None = True if initially_trusted else None
        self._resolved = initially_trusted
        self._input_closed = False

    async def resolve(self) -> bool:
        """Return the project decision, asking the frontend once when needed."""

        if self._resolved:
            assert self._decision is not None
            return self._decision
        override = trust_override_from_env()
        if override is not None:
            return await self._finish(override)
        input_closed_before_store = self._input_closed
        stored = await anyio.to_thread.run_sync(
            is_trusted,
            self._project_path,
            abandon_on_cancel=True,
        )
        if stored is not None:
            return await self._finish(stored)
        if input_closed_before_store:
            return await self._finish(False)

        # A process-independent random identifier prevents an untrusted project
        # from predicting a pending decision token.
        pending = _PendingTrust(request_id=uuid4().hex, event=anyio.Event())
        self._pending = pending
        self._write_event(
            TrustRequested(request_id=pending.request_id, project_path=self._project_path)
        )
        if self._input_closed:
            self.deny_pending_on_input_closed()
        await pending.event.wait()
        trusted = pending.trusted is True
        if is_broad_root(self._project_path):
            # The home folder or a folder above it keeps no decision; this
            # session's answer is not saved, so the next session asks again.
            pass
        elif trusted:
            await anyio.to_thread.run_sync(
                record_trust,
                self._project_path,
                True,
                abandon_on_cancel=True,
            )
        elif not pending.transient:
            await anyio.to_thread.run_sync(
                record_trust,
                self._project_path,
                False,
                abandon_on_cancel=True,
            )
        self._pending = None
        self._write_event(
            TrustResolved(
                request_id=pending.request_id,
                project_path=self._project_path,
                trusted=trusted,
                reason=pending.reason,
            )
        )
        return await self._finish(trusted)

    def resolve_request(
        self,
        *,
        request_id: str,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        release: bool = True,
    ) -> bool:
        pending = self._pending
        if pending is None or pending.resolved or pending.request_id != request_id:
            return False
        pending.resolved = True
        pending.trusted = trusted
        pending.reason = reason
        pending.transient = transient
        if release:
            pending.event.set()
        return True

    def release_request(self, *, request_id: str) -> None:
        """Release an accepted response after its command lifecycle is published."""

        pending = self._pending
        if pending is not None and pending.resolved and pending.request_id == request_id:
            pending.event.set()

    def deny_pending_on_input_closed(self) -> None:
        self._input_closed = True
        pending = self._pending
        if pending is not None and not pending.resolved:
            pending.resolved = True
            pending.trusted = False
            pending.reason = "RPC input closed before trust response"
            pending.transient = True
            pending.event.set()

    async def _finish(self, trusted: bool) -> bool:
        if trusted and self._on_first_trusted is not None and not self._resolved:
            await self._on_first_trusted()
        self._decision = trusted
        self._resolved = True
        return trusted


class _StartGatedTaskGroup:
    """Delay command workers until their initial lifecycle events are published."""

    def __init__(self, task_group: TaskGroup, start_gate: anyio.Event) -> None:
        self._task_group = task_group
        self._start_gate = start_gate

    def start_soon(
        self,
        func: Callable[..., Awaitable[None]],
        *args: object,
    ) -> None:
        self._task_group.start_soon(self._run_when_released, func, args)

    async def _run_when_released(
        self,
        func: Callable[..., Awaitable[None]],
        args: tuple[object, ...],
    ) -> None:
        await self._start_gate.wait()
        await func(*args)


class RpcHost:
    """Run the shared command contract against one runtime and session store."""

    def __init__(
        self,
        *,
        runtime: WispRuntime,
        sessions: JsonlSessionStore,
        agent: CodingSession,
        approval_policy: RpcToolApprovalPolicy,
        trust_gate: RpcTrustGate,
        configure_overrides: _RpcConfigureOverrides,
        coordinator: RpcCoordinator,
        write_event: RpcEventWriter,
        render_events: RpcEventRenderer,
        on_shutdown_dispatched: Callable[[], None] | None = None,
        on_shutdown_abandoned: Callable[[], None] | None = None,
        outbound_frame_limit: int | None = None,
        tool_selection: RpcToolSelection | None = None,
    ) -> None:
        self.runtime = runtime
        self.sessions = sessions
        self.agent = agent
        self.project_files = RpcProjectFiles(agent.tool_context)
        self.mcp_servers = (
            RpcMcpServers(
                runtime=runtime,
                agent=agent,
                tool_selection=tool_selection,
                revoke_session_tool_grants=approval_policy.revoke_session_tool_grants,
            )
            if tool_selection is not None
            else None
        )
        if self.mcp_servers is not None:
            for tool_name in (PROPOSE_MCP_SERVER, PROPOSE_MCP_SERVER_CHANGE):
                approval_policy.register_confirmation(tool_name, self.mcp_servers.confirm_proposal)
        self.approval_policy = approval_policy
        self.trust_gate = trust_gate
        self.configure_overrides = configure_overrides
        self.coordinator = coordinator
        self._write_event = write_event
        self._render_events = render_events
        self._outbound_frame_limit = outbound_frame_limit
        self._on_shutdown_dispatched = on_shutdown_dispatched
        self._on_shutdown_abandoned = on_shutdown_abandoned
        self._event_render_lock = anyio.Lock()
        self._event_task_group: TaskGroup | None = None
        self._pending_published_events = 0
        self._published_events_drained = anyio.Event()
        self._published_events_drained.set()

    @classmethod
    async def create(
        cls,
        config: WispConfig,
        runtime: WispRuntime,
        *,
        options: InProcessOptions,
        write_event: RpcEventWriter,
        render_events: RpcEventRenderer,
        config_overrides: _ConfigOverrides | None = None,
        runtime_builder: RuntimeBuilder | None = None,
        max_queued_commands: int | None = None,
        on_shutdown_dispatched: Callable[[], None] | None = None,
        on_shutdown_abandoned: Callable[[], None] | None = None,
        outbound_frame_limit: int | None = None,
    ) -> RpcHost:
        """Build a host without starting a transport or event loop."""

        sessions = JsonlSessionStore(config.session_dir)

        def load_startup_state() -> tuple[_RpcSessionState, Path, Path]:
            session = select_session(
                sessions,
                resume=options.resume,
                continue_latest=options.continue_latest,
            )
            session_state = rpc_session_state(session)
            cwd = (
                (options.cwd or options.project_context_root or Path.cwd())
                .expanduser()
                .resolve(strict=False)
            )
            project_context_root = (
                (options.project_context_root or resolve_project_context_root(cwd))
                .expanduser()
                .resolve(strict=False)
            )
            return session_state, project_context_root, cwd

        session_state, project_context_root, cwd = await anyio.to_thread.run_sync(
            load_startup_state,
            abandon_on_cancel=True,
        )
        approval_policy = RpcToolApprovalPolicy(
            tool_approval_policy(options.approve_unsafe_tools), project_path=project_context_root
        )
        configure_overrides = _RpcConfigureOverrides()
        selected_runtime_builder = runtime_builder or build_session_runtime
        project_configuration = RpcProjectConfiguration(
            startup_config=config,
            startup_trusted=options.startup_trusted,
            config_overrides=config_overrides,
            project_context_root=project_context_root,
            runtime_builder=selected_runtime_builder,
            configure_overrides=configure_overrides,
        )
        skill_catalog = await discover_skill_catalog(
            project_root=project_context_root,
            trusted=options.startup_trusted,
            protected_paths=config.protected_paths,
        )
        initial_configuration = resolve_coding_session_configuration(
            config,
            providers=runtime.providers,
            models=runtime.models,
            trusted=options.startup_trusted,
            cwd=cwd,
            skill_catalog=skill_catalog,
        )
        agent = CodingSession.from_configuration(
            initial_configuration,
            sessions=sessions,
            events=runtime.events,
            tool_registry=select_tools(
                runtime.tools,
                all_tools=options.all_tools,
                allow_read_tools=options.allow_read_tools,
                allowed_tools=options.allowed_tools,
                ignored_unknown_prefixes=runtime.unavailable_tool_prefixes,
            ),
            tool_approval_policy=approval_policy,
            max_tool_iterations=options.max_tool_iterations,
            project_context_root=project_context_root,
        )

        host: RpcHost | None = None

        def publish_event(event: WispEvent) -> None:
            if host is None:
                write_event(event)
            else:
                host._publish_event(event)

        async def rebuild_agent_for_trusted_project() -> None:
            assert host is not None
            try:
                async with host._event_render_lock:
                    invalidated = host.project_files.begin_policy_transition()

                    async def invalidation_events() -> AsyncIterator[WispEvent]:
                        yield invalidated

                    # Keep invalidation observable once its generation changed.
                    with anyio.CancelScope(shield=True):
                        await host._render_events(invalidation_events())
                event = await project_configuration.apply_trusted_project(
                    runtime=runtime,
                    agent=agent,
                    reserve_protected_paths=host.project_files.reserve_protected_paths,
                )
            finally:
                host.project_files.settle_policy(agent.tool_context)
            if event is not None:
                publish_event(event)
            publish_event(SkillCatalogUpdated(catalog=rpc_skill_catalog_snapshot(agent)))

        trust_gate = RpcTrustGate(
            project_context_root,
            write_event=publish_event,
            on_first_trusted=rebuild_agent_for_trusted_project,
            initially_trusted=options.startup_trusted,
        )

        async def render_completion_events(events: tuple[WispEvent, ...]) -> None:
            assert host is not None
            await host._render_event_batch(events)

        coordinator = RpcCoordinator(
            session_state,
            session_grants_reset=approval_policy.reset_session_grants,
            input_closed_handlers=(
                approval_policy.deny_pending_on_input_closed,
                trust_gate.deny_pending_on_input_closed,
            ),
            max_queued_commands=(
                _MAX_QUEUED_RPC_COMMANDS if max_queued_commands is None else max_queued_commands
            ),
            input_closed_type=_RpcInputClosed,
            command_completed_type=_RpcCommandCompleted,
            completion_event_writer=publish_event,
            completion_event_renderer=render_completion_events,
            queued_message_rejection=CodingSession.queued_message_rejection,
        )
        host = cls(
            runtime=runtime,
            sessions=sessions,
            agent=agent,
            approval_policy=approval_policy,
            trust_gate=trust_gate,
            configure_overrides=configure_overrides,
            coordinator=coordinator,
            write_event=write_event,
            render_events=render_events,
            on_shutdown_dispatched=on_shutdown_dispatched,
            on_shutdown_abandoned=on_shutdown_abandoned,
            outbound_frame_limit=outbound_frame_limit,
            tool_selection=RpcToolSelection(
                all_tools=options.all_tools,
                allow_read_tools=options.allow_read_tools,
                allowed_tools=options.allowed_tools,
            ),
        )
        for event in runtime.startup_events:
            write_event(event)
        for notice in _broad_root_notices(project_context_root):
            write_event(ErrorEvent(message=notice))
        write_event(await rpc_workspace_report(cwd))
        return host

    async def run_with_streams(
        self,
        receive: RpcControlReceiver,
        *,
        send: MemoryObjectSendStream[_RpcControlEvent],
        task_group: TaskGroup,
    ) -> bool:
        """Serve a bidirectional control stream until it is closed or shut down."""

        async def dispatch(
            command: ParsedRpcCommand,
            running_command: _RpcRunningCommand | None,
        ) -> _RpcDispatchResult:
            buffered_events: list[WispEvent] = []
            capturing = True

            def write_event(event: WispEvent) -> None:
                if capturing:
                    buffered_events.append(event)
                else:
                    self._publish_event(event)

            start_gate = anyio.Event()
            after_flush: list[Callable[[], None]] = []
            executor = RpcCommandExecutor(
                agent=self.agent,
                runtime=self.runtime,
                sessions=self.sessions,
                session_state=self.coordinator.session_state,
                task_group=cast(TaskGroup, _StartGatedTaskGroup(task_group, start_gate)),
                send=send,
                approval_policy=self.approval_policy,
                trust_gate=self.trust_gate,
                configure_overrides=self.configure_overrides,
                coordinator=self.coordinator,
                project_files=self.project_files,
                publish_project_files=self._publish_project_files,
                mcp_servers=self.mcp_servers,
                write_event=write_event,
                render_events=self._render_event_stream,
                defer_until_after_flush=after_flush.append,
                outbound_frame_limit=self._outbound_frame_limit,
            )
            try:
                result = await executor.dispatch_parsed(command, running_command)
                if result.should_shutdown and self._on_shutdown_dispatched is not None:
                    self._on_shutdown_dispatched()
                shutdown_abandoned = any(
                    isinstance(event, RpcCommandFinished)
                    and event.command_type == "shutdown"
                    and not event.ok
                    for event in buffered_events
                )
                if buffered_events:
                    await self._render_event_batch(tuple(buffered_events))
                    buffered_events.clear()
                capturing = False
                if shutdown_abandoned and self._on_shutdown_abandoned is not None:
                    self._on_shutdown_abandoned()
                for release in after_flush:
                    release()
                return result
            finally:
                start_gate.set()

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            buffered_events: list[WispEvent] = []
            executor = RpcCommandExecutor(
                agent=self.agent,
                runtime=self.runtime,
                sessions=self.sessions,
                session_state=self.coordinator.session_state,
                task_group=task_group,
                send=send,
                approval_policy=self.approval_policy,
                trust_gate=self.trust_gate,
                configure_overrides=self.configure_overrides,
                coordinator=self.coordinator,
                project_files=self.project_files,
                publish_project_files=self._publish_project_files,
                write_event=buffered_events.append,
                render_events=self._render_event_stream,
            )
            executor.reject_parsed(command, message)
            if buffered_events:
                await self._render_event_batch(tuple(buffered_events))
                buffered_events.clear()
            if command.command_type == "shutdown" and self._on_shutdown_abandoned is not None:
                self._on_shutdown_abandoned()

        previous_event_task_group = self._event_task_group
        self._event_task_group = task_group
        try:
            return await self.coordinator.run(
                receive,
                dispatch=dispatch,
                reject=reject,
            )
        finally:
            await self._wait_for_published_events()
            self._event_task_group = previous_event_task_group

    def _publish_event(self, event: WispEvent) -> None:
        task_group = self._event_task_group
        if task_group is None:
            self._write_event(event)
            return
        if self._pending_published_events == 0:
            self._published_events_drained = anyio.Event()
        self._pending_published_events += 1
        try:
            task_group.start_soon(self._render_published_event, event)
        except BaseException:
            self._published_event_finished()
            raise

    async def _render_published_event(self, event: WispEvent) -> None:
        try:
            await self._render_event(event)
        finally:
            self._published_event_finished()

    async def _wait_for_published_events(self) -> None:
        while self._pending_published_events:
            await self._published_events_drained.wait()

    def _published_event_finished(self) -> None:
        self._pending_published_events -= 1
        if self._pending_published_events == 0:
            self._published_events_drained.set()

    async def _publish_project_files(
        self, report: RpcProjectFilesReported | RpcGitDiffReported, scope: anyio.CancelScope
    ) -> bool:
        """Commit only a current result, serialized against policy invalidation."""

        async with self._event_render_lock:
            if scope.cancel_called or not self.project_files.is_current(report.generation):
                return False

            async def events() -> AsyncIterator[WispEvent]:
                yield report

            # Publication commits here. Once bytes can reach the client, finish
            # this report even if cancellation arrives while output is blocked.
            with anyio.CancelScope(shield=True):
                await self._render_events(events())
            return True

    async def _render_event_stream(self, events: AsyncIterator[WispEvent]) -> None:
        async def serialized_events() -> AsyncIterator[WispEvent]:
            async for event in events:
                with anyio.CancelScope(shield=True):
                    await self._event_render_lock.acquire()
                try:
                    yield event
                finally:
                    self._event_render_lock.release()

        await self._render_events(serialized_events())

    async def _render_event(self, event: WispEvent) -> None:
        await self._render_event_batch((event,))

    async def _render_event_batch(self, batch: tuple[WispEvent, ...]) -> None:
        async def events() -> AsyncIterator[WispEvent]:
            for event in batch:
                yield event

        async with self._event_render_lock:
            await self._render_events(events())


async def build_runtime_for_config(
    config: WispConfig,
    *,
    anthropic_cache_ttl: AnthropicCacheTtl = "5m",
) -> WispRuntime:
    """Build a runtime from the configuration's auth, retry, timeout, MCP, and endpoint settings.

    Args:
        config (WispConfig): Runtime configuration.
        anthropic_cache_ttl (AnthropicCacheTtl, optional): Anthropic prompt-cache
            lifetime. Sessions hosted by `RpcHost` use `build_session_runtime`,
            which passes "1h". Defaults to "5m" for one-off runs.

    Returns:
        WispRuntime: Runtime with built-in extensions and configured MCP servers.
    """

    return await build_runtime(
        auth_path=config.auth_path,
        retry_policy=config.retry_policy,
        codex_read_timeout_seconds=config.codex_read_timeout_seconds,
        mcp_servers=config.mcp_servers,
        openai_compatible=config.openai_compatible,
        anthropic_cache_ttl=anthropic_cache_ttl,
    )


async def build_session_runtime(config: WispConfig) -> WispRuntime:
    """Build the runtime for a session hosted by `RpcHost`, with a 1-hour Anthropic cache.

    Hosted sessions (the JSONL-RPC backend, which the TUI drives, and the
    in-process SDK) stay open between prompts, and the next prompt often arrives
    more than five minutes later, after a 5-minute cache entry would have
    expired. `RpcHost` uses this builder by default for trust-time and
    configuration rebuilds, so they keep the same lifetime.

    Args:
        config (WispConfig): Runtime configuration.

    Returns:
        WispRuntime: Runtime with built-in extensions and configured MCP servers.
    """

    return await build_runtime_for_config(config, anthropic_cache_ttl="1h")


__all__ = ["InProcessOptions", "RpcHost", "build_runtime_for_config", "build_session_runtime"]
