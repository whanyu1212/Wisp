"""The `TuiRenderer` implementation backed by the Textual app.

`TextualTuiRenderer` is the adapter `TuiShell` drives: it translates the shared,
framework-agnostic renderer protocol (`view_updated`, `event`, `approval_request`,
token streaming, …) into calls on a `TextualTui`. It owns only its own progress
and tool-timing bookkeeping and reaches the app exclusively through the app's
public surface, so it lives here rather than inside the app module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from textual.widget import Widget

from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    AgentCompleted,
    CompactionCompleted,
    CompactionStarted,
    ErrorEvent,
    JsonObject,
    KnownWispEvent,
    MessageCompleted,
    MessageStarted,
    ProviderRetrying,
    RpcCommandFinished,
    RpcModelCatalogSnapshot,
    RpcSessionSummary,
    RpcSkillCatalogSnapshot,
    SessionStats,
    SkillInvoked,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
    TrustResolved,
    TurnStarted,
)
from wisp.tool_presentation import tool_result_status
from wisp.tui.commands import TuiCommandCatalog
from wisp.tui.connections import ConnectionProviderStatus
from wisp.tui.history import (
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    HistoryHydrationPolicy,
)
from wisp.tui.input_types import TuiSubmission
from wisp.tui.process_lifecycle import (
    ProcessCallIdentity,
    ProcessLifecycle,
    ProcessOperation,
    historical_process_observation,
    process_call_identity,
)
from wisp.tui.rendering import (
    TuiViewSnapshot,
    _compaction_completed_text,
    _compaction_started_text,
    _truncate_to_cell_width,
)
from wisp.tui.textual_history import TextualHistoryController
from wisp.tui.tool_output import full_tool_result_for_display, render_tool_result
from wisp.tui.update_types import UpdatePromptAction
from wisp.update_check import UpdateAvailable

if TYPE_CHECKING:
    from wisp.tui.textual_app import TextualTui
    from wisp.tui.widgets import LineMessage

_RETRY_REASON_LABELS = {
    "network": "network error",
    "timeout": "request timed out",
    "rate_limit": "rate limited",
    "server_error": "server error",
    "transient_http": "temporary HTTP error",
}


def _retry_progress_label(event: ProviderRetrying) -> str:
    """Return compact, human-readable retry progress for the status footer."""

    provider = _truncate_to_cell_width(event.provider, 20)
    reason = _RETRY_REASON_LABELS[event.reason]
    status = f" ({event.status_code})" if event.status_code is not None else ""
    return (
        f"Retrying {provider} · {event.attempt}/{event.max_attempts} "
        f"in {event.delay_seconds:.1f}s · {reason}{status}"
    )


class TextualTuiRenderer:
    """Renderer adapter consumed by `TuiShell` and backed by `TextualTui`."""

    history_hydration_policy = HistoryHydrationPolicy.COMPLETE

    def __init__(self, app: TextualTui) -> None:
        self.app = app
        # Mode the shell last reported via view_updated(); this is the mode in
        # effect while the user types the next line.
        self._visible_input_mode = "idle"
        self._visible_cwd = ""
        # Mode captured at the instant a line was submitted. It can differ from
        # the mode the shell polled when read_prompt() began waiting (e.g. a
        # tool approval that arrived mid-line), so the shell reconciles against
        # it via consume_submitted_input_mode().
        self._submitted_input_mode: str | None = None
        self._prompt_widgets: list[tuple[str, LineMessage | None]] = []
        app.set_submit_hook(self._capture_submitted_input_mode)
        # call_id → request event timestamp, so a tool card can show the true
        # wall-clock duration (result.timestamp − request.timestamp) when it
        # resolves. Every WispEvent carries a UTC timestamp, so this needs no clock.
        self._tool_started: dict[str, datetime] = {}
        # call_id → the request's arguments, retained so the result renderer can
        # build tool-aware detail (e.g. an edit diff needs the oldText/newText
        # hunks, which travel only on ToolCallRequested, not the result event).
        # Popped when the call resolves, mirroring _tool_started's lifecycle so
        # neither map grows across a session.
        self._tool_arguments: dict[str, JsonObject] = {}
        self._process_calls: dict[str, ProcessCallIdentity] = {}
        self._process_lifecycles: dict[str, ProcessLifecycle] = {}
        self._process_started: dict[str, datetime] = {}
        self._denied_process_calls: dict[str, ProcessOperation] = {}
        self._history = TextualHistoryController(app)
        app.set_live_widget_evicted_hook(self._forget_live_widget)
        app.set_history_window_hooks(
            shift_older=self._history.shift_older,
            shift_newer=self._history.shift_newer,
            show_oldest=self._history.show_oldest,
            show_latest=self._history.show_latest,
        )
        self._progress_active = False
        self._progress_turn: int | None = None
        self._response_started = False
        self._retry_attempt = 0
        self._stream_completion_pending = False
        self._overflow_recovery_failed = False

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        self._visible_input_mode = snapshot.input_mode
        # Rebuild the `@`-picker corpus only when the project root actually moves.
        # view_updated fires on every snapshot, and the walk is a threaded worker —
        # kicking one off per redraw would thrash the pool for no benefit.
        if snapshot.cwd != self._visible_cwd:
            self.app.load_file_suggestions(snapshot.cwd)
        self._visible_cwd = snapshot.cwd
        self.app.set_input_hint(snapshot.input_hint)
        self.app.set_status(snapshot)
        if snapshot.input_mode not in {"running", "approval", "trust"}:
            self._finish_progress(force=False)
        if snapshot.input_mode not in {"approval", "trust"}:
            self.app.hide_decision()

    def project_auth_path_changed(self, auth_path: Path) -> None:
        """Re-protect and re-index after a trusted project moved the credential file.

        Deferred trust (``project_trusted=None``) means the RPC side can adopt a
        project's own ``auth_path`` mid-session, after the picker already captured
        its startup policy. Without this the stale snapshot would keep offering the
        new credential filename for mention while the agent's tool context protects
        it. Update the policy first, then rebuild the corpus with it.
        """

        self.app.set_picker_auth_path(auth_path)
        if self._visible_cwd:
            self.app.load_file_suggestions(self._visible_cwd)

    def set_update_action_hook(
        self,
        hook: Callable[[UpdatePromptAction, UpdateAvailable], Awaitable[None]],
    ) -> None:
        self.app.set_update_action_hook(hook)

    def update_available(self, update: UpdateAvailable, *, automatic_install: bool) -> None:
        if automatic_install:
            self.app.offer_update(update)
            return
        self.notice(
            f"Wisp {update.latest_version} is available (current {update.current_version}). "
            "Update it with the package manager that installed Wisp."
        )

    def update_operation_started(self, update: UpdateAvailable) -> None:
        self.app.update_operation_started(update)

    def update_operation_finished(self, *, installed: bool, restarting: bool) -> None:
        self.app.update_operation_finished(installed=installed, restarting=restarting)

    def _begin_progress(self) -> None:
        self._stream_completion_pending = False
        self._progress_active = True
        self._progress_turn = None
        self._response_started = False
        self._retry_attempt = 0
        self.app.restart_working_indicator()

    def _finish_progress(self, *, after_stream: bool = False, force: bool = True) -> None:
        self._progress_active = False
        self._progress_turn = None
        self._response_started = False
        self._retry_attempt = 0
        if after_stream:
            if self._stream_completion_pending:
                return
            self._stream_completion_pending = True
            self.app.hide_working_indicator_after_stream()
        elif force or not self._stream_completion_pending:
            self._stream_completion_pending = False
            self.app.hide_working_indicator()

    def _suspend_progress(self) -> None:
        self.app.hide_working_indicator()

    def _show_activity(self, label: str) -> None:
        """Relabel an active command heartbeat without creating one for idle UI work."""

        if self._progress_active:
            self.app.show_activity_indicator(label)

    def _turn_started(self, turn: int) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is not None and turn <= self._progress_turn:
            return
        self._progress_turn = turn
        self._response_started = False
        self._retry_attempt = 0
        self.app.renew_working_indicator()

    def _provider_retrying(self, event: ProviderRetrying) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is None or event.turn > self._progress_turn:
            self._progress_turn = event.turn
            self._response_started = False
            self._retry_attempt = 0
        elif event.turn < self._progress_turn:
            return
        if self._response_started or event.attempt <= self._retry_attempt:
            return
        self._retry_attempt = event.attempt
        self.app.show_retry_indicator(_retry_progress_label(event))

    def _message_started(self, turn: int) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is not None and turn < self._progress_turn:
            return
        if self._progress_turn == turn and self._response_started:
            return
        self._progress_turn = turn
        self._response_started = True
        self._retry_attempt = 0
        self.app.renew_working_indicator()

    def _forget_live_widget(self, widget: Widget) -> None:
        """Release history and process lifecycle state after bounded UI eviction."""

        self._history.forget_live_widget(widget)
        process_id = getattr(widget, "process_id", None)
        if not isinstance(process_id, str):
            return
        self._process_lifecycles.pop(process_id, None)
        self._process_started.pop(process_id, None)

    def _tool_elapsed(self, call_id: str, finished: datetime) -> float | None:
        # True wall-clock duration for a resolving tool call: result timestamp −
        # request timestamp. Pops the start time so the map doesn't grow and a
        # duplicate result (denial then error result for the same call) doesn't
        # double-count. Returns None when the request was never seen (e.g. resume),
        # leaving the card timer-less rather than showing a bogus duration.
        started = self._tool_started.pop(call_id, None)
        if started is None:
            return None
        return (finished - started).total_seconds()

    def _abort_pending_tools(self, detail: str = "cancelled") -> None:
        # A turn ended without results (cancel / failure / stream death): drain any
        # still-pending tool cards and forget their request timestamps and retained
        # arguments so neither a spinning card nor stale per-call state leaks into
        # the next turn. A missing poll result does not establish process termination.
        for call_id, identity in self._process_calls.items():
            lifecycle = self._process_lifecycles.get(identity.process_id)
            if lifecycle is not None:
                denied_operation = self._denied_process_calls.get(call_id)
                presentation = (
                    lifecycle.deny(denied_operation)
                    if denied_operation is not None
                    else lifecycle.interrupt(identity.operation)
                )
                self.app.resolve_process_call(call_id, presentation)
        self._process_calls.clear()
        self._denied_process_calls.clear()
        self._tool_started.clear()
        self._tool_arguments.clear()
        self.app.fail_pending_tool_calls(detail)

    def _capture_submitted_input_mode(self) -> str:
        self._submitted_input_mode = self._visible_input_mode
        return self._visible_input_mode

    def consume_submitted_input_mode(self, fallback: str) -> str:
        """Return and clear the mode captured when the last line was accepted."""

        mode = self._submitted_input_mode or fallback
        self._submitted_input_mode = None
        return mode

    def startup(self) -> None:
        # Textual renders identity as a disposable empty state. Keeping startup()
        # as a no-op preserves the shared renderer protocol without putting the
        # wordmark into scrollback; line/fullscreen keep their own startup output.
        pass

    def help(self) -> None:
        if self.app.is_running:
            self.app.action_toggle_contextual_help()

    def notice(self, message: str) -> None:
        self.app.write_notice(message)

    def command_error(self, message: str) -> None:
        self.app.write_error(message)

    def prompt_submitted(self, prompt: str | TuiSubmission) -> None:
        # Echo a compact line for large pastes (marker kept) while the model still
        # received the full expanded text via controller.prompt(prompt).
        content = prompt.content if isinstance(prompt, TuiSubmission) else prompt
        if isinstance(prompt, TuiSubmission):
            display = prompt.display
            if prompt.display != prompt.content:
                self.app.compact_echo_for(prompt.content)
        else:
            display = self.app.compact_echo_for(prompt)
        if isinstance(prompt, TuiSubmission):
            self.app.resolve_submission(int(prompt.id))
        widget = self.app.write_user(display)
        self._prompt_widgets.append((content, widget))
        del self._prompt_widgets[:-100]
        self._history.record_live_message("user", content, widget=widget)

    def prompt_accepted(self, prompt: str) -> None:
        self.app.record_prompt(prompt)

    def resolve_submission(self, submission_id: int) -> None:
        self.app.resolve_submission(submission_id)

    def restore_submissions(self, submissions: tuple[TuiSubmission, ...]) -> bool:
        return self.app.restore_submissions(submissions)

    def report_unsent_submissions(self, submissions: tuple[TuiSubmission, ...]) -> None:
        self.app.report_unsent_submissions(submissions)

    def discard_live_prompt(self, prompt: str) -> None:
        self._pop_prompt_widget(prompt)
        self._history.discard_live_message("user", prompt)

    def _pop_prompt_widget(self, prompt: str) -> LineMessage | None:
        for index in range(len(self._prompt_widgets) - 1, -1, -1):
            candidate_prompt, widget = self._prompt_widgets[index]
            if candidate_prompt == prompt:
                del self._prompt_widgets[index]
                return widget
        return None

    def record_streamed_message_completed(self, event: MessageCompleted) -> None:
        """Record a streamed message that the shell suppresses from normal rendering."""

        if event.content:
            self._history.record_live_message(
                "assistant",
                event.content,
                widget=self.app.stream_widget_for_completed_message(),
            )

    def prompt_history_request(self) -> None:
        self.app.show_prompt_history()

    def history_hydration_started(self) -> None:
        self.app.history_hydration_started()

    def history_hydration_progress(self, label: str) -> None:
        self.app.history_hydration_progress(label)

    def history_hydration_finished(self) -> None:
        self.app.history_hydration_finished()

    def set_history_page_request_hook(
        self,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        self.app.set_history_page_request_hook(hook)

    def set_history_latest_request_hook(
        self,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        self.app.set_history_latest_request_hook(hook)

    def set_history_newer_page_request_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        self.app.set_history_newer_page_request_hook(hook)

    def set_history_detail_request_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        self.app.set_history_detail_request_hook(hook)

    def history_detail_loaded(self, entry_id: str, output: str) -> None:
        self.app.history_detail_loaded(entry_id, output)

    def history_detail_failed(self, entry_id: str, error: str) -> None:
        self.app.history_detail_failed(entry_id, error)

    def history_page_loaded(self, *, has_more: bool) -> None:
        self.app.history_page_loaded(has_more=has_more)

    def history_page_request_failed(self) -> None:
        self.app.history_page_request_failed()

    @property
    def retained_history_entry_count(self) -> int:
        """Return bounded Textual history retention for diagnostics and benchmarks."""

        return self._history.retained_entry_count

    def render_history(self, messages: tuple[HistoricalTranscriptMessage, ...]) -> None:
        self.render_history_entries(messages)

    def render_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
        self._history.render_entries(entries)

    def clear_session(self) -> None:
        self._clear_tool_presentation_state()
        self._history.clear_entries()

    def replace_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
        *,
        session_label: str,
    ) -> None:
        self._clear_tool_presentation_state()
        self._history.replace_entries(entries, session_label=session_label)

    async def hydrate_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
        *,
        session_label: str | None,
    ) -> None:
        """Mount complete history in responsive batches behind the operation overlay."""

        self._clear_tool_presentation_state()

        def report_progress(completed: int, total: int) -> None:
            self.history_hydration_progress(
                f"Preparing transcript… {completed:,} / {total:,} cards"
            )

        await self._history.hydrate_entries(
            entries,
            session_label=session_label,
            progress=report_progress,
        )

    def prepend_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
        self._history.prepend_entries(entries)

    def append_newer_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
        *,
        next_after_entry_id: str | None,
    ) -> str | None:
        next_before_entry_id = self._history.append_newer_entries(
            entries,
            next_after_entry_id=next_after_entry_id,
        )
        self.app.history_newer_page_loaded()
        return next_before_entry_id

    def history_newer_page_request_failed(self) -> None:
        self.app.history_newer_page_request_failed()

    def replace_latest_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
    ) -> bool:
        if self.app.consume_live_history_recovery() is not None:
            if self._history.recover_evicted_entries(entries):
                self.app.live_history_reloaded()
                return True
            self.app.live_history_recovery_deferred()
            return False
        if self._history.replace_latest_entries(entries):
            self.app.live_history_reloaded()
            return True
        else:
            self.app.live_history_reload_failed()
            return False

    def latest_history_reload_failed(self) -> None:
        """Allow a later live-widget eviction to retry a failed durable reload."""

        self.app.live_history_reload_failed()

    def capture_latest_history_reload(self) -> None:
        self.app.capture_live_history_reload()
        self._history.capture_latest_reload_live_entries()

    def _clear_tool_presentation_state(self) -> None:
        self._tool_started.clear()
        self._tool_arguments.clear()
        self._process_calls.clear()
        self._process_lifecycles.clear()
        self._process_started.clear()
        self._denied_process_calls.clear()

    def queued_prompts_cleared(self) -> None:
        # The shell dropped its queued follow-ups (cancel/quit/input-closed/error),
        # so their pending compact echoes will never be consumed — reclaim them.
        self.app.clear_compact_echoes()

    def running(self) -> None:
        self._begin_progress()

    def queued_follow_up(self, count: int) -> None:
        self.app.write_dim(f"queued follow-up #{count}")

    def running_queued_follow_up(self, count: int) -> None:
        self.app.write_dim(f"running queued follow-up; {count} queued")

    def input_closed_finishing_prompt(self) -> None:
        self.app.write_dim("input closed; finishing current prompt")

    def input_cleared(self) -> None:
        self.app.write_dim("input cleared")

    def cancelling(self, message: str) -> None:
        self._finish_progress()
        self.app.write_notice(message)

    def cancel_already_requested(self) -> None:
        self.app.write_dim("cancel already requested")

    def approval_input_closed(self) -> None:
        self.app.write_notice("Approval input closed; denying tool request.")

    def approval_interrupted(self) -> None:
        self.app.write_notice("Approval interrupted; denying tool request.")

    def quit_requested_denying_approval(self) -> None:
        self.app.write_notice("Quit requested; denying pending tool request.")

    def send_failed(self, action: str, error: object) -> None:
        self._finish_progress()
        self.app.hide_decision()
        self._abort_pending_tools(f"send failed: {action}")
        self.app.write_error(f"failed to send {action}: {error}")

    def shutdown_failed(self, error: object) -> None:
        self._finish_progress()
        self._abort_pending_tools("shutdown failed")
        self.app.write_error(f"shutdown failed: {error}")

    def cancelled(self) -> None:
        self._finish_progress()
        self._abort_pending_tools("cancelled")
        self.app.write_notice("cancelled")

    def token_delta(self, delta: str) -> None:
        # Stream live into the assistant Markdown widget. Its first visible frame
        # retires the command heartbeat so completion cannot shorten and jump the
        # followed transcript after a long response.
        self.app.append_stream(delta)

    def end_token_stream_with_content(self, completed_content: str) -> None:
        """Finalize streamed output using authoritative completed content."""

        self.app.flush_stream(completed_content)

    def end_token_stream(self) -> None:
        self.app.flush_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self._show_activity("Waiting for approval…")
        self.app.show_approval(event, cwd=self._visible_cwd)

    def trust_request(self, event: TrustRequested) -> None:
        self._show_activity("Waiting for trust…")
        self.app.show_trust(event)

    def command_catalog_updated(self, catalog: TuiCommandCatalog) -> None:
        self.app.set_command_catalog(catalog)

    def skill_catalog_updated(self, catalog: RpcSkillCatalogSnapshot) -> None:
        self.app.set_skill_catalog(catalog)

    def skills_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
        self.app.show_skill_catalog(catalog)

    def skill_invoked(self, event: SkillInvoked) -> None:
        widget = self._pop_prompt_widget(event.invocation.original_content)
        self._history.record_live_skill_invocation(
            event.message_entry_id,
            event.invocation.original_content,
        )
        self.app.show_skill_invocation(event, widget=widget)

    def set_connect_api_key_hook(
        self,
        hook: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self.app.set_connect_api_key_hook(hook)

    def set_connect_oauth_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        self.app.set_connect_oauth_hook(hook)

    def set_connect_cancel_hook(self, hook: Callable[[], object]) -> None:
        self.app.set_connect_cancel_hook(hook)

    def connect_picker_request(
        self,
        providers: tuple[ConnectionProviderStatus, ...],
        *,
        provider: str | None = None,
    ) -> None:
        self.app.show_connect_panel(providers, provider=provider)

    def disconnect_picker_request(
        self,
        providers: tuple[ConnectionProviderStatus, ...],
    ) -> None:
        self.app.show_connect_panel(providers, mode="disconnect")

    def connect_device_code(self, verification_uri: str, user_code: str) -> None:
        self.app.show_connect_device_code(verification_uri, user_code)

    def connect_completed(self, _provider: str) -> None:
        self.app.hide_connect_panel()

    def connect_failed(self, message: str) -> None:
        self.app.show_connect_error(message)

    def model_picker_request(
        self,
        catalog: RpcModelCatalogSnapshot,
    ) -> None:
        self.app.show_model_picker(catalog)

    def session_picker_request(
        self,
        sessions: tuple[RpcSessionSummary, ...],
        *,
        selected_session_id: str | None,
    ) -> None:
        self.app.show_session_picker(
            sessions,
            selected_session_id=selected_session_id,
        )

    def session_catalog_started(self) -> None:
        self.app.session_catalog_started()

    def session_catalog_finished(self) -> None:
        self.app.session_catalog_finished()

    def session_switch_started(self, session_id: str) -> None:
        self.app.session_switch_started()

    def session_switch_finished(self) -> None:
        self.app.session_switch_finished()

    async def wait_for_session_operation_paint(self) -> None:
        await self.app.wait_for_session_operation_paint()

    def context_status(self, stats: SessionStats) -> None:
        self.app.show_context_status(stats)

    def event(self, event: KnownWispEvent) -> None:
        # Typed dispatch mirroring LineTuiRenderer.event() so tool calls, tool
        # results, and approvals render as distinct, semantically-styled lines
        # instead of an undifferentiated str(event) repr.
        if isinstance(event, TurnStarted):
            self._turn_started(event.turn)
        elif isinstance(event, ProviderRetrying):
            self._provider_retrying(event)
        elif isinstance(event, CompactionStarted):
            self.app.write_notice(_compaction_started_text(event))
            self._show_activity("Compacting…")
        elif isinstance(event, CompactionCompleted):
            if self._progress_active:
                self.app.show_working_indicator()
            overflow_retry_failed = event.reason == "overflow" and not event.will_retry
            if (
                event.outcome == "failed" and event.reason in {"manual", "overflow", "threshold"}
            ) or (event.outcome == "completed" and overflow_retry_failed):
                if event.reason == "overflow":
                    self._overflow_recovery_failed = True
                self.app.write_error(_compaction_completed_text(event))
            else:
                self.app.write_notice(_compaction_completed_text(event))
        elif isinstance(event, MessageStarted):
            self._message_started(event.turn)
        elif isinstance(event, MessageCompleted):
            if event.content:
                widget = self.app.write_assistant(event.content)
                self._history.record_live_message("assistant", event.content, widget=widget)
        elif isinstance(event, ToolCallRequested):
            # Mount the evolving card; approval/result mutate it in place. Record
            # the request time so the card can show its true duration on resolve,
            # and retain the arguments so the result renderer can build tool-aware
            # detail (they don't travel on the result event).
            self._tool_started[event.call_id] = event.timestamp
            self._tool_arguments[event.call_id] = event.arguments
            identity = process_call_identity(event.name, event.arguments)
            if identity is None:
                card = self.app.mount_tool_call(event.call_id, event.name, event.arguments)
            else:
                self._process_calls[event.call_id] = identity
                self._process_started.setdefault(identity.process_id, event.timestamp)
                card = self.app.mount_process_call(event.call_id, identity.process_id)
                if card is not None:
                    self._history.transfer_widget_to_live(card)
                lifecycle = self._process_lifecycles.get(identity.process_id)
                if lifecycle is None:
                    lifecycle = (
                        ProcessLifecycle.from_presentation(card.lifecycle_presentation)
                        if card is not None
                        else ProcessLifecycle(identity.process_id)
                    )
                    self._process_lifecycles[identity.process_id] = lifecycle
                self.app.update_process_card(lifecycle.begin(identity.operation))
            self._history.record_live_tool_call(event.call_id, widget=card)
        elif isinstance(event, TrustResolved):
            if self._progress_active:
                self.app.show_working_indicator()
        elif isinstance(event, ToolApprovalResolved):
            if self._progress_active:
                self.app.show_working_indicator()
            # Only a denial changes the card here: an approval leaves it pending
            # until the result lands (the tool still has to run). A denial short-
            # circuits to an error result, but flip the card to "denied" now so the
            # reason shows immediately rather than as a generic error line.
            if not event.approved:
                identity = self._process_calls.get(event.call_id)
                if identity is None:
                    self.app.resolve_tool_call(
                        event.call_id,
                        "denied",
                        detail=event.reason or "denied",
                        elapsed=self._tool_elapsed(event.call_id, event.timestamp),
                    )
                else:
                    lifecycle = self._process_lifecycles[identity.process_id]
                    presentation = lifecycle.deny(
                        identity.operation,
                        event.reason or "denied",
                    )
                    self._denied_process_calls[event.call_id] = identity.operation
                    self.app.update_process_card(presentation)
                    self._tool_elapsed(event.call_id, event.timestamp)
        elif isinstance(event, ToolResultReady):
            # A nonzero-exit command is is_error=False on the wire (a normal
            # model-visible result) but should still present as a failure; drive
            # the glyph and the detail from the same judgment so they agree.
            status = tool_result_status(
                event.is_error,
                event.exit_code,
                process_state=event.process_state,
            )
            # Consume the retained arguments for this call (empty if the request
            # was never seen, e.g. a resumed session) so tool-aware renderers can
            # use them; pop so the map doesn't grow across the session.
            arguments = self._tool_arguments.pop(event.call_id, {})
            identity = self._process_calls.pop(event.call_id, None)
            if identity is None:
                card = self.app.resolve_tool_call(
                    event.call_id,
                    status,
                    detail=render_tool_result(
                        event.name,
                        arguments,
                        event.output,
                        is_error=event.is_error,
                        exit_code=event.exit_code,
                        output_has_exit_status=event.output_has_exit_status,
                        before_text=event.before_text,
                        created=event.created,
                        summary=event.summary,
                        truncated=event.truncated,
                        process_state=event.process_state,
                    ),
                    elapsed=self._tool_elapsed(event.call_id, event.timestamp),
                    # Retain the full (tool-bounded) output so the card can expand past
                    # the collapsed detail; the card only offers expansion when this adds
                    # something. `truncated` flags that even the full output was capped by
                    # the tool itself.
                    full_output=full_tool_result_for_display(
                        event.name,
                        event.output,
                        event.exit_code,
                        output_has_exit_status=event.output_has_exit_status,
                        summary=event.summary if status == "done" else None,
                    ),
                    truncated=event.truncated,
                )
            else:
                lifecycle = self._process_lifecycles[identity.process_id]
                denied_operation = self._denied_process_calls.pop(event.call_id, None)
                if denied_operation is not None:
                    presentation = lifecycle.deny(denied_operation)
                elif (
                    event.process_id is None
                    and event.process_state == "cancelled"
                    and event.output == INTERRUPTED_TOOL_RESULT_TEXT
                ):
                    presentation = lifecycle.interrupt(identity.operation)
                else:
                    historical_observation = historical_process_observation(
                        identity.process_id,
                        event.output,
                    )
                    if event.process_id == identity.process_id:
                        matching_state = event.process_state
                    elif event.process_id is None:
                        matching_state = historical_observation.state
                    else:
                        matching_state = None
                    presentation = lifecycle.observe(
                        operation=identity.operation,
                        state=matching_state,
                        stdout=event.stdout or historical_observation.stdout,
                        stderr=event.stderr or historical_observation.stderr,
                        source_truncated=(
                            event.truncated or event.stdout_truncated or event.stderr_truncated
                        ),
                        source_dropped_bytes=(
                            event.stdout_dropped_bytes + event.stderr_dropped_bytes
                        ),
                        fallback_output=historical_observation.fallback_output,
                        failure_reason=(
                            event.process_error or historical_observation.failure_reason
                        ),
                        failed=status == "error",
                    )
                started = self._process_started.get(identity.process_id)
                elapsed = (
                    (event.timestamp - started).total_seconds()
                    if presentation.terminal and started is not None
                    else None
                )
                card = self.app.resolve_process_call(
                    event.call_id,
                    presentation,
                    elapsed=elapsed,
                )
                if presentation.terminal:
                    self._process_started.pop(identity.process_id, None)
                self._tool_elapsed(event.call_id, event.timestamp)
            self._history.record_live_tool_result(event.call_id, widget=card)
        elif isinstance(event, AgentCompleted):
            self._finish_progress(
                after_stream=event.outcome == "completed",
                force=event.outcome != "completed",
            )
        elif isinstance(event, ErrorEvent):
            self._finish_progress(force=True)
            if not (
                self._overflow_recovery_failed
                and event.message.startswith("Context overflow recovery failed:")
            ):
                self.app.write_error(f"error: {event.message}")
        elif isinstance(event, RpcCommandFinished):
            if event.command_type in {"prompt", "compact"}:
                self._finish_progress(force=not event.ok)
            if not event.ok and event.command_type != "compact":
                self._suspend_progress()
                self._abort_pending_tools("command failed")
                if self._overflow_recovery_failed:
                    self._overflow_recovery_failed = False
                else:
                    self.app.write_error(f"command failed: {event.error or event.command_id}")
        # Framing/plumbing events (RpcCommandStarted, a successful RpcCommandFinished,
        # AgentStarted, ToolExecutionStarted/Ended, SessionSaved) are intentionally
        # not rendered. They are session/RPC audit, not conversation — and the active
        # session id already lives in the footer, so a per-turn "session saved:"
        # line is pure redundancy. Dropping them keeps the transcript conversational.

    def rpc_event_reader_failed(self, error: str) -> None:
        self._finish_progress()
        self._abort_pending_tools("event reader failed")
        self.app.write_error(f"RPC event reader failed: {error}")

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self._finish_progress()
        self._abort_pending_tools("stream ended")
        self.app.write_error(f"RPC stream ended before command finished: {command_id}")

    def rpc_stream_ended_before_shutdown(self, command_id: str) -> None:
        self._finish_progress()
        self._abort_pending_tools("stream ended")
        self.app.write_error(f"RPC stream ended before shutdown finished: {command_id}")

    def rpc_stream_ended_unexpectedly(self) -> None:
        self._finish_progress()
        self._abort_pending_tools("stream ended")
        self.app.write_error("RPC stream ended unexpectedly")
