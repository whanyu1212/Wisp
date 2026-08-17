"""Transport-independent host for Wisp's long-lived command contract.

Both JSONL-RPC and in-process embeddings submit the same typed commands to this
host.  It owns command scheduling, durable-session selection, approvals, trust,
and configuration transitions; transports only adapt input and events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.prompt import resolve_project_context_root
from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.config import WispConfig
from wisp.events import (
    RpcCommandFinished,
    SkillCatalogUpdated,
    TrustRequested,
    TrustResolved,
    WispEvent,
)
from wisp.rpc.commands import ApprovalScope
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
    rpc_command_type,
    rpc_session_state,
    rpc_skill_catalog_snapshot,
)
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.skills.lifecycle import discover_skill_catalog
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.base import Tool, ToolSafety
from wisp.tools.selection import select_session, select_tools, tool_approval_policy
from wisp.trust import is_trusted, record_trust, trust_override_from_env

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


class RpcToolApprovalPolicy(ToolApprovalPolicy):
    """Approval policy resolved by typed ``approval`` commands."""

    def __init__(self, fallback: ToolApprovalPolicy) -> None:
        super().__init__(
            approved_tools=fallback.approved_tools,
            approved_safety=fallback.approved_safety,
        )
        self._pending: dict[str, _PendingApproval] = {}
        self._input_closed_reason: str | None = None

    def prepare_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> None:
        del arguments
        if self.approves(tool):
            return
        pending = _PendingApproval(
            call_id=call_id,
            tool_name=tool.name,
            tool_safety=tool.safety,
            event=anyio.Event(),
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
        if self.approves(tool):
            return ToolApprovalDecision(approved=True)
        pending = self._pending.get(call_id)
        if pending is None:
            return ToolApprovalDecision(approved=False, reason=self.block_reason(tool))
        try:
            await pending.event.wait()
            approved = pending.approved is True
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
        if approved and scope == "tool_session":
            self.approved_tools = self.approved_tools | {pending.tool_name}
        elif approved and scope == "all_session":
            self.approved_safety = self.approved_safety | {"mutating", "command"}
        self._resolve_pending(pending, approved=approved, reason=reason)
        return True

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
        if trusted:
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
    ) -> None:
        self.runtime = runtime
        self.sessions = sessions
        self.agent = agent
        self.approval_policy = approval_policy
        self.trust_gate = trust_gate
        self.configure_overrides = configure_overrides
        self.coordinator = coordinator
        self._write_event = write_event
        self._render_events = render_events
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
        max_queued_commands: int = _MAX_QUEUED_RPC_COMMANDS,
        on_shutdown_dispatched: Callable[[], None] | None = None,
        on_shutdown_abandoned: Callable[[], None] | None = None,
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
        approval_policy = RpcToolApprovalPolicy(tool_approval_policy(options.approve_unsafe_tools))
        configure_overrides = _RpcConfigureOverrides()
        selected_runtime_builder = runtime_builder or build_runtime_for_config
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
            event = await project_configuration.apply_trusted_project(runtime=runtime, agent=agent)
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
            input_closed_handlers=(
                approval_policy.deny_pending_on_input_closed,
                trust_gate.deny_pending_on_input_closed,
            ),
            max_queued_commands=max_queued_commands,
            input_closed_type=_RpcInputClosed,
            command_completed_type=_RpcCommandCompleted,
            completion_event_writer=publish_event,
            completion_event_renderer=render_completion_events,
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
        )
        for event in runtime.startup_events:
            write_event(event)
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
            command: dict[str, object],
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
                write_event=write_event,
                render_events=self._render_event_stream,
                defer_until_after_flush=after_flush.append,
            )
            try:
                result = await executor.dispatch_async(command, running_command)
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

        async def reject(command: dict[str, object], message: str) -> None:
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
                write_event=buffered_events.append,
                render_events=self._render_event_stream,
            )
            executor.reject(command, message)
            if buffered_events:
                await self._render_event_batch(tuple(buffered_events))
                buffered_events.clear()
            if rpc_command_type(command) == "shutdown" and self._on_shutdown_abandoned is not None:
                self._on_shutdown_abandoned()

        previous_event_task_group = self._event_task_group
        self._event_task_group = task_group
        try:
            return await self.coordinator.run_async(
                receive,
                dispatch=dispatch,
                reject=reject,
                command_type=rpc_command_type,
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


async def build_runtime_for_config(config: WispConfig) -> WispRuntime:
    """Build a runtime from configuration while retaining factory compatibility."""

    for kwargs in (
        {
            "auth_path": config.auth_path,
            "retry_policy": config.retry_policy,
            "mcp_servers": config.mcp_servers,
            "openai_compatible": config.openai_compatible,
        },
        {
            "auth_path": config.auth_path,
            "retry_policy": config.retry_policy,
            "openai_compatible": config.openai_compatible,
        },
        {
            "auth_path": config.auth_path,
            "retry_policy": config.retry_policy,
            "mcp_servers": config.mcp_servers,
        },
        {"auth_path": config.auth_path, "retry_policy": config.retry_policy},
        {"auth_path": config.auth_path},
        {},
    ):
        try:
            return await build_runtime(**kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc) or not kwargs:
                raise
    raise AssertionError("runtime factory compatibility loop exhausted")


__all__ = ["InProcessOptions", "RpcHost", "build_runtime_for_config"]
