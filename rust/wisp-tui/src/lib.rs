//! Minimal native owner for Wisp's negotiated JSONL-RPC backend transport.

#![forbid(unsafe_code)]

mod cli;
mod detail_view;
mod framing;
pub mod history;
mod markdown;
mod process;
mod prompt_editor;
pub mod reducer;
mod session_picker;
mod session_tree_picker;
mod syntax;
mod terminal;
mod tool_cards;
mod tool_detail;
mod transcript;
#[cfg(feature = "transcript-benchmark")]
pub mod transcript_benchmark;
mod transcript_view;
mod ui;

use bytes::Bytes;
use clap::Parser;
use cli::Cli;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use detail_view::DetailView;
use framing::FrameReader;
use nix::sys::signal::Signal;
use process::{BackendProcess, CleanupOutcome};
use prompt_editor::{EditOutcome, EditorAction, PromptEditor};
use ratatui::Terminal;
use ratatui::backend::Backend;
use reducer::{
    BackendEvent, CommandIdSource, CommandKind, PendingApproval, UiAction, UiEffect, UiState,
    ViewStatus,
};
use session_picker::{SessionPicker, SessionPickerAction};
use session_tree_picker::{SessionTreePicker, SessionTreePickerAction};
use std::collections::{HashSet, VecDeque};
use std::ffi::OsString;
use std::future::pending;
use std::io;
use std::sync::Arc;
use std::time::Duration;
use terminal::{PanicHookGuard, TerminalGuard};
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc::error::{TryRecvError, TrySendError};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot, watch};
use tokio::task::JoinHandle;
use tokio::time::{Instant, MissedTickBehavior, interval, timeout};
use tool_detail::{DetailAvailability, ToolDetailPresentation};
use transcript::TranscriptEntryId;
use transcript_view::{
    TranscriptRowCache, TranscriptRowKind, TranscriptViewAction, TranscriptViewport,
};
use ui::ConnectionInfo;
use wisp_protocol::commands::{ApprovalScope, QueueKind, WispTypedClientRpcCommands};
use wisp_protocol::events::WispCurrentLiveEventOutput;
use wisp_protocol::handshake_request::RpcHandshakeRequest;
use wisp_protocol::handshake_response::RpcHandshakeResponse;
use wisp_protocol::{
    EVENT_SCHEMA_VERSION, HANDSHAKE_FRAME_BYTES, LIVE_RPC_PROTOCOL_VERSION,
    MAX_APPLICATION_FRAME_BYTES, ProtocolDecodeError,
};

const WRITER_CHANNEL_CAPACITY: usize = 1;
const EVENT_CHANNEL_CAPACITY: usize = 64;
// Count permits from original frame lengths so ordinary bursts fit while all
// queued events together retain at most one maximum-size wire-frame budget.
const EVENT_RETAINED_WIRE_BYTES: usize = MAX_APPLICATION_FRAME_BYTES;
const INPUT_CHANNEL_CAPACITY: usize = 16;
const STDERR_RETAINED_BYTES: usize = 64 * 1024;
const TOP_LEVEL_ERROR_MAX_BYTES: usize = 8 * 1024;
const TOP_LEVEL_ERROR_MAX_CHARS: usize = 4 * 1024;
const TRUNCATION_NOTICE: &str = " [output truncated]";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);
const TASK_JOIN_TIMEOUT: Duration = Duration::from_secs(1);
const SHUTDOWN_COMMAND_ID: &str = "rust-tui-shutdown";
const FRAME_INTERVAL: Duration = Duration::from_millis(16);

#[derive(Debug, Error)]
pub enum Error {
    #[error("backend command after `--` is required")]
    MissingBackendCommand,
    #[error("failed to spawn backend {program:?}: {source}")]
    Spawn {
        program: OsString,
        source: io::Error,
    },
    #[error("spawned backend has no process id")]
    MissingProcessId,
    #[error("spawned backend has no piped {0}")]
    MissingProcessPipe(&'static str),
    #[error("I/O failure: {0}")]
    Io(#[from] io::Error),
    #[error("RPC frame exceeds the {limit}-byte limit")]
    FrameTooLarge { limit: usize },
    #[error("RPC stream ended with an incomplete frame")]
    IncompleteFrame,
    #[error("RPC stream ended before the handshake response")]
    HandshakeEof,
    #[error("RPC backend did not complete the handshake within 5 seconds")]
    HandshakeTimeout,
    #[error("invalid RPC protocol value: {0}")]
    Protocol(#[from] ProtocolDecodeError),
    #[error("failed to project RPC event into UI state: {0}")]
    EventProjection(#[from] reducer::EventProjectionError),
    #[error("invalid UI state transition: {0}")]
    Reducer(#[from] reducer::ReduceError),
    #[error("invalid UTF-8 JSON RPC object: {0}")]
    InvalidProtocolFrame(serde_json::Error),
    #[error("failed to encode RPC protocol value: {0}")]
    Encode(#[from] serde_json::Error),
    #[error("backend rejected RPC negotiation ({code}): {message}")]
    HandshakeRejected { code: String, message: String },
    #[error(
        "Rust frontend version {frontend:?} does not match expected backend version {expected:?}"
    )]
    FrontendVersionMismatch { expected: String, frontend: String },
    #[error("backend package version {actual:?} does not match required version {expected:?}")]
    BackendVersionMismatch { expected: String, actual: String },
    #[error("backend selected unsupported RPC v{protocol}/event v{events}")]
    ContractMismatch { protocol: u32, events: u32 },
    #[error("RPC writer stopped unexpectedly")]
    WriterStopped,
    #[error("queue submission was not accepted by the writer within 5 seconds")]
    QueueSubmissionTimeout,
    #[error("RPC reader stopped unexpectedly")]
    ReaderStopped,
    #[error("RPC stdout event queue is full; the frontend cannot keep up with the backend")]
    InboundOverloaded,
    #[error("RPC backend stdout ended unexpectedly")]
    BackendStreamEnded,
    #[error("backend exited unexpectedly with {0}")]
    BackendExited(std::process::ExitStatus),
    #[error("backend exited unsuccessfully with {0}")]
    BackendExitFailure(std::process::ExitStatus),
    #[error("shutdown command failed: {message}")]
    ShutdownCommandFailed { message: String },
    #[error("backend exited without a successful shutdown completion event")]
    ShutdownCompletionMissing,
    #[error("backend did not exit before the graceful shutdown deadline")]
    GracefulShutdownTimeout,
    #[error("backend cleanup required {stage}")]
    CleanupEscalated { stage: &'static str },
    #[error("failed to send {signal:?} to backend process: {source}")]
    Signal {
        signal: Signal,
        source: nix::errno::Errno,
    },
    #[error("backend process did not exit after SIGKILL")]
    CleanupTimeout,
    #[error("background task failed: {0}")]
    Task(#[from] tokio::task::JoinError),
    #[error("{0} task did not stop within 1 second")]
    TaskTimeout(&'static str),
}

/// Render the complete command-line diagnostic within terminal-safe output limits.
pub fn render_top_level_error(error: &Error) -> String {
    let mut output = BoundedTerminalText::new(TOP_LEVEL_ERROR_MAX_BYTES, TOP_LEVEL_ERROR_MAX_CHARS);
    std::fmt::write(&mut output, format_args!("wisp-tui: {error}"))
        .expect("bounded terminal renderer cannot fail");
    output.finish()
}

fn render_transport_closed_diagnostic(state: &UiState) -> String {
    let mut output = BoundedTerminalText::new(TOP_LEVEL_ERROR_MAX_BYTES, TOP_LEVEL_ERROR_MAX_CHARS);
    std::fmt::write(
        &mut output,
        format_args!("wisp-tui: backend stream ended unexpectedly"),
    )
    .expect("bounded terminal renderer cannot fail");
    if let Some(content) = state
        .latest_assistant_text()
        .filter(|content| !content.is_empty())
    {
        std::fmt::write(
            &mut output,
            format_args!("; partial assistant response: {content}"),
        )
        .expect("bounded terminal renderer cannot fail");
    }
    output.finish()
}

const UNSENT_QUEUE_REPORT_MAX_LINES: usize = 16;
const UNSENT_QUEUE_REPORT_MAX_BYTES: usize = 1024;
const UNSENT_QUEUE_REPORT_MAX_CHARS: usize = 512;

fn queue_kind_label(kind: QueueKind) -> &'static str {
    match kind {
        QueueKind::Steering => "steer",
        QueueKind::FollowUp => "later",
    }
}

fn render_unsent_queue_diagnostics<'a>(
    state: &'a UiState,
    deferred: &'a [DeferredQueueRecovery],
) -> Vec<String> {
    let mut items = Vec::<(&'static str, &'static str, &'a str, usize)>::new();
    let mut add = |context: &'static str, label: &'static str, content: &'a str| {
        if let Some((_, _, _, count)) =
            items
                .iter_mut()
                .find(|(item_context, item_label, item_content, _)| {
                    *item_context == context && *item_label == label && *item_content == content
                })
        {
            *count = count.saturating_add(1);
        } else {
            items.push((context, label, content, 1));
        }
    };

    for (kind, _, content) in state.queue_items() {
        add("queued", queue_kind_label(kind), content);
    }
    for (kind, content) in state.unobserved_queue_submissions() {
        add("in-flight", queue_kind_label(kind), content);
    }
    if let Some((kind, content)) = state.pending_queue_restore_item() {
        add("restoring", queue_kind_label(kind), content);
    }
    for recovery in deferred {
        add("deferred recovery", "queue", &recovery.content);
    }
    let mut lines = Vec::new();
    let mut omitted = 0_usize;
    for (context, label, content, count) in items {
        if lines.len() >= UNSENT_QUEUE_REPORT_MAX_LINES.saturating_sub(1) {
            omitted = omitted.saturating_add(count);
            continue;
        }
        let mut output =
            BoundedTerminalText::new(UNSENT_QUEUE_REPORT_MAX_BYTES, UNSENT_QUEUE_REPORT_MAX_CHARS);
        if count == 1 {
            std::fmt::write(
                &mut output,
                format_args!("wisp-tui: {context} {label} item will not run: {content}"),
            )
        } else {
            std::fmt::write(
                &mut output,
                format_args!("wisp-tui: {context} {label} item x{count} will not run: {content}"),
            )
        }
        .expect("bounded terminal renderer cannot fail");
        lines.push(output.finish());
    }
    if omitted > 0 {
        lines.push(format!(
            "wisp-tui: {omitted} additional queued item(s) will not run."
        ));
    }
    lines
}

enum WriterMessage {
    Frame {
        payload: Bytes,
        limit: usize,
        ack: Option<oneshot::Sender<Result<(), ()>>>,
    },
    Close,
}

#[derive(Debug)]
enum ReaderTermination {
    Eof,
}

struct QueuedEvent {
    event: BackendEvent,
    _wire_bytes: OwnedSemaphorePermit,
}

#[derive(Debug)]
struct StderrCapture {
    bytes: Vec<u8>,
    dropped_bytes: usize,
}

#[derive(Default)]
struct ShutdownObservation {
    command_succeeded: bool,
    backend_status: Option<std::process::ExitStatus>,
}

impl ShutdownObservation {
    fn observe_event(&mut self, event: &BackendEvent) -> Result<(), Error> {
        let BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } = event
        else {
            return Ok(());
        };
        if command_id != SHUTDOWN_COMMAND_ID || command_type != "shutdown" {
            return Ok(());
        }
        if *ok {
            self.command_succeeded = true;
            return Ok(());
        }
        Err(Error::ShutdownCommandFailed {
            message: error
                .clone()
                .unwrap_or_else(|| "backend reported failure".into()),
        })
    }

    fn observe_exit(&mut self, status: std::process::ExitStatus) -> Result<(), Error> {
        if !status.success() {
            return Err(Error::BackendExitFailure(status));
        }
        self.backend_status = Some(status);
        Ok(())
    }

    fn completed(&self) -> bool {
        self.command_succeeded && self.backend_status.is_some()
    }

    fn deadline_error(&self) -> Error {
        if self.command_succeeded {
            Error::GracefulShutdownTimeout
        } else {
            Error::ShutdownCompletionMissing
        }
    }
}

#[derive(Clone, Default)]
struct SequentialCommandIds {
    next: u64,
}

impl SequentialCommandIds {
    fn peek_id(&self, kind: CommandKind) -> String {
        self.peek_prefixed_id(kind.prefix())
    }

    fn peek_prefixed_id(&self, prefix: &str) -> String {
        self.peek_prefixed_offset_id(prefix, 1)
    }

    fn peek_following_prefixed_id(&self, prefix: &str) -> String {
        self.peek_prefixed_offset_id(prefix, 2)
    }

    fn peek_prefixed_offset_id(&self, prefix: &str, offset: u64) -> String {
        let next = self
            .next
            .checked_add(offset)
            .expect("a frontend process cannot exhaust u64 command IDs");
        format!("{prefix}-{next}")
    }

    fn next_prefixed_id(&mut self, prefix: &str) -> String {
        let id = self.peek_prefixed_id(prefix);
        self.next += 1;
        id
    }
}

impl CommandIdSource for SequentialCommandIds {
    fn next_id(&mut self, kind: CommandKind) -> String {
        self.next_prefixed_id(kind.prefix())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LoopControl {
    Continue,
    Exit,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum RenderedDecisionContext {
    Approval(String),
    Trust(String),
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum UnsendableResponseContext {
    Interactive(RenderedDecisionContext),
    Cancelling(String),
}

#[derive(Debug)]
struct DeferredQueueRecovery {
    content: String,
    local_order: Option<u64>,
}

struct LiveUi {
    state: UiState,
    transcript_viewport: TranscriptViewport,
    transcript_row_cache: TranscriptRowCache,
    detail_view: DetailView,
    browse_selected: Option<TranscriptEntryId>,
    editor: PromptEditor,
    ids: SequentialCommandIds,
    notice: Option<String>,
    render_pending: bool,
    rendered_decision_context: Option<RenderedDecisionContext>,
    unsendable_response_context: Option<UnsendableResponseContext>,
    deferred_queue_recovery: Vec<DeferredQueueRecovery>,
    recovered_queue_recovery: bool,
    session_picker: Option<SessionPicker>,
    session_tree_picker: Option<SessionTreePicker>,
}

impl Default for LiveUi {
    fn default() -> Self {
        Self {
            state: UiState::unconfigured(),
            transcript_viewport: TranscriptViewport::default(),
            transcript_row_cache: TranscriptRowCache::default(),
            detail_view: DetailView::default(),
            browse_selected: None,
            editor: PromptEditor::default(),
            ids: SequentialCommandIds::default(),
            notice: None,
            render_pending: true,
            rendered_decision_context: None,
            unsendable_response_context: None,
            deferred_queue_recovery: Vec::new(),
            recovered_queue_recovery: false,
            session_picker: None,
            session_tree_picker: None,
        }
    }
}

impl LiveUi {
    fn reset_transcript_presentation(&mut self) {
        self.transcript_viewport = TranscriptViewport::default();
        self.transcript_row_cache = TranscriptRowCache::default();
        self.detail_view = DetailView::default();
        self.browse_selected = None;
    }

    fn deferred_queue_recovery_bytes(&self) -> usize {
        self.deferred_queue_recovery
            .iter()
            .fold(0_usize, |bytes, recovery| {
                bytes.saturating_add(recovery.content.len())
            })
    }

    fn deferred_queue_recovery_can_accept(&self, content: &str) -> bool {
        self.deferred_queue_recovery.len() < reducer::QUEUE_MESSAGE_LIMIT
            && self
                .deferred_queue_recovery_bytes()
                .saturating_add(content.len())
                <= reducer::QUEUE_CONTENT_BYTES_LIMIT
    }

    fn defer_queue_recovery(&mut self, content: String, local_order: Option<u64>) {
        assert!(
            self.deferred_queue_recovery_can_accept(&content),
            "deferred recoveries must stay within runtime queue limits"
        );
        let position = local_order
            .and_then(|local_order| {
                self.deferred_queue_recovery.iter().position(|recovery| {
                    recovery
                        .local_order
                        .is_some_and(|existing| existing > local_order)
                })
            })
            .unwrap_or(self.deferred_queue_recovery.len());
        self.deferred_queue_recovery.insert(
            position,
            DeferredQueueRecovery {
                content,
                local_order,
            },
        );
        self.notice = Some(
            "Could not restore queued text because it exceeds the editor limit; Alt+Up restores deferred items one at a time."
                .into(),
        );
        self.render_pending = true;
    }

    fn retry_deferred_queue_recovery(&mut self) -> bool {
        let Some(recovery) = self.deferred_queue_recovery.first() else {
            return false;
        };
        let outcome = self.editor.prepend_restored(&recovery.content);
        self.notice = if outcome.rejected_limit {
            Some(
                "Deferred queued text still exceeds the editor limit; kept your newer draft unchanged."
                    .into(),
            )
        } else if outcome.changed {
            self.deferred_queue_recovery.remove(0);
            self.recovered_queue_recovery = true;
            if outcome.ignored_controls > 0 {
                Some(format!(
                    "Restored queued text after ignoring {} unsafe terminal control character(s).",
                    outcome.ignored_controls
                ))
            } else {
                None
            }
        } else {
            Some("Deferred queued text had no editable content; it remains pending.".into())
        };
        self.render_pending = true;
        true
    }

    async fn apply_effects(
        &mut self,
        effects: Vec<UiEffect>,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let mut control = LoopControl::Continue;
        let mut pending = VecDeque::from(effects);
        while let Some(effect) = pending.pop_front() {
            match effect {
                UiEffect::SendCommand(command) => {
                    let value = serde_json::to_value(&command)?;
                    let command_type = value.get("type").and_then(|value| value.as_str());
                    let payload = Bytes::from(serde_json::to_vec(&value)?);
                    if payload.len() > limit && command_type == Some("get_session_stats") {
                        self.notice = Some(format!(
                            "Skipped session stats refresh because the negotiated {limit}-byte RPC frame limit is too small."
                        ));
                        self.render_pending = true;
                        let command_id = value
                            .get("id")
                            .and_then(serde_json::Value::as_str)
                            .expect("validated RPC commands have string IDs")
                            .to_owned();
                        pending.extend(reducer::reduce(
                            &mut self.state,
                            UiAction::SkipPostPromptStats { command_id },
                            &mut self.ids,
                        )?);
                        continue;
                    }
                    send_payload(writer, payload, limit).await?;
                }
                UiEffect::RestoreDraft {
                    content,
                    local_order: Some(local_order),
                } => self.defer_queue_recovery(content, Some(local_order)),
                UiEffect::RestoreDraft {
                    content,
                    local_order: None,
                } => {
                    let outcome = self.editor.prepend_restored(&content);
                    if outcome.rejected_limit {
                        self.defer_queue_recovery(content, None);
                    } else {
                        if outcome.changed {
                            self.recovered_queue_recovery = true;
                        }
                        self.notice = if outcome.ignored_controls > 0 {
                            Some(format!(
                                "Restored queued text after ignoring {} unsafe terminal control character(s).",
                                outcome.ignored_controls
                            ))
                        } else if outcome.changed {
                            None
                        } else {
                            Some(
                                "Queued text had no editable content; kept your newer draft unchanged."
                                    .into(),
                            )
                        };
                        self.render_pending = true;
                    }
                }
                UiEffect::ShowSessionPicker {
                    sessions,
                    selected_session_id,
                } => {
                    self.session_tree_picker = None;
                    self.session_picker =
                        Some(SessionPicker::new(sessions, selected_session_id.as_deref()));
                    self.render_pending = true;
                }
                UiEffect::ShowSessionTreePage { page, append } => {
                    self.session_picker = None;
                    if append {
                        if let Some(picker) = self.session_tree_picker.as_mut() {
                            if let Err(notice) = picker.append(page) {
                                self.notice = Some(notice.into());
                            }
                        } else {
                            self.notice = Some(
                                "Session tree picker was closed before its next page arrived."
                                    .into(),
                            );
                        }
                    } else {
                        self.session_tree_picker = Some(SessionTreePicker::new(page));
                    }
                    self.render_pending = true;
                }
                UiEffect::CloseSessionTree => {
                    self.session_tree_picker = None;
                    self.render_pending = true;
                }
                UiEffect::RestoreSessionDraft(content) => {
                    let outcome = self.editor.insert_paste(&content);
                    self.notice = if outcome.rejected_limit {
                        Some(
                            "The restored session prompt exceeds the editor limit; it was not truncated or inserted."
                                .into(),
                        )
                    } else if outcome.ignored_controls > 0 {
                        Some(format!(
                            "Restored the session prompt after ignoring {} unsafe terminal control character(s).",
                            outcome.ignored_controls
                        ))
                    } else {
                        None
                    };
                    self.render_pending = true;
                }
                UiEffect::SendCommittedHydration {
                    command,
                    session_id: _,
                } => {
                    let value = serde_json::to_value(&command)?;
                    let payload = Bytes::from(serde_json::to_vec(&value)?);
                    if payload.len() > limit {
                        let command_id = value
                            .get("id")
                            .and_then(serde_json::Value::as_str)
                            .expect("validated RPC commands have string IDs")
                            .to_owned();
                        pending.extend(reducer::reduce(
                            &mut self.state,
                            UiAction::RejectCommittedHydration { command_id, limit },
                            &mut self.ids,
                        )?);
                        continue;
                    }
                    send_payload(writer, payload, limit).await?;
                }
                UiEffect::SendPostPromptSessionSync(command) => {
                    let value = serde_json::to_value(&command)?;
                    let payload = Bytes::from(serde_json::to_vec(&value)?);
                    if payload.len() > limit {
                        let command_id = value
                            .get("id")
                            .and_then(serde_json::Value::as_str)
                            .expect("validated RPC commands have string IDs")
                            .to_owned();
                        pending.extend(reducer::reduce(
                            &mut self.state,
                            UiAction::RejectPostPromptSessionSync { command_id, limit },
                            &mut self.ids,
                        )?);
                        continue;
                    }
                    send_payload(writer, payload, limit).await?;
                }
                UiEffect::ReplaceTranscript => self.reset_transcript_presentation(),
                UiEffect::HistoryWindowChanged => self.render_pending = true,
                UiEffect::OpenExactDetail(entry_id) => {
                    if self.browse_selected != Some(entry_id) {
                        self.state.history.active_exact_detail = None;
                        continue;
                    }
                    if let Some(presentation) = self
                        .state
                        .history
                        .active_exact_detail
                        .as_ref()
                        .filter(|detail| detail.target == entry_id)
                        .map(|detail| detail.presentation.clone())
                    {
                        self.detail_view.open(entry_id, &presentation);
                        self.browse_selected = None;
                        self.render_pending = true;
                    }
                }
                UiEffect::Notice(notice) => {
                    self.notice = Some(notice);
                    self.render_pending = true;
                }
                UiEffect::RequestRender => self.render_pending = true,
                UiEffect::Exit => control = LoopControl::Exit,
            }
        }
        Ok(control)
    }

    async fn dispatch_session_action(
        &mut self,
        action: UiAction,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let startup = matches!(&action, UiAction::StartupHydration);
        if let Some(notice) =
            self.reduced_action_frame_limit_notice(&action, "session command", limit)?
        {
            if startup {
                return Err(Error::FrameTooLarge { limit });
            }
            self.notice = Some(notice);
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        self.notice = None;
        let control = self.dispatch(action, writer, limit).await?;
        if self.notice.is_none() {
            self.editor.clear();
        }
        Ok(control)
    }

    async fn dispatch(
        &mut self,
        action: UiAction,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let automatic_decision_response = self.automatic_decision_label(&action).is_some();
        if let Some(notice) = self.automatic_decision_frame_limit_notice(&action, limit)? {
            self.unsendable_response_context = self.current_unsendable_response_context();
            self.notice = Some(notice);
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        let blocked_context = self.unsendable_response_context.clone();
        let follow_after_update = matches!(&action, UiAction::Submit(_));
        let transcript_generation = self.state.transcript.generation();
        let effects = reducer::reduce(&mut self.state, action, &mut self.ids)?;
        let transcript_replaced = effects
            .iter()
            .any(|effect| matches!(effect, UiEffect::ReplaceTranscript));
        let history_window_changed = effects
            .iter()
            .any(|effect| matches!(effect, UiEffect::HistoryWindowChanged));
        if matches!(
            self.state.view_status,
            ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
        ) {
            self.detail_view.close();
            self.state.history.active_exact_detail = None;
            self.browse_selected = None;
        } else {
            if self
                .detail_view
                .selected_entry()
                .is_some_and(|entry_id| retained_detail(&self.state, entry_id).is_none())
            {
                self.detail_view.close();
                self.state.history.active_exact_detail = None;
            }
            if self
                .browse_selected
                .is_some_and(|entry_id| retained_detail(&self.state, entry_id).is_none())
            {
                self.browse_selected = None;
            }
        }
        if !transcript_replaced && self.state.transcript.generation() != transcript_generation {
            let action = if history_window_changed {
                self.transcript_row_cache = TranscriptRowCache::default();
                TranscriptViewAction::HistoryChanged
            } else if follow_after_update {
                TranscriptViewAction::FollowTail
            } else {
                TranscriptViewAction::OutputChanged
            };
            self.transcript_viewport.reduce(
                action,
                &self.state.transcript,
                &mut self.transcript_row_cache,
            );
            self.reconcile_browse_selection();
        }
        if automatic_decision_response
            || (blocked_context.is_some()
                && blocked_context != self.current_unsendable_response_context())
        {
            self.unsendable_response_context = None;
            self.notice = None;
        }
        self.apply_effects(effects, writer, limit).await
    }

    fn automatic_decision_label(&self, action: &UiAction) -> Option<&'static str> {
        match action {
            UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(_))
                if self.state.cancel_requested =>
            {
                Some("automatic approval denial")
            }
            UiAction::BackendEvent(BackendEvent::TrustRequested { .. })
                if self.state.cancel_requested =>
            {
                Some("automatic trust denial")
            }
            _ => None,
        }
    }

    fn automatic_decision_frame_limit_notice(
        &self,
        action: &UiAction,
        limit: usize,
    ) -> Result<Option<String>, Error> {
        let Some(label) = self.automatic_decision_label(action) else {
            return Ok(None);
        };
        self.reduced_action_frame_limit_notice(action, label, limit)
    }

    fn decision_frame_limit_notice(
        &self,
        action: &UiAction,
        limit: usize,
    ) -> Result<Option<String>, Error> {
        let label = match action {
            UiAction::ApprovalDecision { .. } => "approval response",
            UiAction::TrustDecision { .. } => "trust response",
            UiAction::Cancel if self.state.pending_approval.is_some() => "approval denial",
            UiAction::Cancel if self.state.pending_trust_request_id.is_some() => "trust denial",
            _ => return Ok(None),
        };
        self.reduced_action_frame_limit_notice(action, label, limit)
    }

    fn reduced_action_frame_limit_notice(
        &self,
        action: &UiAction,
        label: &str,
        limit: usize,
    ) -> Result<Option<String>, Error> {
        if let UiAction::SelectSession { session_id } = action {
            if reducer::valid_session_id(session_id) {
                let id = self
                    .ids
                    .peek_following_prefixed_id(CommandKind::GetMessages.prefix());
                let command = WispTypedClientRpcCommands::get_messages(&id, Some(session_id))?;
                let encoded_len = serde_json::to_vec(&command)?.len();
                if encoded_len > limit {
                    return Ok(Some(format!(
                        "Skipped session selection: its {encoded_len}-byte get_messages frame exceeds the negotiated {limit}-byte limit; selection was not sent."
                    )));
                }
            }
        }
        let mut state = self.state.clone();
        let mut ids = self.ids.clone();
        let effects = reducer::reduce(&mut state, action.clone(), &mut ids)?;
        for effect in effects {
            if let UiEffect::SendCommand(command) = effect {
                let encoded_len = serde_json::to_vec(&command)?.len();
                if encoded_len > limit {
                    return Ok(Some(format!(
                        "Esc/Ctrl-C again exits. Skipped {label}: its {encoded_len}-byte RPC frame exceeds the negotiated {limit}-byte limit; the response remains pending."
                    )));
                }
            }
        }
        Ok(None)
    }

    async fn dispatch_decision(
        &mut self,
        action: UiAction,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if let Some(notice) = self.decision_frame_limit_notice(&action, limit)? {
            self.unsendable_response_context = self.current_unsendable_response_context();
            self.notice = Some(notice);
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        self.unsendable_response_context = None;
        self.notice = None;
        self.dispatch(action, writer, limit).await
    }

    fn draw<B: Backend>(
        &mut self,
        terminal: &mut Terminal<B>,
        connection: &ConnectionInfo,
    ) -> Result<(), Error> {
        let mut rendered_decision_context = None;
        terminal.draw(|frame| {
            if ui::decision_context_visible(frame.area()) {
                rendered_decision_context = match self.state.view_status {
                    ViewStatus::WaitingForApproval => {
                        self.state.pending_approval.as_ref().map(|pending| {
                            RenderedDecisionContext::Approval(pending.call_id.clone())
                        })
                    }
                    ViewStatus::WaitingForTrust => self
                        .state
                        .pending_trust_request_id
                        .clone()
                        .map(RenderedDecisionContext::Trust),
                    _ => None,
                };
            }
            if let Some(picker) = &self.session_tree_picker {
                session_tree_picker::render(frame, frame.area(), picker);
            } else if let Some(picker) = &self.session_picker {
                session_picker::render(frame, frame.area(), picker);
            } else {
                ui::render_interactive(
                    frame,
                    &self.state,
                    &mut self.transcript_viewport,
                    &mut self.transcript_row_cache,
                    &self.editor,
                    connection,
                    self.notice.as_deref(),
                    self.browse_selected,
                    Some(&mut self.detail_view),
                );
            }
        })?;
        self.rendered_decision_context = rendered_decision_context;
        self.render_pending = false;
        let rendered_browse_selection = self.browse_selected;
        self.reconcile_browse_selection();
        if self.browse_selected != rendered_browse_selection {
            self.render_pending = true;
        }
        Ok(())
    }

    fn current_decision_context(&self) -> Option<RenderedDecisionContext> {
        match self.state.view_status {
            ViewStatus::WaitingForApproval => self
                .state
                .pending_approval
                .as_ref()
                .map(|pending| RenderedDecisionContext::Approval(pending.call_id.clone())),
            ViewStatus::WaitingForTrust => self
                .state
                .pending_trust_request_id
                .clone()
                .map(RenderedDecisionContext::Trust),
            _ => None,
        }
    }

    fn current_unsendable_response_context(&self) -> Option<UnsendableResponseContext> {
        if let Some(decision) = self.current_decision_context() {
            return Some(UnsendableResponseContext::Interactive(decision));
        }
        if self.state.cancel_requested {
            return self
                .state
                .current_command
                .as_ref()
                .map(|current| UnsendableResponseContext::Cancelling(current.id.clone()));
        }
        None
    }

    fn unsendable_current_response(&self) -> bool {
        self.unsendable_response_context.is_some()
            && self.unsendable_response_context == self.current_unsendable_response_context()
    }

    fn rendered_approval_matches(&self, pending: Option<&PendingApproval>) -> bool {
        matches!(
            (&self.rendered_decision_context, pending),
            (
                Some(RenderedDecisionContext::Approval(rendered_call_id)),
                Some(pending),
            ) if rendered_call_id == &pending.call_id
        )
    }

    fn rendered_trust_matches(&self, request_id: Option<&str>) -> bool {
        matches!(
            (&self.rendered_decision_context, request_id),
            (Some(RenderedDecisionContext::Trust(rendered_request_id)), Some(request_id))
                if rendered_request_id == request_id
        )
    }

    async fn drain_backend_events(
        &mut self,
        events: &mut mpsc::Receiver<QueuedEvent>,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        while let Ok(event) = events.try_recv() {
            if self
                .dispatch(UiAction::BackendEvent(event.event), writer, limit)
                .await?
                == LoopControl::Exit
            {
                return Ok(LoopControl::Exit);
            }
        }
        Ok(LoopControl::Continue)
    }

    async fn close_transport<B: Backend>(
        &mut self,
        terminal: &mut Terminal<B>,
        connection: &ConnectionInfo,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
        error: Option<String>,
    ) -> Result<LoopControl, Error> {
        let control = self
            .dispatch(UiAction::TransportClosed { error }, writer, limit)
            .await?;
        self.draw(terminal, connection)?;
        Ok(control)
    }

    fn cancel_frame_limit_notice(&self, limit: usize) -> Result<Option<String>, Error> {
        if self.state.pending_trust_request_id.is_some() || self.state.pending_approval.is_some() {
            return self.decision_frame_limit_notice(&UiAction::Cancel, limit);
        }
        let Some(current) = self.state.current_command.as_ref() else {
            return Ok(None);
        };
        if self.state.cancel_requested {
            return Ok(None);
        }
        let id = self.ids.peek_id(CommandKind::Cancel);
        let command = WispTypedClientRpcCommands::cancel(&id, &current.id)?;
        let encoded_len = serde_json::to_vec(&command)?.len();
        if encoded_len <= limit {
            return Ok(None);
        }
        Ok(Some(format!(
            "Skipped prompt cancellation because the negotiated {limit}-byte RPC frame limit is too small."
        )))
    }

    async fn interrupt(
        &mut self,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
        quit_if_idle: bool,
    ) -> Result<LoopControl, Error> {
        if self.unsendable_current_response() {
            return Ok(LoopControl::Exit);
        }
        if self.state.session_operation.is_some() {
            return Ok(if quit_if_idle {
                LoopControl::Exit
            } else {
                LoopControl::Continue
            });
        }
        if self.idle_prompt_editable() || self.state.view_status == ViewStatus::Error {
            if quit_if_idle {
                return Ok(LoopControl::Exit);
            }
            return Ok(LoopControl::Continue);
        }
        if let Some(notice) = self.cancel_frame_limit_notice(limit)? {
            self.unsendable_response_context = self.current_unsendable_response_context();
            self.notice = Some(notice);
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        self.dispatch(UiAction::Cancel, writer, limit).await
    }

    async fn dispatch_queue_action(
        &mut self,
        action: UiAction,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        if matches!(&action, UiAction::RestoreNewestQueueDraft)
            && self.retry_deferred_queue_recovery()
        {
            return Ok(LoopControl::Continue);
        }
        let clear_editor = matches!(&action, UiAction::Steer(_) | UiAction::FollowUp(_));
        if clear_editor
            && !self.deferred_queue_recovery.is_empty()
            && !self.recovered_queue_recovery
        {
            self.notice = Some(
                "Restore deferred queued text with Alt+Up before queueing another message.".into(),
            );
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        if clear_editor
            && self.recovered_queue_recovery
            && !self.deferred_queue_recovery_can_accept(self.editor.text())
        {
            self.notice = Some(
                "Recovered text plus deferred items exceeds the queue limit; keep editing before queueing it."
                    .into(),
            );
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        let command = match &action {
            UiAction::Steer(content) => {
                if let Err(error) = self.state.queue_submission_preflight(content) {
                    self.notice = Some(error.notice().into());
                    self.render_pending = true;
                    return Ok(LoopControl::Continue);
                }
                Some((
                    "Steering",
                    WispTypedClientRpcCommands::steer(
                        &self.ids.peek_id(CommandKind::Steer),
                        content,
                    )?,
                ))
            }
            UiAction::FollowUp(content) => {
                if let Err(error) = self.state.queue_submission_preflight(content) {
                    self.notice = Some(error.notice().into());
                    self.render_pending = true;
                    return Ok(LoopControl::Continue);
                }
                Some((
                    "Follow-up",
                    WispTypedClientRpcCommands::follow_up(
                        &self.ids.peek_id(CommandKind::FollowUp),
                        content,
                    )?,
                ))
            }
            UiAction::RestoreNewestQueueDraft => {
                let Some((kind, content)) = self.state.queue_restore_candidate() else {
                    return self.dispatch(action, writer, limit).await;
                };
                if !self.editor.can_prepend_restored(content) {
                    self.notice = Some(
                        "Queued text no longer fits with your newer draft; kept the queue unchanged."
                            .into(),
                    );
                    self.render_pending = true;
                    return Ok(LoopControl::Continue);
                }
                Some((
                    "Queued-item restoration",
                    WispTypedClientRpcCommands::pop_queue(
                        &self.ids.peek_id(CommandKind::PopQueue),
                        kind,
                    )
                    .expect("validated queue kind builds a pop command"),
                ))
            }
            _ => None,
        };
        let Some((label, command)) = command else {
            return self.dispatch(action, writer, limit).await;
        };
        let payload = Bytes::from(serde_json::to_vec(&command)?);
        if payload.len() > limit {
            self.notice = Some(format!(
                "{label} encoded RPC frame is {} bytes, exceeding the negotiated {limit}-byte limit; the editor text was kept.",
                payload.len()
            ));
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        send_payload_confirmed(writer, payload, limit).await?;
        self.notice = None;
        let mut effects = reducer::reduce(&mut self.state, action, &mut self.ids)?;
        if matches!(effects.first(), Some(UiEffect::SendCommand(_))) {
            effects.remove(0);
        }
        let control = self.apply_effects(effects, writer, limit).await?;
        if clear_editor && self.notice.is_none() {
            self.editor.clear();
            self.recovered_queue_recovery = false;
        }
        Ok(control)
    }

    fn idle_prompt_editable(&self) -> bool {
        self.state.editor_editable() && self.state.current_command.is_none()
    }

    fn active_prompt_editable(&self) -> bool {
        self.state.active_prompt_editable()
    }

    fn editor_editable(&self) -> bool {
        self.state.editor_editable()
    }

    fn update_edit_notice(&mut self, outcome: EditOutcome) -> bool {
        if outcome.rejected_limit {
            self.notice = Some(format!(
                "Prompt limit is {} MiB or {} lines; edit rejected.",
                prompt_editor::MAX_PROMPT_BYTES / (1024 * 1024),
                prompt_editor::MAX_PROMPT_LINES
            ));
            return true;
        }
        if outcome.ignored_controls > 0 {
            self.notice = Some(format!(
                "Ignored {} unsafe terminal control character(s).",
                outcome.ignored_controls
            ));
            return true;
        }
        if outcome.changed {
            let changed = self.notice.is_some();
            self.notice = None;
            return changed;
        }
        false
    }

    fn prompt_frame_limit_notice(
        &self,
        prompt: &str,
        limit: usize,
    ) -> Result<Option<String>, Error> {
        let id = self.ids.peek_id(CommandKind::Prompt);
        let command = WispTypedClientRpcCommands::prompt(&id, prompt)?;
        let encoded_len = serde_json::to_vec(&command)?.len();
        if encoded_len <= limit {
            let cancel_id = self.ids.peek_following_prefixed_id("cancel");
            let cancel = WispTypedClientRpcCommands::cancel(&cancel_id, &id)?;
            let cancel_len = serde_json::to_vec(&cancel)?.len();
            if cancel_len <= limit {
                return Ok(None);
            }
            return Ok(Some(format!(
                "Prompt cancellation encoded RPC frame is {cancel_len} bytes, exceeding the negotiated {limit}-byte limit; cannot safely start this prompt."
            )));
        }
        Ok(Some(format!(
            "Prompt encoded RPC frame is {encoded_len} bytes, exceeding the negotiated {limit}-byte limit; shorten it and try again."
        )))
    }

    fn visible_detail_entries(&mut self) -> Vec<TranscriptEntryId> {
        let rows = self
            .transcript_viewport
            .visible_rows(&self.state.transcript, &mut self.transcript_row_cache);
        let mut entries = Vec::new();
        let mut seen = HashSet::new();
        for row in rows {
            if !matches!(
                row.kind,
                TranscriptRowKind::CardAction
                    | TranscriptRowKind::CardDetail
                    | TranscriptRowKind::CardOmission
            ) || !seen.insert(row.anchor.entry_id)
            {
                continue;
            }
            let eligible = self
                .state
                .transcript
                .entry(row.anchor.entry_id)
                .and_then(|entry| entry.tool_card())
                .is_some_and(|card| card.has_retained_detail())
                || self
                    .state
                    .transcript
                    .exact_historical_detail_target(row.anchor.entry_id)
                    .is_some();
            if eligible {
                entries.push(row.anchor.entry_id);
            }
        }
        entries
    }

    fn enter_or_cycle_browse(&mut self) {
        if matches!(
            self.state.view_status,
            ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
        ) {
            return;
        }
        let entries = self.visible_detail_entries();
        if entries.is_empty() {
            self.browse_selected = None;
            self.notice =
                Some("No visible tool card has retained detail; scroll one into view.".into());
        } else if let Some(selected) = self.browse_selected {
            let next = entries
                .iter()
                .position(|entry| *entry == selected)
                .map_or(entries.len() - 1, |index| (index + 1) % entries.len());
            self.browse_selected = Some(entries[next]);
            self.notice =
                Some("Card browse: Tab/Shift-Tab select · Enter details · Esc prompt".into());
        } else {
            self.browse_selected = entries.last().copied();
            self.notice =
                Some("Card browse: Tab/Shift-Tab select · Enter details · Esc prompt".into());
        }
        self.render_pending = true;
    }

    fn cycle_browse(&mut self, reverse: bool) {
        let entries = self.visible_detail_entries();
        if entries.is_empty() {
            self.browse_selected = None;
            self.notice =
                Some("No visible tool card has retained detail; scroll one into view.".into());
            self.render_pending = true;
            return;
        }
        let current = self
            .browse_selected
            .and_then(|selected| entries.iter().position(|entry| *entry == selected));
        let index = match (current, reverse) {
            (Some(0), true) | (None, true) => entries.len() - 1,
            (Some(index), true) => index - 1,
            (Some(index), false) => (index + 1) % entries.len(),
            (None, false) => 0,
        };
        self.browse_selected = Some(entries[index]);
        self.render_pending = true;
    }

    fn reconcile_browse_selection(&mut self) {
        if self.browse_selected.is_none() || self.detail_view.is_open() {
            return;
        }
        let entries = self.visible_detail_entries();
        if !entries.contains(&self.browse_selected.expect("checked above")) {
            self.browse_selected = entries.last().copied();
            self.notice = if self.browse_selected.is_some() {
                Some("Card browse: Tab/Shift-Tab select · Enter details · Esc prompt".into())
            } else {
                Some("No visible tool card has retained detail; scroll one into view.".into())
            };
        }
    }

    fn open_selected_detail(&mut self) {
        let Some(entry_id) = self.browse_selected else {
            return;
        };
        let Some(presentation) = retained_detail(&self.state, entry_id) else {
            self.reconcile_browse_selection();
            return;
        };
        self.detail_view.open(entry_id, presentation);
        self.notice = None;
        self.render_pending = true;
    }

    async fn request_selected_detail(
        &mut self,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let Some(entry_id) = self.browse_selected else {
            return Ok(LoopControl::Continue);
        };
        if self
            .state
            .transcript
            .exact_historical_detail_target(entry_id)
            .is_none()
        {
            self.open_selected_detail();
            return Ok(LoopControl::Continue);
        }
        let action = UiAction::LoadExactDetail { target: entry_id };
        if let Some(notice) =
            self.reduced_action_frame_limit_notice(&action, "exact detail request", limit)?
        {
            self.notice = Some(notice);
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        self.dispatch(action, writer, limit).await
    }

    async fn handle_session_picker_key(
        &mut self,
        key: KeyEvent,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let Some(picker) = self.session_picker.as_mut() else {
            return Ok(LoopControl::Continue);
        };
        match picker.handle_key(key) {
            SessionPickerAction::None => {
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            SessionPickerAction::Cancelled => {
                self.session_picker = None;
                self.notice = Some("Session selection cancelled.".into());
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            SessionPickerAction::Selected(session_id) => {
                if !reducer::valid_session_id(&session_id) {
                    self.notice =
                        Some("Session ID is empty or exceeds the 4096-byte limit.".into());
                    self.render_pending = true;
                    return Ok(LoopControl::Continue);
                }
                if let Some(notice) = self.reduced_action_frame_limit_notice(
                    &UiAction::SelectSession {
                        session_id: session_id.clone(),
                    },
                    "session command",
                    limit,
                )? {
                    self.notice = Some(notice);
                    self.render_pending = true;
                    return Ok(LoopControl::Continue);
                }
                self.session_picker = None;
                self.dispatch(UiAction::SelectSession { session_id }, writer, limit)
                    .await
            }
        }
    }

    async fn handle_session_tree_picker_key(
        &mut self,
        key: KeyEvent,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let Some(picker) = self.session_tree_picker.as_mut() else {
            return Ok(LoopControl::Continue);
        };
        let action = picker.handle_key(key);
        if self.state.session_operation.is_some()
            && matches!(
                action,
                SessionTreePickerAction::LoadNext(_)
                    | SessionTreePickerAction::Navigate(_)
                    | SessionTreePickerAction::Fork(_)
            )
        {
            return Ok(LoopControl::Continue);
        }
        match action {
            SessionTreePickerAction::None => {
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            SessionTreePickerAction::Cancelled => {
                self.session_tree_picker = None;
                self.notice = None;
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            SessionTreePickerAction::ForkUnavailable => {
                self.notice = Some("Only persisted user-message nodes can be forked.".into());
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            SessionTreePickerAction::LoadNext(after_entry_id) => {
                self.dispatch_session_action(
                    UiAction::LoadSessionTree {
                        after_entry_id: Some(after_entry_id),
                    },
                    writer,
                    limit,
                )
                .await
            }
            SessionTreePickerAction::Navigate(entry_id) => {
                self.dispatch_session_action(
                    UiAction::NavigateSessionTree { entry_id },
                    writer,
                    limit,
                )
                .await
            }
            SessionTreePickerAction::Fork(entry_id) => {
                self.dispatch_session_action(UiAction::ForkSession { entry_id }, writer, limit)
                    .await
            }
        }
    }

    fn handle_detail_key(&mut self, key: KeyEvent) -> LoopControl {
        if matches!(key.code, KeyCode::Esc | KeyCode::Enter | KeyCode::Char(' ')) {
            self.detail_view.close();
            self.state.history.active_exact_detail = None;
            self.render_pending = true;
            return LoopControl::Continue;
        }
        let Some(entry_id) = self.detail_view.selected_entry() else {
            return LoopControl::Continue;
        };
        let Some(presentation) = retained_detail(&self.state, entry_id) else {
            self.detail_view.close();
            self.browse_selected = None;
            self.notice = Some("Retained detail is no longer available.".into());
            self.render_pending = true;
            return LoopControl::Continue;
        };
        match key.code {
            KeyCode::Up => self.detail_view.scroll_lines(presentation, -1),
            KeyCode::Down => self.detail_view.scroll_lines(presentation, 1),
            KeyCode::PageUp => self.detail_view.page_up(presentation),
            KeyCode::PageDown => self.detail_view.page_down(presentation),
            KeyCode::Home => self.detail_view.home(presentation),
            KeyCode::End => self.detail_view.end(presentation),
            _ => return LoopControl::Continue,
        }
        self.render_pending = true;
        LoopControl::Continue
    }

    async fn handle_browse_key(
        &mut self,
        key: KeyEvent,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        match key.code {
            KeyCode::Esc => {
                self.browse_selected = None;
                self.notice = None;
            }
            KeyCode::F(6) | KeyCode::Tab => self.cycle_browse(false),
            KeyCode::BackTab => self.cycle_browse(true),
            KeyCode::Enter | KeyCode::Char(' ') => {
                return self.request_selected_detail(writer, limit).await;
            }
            _ if transcript_view_action(key).is_some() => {
                let control = self.navigate_transcript(key, writer, limit).await?;
                self.reconcile_browse_selection();
                return Ok(control);
            }
            _ => {}
        }
        self.render_pending = true;
        Ok(LoopControl::Continue)
    }

    async fn navigate_transcript(
        &mut self,
        key: KeyEvent,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let action = transcript_view_action(key).expect("navigation key is prefiltered");
        self.transcript_viewport.reduce(
            action,
            &self.state.transcript,
            &mut self.transcript_row_cache,
        );
        let history_action = match action {
            TranscriptViewAction::PageUp | TranscriptViewAction::Home
                if self
                    .transcript_viewport
                    .at_oldest(&self.state.transcript, &mut self.transcript_row_cache) =>
            {
                Some(UiAction::LoadOlderHistory)
            }
            TranscriptViewAction::PageDown | TranscriptViewAction::FollowTail
                if self.transcript_viewport.follows_tail() && self.state.history.tail_evicted =>
            {
                Some(UiAction::LoadNewerHistory)
            }
            _ => None,
        };
        if let Some(history_action) = history_action {
            if let Some(notice) =
                self.reduced_action_frame_limit_notice(&history_action, "history request", limit)?
            {
                self.notice = Some(notice);
                self.render_pending = true;
                return Ok(LoopControl::Continue);
            }
            return self.dispatch(history_action, writer, limit).await;
        }
        self.render_pending = true;
        Ok(LoopControl::Continue)
    }

    async fn handle_input(
        &mut self,
        input: Input,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        match input {
            Input::Key(key) if is_ctrl_c(key) => self.interrupt(writer, limit, true).await,
            Input::Key(key) if self.session_tree_picker.is_some() => {
                self.handle_session_tree_picker_key(key, writer, limit)
                    .await
            }
            Input::Paste(_) if self.session_tree_picker.is_some() => Ok(LoopControl::Continue),
            Input::Key(key) if self.session_picker.is_some() => {
                self.handle_session_picker_key(key, writer, limit).await
            }
            Input::Paste(_) if self.session_picker.is_some() => Ok(LoopControl::Continue),
            Input::Key(key) if self.detail_view.is_open() => Ok(self.handle_detail_key(key)),
            Input::Paste(_) if self.detail_view.is_open() => Ok(LoopControl::Continue),
            Input::Key(key) if self.browse_selected.is_some() => {
                self.handle_browse_key(key, writer, limit).await
            }
            Input::Paste(_) if self.browse_selected.is_some() => Ok(LoopControl::Continue),
            Input::Key(key) if is_browse_key(key) => {
                self.enter_or_cycle_browse();
                Ok(LoopControl::Continue)
            }
            Input::Key(key) if is_escape(key) => self.interrupt(writer, limit, false).await,
            Input::Key(key)
                if transcript_view_action(key).is_some()
                    && !matches!(
                        self.state.view_status,
                        ViewStatus::WaitingForApproval | ViewStatus::WaitingForTrust
                    ) =>
            {
                self.navigate_transcript(key, writer, limit).await
            }
            Input::Key(key) if self.state.view_status == ViewStatus::WaitingForApproval => {
                let context_visible =
                    self.rendered_approval_matches(self.state.pending_approval.as_ref());
                match approval_decision(key, self.state.pending_approval.as_ref()) {
                    Some(UiAction::ApprovalDecision { approved: true, .. }) if !context_visible => {
                        Ok(LoopControl::Continue)
                    }
                    Some(action) => self.dispatch_decision(action, writer, limit).await,
                    None if key_can_edit(key) => {
                        if !self.unsendable_current_response() {
                            self.notice = Some(
                                "Approve with y once, t tool, a all, or deny with n/Esc.".into(),
                            );
                        }
                        self.render_pending = true;
                        Ok(LoopControl::Continue)
                    }
                    None => Ok(LoopControl::Continue),
                }
            }
            Input::Key(key) if self.state.view_status == ViewStatus::WaitingForTrust => {
                let context_visible =
                    self.rendered_trust_matches(self.state.pending_trust_request_id.as_deref());
                match trust_decision(key, self.state.pending_trust_request_id.as_deref()) {
                    Some(UiAction::TrustDecision { trusted: true, .. }) if !context_visible => {
                        Ok(LoopControl::Continue)
                    }
                    Some(action) => self.dispatch_decision(action, writer, limit).await,
                    None if key_can_edit(key) => {
                        if !self.unsendable_current_response() {
                            self.notice = Some("Trust with y, or deny with n/Esc.".into());
                        }
                        self.render_pending = true;
                        Ok(LoopControl::Continue)
                    }
                    None => Ok(LoopControl::Continue),
                }
            }
            Input::Key(key)
                if self.editor_editable()
                    && key.code == KeyCode::Up
                    && key.modifiers == KeyModifiers::ALT =>
            {
                if self.retry_deferred_queue_recovery() {
                    Ok(LoopControl::Continue)
                } else if self.active_prompt_editable() {
                    self.dispatch_queue_action(UiAction::RestoreNewestQueueDraft, writer, limit)
                        .await
                } else {
                    Ok(LoopControl::Continue)
                }
            }
            Input::Key(key) if self.active_prompt_editable() && active_queue_submit(key) => {
                let action = if key.modifiers.contains(KeyModifiers::ALT) {
                    UiAction::FollowUp(self.editor.text().to_owned())
                } else {
                    UiAction::Steer(self.editor.text().to_owned())
                };
                self.dispatch_queue_action(action, writer, limit).await
            }
            Input::Key(key) if self.editor_editable() => match self.editor.handle_key(key) {
                EditorAction::Submit => {
                    let prompt = self.editor.text().to_owned();
                    match session_command(&prompt) {
                        SessionCommand::ResumeCatalog => {
                            self.dispatch_session_action(
                                UiAction::LoadSessionCatalog,
                                writer,
                                limit,
                            )
                            .await
                        }
                        SessionCommand::ResumeSession(session_id) => {
                            if !reducer::valid_session_id(&session_id) {
                                self.notice = Some(
                                    "Session ID is empty or exceeds the 4096-byte limit.".into(),
                                );
                                self.render_pending = true;
                                return Ok(LoopControl::Continue);
                            }
                            self.dispatch_session_action(
                                UiAction::SelectSession { session_id },
                                writer,
                                limit,
                            )
                            .await
                        }
                        SessionCommand::New => {
                            self.dispatch_session_action(UiAction::NewSession, writer, limit)
                                .await
                        }
                        SessionCommand::Name(name) => {
                            self.dispatch_session_action(
                                UiAction::SetSessionName(name),
                                writer,
                                limit,
                            )
                            .await
                        }
                        SessionCommand::Clone => {
                            self.dispatch_session_action(UiAction::CloneSession, writer, limit)
                                .await
                        }
                        SessionCommand::Tree => {
                            self.dispatch_session_action(
                                UiAction::LoadSessionTree {
                                    after_entry_id: None,
                                },
                                writer,
                                limit,
                            )
                            .await
                        }
                        SessionCommand::Unrevert => {
                            self.dispatch_session_action(
                                UiAction::UnrevertSessionTree,
                                writer,
                                limit,
                            )
                            .await
                        }
                        SessionCommand::Invalid(usage) => {
                            self.notice = Some(usage.into());
                            self.render_pending = true;
                            Ok(LoopControl::Continue)
                        }
                        SessionCommand::Prompt => {
                            if prompt.trim().is_empty() {
                                self.notice =
                                    Some("Enter a non-empty prompt before sending.".into());
                                self.render_pending = true;
                                return Ok(LoopControl::Continue);
                            }
                            if let Some(notice) = self.prompt_frame_limit_notice(&prompt, limit)? {
                                self.notice = Some(notice);
                                self.render_pending = true;
                                return Ok(LoopControl::Continue);
                            }
                            self.notice = None;
                            let control = self
                                .dispatch(UiAction::Submit(prompt), writer, limit)
                                .await?;
                            if self.notice.is_none() {
                                self.editor.clear();
                            }
                            Ok(control)
                        }
                    }
                }
                EditorAction::Edit(outcome) => {
                    let notice_changed = self.update_edit_notice(outcome);
                    if outcome.changed || notice_changed {
                        self.render_pending = true;
                    }
                    Ok(LoopControl::Continue)
                }
                EditorAction::Ignored => Ok(LoopControl::Continue),
            },
            Input::Paste(pasted) if self.editor_editable() => {
                let outcome = self.editor.insert_paste(&pasted);
                let notice_changed = self.update_edit_notice(outcome);
                if outcome.changed || notice_changed {
                    self.render_pending = true;
                }
                Ok(LoopControl::Continue)
            }
            Input::Key(key) if key_can_edit(key) => {
                if !self.unsendable_current_response() {
                    self.notice = Some("Prompt input is unavailable while Wisp is busy.".into());
                }
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            Input::Paste(_) => {
                if !self.unsendable_current_response() {
                    self.notice = Some("Prompt input is unavailable while Wisp is busy.".into());
                }
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            Input::Redraw => {
                self.rendered_decision_context = None;
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            Input::Error(error) => Err(Error::Io(error)),
            Input::Key(_) => Ok(LoopControl::Continue),
        }
    }
}

fn retained_detail(
    state: &UiState,
    entry_id: TranscriptEntryId,
) -> Option<&ToolDetailPresentation> {
    if let Some(detail) = state
        .history
        .active_exact_detail
        .as_ref()
        .filter(|detail| detail.target == entry_id)
    {
        return Some(&detail.presentation);
    }
    let card = state.transcript.entry(entry_id)?.tool_card()?;
    let DetailAvailability::LiveRetained(presentation) = &card.structured_detail else {
        return None;
    };
    Some(presentation)
}

fn is_ctrl_c(key: KeyEvent) -> bool {
    key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL)
}

fn is_escape(key: KeyEvent) -> bool {
    key.code == KeyCode::Esc
}

fn is_browse_key(key: KeyEvent) -> bool {
    key.code == KeyCode::F(6) && key.modifiers == KeyModifiers::NONE
}

fn active_queue_submit(key: KeyEvent) -> bool {
    key.code == KeyCode::Enter && matches!(key.modifiers, KeyModifiers::NONE | KeyModifiers::ALT)
}

fn transcript_view_action(key: KeyEvent) -> Option<TranscriptViewAction> {
    match (key.code, key.modifiers) {
        (KeyCode::PageUp, KeyModifiers::NONE) => Some(TranscriptViewAction::PageUp),
        (KeyCode::PageDown, KeyModifiers::NONE) => Some(TranscriptViewAction::PageDown),
        (KeyCode::Home, modifiers) if modifiers.contains(KeyModifiers::CONTROL) => {
            Some(TranscriptViewAction::Home)
        }
        (KeyCode::Up, modifiers) if modifiers.contains(KeyModifiers::CONTROL) => {
            Some(TranscriptViewAction::ScrollLines(-1))
        }
        (KeyCode::Down, modifiers) if modifiers.contains(KeyModifiers::CONTROL) => {
            Some(TranscriptViewAction::ScrollLines(1))
        }
        (KeyCode::End, modifiers) if modifiers.contains(KeyModifiers::CONTROL) => {
            Some(TranscriptViewAction::FollowTail)
        }
        _ => None,
    }
}

fn printable_char(key: KeyEvent) -> Option<char> {
    match key.code {
        KeyCode::Char(character)
            if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
        {
            Some(character)
        }
        _ => None,
    }
}

fn approval_decision(key: KeyEvent, pending: Option<&PendingApproval>) -> Option<UiAction> {
    let pending = pending?;
    let approved = match printable_char(key)?.to_ascii_lowercase() {
        'y' => true,
        't' => true,
        'a' => true,
        'n' => false,
        _ => return None,
    };
    let scope = match printable_char(key)?.to_ascii_lowercase() {
        't' => Some(ApprovalScope::ToolSession),
        'a' => Some(ApprovalScope::AllSession),
        'y' => Some(ApprovalScope::Once),
        _ => None,
    };
    Some(UiAction::ApprovalDecision {
        call_id: pending.call_id.clone(),
        approved,
        reason: None,
        scope,
    })
}

fn trust_decision(key: KeyEvent, request_id: Option<&str>) -> Option<UiAction> {
    let request_id = request_id?;
    let trusted = match printable_char(key)?.to_ascii_lowercase() {
        'y' => true,
        'n' => false,
        _ => return None,
    };
    Some(UiAction::TrustDecision {
        request_id: request_id.to_owned(),
        trusted,
        reason: None,
        transient: Some(false),
    })
}

fn key_can_edit(key: KeyEvent) -> bool {
    matches!(
        key.code,
        KeyCode::Char(_)
            | KeyCode::Enter
            | KeyCode::Tab
            | KeyCode::Backspace
            | KeyCode::Delete
            | KeyCode::Left
            | KeyCode::Right
            | KeyCode::Up
            | KeyCode::Down
            | KeyCode::Home
            | KeyCode::End
    )
}

pub async fn run_from_env() -> Result<(), Error> {
    run(Cli::parse()).await
}

async fn run(cli: Cli) -> Result<(), Error> {
    validate_frontend_version(&cli.expected_backend_version)?;
    let _panic_hook = PanicHookGuard::install();
    let (mut backend, stdin, stdout, stderr) = BackendProcess::spawn(&cli.backend)?;
    let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
    let (handshake_tx, handshake_rx) = oneshot::channel();
    let (event_tx, mut event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
    let event_wire_budget = Arc::new(Semaphore::new(EVENT_RETAINED_WIRE_BYTES));
    let (reader_outcome_tx, reader_outcome_rx) = oneshot::channel();
    let mut writer = Some(tokio::spawn(writer_task(stdin, writer_rx)));
    let reader = tokio::spawn(stdout_reader_task(
        stdout,
        handshake_tx,
        event_tx,
        event_wire_budget,
        reader_outcome_tx,
    ));
    let stderr_drainer = tokio::spawn(stderr_drainer_task(stderr));
    let mut reader_outcome = Some(reader_outcome_rx);
    let mut events_open = true;

    let result = async {
        let request = RpcHandshakeRequest::current("wisp-rust-tui", &cli.expected_backend_version)?;
        send_value(&writer_tx, &request, HANDSHAKE_FRAME_BYTES).await?;
        let response = match timeout(HANDSHAKE_TIMEOUT, handshake_rx).await {
            Ok(Ok(Ok(response))) => response,
            Ok(Ok(Err(error))) => return Err(error),
            Ok(Err(_)) => return Err(Error::HandshakeEof),
            Err(_) => return Err(Error::HandshakeTimeout),
        };
        if let Some((code, message)) = response.rejection() {
            return Err(Error::HandshakeRejected { code, message });
        }
        let actual_version = response.backend_package_version();
        if actual_version != cli.expected_backend_version {
            return Err(Error::BackendVersionMismatch {
                expected: cli.expected_backend_version.clone(),
                actual: actual_version,
            });
        }
        let (protocol, events, max_client_frame, _max_server_frame) = response
            .accepted_contract()
            .expect("non-rejected validated handshake must be accepted");
        if protocol != LIVE_RPC_PROTOCOL_VERSION || events != EVENT_SCHEMA_VERSION {
            return Err(Error::ContractMismatch { protocol, events });
        }

        let mut terminal = TerminalGuard::enter()?;
        let (input_tx, mut input_rx) = mpsc::channel(INPUT_CHANNEL_CAPACITY);
        let (input_stop_tx, input_stop_rx) = watch::channel(false);
        let input = tokio::task::spawn_blocking(move || input_task(input_tx, input_stop_rx));
        let connection = ConnectionInfo {
            backend_version: actual_version,
            protocol_version: protocol,
            event_schema_version: events,
        };
        let mut live_ui = LiveUi::default();
        let mut transport_closed_diagnostic = None;
        let loop_result = async {
            live_ui
                .dispatch_session_action(UiAction::StartupHydration, &writer_tx, max_client_frame)
                .await?;
            let mut redraw = interval(FRAME_INTERVAL);
            redraw.set_missed_tick_behavior(MissedTickBehavior::Skip);
            loop {
                match input_rx.try_recv() {
                    Ok(input) => {
                        if live_ui
                            .handle_input(input, &writer_tx, max_client_frame)
                            .await?
                            == LoopControl::Exit
                        {
                            break Ok(());
                        }
                        continue;
                    }
                    Err(TryRecvError::Empty) => {}
                    Err(TryRecvError::Disconnected) => break Ok(()),
                }
                tokio::select! {
                    input = input_rx.recv() => {
                        match input {
                            Some(input) => {
                                if live_ui.handle_input(input, &writer_tx, max_client_frame).await?
                                    == LoopControl::Exit
                                {
                                    break Ok(());
                                }
                            }
                            None => break Ok(()),
                        }
                    }
                    event = receive_event(&mut event_rx, events_open) => {
                        match event {
                            Some(event) => {
                                if live_ui.dispatch(
                                    UiAction::BackendEvent(event.event),
                                    &writer_tx,
                                    max_client_frame,
                                ).await? == LoopControl::Exit {
                                    break Ok(());
                                }
                            }
                            None => events_open = false,
                        }
                    }
                    outcome = receive_reader_outcome(&mut reader_outcome) => {
                        reader_outcome = None;
                        match outcome {
                            Ok(ReaderTermination::Eof) => {
                                live_ui
                                    .drain_backend_events(
                                        &mut event_rx,
                                        &writer_tx,
                                        max_client_frame,
                                    )
                                    .await?;
                                live_ui
                                    .close_transport(
                                        terminal.terminal(),
                                        &connection,
                                        &writer_tx,
                                        max_client_frame,
                                        None,
                                    )
                                    .await?;
                                transport_closed_diagnostic =
                                    Some(render_transport_closed_diagnostic(&live_ui.state));
                                backend
                                    .wait_gracefully(Duration::from_millis(100))
                                    .await?;
                                let error = match backend.try_wait()? {
                                    Some(status) => classify_backend_exit(status),
                                    None => Error::BackendStreamEnded,
                                };
                                break Err(error);
                            }
                            Err(error) => break Err(error),
                        }
                    }
                    signal = tokio::signal::ctrl_c() => {
                        signal?;
                        if live_ui.interrupt(&writer_tx, max_client_frame, true).await?
                            == LoopControl::Exit
                        {
                            break Ok(());
                        }
                    }
                    _ = redraw.tick() => {
                        if live_ui.render_pending {
                            live_ui.draw(terminal.terminal(), &connection)?;
                        }
                    }
                }
            }
        }
        .await;
        let _ = input_stop_tx.send(true);
        drop(input_rx);
        let input_result = input.await.map_err(Error::Task).and_then(|result| result);
        drop(terminal);
        let unsent_queue_diagnostics =
            render_unsent_queue_diagnostics(&live_ui.state, &live_ui.deferred_queue_recovery);
        if let Some(diagnostic) = transport_closed_diagnostic {
            eprintln!("{diagnostic}");
        }
        for diagnostic in unsent_queue_diagnostics {
            eprintln!("{diagnostic}");
        }
        loop_result?;
        input_result?;

        queue_shutdown_and_close(&writer_tx, max_client_frame).await?;
        let shutdown_writer = writer.take().expect("RPC writer is still owned");
        finish_task("RPC writer", shutdown_writer)
            .await
            .and_then(|result| result)?;
        let deadline = Instant::now() + GRACEFUL_SHUTDOWN_TIMEOUT;
        let mut shutdown = ShutdownObservation::default();
        while Instant::now() < deadline {
            if let Some(status) = backend.try_wait()? {
                shutdown.observe_exit(status)?;
            }
            if shutdown.completed() {
                return Ok(());
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            tokio::select! {
                event = receive_event(&mut event_rx, events_open) => {
                    match event {
                        Some(event) => shutdown.observe_event(&event.event)?,
                        None => events_open = false,
                    }
                }
                outcome = receive_reader_outcome(&mut reader_outcome) => {
                    reader_outcome = None;
                    shutdown_reader_outcome(outcome)?;
                }
                _ = tokio::time::sleep(remaining.min(Duration::from_millis(100))) => {}
            }
        }
        if let Some(status) = backend.try_wait()? {
            shutdown.observe_exit(status)?;
        }
        if shutdown.completed() {
            return Ok(());
        }
        Err(shutdown.deadline_error())
    }
    .await;

    let _ = writer_tx.try_send(WriterMessage::Close);
    drop(writer_tx);
    let cleanup = match backend.wait_gracefully(Duration::from_millis(100)).await {
        Ok(true) => Ok(()),
        Ok(false) => backend
            .terminate_then_kill()
            .await
            .and_then(cleanup_outcome_result),
        Err(error) => Err(error),
    };
    let writer_result = match writer {
        Some(writer) => finish_task("RPC writer", writer)
            .await
            .and_then(|result| result),
        None => Ok(()),
    };
    let reader_result = finish_task("RPC reader", reader).await;
    let stderr_result = finish_task("stderr drainer", stderr_drainer)
        .await
        .and_then(|result| result);
    let final_result = result.and(cleanup).and(writer_result).and(reader_result);
    if let Ok(capture) = &stderr_result {
        if final_result.is_err() && !capture.bytes.is_empty() {
            let sanitized = sanitize_backend_stderr(&capture.bytes);
            eprintln!(
                "backend stderr tail (retained_bytes={}, dropped_bytes={}):\n{}",
                capture.bytes.len(),
                capture.dropped_bytes,
                sanitized
            );
        }
    }
    final_result.and(stderr_result.map(|_| ()))
}

fn validate_frontend_version(expected: &str) -> Result<(), Error> {
    let frontend = env!("CARGO_PKG_VERSION");
    if expected == frontend {
        return Ok(());
    }
    Err(Error::FrontendVersionMismatch {
        expected: expected.to_owned(),
        frontend: frontend.to_owned(),
    })
}

async fn receive_event(
    events: &mut mpsc::Receiver<QueuedEvent>,
    open: bool,
) -> Option<QueuedEvent> {
    if open {
        return events.recv().await;
    }
    pending().await
}

async fn receive_reader_outcome(
    outcome: &mut Option<oneshot::Receiver<Result<ReaderTermination, Error>>>,
) -> Result<ReaderTermination, Error> {
    match outcome {
        Some(outcome) => outcome.await.map_err(|_| Error::ReaderStopped)?,
        None => pending().await,
    }
}

fn shutdown_reader_outcome(outcome: Result<ReaderTermination, Error>) -> Result<(), Error> {
    match outcome {
        Ok(ReaderTermination::Eof) => Ok(()),
        Err(error) => Err(error),
    }
}

fn classify_backend_exit(status: std::process::ExitStatus) -> Error {
    if status.success() {
        Error::BackendExited(status)
    } else {
        Error::BackendExitFailure(status)
    }
}

fn cleanup_outcome_result(outcome: CleanupOutcome) -> Result<(), Error> {
    match outcome {
        CleanupOutcome::AlreadyExited => Ok(()),
        CleanupOutcome::Terminated => Err(Error::CleanupEscalated { stage: "SIGTERM" }),
        CleanupOutcome::Killed => Err(Error::CleanupEscalated { stage: "SIGKILL" }),
    }
}

async fn finish_task<T>(name: &'static str, mut task: JoinHandle<T>) -> Result<T, Error> {
    match timeout(TASK_JOIN_TIMEOUT, &mut task).await {
        Ok(result) => result.map_err(Error::Task),
        Err(_) => {
            task.abort();
            let _ = task.await;
            Err(Error::TaskTimeout(name))
        }
    }
}

async fn send_value<T: serde::Serialize>(
    writer: &mpsc::Sender<WriterMessage>,
    value: &T,
    limit: usize,
) -> Result<(), Error> {
    let payload = Bytes::from(serde_json::to_vec(value)?);
    send_payload(writer, payload, limit).await
}

async fn send_payload(
    writer: &mpsc::Sender<WriterMessage>,
    payload: Bytes,
    limit: usize,
) -> Result<(), Error> {
    if payload.len() > limit {
        return Err(Error::FrameTooLarge { limit });
    }
    writer
        .send(WriterMessage::Frame {
            payload,
            limit,
            ack: None,
        })
        .await
        .map_err(|_| Error::WriterStopped)
}

async fn send_payload_confirmed(
    writer: &mpsc::Sender<WriterMessage>,
    payload: Bytes,
    limit: usize,
) -> Result<(), Error> {
    if payload.len() > limit {
        return Err(Error::FrameTooLarge { limit });
    }
    let (ack_tx, ack_rx) = oneshot::channel();
    timeout(HANDSHAKE_TIMEOUT, async {
        writer
            .send(WriterMessage::Frame {
                payload,
                limit,
                ack: Some(ack_tx),
            })
            .await
            .map_err(|_| Error::WriterStopped)?;
        ack_rx
            .await
            .map_err(|_| Error::WriterStopped)?
            .map_err(|_| Error::WriterStopped)
    })
    .await
    .map_err(|_| Error::QueueSubmissionTimeout)?
}

async fn queue_shutdown_and_close(
    writer: &mpsc::Sender<WriterMessage>,
    limit: usize,
) -> Result<(), Error> {
    let shutdown = WispTypedClientRpcCommands::shutdown(SHUTDOWN_COMMAND_ID)?;
    send_value(writer, &shutdown, limit).await?;
    writer
        .send(WriterMessage::Close)
        .await
        .map_err(|_| Error::WriterStopped)
}

async fn writer_task<W: AsyncWrite + Unpin>(
    mut writer: W,
    mut messages: mpsc::Receiver<WriterMessage>,
) -> Result<(), Error> {
    while let Some(message) = messages.recv().await {
        match message {
            WriterMessage::Frame {
                payload,
                limit,
                ack,
            } => {
                let result = async {
                    if payload.len() > limit {
                        return Err(Error::FrameTooLarge { limit });
                    }
                    writer.write_all(&payload).await?;
                    writer.write_all(b"\n").await?;
                    writer.flush().await?;
                    Ok(())
                }
                .await;
                match result {
                    Ok(()) => {
                        if let Some(ack) = ack {
                            let _ = ack.send(Ok(()));
                        }
                    }
                    Err(error) => {
                        if let Some(ack) = ack {
                            let _ = ack.send(Err(()));
                        }
                        return Err(error);
                    }
                }
            }
            WriterMessage::Close => break,
        }
    }
    writer.shutdown().await?;
    Ok(())
}

async fn stdout_reader_task<R: AsyncRead + Unpin>(
    reader: R,
    handshake: oneshot::Sender<Result<RpcHandshakeResponse, Error>>,
    events: mpsc::Sender<QueuedEvent>,
    event_wire_budget: Arc<Semaphore>,
    outcome: oneshot::Sender<Result<ReaderTermination, Error>>,
) {
    let mut frames = FrameReader::new(reader);
    let handshake_frame = match frames.read_frame(HANDSHAKE_FRAME_BYTES).await {
        Ok(Some(frame)) => frame,
        Ok(None) => {
            let _ = handshake.send(Err(Error::HandshakeEof));
            return;
        }
        Err(error) => {
            let _ = handshake.send(Err(error));
            return;
        }
    };
    let response = match serde_json::from_slice::<RpcHandshakeResponse>(&handshake_frame) {
        Ok(response) => response,
        Err(error) => {
            let _ = handshake.send(Err(Error::InvalidProtocolFrame(error)));
            return;
        }
    };
    let server_limit = response
        .accepted_contract()
        .map_or(HANDSHAKE_FRAME_BYTES, |contract| contract.3);
    if handshake.send(Ok(response)).is_err() {
        return;
    }
    let result = loop {
        match frames.read_frame(server_limit).await {
            Ok(Some(frame)) => match serde_json::from_slice::<WispCurrentLiveEventOutput>(&frame) {
                Ok(event) => {
                    if event.schema_version() != EVENT_SCHEMA_VERSION {
                        break Err(Error::ContractMismatch {
                            protocol: LIVE_RPC_PROTOCOL_VERSION,
                            events: event.schema_version(),
                        });
                    }
                    let wire_bytes = u32::try_from(frame.len())
                        .expect("negotiated event frame lengths fit in u32");
                    let permit =
                        match Arc::clone(&event_wire_budget).try_acquire_many_owned(wire_bytes) {
                            Ok(permit) => permit,
                            Err(_) => break Err(Error::InboundOverloaded),
                        };
                    let event = match BackendEvent::from_live(&event) {
                        Ok(event) => event,
                        Err(error) => break Err(Error::EventProjection(error)),
                    };
                    let queued = QueuedEvent {
                        event,
                        _wire_bytes: permit,
                    };
                    match events.try_send(queued) {
                        Ok(()) => {}
                        Err(TrySendError::Full(_)) => break Err(Error::InboundOverloaded),
                        Err(TrySendError::Closed(_)) => break Err(Error::ReaderStopped),
                    }
                }
                Err(error) => break Err(Error::InvalidProtocolFrame(error)),
            },
            Ok(None) => break Ok(ReaderTermination::Eof),
            Err(error) => break Err(error),
        }
    };
    let _ = outcome.send(result);
}

async fn stderr_drainer_task<R: AsyncRead + Unpin>(mut stderr: R) -> Result<StderrCapture, Error> {
    let mut retained = VecDeque::with_capacity(STDERR_RETAINED_BYTES);
    let mut dropped_bytes = 0_usize;
    let mut chunk = [0_u8; 8192];
    loop {
        let read = stderr.read(&mut chunk).await?;
        if read == 0 {
            break;
        }
        if read >= STDERR_RETAINED_BYTES {
            dropped_bytes = dropped_bytes
                .saturating_add(retained.len())
                .saturating_add(read - STDERR_RETAINED_BYTES);
            retained.clear();
            retained.extend(&chunk[read - STDERR_RETAINED_BYTES..read]);
            continue;
        }
        let overflow = retained
            .len()
            .saturating_add(read)
            .saturating_sub(STDERR_RETAINED_BYTES);
        if overflow > 0 {
            retained.drain(..overflow);
            dropped_bytes = dropped_bytes.saturating_add(overflow);
        }
        retained.extend(&chunk[..read]);
    }
    Ok(StderrCapture {
        bytes: retained.into(),
        dropped_bytes,
    })
}

fn sanitize_backend_stderr(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .chars()
        .map(|character| sanitize_terminal_character(character, true))
        .collect()
}

fn sanitize_terminal_character(character: char, preserve_layout: bool) -> char {
    if preserve_layout && (character == '\n' || character == '\t') {
        character
    } else if character.is_control() || is_bidi_control(character) {
        '\u{fffd}'
    } else {
        character
    }
}

struct BoundedTerminalText {
    output: String,
    character_count: usize,
    max_bytes: usize,
    max_characters: usize,
    truncated: bool,
}

impl BoundedTerminalText {
    fn new(max_bytes: usize, max_characters: usize) -> Self {
        Self {
            output: String::with_capacity(max_bytes),
            character_count: 0,
            max_bytes,
            max_characters,
            truncated: false,
        }
    }

    fn finish(mut self) -> String {
        if self.truncated {
            let notice_characters = TRUNCATION_NOTICE.chars().count();
            while self.output.len() + TRUNCATION_NOTICE.len() > self.max_bytes
                || self.character_count + notice_characters > self.max_characters
            {
                let removed = self
                    .output
                    .pop()
                    .expect("diagnostic limits leave room for the truncation notice");
                self.character_count -= 1;
                debug_assert!(removed.len_utf8() <= self.max_bytes);
            }
            self.output.push_str(TRUNCATION_NOTICE);
        }
        self.output
    }
}

impl std::fmt::Write for BoundedTerminalText {
    fn write_str(&mut self, value: &str) -> std::fmt::Result {
        if self.truncated {
            return Ok(());
        }
        for character in value.chars() {
            let character = sanitize_terminal_character(character, false);
            if self.output.len() + character.len_utf8() > self.max_bytes
                || self.character_count + 1 > self.max_characters
            {
                self.truncated = true;
                break;
            }
            self.output.push(character);
            self.character_count += 1;
        }
        Ok(())
    }
}

fn is_bidi_control(character: char) -> bool {
    matches!(
        character,
        '\u{061c}'
            | '\u{200e}'
            | '\u{200f}'
            | '\u{202a}'
            | '\u{202b}'
            | '\u{202c}'
            | '\u{202d}'
            | '\u{202e}'
            | '\u{2066}'
            | '\u{2067}'
            | '\u{2068}'
            | '\u{2069}'
    )
}

enum SessionCommand {
    Prompt,
    ResumeCatalog,
    ResumeSession(String),
    New,
    Name(String),
    Clone,
    Tree,
    Unrevert,
    Invalid(&'static str),
}

fn session_command(prompt: &str) -> SessionCommand {
    let trimmed = prompt.trim();
    if trimmed == "/resume" {
        return SessionCommand::ResumeCatalog;
    }
    if trimmed == "/new" {
        return SessionCommand::New;
    }
    if trimmed == "/clone" {
        return SessionCommand::Clone;
    }
    if trimmed == "/tree" {
        return SessionCommand::Tree;
    }
    if trimmed == "/unrevert" {
        return SessionCommand::Unrevert;
    }
    let parts = trimmed.split_whitespace().collect::<Vec<_>>();
    match parts.as_slice() {
        ["/resume", session_id] => SessionCommand::ResumeSession((*session_id).into()),
        ["/resume", ..] => SessionCommand::Invalid("Usage: /resume [session-id]"),
        ["/new", ..] => SessionCommand::Invalid("Usage: /new"),
        ["/clone", ..] => SessionCommand::Invalid("Usage: /clone"),
        ["/tree", ..] => SessionCommand::Invalid("Usage: /tree"),
        ["/unrevert", ..] => SessionCommand::Invalid("Usage: /unrevert"),
        ["/name", "--clear"] => SessionCommand::Name(String::new()),
        ["/name", "--clear", ..] | ["/name"] => {
            SessionCommand::Invalid("Usage: /name <display name> | /name --clear")
        }
        ["/name", ..] => SessionCommand::Name(
            trimmed
                .strip_prefix("/name")
                .expect("matched /name command")
                .trim()
                .into(),
        ),
        _ => SessionCommand::Prompt,
    }
}

enum Input {
    Key(KeyEvent),
    Paste(String),
    Redraw,
    Error(io::Error),
}

fn input_task(sender: mpsc::Sender<Input>, mut stop: watch::Receiver<bool>) -> Result<(), Error> {
    while !*stop.borrow() {
        if !event::poll(Duration::from_millis(50))? {
            match stop.has_changed() {
                Ok(true) => {
                    let _ = stop.borrow_and_update();
                }
                Ok(false) => {}
                Err(_) => return Ok(()),
            }
            continue;
        }
        match event::read() {
            Ok(Event::Key(key))
                if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) =>
            {
                if sender.blocking_send(Input::Key(key)).is_err() {
                    return Ok(());
                }
            }
            Ok(Event::Paste(pasted)) => {
                if sender.blocking_send(Input::Paste(pasted)).is_err() {
                    return Ok(());
                }
            }
            Ok(Event::Resize(_, _)) => {
                if sender.blocking_send(Input::Redraw).is_err() {
                    return Ok(());
                }
            }
            Ok(_) => {}
            Err(error) => {
                let _ = sender.blocking_send(Input::Error(error));
                return Ok(());
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::{ActiveCommand, ActiveCommandType, InteractionStatus};
    use ratatui::backend::TestBackend;
    use serde_json::json;
    use std::fmt::Write as _;
    use tokio::io::duplex;
    use wisp_protocol::events;

    fn handshake() -> serde_json::Value {
        json!({
            "type": "rpc.handshake.accepted",
            "backend_package_version": "0.1.0",
            "protocol_version": 2,
            "event_schema_version": 34,
            "min_protocol_version": 2,
            "max_protocol_version": 2,
            "capabilities": [],
            "limits": {"max_client_frame_bytes": 1024, "max_server_frame_bytes": 2048}
        })
    }

    fn active_prompt_ui(draft: &str) -> LiveUi {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste(draft);
        live_ui
    }

    fn shutdown_event(command_id: &str) -> serde_json::Value {
        json!({
            "type": "rpc.command.finished",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "command_id": command_id,
            "command_type": "shutdown",
            "ok": true,
            "error": null
        })
    }

    fn shutdown_started_event(command_id: &str) -> serde_json::Value {
        json!({
            "type": "rpc.command.started",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "command_id": command_id,
            "command_type": "shutdown"
        })
    }

    fn failed_shutdown_event(command_id: &str) -> serde_json::Value {
        json!({
            "type": "rpc.command.finished",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "command_id": command_id,
            "command_type": "shutdown",
            "ok": false,
            "error": "shutdown refused"
        })
    }

    fn parsed_event(value: serde_json::Value) -> WispCurrentLiveEventOutput {
        events::deserialize(value).unwrap()
    }

    fn projected_event(value: serde_json::Value) -> BackendEvent {
        BackendEvent::from_live(&parsed_event(value)).unwrap()
    }

    fn exit_status(code: u8) -> std::process::ExitStatus {
        std::process::Command::new("/bin/sh")
            .args(["-c", &format!("exit {code}")])
            .status()
            .unwrap()
    }

    #[tokio::test]
    async fn reader_negotiates_limit_and_validates_events() {
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(EVENT_RETAINED_WIRE_BYTES)),
            outcome_tx,
        ));
        let handshake = handshake();
        let event = shutdown_event(SHUTDOWN_COMMAND_ID);
        server
            .write_all(format!("{handshake}\n{event}\n").as_bytes())
            .await
            .unwrap();
        server.shutdown().await.unwrap();
        let response = handshake_rx.await.unwrap().unwrap();
        assert_eq!(response.backend_package_version(), "0.1.0");
        let event = event_rx.recv().await.unwrap();
        assert!(matches!(
            event.event,
            BackendEvent::CommandFinished {
                command_id,
                command_type,
                ok: true,
                ..
            } if command_id == "rust-tui-shutdown" && command_type == "shutdown"
        ));
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Ok(ReaderTermination::Eof)
        ));
        task.await.unwrap();
    }

    #[tokio::test]
    async fn reader_queues_only_bounded_tool_result_projection() {
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(EVENT_RETAINED_WIRE_BYTES)),
            outcome_tx,
        ));
        let event = json!({
            "type": "tool.result",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "call_id": "call-large",
            "name": "bash",
            "output": "x".repeat(200_000),
            "is_error": false,
            "failure_code": null,
            "retryable": false,
            "recovery_hint": null,
            "exit_code": null,
            "output_has_exit_status": false,
            "before_text": null,
            "created": false,
            "summary": null,
            "truncated": false,
            "process_id": "process-1",
            "process_state": "running",
            "process_error": null,
            "stdout": "y".repeat(200_000),
            "stderr": null,
            "stdout_truncated": false,
            "stderr_truncated": false,
            "stdout_dropped_bytes": 0,
            "stderr_dropped_bytes": 0
        })
        .to_string();
        let mut accepted = handshake();
        accepted["limits"]["max_server_frame_bytes"] = json!(1024 * 1024);
        server
            .write_all(format!("{accepted}\n{event}\n").as_bytes())
            .await
            .unwrap();
        server.shutdown().await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        let queued = event_rx.recv().await.unwrap();
        let BackendEvent::ToolResult(result) = queued.event else {
            panic!("bounded tool result expected");
        };
        assert!(result.output.len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert!(result.stdout.unwrap().len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert_eq!(result.output_source_bytes, 200_000);
        assert_eq!(result.stdout_source_bytes, 200_000);
        drop(queued._wire_bytes);
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Ok(ReaderTermination::Eof)
        ));
        task.await.unwrap();
    }

    #[tokio::test]
    async fn back_to_back_shutdown_events_fit_the_bounded_queue() {
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let event_wire_budget = Arc::new(Semaphore::new(EVENT_RETAINED_WIRE_BYTES));
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::clone(&event_wire_budget),
            outcome_tx,
        ));
        let started = shutdown_started_event(SHUTDOWN_COMMAND_ID).to_string();
        let finished = shutdown_event(SHUTDOWN_COMMAND_ID).to_string();
        server
            .write_all(format!("{}\n{started}\n{finished}\n", handshake()).as_bytes())
            .await
            .unwrap();
        server.shutdown().await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Ok(ReaderTermination::Eof)
        ));
        assert_eq!(
            event_wire_budget.available_permits(),
            EVENT_RETAINED_WIRE_BYTES - started.len() - finished.len()
        );
        let started_event = event_rx.recv().await.unwrap();
        let finished_event = event_rx.recv().await.unwrap();
        assert!(matches!(
            &started_event.event,
            BackendEvent::Other { event_type } if event_type == "rpc.command.started"
        ));
        assert!(matches!(
            &finished_event.event,
            BackendEvent::CommandFinished {
                command_id,
                command_type,
                ok: true,
                ..
            } if command_id == SHUTDOWN_COMMAND_ID && command_type == "shutdown"
        ));
        drop((started_event, finished_event));
        assert_eq!(
            event_wire_budget.available_permits(),
            EVENT_RETAINED_WIRE_BYTES
        );
        task.await.unwrap();
    }

    #[tokio::test]
    async fn reader_fails_fast_when_the_event_count_is_exhausted() {
        let (mut server, client) = duplex(32 * 1024);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(EVENT_RETAINED_WIRE_BYTES)),
            outcome_tx,
        ));
        let mut input = format!("{}\n", handshake());
        for index in 0..=EVENT_CHANNEL_CAPACITY {
            input.push_str(&shutdown_event(&format!("shutdown-{index}")).to_string());
            input.push('\n');
        }
        server.write_all(input.as_bytes()).await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        let outcome = timeout(Duration::from_secs(1), outcome_rx)
            .await
            .expect("reader must not wait for a full event queue")
            .unwrap();
        assert!(matches!(outcome, Err(Error::InboundOverloaded)));
        assert_eq!(event_rx.len(), EVENT_CHANNEL_CAPACITY);
        drop(event_rx);
        task.await.unwrap();
    }

    #[tokio::test]
    async fn reader_fails_fast_when_the_event_byte_budget_is_exhausted() {
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let event = shutdown_event(SHUTDOWN_COMMAND_ID).to_string();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(event.len() - 1)),
            outcome_tx,
        ));
        server
            .write_all(format!("{}\n{event}\n", handshake()).as_bytes())
            .await
            .unwrap();
        handshake_rx.await.unwrap().unwrap();
        let outcome = timeout(Duration::from_secs(1), outcome_rx)
            .await
            .expect("reader must not wait for event-byte permits")
            .unwrap();
        assert!(matches!(outcome, Err(Error::InboundOverloaded)));
        assert!(event_rx.try_recv().is_err());
        task.await.unwrap();
    }

    #[tokio::test]
    async fn stderr_drainer_retains_only_the_bounded_tail() {
        let mut input = vec![b'a'; 17];
        input.extend(vec![b'x'; STDERR_RETAINED_BYTES]);
        let capture = stderr_drainer_task(&input[..]).await.unwrap();
        assert_eq!(capture.bytes, vec![b'x'; STDERR_RETAINED_BYTES]);
        assert_eq!(capture.dropped_bytes, 17);
    }

    #[test]
    fn stderr_sanitizer_neutralizes_terminal_controls() {
        let input = "plain\ttext\n\u{1b}[31mred\u{1b}[0m\u{1b}]0;owned\u{7}\rline\u{202e}end";
        let sanitized = sanitize_backend_stderr(input.as_bytes());
        assert!(sanitized.contains("plain\ttext\n"));
        assert!(sanitized.contains("[31mred"));
        assert!(sanitized.contains("]0;owned"));
        assert!(sanitized.chars().all(|character| {
            character == '\n' || character == '\t' || !character.is_control()
        }));
        assert!(!sanitized.contains('\u{1b}'));
        assert!(!sanitized.contains('\u{7}'));
        assert!(!sanitized.contains('\r'));
        assert!(!sanitized.contains('\u{202e}'));
    }

    #[test]
    fn transport_closed_diagnostic_is_bounded_and_terminal_safe() {
        let mut state = UiState::unconfigured();
        state.transcript.complete_message(
            1,
            "partial\u{1b}]0;owned\u{7}\u{202e}\n".repeat(TOP_LEVEL_ERROR_MAX_CHARS),
        );
        let rendered = render_transport_closed_diagnostic(&state);
        assert!(rendered.starts_with("wisp-tui: backend stream ended unexpectedly"));
        assert!(rendered.contains("partial assistant response"));
        assert!(rendered.ends_with(TRUNCATION_NOTICE));
        assert!(rendered.len() <= TOP_LEVEL_ERROR_MAX_BYTES);
        assert!(rendered.chars().count() <= TOP_LEVEL_ERROR_MAX_CHARS);
        assert!(
            rendered
                .chars()
                .all(|character| !character.is_control() && !is_bidi_control(character))
        );
    }

    #[test]
    fn top_level_protocol_errors_are_bounded_and_terminal_safe() {
        fn assert_safe(error: &Error) {
            let rendered = render_top_level_error(error);
            assert!(rendered.len() <= TOP_LEVEL_ERROR_MAX_BYTES);
            assert!(rendered.chars().count() <= TOP_LEVEL_ERROR_MAX_CHARS);
            assert!(rendered.ends_with(TRUNCATION_NOTICE));
            assert!(rendered.contains('\u{fffd}'));
            assert!(
                rendered
                    .chars()
                    .all(|character| !character.is_control() && !is_bidi_control(character))
            );
            std::str::from_utf8(rendered.as_bytes()).unwrap();
        }

        let adversarial = "visible🙂\u{1b}]0;owned\u{7}\u{202e}\u{061c}\r\n\t";
        let rejection_message = adversarial.repeat(HANDSHAKE_FRAME_BYTES / adversarial.len());
        assert!(rejection_message.len() > HANDSHAKE_FRAME_BYTES - adversarial.len());
        assert_safe(&Error::HandshakeRejected {
            code: "rejected\u{1b}[31m".into(),
            message: rejection_message,
        });

        let shutdown_message = adversarial.repeat(MAX_APPLICATION_FRAME_BYTES / adversarial.len());
        assert!(shutdown_message.len() > MAX_APPLICATION_FRAME_BYTES - adversarial.len());
        assert_safe(&Error::ShutdownCommandFailed {
            message: shutdown_message,
        });
    }

    #[tokio::test]
    async fn writer_enforces_each_frame_limit() {
        let (client, mut server) = duplex(64);
        let (tx, rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let task = tokio::spawn(writer_task(client, rx));
        tx.send(WriterMessage::Frame {
            payload: Bytes::from_static(b"12345"),
            limit: 4,
            ack: None,
        })
        .await
        .unwrap();
        drop(tx);
        assert!(matches!(
            task.await.unwrap(),
            Err(Error::FrameTooLarge { limit: 4 })
        ));
        let mut output = Vec::new();
        server.read_to_end(&mut output).await.unwrap();
        assert!(output.is_empty());
    }

    #[tokio::test]
    async fn shutdown_frame_precedes_backend_stdin_eof() {
        let (client, mut server) = duplex(256);
        let (tx, rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let task = tokio::spawn(writer_task(client, rx));

        queue_shutdown_and_close(&tx, 256).await.unwrap();

        let mut output = Vec::new();
        timeout(Duration::from_secs(1), server.read_to_end(&mut output))
            .await
            .expect("backend stdin must reach EOF promptly")
            .unwrap();
        assert_eq!(output.last(), Some(&b'\n'));
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&output[..output.len() - 1]).unwrap(),
            json!({"type": "shutdown", "id": SHUTDOWN_COMMAND_ID})
        );
        task.await.unwrap().unwrap();
    }

    #[test]
    fn transport_channels_apply_backpressure() {
        assert_eq!(WRITER_CHANNEL_CAPACITY, 1);
        assert_eq!(EVENT_CHANNEL_CAPACITY, 64);
        assert_eq!(EVENT_RETAINED_WIRE_BYTES, 64 * 1024 * 1024);
        let (tx, _rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        tx.try_send(1_u8).unwrap();
        assert!(matches!(tx.try_send(2_u8), Err(TrySendError::Full(2))));
    }

    #[test]
    fn graceful_reader_eof_is_not_a_reader_failure() {
        assert!(shutdown_reader_outcome(Ok(ReaderTermination::Eof)).is_ok());
    }

    #[test]
    fn shutdown_requires_exact_success_and_zero_exit() {
        let mut shutdown = ShutdownObservation::default();
        shutdown
            .observe_event(&projected_event(shutdown_event(SHUTDOWN_COMMAND_ID)))
            .unwrap();
        assert!(!shutdown.completed());
        shutdown.observe_exit(exit_status(0)).unwrap();
        assert!(shutdown.completed());
    }

    #[test]
    fn failed_or_missing_shutdown_completion_is_an_error() {
        let mut shutdown = ShutdownObservation::default();
        assert!(matches!(
            shutdown.observe_event(&projected_event(failed_shutdown_event(SHUTDOWN_COMMAND_ID))),
            Err(Error::ShutdownCommandFailed { .. })
        ));

        let mut missing = ShutdownObservation::default();
        missing
            .observe_event(&projected_event(shutdown_event("different-command")))
            .unwrap();
        missing.observe_exit(exit_status(0)).unwrap();
        assert!(matches!(
            missing.deadline_error(),
            Error::ShutdownCompletionMissing
        ));
    }

    #[test]
    fn shutdown_timeout_and_nonzero_exit_are_errors() {
        let mut timeout = ShutdownObservation::default();
        timeout
            .observe_event(&projected_event(shutdown_event(SHUTDOWN_COMMAND_ID)))
            .unwrap();
        assert!(matches!(
            timeout.deadline_error(),
            Error::GracefulShutdownTimeout
        ));

        let mut failed_exit = ShutdownObservation::default();
        assert!(matches!(
            failed_exit.observe_exit(exit_status(7)),
            Err(Error::BackendExitFailure(_))
        ));
    }

    #[test]
    fn cleanup_escalation_is_always_an_error() {
        assert!(cleanup_outcome_result(CleanupOutcome::AlreadyExited).is_ok());
        assert!(matches!(
            cleanup_outcome_result(CleanupOutcome::Terminated),
            Err(Error::CleanupEscalated { stage: "SIGTERM" })
        ));
        assert!(matches!(
            cleanup_outcome_result(CleanupOutcome::Killed),
            Err(Error::CleanupEscalated { stage: "SIGKILL" })
        ));
    }

    #[test]
    fn unexpected_backend_exit_preserves_status() {
        assert!(matches!(
            classify_backend_exit(exit_status(0)),
            Error::BackendExited(status) if status.success()
        ));
        assert!(matches!(
            classify_backend_exit(exit_status(9)),
            Error::BackendExitFailure(status) if status.code() == Some(9)
        ));
    }

    #[test]
    fn session_slash_parser_keeps_unrelated_slashes_as_prompts() {
        assert!(matches!(
            session_command(" /resume "),
            SessionCommand::ResumeCatalog
        ));
        assert!(matches!(
            session_command("/resume session-1"),
            SessionCommand::ResumeSession(session_id) if session_id == "session-1"
        ));
        assert!(matches!(session_command("/new"), SessionCommand::New));
        assert!(matches!(
            session_command("/name release candidate"),
            SessionCommand::Name(name) if name == "release candidate"
        ));
        assert!(matches!(
            session_command("/name --clear"),
            SessionCommand::Name(name) if name.is_empty()
        ));
        assert!(matches!(session_command("/clone"), SessionCommand::Clone));
        assert!(matches!(session_command("/tree"), SessionCommand::Tree));
        assert!(matches!(
            session_command("/unrevert"),
            SessionCommand::Unrevert
        ));
        assert!(matches!(
            session_command("/resume one two"),
            SessionCommand::Invalid("Usage: /resume [session-id]")
        ));
        assert!(matches!(
            session_command("/new extra"),
            SessionCommand::Invalid("Usage: /new")
        ));
        assert!(matches!(
            session_command("/name --clear extra"),
            SessionCommand::Invalid("Usage: /name <display name> | /name --clear")
        ));
        assert!(matches!(
            session_command("/tree extra"),
            SessionCommand::Invalid("Usage: /tree")
        ));
        assert!(matches!(
            session_command("/unrelated"),
            SessionCommand::Prompt
        ));
    }

    #[tokio::test]
    async fn transcript_replacement_effect_resets_live_presentation_state() {
        let (writer_tx, _writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi::default();
        for prompt in ["one", "two", "three"] {
            live_ui.state.transcript.append_prompt(prompt.into());
        }
        live_ui
            .transcript_viewport
            .visible_rows(&live_ui.state.transcript, &mut live_ui.transcript_row_cache);
        live_ui.transcript_viewport.reduce(
            TranscriptViewAction::PageUp,
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
        );
        live_ui.transcript_viewport.reduce(
            TranscriptViewAction::OutputChanged,
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
        );
        live_ui.browse_selected = live_ui
            .state
            .transcript
            .entries()
            .first()
            .map(|entry| entry.id);
        assert!(!live_ui.transcript_viewport.follows_tail());
        assert!(live_ui.transcript_viewport.has_unseen_output());
        assert!(live_ui.browse_selected.is_some());

        live_ui
            .apply_effects(
                vec![UiEffect::ReplaceTranscript],
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert!(live_ui.transcript_viewport.follows_tail());
        assert!(!live_ui.transcript_viewport.has_unseen_output());
        assert!(live_ui.browse_selected.is_none());
        assert!(!live_ui.detail_view.is_open());
    }

    #[tokio::test]
    async fn restored_session_prompts_are_filtered_and_never_truncated_into_the_editor() {
        let (writer_tx, _writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi::default();
        live_ui
            .apply_effects(
                vec![UiEffect::RestoreSessionDraft("safe\u{1b}[31m".into())],
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "safe[31m");
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("ignoring 1"))
        );

        live_ui.editor.clear();
        live_ui.notice = None;
        live_ui
            .apply_effects(
                vec![UiEffect::RestoreSessionDraft(
                    "x".repeat(prompt_editor::MAX_PROMPT_BYTES + 1),
                )],
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(live_ui.editor.text().is_empty());
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("not truncated"))
        );
    }

    #[tokio::test]
    async fn oversized_post_commit_hydration_stays_recoverable() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi::default();
        let selected = reducer::SessionIdentity {
            session_id: "committed-session".into(),
            session_path: "/sessions/committed-session.jsonl".into(),
            session_name: None,
        };
        live_ui.state.selected_session = Some(selected.clone());
        live_ui.state.input_ready = false;
        live_ui.state.session_operation = Some(reducer::SessionOperation::HydratingSelection {
            command_id: "get_messages-1".into(),
            selected: selected.clone(),
            restore_editor_text: None,
            committed_operation: "Session clone",
            report: None,
            completion: None,
        });
        let command =
            WispTypedClientRpcCommands::get_messages("get_messages-1", Some(&selected.session_id))
                .unwrap();

        live_ui
            .apply_effects(
                vec![UiEffect::SendCommittedHydration {
                    command,
                    session_id: selected.session_id.clone(),
                }],
                &writer_tx,
                32,
            )
            .await
            .unwrap();

        assert!(live_ui.state.session_operation.is_none());
        assert!(live_ui.state.input_ready);
        assert_eq!(live_ui.state.selected_session, Some(selected));
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("committed session"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn transport_close_draws_retained_error_state_before_exit() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.transcript.append_exchange("hello".into());
        state
            .transcript
            .complete_message(0, "partial response".into());
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(80, 18)).unwrap();
        let (event_tx, mut event_rx) = mpsc::channel(EVENT_CHANNEL_CAPACITY);
        let permit = Arc::new(Semaphore::new(1)).acquire_owned().await.unwrap();
        event_tx
            .send(QueuedEvent {
                event: BackendEvent::from_live(&parsed_event(json!({
                    "type": "message.delta",
                    "schema_version": 34,
                    "timestamp": "2026-01-02T03:04:05Z",
                    "turn": 1,
                    "role": "assistant",
                    "content_index": 0,
                    "content_kind": "text",
                    "delta": "final queued fragment"
                })))
                .unwrap(),
                _wire_bytes: permit,
            })
            .await
            .unwrap();
        drop(event_tx);

        let drain_control = live_ui
            .drain_backend_events(&mut event_rx, &writer_tx, MAX_APPLICATION_FRAME_BYTES)
            .await
            .unwrap();
        assert_eq!(drain_control, LoopControl::Continue);
        assert_eq!(
            live_ui.state.latest_assistant_text(),
            Some("final queued fragment")
        );

        let control = live_ui
            .close_transport(
                &mut terminal,
                &ConnectionInfo {
                    backend_version: "0.1.0".into(),
                    protocol_version: 2,
                    event_schema_version: 34,
                },
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
                None,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Exit);
        assert!(!live_ui.render_pending);
        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("final queued fragment"));
        assert!(rendered.contains("prompt failed"));
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn live_submit_queues_exact_prompt_and_clears_editor() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi {
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("hello\nworld");

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Continue);
        assert!(live_ui.editor.text().is_empty());
        assert_eq!(
            live_ui.state.transcript.latest_user_text(),
            Some("hello\nworld")
        );
        assert!(live_ui.render_pending);
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("prompt submission must queue one frame");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            json!({"type": "prompt", "id": "prompt-1", "prompt": "hello\nworld"})
        );
    }

    #[tokio::test]
    async fn active_prompt_editor_routes_steer_follow_up_and_newlines() {
        let (writer_client, mut writer_server) = duplex(1024);
        let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let writer = tokio::spawn(writer_task(writer_client, writer_rx));
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };

        live_ui.editor.insert_paste("steer now");
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let mut frame = [0; 256];
        let frame_len = writer_server.read(&mut frame).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&frame[..frame_len - 1]).unwrap(),
            json!({"type": "steer", "id": "steer-1", "content": "steer now"})
        );
        assert!(live_ui.editor.text().is_empty());
        assert_eq!(live_ui.state.queued_steering(), 0);

        live_ui.editor.insert_paste("later please");
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let frame_len = writer_server.read(&mut frame).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&frame[..frame_len - 1]).unwrap(),
            json!({"type": "follow_up", "id": "follow_up-2", "content": "later please"})
        );
        assert!(live_ui.editor.text().is_empty());
        assert_eq!(live_ui.state.queued_follow_ups(), 0);

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "\n\n");

        let mut idle = LiveUi::default();
        idle.handle_input(
            Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)),
            &writer_tx,
            MAX_APPLICATION_FRAME_BYTES,
        )
        .await
        .unwrap();
        assert_eq!(idle.editor.text(), "\n");
        drop(writer_tx);
        writer.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn compacting_active_prompt_rejects_queue_input() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Compacting;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("keep this draft");

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert!(!live_ui.active_prompt_editable());
        assert_eq!(live_ui.editor.text(), "keep this draft");
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn active_alt_up_restores_after_pop_and_keeps_the_newer_draft() {
        let (writer_client, mut writer_server) = duplex(1024);
        let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let writer = tokio::spawn(writer_task(writer_client, writer_rx));
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::QueueUpdated {
                    steering: vec!["restored steering".into()],
                    follow_up: Vec::new(),
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui.editor.insert_paste("newer draft");

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let mut frame = [0; 256];
        let frame_len = writer_server.read(&mut frame).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&frame[..frame_len - 1]).unwrap(),
            json!({"type": "pop_queue", "id": "pop_queue-1", "kind": "steering"})
        );
        assert_eq!(live_ui.editor.text(), "newer draft");
        assert_eq!(live_ui.state.queued_steering(), 1);

        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::QueueItemsRemoved {
                    command_id: "pop_queue-1".into(),
                    operation: reducer::QueueRemovalOperation::Pop,
                    kind: Some(QueueKind::Steering),
                    steering: vec!["restored steering".into()],
                    follow_up: Vec::new(),
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::CommandFinished {
                    command_id: "pop_queue-1".into(),
                    command_type: "pop_queue".into(),
                    ok: true,
                    error: None,
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "restored steering\nnewer draft");
        assert_eq!(live_ui.state.queued_steering(), 0);
        drop(writer_tx);
        writer.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn alt_up_preflights_cached_queue_text_and_retries_deferred_recovery() {
        let (writer_client, mut writer_server) = duplex(1024);
        let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let writer = tokio::spawn(writer_task(writer_client, writer_rx));
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::QueueUpdated {
                    steering: vec!["restored steering".into()],
                    follow_up: Vec::new(),
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui.editor.insert_paste("newer draft");
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let mut frame = [0; 256];
        let frame_len = writer_server.read(&mut frame).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&frame[..frame_len - 1]).unwrap()["type"],
            "pop_queue"
        );

        live_ui
            .editor
            .insert_paste(&"x".repeat(crate::prompt_editor::MAX_PROMPT_BYTES - 11));
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::QueueItemsRemoved {
                    command_id: "pop_queue-1".into(),
                    operation: reducer::QueueRemovalOperation::Pop,
                    kind: Some(QueueKind::Steering),
                    steering: vec!["restored steering".into()],
                    follow_up: Vec::new(),
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::CommandFinished {
                    command_id: "pop_queue-1".into(),
                    command_type: "pop_queue".into(),
                    ok: true,
                    error: None,
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(
            live_ui
                .deferred_queue_recovery
                .first()
                .map(|recovery| recovery.content.as_str()),
            Some("restored steering")
        );

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(!live_ui.deferred_queue_recovery.is_empty());
        assert!(
            timeout(Duration::from_millis(10), writer_server.read(&mut frame))
                .await
                .is_err()
        );
        live_ui.editor.clear();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "restored steering");
        assert!(live_ui.deferred_queue_recovery.is_empty());
        live_ui.editor.insert_paste(
            &"x".repeat(crate::prompt_editor::MAX_PROMPT_BYTES - live_ui.editor.text().len()),
        );
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::QueueUpdated {
                    steering: vec!["cached candidate".into()],
                    follow_up: Vec::new(),
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.state.queued_steering(), 1);
        assert!(
            timeout(Duration::from_millis(10), writer_server.read(&mut frame))
                .await
                .is_err()
        );
        drop(writer_tx);
        writer.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn idle_alt_up_restores_deferred_queue_recovery_without_popping() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi::default();
        live_ui.defer_queue_recovery("recover after idle".into(), Some(0));

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert_eq!(live_ui.editor.text(), "recover after idle");
        assert!(live_ui.deferred_queue_recovery.is_empty());
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test(start_paused = true)]
    async fn queued_submission_times_out_without_changing_state_when_writer_channel_stalls() {
        let (writer_tx, _writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        writer_tx.send(WriterMessage::Close).await.unwrap();
        let mut live_ui = active_prompt_ui("keep this draft");
        let state_before = live_ui.state.clone();

        assert!(matches!(
            live_ui
                .handle_input(
                    Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                    &writer_tx,
                    MAX_APPLICATION_FRAME_BYTES,
                )
                .await,
            Err(Error::QueueSubmissionTimeout)
        ));
        assert_eq!(live_ui.state, state_before);
        assert_eq!(live_ui.editor.text(), "keep this draft");
    }

    #[tokio::test(start_paused = true)]
    async fn queued_submission_times_out_without_changing_state_when_writer_ack_stalls() {
        let (writer_tx, _writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = active_prompt_ui("keep this draft");
        let state_before = live_ui.state.clone();

        assert!(matches!(
            live_ui
                .handle_input(
                    Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                    &writer_tx,
                    MAX_APPLICATION_FRAME_BYTES,
                )
                .await,
            Err(Error::QueueSubmissionTimeout)
        ));
        assert_eq!(live_ui.state, state_before);
        assert_eq!(live_ui.editor.text(), "keep this draft");
    }

    #[tokio::test]
    async fn queue_writer_failure_does_not_commit_state_or_clear_editor() {
        let (writer_client, writer_server) = duplex(64);
        drop(writer_server);
        let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let writer = tokio::spawn(writer_task(writer_client, writer_rx));
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("keep this draft");
        let state_before = live_ui.state.clone();

        assert!(matches!(
            live_ui
                .handle_input(
                    Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                    &writer_tx,
                    MAX_APPLICATION_FRAME_BYTES,
                )
                .await,
            Err(Error::WriterStopped)
        ));
        assert_eq!(live_ui.state, state_before);
        assert_eq!(live_ui.editor.text(), "keep this draft");
        assert!(matches!(writer.await.unwrap(), Err(Error::Io(_))));
    }

    #[tokio::test]
    async fn queue_submission_whitespace_keeps_editor_and_does_not_send() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste(" \n\t");

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), " \n\t");
        assert_eq!(live_ui.state.unobserved_queue_submissions().count(), 0);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("non-empty"))
        );
    }

    #[tokio::test]
    async fn active_queue_preflight_and_writer_failure_keep_editor_and_state_retryable() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("exact frame");
        let encoded_len = serde_json::to_vec(
            &WispTypedClientRpcCommands::steer("steer-1", "exact frame").unwrap(),
        )
        .unwrap()
        .len();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                encoded_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "exact frame");
        assert_eq!(live_ui.state.unobserved_queue_submissions().count(), 0);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));

        drop(writer_rx);
        assert!(matches!(
            live_ui
                .handle_input(
                    Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                    &writer_tx,
                    MAX_APPLICATION_FRAME_BYTES,
                )
                .await,
            Err(Error::WriterStopped)
        ));
        assert_eq!(live_ui.editor.text(), "exact frame");
        assert_eq!(live_ui.state.unobserved_queue_submissions().count(), 0);
    }

    #[tokio::test]
    async fn approval_and_trust_keep_the_queue_draft_exclusive_then_editable() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("saved queue draft");
        live_ui
            .handle_input(
                Input::Paste(" ignored".into()),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "saved queue draft");

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("approval denial must remain exclusive");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap()["type"],
            "approval"
        );
        assert_eq!(live_ui.editor.text(), "saved queue draft");
        assert!(live_ui.active_prompt_editable());
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "saved queue draftx");

        live_ui.state.view_status = ViewStatus::WaitingForTrust;
        live_ui.state.pending_trust_request_id = Some("trust-1".into());
        live_ui
            .handle_input(
                Input::Paste(" ignored again".into()),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "saved queue draftx");
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("trust denial must remain exclusive");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap()["type"],
            "trust"
        );
        assert_eq!(live_ui.editor.text(), "saved queue draftx");
        assert!(live_ui.active_prompt_editable());
    }

    #[test]
    fn unsent_queue_diagnostics_are_bounded_safe_and_deduplicated() {
        let mut state = UiState::unconfigured();
        let mut ids = SequentialCommandIds::default();
        reducer::reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["queued\u{1b}[2J\u{202e}steer".into()],
                follow_up: vec!["restore me".into()],
            }),
            &mut ids,
        )
        .unwrap();
        reducer::reduce(&mut state, UiAction::Steer("in flight".into()), &mut ids).unwrap();
        reducer::reduce(&mut state, UiAction::RestoreNewestQueueDraft, &mut ids).unwrap();
        reducer::reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueItemsRemoved {
                command_id: "pop_queue-2".into(),
                operation: reducer::QueueRemovalOperation::Pop,
                kind: Some(QueueKind::FollowUp),
                steering: Vec::new(),
                follow_up: vec!["restore me".into()],
            }),
            &mut ids,
        )
        .unwrap();

        let deferred = [DeferredQueueRecovery {
            content: "deferred recovery".into(),
            local_order: Some(0),
        }];
        let lines = render_unsent_queue_diagnostics(&state, &deferred);
        let rendered = lines.join("\n");
        assert!(rendered.contains("queued steer item will not run"));
        assert!(rendered.contains("in-flight steer item will not run"));
        assert!(rendered.contains("restoring later item will not run"));
        assert_eq!(rendered.matches("restore me").count(), 1);
        assert_eq!(
            lines
                .iter()
                .filter(|line| line.contains("deferred recovery queue item"))
                .count(),
            1
        );
        assert!(lines.iter().all(|line| {
            line.len() <= UNSENT_QUEUE_REPORT_MAX_BYTES
                && line.chars().count() <= UNSENT_QUEUE_REPORT_MAX_CHARS
                && line
                    .chars()
                    .all(|character| !character.is_control() && !is_bidi_control(character))
        }));
    }

    #[tokio::test]
    async fn failed_queue_recoveries_are_individual_ordered_and_recoverable() {
        let (writer_client, _writer_server) = duplex(2 * prompt_editor::MAX_PROMPT_BYTES);
        let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let writer = tokio::spawn(writer_task(writer_client, writer_rx));
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui
            .editor
            .insert_paste(&"n".repeat(prompt_editor::MAX_PROMPT_BYTES - 512 * 1024));
        let first = format!("first{}", "a".repeat(600 * 1024 - "first".len()));
        let second = format!("second{}", "b".repeat(600 * 1024 - "second".len()));

        live_ui
            .apply_effects(
                vec![
                    UiEffect::RestoreDraft {
                        content: second.clone(),
                        local_order: Some(1),
                    },
                    UiEffect::RestoreDraft {
                        content: first.clone(),
                        local_order: Some(0),
                    },
                ],
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(
            live_ui
                .deferred_queue_recovery
                .iter()
                .map(|recovery| recovery.content.as_str())
                .collect::<Vec<_>>(),
            [first.as_str(), second.as_str()]
        );
        assert_eq!(live_ui.deferred_queue_recovery_bytes(), 1_200 * 1024);

        live_ui.editor.clear();
        assert!(live_ui.retry_deferred_queue_recovery());
        assert_eq!(live_ui.editor.text(), first);
        assert_eq!(live_ui.deferred_queue_recovery.len(), 1);
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(live_ui.editor.text().is_empty());
        assert!(!live_ui.recovered_queue_recovery);
        assert_eq!(live_ui.deferred_queue_recovery.len(), 1);

        live_ui.editor.insert_paste("newer draft");
        assert!(live_ui.retry_deferred_queue_recovery());
        assert_eq!(live_ui.editor.text(), format!("{second}\nnewer draft"));
        drop(writer_tx);
        writer.await.unwrap().unwrap();
    }

    #[test]
    fn deferred_recovery_queue_uses_exact_runtime_bounds() {
        let mut live_ui = LiveUi::default();
        let item_bytes = reducer::QUEUE_CONTENT_BYTES_LIMIT / reducer::QUEUE_MESSAGE_LIMIT;
        for local_order in 0..reducer::QUEUE_MESSAGE_LIMIT {
            let bytes = if local_order + 1 == reducer::QUEUE_MESSAGE_LIMIT {
                reducer::QUEUE_CONTENT_BYTES_LIMIT - live_ui.deferred_queue_recovery_bytes()
            } else {
                item_bytes
            };
            live_ui.defer_queue_recovery("x".repeat(bytes), Some(local_order as u64));
        }
        assert_eq!(
            live_ui.deferred_queue_recovery.len(),
            reducer::QUEUE_MESSAGE_LIMIT
        );
        assert_eq!(
            live_ui.deferred_queue_recovery_bytes(),
            reducer::QUEUE_CONTENT_BYTES_LIMIT
        );
        assert!(!live_ui.deferred_queue_recovery_can_accept("x"));
    }

    #[test]
    fn unsent_queue_diagnostics_preserve_deferred_multiplicity() {
        let deferred = [
            DeferredQueueRecovery {
                content: "same deferred text".into(),
                local_order: Some(0),
            },
            DeferredQueueRecovery {
                content: "same deferred text".into(),
                local_order: Some(1),
            },
        ];
        let lines = render_unsent_queue_diagnostics(&UiState::unconfigured(), &deferred);
        assert_eq!(lines.len(), 1);
        assert!(lines[0].contains("item x2 will not run: same deferred text"));
        assert!(lines[0].len() <= UNSENT_QUEUE_REPORT_MAX_BYTES);
        assert!(lines[0].chars().count() <= UNSENT_QUEUE_REPORT_MAX_CHARS);
    }

    #[tokio::test]
    async fn ctrl_c_cancels_active_prompt_without_exiting() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Continue);
        assert!(live_ui.state.cancel_requested);
        assert!(live_ui.render_pending);
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("Ctrl-C with an active prompt must queue one cancel frame");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            json!({"type": "cancel", "id": "cancel-1", "target_id": "prompt-1"})
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn oversized_ctrl_c_cancel_frame_stays_in_the_loop() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        let cancel = WispTypedClientRpcCommands::cancel("cancel-1", "prompt-1").unwrap();
        let encoded_len = serde_json::to_vec(&cancel).unwrap().len();

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
                &writer_tx,
                encoded_len - 1,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Continue);
        assert!(!live_ui.state.cancel_requested);
        assert!(live_ui.render_pending);
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("prompt cancellation"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn idle_ctrl_c_exits_without_a_cancel_command() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi {
            render_pending: false,
            ..LiveUi::default()
        };

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Exit);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn approval_and_trust_keys_emit_typed_commands() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(30, 8)).unwrap();
        live_ui
            .draw(
                &mut terminal,
                &ConnectionInfo {
                    backend_version: "0.1.0".into(),
                    protocol_version: 2,
                    event_schema_version: 34,
                },
            )
            .unwrap();

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("approval key must queue one frame");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            json!({
                "type": "approval",
                "id": "approval-1",
                "call_id": "call-1",
                "approved": true
            })
        );

        live_ui.state.view_status = ViewStatus::WaitingForTrust;
        live_ui.state.pending_trust_request_id = Some("trust-req-1".into());
        live_ui
            .draw(
                &mut terminal,
                &ConnectionInfo {
                    backend_version: "0.1.0".into(),
                    protocol_version: 2,
                    event_schema_version: 34,
                },
            )
            .unwrap();
        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("trust key must queue one frame");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            json!({
                "type": "trust",
                "id": "trust-2",
                "request_id": "trust-req-1",
                "trusted": true,
                "transient": false
            })
        );
    }

    #[tokio::test]
    async fn positive_decision_keys_require_visible_current_context() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::WaitingForApproval;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: json!({"path": "/sensitive"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(80, 18)).unwrap();
        let connection = ConnectionInfo {
            backend_version: "0.1.0".into(),
            protocol_version: 2,
            event_schema_version: 34,
        };
        live_ui.draw(&mut terminal, &connection).unwrap();
        live_ui.state.pending_approval.as_mut().unwrap().call_id = "call-2".into();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
        live_ui.state.pending_approval.as_mut().unwrap().call_id = "call-1".into();

        live_ui
            .handle_input(Input::Redraw, &writer_tx, MAX_APPLICATION_FRAME_BYTES)
            .await
            .unwrap();
        for key in ['y', 't', 'a'] {
            live_ui
                .handle_input(
                    Input::Key(KeyEvent::new(KeyCode::Char(key), KeyModifiers::NONE)),
                    &writer_tx,
                    MAX_APPLICATION_FRAME_BYTES,
                )
                .await
                .unwrap();
            assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
        }

        let mut terminal = Terminal::new(TestBackend::new(29, 7)).unwrap();
        live_ui.draw(&mut terminal, &connection).unwrap();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("approval denial must remain available in a small terminal");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            json!({
                "type": "approval",
                "id": "approval-1",
                "call_id": "call-1",
                "approved": false,
                "reason": "Denied from TUI"
            })
        );

        live_ui.state.view_status = ViewStatus::WaitingForTrust;
        live_ui.state.pending_approval = None;
        live_ui.state.pending_trust_request_id = Some("trust-req-1".into());
        live_ui.draw(&mut terminal, &connection).unwrap();
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("trust denial must remain available in a small terminal");
        };
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            json!({
                "type": "trust",
                "id": "trust-2",
                "request_id": "trust-req-1",
                "trusted": false,
                "reason": "Denied from TUI",
                "transient": false
            })
        );
    }

    #[tokio::test]
    async fn decision_responses_are_preflighted_before_state_changes() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let call_id = "c".repeat(32);
        let mut approval_state = UiState::unconfigured();
        approval_state.view_status = ViewStatus::WaitingForApproval;
        approval_state.pending_approval = Some(PendingApproval {
            call_id: call_id.clone(),
            name: "read".into(),
            arguments: json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        let mut approval_ui = LiveUi {
            state: approval_state,
            render_pending: false,
            ..LiveUi::default()
        };
        let approval = WispTypedClientRpcCommands::approval(
            "approval-1",
            &call_id,
            false,
            Some("Denied from TUI"),
            None,
        )
        .unwrap();
        let approval_len = serde_json::to_vec(&approval).unwrap().len();

        approval_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
                &writer_tx,
                approval_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(
            approval_ui.state.view_status,
            ViewStatus::WaitingForApproval
        );
        assert_eq!(
            approval_ui
                .state
                .pending_approval
                .as_ref()
                .map(|pending| pending.call_id.as_str()),
            Some(call_id.as_str())
        );
        assert!(
            approval_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("response remains pending"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));

        approval_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
                &writer_tx,
                approval_len,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("approval response must be sent once it fits");
        };
        assert_eq!(payload.as_ref(), serde_json::to_vec(&approval).unwrap());

        let request_id = "t".repeat(32);
        let mut trust_state = UiState::unconfigured();
        trust_state.view_status = ViewStatus::WaitingForTrust;
        trust_state.pending_trust_request_id = Some(request_id.clone());
        let mut trust_ui = LiveUi {
            state: trust_state,
            render_pending: false,
            ..LiveUi::default()
        };
        let trust = WispTypedClientRpcCommands::trust(
            "trust-1",
            &request_id,
            false,
            Some("Trust prompt cancelled"),
            Some(true),
        )
        .unwrap();
        let trust_len = serde_json::to_vec(&trust).unwrap().len();

        trust_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
                &writer_tx,
                trust_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(trust_ui.state.view_status, ViewStatus::WaitingForTrust);
        assert_eq!(
            trust_ui.state.pending_trust_request_id.as_deref(),
            Some(request_id.as_str())
        );
        assert!(
            trust_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("response remains pending"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));

        let replacement_request_id = "u".repeat(32);
        trust_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::TrustRequested {
                    request_id: replacement_request_id.clone(),
                    project_path: "/replacement".into(),
                }),
                &writer_tx,
                trust_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(trust_ui.notice, None);
        assert_eq!(trust_ui.unsendable_response_context, None);

        let control = trust_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
                &writer_tx,
                trust_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        assert!(
            trust_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.starts_with("Esc/Ctrl-C again exits"))
        );
        let control = trust_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
                &writer_tx,
                trust_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Exit);
        assert_eq!(trust_ui.state.view_status, ViewStatus::WaitingForTrust);
        assert_eq!(
            trust_ui.state.pending_trust_request_id.as_deref(),
            Some(replacement_request_id.as_str())
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn automatic_cancellation_denials_are_preflighted() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let call_id = "c".repeat(32);
        let mut approval_state = UiState::unconfigured();
        approval_state.view_status = ViewStatus::Running;
        approval_state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        approval_state.cancel_requested = true;
        let mut approval_ui = LiveUi {
            state: approval_state,
            render_pending: false,
            ..LiveUi::default()
        };
        let approval = WispTypedClientRpcCommands::approval(
            "approval-1",
            &call_id,
            false,
            Some("Denied from TUI: cancelling"),
            None,
        )
        .unwrap();
        let approval_len = serde_json::to_vec(&approval).unwrap().len();
        let approval_event =
            UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
                call_id,
                name: "shell".into(),
                arguments: json!({}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                safety: "command".into(),
            }));

        let control = approval_ui
            .dispatch(approval_event.clone(), &writer_tx, approval_len - 1)
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        assert!(approval_ui.state.cancel_requested);
        assert_eq!(approval_ui.state.view_status, ViewStatus::Running);
        assert_eq!(
            approval_ui.unsendable_response_context,
            Some(UnsendableResponseContext::Cancelling("prompt-1".into()))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));

        approval_ui
            .dispatch(approval_event, &writer_tx, approval_len)
            .await
            .unwrap();
        let WriterMessage::Frame { payload, .. } = writer_rx.recv().await.unwrap() else {
            panic!("automatic approval denial must be sent once it fits");
        };
        assert_eq!(payload.as_ref(), serde_json::to_vec(&approval).unwrap());
        assert_eq!(approval_ui.unsendable_response_context, None);
        assert_eq!(approval_ui.notice, None);

        let request_id = "t".repeat(32);
        let mut trust_state = UiState::unconfigured();
        trust_state.view_status = ViewStatus::Running;
        trust_state.current_command = Some(ActiveCommand {
            id: "prompt-2".into(),
            command_type: ActiveCommandType::Prompt,
        });
        trust_state.cancel_requested = true;
        let mut trust_ui = LiveUi {
            state: trust_state,
            render_pending: false,
            ..LiveUi::default()
        };
        let trust = WispTypedClientRpcCommands::trust(
            "trust-1",
            &request_id,
            false,
            Some("Trust prompt cancelled"),
            Some(true),
        )
        .unwrap();
        let trust_len = serde_json::to_vec(&trust).unwrap().len();

        trust_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::TrustRequested {
                    request_id,
                    project_path: "/workspace".into(),
                }),
                &writer_tx,
                trust_len - 1,
            )
            .await
            .unwrap();
        assert!(trust_ui.state.cancel_requested);
        assert_eq!(trust_ui.state.view_status, ViewStatus::Running);
        assert_eq!(
            trust_ui.unsendable_response_context,
            Some(UnsendableResponseContext::Cancelling("prompt-2".into()))
        );
        assert!(
            trust_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.starts_with("Esc/Ctrl-C again exits"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
        let control = trust_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
                &writer_tx,
                trust_len - 1,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Exit);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn live_submit_rejects_prompt_that_exceeds_negotiated_frame_limit() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi {
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("hello");
        let command = WispTypedClientRpcCommands::prompt("prompt-1", "hello").unwrap();
        let encoded_len = serde_json::to_vec(&command).unwrap().len();

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                encoded_len - 1,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Continue);
        assert_eq!(live_ui.editor.text(), "hello");
        assert!(live_ui.state.current_command.is_none());
        assert_eq!(live_ui.state.transcript.latest_user_text(), None);
        assert!(live_ui.render_pending);
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("negotiated"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn live_submit_rejects_prompt_when_cancel_would_exceed_negotiated_frame_limit() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi {
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("x");
        let prompt = WispTypedClientRpcCommands::prompt("prompt-1", "x").unwrap();
        let prompt_len = serde_json::to_vec(&prompt).unwrap().len();
        let cancel = WispTypedClientRpcCommands::cancel("cancel-2", "prompt-1").unwrap();
        let cancel_len = serde_json::to_vec(&cancel).unwrap().len();
        assert!(prompt_len < cancel_len);

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                prompt_len,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Continue);
        assert_eq!(live_ui.editor.text(), "x");
        assert!(live_ui.state.current_command.is_none());
        assert_eq!(live_ui.state.transcript.latest_user_text(), None);
        assert!(live_ui.render_pending);
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("cancellation"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn selection_preflight_keeps_direct_and_picker_selection_retryable() {
        let session_id = "x".repeat(reducer::SESSION_ID_MAX_BYTES);
        let select =
            WispTypedClientRpcCommands::select_session("select_session-1", &session_id).unwrap();
        let hydration =
            WispTypedClientRpcCommands::get_messages("get_messages-2", Some(&session_id)).unwrap();
        let limit = serde_json::to_vec(&select).unwrap().len();
        assert!(serde_json::to_vec(&hydration).unwrap().len() > limit);

        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut direct = LiveUi {
            render_pending: false,
            ..LiveUi::default()
        };
        direct.state.transcript.append_prompt("old content".into());
        let prompt = format!("/resume {session_id}");
        direct.editor.insert_paste(&prompt);
        direct
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                limit,
            )
            .await
            .unwrap();
        assert_eq!(direct.editor.text(), prompt);
        assert!(direct.state.selected_session.is_none());
        assert!(direct.state.session_operation.is_none());
        assert_eq!(
            direct.state.transcript.latest_user_text(),
            Some("old content")
        );
        assert!(
            direct
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("get_messages"))
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));

        let mut picker = LiveUi {
            render_pending: false,
            session_picker: Some(SessionPicker::new(
                vec![reducer::SessionSummary {
                    session_id,
                    session_path: "/sessions/large.jsonl".into(),
                    name: None,
                    updated_at: "now".into(),
                    entry_count: 0,
                }],
                None,
            )),
            ..LiveUi::default()
        };
        picker
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                limit,
            )
            .await
            .unwrap();
        assert!(picker.session_picker.is_some());
        assert!(picker.state.selected_session.is_none());
        assert!(picker.state.session_operation.is_none());
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn startup_hydration_preflight_is_terminal() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi::default();
        let command = WispTypedClientRpcCommands::get_messages("get_messages-1", None).unwrap();
        let limit = serde_json::to_vec(&command).unwrap().len() - 1;

        assert!(matches!(
            live_ui
                .dispatch_session_action(UiAction::StartupHydration, &writer_tx, limit)
                .await,
            Err(Error::FrameTooLarge { limit: actual }) if actual == limit
        ));
        assert!(live_ui.state.session_operation.is_none());
        assert!(live_ui.state.input_ready);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn tree_picker_ignores_navigation_and_fork_while_a_page_is_loading() {
        let selected = reducer::SessionIdentity {
            session_id: "active".into(),
            session_path: "/sessions/active.jsonl".into(),
            session_name: None,
        };
        let page = reducer::SessionTreePage {
            session: Some(selected.clone()),
            active_leaf_id: Some("entry-1".into()),
            total_node_count: 1,
            nodes: vec![reducer::SessionTreeNode {
                entry_id: "entry-1".into(),
                parent_id: None,
                created_at: "2026-01-02T03:04:05Z".into(),
                kind: reducer::SessionTreeNodeKind::Message,
                role: Some("user".into()),
                preview: "prompt".into(),
                preview_truncated: false,
            }],
            truncated: true,
            next_after_entry_id: Some("entry-1".into()),
        };
        let mut live_ui = LiveUi {
            session_tree_picker: Some(SessionTreePicker::new(page)),
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.state.selected_session = Some(selected);
        live_ui.state.session_operation = Some(reducer::SessionOperation::LoadingTree {
            command_id: "get_session_tree-1".into(),
            after_entry_id: Some("previous-entry".into()),
            page: None,
            completion: None,
        });
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);

        for code in [KeyCode::Enter, KeyCode::Char('f'), KeyCode::PageDown] {
            assert_eq!(
                live_ui
                    .handle_session_tree_picker_key(
                        KeyEvent::new(code, KeyModifiers::NONE),
                        &writer_tx,
                        MAX_APPLICATION_FRAME_BYTES,
                    )
                    .await
                    .unwrap(),
                LoopControl::Continue
            );
        }

        assert!(live_ui.session_tree_picker.is_some());
        assert!(live_ui.state.session_operation.is_some());
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn history_blocked_commands_keep_notice_and_editor_text() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi::default();
        live_ui
            .dispatch_session_action(
                UiAction::StartupHydration,
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { .. } = writer_rx.recv().await.unwrap() else {
            panic!("startup hydration must queue one frame");
        };
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::MessagesReported {
                    command_id: "get_messages-1".into(),
                    messages: reducer::SessionMessages {
                        session: Some(reducer::SessionIdentity {
                            session_id: "active".into(),
                            session_path: "/sessions/active.jsonl".into(),
                            session_name: None,
                        }),
                        active_leaf_id: Some("leaf".into()),
                        truncated: false,
                        next_before_entry_id: None,
                        next_after_entry_id: None,
                        durable_entry_ids: Vec::new(),
                        exact_tool_result: None,
                        transcript: transcript::SharedTranscript::default(),
                    },
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::CommandFinished {
                    command_id: "get_messages-1".into(),
                    command_type: "get_messages".into(),
                    ok: true,
                    error: None,
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { .. } = writer_rx.recv().await.unwrap() else {
            panic!("startup history completion must refresh queue state");
        };
        live_ui
            .dispatch(
                UiAction::ReloadLatestHistory,
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        let WriterMessage::Frame { .. } = writer_rx.recv().await.unwrap() else {
            panic!("latest history reload must queue one frame");
        };

        live_ui.editor.insert_paste("/new");
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert_eq!(live_ui.editor.text(), "/new");
        assert_eq!(
            live_ui.notice.as_deref(),
            Some("Wait for the current history request to finish.")
        );
        assert!(live_ui.render_pending);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));

        live_ui.editor.clear();
        live_ui.editor.insert_paste("keep this prompt");
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert_eq!(live_ui.editor.text(), "keep this prompt");
        assert_eq!(
            live_ui.notice.as_deref(),
            Some("Wait for the current history request to finish.")
        );
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn oversized_automatic_refreshes_are_non_terminal() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        let stats = WispTypedClientRpcCommands::get_session_stats("get_session_stats-1").unwrap();
        let encoded_len = serde_json::to_vec(&stats).unwrap().len();

        let control = live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::CommandFinished {
                    command_id: "prompt-1".into(),
                    command_type: "prompt".into(),
                    ok: true,
                    error: None,
                }),
                &writer_tx,
                encoded_len - 1,
            )
            .await
            .unwrap();

        assert_eq!(control, LoopControl::Continue);
        assert!(live_ui.state.current_command.is_none());
        assert_eq!(live_ui.state.view_status, ViewStatus::Idle);
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.contains("Session metadata refresh"))
        );
        assert!(live_ui.render_pending);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn explicit_detail_modes_preserve_prompt_and_transcript_viewport() {
        let mut live_ui = LiveUi {
            state: UiState::new("fake".into(), None, None),
            ..LiveUi::default()
        };
        let BackendEvent::ToolCall(call) = BackendEvent::from_projection_value(&json!({
            "type": "tool.call",
            "call_id": "edit-detail",
            "name": "edit",
            "arguments": {
                "path": "file.txt",
                "edits": [{"oldText": "old\n", "newText": "new\n"}]
            }
        }))
        .unwrap() else {
            panic!("tool call expected");
        };
        let card_id = live_ui.state.transcript.observe_tool_call(call);
        let BackendEvent::ToolResult(result) = BackendEvent::from_projection_value(&json!({
            "type": "tool.result",
            "call_id": "edit-detail",
            "name": "edit",
            "output": "Applied 1 edit",
            "is_error": false
        }))
        .unwrap() else {
            panic!("tool result expected");
        };
        live_ui.state.transcript.observe_tool_result(*result);
        live_ui.editor.insert_paste("draft");
        live_ui.transcript_viewport.set_geometry(
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
            60,
            10,
        );
        let before = live_ui
            .transcript_viewport
            .visible_rows(&live_ui.state.transcript, &mut live_ui.transcript_row_cache)
            .into_iter()
            .map(|row| row.anchor)
            .collect::<Vec<_>>();
        let (writer_tx, mut writer_rx) = mpsc::channel(4);

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::F(6), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.browse_selected, Some(card_id));
        assert_eq!(live_ui.editor.text(), "draft");

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(live_ui.detail_view.is_open());
        live_ui
            .handle_input(
                Input::Paste("ignored".into()),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "draft");

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(!live_ui.detail_view.is_open());
        assert_eq!(live_ui.browse_selected, Some(card_id));
        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(live_ui.browse_selected.is_none());

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(live_ui.editor.text(), "draftx");
        let after = live_ui
            .transcript_viewport
            .visible_rows(&live_ui.state.transcript, &mut live_ui.transcript_row_cache)
            .into_iter()
            .map(|row| row.anchor)
            .collect::<Vec<_>>();
        assert_eq!(after, before);

        let stale_presentation = retained_detail(&live_ui.state, card_id).unwrap().clone();
        live_ui.state.history.active_exact_detail = Some(reducer::ActiveExactDetail {
            target: card_id,
            presentation: stale_presentation,
        });
        live_ui
            .apply_effects(
                vec![UiEffect::OpenExactDetail(card_id)],
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(!live_ui.detail_view.is_open());
        assert!(live_ui.state.history.active_exact_detail.is_none());

        live_ui.enter_or_cycle_browse();
        live_ui.open_selected_detail();
        assert!(live_ui.detail_view.is_open());
        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
                    call_id: "approval".into(),
                    name: "read".into(),
                    arguments: json!({"path": "README.md"}),
                    detail_source: tool_detail::ToolDetailSource::None,
                    safety: "read".into(),
                })),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(!live_ui.detail_view.is_open());
        assert!(live_ui.browse_selected.is_none());
        assert_eq!(live_ui.state.view_status, ViewStatus::WaitingForApproval);
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[test]
    fn browse_selection_reconciles_after_resize_and_ignores_spacers() {
        let mut live_ui = LiveUi {
            state: UiState::new("fake".into(), None, None),
            ..LiveUi::default()
        };
        let BackendEvent::ToolCall(call) = BackendEvent::from_projection_value(&json!({
            "type": "tool.call",
            "call_id": "resize-detail",
            "name": "edit",
            "arguments": {
                "path": "file.txt",
                "edits": [{"oldText": "old\n", "newText": "new\n"}]
            }
        }))
        .unwrap() else {
            panic!("tool call expected");
        };
        let card_id = live_ui.state.transcript.observe_tool_call(call);
        let BackendEvent::ToolResult(result) = BackendEvent::from_projection_value(&json!({
            "type": "tool.result",
            "call_id": "resize-detail",
            "name": "edit",
            "output": "Applied 1 edit",
            "is_error": false
        }))
        .unwrap() else {
            panic!("tool result expected");
        };
        live_ui.state.transcript.observe_tool_result(*result);
        live_ui.state.transcript.append_exchange("later".into());
        live_ui.transcript_viewport.set_geometry(
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
            58,
            10,
        );
        live_ui.enter_or_cycle_browse();
        assert_eq!(live_ui.browse_selected, Some(card_id));

        live_ui.transcript_viewport.set_geometry(
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
            58,
            1,
        );
        live_ui.transcript_viewport.reduce(
            TranscriptViewAction::ScrollLines(-100),
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
        );
        let mut spacer_visible = false;
        for _ in 0..32 {
            let visible = live_ui
                .transcript_viewport
                .visible_rows(&live_ui.state.transcript, &mut live_ui.transcript_row_cache);
            if visible.len() == 1 && visible[0].kind == TranscriptRowKind::Spacer {
                spacer_visible = true;
                break;
            }
            live_ui.transcript_viewport.reduce(
                TranscriptViewAction::ScrollLines(1),
                &live_ui.state.transcript,
                &mut live_ui.transcript_row_cache,
            );
        }
        assert!(spacer_visible);
        assert!(live_ui.visible_detail_entries().is_empty());
        assert_eq!(live_ui.browse_selected, Some(card_id));

        let connection = ConnectionInfo {
            backend_version: "0.1.0".into(),
            protocol_version: LIVE_RPC_PROTOCOL_VERSION,
            event_schema_version: EVENT_SCHEMA_VERSION,
        };
        let mut terminal = Terminal::new(TestBackend::new(60, 8)).unwrap();
        live_ui.draw(&mut terminal, &connection).unwrap();

        assert!(live_ui.browse_selected.is_none());
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.starts_with("No visible tool card"))
        );
        assert!(live_ui.render_pending);
    }

    #[tokio::test]
    async fn browse_selection_reconciles_after_backend_output() {
        let mut live_ui = LiveUi {
            state: UiState::new("fake".into(), None, None),
            ..LiveUi::default()
        };
        let BackendEvent::ToolCall(call) = BackendEvent::from_projection_value(&json!({
            "type": "tool.call",
            "call_id": "output-detail",
            "name": "edit",
            "arguments": {
                "path": "file.txt",
                "edits": [{"oldText": "old\n", "newText": "new\n"}]
            }
        }))
        .unwrap() else {
            panic!("tool call expected");
        };
        let card_id = live_ui.state.transcript.observe_tool_call(call);
        let BackendEvent::ToolResult(result) = BackendEvent::from_projection_value(&json!({
            "type": "tool.result",
            "call_id": "output-detail",
            "name": "edit",
            "output": "Applied 1 edit",
            "is_error": false
        }))
        .unwrap() else {
            panic!("tool result expected");
        };
        live_ui.state.transcript.observe_tool_result(*result);
        live_ui.transcript_viewport.set_geometry(
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
            58,
            1,
        );
        live_ui.enter_or_cycle_browse();
        assert_eq!(live_ui.browse_selected, Some(card_id));
        assert_eq!(live_ui.visible_detail_entries(), vec![card_id]);

        let (writer_tx, _writer_rx) = mpsc::channel(4);
        live_ui
            .dispatch(
                UiAction::BackendEvent(projected_event(json!({
                    "type": "message.delta",
                    "schema_version": 34,
                    "timestamp": "2026-01-02T03:04:05Z",
                    "turn": 1,
                    "role": "assistant",
                    "content_index": 0,
                    "content_kind": "text",
                    "delta": "new output"
                }))),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();

        assert!(live_ui.browse_selected.is_none());
        assert!(
            live_ui
                .notice
                .as_deref()
                .is_some_and(|notice| notice.starts_with("No visible tool card"))
        );
    }

    #[test]
    fn transcript_navigation_keybindings_do_not_capture_editor_arrows() {
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE)),
            Some(TranscriptViewAction::PageUp)
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)),
            Some(TranscriptViewAction::PageDown)
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::Up, KeyModifiers::CONTROL)),
            Some(TranscriptViewAction::ScrollLines(-1))
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::Down, KeyModifiers::CONTROL)),
            Some(TranscriptViewAction::ScrollLines(1))
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::End, KeyModifiers::CONTROL)),
            Some(TranscriptViewAction::FollowTail)
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::Home, KeyModifiers::CONTROL)),
            Some(TranscriptViewAction::Home)
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE)),
            None
        );
        assert_eq!(
            transcript_view_action(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE)),
            None
        );
    }

    #[tokio::test]
    async fn transcript_navigation_preserves_editor_and_tracks_unseen_output() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.transcript.append_exchange("prompt".into());
        state.transcript.start_message(1);
        let mut transcript_content = String::new();
        for line in 0..50 {
            writeln!(transcript_content, "line-{line}").unwrap();
        }
        state
            .transcript
            .append_message_delta(1, &transcript_content);
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        live_ui.editor.insert_paste("draft");
        live_ui.transcript_viewport.set_geometry(
            &live_ui.state.transcript,
            &mut live_ui.transcript_row_cache,
            40,
            6,
        );
        let _ = live_ui
            .transcript_viewport
            .visible_rows(&live_ui.state.transcript, &mut live_ui.transcript_row_cache);

        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        assert_eq!(live_ui.editor.text(), "draft");
        assert!(!live_ui.transcript_viewport.follows_tail());

        live_ui
            .dispatch(
                UiAction::BackendEvent(BackendEvent::MessageDelta {
                    turn: 1,
                    delta: "new output".into(),
                    content_kind: reducer::MessageContentKind::Text,
                }),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(live_ui.transcript_viewport.has_unseen_output());

        live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::End, KeyModifiers::CONTROL)),
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert!(live_ui.transcript_viewport.follows_tail());
        assert!(!live_ui.transcript_viewport.has_unseen_output());
        assert!(matches!(writer_rx.try_recv(), Err(TryRecvError::Empty)));
    }

    #[tokio::test]
    async fn render_effects_coalesce_into_one_pending_flag() {
        let (writer_tx, _writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut live_ui = LiveUi {
            render_pending: false,
            ..LiveUi::default()
        };
        let control = live_ui
            .apply_effects(
                vec![UiEffect::RequestRender, UiEffect::RequestRender],
                &writer_tx,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        assert!(live_ui.render_pending);
    }

    #[tokio::test]
    async fn frontend_version_mismatch_fails_before_backend_spawn() {
        let error = run(Cli {
            expected_backend_version: "definitely-not-this-build".into(),
            backend: vec![OsString::from("/path/that/must/not/be-spawned")],
        })
        .await
        .unwrap_err();
        assert!(matches!(error, Error::FrontendVersionMismatch { .. }));
    }
}
