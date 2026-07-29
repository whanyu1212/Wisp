"""The `TuiRenderer` implementation backed by the Textual app.

`TextualTuiRenderer` is the adapter `TuiShell` drives: it translates the shared,
framework-agnostic renderer protocol (`view_updated`, `event`, `approval_request`,
token streaming, …) into calls on a `TextualTui`. It owns only its own progress
and tool-timing bookkeeping and reaches the app exclusively through the app's
public surface, so it lives here rather than inside the app module.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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
    RpcSessionSummary,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
    TurnStarted,
)
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tui.commands import TuiCommandCatalog
from wisp.tui.history import (
    HistoricalToolCard,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    historical_tool_status,
)
from wisp.tui.rendering import (
    TuiViewSnapshot,
    _compaction_completed_text,
    _compaction_started_text,
    _truncate_to_cell_width,
    _tui_help_text,
)
from wisp.tui.tool_output import (
    full_tool_output_for_display,
    render_tool_result,
    tool_result_failed,
)

if TYPE_CHECKING:
    from wisp.tui.textual_app import TextualTui

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
        self._progress_active = False
        self._progress_turn: int | None = None
        self._response_started = False
        self._retry_attempt = 0
        self._overflow_recovery_failed = False

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        self._visible_input_mode = snapshot.input_mode
        self._visible_cwd = snapshot.cwd
        self.app.set_input_hint(snapshot.input_hint)
        self.app.set_status(snapshot)
        if snapshot.input_mode != "running":
            self.app.hide_working_indicator()
        if snapshot.input_mode not in {"running", "approval", "trust"}:
            self._finish_progress()
        if snapshot.input_mode not in {"approval", "trust"}:
            self.app.hide_decision()

    def _begin_progress(self) -> None:
        self._progress_active = True
        self._progress_turn = None
        self._response_started = False
        self._retry_attempt = 0
        self.app.restart_working_indicator()

    def _finish_progress(self) -> None:
        self._progress_active = False
        self._progress_turn = None
        self._response_started = False
        self._retry_attempt = 0
        self.app.hide_working_indicator()

    def _suspend_progress(self) -> None:
        self.app.hide_working_indicator()

    def _turn_started(self, turn: int) -> None:
        if not self._progress_active:
            return
        if self._progress_turn is not None and turn <= self._progress_turn:
            return
        self._progress_turn = turn
        self._response_started = False
        self._retry_attempt = 0
        self.app.show_working_indicator()

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
        self.app.show_working_indicator()

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
        # the next turn.
        self._tool_started.clear()
        self._tool_arguments.clear()
        self.app.fail_pending_tool_calls(detail)

    def _capture_submitted_input_mode(self) -> None:
        self._submitted_input_mode = self._visible_input_mode

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
        self.app.write_notice(
            _tui_help_text(
                approval_hint=(
                    "Tool approvals default to 1 (Approve once) — Enter approves; "
                    "Escape or 4 denies."
                )
            )
        )

    def notice(self, message: str) -> None:
        self.app.write_notice(message)

    def command_error(self, message: str) -> None:
        self.app.write_error(message)

    def prompt_submitted(self, prompt: str) -> None:
        # Echo a compact line for large pastes (marker kept) while the model still
        # received the full expanded text via controller.prompt(prompt).
        self.app.write_user(self.app.compact_echo_for(prompt))

    def prompt_accepted(self, prompt: str) -> None:
        self.app.record_prompt(prompt)

    def prompt_history_request(self) -> None:
        self.app.show_prompt_history()

    def render_history(self, messages: tuple[HistoricalTranscriptMessage, ...]) -> None:
        for message in messages:
            if message.role == "user":
                self.app.write_user(message.content)
            else:
                self.app.write_assistant(message.content)

    def render_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
        for entry in entries:
            if isinstance(entry, HistoricalTranscriptMessage):
                if entry.role == "user":
                    self.app.write_user(entry.content)
                else:
                    self.app.write_assistant(entry.content)
            else:
                self._render_historical_tool_card(entry)

    def replace_history_entries(
        self,
        entries: tuple[HistoricalTranscriptEntry, ...],
        *,
        session_label: str,
    ) -> None:
        self._tool_started.clear()
        self._tool_arguments.clear()
        self.app.replace_transcript()
        self.app.write_dim(f"resumed session: {session_label}")
        self.render_history_entries(entries)

    def _render_historical_tool_card(self, entry: HistoricalToolCard) -> None:
        self.app.mount_tool_call(entry.card_id, entry.name, entry.arguments)
        status = historical_tool_status(entry)
        if status in {"cancelled", "denied"}:
            self.app.resolve_tool_call(
                entry.card_id,
                status,
                detail=entry.output,
            )
            return
        self.app.resolve_tool_call(
            entry.card_id,
            status,
            detail=render_tool_result(
                entry.name,
                entry.arguments,
                entry.output,
                is_error=entry.is_error,
                exit_code=entry.exit_code,
                output_has_exit_status=entry.output_has_exit_status,
                before_text=entry.before_text,
                created=entry.created,
                summary=entry.summary,
            ),
            full_output=full_tool_output_for_display(
                entry.output,
                entry.exit_code,
                output_has_exit_status=entry.output_has_exit_status,
            ),
            truncated=entry.truncated,
        )

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
        # Stream live into the assistant Markdown widget; append_stream buffers
        # and coalesces the reconcile. end_token_stream() finalizes the bubble.
        self._suspend_progress()
        self.app.append_stream(delta)

    def end_token_stream(self) -> None:
        self.app.flush_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        self._suspend_progress()
        self.app.show_approval(event, cwd=self._visible_cwd)

    def approval_all_confirmation(self, event: ToolApprovalRequested) -> None:
        self.app.show_approval_all_confirmation(event)

    def trust_request(self, event: TrustRequested) -> None:
        self._suspend_progress()
        self.app.show_trust(event)

    def command_catalog_updated(self, catalog: TuiCommandCatalog) -> None:
        self.app.set_command_catalog(catalog)

    def model_picker_request(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None:
        self.app.show_model_picker(
            entries,
            current_provider=current_provider,
            current_model=current_model,
            current_effort=current_effort,
        )

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
            if event.reason in {"threshold", "overflow"}:
                self.app.show_working_indicator()
        elif isinstance(event, CompactionCompleted):
            if event.reason in {"threshold", "overflow"}:
                self._suspend_progress()
            overflow_retry_failed = event.reason == "overflow" and not event.will_retry
            if (event.outcome == "failed" and event.reason in {"manual", "overflow"}) or (
                event.outcome == "completed" and overflow_retry_failed
            ):
                if event.reason == "overflow":
                    self._overflow_recovery_failed = True
                self.app.write_error(_compaction_completed_text(event))
            else:
                self.app.write_notice(_compaction_completed_text(event))
        elif isinstance(event, MessageStarted):
            self._message_started(event.turn)
        elif isinstance(event, MessageCompleted):
            self._suspend_progress()
            if event.content:
                self.app.write_assistant(event.content)
        elif isinstance(event, ToolCallRequested):
            # Mount the evolving card; approval/result mutate it in place. Record
            # the request time so the card can show its true duration on resolve,
            # and retain the arguments so the result renderer can build tool-aware
            # detail (they don't travel on the result event).
            self._suspend_progress()
            self._tool_started[event.call_id] = event.timestamp
            self._tool_arguments[event.call_id] = event.arguments
            self.app.mount_tool_call(event.call_id, event.name, event.arguments)
        elif isinstance(event, ToolApprovalResolved):
            # Only a denial changes the card here: an approval leaves it pending
            # until the result lands (the tool still has to run). A denial short-
            # circuits to an error result, but flip the card to "denied" now so the
            # reason shows immediately rather than as a generic error line.
            if not event.approved:
                self.app.resolve_tool_call(
                    event.call_id,
                    "denied",
                    detail=event.reason or "denied",
                    elapsed=self._tool_elapsed(event.call_id, event.timestamp),
                )
        elif isinstance(event, ToolResultReady):
            # A nonzero-exit command is is_error=False on the wire (a normal
            # model-visible result) but should still present as a failure; drive
            # the glyph and the detail from the same judgment so they agree.
            failed = tool_result_failed(event.is_error, event.exit_code)
            status = "error" if failed else "done"
            # Consume the retained arguments for this call (empty if the request
            # was never seen, e.g. a resumed session) so tool-aware renderers can
            # use them; pop so the map doesn't grow across the session.
            arguments = self._tool_arguments.pop(event.call_id, {})
            self.app.resolve_tool_call(
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
                ),
                elapsed=self._tool_elapsed(event.call_id, event.timestamp),
                # Retain the full (tool-bounded) output so the card can expand past
                # the collapsed detail; the card only offers expansion when this adds
                # something. `truncated` flags that even the full output was capped by
                # the tool itself.
                full_output=full_tool_output_for_display(
                    event.output,
                    event.exit_code,
                    output_has_exit_status=event.output_has_exit_status,
                ),
                truncated=event.truncated,
            )
        elif isinstance(event, AgentCompleted):
            self._finish_progress()
        elif isinstance(event, ErrorEvent):
            self._finish_progress()
            if not (
                self._overflow_recovery_failed
                and event.message.startswith("Context overflow recovery failed:")
            ):
                self.app.write_error(f"error: {event.message}")
        elif isinstance(event, RpcCommandFinished):
            if event.command_type in {"prompt", "compact"}:
                self._finish_progress()
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
