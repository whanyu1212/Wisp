"""TUI shell and controller-facing event loop."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from rich.console import Console

from wisp.auth.storage import (
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
    RpcCommandFinished,
    RpcCommandsReported,
    RpcMessagesReported,
    RpcSessionSelected,
    RpcSessionsReported,
    SessionSaved,
    SessionStatsReported,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.providers.catalog import (
    AmbiguousModelError,
    ModelRegistry,
    UnknownModelError,
    effective_catalog,
    startup_effort,
)
from wisp.rpc.commands import ApprovalScope
from wisp.settings import persist_user_effort
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
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    history_entries_from_rpc_messages,
    history_from_rpc_messages,
)
from wisp.tui.launch import _stdin_is_interactive
from wisp.tui.live import LiveFullscreenInputInterrupted
from wisp.tui.rendering import TuiRenderer, TuiRendererKind, create_tui_renderer
from wisp.tui.state import (
    TuiInteractionState,
    TuiStatus,
    TuiViewState,
    _coerce_input_mode,
    _input_mode_for_status,
    _InputClosed,
    _InputInterrupted,
    _InputLine,
    _InputMode,
    _prompt_for_mode,
    _prompt_for_status,
    _RpcEvent,
    _RpcEventsClosed,
    _TuiSignal,
    _view_status_for_status,
)


class TuiController(Protocol):
    """Controller surface consumed by the TUI shell."""

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str: ...

    async def compact(
        self,
        instructions: str | None = None,
        *,
        command_id: str | None = None,
    ) -> str: ...

    async def get_session_stats(self, *, command_id: str | None = None) -> str: ...

    async def get_commands(self, *, command_id: str | None = None) -> str: ...

    async def get_messages(
        self,
        *,
        session_id: str | None = None,
        limit: int = 200,
        before_entry_id: str | None = None,
        command_id: str | None = None,
    ) -> str: ...

    async def get_sessions(self, *, limit: int = 50, command_id: str | None = None) -> str: ...

    async def select_session(self, session_id: str, *, command_id: str | None = None) -> str: ...

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
        command_id: str | None = None,
    ) -> str: ...

    def events(self) -> AsyncIterator[KnownWispEvent]: ...

    async def close(self) -> None: ...


PromptReader = Callable[[str], Awaitable[str]]
_TRUST_ANSWERS = {"y", "yes", "n", "no"}


@dataclass(frozen=True)
class _PendingConfigure:
    command_id: str
    provider: str | None = None
    model: str | None = None
    reset_model: bool = False
    effort: str | None = None
    has_effort: bool = False


@dataclass
class _PendingSessionCatalog:
    command_id: str
    report: RpcSessionsReported | None = None


@dataclass
class _PendingSessionSwitch:
    requested_session_id: str
    select_command_id: str
    selected: RpcSessionSelected | None = None
    history_command_id: str | None = None
    history_report: RpcMessagesReported | None = None


@dataclass
class _HistoryPagination:
    """Cursor and in-flight command state for one mounted transcript history."""

    session_id: str | None
    next_before_entry_id: str | None
    command_id: str | None = None
    report: RpcMessagesReported | None = None


def _default_model_for(models: ModelRegistry, provider_name: str) -> str | None:
    """Return ``provider_name``'s catalog ``default_model``, or ``None`` if unknown."""

    for entry in models.providers():
        if entry.name == provider_name:
            return entry.default_model
    return None


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
        settings_home_dir: Path | None = None,
    ) -> None:
        self.controller = controller
        self.renderer = (
            renderer if renderer is not None else create_tui_renderer(TuiRendererKind.line, console)
        )
        self.prompt_reader = prompt_reader or _default_prompt_reader
        self.state = state or TuiInteractionState()
        self.view = TuiViewState(provider=provider, model=model)
        self.current_provider = provider
        self.current_model = model
        self.command_catalog = DEFAULT_TUI_COMMAND_CATALOG
        self.models = ModelRegistry(effective_catalog())
        # Mirrors wisp.providers.catalog.startup_effort's filtering on the RPC
        # side (see CodingSession construction in cli/rpc.py and cli/__init__.py):
        # the TUI resolves its own config.effort independently via its own
        # WispConfig.from_env() call in the same process launch, so a stale
        # tier from a different provider/model's vocabulary must be dropped
        # here too -- otherwise the picker would seed it into the "current"
        # row (see ModelPicker.show) and an untouched Enter could re-submit an
        # incompatible tier the RPC side had already filtered out at its own
        # startup.
        self.current_effort = startup_effort(
            self.models,
            provider_name=provider,
            model=model,
            default_model=_default_model_for(self.models, provider),
            effort=effort,
        )
        self.pending_configures: dict[str, _PendingConfigure] = {}
        self.pending_session_catalog: _PendingSessionCatalog | None = None
        self.pending_session_switch: _PendingSessionSwitch | None = None
        self._history_pagination: _HistoryPagination | None = None
        self._ignored_history_page_commands: set[str] = set()
        self._call_renderer_optional(
            "set_history_page_request_hook",
            self._request_previous_history_page,
        )
        self.auth_store = JsonAuthStore(auth_path or default_auth_path())
        # Overrides ~/.wisp for /model picker effort persistence in tests; None
        # in production so persist_user_effort resolves the real home dir.
        self._settings_home_dir = settings_home_dir
        # Credential commands read auth_store lazily (it is rebound on a trusted-
        # project rebuild) and the default provider from live shell state.
        self._auth = AuthCommands(
            self.renderer,
            lambda: self.auth_store,
            self._default_auth_provider,
        )

    async def run(self) -> None:
        """Run the interactive prompt/event loop."""

        self.renderer.startup()
        self._sync_view()
        send, receive = anyio.create_memory_object_stream[_TuiSignal](100)
        async with anyio.create_task_group() as task_group, send, receive:
            task_group.start_soon(self._read_rpc_events, send.clone())
            if await self._hydrate_session_history(receive):
                task_group.cancel_scope.cancel()
                return
            if await self._hydrate_command_catalog(receive):
                task_group.cancel_scope.cancel()
                return
            await self._request_session_stats()
            task_group.start_soon(self._read_inputs, send.clone())
            while True:
                signal = await receive.receive()
                should_exit = await self._handle_signal(signal)
                if should_exit:
                    task_group.cancel_scope.cancel()
                    return

    def _sync_view(self) -> None:
        self.view.provider = self.current_provider
        self.view.model = self.current_model
        mode = _input_mode_for_status(self.state.status)
        self._update_view(
            status=_view_status_for_status(self.state.status),
            input_hint=_prompt_for_status(self.state.status),
            input_mode=mode,
            queued_follow_ups=len(self.state.queued_prompts),
        )

    def _update_view(
        self,
        *,
        status: str | None = None,
        input_hint: str | None = None,
        input_mode: _InputMode | None = None,
        queued_follow_ups: int | None = None,
        last_session: str | None = None,
    ) -> None:
        if status is not None:
            self.view.status = status
        if input_hint is not None:
            self.view.input_hint = input_hint
        if input_mode is not None:
            self.view.input_mode = input_mode.value
        if queued_follow_ups is not None:
            self.view.queued_follow_ups = queued_follow_ups
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
                except (KeyboardInterrupt, LiveFullscreenInputInterrupted):
                    await send.send(_InputInterrupted(mode=self._submitted_input_mode(mode)))
                    continue
                await send.send(_InputLine(text=text, mode=self._submitted_input_mode(mode)))

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
        command_id = await self._request_session_history()
        if command_id is None:
            return False
        rendered = False
        while True:
            signal = await receive.receive()
            if isinstance(signal, _RpcEvent):
                event = signal.event
                if isinstance(event, RpcMessagesReported) and event.command_id == command_id:
                    if not rendered:
                        self._render_history_entries(
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

    def _publish_command_catalog(self) -> None:
        update_catalog = getattr(self.renderer, "command_catalog_updated", None)
        if callable(update_catalog):
            cast(Callable[[TuiCommandCatalog], None], update_catalog)(self.command_catalog)

    def _render_history(self, messages: tuple[HistoricalTranscriptMessage, ...]) -> None:
        render_history = getattr(self.renderer, "render_history", None)
        if not callable(render_history):
            return
        cast(Callable[[tuple[HistoricalTranscriptMessage, ...]], None], render_history)(messages)

    def _render_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
        *,
        text_fallback: tuple[HistoricalTranscriptMessage, ...],
    ) -> None:
        render_entries = getattr(self.renderer, "render_history_entries", None)
        if callable(render_entries):
            cast(Callable[[tuple[HistoricalTranscriptEntry, ...]], None], render_entries)(entries)
            return
        self._render_history(text_fallback)

    def _activate_history_pagination(self, report: RpcMessagesReported) -> None:
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
        if pagination is not None and pagination.command_id is not None:
            self._ignored_history_page_commands.add(pagination.command_id)
        self._history_pagination = None
        self._call_renderer_optional("history_page_loaded", has_more=False)

    async def _handle_signal(self, signal: _TuiSignal) -> bool:
        if isinstance(signal, _InputLine):
            return await self._handle_input_line(signal)
        if isinstance(signal, _InputClosed):
            return await self._handle_input_closed(signal)
        if isinstance(signal, _InputInterrupted):
            return await self._handle_input_interrupted(signal)
        if isinstance(signal, _RpcEvent):
            return await self._handle_rpc_event(signal.event)
        return self._handle_rpc_closed(signal)

    def _clear_queued_prompts(self) -> None:
        # Single seam for dropping queued follow-ups: also tell the renderer so it
        # can reclaim any pending large-paste compact echoes tied to those dropped
        # prompts (only these paths abandon the queue — denying an approval leaves
        # queued follow-ups, and their echoes, intact).
        self.state.queued_prompts.clear()
        self.renderer.queued_prompts_cleared()

    def _queue_prompt(self, prompt: str) -> None:
        """Retain and queue one accepted follow-up through a single seam."""

        self._record_prompt_acceptance(prompt)
        self.state.queued_prompts.append(prompt)
        self._update_view(queued_follow_ups=len(self.state.queued_prompts))
        self.renderer.queued_follow_up(len(self.state.queued_prompts))

    def _record_prompt_acceptance(self, prompt: str) -> None:
        """Notify renderers that implement the optional prompt-history hook."""

        prompt_accepted = getattr(self.renderer, "prompt_accepted", None)
        if callable(prompt_accepted):
            prompt_accepted(prompt)

    async def _handle_input_line(self, signal: _InputLine) -> bool:
        text = signal.text
        has_content = bool(text.strip())
        if self.state.status is TuiStatus.exiting:
            return False
        try:
            command = parse_tui_slash_command(text, catalog=self.command_catalog)
        except TuiSlashCommandError as exc:
            self.renderer.command_error(str(exc))
            return False
        if command is not None:
            return await self._handle_slash_command(command)
        if self._session_operation_active():
            if has_content:
                self.renderer.command_error(
                    f"Cannot submit prompts while {self._session_operation_name()}."
                )
            return False
        if (
            self.state.status is TuiStatus.confirming_all_tools
            and self.state.pending_approval is not None
        ):
            if signal.mode is _InputMode.all_tools_confirmation:
                return await self._answer_all_tools_confirmation(text)
            if has_content and self.state.current_command_id is not None:
                self._queue_prompt(text)
            return False
        if self.state.pending_trust is not None:
            if signal.mode is _InputMode.trust or _is_trust_answer(text):
                return await self._answer_pending_trust(text)
            if has_content and self.state.current_command_id is not None:
                self._queue_prompt(text)
            return False
        if self.state.pending_approval is not None:
            if signal.mode is _InputMode.approval:
                return await self._answer_pending_approval(text, exit_after_denial=False)
            if has_content and self.state.current_command_id is not None:
                self._queue_prompt(text)
            return False
        if not has_content:
            return False
        if self.state.current_command_id is not None:
            self._queue_prompt(text)
            return False
        self._record_prompt_acceptance(text)
        return await self._start_prompt(text)

    async def _handle_slash_command(self, command: TuiSlashCommand) -> bool:
        if command.name is TuiSlashCommandName.help:
            self._render_help()
            return False
        if command.name is TuiSlashCommandName.quit:
            return await self._handle_quit()
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
        if self.state.current_command_id is not None:
            operation = self._active_operation()
            self.renderer.command_error(f"Cannot run slash commands while {operation} is running.")
            return False
        if self._session_operation_active():
            self.renderer.command_error(
                f"Cannot run slash commands while {self._session_operation_name()}."
            )
            return False
        if command.name is TuiSlashCommandName.compact:
            if self.state.status is not TuiStatus.idle:
                self.renderer.command_error("Cannot compact while another operation is active.")
                return False
            instructions = " ".join(command.args).strip() or None
            return await self._start_compaction(instructions)
        if command.name is TuiSlashCommandName.auth:
            self._auth.status(command.args)
            return False
        if command.name is TuiSlashCommandName.login:
            await self._auth.login(command.args)
            return False
        if command.name is TuiSlashCommandName.logout:
            self._auth.logout(command.args)
            return False
        if command.name is TuiSlashCommandName.provider:
            await self._handle_provider_command(command.args)
            return False
        if command.name is TuiSlashCommandName.model:
            await self._handle_model_command(command.args)
            return False
        if command.name is TuiSlashCommandName.resume:
            await self._handle_resume_command(command.args)
            return False
        self.renderer.command_error(f"Unknown command: /{command.name.value}")
        return False

    async def _handle_resume_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self.renderer.command_error("Usage: /resume [session-id]")
            return
        if not args:
            self._call_renderer_optional("session_catalog_started")
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
        # Guard the composer before awaiting a potentially backpressured
        # transport. Picker selections may already have entered this lifecycle;
        # renderer implementations intentionally make the repeated start
        # idempotent.
        self._clear_history_pagination()
        self._call_renderer_optional("session_switch_started", session_id)
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
        return self.pending_session_catalog is not None or self.pending_session_switch is not None

    def _session_operation_name(self) -> str:
        if self.pending_session_switch is not None:
            return "a session switch is in progress"
        return "the session catalog is loading"

    def _call_renderer_optional(self, method_name: str, *args: object, **kwargs: object) -> None:
        method = getattr(self.renderer, method_name, None)
        if callable(method):
            method(*args, **kwargs)

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
            # _handle_rpc_configure_command unconditionally resets agent.effort
            # to None whenever a configure carries `provider` and no explicit
            # `effort` (see its has_provider branch) -- mirror that here so
            # current_effort/the persisted setting don't go stale relative to
            # what the RPC agent is actually using on the next prompt.
            has_effort=True,
        )
        self._update_view(status="configuring")
        self.renderer.notice(f"Configuring provider: {provider}")

    async def _handle_model_command(self, args: tuple[str, ...]) -> None:
        if len(args) > 2:
            self.renderer.command_error("Usage: /model [model] [effort]")
            return
        if not args:
            self.renderer.model_picker_request(
                self.models.providers(),
                current_provider=self.current_provider,
                current_model=self.current_model,
                current_effort=self.current_effort,
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
        if effort is not None:
            effort = self._validated_effort(provider=provider, model=model, effort=effort)
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
            # _handle_rpc_configure_command unconditionally resets agent.effort
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

    def _validated_effort(self, *, provider: str | None, model: str, effort: str) -> str | None:
        """Return ``effort`` if valid for ``model``'s resolved provider, else warn and drop it.

        The picker only ever offers effort tiers a model's catalog entry
        actually lists (see ``ModelPicker.show``'s seeding filter), but a
        typed ``/model <id> <effort>`` bypasses the picker entirely -- without
        this check, an unsupported tier (e.g. a tier valid for one provider's
        vocabulary but not another's, or a model the catalog deliberately
        omits from ``effort_levels`` like claude-haiku-4-5) would reach the
        RPC agent and, eventually, that provider's API unvalidated. Permissive
        like the rest of ``/model``'s validation -- an unresolvable model
        (unknown to the catalog, or ambiguous when ``provider`` isn't given)
        skips the check entirely rather than blocking the command, since a
        brand-new model ahead of a catalog update must still work. This
        applies equally to an explicitly provider-qualified unknown model
        (``/model openai::gpt-6 high``) as to a bare one -- ``knows_model`` is
        checked regardless of whether ``provider`` came from the caller or
        from ``resolve()``, since ``supports_effort`` alone can't distinguish
        "model known, tier not listed" from "model unknown to this provider."
        """

        target_provider = provider
        if target_provider is None:
            try:
                target_provider, _entry = self.models.resolve(model, prefer=self.current_provider)
            except (UnknownModelError, AmbiguousModelError):
                return effort
        if not self.models.knows_model(target_provider, model):
            return effort
        if self.models.supports_effort(target_provider, model, effort):
            return effort
        self.renderer.command_error(
            f"Effort {effort!r} is not supported by {model} on {target_provider}; "
            "configuring model without it."
        )
        return None

    async def _handle_input_closed(self, signal: _InputClosed) -> bool:
        self.state.input_closed = True
        if self.state.status is TuiStatus.exiting:
            return False
        self.state.exit_requested = True
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
            self._clear_queued_prompts()
            self._update_view(queued_follow_ups=0)
            if self.state.current_command_type == "compact":
                self.renderer.notice("input closed; finishing compaction")
            else:
                self.renderer.input_closed_finishing_prompt()
            return False
        return await self._request_shutdown()

    async def _handle_input_interrupted(self, signal: _InputInterrupted) -> bool:
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
        self.renderer.input_cleared()
        return False

    async def _handle_quit(self) -> bool:
        self.state.exit_requested = True
        self._clear_queued_prompts()
        self._update_view(queued_follow_ups=0)
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

    async def _start_prompt(self, prompt: str) -> bool:
        self.state.status = TuiStatus.running
        self.state.current_command_type = "prompt"
        self.state.pending_approval = None
        self.state.cancel_requested = False
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        self._sync_view()
        self.renderer.prompt_submitted(prompt)
        self.renderer.running()
        try:
            command_id = await self.controller.prompt(prompt)
        except Exception as exc:
            self.state.current_command_type = None
            self._update_view(status="error")
            self.renderer.send_failed("prompt", exc)
            return True
        self.state.current_command_id = command_id
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
        self._clear_queued_prompts()
        self.state.cancel_requested = True
        self._update_view(status="cancelling", queued_follow_ups=0)
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
        self.state.status = TuiStatus.exiting
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
        if approved is None and normalized in {"a", "all", "yolo"}:
            self.state.status = TuiStatus.confirming_all_tools
            self._sync_view()
            self.renderer.approval_all_confirmation(approval)
            return False
        selected_scope: ApprovalScope = scope or (
            "tool_session" if normalized in {"t", "tool"} else "once"
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

    async def _answer_all_tools_confirmation(self, answer: str) -> bool:
        approval = self.state.pending_approval
        if approval is None:
            return False
        if answer.strip().lower() in {"y", "yes", "confirm-all"}:
            return await self._answer_pending_approval(
                "",
                approved=True,
                scope="all_session",
                exit_after_denial=False,
            )
        self.state.status = TuiStatus.waiting_for_approval
        self._sync_view()
        self.renderer.approval_request(approval)
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
                session_switch.history_report = event
                return False

        pagination = self._history_pagination
        if (
            isinstance(event, RpcMessagesReported)
            and pagination is not None
            and event.command_id == pagination.command_id
        ):
            pagination.report = event
            return False

        context_updated = self.view.update_context_from_event(event)
        if context_updated:
            self._update_view()
        if isinstance(event, (ContextEstimated, SessionStatsReported)):
            return False
        if isinstance(event, ProviderRetrying):
            self._update_view(
                status=(
                    f"retrying {event.attempt}/{event.max_attempts} in {event.delay_seconds:.1f}s"
                ),
                input_hint=_prompt_for_status(self._active_status()),
                input_mode=_InputMode.running,
                queued_follow_ups=len(self.state.queued_prompts),
            )
            self.renderer.event(event)
            return False
        if isinstance(event, MessageStarted):
            self._sync_view()
        if isinstance(event, MessageDelta) and event.content_kind == "text":
            self.state.token_stream_started = True
            self.state.rendered_tokens = True
            self.renderer.token_delta(event.delta)
            return False
        if self.state.token_stream_started:
            self.renderer.end_token_stream()
            self.state.token_stream_started = False
        if isinstance(event, MessageCompleted):
            suppress_completed_message = self.state.rendered_tokens
            self.state.rendered_tokens = False
            if suppress_completed_message:
                return False
        if isinstance(event, ToolApprovalRequested):
            self.state.pending_approval = event
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
            # and /provider,/model,/auth,/login stop showing the untrusted-startup ones.
            self.current_provider = event.provider
            self.current_model = event.model
            # Adopt the RPC agent's own already-filtered, authoritative effort
            # (see _rebuild_agent_for_trusted_project in wisp.cli.rpc) rather
            # than re-deriving it from self.current_effort here -- that local
            # copy was itself already filtered once, against the
            # untrusted-startup provider/model, in __init__. A tier invalid
            # there but valid for the trusted project's provider/model would
            # already be gone from it and unrecoverable, the same class of bug
            # ProjectConfigApplied.effort's docstring explains in more detail.
            self.current_effort = event.effort
            self.auth_store = JsonAuthStore(event.auth_path)
            self.renderer.notice(
                f"Applied trusted project config: provider {event.provider}"
                f"{f', model {event.model}' if event.model else ''}."
            )
            self._sync_view()
            return False

        if isinstance(event, ModelProviderAutoSwitched):
            # A model-only /model <id> resolved to a different provider server-side
            # (see _auto_switch_provider_for_model in wisp.cli.rpc). Record the
            # provider on the still-pending configure so _finish_pending_configure
            # adopts it exactly like an explicit /provider request would, instead of
            # only updating current_model and leaving current_provider stale.
            pending = self.pending_configures.get(event.command_id)
            if pending is not None:
                self.pending_configures[event.command_id] = replace(
                    pending, provider=event.provider
                )
            return False

        if isinstance(event, RpcCommandFinished):
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
            if event.command_id in self.pending_configures:
                await self._finish_pending_configure(event)
                return False
            if event.command_id == self.state.shutdown_command_id:
                self._render_event(event)
                return True
            if event.command_id == self.state.current_command_id:
                self._render_event(event)
                return await self._finish_current_prompt(event)
        self._render_event(event)
        return False

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
            try:
                pending.history_command_id = await self.controller.get_messages(
                    limit=TUI_HISTORY_MESSAGE_LIMIT
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

        label = selected.session_name or _compact_session_path(selected.session_path)
        entries = history_entries_from_rpc_messages(pending.history_report.messages)
        self._call_renderer_optional(
            "replace_history_entries",
            entries,
            session_label=label,
        )
        self._activate_history_pagination(pending.history_report)
        self.view.context = None
        self.view.cost = None
        self._update_view(last_session=label)
        self._finish_session_switch_ui()
        await self._request_session_stats()

    def _fail_committed_session_hydration(self, detail: str) -> None:
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

    async def _finish_pending_configure(self, event: RpcCommandFinished) -> None:
        pending = self.pending_configures.pop(event.command_id)
        if event.ok:
            if pending.provider is not None:
                self.current_provider = pending.provider
                if pending.reset_model:
                    self.current_model = None
                    self.renderer.notice(
                        f"Provider set to {pending.provider}; model reset to provider default."
                    )
                else:
                    self.renderer.notice(f"Provider set to {pending.provider}")
            if pending.model is not None:
                self.current_model = pending.model
                self.renderer.notice(f"Model set to {pending.model}")
            if pending.has_effort:
                self.current_effort = pending.effort
                persist_user_effort(pending.effort, home_dir=self._settings_home_dir)
            self.view.context = None
            self._sync_view()
            await self._request_session_stats()
            return
        message = event.error or "configure failed"
        if pending.provider is not None:
            self.renderer.command_error(f"Provider unchanged ({self.current_provider}): {message}")
        elif pending.model is not None:
            self.renderer.command_error(
                f"Model unchanged ({self.current_model or 'provider default'}): {message}"
            )
        self._update_view(
            status="error",
            input_hint=_prompt_for_mode(_InputMode.idle),
            input_mode=_InputMode.idle,
            queued_follow_ups=len(self.state.queued_prompts),
        )

    async def _finish_current_prompt(self, event: RpcCommandFinished) -> bool:
        was_cancelled = (not event.ok) and _is_rpc_cancelled_message(event.error)
        finished_command_type = self.state.current_command_type
        self.state.current_command_id = None
        self.state.current_command_type = None
        self.state.pending_approval = None
        self.state.token_stream_started = False
        self.state.rendered_tokens = False
        if finished_command_type in {"prompt", "compact"}:
            await self._request_session_stats()
        if self.state.exit_requested or (not event.ok and finished_command_type != "compact"):
            self._clear_queued_prompts()
        if self.state.exit_requested:
            self.state.cancel_requested = False
            return await self._request_shutdown()
        if self.state.queued_prompts:
            queued_prompt = self.state.queued_prompts.popleft()
            self._update_view(
                status="running queued follow-up",
                input_hint=_prompt_for_mode(_InputMode.running),
                input_mode=_InputMode.running,
                queued_follow_ups=len(self.state.queued_prompts),
            )
            self.renderer.running_queued_follow_up(len(self.state.queued_prompts))
            return await self._start_prompt(queued_prompt)
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
                queued_follow_ups=0,
            )
        return False

    async def _request_session_stats(self) -> None:
        try:
            await self.controller.get_session_stats()
        except Exception as exc:  # noqa: BLE001 - stats are optional TUI chrome
            self.renderer.send_failed("session stats", exc)

    async def _request_session_history(self) -> str | None:
        get_messages = getattr(self.controller, "get_messages", None)
        if not callable(get_messages):
            return None
        try:
            return await cast(Callable[..., Awaitable[str]], get_messages)(
                limit=TUI_HISTORY_MESSAGE_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - history is optional TUI chrome
            self.renderer.send_failed("session history", exc)
            return None

    async def _request_previous_history_page(self) -> None:
        pagination = self._history_pagination
        if pagination is None or pagination.next_before_entry_id is None:
            self._call_renderer_optional("history_page_loaded", has_more=False)
            return
        if pagination.command_id is not None:
            return

        command_id = f"history-page-{uuid4().hex}"
        pagination.command_id = command_id
        try:
            await self.controller.get_messages(
                session_id=pagination.session_id,
                limit=TUI_HISTORY_MESSAGE_LIMIT,
                before_entry_id=pagination.next_before_entry_id,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - preserve retry after a transient send failure
            if self._history_pagination is pagination:
                pagination.command_id = None
                self.renderer.command_error(f"Failed to load older session history: {exc}")
                self._call_renderer_optional("history_page_request_failed")
            return

    async def _finish_history_page(self, event: RpcCommandFinished) -> None:
        pagination = self._history_pagination
        if pagination is None or event.command_id != pagination.command_id:
            return
        report = pagination.report
        pagination.command_id = None
        pagination.report = None
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

        entries = history_entries_from_rpc_messages(
            report.messages,
            include_missing_tool_cards=False,
        )
        self._call_renderer_optional("prepend_history_entries", entries)
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
                self._clear_queued_prompts()
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
