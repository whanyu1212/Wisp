"""TUI shell and controller-facing event loop."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from rich.console import Console

from wisp.agent.mode import AgentMode
from wisp.auth.connections import (
    ConnectionMethodStatus,
    ConnectionProviderStatus,
)
from wisp.auth.connections import (
    connection_catalog as local_connection_catalog,
)
from wisp.auth.storage import (
    AuthCredential,
    JsonAuthStore,
)
from wisp.config import DEFAULT_PROVIDER, default_auth_path
from wisp.events import (
    CompactionCompleted,
    ContextEstimated,
    ErrorEvent,
    KnownWispEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ModelProviderAutoSwitched,
    ProjectConfigApplied,
    ProviderRetrying,
    QueueItemsRemoved,
    QueueKind,
    QueueMessageInjected,
    QueueUpdated,
    RpcCommandFinished,
    RpcCommandsReported,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcDeviceCodeProgressReported,
    RpcDeviceCodeReported,
    RpcMcpStatusReported,
    RpcMessageSnapshot,
    RpcMessagesReported,
    RpcModelCatalogReported,
    RpcModelCatalogSnapshot,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSkillCatalogSnapshot,
    RpcSkillsReported,
    SessionSaved,
    SessionStatsReported,
    SkillCatalogUpdated,
    SkillInvoked,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.rpc.commands import ApprovalScope
from wisp.settings import persist_user_model_selection
from wisp.tui.auth_commands import AuthCommands
from wisp.tui.commands import (
    DEFAULT_TUI_COMMAND_CATALOG,
    MODEL_COMMAND_CLEAR_EFFORT_TOKEN,
    TuiCommandCatalog,
    TuiSlashCommand,
    TuiSlashCommandError,
    TuiSlashCommandName,
    parse_tui_slash_command,
)
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    TUI_HISTORY_PAGE_LIMIT,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    HistoryHydrationPolicy,
    history_entries_from_rpc_messages,
    history_from_rpc_messages,
    represented_history_entry_ids,
)
from wisp.tui.input_types import PendingSubmissionView, TuiSubmission, new_submission_id
from wisp.tui.launch import _stdin_is_interactive
from wisp.tui.live import LiveFullscreenInputInterrupted
from wisp.tui.mcp import mcp_status_text
from wisp.tui.rendering import (
    TuiRenderer,
    TuiRendererKind,
    create_tui_renderer,
)
from wisp.tui.state import (
    TuiCancelRequested,
    TuiExitReason,
    TuiInteractionState,
    TuiQueueRestoreRequested,
    TuiQuitRequested,
    TuiStatus,
    TuiViewState,
    _coerce_input_mode,
    _input_mode_for_status,
    _InputCancelled,
    _InputClosed,
    _InputInterrupted,
    _InputLine,
    _InputMode,
    _prompt_for_mode,
    _prompt_for_status,
    _QueueRestoreRequested,
    _QuitPressed,
    _RpcEvent,
    _RpcEventsClosed,
    _TuiSignal,
    _UpdateCheckCompleted,
    _UpdateOperationFinished,
    _view_status_for_status,
)
from wisp.tui.update_commands import UpdateCommands, UpdateInstaller, UpdateStatusChecker
from wisp.tui.update_types import UpdatePromptAction
from wisp.update_check import (
    UpdateAvailable,
    can_install_update,
    get_update_status,
    install_update,
    skip_update_version,
)


class TuiController(Protocol):
    """Controller surface consumed by the TUI shell."""

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str: ...

    async def init(self, *, command_id: str | None = None) -> str: ...

    async def compact(
        self,
        instructions: str | None = None,
        *,
        command_id: str | None = None,
    ) -> str: ...

    async def get_session_stats(self, *, command_id: str | None = None) -> str: ...

    async def get_commands(self, *, command_id: str | None = None) -> str: ...

    async def get_model_catalog(self, *, command_id: str | None = None) -> str: ...

    async def get_connection_catalog(self, *, command_id: str | None = None) -> str: ...

    async def store_api_key(
        self,
        provider: str,
        api_key: str,
        *,
        command_id: str | None = None,
    ) -> str: ...

    async def disconnect_provider(self, provider: str, *, command_id: str | None = None) -> str: ...

    async def begin_device_code(self, provider: str, *, command_id: str | None = None) -> str: ...

    async def get_skills(self, *, command_id: str | None = None) -> str: ...

    async def get_mcp_status(self, *, command_id: str | None = None) -> str: ...

    async def get_messages(
        self,
        *,
        session_id: str | None = None,
        limit: int = 200,
        before_entry_id: str | None = None,
        after_entry_id: str | None = None,
        entry_ids: tuple[str, ...] = (),
        complete_structure: bool = False,
        full_content: bool = False,
        allow_during_prompt: bool = False,
        command_id: str | None = None,
    ) -> str: ...

    async def get_sessions(self, *, limit: int = 50, command_id: str | None = None) -> str: ...

    async def new_session(self, *, command_id: str | None = None) -> str: ...

    async def select_session(self, session_id: str, *, command_id: str | None = None) -> str: ...

    async def steer(self, content: str, *, command_id: str | None = None) -> str: ...

    async def follow_up(self, content: str, *, command_id: str | None = None) -> str: ...

    async def get_queue_state(self, *, command_id: str | None = None) -> str: ...

    async def pop_queue(self, kind: QueueKind, *, command_id: str | None = None) -> str: ...

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str: ...

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str: ...

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str: ...

    async def shutdown(self, *, command_id: str | None = None) -> str: ...

    async def configure(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear_effort: bool = False,
        auto_compaction_enabled: bool | None = None,
        mode: AgentMode | None = None,
        command_id: str | None = None,
    ) -> str: ...

    def events(self) -> AsyncIterator[KnownWispEvent]: ...

    async def close(self) -> None: ...


PromptReader = Callable[[str], Awaitable[str]]
UpdateChecker = Callable[[], Awaitable[UpdateAvailable | None]]
UpdateCapabilityChecker = Callable[[], Awaitable[bool]]
UpdateSkipWriter = Callable[..., Awaitable[bool]]
_TRUST_ANSWERS = {"y", "yes", "n", "no"}
_MODEL_PICKER_HIDDEN_PROVIDERS = frozenset({"fake"})


@dataclass(frozen=True)
class _PendingConfigure:
    command_id: str
    provider: str | None = None
    model: str | None = None
    reset_model: bool = False
    effort: str | None = None
    has_effort: bool = False
    auto_compaction_enabled: bool | None = None
    has_auto_compaction_enabled: bool = False
    mode: AgentMode | None = None
    catalog: RpcModelCatalogSnapshot | None = None
    completion: RpcCommandFinished | None = None


@dataclass
class _PendingQueueSubmission:
    command_id: str
    kind: QueueKind
    submission: TuiSubmission


@dataclass
class _PendingQueueRestore:
    command_id: str
    kind: QueueKind
    submission: TuiSubmission
    removed: QueueItemsRemoved | None = None


@dataclass
class _PendingSessionCatalog:
    command_id: str
    report: RpcSessionsReported | None = None


@dataclass
class _PendingHistoryDetail:
    """One exact persisted-row lookup requested by a mounted history card."""

    entry_id: str
    session_id: str
    report: RpcMessagesReported | None = None
    error: str | None = None


@dataclass
class _CompleteHistoryHydration:
    """Validated newest-to-oldest RPC pages awaiting one chronological render."""

    expected_session_id: str | None
    newest_first_pages: list[tuple[RpcMessageSnapshot, ...]] = field(default_factory=list)
    seen_before_entry_ids: set[str] = field(default_factory=set)
    seen_entry_ids: set[str] = field(default_factory=set)
    loaded_message_count: int = 0

    def add(self, report: RpcMessagesReported) -> None:
        if self.newest_first_pages:
            if report.session_id != self.expected_session_id:
                raise ValueError("session history page did not match the active session")
        else:
            if (
                self.expected_session_id is not None
                and report.session_id != self.expected_session_id
            ):
                raise ValueError("session history page did not match the active session")
            self.expected_session_id = report.session_id
        cursor = report.next_before_entry_id
        if cursor is not None and cursor in self.seen_before_entry_ids:
            raise ValueError("session history pagination repeated an older-page cursor")
        if cursor is not None:
            self.seen_before_entry_ids.add(cursor)
        duplicate_entry_id = next(
            (
                message.entry_id
                for message in report.messages
                if message.entry_id in self.seen_entry_ids
            ),
            None,
        )
        if duplicate_entry_id is not None:
            raise ValueError(
                f"session history pagination repeated message entry {duplicate_entry_id}"
            )
        self.seen_entry_ids.update(message.entry_id for message in report.messages)
        self.newest_first_pages.append(report.messages)
        self.loaded_message_count += len(report.messages)

    @property
    def messages(self) -> tuple[RpcMessageSnapshot, ...]:
        return tuple(message for page in reversed(self.newest_first_pages) for message in page)


@dataclass
class _PendingSessionSwitch:
    requested_session_id: str
    select_command_id: str
    selected: RpcSessionSelected | None = None
    history_command_id: str | None = None
    history_report: RpcMessagesReported | None = None
    complete_history: _CompleteHistoryHydration | None = None


@dataclass
class _PendingConnectionMutation:
    command_id: str
    command_type: str
    provider: str
    wait: bool = False
    report: RpcConnectionCatalogReported | None = None
    device_code: RpcDeviceCodeReported | None = None
    progress_attempt: int = 0
    completion: RpcCommandFinished | None = None
    error: str | None = None
    done: anyio.Event = field(default_factory=anyio.Event)


@dataclass
class _HistoryPagination:
    """Cursor and in-flight command state for one mounted transcript history."""

    session_id: str | None
    next_before_entry_id: str | None
    command_id: str | None = None
    report: RpcMessagesReported | None = None
    latest_command_id: str | None = None
    latest_report: RpcMessagesReported | None = None
    newer_command_id: str | None = None
    newer_report: RpcMessagesReported | None = None
    latest_reload_pending: bool = False


class TuiShell:
    """Small prompt/event shell that drives Wisp through `RpcController`."""

    def __init__(
        self,
        controller: TuiController,
        *,
        console: Console | None = None,
        prompt_reader: PromptReader | None = None,
        state: TuiInteractionState | None = None,
        renderer: TuiRenderer | None = None,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        effort: str | None = None,
        auth_path: Path | None = None,
        openai_compatible_provider: str | None = None,
        settings_home_dir: Path | None = None,
        update_checker: UpdateChecker | None = None,
        update_capability_checker: UpdateCapabilityChecker = can_install_update,
        update_skip_writer: UpdateSkipWriter = skip_update_version,
        manual_update_checker: UpdateStatusChecker = get_update_status,
        update_installer: UpdateInstaller = install_update,
        quit_press_window: float = 1.5,
        command_id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.controller = controller
        self.renderer = (
            renderer if renderer is not None else create_tui_renderer(TuiRendererKind.line, console)
        )
        self.prompt_reader = prompt_reader or _default_prompt_reader
        self.state = state or TuiInteractionState()
        self._quit_press_window = quit_press_window
        self._quit_armed_at: float | None = None
        self._command_id_factory = command_id_factory
        self._clock = clock or time.monotonic
        self.view = TuiViewState(provider=provider, model=model)
        self.current_provider = provider
        self.current_mode: AgentMode = "build"
        self.current_model = model
        self.command_catalog = DEFAULT_TUI_COMMAND_CATALOG
        self.skill_catalog = RpcSkillCatalogSnapshot()
        self._queue_steering: tuple[TuiSubmission, ...] = ()
        self._queue_follow_up: tuple[TuiSubmission, ...] = ()
        self._pending_queue_submissions: dict[str, _PendingQueueSubmission] = {}
        self._local_queue_submissions: list[tuple[QueueKind, TuiSubmission]] = []
        self._pending_queue_restore: _PendingQueueRestore | None = None
        self.model_catalog: RpcModelCatalogSnapshot | None = None
        self.connection_catalog: tuple[ConnectionProviderStatus, ...] | None = None
        self.current_effort = effort
        self._pending_model_catalog_command_id: str | None = None
        self._pending_model_catalog_report: RpcModelCatalogReported | None = None
        self._pending_model_catalog_completion: RpcCommandFinished | None = None
        self._pending_connection_catalog_command_id: str | None = None
        self._pending_connection_catalog_report: RpcConnectionCatalogReported | None = None
        self._pending_connection_catalog_completion: RpcCommandFinished | None = None
        self._pending_connection_catalog_done: anyio.Event | None = None
        self._pending_connection_mutations: dict[str, _PendingConnectionMutation] = {}
        self._connection_catalog_error: str | None = None
        self.pending_configures: dict[str, _PendingConfigure] = {}
        self.pending_context_status_command_id: str | None = None
        self.pending_context_status_received = False
        self._pending_session_stats_command_ids: set[str] = set()
        self._ignored_session_stats_command_ids: set[str] = set()
        self.pending_session_catalog: _PendingSessionCatalog | None = None
        self.pending_session_switch: _PendingSessionSwitch | None = None
        self.pending_new_session_command_id: str | None = None
        self._history_pagination: _HistoryPagination | None = None
        self._history_recovery_command_id: str | None = None
        self._history_recovery_report_received = False
        self._ignored_history_page_commands: set[str] = set()
        self._pending_history_details: dict[str, _PendingHistoryDetail] = {}
        self._history_detail_commands_by_entry_id: dict[str, str] = {}
        self._active_history_session_id: str | None = None
        hydration_policy = getattr(
            self.renderer,
            "history_hydration_policy",
            HistoryHydrationPolicy.LATEST_PAGE,
        )
        self._history_hydration_policy = (
            hydration_policy
            if isinstance(hydration_policy, HistoryHydrationPolicy)
            else HistoryHydrationPolicy.LATEST_PAGE
        )
        paginating_renderer = callable(
            getattr(self.renderer, "set_history_page_request_hook", None)
        )
        self._history_message_limit = (
            TUI_HISTORY_PAGE_LIMIT if paginating_renderer else TUI_HISTORY_MESSAGE_LIMIT
        )
        self._call_renderer_optional(
            "set_history_page_request_hook",
            self._request_previous_history_page,
        )
        self._call_renderer_optional(
            "set_history_latest_request_hook",
            self._request_latest_history_page,
        )
        self._call_renderer_optional(
            "set_history_newer_page_request_hook",
            self._request_newer_history_page,
        )
        self._call_renderer_optional(
            "set_history_detail_request_hook",
            self._request_history_detail,
        )
        self.auth_store = JsonAuthStore(auth_path or default_auth_path())
        # Overrides ~/.wisp for model-selection persistence in tests; None in
        # production resolves the real home directory.
        self._settings_home_dir = settings_home_dir
        self._update_checker = update_checker
        self._update_capability_checker = update_capability_checker
        self._update_skip_writer = update_skip_writer
        self._updates = UpdateCommands(
            self.renderer,
            checker=manual_update_checker,
            installer=update_installer,
        )
        self._task_group: anyio.abc.TaskGroup | None = None
        self._connect_cancel_scope: anyio.CancelScope | None = None
        self._update_cancel_scope: anyio.CancelScope | None = None
        self._signal_send: MemoryObjectSendStream[_TuiSignal] | None = None
        self._exit_reason = TuiExitReason.exited
        self._auth = AuthCommands(
            self.renderer,
            self._current_connection_catalog,
            self._default_auth_provider,
            store_api_key=self._store_api_key,
            disconnect_provider=self._disconnect_provider,
            begin_device_code=self._begin_device_code,
            openai_compatible_provider=openai_compatible_provider,
        )
        self._call_renderer_optional(
            "set_connect_api_key_hook",
            self._auth.connect_api_key,
        )
        self._call_renderer_optional(
            "set_connect_oauth_hook",
            self._auth.connect_oauth,
        )
        self._call_renderer_optional(
            "set_connect_cancel_hook",
            lambda: self._cancel_connect("Provider connection cancelled."),
        )
        self._call_renderer_optional(
            "set_update_action_hook",
            self._handle_update_prompt_action,
        )

    def _next_command_id(self, prefix: str) -> str:
        if self._command_id_factory is not None:
            return self._command_id_factory(prefix)
        return f"{prefix}-{uuid4().hex}"

    async def run(self) -> TuiExitReason:
        """Run the interactive prompt/event loop."""

        self.view.input_ready = False
        self.renderer.startup()
        self._sync_view()
        send, receive = anyio.create_memory_object_stream[_TuiSignal](100)
        async with anyio.create_task_group() as task_group, send, receive:
            self._task_group = task_group
            self._signal_send = send
            try:
                task_group.start_soon(self._read_rpc_events, send.clone())
                if await self._hydrate_session_history(receive):
                    task_group.cancel_scope.cancel()
                    return self._exit_reason
                if await self._hydrate_model_catalog(receive):
                    task_group.cancel_scope.cancel()
                    return self._exit_reason
                await self._request_connection_catalog()
                if await self._hydrate_command_catalog(receive):
                    task_group.cancel_scope.cancel()
                    return self._exit_reason
                if await self._hydrate_skill_catalog(receive):
                    task_group.cancel_scope.cancel()
                    return self._exit_reason
                await self._request_queue_state()
                await self._request_session_stats()
                self.view.input_ready = True
                self._sync_view()
                if self._update_checker is not None:
                    task_group.start_soon(self._check_for_update, send.clone())
                task_group.start_soon(self._read_inputs, send.clone())
                while True:
                    signal = await receive.receive()
                    should_exit = await self._handle_signal(signal)
                    if should_exit:
                        task_group.cancel_scope.cancel()
                        return self._exit_reason
            finally:
                self._signal_send = None
                self._task_group = None
        return self._exit_reason

    async def _check_for_update(
        self,
        send: MemoryObjectSendStream[_TuiSignal],
    ) -> None:
        checker = self._update_checker
        if checker is None:
            return
        async with send:
            try:
                update = await checker()
                if update is None:
                    return
                automatic_install = await self._update_capability_checker()
                await send.send(
                    _UpdateCheckCompleted(
                        update=update,
                        automatic_install=automatic_install,
                    )
                )
            except Exception:  # noqa: BLE001 - update checks are optional TUI chrome
                return

    def _sync_view(self) -> None:
        self.view.provider = self.current_provider
        self.view.model = self.current_model
        self.view.mode = self.current_mode
        mode = _input_mode_for_status(self.state.status)
        queued = self._queued_submissions()
        self._update_view(
            status=_view_status_for_status(self.state.status),
            input_hint=_prompt_for_status(self.state.status),
            input_mode=mode,
            queued_steering=len(self._queue_steering),
            queued_follow_ups=len(self._queue_follow_up),
            pending_submissions=tuple(
                submission.pending_view(kind=kind) for kind, submission in queued
            ),
        )

    def _update_view(
        self,
        *,
        status: str | None = None,
        input_hint: str | None = None,
        input_mode: _InputMode | None = None,
        queued_steering: int | None = None,
        queued_follow_ups: int | None = None,
        pending_submissions: tuple[PendingSubmissionView, ...] | None = None,
        last_session: str | None = None,
    ) -> None:
        if status is not None:
            self.view.status = status
        if input_hint is not None:
            self.view.input_hint = input_hint
        if input_mode is not None:
            self.view.input_mode = input_mode.value
        if queued_steering is not None:
            self.view.queued_steering = queued_steering
        if queued_follow_ups is not None:
            self.view.queued_follow_ups = queued_follow_ups
        if pending_submissions is not None:
            self.view.pending_submissions = pending_submissions
        if last_session is not None:
            self.view.last_session = last_session
        self.renderer.view_updated(self.view.snapshot())

    async def _read_inputs(self, send: MemoryObjectSendStream[_TuiSignal]) -> None:
        async with send:
            while True:
                mode = _input_mode_for_status(self.state.status)
                try:
                    text = await self.prompt_reader(_prompt_for_status(self.state.status))
                except EOFError:
                    await send.send(_InputClosed(mode=self._submitted_input_mode(mode)))
                    return
                except TuiQuitRequested as exc:
                    await send.send(
                        _QuitPressed(
                            mode=self._submitted_input_mode(mode),
                            pressed_at=exc.pressed_at,
                        )
                    )
                    continue
                except TuiQueueRestoreRequested:
                    await send.send(_QueueRestoreRequested())
                    continue
                except (TuiCancelRequested, LiveFullscreenInputInterrupted):
                    await send.send(_InputCancelled(mode=self._submitted_input_mode(mode)))
                    continue
                except KeyboardInterrupt:
                    await send.send(_InputInterrupted(mode=self._submitted_input_mode(mode)))
                    continue
                submitted_mode = (
                    _coerce_input_mode(text.input_mode, fallback=mode)
                    if isinstance(text, TuiSubmission)
                    else self._submitted_input_mode(mode)
                )
                await send.send(_InputLine(text=text, mode=submitted_mode))

    def _submitted_input_mode(self, requested_mode: _InputMode) -> _InputMode:
        consume_mode = getattr(self.renderer, "consume_submitted_input_mode", None)
        if callable(consume_mode):
            return _coerce_input_mode(consume_mode(requested_mode.value), fallback=requested_mode)
        return requested_mode

    async def _read_rpc_events(self, send: MemoryObjectSendStream[_TuiSignal]) -> None:
        async with send:
            try:
                async for event in self.controller.events():
                    await send.send(_RpcEvent(event=event))
            except Exception as exc:  # noqa: BLE001 - surface event reader failures in the TUI
                await send.send(_RpcEventsClosed(error=str(exc)))
            else:
                await send.send(_RpcEventsClosed())

    async def _hydrate_session_history(
        self,
        receive: anyio.abc.ObjectReceiveStream[_TuiSignal],
    ) -> bool:
        return await self._hydrate_session_history_pages(receive)

    async def _hydrate_session_history_pages(
        self,
        receive: anyio.abc.ObjectReceiveStream[_TuiSignal],
    ) -> bool:
        command_id = await self._request_session_history()
        if command_id is None:
            return False
        report: RpcMessagesReported | None = None
        rendered = False
        while True:
            signal = await receive.receive()
            if isinstance(signal, _RpcEvent):
                event = signal.event
                if isinstance(event, RpcMessagesReported) and event.command_id == command_id:
                    if report is not None:
                        continue
                    report = event
                    if not rendered:
                        await self._render_history_entries(
                            history_entries_from_rpc_messages(event.messages),
                            text_fallback=history_from_rpc_messages(event.messages),
                        )
                        self._activate_history_pagination(event)
                        rendered = True
                    continue
                should_exit = await self._handle_rpc_event(event)
                if should_exit:
                    return True
                if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                    if not event.ok or report is None:
                        return False
                    return False
                continue
            if isinstance(signal, _RpcEventsClosed):
                return self._handle_rpc_closed(signal, pending_command_id=command_id)
            if await self._handle_signal(signal):
                return True

    async def _hydrate_command_catalog(
        self,
        receive: anyio.abc.ObjectReceiveStream[_TuiSignal],
    ) -> bool:
        """Load executable command metadata before accepting interactive input."""

        try:
            command_id = await self.controller.get_commands()
        except Exception as exc:  # noqa: BLE001 - discovery is optional TUI startup polish
            self.renderer.notice(f"Command discovery unavailable; using built-ins: {exc}")
            self._publish_command_catalog()
            return False

        report: RpcCommandsReported | None = None
        while True:
            signal = await receive.receive()
            if isinstance(signal, _RpcEvent):
                event = signal.event
                if isinstance(event, RpcCommandsReported) and event.command_id == command_id:
                    report = event
                    continue
                if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                    if event.ok and report is not None:
                        try:
                            self.command_catalog = TuiCommandCatalog.from_rpc(report.commands)
                        except ValueError as exc:
                            self.renderer.notice(
                                f"Command discovery was invalid; using built-ins: {exc}"
                            )
                            self._publish_command_catalog()
                        else:
                            self._publish_command_catalog()
                    else:
                        reason = event.error or "command catalog completed without a result"
                        self.renderer.notice(
                            f"Command discovery unavailable; using built-ins: {reason}"
                        )
                        self._publish_command_catalog()
                    return False
                if await self._handle_rpc_event(event):
                    return True
                continue
            if isinstance(signal, _RpcEventsClosed):
                return self._handle_rpc_closed(signal, pending_command_id=command_id)
            if await self._handle_signal(signal):
                return True

    async def _hydrate_model_catalog(
        self,
        receive: anyio.abc.ObjectReceiveStream[_TuiSignal],
    ) -> bool:
        """Load the backend-authoritative model catalog before accepting input."""

        if not await self._request_model_catalog():
            return False
        while self._pending_model_catalog_command_id is not None:
            signal = await receive.receive()
            if isinstance(signal, _RpcEvent):
                if await self._handle_rpc_event(signal.event):
                    return True
                continue
            if isinstance(signal, _RpcEventsClosed):
                return self._handle_rpc_closed(
                    signal,
                    pending_command_id=self._pending_model_catalog_command_id,
                )
            if await self._handle_signal(signal):
                return True
        return False

    async def _request_model_catalog(self) -> bool:
        if self._pending_model_catalog_command_id is not None:
            return True
        try:
            command_id = await self.controller.get_model_catalog()
        except Exception as exc:  # noqa: BLE001 - discovery is non-fatal
            self.renderer.notice(f"Model catalog unavailable: {exc}")
            return False
        self._pending_model_catalog_command_id = command_id
        self._pending_model_catalog_report = None
        self._pending_model_catalog_completion = None
        return True

    def _observe_model_catalog_event(self, event: KnownWispEvent) -> bool:
        command_id = self._pending_model_catalog_command_id
        if command_id is None:
            return False
        if isinstance(event, RpcModelCatalogReported) and event.command_id == command_id:
            if self._pending_model_catalog_report is None:
                self._pending_model_catalog_report = event
        elif isinstance(event, RpcCommandFinished) and event.command_id == command_id:
            if self._pending_model_catalog_completion is None:
                self._pending_model_catalog_completion = event
        else:
            return False

        completion = self._pending_model_catalog_completion
        if completion is not None and not completion.ok:
            self.renderer.notice(
                f"Model catalog unavailable: {completion.error or 'request failed'}"
            )
            self._clear_model_catalog_request()
        elif completion is not None and self._pending_model_catalog_report is not None:
            self._adopt_model_catalog(self._pending_model_catalog_report.catalog)
            self._clear_model_catalog_request()
        return True

    def _adopt_model_catalog(self, catalog: RpcModelCatalogSnapshot) -> None:
        self.model_catalog = catalog
        self.current_provider = catalog.selection.provider
        self.current_model = catalog.selection.model
        self.current_effort = catalog.selection.effort
        self._sync_view()

    def _clear_model_catalog_request(self) -> None:
        self._pending_model_catalog_command_id = None
        self._pending_model_catalog_report = None
        self._pending_model_catalog_completion = None

    async def _hydrate_connection_catalog(
        self,
        receive: anyio.abc.ObjectReceiveStream[_TuiSignal],
    ) -> bool:
        if not await self._request_connection_catalog():
            return False
        while self._pending_connection_catalog_command_id is not None:
            signal = await receive.receive()
            if isinstance(signal, _RpcEvent):
                if await self._handle_rpc_event(signal.event):
                    return True
                continue
            if isinstance(signal, _RpcEventsClosed):
                return self._handle_rpc_closed(
                    signal,
                    pending_command_id=self._pending_connection_catalog_command_id,
                )
            if await self._handle_signal(signal):
                return True
        return False

    async def _request_connection_catalog(self) -> bool:
        if self._pending_connection_catalog_command_id is not None:
            return True
        command_id = self._next_command_id("connection-catalog")
        self._pending_connection_catalog_command_id = command_id
        self._pending_connection_catalog_report = None
        self._pending_connection_catalog_completion = None
        self._pending_connection_catalog_done = anyio.Event()
        try:
            await self.controller.get_connection_catalog(command_id=command_id)
        except Exception as extra:  # noqa: BLE001 - discovery is non-fatal
            self._connection_catalog_error = str(extra)
            self.renderer.notice(f"Connection catalog unavailable: {extra}")
            self.connection_catalog = self._safe_fallback_connection_catalog()
            self._clear_connection_catalog_request()
            return False
        return True

    async def _refresh_connection_catalog_if_needed(self) -> None:
        if self._pending_connection_catalog_command_id is not None:
            pending_done = self._pending_connection_catalog_done
            if pending_done is not None:
                await pending_done.wait()
        if self._connection_catalog_error is None and self.connection_catalog is not None:
            return
        if not await self._request_connection_catalog():
            return
        retry_done = self._pending_connection_catalog_done
        if retry_done is not None:
            await retry_done.wait()

    def _observe_connection_catalog_event(self, event: KnownWispEvent) -> bool:
        command_id = self._pending_connection_catalog_command_id
        if command_id is None:
            return False
        if isinstance(event, RpcConnectionCatalogReported) and event.command_id == command_id:
            if self._pending_connection_catalog_report is None:
                self._pending_connection_catalog_report = event
        elif isinstance(event, RpcCommandFinished) and event.command_id == command_id:
            if self._pending_connection_catalog_completion is None:
                self._pending_connection_catalog_completion = event
        else:
            return False
        completion = self._pending_connection_catalog_completion
        if completion is not None and not completion.ok:
            self._connection_catalog_error = completion.error or "request failed"
            self.renderer.notice(
                f"Connection catalog unavailable: {self._connection_catalog_error}"
            )
            self.connection_catalog = self._safe_fallback_connection_catalog()
            self._clear_connection_catalog_request()
        elif completion is not None and self._pending_connection_catalog_report is not None:
            self._connection_catalog_error = None
            self._adopt_connection_catalog(self._pending_connection_catalog_report.catalog)
            self._clear_connection_catalog_request()
        return True

    def _adopt_connection_catalog(self, catalog: RpcConnectionCatalogSnapshot) -> None:
        self.connection_catalog = tuple(
            ConnectionProviderStatus(
                id=family.id,
                label=family.label,
                methods=tuple(
                    ConnectionMethodStatus(
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
            for family in catalog.providers
        )

    def _clear_connection_catalog_request(self) -> None:
        done = self._pending_connection_catalog_done
        self._pending_connection_catalog_command_id = None
        self._pending_connection_catalog_report = None
        self._pending_connection_catalog_completion = None
        self._pending_connection_catalog_done = None
        if done is not None:
            done.set()

    def _current_connection_catalog(self) -> tuple[ConnectionProviderStatus, ...]:
        if self.connection_catalog is not None:
            return self.connection_catalog
        if self._connection_catalog_error is not None:
            raise RuntimeError(self._connection_catalog_error)
        return self._fallback_connection_catalog()

    def _fallback_connection_catalog(self) -> tuple[ConnectionProviderStatus, ...]:
        empty_store: dict[str, AuthCredential] = {}
        return local_connection_catalog(empty_store, environ=lambda _name: None)

    def _safe_fallback_connection_catalog(
        self,
    ) -> tuple[ConnectionProviderStatus, ...] | None:
        try:
            return self._fallback_connection_catalog()
        except Exception:
            return None

    async def _store_api_key(self, provider: str, api_key: str) -> None:
        command_id = self._next_command_id("store-api-key")
        await self._send_connection_mutation(
            command_id,
            "store_api_key",
            provider,
            wait=True,
            sender=lambda: self.controller.store_api_key(provider, api_key, command_id=command_id),
        )

    async def _disconnect_provider(self, provider: str) -> None:
        command_id = self._next_command_id("disconnect-provider")
        await self._send_connection_mutation(
            command_id,
            "disconnect_provider",
            provider,
            wait=True,
            sender=lambda: self.controller.disconnect_provider(provider, command_id=command_id),
        )

    async def _begin_device_code(self, provider: str) -> None:
        command_id = self._next_command_id("device-code")
        await self._send_connection_mutation(
            command_id,
            "begin_device_code",
            provider,
            wait=True,
            sender=lambda: self.controller.begin_device_code(provider, command_id=command_id),
        )

    async def _send_connection_mutation(
        self,
        command_id: str,
        command_type: str,
        provider: str,
        *,
        wait: bool,
        sender: Callable[[], Awaitable[str]],
    ) -> None:
        pending = _PendingConnectionMutation(
            command_id=command_id,
            command_type=command_type,
            provider=provider,
            wait=wait,
        )
        self._pending_connection_mutations[command_id] = pending
        try:
            await sender()
        except Exception:
            self._pending_connection_mutations.pop(command_id, None)
            pending.done.set()
            raise
        if not wait:
            return
        await pending.done.wait()
        if pending.error is not None:
            raise RuntimeError(pending.error)

    def _observe_connection_mutation_event(self, event: KnownWispEvent) -> bool:
        command_id = getattr(event, "command_id", None)
        if not isinstance(command_id, str):
            return False
        pending = self._pending_connection_mutations.get(command_id)
        if pending is None:
            return False
        if isinstance(event, RpcConnectionCatalogReported):
            pending.report = event
        elif isinstance(event, RpcDeviceCodeReported):
            pending.device_code = event
            self._call_renderer_optional(
                "connect_device_code",
                event.verification_uri,
                event.user_code,
            )
            self.renderer.notice(f"Open {event.verification_uri} and enter code {event.user_code}")
        elif isinstance(event, RpcDeviceCodeProgressReported):
            pending.progress_attempt = event.attempt
            self._call_renderer_optional("connect_progress", event.attempt)
        elif isinstance(event, RpcCommandFinished):
            pending.completion = event
            self._finish_connection_mutation(pending)
        else:
            return False
        return True

    def _finish_connection_mutation(self, pending: _PendingConnectionMutation) -> None:
        completion = pending.completion
        if completion is None:
            return
        if not completion.ok:
            pending.error = completion.error or "request failed"
        elif pending.report is not None:
            self._adopt_connection_catalog(pending.report.catalog)
        if pending.command_type == "begin_device_code" and pending.error is None:
            self._call_renderer_optional("connect_completed", pending.provider)
            self.renderer.notice(f"Connected: {pending.provider}")
        elif pending.error is not None and not pending.wait:
            self.renderer.command_error(pending.error)
        self._pending_connection_mutations.pop(pending.command_id, None)
        pending.done.set()

    def _publish_command_catalog(self) -> None:
        update_catalog = getattr(self.renderer, "command_catalog_updated", None)
        if callable(update_catalog):
            cast(Callable[[TuiCommandCatalog], None], update_catalog)(self.command_catalog)

    async def _hydrate_skill_catalog(
        self,
        receive: anyio.abc.ObjectReceiveStream[_TuiSignal],
    ) -> bool:
        """Load the immutable skill catalog before accepting interactive input."""

        try:
            command_id = await self.controller.get_skills()
        except Exception as exc:  # noqa: BLE001 - discovery is optional TUI startup polish
            self.renderer.notice(f"Skill discovery unavailable: {exc}")
            self._publish_skill_catalog()
            return False

        report: RpcSkillsReported | None = None
        while True:
            signal = await receive.receive()
            if isinstance(signal, _RpcEvent):
                event = signal.event
                if isinstance(event, RpcSkillsReported) and event.command_id == command_id:
                    report = event
                    continue
                if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                    if event.ok and report is not None:
                        self.skill_catalog = report.catalog
                        self._publish_skill_catalog()
                    else:
                        reason = event.error or "skill catalog completed without a result"
                        self.renderer.notice(f"Skill discovery unavailable: {reason}")
                        self._publish_skill_catalog()
                    return False
                if await self._handle_rpc_event(event):
                    return True
                continue
            if isinstance(signal, _RpcEventsClosed):
                return self._handle_rpc_closed(signal, pending_command_id=command_id)
            if await self._handle_signal(signal):
                return True

    def _publish_skill_catalog(self) -> None:
        self._call_renderer_optional("skill_catalog_updated", self.skill_catalog)

    def _render_history(self, messages: tuple[HistoricalTranscriptMessage, ...]) -> None:
        render_history = getattr(self.renderer, "render_history", None)
        if not callable(render_history):
            return
        cast(Callable[[tuple[HistoricalTranscriptMessage, ...]], None], render_history)(messages)

    async def _render_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
        *,
        text_fallback: tuple[HistoricalTranscriptMessage, ...] | None = None,
        session_label: str | None = None,
        complete: bool = False,
    ) -> None:
        hydrate_entries = getattr(self.renderer, "hydrate_history_entries", None)
        if complete and callable(hydrate_entries):
            await cast(Callable[..., Awaitable[None]], hydrate_entries)(
                entries,
                session_label=session_label,
            )
            return
        if session_label is not None:
            self._call_renderer_optional(
                "replace_history_entries",
                entries,
                session_label=session_label,
            )
            return
        render_entries = getattr(self.renderer, "render_history_entries", None)
        if callable(render_entries):
            cast(Callable[[tuple[HistoricalTranscriptEntry, ...]], None], render_entries)(entries)
            return
        if text_fallback is not None:
            self._render_history(text_fallback)

    def _activate_history_pagination(self, report: RpcMessagesReported) -> None:
        self._active_history_session_id = report.session_id
        self._history_pagination = _HistoryPagination(
            session_id=report.session_id,
            next_before_entry_id=report.next_before_entry_id,
        )
        self._call_renderer_optional(
            "history_page_loaded",
            has_more=report.next_before_entry_id is not None,
        )

    def _clear_history_pagination(self) -> None:
        pagination = self._history_pagination
        if pagination is not None:
            for command_id in (
                pagination.command_id,
                pagination.latest_command_id,
                pagination.newer_command_id,
            ):
                if command_id is not None:
                    self._ignored_history_page_commands.add(command_id)
        if self._history_recovery_command_id is not None:
            self._ignored_history_page_commands.add(self._history_recovery_command_id)
        self._history_recovery_command_id = None
        self._history_recovery_report_received = False
        self._history_pagination = None
        self._call_renderer_optional("history_page_loaded", has_more=False)

    def _clear_history_detail_requests(self, reason: str) -> None:
        """Invalidate exact-row requests whose mounted cards are being replaced."""

        for command_id, pending in self._pending_history_details.items():
            self._ignored_history_page_commands.add(command_id)
            self._call_renderer_optional(
                "history_detail_failed",
                pending.entry_id,
                reason,
            )
        self._pending_history_details.clear()
        self._history_detail_commands_by_entry_id.clear()

    async def _request_history_detail(self, entry_id: str) -> None:
        """Load one exact active-path row after the reader expands a process update."""

        session_id = self._active_history_session_id
        if session_id is None:
            self._call_renderer_optional(
                "history_detail_failed",
                entry_id,
                "No resumed session is active.",
            )
            return
        if entry_id in self._history_detail_commands_by_entry_id:
            return
        command_id = self._next_command_id("history-detail")
        pending = _PendingHistoryDetail(entry_id=entry_id, session_id=session_id)
        self._pending_history_details[command_id] = pending
        self._history_detail_commands_by_entry_id[entry_id] = command_id
        try:
            await self.controller.get_messages(
                session_id=session_id,
                limit=1,
                entry_ids=(entry_id,),
                complete_structure=True,
                full_content=True,
                allow_during_prompt=True,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - card-local lookup remains retryable
            self._pending_history_details.pop(command_id, None)
            self._history_detail_commands_by_entry_id.pop(entry_id, None)
            self._call_renderer_optional(
                "history_detail_failed",
                entry_id,
                f"Failed to load persisted output: {exc}",
            )

    async def _handle_signal(self, signal: _TuiSignal) -> bool:
        if isinstance(signal, _UpdateCheckCompleted):
            present = getattr(self.renderer, "update_available", None)
            if callable(present):
                present(signal.update, automatic_install=signal.automatic_install)
            else:
                guidance = (
                    f"Update with: {signal.update.update_command}"
                    if signal.automatic_install
                    else "Update it with the package manager that installed Wisp."
                )
                self.renderer.notice(
                    f"Wisp {signal.update.latest_version} is available "
                    f"(current {signal.update.current_version}). {guidance}"
                )
            return False
        if isinstance(signal, _UpdateOperationFinished):
            restarting = (
                signal.installed and signal.restart_requested and not self.state.exit_requested
            )
            self._call_renderer_optional(
                "update_operation_finished",
                installed=signal.installed,
                restarting=restarting,
            )
            if not signal.installed or not signal.restart_requested:
                return False
            if restarting:
                self._exit_reason = TuiExitReason.restart_requested
            return await self._request_shutdown()
        if isinstance(signal, _InputLine):
            return await self._handle_input_line(signal)
        if isinstance(signal, _InputClosed):
            return await self._handle_input_closed(signal)
        if isinstance(signal, _QuitPressed):
            return await self._handle_quit_pressed(signal)
        if isinstance(signal, _QueueRestoreRequested):
            await self._restore_latest_queue_item()
            return False
        if isinstance(signal, _InputCancelled):
            return await self._handle_input_cancelled(signal)
        if isinstance(signal, _InputInterrupted):
            return await self._handle_input_interrupted(signal)
        if isinstance(signal, _RpcEvent):
            return await self._handle_rpc_event(signal.event)
        return self._handle_rpc_closed(signal)

    def _submission(self, value: str, mode: _InputMode) -> TuiSubmission:
        if isinstance(value, TuiSubmission):
            return value
        return TuiSubmission(
            id=new_submission_id(),
            content=value,
            display=value,
            input_mode=mode.value,
        )

    def _queued_submissions(self) -> tuple[tuple[QueueKind, TuiSubmission], ...]:
        return tuple(
            [("steering", item) for item in self._queue_steering]
            + [("follow_up", item) for item in self._queue_follow_up]
        )

    def _sync_pending_view(self) -> None:
        submissions = self._queued_submissions()
        self._update_view(
            queued_steering=len(self._queue_steering),
            queued_follow_ups=len(self._queue_follow_up),
            pending_submissions=tuple(item.pending_view(kind=kind) for kind, item in submissions),
        )

    def _resolve_submission(self, submission: TuiSubmission) -> None:
        self._call_renderer_optional("resolve_submission", int(submission.id))

    def _restore_submissions(self, submissions: tuple[TuiSubmission, ...]) -> None:
        if not submissions:
            return
        restored = self._call_renderer_optional("restore_submissions", submissions)
        if restored is not True:
            for submission in submissions:
                self._resolve_submission(submission)
            self._call_renderer_optional("report_unsent_submissions", submissions)

    def _clear_queued_prompts(self) -> None:
        """Compatibility seam retained for callers; runtime queues remain authoritative."""

        self.state.queued_prompts.clear()

    def _abandon_queued_prompts(self) -> None:
        """Drop only obsolete shell-local queue state, never runtime-owned messages."""

        self.state.queued_prompts.clear()

    def _report_runtime_queue_before_shutdown(self) -> None:
        """Report runtime-owned queued input before the RPC process exits."""

        queued = self._queued_submissions()
        pending = tuple(item.submission for item in self._pending_queue_submissions.values())
        restoring = (
            (self._pending_queue_restore.submission,)
            if self._pending_queue_restore is not None
            else ()
        )
        if not queued and not pending and not restoring:
            return
        by_id = {int(submission.id): submission for _, submission in queued}
        for submission in (*pending, *restoring):
            by_id.setdefault(int(submission.id), submission)
        submissions = sorted(by_id.values(), key=lambda submission: int(submission.id))
        self._queue_steering = ()
        self._queue_follow_up = ()
        self._local_queue_submissions.clear()
        self._pending_queue_submissions.clear()
        self._pending_queue_restore = None
        self._sync_pending_view()
        for submission in submissions:
            self._resolve_submission(submission)
        self._call_renderer_optional("report_unsent_submissions", tuple(submissions))

    async def _submit_queue_message(
        self,
        submission: TuiSubmission,
        kind: QueueKind,
    ) -> None:
        command_id = self._next_command_id(f"tui-{kind}")
        pending = _PendingQueueSubmission(command_id, kind, submission)
        self._pending_queue_submissions[command_id] = pending
        try:
            if kind == "steering":
                await self.controller.steer(submission.content, command_id=command_id)
            else:
                await self.controller.follow_up(submission.content, command_id=command_id)
        except Exception as exc:  # noqa: BLE001 - restore an unsent composer submission
            self._pending_queue_submissions.pop(command_id, None)
            self._restore_submissions((submission,))
            self.renderer.send_failed("queue submission", exc)

    async def _restore_latest_queue_item(self) -> None:
        if self._pending_queue_restore is not None:
            self.renderer.notice("A queued item is already being restored.")
            return
        local = next(
            (
                (kind, submission)
                for kind, submission in reversed(self._local_queue_submissions)
                if submission in self._queue_for(kind)
            ),
            None,
        )
        if local is None:
            if self._queue_follow_up:
                local = ("follow_up", self._queue_follow_up[-1])
            elif self._queue_steering:
                local = ("steering", self._queue_steering[-1])
        if local is None:
            self.renderer.notice("No queued steering or follow-up to restore.")
            return
        kind, submission = local
        command_id = self._next_command_id("tui-queue-restore")
        self._pending_queue_restore = _PendingQueueRestore(command_id, kind, submission)
        try:
            await self.controller.pop_queue(kind, command_id=command_id)
        except Exception as exc:  # noqa: BLE001 - queue remains intact after send failure
            self._pending_queue_restore = None
            self.renderer.send_failed("restore queued item", exc)

    def _queue_for(self, kind: QueueKind) -> tuple[TuiSubmission, ...]:
        return self._queue_steering if kind == "steering" else self._queue_follow_up

    def _reconcile_queue(
        self,
        kind: QueueKind,
        contents: tuple[str, ...],
    ) -> tuple[TuiSubmission, ...]:
        old = list(self._queue_for(kind))
        if contents and len(contents) < len(old):
            suffix = old[-len(contents) :]
            if tuple(item.content for item in suffix) == contents:
                old = suffix
        candidates = [
            submission
            for candidate_kind, submission in self._local_queue_submissions
            if candidate_kind == kind
        ]
        candidates.extend(
            pending.submission
            for pending in self._pending_queue_submissions.values()
            if pending.kind == kind
        )
        selected: list[TuiSubmission] = []
        used_ids: set[int] = set()
        for content in contents:
            submission = next(
                (
                    item
                    for item in (*old, *candidates)
                    if item.content == content and int(item.id) not in used_ids
                ),
                None,
            )
            if submission is None:
                submission = TuiSubmission(
                    id=new_submission_id(),
                    content=content,
                    display=content,
                    input_mode="running",
                    queue_kind=kind,
                )
            selected.append(submission)
            used_ids.add(int(submission.id))
        return tuple(selected)

    def _apply_queue_update(self, event: QueueUpdated) -> None:
        self._queue_steering = self._reconcile_queue("steering", event.steering)
        self._queue_follow_up = self._reconcile_queue("follow_up", event.follow_up)
        live_ids = {int(item.id) for _, item in self._queued_submissions()}
        self._local_queue_submissions = [
            (kind, submission)
            for kind, submission in self._local_queue_submissions
            if int(submission.id) in live_ids
        ]
        self._sync_pending_view()

    def _record_prompt_acceptance(self, prompt: str) -> None:
        """Notify renderers that implement the optional prompt-history hook."""

        prompt_accepted = getattr(self.renderer, "prompt_accepted", None)
        if callable(prompt_accepted):
            prompt_accepted(prompt)

    async def _handle_input_line(self, signal: _InputLine) -> bool:
        self._disarm_quit()
        submission = self._submission(signal.text, signal.mode)
        text = submission.content
        has_content = bool(text.strip())
        if self.state.status is TuiStatus.exiting:
            self._restore_submissions((submission,))
            return False
        try:
            command = parse_tui_slash_command(text, catalog=self.command_catalog)
        except TuiSlashCommandError as exc:
            self._restore_submissions((submission,))
            self.renderer.command_error(str(exc))
            return False
        if command is not None:
            self._resolve_submission(submission)
            return await self._handle_slash_command(command)
        if self._update_cancel_scope is not None:
            if has_content:
                self.renderer.command_error(
                    "Cannot submit prompts while a Wisp update operation is in progress."
                )
                self._restore_submissions((submission,))
            else:
                self._resolve_submission(submission)
            return False
        if self._connect_cancel_scope is not None:
            if has_content:
                self.renderer.command_error(
                    "Cannot submit prompts while a provider connection is in progress."
                )
                self._restore_submissions((submission,))
            else:
                self._resolve_submission(submission)
            return False
        if self._session_operation_active():
            if has_content:
                self.renderer.command_error(
                    f"Cannot submit prompts while {self._session_operation_name()}."
                )
                self._restore_submissions((submission,))
            else:
                self._resolve_submission(submission)
            return False
        if self.state.pending_trust is not None:
            if signal.mode is _InputMode.trust or _is_trust_answer(text):
                self._resolve_submission(submission)
                return await self._answer_pending_trust(text)
            if has_content and self.state.current_command_id is not None:
                await self._submit_queue_message(
                    submission,
                    "steering" if submission.queue_kind == "steering" else "follow_up",
                )
            else:
                self._resolve_submission(submission)
            return False
        if self.state.pending_approval is not None:
            if signal.mode is _InputMode.approval:
                self._resolve_submission(submission)
                return await self._answer_pending_approval(text, exit_after_denial=False)
            if has_content and self.state.current_command_id is not None:
                await self._submit_queue_message(
                    submission,
                    "steering" if submission.queue_kind == "steering" else "follow_up",
                )
            else:
                self._resolve_submission(submission)
            return False
        if not has_content:
            self._resolve_submission(submission)
            return False
        if self.state.current_command_id is not None:
            if self.state.current_command_type == "compact":
                self.renderer.command_error("Cannot steer or queue a follow-up during compaction.")
                self._restore_submissions((submission,))
                return False
            kind: QueueKind = (
                "follow_up" if submission.queue_kind in {"auto", "follow_up"} else "steering"
            )
            await self._submit_queue_message(submission, kind)
            return False
        self._record_prompt_acceptance(text)
        return await self._start_prompt(submission)

    async def _handle_slash_command(self, command: TuiSlashCommand) -> bool:
        if command.name is TuiSlashCommandName.help:
            self._render_help()
            return False
        if command.name is TuiSlashCommandName.quit:
            return await self._handle_quit()
        if (
            command.name is TuiSlashCommandName.disconnect
            and self._connect_cancel_scope is not None
        ):
            self.renderer.command_error(
                "Cannot disconnect while a provider connection is in progress."
            )
            return False
        if self.state.pending_approval is not None:
            self.renderer.command_error("Cannot run slash commands while approval is pending.")
            return False
        if self.state.pending_trust is not None:
            self.renderer.command_error("Cannot run slash commands while trust is pending.")
            return False
        if command.name is TuiSlashCommandName.history:
            if self._session_operation_active():
                self.renderer.command_error(
                    f"Cannot run slash commands while {self._session_operation_name()}."
                )
                return False
            if command.args:
                self.renderer.command_error("Usage: /history")
                return False
            self.renderer.prompt_history_request()
            return False
        if command.name is TuiSlashCommandName.skills:
            if command.args:
                self.renderer.command_error("Usage: /skills")
                return False
            self._call_renderer_optional("skills_catalog", self.skill_catalog)
            return False
        if command.name is TuiSlashCommandName.mcp:
            if command.args:
                self.renderer.command_error("Usage: /mcp")
                return False
            try:
                await self.controller.get_mcp_status()
            except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
                self.renderer.send_failed("MCP status", exc)
            return False
        if self._update_cancel_scope is not None:
            if command.name is TuiSlashCommandName.update:
                self._start_update(command.args)
            else:
                self.renderer.command_error(
                    "Cannot run this slash command while a Wisp update operation is in progress."
                )
            return False
        if self.state.current_command_id is not None:
            operation = self._active_operation()
            self.renderer.command_error(f"Cannot run slash commands while {operation} is running.")
            return False
        if self._session_operation_active():
            self.renderer.command_error(
                f"Cannot run slash commands while {self._session_operation_name()}."
            )
            return False
        if command.name is TuiSlashCommandName.update:
            self._start_update(command.args)
            return False
        if command.name is TuiSlashCommandName.init:
            if command.args:
                self.renderer.command_error("Usage: /init")
                return False
            return await self._start_init()
        if command.name in {TuiSlashCommandName.plan, TuiSlashCommandName.build}:
            if command.args:
                self.renderer.command_error(f"Usage: /{command.name.value}")
                return False
            await self._handle_mode_command(cast(AgentMode, command.name.value))
            return False
        if command.name is TuiSlashCommandName.compact:
            if self.state.status is not TuiStatus.idle:
                self.renderer.command_error("Cannot compact while another operation is active.")
                return False
            instructions = " ".join(command.args).strip() or None
            return await self._start_compaction(instructions)
        if command.name is TuiSlashCommandName.context:
            await self._handle_context_command(command.args)
            return False
        if command.name is TuiSlashCommandName.auth:
            self._auth.status(command.args)
            return False
        if command.name is TuiSlashCommandName.connect:
            self._start_connect(command.args)
            return False
        if command.name is TuiSlashCommandName.disconnect:
            self._start_disconnect(command.args)
            return False
        if command.name is TuiSlashCommandName.provider:
            await self._handle_provider_command(command.args)
            return False
        if command.name is TuiSlashCommandName.model:
            await self._handle_model_command(command.args)
            return False
        if command.name is TuiSlashCommandName.new:
            await self._handle_new_session_command(command.args)
            return False
        if command.name is TuiSlashCommandName.resume:
            await self._handle_resume_command(command.args)
            return False
        self.renderer.command_error(f"Unknown command: /{command.name.value}")
        return False

    async def _handle_mode_command(self, mode: AgentMode) -> None:
        try:
            command_id = await self.controller.configure(mode=mode)
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("configure", exc)
            return
        self.pending_configures[command_id] = _PendingConfigure(
            command_id=command_id,
            mode=mode,
        )

    async def _handle_new_session_command(self, args: tuple[str, ...]) -> None:
        if args:
            self.renderer.command_error("Usage: /new")
            return
        try:
            command_id = await self.controller.new_session()
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("new session", exc)
            return
        self.pending_new_session_command_id = command_id
        self._update_view(status="starting new session")

    async def _handle_resume_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /resume [session-id]")
            return
        if not args:
            self._call_renderer_optional("session_catalog_started")
            await self._wait_for_session_operation_paint()
            try:
                command_id = await self.controller.get_sessions(limit=200)
            except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
                self.renderer.send_failed("session catalog", exc)
                self._call_renderer_optional("session_catalog_finished")
                return
            self.pending_session_catalog = _PendingSessionCatalog(command_id=command_id)
            self._update_view(status="loading sessions")
            return

        session_id = args[0].strip()
        if not session_id:
            self.renderer.command_error("Usage: /resume [session-id]")
            return
        self._clear_history_detail_requests("Session changed before output loaded.")
        self._active_history_session_id = None
        # Guard the composer before awaiting a potentially backpressured
        # transport. Picker selections may already have entered this lifecycle;
        # renderer implementations intentionally make the repeated start
        # idempotent.
        self._call_renderer_optional("session_switch_started", session_id)
        await self._wait_for_session_operation_paint()
        try:
            command_id = await self.controller.select_session(session_id)
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("session selection", exc)
            self._call_renderer_optional("session_switch_finished")
            return
        self.pending_session_switch = _PendingSessionSwitch(
            requested_session_id=session_id,
            select_command_id=command_id,
        )
        self._update_view(status="switching session")

    def _session_operation_active(self) -> bool:
        return (
            self.pending_session_catalog is not None
            or self.pending_session_switch is not None
            or self.pending_new_session_command_id is not None
        )

    def _session_operation_name(self) -> str:
        if self.pending_session_switch is not None:
            return "a session switch is in progress"
        if self.pending_new_session_command_id is not None:
            return "a new session is starting"
        return "the session catalog is loading"

    def _call_renderer_optional(
        self, method_name: str, *args: object, **kwargs: object
    ) -> object | None:
        method = getattr(self.renderer, method_name, None)
        if callable(method):
            return cast(object, method(*args, **kwargs))
        return None

    async def _wait_for_session_operation_paint(self) -> None:
        """Let Textual reveal session-operation chrome before cold RPC work starts."""

        wait_for_paint = getattr(self.renderer, "wait_for_session_operation_paint", None)
        if callable(wait_for_paint):
            await cast(Callable[[], Awaitable[None]], wait_for_paint)()

    def _end_token_stream(self, completed_content: str | None = None) -> None:
        """Finalize streaming while preserving the original renderer contract."""

        if completed_content is not None:
            reconcile = getattr(self.renderer, "end_token_stream_with_content", None)
            if callable(reconcile):
                reconcile(completed_content)
                return
        self.renderer.end_token_stream()

    async def _handle_provider_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /provider [provider]")
            return
        if not args:
            line = f"Current provider: {self.current_provider}"
            pending_provider = self._latest_pending_provider()
            if pending_provider is not None:
                line += f" (pending: {pending_provider})"
            self.renderer.notice(line)
            return
        provider = args[0]
        try:
            command_id = await self.controller.configure(provider=provider)
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("configure", exc)
            return
        self.pending_configures[command_id] = _PendingConfigure(
            command_id=command_id,
            provider=provider,
            reset_model=True,
            # handle_rpc_configure_command unconditionally resets agent.effort
            # to None whenever a configure carries `provider` and no explicit
            # `effort` (see its has_provider branch) -- mirror that here so
            # current_effort/the persisted setting don't go stale relative to
            # what the RPC agent is actually using on the next prompt.
            has_effort=True,
        )
        self._update_view(status="configuring")
        self.renderer.notice(f"Configuring provider: {provider}")

    async def _handle_context_command(self, args: tuple[str, ...]) -> None:
        if not args:
            if self.pending_context_status_command_id is not None:
                self.renderer.command_error("Context status request is already pending.")
                return
            try:
                command_id = await self.controller.get_session_stats()
            except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
                self.renderer.send_failed("context status", exc)
                return
            self.pending_context_status_command_id = command_id
            self.pending_context_status_received = False
            return
        if len(args) != 2 or args[0].lower() != "auto" or args[1].lower() not in {"on", "off"}:
            self.renderer.command_error("Usage: /context [auto on|off]")
            return
        enabled = args[1].lower() == "on"
        try:
            command_id = await self.controller.configure(auto_compaction_enabled=enabled)
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("configure", exc)
            return
        self.pending_configures[command_id] = _PendingConfigure(
            command_id=command_id,
            auto_compaction_enabled=enabled,
            has_auto_compaction_enabled=True,
        )
        self._update_view(status="configuring")
        self.renderer.notice(f"Configuring automatic compaction: {'on' if enabled else 'off'}")

    async def _handle_model_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 2:
            self.renderer.command_error("Usage: /model [model] [effort]")
            return
        if not args:
            if self.model_catalog is None:
                self.renderer.command_error("Model catalog is unavailable; use /model <model>.")
                return
            self.renderer.model_picker_request(
                self.model_catalog.model_copy(
                    update={
                        "providers": tuple(
                            entry
                            for entry in self.model_catalog.providers
                            if entry.name not in _MODEL_PICKER_HIDDEN_PROVIDERS
                        )
                    }
                )
            )
            return
        # ModelPicker qualifies its selection as "provider::model" (see
        # widgets.ModelPicker.submit_current_selection) so a row for a model id
        # shared by multiple providers (e.g. "gpt-5.5" under both openai and
        # openai-codex) always switches to the exact provider the user picked,
        # rather than depending on ModelRegistry.resolve's ambiguity handling.
        # A typed /model <id> (no "::") never carries a provider -- unaffected.
        maybe_provider, _, qualified_model = args[0].partition("::")
        model = qualified_model if qualified_model else args[0]
        provider: str | None = maybe_provider if qualified_model else None
        raw_effort = args[1] if len(args) > 1 else None
        clear_effort = raw_effort == MODEL_COMMAND_CLEAR_EFFORT_TOKEN
        effort = None if clear_effort else raw_effort
        try:
            command_id = await self.controller.configure(
                provider=provider, model=model, effort=effort, clear_effort=clear_effort
            )
        except Exception as exc:  # noqa: BLE001 - show send failure in the TUI
            self.renderer.send_failed("configure", exc)
            return
        self.pending_configures[command_id] = _PendingConfigure(
            command_id=command_id,
            provider=provider,
            model=model,
            effort=effort,
            # handle_rpc_configure_command unconditionally resets agent.effort
            # to None whenever a configure carries `model` and no explicit
            # `effort` -- whether via an explicit provider switch, a
            # model-triggered auto-switch, or a same-provider model change (the
            # tier may not be valid for the new model). /model always sends
            # `model` in this branch (bare /model returns via the picker path
            # above), so has_effort is unconditionally True here too, matching
            # the server exactly rather than only when an effort arg was given.
            has_effort=True,
        )
        self._update_view(status="configuring")
        detail = f", effort {effort or 'provider default'}" if raw_effort is not None else ""
        self.renderer.notice(f"Configuring model: {model}{detail}")

    async def _handle_input_closed(self, signal: _InputClosed) -> bool:
        self._disarm_quit()
        self.state.input_closed = True
        if self.state.status is TuiStatus.exiting:
            return False
        self.state.exit_requested = True
        self._cancel_connect("Provider connection cancelled: input closed.")
        self._cancel_update("Wisp update cancelled: input closed.")
        if self.state.pending_trust is not None:
            # Resolve pending trust as untrusted (safe) so the RPC side unblocks.
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt closed",
                transient=True,
            )
        if self.state.pending_approval is not None:
            # Denying the pending approval is the conservative safety behavior even
            # when a live renderer reports that EOF began under an older mode.
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: input closed",
                exit_after_denial=True,
            )
        if self.state.current_command_id is not None:
            self._abandon_queued_prompts()
            if self.state.current_command_type == "compact":
                self.renderer.notice("input closed; finishing compaction")
            else:
                self.renderer.input_closed_finishing_prompt()
            return False
        return await self._request_shutdown()

    async def _handle_quit_pressed(self, signal: _QuitPressed) -> bool:
        """Arm graceful quit, or execute it on a second timely Ctrl+C press."""

        armed_at = self._quit_armed_at
        if armed_at is not None and 0 <= signal.pressed_at - armed_at <= self._quit_press_window:
            self._disarm_quit()
            return await self._handle_quit()
        self._quit_armed_at = signal.pressed_at
        self.renderer.notice("Press Ctrl+C again to quit.")
        return False

    async def _handle_input_cancelled(self, signal: _InputCancelled) -> bool:
        """Cancel the nearest shell-owned operation in response to Escape."""

        self._disarm_quit()
        if self.state.pending_trust is not None:
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt cancelled",
                transient=True,
            )
        if self.state.pending_approval is not None:
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: cancelled",
                exit_after_denial=False,
            )
        if self.state.current_command_id is not None:
            message = (
                "Cancelling compaction..."
                if self.state.current_command_type == "compact"
                else "Cancelling current prompt..."
            )
            return await self._cancel_current(message)
        if self._cancel_connect("Provider connection cancelled."):
            return False
        if self._cancel_update("Wisp update cancelled."):
            return False
        return False

    async def _handle_input_interrupted(self, signal: _InputInterrupted) -> bool:
        self._disarm_quit()
        if self.state.pending_trust is not None:
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt interrupted",
                transient=True,
            )
        if self.state.pending_approval is not None:
            # Denying the pending approval is the conservative safety behavior even
            # when a live renderer reports that Ctrl-C began under an older mode.
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: interrupted",
                exit_after_denial=False,
            )
        if self.state.current_command_id is not None:
            message = (
                "Cancelling compaction..."
                if self.state.current_command_type == "compact"
                else "Cancelling current prompt..."
            )
            return await self._cancel_current(message)
        if self._cancel_connect("Provider connection cancelled."):
            return False
        if self._cancel_update("Wisp update cancelled."):
            return False
        self.renderer.input_cleared()
        return False

    def _disarm_quit(self) -> None:
        self._quit_armed_at = None

    async def _handle_quit(self) -> bool:
        self._disarm_quit()
        self.state.exit_requested = True
        self._cancel_connect("Provider connection cancelled: quit requested.")
        self._cancel_update("Wisp update cancelled: quit requested.")
        self._abandon_queued_prompts()
        if self.state.pending_trust is not None:
            return await self._answer_pending_trust(
                "",
                trusted=False,
                reason="Trust prompt: quit requested",
                transient=True,
            )
        if self.state.pending_approval is not None:
            return await self._answer_pending_approval(
                "",
                approved=False,
                reason="Denied from TUI: quit requested",
                exit_after_denial=True,
            )
        if self.state.current_command_id is not None:
            message = (
                "Quit requested; cancelling compaction..."
                if self.state.current_command_type == "compact"
                else "Quit requested; cancelling current prompt..."
            )
            return await self._cancel_current(message)
        return await self._request_shutdown()

    def _start_connect(self, args: tuple[str, ...]) -> None:
        if self._connect_cancel_scope is not None:
            self.renderer.command_error("A provider connection is already in progress.")
            return
        task_group = self._task_group
        if task_group is None:
            raise RuntimeError("provider connections require an active TUI task group")
        cancel_scope = anyio.CancelScope()
        self._connect_cancel_scope = cancel_scope
        task_group.start_soon(self._run_connect, args, cancel_scope)

    def _start_disconnect(self, args: tuple[str, ...]) -> None:
        if self._connect_cancel_scope is not None:
            self.renderer.command_error("A provider connection is already in progress.")
            return
        task_group = self._task_group
        if task_group is None:
            raise RuntimeError("provider disconnections require an active TUI task group")
        cancel_scope = anyio.CancelScope()
        self._connect_cancel_scope = cancel_scope
        task_group.start_soon(self._run_disconnect, args, cancel_scope)

    async def _handle_update_prompt_action(
        self,
        action: UpdatePromptAction,
        update: UpdateAvailable,
    ) -> None:
        if action is UpdatePromptAction.later:
            return
        if action is UpdatePromptAction.skip_version:
            persisted = await self._update_skip_writer(
                update.latest_version,
                home_dir=self._settings_home_dir,
            )
            if not persisted:
                self.renderer.command_error(
                    "Could not remember the skipped Wisp version; it may be offered again."
                )
            return
        self._start_update((), offered_update=update, restart=True)

    def _start_update(
        self,
        args: tuple[str, ...],
        *,
        offered_update: UpdateAvailable | None = None,
        restart: bool = False,
    ) -> None:
        if args not in {(), ("check",), ("install",)}:
            self.renderer.command_error("Usage: /update [check|install]")
            return
        if self._update_cancel_scope is not None:
            self.renderer.command_error("A Wisp update operation is already in progress.")
            return
        if self._connect_cancel_scope is not None:
            self.renderer.command_error(
                "Cannot update Wisp while a provider connection is in progress."
            )
            return
        task_group = self._task_group
        if task_group is None:
            raise RuntimeError("updates require an active TUI task group")
        cancel_scope = anyio.CancelScope()
        self._update_cancel_scope = cancel_scope
        if restart and offered_update is not None:
            self._call_renderer_optional(
                "update_operation_started",
                offered_update,
            )
        else:
            self.renderer.notice("Checking PyPI for Wisp updates...")
        task_group.start_soon(
            self._run_update,
            args,
            cancel_scope,
            offered_update,
            restart,
        )

    async def _run_update(
        self,
        args: tuple[str, ...],
        cancel_scope: anyio.CancelScope,
        offered_update: UpdateAvailable | None,
        restart: bool,
    ) -> None:
        installed = False
        try:
            with cancel_scope:
                installed = (
                    await self._updates.install_available(offered_update, restart=True)
                    if offered_update is not None
                    else await self._updates.run(args)
                )
        except Exception as exc:  # noqa: BLE001 - keep optional update UI recoverable
            self.renderer.command_error(f"Update failed: {exc}")
        finally:
            if self._update_cancel_scope is cancel_scope:
                self._update_cancel_scope = None
            if restart:
                send = self._signal_send
                if send is not None:
                    await send.send(
                        _UpdateOperationFinished(
                            installed=installed,
                            restart_requested=True,
                        )
                    )

    def _cancel_update(self, message: str) -> bool:
        cancel_scope = self._update_cancel_scope
        if cancel_scope is None:
            return False
        if self._updates.installing:
            self.renderer.notice(
                "Wisp update installation is in progress; waiting for it to finish safely."
            )
            return True
        cancel_scope.cancel()
        self.renderer.notice(message)
        return True

    async def _run_connect(
        self,
        args: tuple[str, ...],
        cancel_scope: anyio.CancelScope,
    ) -> None:
        try:
            with cancel_scope:
                await self._refresh_connection_catalog_if_needed()
                await self._auth.connect(args)
        finally:
            if self._connect_cancel_scope is cancel_scope:
                self._connect_cancel_scope = None

    async def _run_disconnect(
        self,
        args: tuple[str, ...],
        cancel_scope: anyio.CancelScope,
    ) -> None:
        try:
            with cancel_scope:
                await self._refresh_connection_catalog_if_needed()
                await self._auth.disconnect(args)
        finally:
            if self._connect_cancel_scope is cancel_scope:
                self._connect_cancel_scope = None

    def _cancel_connect(self, message: str) -> bool:
        cancel_scope = self._connect_cancel_scope
        pending_device_codes = tuple(
            pending.command_id
            for pending in self._pending_connection_mutations.values()
            if pending.command_type == "begin_device_code"
        )
        if cancel_scope is None and not pending_device_codes:
            return False
        if cancel_scope is not None and not cancel_scope.cancel_called:
            cancel_scope.cancel()
        task_group = self._task_group
        if task_group is not None:
            for command_id in pending_device_codes:
                task_group.start_soon(self.controller.cancel, command_id)
        self.renderer.notice(message)
        return True

    async def _start_init(self) -> bool:
        self.state.status = TuiStatus.running
        self.state.current_command_type = "init"
        self.state.pending_approval = None
        self.state.cancel_requested = False
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        self._sync_view()
        self.renderer.prompt_submitted("/init")
        self.renderer.running()
        try:
            command_id = await self.controller.init()
        except Exception as exc:
            self.state.current_command_type = None
            self._update_view(status="error")
            self._call_renderer_optional("discard_live_prompt", "/init")
            self.renderer.send_failed("init", exc)
            pagination = self._history_pagination
            if pagination is not None and pagination.latest_reload_pending:
                await self._request_latest_history_page()
            return True
        self.state.current_command_id = command_id
        return False

    async def _start_prompt(self, prompt: TuiSubmission) -> bool:
        self.state.status = TuiStatus.running
        self.state.current_command_type = "prompt"
        self.state.pending_approval = None
        self.state.cancel_requested = False
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        self._sync_view()
        self.renderer.running()
        try:
            command_id = await self.controller.prompt(prompt.content)
        except Exception as exc:
            self.state.status = TuiStatus.idle
            self.state.current_command_type = None
            self._update_view(
                status="error",
                input_hint=_prompt_for_mode(_InputMode.idle),
                input_mode=_InputMode.idle,
            )
            self._restore_submissions((prompt,))
            self.renderer.send_failed("prompt", exc)
            pagination = self._history_pagination
            if pagination is not None and pagination.latest_reload_pending:
                await self._request_latest_history_page()
            return False
        self.state.current_command_id = command_id
        self._resolve_submission(prompt)
        self.renderer.prompt_submitted(prompt)
        return False

    async def _start_compaction(self, instructions: str | None) -> bool:
        self.state.status = TuiStatus.compacting
        self.state.current_command_type = "compact"
        self.state.pending_approval = None
        self.state.cancel_requested = False
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        self._sync_view()
        self.renderer.running()
        try:
            command_id = await self.controller.compact(instructions)
        except Exception as exc:  # noqa: BLE001 - keep the TUI usable after a send failure
            self.state.status = TuiStatus.idle
            self.state.current_command_type = None
            self._update_view(
                status="error",
                input_hint=_prompt_for_mode(_InputMode.idle),
                input_mode=_InputMode.idle,
            )
            self.renderer.send_failed("compact", exc)
            pagination = self._history_pagination
            if pagination is not None and pagination.latest_reload_pending:
                await self._request_latest_history_page()
            return False
        self.state.current_command_id = command_id
        return False

    async def _cancel_current(self, message: str) -> bool:
        command_id = self.state.current_command_id
        if command_id is None:
            return False
        if self.state.cancel_requested:
            self.renderer.cancel_already_requested()
            return False
        self.state.cancel_requested = True
        self._update_view(status="cancelling")
        self.renderer.cancelling(message)
        try:
            await self.controller.cancel(command_id)
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("cancel", exc)
            return True
        self.state.status = self._active_status()
        self.state.pending_approval = None
        return False

    async def _request_shutdown(self) -> bool:
        if self.state.shutdown_command_id is not None:
            self.state.status = TuiStatus.exiting
            self._sync_view()
            return False
        self._report_runtime_queue_before_shutdown()
        self.state.status = TuiStatus.exiting
        self._ignored_session_stats_command_ids.update(self._pending_session_stats_command_ids)
        self._pending_session_stats_command_ids.clear()
        self._sync_view()
        try:
            shutdown_id = await self.controller.shutdown()
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.shutdown_failed(exc)
            return True
        self.state.shutdown_command_id = shutdown_id
        return False

    async def _answer_pending_approval(
        self,
        answer: str,
        *,
        approved: bool | None = None,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        exit_after_denial: bool,
    ) -> bool:
        approval = self.state.pending_approval
        if approval is None:
            return False
        normalized = answer.strip().lower()
        selected_scope: ApprovalScope = scope or (
            "all_session"
            if normalized in {"a", "all", "yolo"}
            else "tool_session"
            if normalized in {"t", "tool"}
            else "once"
        )
        selected_approved = (
            approved
            if approved is not None
            else normalized
            in {
                "y",
                "yes",
                "t",
                "tool",
                "a",
                "all",
                "yolo",
            }
        )
        selected_reason = None if selected_approved else reason or "Denied from TUI"
        if reason == "Denied from TUI: input closed":
            self.renderer.approval_input_closed()
        elif reason == "Denied from TUI: interrupted":
            self.renderer.approval_interrupted()
        elif reason == "Denied from TUI: quit requested":
            self.renderer.quit_requested_denying_approval()
        ok = await self._send_approval(
            approval.call_id,
            approved=selected_approved,
            reason=selected_reason,
            scope=(selected_scope if selected_approved and selected_scope != "once" else None),
        )
        self.state.pending_approval = None
        if not ok:
            return True
        self.state.status = self._active_status()
        if exit_after_denial and not selected_approved:
            self.state.exit_requested = True
        self._sync_view()
        return False

    async def _send_approval(
        self,
        call_id: str,
        *,
        approved: bool,
        reason: str | None,
        scope: ApprovalScope | None,
    ) -> bool:
        try:
            if scope is None:
                await self.controller.approve(call_id, approved=approved, reason=reason)
            else:
                await self.controller.approve(
                    call_id,
                    approved=approved,
                    reason=reason,
                    scope=scope,
                )
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("approval", exc)
            return False
        return True

    async def _answer_pending_trust(
        self,
        answer: str,
        *,
        trusted: bool | None = None,
        reason: str | None = None,
        transient: bool = False,
    ) -> bool:
        trust = self.state.pending_trust
        if trust is None:
            return False
        selected_trusted = (
            trusted if trusted is not None else answer.strip().lower() in {"y", "yes"}
        )
        selected_reason = reason if not selected_trusted else None
        ok = await self._send_trust(
            trust.request_id,
            trusted=selected_trusted,
            reason=selected_reason,
            transient=transient and not selected_trusted,
        )
        self.state.pending_trust = None
        if not ok:
            return True
        self.state.status = self._active_status()
        self._sync_view()
        return False

    async def _send_trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None,
        transient: bool,
    ) -> bool:
        try:
            await self.controller.trust(
                request_id,
                trusted=trusted,
                reason=reason,
                transient=transient,
            )
        except Exception as exc:
            self._update_view(status="error")
            self.renderer.send_failed("trust", exc)
            return False
        return True

    async def _handle_rpc_event(self, event: KnownWispEvent) -> bool:
        if self._observe_model_catalog_event(event):
            return False
        if self._observe_connection_catalog_event(event):
            return False
        if self._observe_connection_mutation_event(event):
            return False
        if isinstance(event, RpcModelCatalogReported):
            pending_configure = self.pending_configures.get(event.command_id)
            if pending_configure is not None and pending_configure.catalog is None:
                self.pending_configures[event.command_id] = replace(
                    pending_configure,
                    catalog=event.catalog,
                )
                await self._maybe_finish_pending_configure(event.command_id)
            return False
        if (
            isinstance(event, RpcMessagesReported)
            and event.command_id in self._ignored_history_page_commands
        ):
            return False
        if (
            isinstance(event, RpcCommandFinished)
            and event.command_id in self._ignored_history_page_commands
        ):
            self._ignored_history_page_commands.discard(event.command_id)
            return False

        if isinstance(event, RpcMessagesReported):
            pending_detail_report = self._pending_history_details.get(event.command_id)
            if pending_detail_report is not None:
                if pending_detail_report.report is not None:
                    pending_detail_report.error = (
                        "persisted output lookup returned more than one result"
                    )
                elif event.session_id != pending_detail_report.session_id:
                    pending_detail_report.error = (
                        "persisted output did not match the active session"
                    )
                elif len(event.messages) != 1:
                    pending_detail_report.error = (
                        "persisted output lookup did not return exactly one row"
                    )
                elif event.messages[0].entry_id != pending_detail_report.entry_id:
                    pending_detail_report.error = "persisted output lookup returned the wrong row"
                else:
                    pending_detail_report.report = event
                return False

        catalog = self.pending_session_catalog
        if (
            isinstance(event, RpcSessionsReported)
            and catalog is not None
            and event.command_id == catalog.command_id
        ):
            catalog.report = event
            return False

        session_switch = self.pending_session_switch
        if session_switch is not None:
            if (
                isinstance(event, RpcSessionSelected)
                and event.command_id == session_switch.select_command_id
            ):
                session_switch.selected = event
                self._clear_history_pagination()
                return False
            if (
                isinstance(event, RpcMessagesReported)
                and event.command_id == session_switch.history_command_id
            ):
                if session_switch.history_report is not None:
                    return False
                session_switch.history_report = event
                hydration = session_switch.complete_history
                if hydration is not None:
                    try:
                        hydration.add(event)
                    except ValueError as exc:
                        self._fail_committed_session_hydration(str(exc))
                        return False
                    self._call_renderer_optional(
                        "history_hydration_progress",
                        f"Loading session history… {hydration.loaded_message_count:,} messages",
                    )
                return False

        if (
            isinstance(event, RpcMessagesReported)
            and event.command_id == self._history_recovery_command_id
        ):
            self._history_recovery_report_received = True
            self._activate_history_pagination(event)
            entries = history_entries_from_rpc_messages(event.messages)
            self._call_renderer_optional("replace_latest_history_entries", entries)
            return False

        pagination = self._history_pagination
        if (
            isinstance(event, RpcMessagesReported)
            and pagination is not None
            and event.command_id == pagination.command_id
        ):
            pagination.report = event
            return False
        if (
            isinstance(event, RpcMessagesReported)
            and pagination is not None
            and event.command_id == pagination.latest_command_id
        ):
            pagination.latest_report = event
            return False

        if (
            isinstance(event, RpcMessagesReported)
            and pagination is not None
            and event.command_id == pagination.newer_command_id
        ):
            pagination.newer_report = event
            return False

        if isinstance(event, SessionStatsReported):
            if event.command_id in self._ignored_session_stats_command_ids:
                return False
            is_context_status = event.command_id == self.pending_context_status_command_id
            if (
                not is_context_status
                and event.command_id not in self._pending_session_stats_command_ids
            ):
                return False
            if self.view.update_context_from_event(event):
                self._update_view()
            if is_context_status:
                self.pending_context_status_received = True
                self.renderer.context_status(event.stats)
            return False
        if isinstance(event, RpcMcpStatusReported):
            self.renderer.notice(mcp_status_text(event.status))
            return False
        context_updated = self.view.update_context_from_event(event)
        if context_updated:
            self._update_view()
        if isinstance(event, ContextEstimated):
            return False
        if isinstance(event, QueueUpdated):
            self._apply_queue_update(event)
            return False
        if isinstance(event, QueueItemsRemoved):
            pending_restore = self._pending_queue_restore
            if pending_restore is not None and event.command_id == pending_restore.command_id:
                pending_restore.removed = event
            if event.operation == "pop" and event.kind is not None:
                queue = self._queue_for(event.kind)
                if queue:
                    if event.kind == "steering":
                        self._queue_steering = queue[:-1]
                    else:
                        self._queue_follow_up = queue[:-1]
                    self._sync_pending_view()
            return False
        if isinstance(event, QueueMessageInjected):
            queued = self._queue_for(event.kind)
            visible_content = (
                event.skill_invocation.original_content
                if event.skill_invocation is not None
                else event.content
            )
            submission = next(
                (item for item in queued if item.content == visible_content),
                None,
            )
            self.renderer.prompt_submitted(submission or visible_content)
            if submission is not None:
                self._resolve_submission(submission)
                self._local_queue_submissions = [
                    (kind, item)
                    for kind, item in self._local_queue_submissions
                    if int(item.id) != int(submission.id)
                ]
                remaining = tuple(item for item in queued if int(item.id) != int(submission.id))
                if event.kind == "steering":
                    self._queue_steering = remaining
                else:
                    self._queue_follow_up = remaining
                self._sync_pending_view()
            return False
        if isinstance(event, ProviderRetrying):
            self._update_view(
                status=(
                    f"retrying {event.attempt}/{event.max_attempts} in {event.delay_seconds:.1f}s"
                ),
                input_hint=_prompt_for_status(self._active_status()),
                input_mode=_InputMode.running,
                queued_steering=len(self._queue_steering),
                queued_follow_ups=len(self._queue_follow_up),
            )
            self.renderer.event(event)
            return False
        if isinstance(event, MessageStarted):
            self._sync_view()
        if isinstance(event, MessageDelta):
            if event.content_kind == "text":
                self.state.token_stream_started = True
                self.state.rendered_tokens = True
                self.renderer.token_delta(event.delta)
            # Thinking deltas may be interleaved with visible text. They are not a
            # message boundary and must not finalize the active Markdown widget.
            return False
        if isinstance(event, MessageCompleted):
            suppress_completed_message = self.state.rendered_tokens
            if self.state.token_stream_started:
                # The terminal event is authoritative: reconcile the incremental
                # renderer with its complete content before retaining the widget.
                self._end_token_stream(event.content)
                self.state.token_stream_started = False
            self.state.rendered_tokens = False
            if suppress_completed_message:
                self._call_renderer_optional("record_streamed_message_completed", event)
                return False
        if isinstance(event, ErrorEvent) and self.state.token_stream_started:
            # Provider failures and cancellations can omit MessageCompleted. Close
            # the partial assistant output before rendering the terminal error so
            # line and fullscreen renderers preserve transcript order.
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
            self.state.rendered_tokens = False
        if isinstance(event, ToolApprovalRequested):
            self.state.pending_approval = event
            if self.state.cancel_requested:
                # A cancel for this command was already sent before the agent
                # reached its approval checkpoint. The event still arrives over
                # the RPC transport, but reopening a prompt for a turn the user
                # is actively aborting would contradict their cancel and could
                # mislead them into thinking it didn't register. Deny quietly —
                # the eventual RpcCommandFinished(cancelled) unwinds the rest.
                return await self._answer_pending_approval(
                    "",
                    approved=False,
                    reason="Denied from TUI: cancelling",
                    exit_after_denial=False,
                )
            self.state.status = TuiStatus.waiting_for_approval
            self._sync_view()
            self.renderer.approval_request(event)
            if self.state.input_closed:
                return await self._answer_pending_approval(
                    "",
                    approved=False,
                    reason="Denied from TUI: input closed",
                    exit_after_denial=True,
                )
            return False

        if isinstance(event, TrustRequested):
            self.state.pending_trust = event
            if self.state.cancel_requested:
                # The trust event may already be queued when the user cancels.
                # Do not persist trust for a turn that is actively unwinding.
                return await self._answer_pending_trust(
                    "",
                    trusted=False,
                    reason="Trust prompt cancelled",
                    transient=True,
                )
            self.state.status = TuiStatus.waiting_for_trust
            self._sync_view()
            self.renderer.trust_request(event)
            if self.state.input_closed:
                # No way to answer: default to untrusted (safe) so the run proceeds.
                # Mark it transient so the gate does not persist a denial the user
                # never explicitly chose.
                return await self._answer_pending_trust(
                    "",
                    trusted=False,
                    reason="Trust prompt: input closed",
                    transient=True,
                )
            return False

        if isinstance(event, ProjectConfigApplied):
            # The RPC side applied a trusted project's config mid-session (first-run
            # approval). Adopt the provider/model/auth it now runs with, so the header
            # and /provider,/model,/auth,/connect stop showing the untrusted-startup ones.
            self.current_provider = event.provider
            self.current_model = event.model
            # Adopt the RPC agent's own already-filtered, authoritative effort
            # (see the trusted-project rebuild in wisp.rpc.host) rather
            # than re-deriving it from self.current_effort here -- that local
            # copy was itself already filtered once, against the
            # untrusted-startup provider/model, in __init__. A tier invalid
            # there but valid for the trusted project's provider/model would
            # already be gone from it and unrecoverable, the same class of bug
            # ProjectConfigApplied.effort's docstring explains in more detail.
            self.current_effort = event.effort
            self.auth_store = JsonAuthStore(event.auth_path)
            self.connection_catalog = None
            await self._request_connection_catalog()
            # The credential file just moved, so the `@`-picker's startup policy
            # snapshot is now stale and would keep offering the new auth file for
            # mention while the agent's tool context protects it. Hand the renderer
            # the new path; Textual rebuilds its corpus, other renderers have no
            # picker and ignore this.
            self._call_renderer_optional("project_auth_path_changed", event.auth_path)
            self.renderer.notice(
                f"Applied trusted project config: provider {event.provider}"
                f"{f', model {event.model}' if event.model else ''}."
            )
            self._sync_view()
            self.model_catalog = None
            await self._request_model_catalog()
            return False

        if isinstance(event, SkillCatalogUpdated):
            self.skill_catalog = event.catalog
            self._publish_skill_catalog()
            return False

        if isinstance(event, SkillInvoked):
            self._call_renderer_optional("skill_invoked", event)
            return False

        if isinstance(event, ModelProviderAutoSwitched):
            # Retain the compatibility notice while the catalog result remains
            # authoritative for the committed selection.
            pending = self.pending_configures.get(event.command_id)
            if pending is not None:
                self.pending_configures[event.command_id] = replace(
                    pending,
                    provider=event.provider,
                )
            return False

        if isinstance(event, RpcCommandFinished):
            pending_queue = self._pending_queue_submissions.pop(event.command_id, None)
            if pending_queue is not None:
                if event.ok:
                    self._local_queue_submissions.append(
                        (pending_queue.kind, pending_queue.submission)
                    )
                    self._record_prompt_acceptance(pending_queue.submission.content)
                    label = "Steering" if pending_queue.kind == "steering" else "Follow-up"
                    self.renderer.notice(f"{label} queued.")
                    self._sync_pending_view()
                else:
                    self._restore_submissions((pending_queue.submission,))
                    self.renderer.command_error(event.error or "Queue submission failed.")
                return False
            pending_restore = self._pending_queue_restore
            if pending_restore is not None and event.command_id == pending_restore.command_id:
                self._pending_queue_restore = None
                removed = pending_restore.removed
                removed_content = (
                    (removed.steering if pending_restore.kind == "steering" else removed.follow_up)
                    if removed is not None
                    else ()
                )
                if event.ok and removed_content:
                    self._local_queue_submissions = [
                        (kind, item)
                        for kind, item in self._local_queue_submissions
                        if int(item.id) != int(pending_restore.submission.id)
                    ]
                    content = removed_content[0]
                    restored = (
                        pending_restore.submission
                        if pending_restore.submission.content == content
                        else TuiSubmission(
                            id=new_submission_id(),
                            content=content,
                            display=content,
                            input_mode="running",
                            queue_kind=pending_restore.kind,
                        )
                    )
                    self._restore_submissions((restored,))
                    self.renderer.notice("Queued item restored.")
                elif event.ok:
                    self.renderer.notice("The selected queue was already empty.")
                else:
                    self.renderer.command_error(event.error or "Could not restore queued item.")
                return False
            if event.command_id in self._ignored_session_stats_command_ids:
                self._ignored_session_stats_command_ids.discard(event.command_id)
                return False
            self._pending_session_stats_command_ids.discard(event.command_id)
            pending_detail = self._pending_history_details.pop(event.command_id, None)
            if pending_detail is not None:
                self._history_detail_commands_by_entry_id.pop(pending_detail.entry_id, None)
                report = pending_detail.report
                if (
                    event.ok
                    and pending_detail.error is None
                    and report is not None
                    and self._active_history_session_id == pending_detail.session_id
                ):
                    self._call_renderer_optional(
                        "history_detail_loaded",
                        pending_detail.entry_id,
                        report.messages[0].content,
                    )
                else:
                    error = (
                        pending_detail.error
                        or event.error
                        or "persisted output lookup completed without a result"
                    )
                    self._call_renderer_optional(
                        "history_detail_failed",
                        pending_detail.entry_id,
                        error,
                    )
                return False
            if event.command_id == self._history_recovery_command_id:
                received_report = self._history_recovery_report_received
                self._history_recovery_command_id = None
                self._history_recovery_report_received = False
                if not event.ok or not received_report:
                    detail = event.error or "session history completed without a result"
                    self.renderer.command_error(f"Failed to recover session history: {detail}")
                    self._call_renderer_optional("history_page_request_failed")
                    self._call_renderer_optional("latest_history_reload_failed")
                return False
            if catalog is not None and event.command_id == catalog.command_id:
                await self._finish_session_catalog(event)
                return False
            if session_switch is not None and event.command_id in {
                session_switch.select_command_id,
                session_switch.history_command_id,
            }:
                await self._finish_session_switch(event)
                return False
            pagination = self._history_pagination
            if pagination is not None and event.command_id == pagination.command_id:
                await self._finish_history_page(event)
                return False
            if pagination is not None and event.command_id == pagination.latest_command_id:
                await self._finish_latest_history_page(event)
                return False
            if pagination is not None and event.command_id == pagination.newer_command_id:
                await self._finish_newer_history_page(event)
                return False
            if event.command_id == self.pending_new_session_command_id:
                await self._finish_new_session(event)
                return False
            if event.command_id in self.pending_configures:
                pending_configure = self.pending_configures[event.command_id]
                if pending_configure.completion is None:
                    self.pending_configures[event.command_id] = replace(
                        pending_configure,
                        completion=event,
                    )
                await self._maybe_finish_pending_configure(event.command_id)
                return False
            if event.command_id == self.pending_context_status_command_id:
                received = self.pending_context_status_received
                self.pending_context_status_command_id = None
                self.pending_context_status_received = False
                if not event.ok:
                    self.renderer.command_error(event.error or "context status failed")
                elif not received:
                    self.renderer.command_error("Context status completed without a result.")
                return False
            if event.command_id == self.state.shutdown_command_id:
                self._render_event(event)
                return True
            if event.command_id == self.state.current_command_id:
                # A malformed or abruptly closed RPC stream may reach its command
                # boundary without either MessageCompleted or ErrorEvent. Preserve
                # the partial response before rendering the boundary as a fallback.
                if self.state.token_stream_started:
                    self.renderer.end_token_stream()
                    self.state.token_stream_started = False
                    self.state.rendered_tokens = False
                self._render_event(event)
                return await self._finish_current_prompt(event)
        self._render_event(event)
        return False

    async def _finish_new_session(self, event: RpcCommandFinished) -> None:
        self.pending_new_session_command_id = None
        if not event.ok:
            self.renderer.command_error(event.error or "new session failed")
            self._sync_view()
            return
        self._clear_history_detail_requests("Session changed before output loaded.")
        self._active_history_session_id = None
        self._clear_history_pagination()
        self._ignored_session_stats_command_ids.update(self._pending_session_stats_command_ids)
        self._pending_session_stats_command_ids.clear()
        if self.pending_context_status_command_id is not None:
            self._ignored_session_stats_command_ids.add(self.pending_context_status_command_id)
            self.pending_context_status_command_id = None
            self.pending_context_status_received = False
        self.view.context = None
        self.view.cost = None
        self.view.last_session = None
        self._call_renderer_optional("clear_session")
        self.renderer.notice("Started a new session.")
        self._sync_view()
        await self._request_queue_state()

    async def _finish_session_catalog(self, event: RpcCommandFinished) -> None:
        pending = self.pending_session_catalog
        if pending is None or event.command_id != pending.command_id:
            return
        self.pending_session_catalog = None
        if not event.ok:
            self.renderer.command_error(event.error or "session catalog failed")
            self._call_renderer_optional("session_catalog_finished")
            self._sync_view()
            return
        if pending.report is None:
            self.renderer.command_error("Session catalog completed without a result.")
            self._call_renderer_optional("session_catalog_finished")
            self._sync_view()
            return
        self._call_renderer_optional("session_catalog_finished")
        self._call_renderer_optional(
            "session_picker_request",
            pending.report.sessions,
            selected_session_id=pending.report.selected_session_id,
        )
        self._sync_view()

    async def _finish_session_switch(self, event: RpcCommandFinished) -> None:
        pending = self.pending_session_switch
        if pending is None:
            return
        if event.command_id == pending.select_command_id:
            if not event.ok:
                self.renderer.command_error(event.error or "session selection failed")
                self._finish_session_switch_ui()
                return
            if pending.selected is None:
                self._fail_committed_session_hydration(
                    "session selection completed without a result"
                )
                return
            if self._history_hydration_policy is HistoryHydrationPolicy.COMPLETE:
                pending.complete_history = _CompleteHistoryHydration(
                    expected_session_id=pending.selected.session_id
                )
            try:
                pending.history_command_id = await self.controller.get_messages(
                    limit=(
                        TUI_HISTORY_MESSAGE_LIMIT
                        if pending.complete_history is not None
                        else self._history_message_limit
                    ),
                    complete_structure=pending.complete_history is not None,
                )
            except Exception as exc:  # noqa: BLE001 - selection already committed
                self._fail_committed_session_hydration(
                    f"failed to request selected session history: {exc}"
                )
                return
            self._update_view(status="loading session history")
            return

        if event.command_id != pending.history_command_id:
            return
        selected = pending.selected
        if selected is None:
            self.renderer.command_error("Selected session identity was lost during hydration.")
            self._finish_session_switch_ui()
            return
        if not event.ok or pending.history_report is None:
            detail = event.error or "session history completed without a result"
            self._fail_committed_session_hydration(detail)
            return
        if pending.history_report.session_id != selected.session_id:
            self._fail_committed_session_hydration(
                "session history result did not match the selected session"
            )
            return

        hydration = pending.complete_history
        if hydration is not None and pending.history_report.next_before_entry_id is not None:
            try:
                pending.history_command_id = await self.controller.get_messages(
                    session_id=selected.session_id,
                    limit=TUI_HISTORY_MESSAGE_LIMIT,
                    before_entry_id=pending.history_report.next_before_entry_id,
                    complete_structure=True,
                    allow_during_prompt=True,
                )
            except Exception as exc:  # noqa: BLE001 - selection already committed
                self._fail_committed_session_hydration(
                    f"failed to request older selected-session history: {exc}"
                )
                return
            pending.history_report = None
            self._update_view(status="loading session history")
            return

        label = selected.session_name or _compact_session_path(selected.session_path)
        messages = hydration.messages if hydration is not None else pending.history_report.messages
        entries = history_entries_from_rpc_messages(messages)
        if hydration is not None:
            expected_entry_ids = frozenset(
                message.entry_id for message in messages if message.role != "system"
            )
            missing_entry_ids = expected_entry_ids - represented_history_entry_ids(entries)
            if missing_entry_ids:
                self._fail_committed_session_hydration(
                    f"history conversion omitted {len(missing_entry_ids)} persisted rows"
                )
                return
        try:
            await self._render_history_entries(
                entries,
                text_fallback=(
                    None if hydration is not None else history_from_rpc_messages(messages)
                ),
                session_label=label,
                complete=hydration is not None,
            )
        except Exception as exc:  # noqa: BLE001 - selection already committed
            self._fail_committed_session_hydration(
                f"failed to mount selected session history: {exc}"
            )
            return
        self._active_history_session_id = selected.session_id
        if hydration is None:
            self._activate_history_pagination(pending.history_report)
        self.view.context = None
        self.view.cost = None
        self._update_view(last_session=label)
        self._finish_session_switch_ui()
        await self._request_queue_state()
        await self._request_session_stats()

    def _fail_committed_session_hydration(self, detail: str) -> None:
        self._clear_history_detail_requests("Session history could not be loaded.")
        self._active_history_session_id = None
        self._clear_history_pagination()
        pending = self.pending_session_switch
        selected = pending.selected if pending is not None else None
        if pending is not None:
            label = (
                selected.session_name or _compact_session_path(selected.session_path)
                if selected is not None
                else pending.requested_session_id
            )
            self._call_renderer_optional(
                "replace_history_entries",
                (),
                session_label=label,
            )
            self.view.context = None
            self.view.cost = None
            self._update_view(last_session=label)
        self.renderer.command_error(
            "Session changed, but its transcript could not be loaded: " + detail
        )
        self._finish_session_switch_ui()

    def _finish_session_switch_ui(self) -> None:
        self.pending_session_switch = None
        self._call_renderer_optional("session_switch_finished")
        self._sync_view()

    async def _maybe_finish_pending_configure(self, command_id: str) -> None:
        pending = self.pending_configures[command_id]
        event = pending.completion
        if event is None:
            return
        changes_selection = pending.provider is not None or pending.model is not None
        self.pending_configures.pop(command_id)
        if event.ok:
            catalog = pending.catalog
            if catalog is not None:
                requested_effort = pending.effort
                self._adopt_model_catalog(catalog)
                if (
                    pending.has_effort
                    and requested_effort is not None
                    and requested_effort != catalog.selection.effort
                ):
                    self.renderer.notice(
                        f"Effort '{requested_effort}' is not supported by "
                        f"{catalog.selection.effective_model}; using provider default."
                    )
            elif changes_selection:
                self.model_catalog = None
                self.renderer.notice(
                    "Configuration applied, but the authoritative model catalog is unavailable; "
                    "the displayed selection was not changed."
                )
            if pending.provider is not None and catalog is not None:
                if pending.reset_model:
                    self.renderer.notice(
                        f"Provider set to {self.current_provider}; model reset to provider default."
                    )
                else:
                    self.renderer.notice(f"Provider set to {self.current_provider}")
            if pending.model is not None and catalog is not None:
                self.renderer.notice(f"Model set to {self.current_model}")
            if changes_selection and catalog is not None:
                persist_user_model_selection(
                    self.current_provider,
                    self.current_model,
                    self.current_effort,
                    home_dir=self._settings_home_dir,
                )
            if pending.has_auto_compaction_enabled:
                self.renderer.notice(
                    "Automatic compaction "
                    f"{'enabled' if pending.auto_compaction_enabled else 'disabled'}."
                )
            if pending.mode is not None:
                self.current_mode = pending.mode
                self.renderer.notice(
                    "Plan mode enabled." if pending.mode == "plan" else "Build mode enabled."
                )
            if (
                pending.provider is not None
                or pending.model is not None
                or pending.mode is not None
            ):
                # A provider/model change can move the context window or
                # pricing; a mode switch changes the estimate too (plan mode
                # adds PLAN_MODE_SYSTEM_PROMPT and restricts _effective_tools()
                # to read-only, both of which CodingSession.get_session_stats()
                # folds into its estimate). Effort and auto-compaction toggles
                # affect neither, so those alone still skip the round trip.
                self.view.context = None
                await self._request_session_stats()
            self._sync_view()
            return
        message = event.error or "configure failed"
        if pending.provider is not None:
            self.renderer.command_error(f"Provider unchanged ({self.current_provider}): {message}")
        elif pending.model is not None:
            self.renderer.command_error(
                f"Model unchanged ({self.current_model or 'provider default'}): {message}"
            )
        elif pending.has_auto_compaction_enabled:
            self.renderer.command_error(f"Automatic compaction unchanged: {message}")
        elif pending.mode is not None:
            self.renderer.command_error(f"Agent mode unchanged ({self.current_mode}): {message}")
        self._update_view(
            status="error",
            input_hint=_prompt_for_mode(_InputMode.idle),
            input_mode=_InputMode.idle,
            queued_steering=len(self._queue_steering),
            queued_follow_ups=len(self._queue_follow_up),
        )

    async def _finish_current_prompt(self, event: RpcCommandFinished) -> bool:
        was_cancelled = (not event.ok) and _is_rpc_cancelled_message(event.error)
        finished_command_type = self.state.current_command_type
        self.state.current_command_id = None
        self.state.current_command_type = None
        self.state.pending_approval = None
        if self.state.token_stream_started:
            # Failed and cancelled provider turns may terminate without a
            # MessageCompleted event. Settle their partial output before the next
            # prompt can reuse the renderer's active stream.
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
        self.state.rendered_tokens = False
        if self.state.exit_requested:
            self._abandon_queued_prompts()
            self.state.cancel_requested = False
            return await self._request_shutdown()
        if finished_command_type in {"prompt", "init", "compact"}:
            await self._request_session_stats()
        pagination = self._history_pagination
        if pagination is not None and pagination.latest_reload_pending:
            await self._request_latest_history_page()
        self.state.cancel_requested = False
        self.state.status = TuiStatus.idle
        if event.ok:
            self._sync_view()
        elif was_cancelled:
            self._sync_view()
        else:
            self._update_view(
                status="error",
                input_hint=_prompt_for_mode(_InputMode.idle),
                input_mode=_InputMode.idle,
                queued_steering=len(self._queue_steering),
                queued_follow_ups=len(self._queue_follow_up),
            )
        return False

    async def _request_queue_state(self) -> None:
        try:
            await self.controller.get_queue_state()
        except Exception as exc:  # noqa: BLE001 - queue chrome is recoverable
            self.renderer.send_failed("queue state", exc)

    async def _request_session_stats(self) -> None:
        try:
            command_id = await self.controller.get_session_stats()
        except Exception as exc:  # noqa: BLE001 - stats are optional TUI chrome
            self.renderer.send_failed("session stats", exc)
            return
        self._pending_session_stats_command_ids.add(command_id)

    async def _request_session_history(self, *, command_id: str | None = None) -> str | None:
        get_messages = getattr(self.controller, "get_messages", None)
        if not callable(get_messages):
            return None
        try:
            return await cast(Callable[..., Awaitable[str]], get_messages)(
                limit=self._history_message_limit,
                allow_during_prompt=True,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - history is optional TUI chrome
            self.renderer.send_failed("session history", exc)
            return None

    async def _request_previous_history_page(self) -> None:
        pagination = self._history_pagination
        if pagination is None or pagination.next_before_entry_id is None:
            self._call_renderer_optional("history_page_loaded", has_more=False)
            return
        if (
            pagination.command_id is not None
            or pagination.latest_command_id is not None
            or pagination.newer_command_id is not None
            or pagination.latest_reload_pending
        ):
            return

        command_id = self._next_command_id("history-page")
        pagination.command_id = command_id
        try:
            await self.controller.get_messages(
                session_id=pagination.session_id,
                limit=TUI_HISTORY_PAGE_LIMIT,
                before_entry_id=pagination.next_before_entry_id,
                allow_during_prompt=True,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - preserve retry after a transient send failure
            if self._history_pagination is pagination:
                pagination.command_id = None
                self.renderer.command_error(f"Failed to load older session history: {exc}")
                self._call_renderer_optional("history_page_request_failed")
                if pagination.latest_reload_pending:
                    await self._request_latest_history_page()
            return

    async def _request_newer_history_page(self, after_entry_id: str) -> None:
        pagination = self._history_pagination
        if pagination is None or pagination.newer_command_id is not None:
            self._call_renderer_optional("history_newer_page_request_failed")
            return
        if pagination.command_id is not None or pagination.latest_command_id is not None:
            self._call_renderer_optional("history_newer_page_request_failed")
            return

        command_id = self._next_command_id("history-newer")
        pagination.newer_command_id = command_id
        try:
            await self.controller.get_messages(
                session_id=pagination.session_id,
                limit=TUI_HISTORY_PAGE_LIMIT,
                after_entry_id=after_entry_id,
                allow_during_prompt=True,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - preserve retry after send failure
            if self._history_pagination is pagination:
                pagination.newer_command_id = None
                self.renderer.command_error(f"Failed to load newer session history: {exc}")
                self._call_renderer_optional("history_newer_page_request_failed")

    async def _request_latest_history_page(self) -> None:
        pagination = self._history_pagination
        if pagination is None:
            if self._history_recovery_command_id is not None:
                return
            self._call_renderer_optional("capture_latest_history_reload")
            command_id = self._next_command_id("history-recovery")
            self._history_recovery_command_id = command_id
            self._history_recovery_report_received = False
            if await self._request_session_history(command_id=command_id) is None:
                self._history_recovery_command_id = None
                self._call_renderer_optional("latest_history_reload_failed")
                return
            return
        if pagination.latest_command_id is not None:
            return
        if (
            pagination.command_id is not None
            or pagination.newer_command_id is not None
            or self.state.current_command_type == "compact"
        ):
            pagination.latest_reload_pending = True
            return

        command_id = self._next_command_id("history-latest")
        pagination.latest_reload_pending = False
        pagination.latest_command_id = command_id
        try:
            self._call_renderer_optional("capture_latest_history_reload")
            await self.controller.get_messages(
                session_id=pagination.session_id,
                limit=TUI_HISTORY_PAGE_LIMIT,
                allow_during_prompt=True,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - preserve retry after a transient send failure
            if self._history_pagination is pagination:
                pagination.latest_command_id = None
                self.renderer.command_error(f"Failed to reload latest session history: {exc}")
                self._call_renderer_optional("history_page_request_failed")
                self._call_renderer_optional("latest_history_reload_failed")
            return

    async def _finish_history_page(self, event: RpcCommandFinished) -> None:
        pagination = self._history_pagination
        if pagination is None or event.command_id != pagination.command_id:
            return
        report = pagination.report
        pagination.command_id = None
        pagination.report = None
        try:
            if not event.ok or report is None:
                detail = event.error or "older session history completed without a result"
                self.renderer.command_error(f"Failed to load older session history: {detail}")
                self._call_renderer_optional("history_page_request_failed")
                return
            if report.session_id != pagination.session_id:
                self.renderer.command_error(
                    "Failed to load older session history: result did not match the active session."
                )
                self._call_renderer_optional("history_page_request_failed")
                return

            entries = history_entries_from_rpc_messages(report.messages)
            self._call_renderer_optional("prepend_history_entries", entries)
            pagination.next_before_entry_id = report.next_before_entry_id
            self._call_renderer_optional(
                "history_page_loaded",
                has_more=report.next_before_entry_id is not None,
            )
        finally:
            if self._history_pagination is pagination and pagination.latest_reload_pending:
                await self._request_latest_history_page()

    async def _finish_newer_history_page(self, event: RpcCommandFinished) -> None:
        pagination = self._history_pagination
        if pagination is None or event.command_id != pagination.newer_command_id:
            return
        report = pagination.newer_report
        pagination.newer_command_id = None
        pagination.newer_report = None
        try:
            if not event.ok or report is None:
                detail = event.error or "newer session history completed without a result"
                self.renderer.command_error(f"Failed to load newer session history: {detail}")
                self._call_renderer_optional("history_newer_page_request_failed")
                return
            if report.session_id != pagination.session_id:
                self.renderer.command_error(
                    "Failed to load newer session history: result did not match the active session."
                )
                self._call_renderer_optional("history_newer_page_request_failed")
                return
            entries = history_entries_from_rpc_messages(report.messages)
            append_newer_entries = getattr(
                self.renderer,
                "append_newer_history_entries",
                None,
            )
            next_before_entry_id = (
                append_newer_entries(
                    entries,
                    next_after_entry_id=report.next_after_entry_id,
                )
                if callable(append_newer_entries)
                else None
            )
            if isinstance(next_before_entry_id, str):
                pagination.next_before_entry_id = next_before_entry_id
        finally:
            if self._history_pagination is pagination and pagination.latest_reload_pending:
                await self._request_latest_history_page()

    async def _finish_latest_history_page(self, event: RpcCommandFinished) -> None:
        pagination = self._history_pagination
        if pagination is None or event.command_id != pagination.latest_command_id:
            return
        report = pagination.latest_report
        pagination.latest_command_id = None
        pagination.latest_report = None
        if not event.ok or report is None:
            detail = event.error or "latest session history completed without a result"
            self.renderer.command_error(f"Failed to reload latest session history: {detail}")
            self._call_renderer_optional("history_page_request_failed")
            self._call_renderer_optional("latest_history_reload_failed")
            return
        if pagination.session_id is None:
            # A fresh TUI hydrates an empty selected session as ``None``. The
            # first prompt creates that session in the RPC host, so a later live
            # reload is the first place the shell can learn its concrete id.
            pagination.session_id = report.session_id
        elif report.session_id != pagination.session_id:
            self.renderer.command_error(
                "Failed to reload latest session history: result did not match the active session."
            )
            self._call_renderer_optional("history_page_request_failed")
            self._call_renderer_optional("latest_history_reload_failed")
            return

        entries = history_entries_from_rpc_messages(report.messages)
        replace_latest_entries = getattr(self.renderer, "replace_latest_history_entries", None)
        if callable(replace_latest_entries) and replace_latest_entries(entries) is False:
            return
        pagination.next_before_entry_id = report.next_before_entry_id
        self._call_renderer_optional(
            "history_page_loaded",
            has_more=report.next_before_entry_id is not None,
        )

    def _handle_rpc_closed(
        self,
        signal: _RpcEventsClosed,
        *,
        pending_command_id: str | None = None,
    ) -> bool:
        self._update_view(status="error")
        if signal.error is not None:
            self.renderer.rpc_event_reader_failed(signal.error)
        if self.state.token_stream_started:
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
        if self.state.current_command_id is not None:
            self.renderer.rpc_stream_ended_before_command(self.state.current_command_id)
        elif self.pending_configures:
            command_id = next(iter(self.pending_configures))
            self.renderer.rpc_stream_ended_before_command(command_id)
        elif self.pending_session_switch is not None:
            command_id = (
                self.pending_session_switch.history_command_id
                or self.pending_session_switch.select_command_id
            )
            self.renderer.rpc_stream_ended_before_command(command_id)
        elif self.pending_session_catalog is not None:
            self.renderer.rpc_stream_ended_before_command(self.pending_session_catalog.command_id)
        elif self.pending_new_session_command_id is not None:
            self.renderer.rpc_stream_ended_before_command(self.pending_new_session_command_id)
        elif self.pending_context_status_command_id is not None:
            self.renderer.rpc_stream_ended_before_command(self.pending_context_status_command_id)
        elif pending_command_id is not None:
            self.renderer.rpc_stream_ended_before_command(pending_command_id)
        elif self.state.shutdown_command_id is not None:
            self.renderer.rpc_stream_ended_before_shutdown(self.state.shutdown_command_id)
        elif signal.error is None:
            self.renderer.rpc_stream_ended_unexpectedly()
        return True

    def _default_auth_provider(self) -> str:
        return self._latest_pending_provider() or self.current_provider

    def _latest_pending_provider(self) -> str | None:
        for pending in reversed(self.pending_configures.values()):
            if pending.provider is not None:
                return pending.provider
        return None

    def _render_help(self) -> None:
        self.renderer.help()

    def _render_event(self, event: KnownWispEvent) -> None:
        if self.state.cancel_requested:
            if isinstance(event, ErrorEvent) and _is_rpc_cancelled_message(event.message):
                return
            if isinstance(event, CompactionCompleted) and event.outcome == "cancelled":
                return
            if (
                isinstance(event, RpcCommandFinished)
                and not event.ok
                and _is_rpc_cancelled_message(event.error)
            ):
                self.state.status = TuiStatus.idle
                self.state.cancel_requested = False
                self._sync_view()
                if event.command_type == "compact":
                    self.renderer.notice("Compaction cancelled.")
                else:
                    self.renderer.cancelled()
                return
        if (
            isinstance(event, CompactionCompleted)
            and event.outcome == "failed"
            and self.state.current_command_type == "compact"
        ):
            # CodingSession emits a detailed ErrorEvent immediately before this
            # typed terminal event; avoid presenting the same failure twice.
            return
        if isinstance(event, SessionSaved):
            self._update_view(last_session=_compact_session_path(event.path))
        if isinstance(event, ErrorEvent):
            self._update_view(status="error")
        if isinstance(event, RpcCommandFinished) and not event.ok:
            self._update_view(status="error")
        self.renderer.event(event)

    def _active_operation(self) -> str:
        return "compaction" if self.state.current_command_type == "compact" else "a prompt"

    def _active_status(self) -> TuiStatus:
        if self.state.current_command_id is None and self.state.current_command_type is None:
            return TuiStatus.idle
        if self.state.current_command_type == "compact":
            return TuiStatus.compacting
        return TuiStatus.running


async def _default_prompt_reader(prompt: str) -> str:
    selected_prompt = prompt if _stdin_is_interactive() else ""
    return await anyio.to_thread.run_sync(input, selected_prompt, abandon_on_cancel=True)


def _compact_session_path(path: object) -> str:
    path_text = str(path)
    return os.path.basename(path_text) or path_text


def _is_rpc_cancelled_message(message: str | None) -> bool:
    return bool(message and message.startswith("RPC command cancelled:"))


def _is_trust_answer(text: str) -> bool:
    return text.strip().lower() in _TRUST_ANSWERS
