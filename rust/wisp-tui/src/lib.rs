//! Minimal native owner for Wisp's negotiated JSONL-RPC backend transport.

#![forbid(unsafe_code)]

mod cli;
mod framing;
mod process;
mod prompt_editor;
pub mod reducer;
mod terminal;
mod ui;

use bytes::Bytes;
use clap::Parser;
use cli::Cli;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
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
use std::collections::VecDeque;
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
use ui::ConnectionInfo;
use wisp_protocol::commands::{ApprovalScope, WispTypedClientRpcCommands};
use wisp_protocol::events::{CommandFinishedOutcome, WispCurrentLiveEventOutput};
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
        .retained_text
        .as_deref()
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

enum WriterMessage {
    Frame { payload: Bytes, limit: usize },
    Close,
}

#[derive(Debug)]
enum ReaderTermination {
    Eof,
}

struct QueuedEvent {
    event: WispCurrentLiveEventOutput,
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
    fn observe_event(&mut self, event: &WispCurrentLiveEventOutput) -> Result<(), Error> {
        match event.command_finished_outcome(SHUTDOWN_COMMAND_ID, "shutdown") {
            Some(CommandFinishedOutcome::Succeeded) => self.command_succeeded = true,
            Some(CommandFinishedOutcome::Failed { error }) => {
                return Err(Error::ShutdownCommandFailed {
                    message: error.unwrap_or_else(|| "backend reported failure".into()),
                });
            }
            None => {}
        }
        Ok(())
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

#[derive(Default)]
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

struct LiveUi {
    state: UiState,
    editor: PromptEditor,
    ids: SequentialCommandIds,
    notice: Option<String>,
    render_pending: bool,
}

impl Default for LiveUi {
    fn default() -> Self {
        Self {
            state: UiState::unconfigured(),
            editor: PromptEditor::default(),
            ids: SequentialCommandIds::default(),
            notice: None,
            render_pending: true,
        }
    }
}

impl LiveUi {
    async fn apply_effects(
        &mut self,
        effects: Vec<UiEffect>,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let mut control = LoopControl::Continue;
        for effect in effects {
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
                        continue;
                    }
                    send_payload(writer, payload, limit).await?;
                }
                UiEffect::RequestRender => self.render_pending = true,
                UiEffect::Exit => control = LoopControl::Exit,
            }
        }
        Ok(control)
    }

    async fn dispatch(
        &mut self,
        action: UiAction,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let effects = reducer::reduce(&mut self.state, action, &mut self.ids)?;
        self.apply_effects(effects, writer, limit).await
    }

    fn draw<B: Backend>(
        &mut self,
        terminal: &mut Terminal<B>,
        connection: &ConnectionInfo,
    ) -> Result<(), Error> {
        terminal.draw(|frame| {
            ui::render(
                frame,
                &self.state,
                &self.editor,
                connection,
                self.notice.as_deref(),
            );
        })?;
        self.render_pending = false;
        Ok(())
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
        let Some(current) = self.state.current_command.as_ref() else {
            return Ok(None);
        };
        if self.state.pending_trust_request_id.is_some()
            || self.state.pending_approval.is_some()
            || self.state.cancel_requested
        {
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
        if self.prompt_editable() || self.state.view_status == ViewStatus::Error {
            if quit_if_idle {
                return Ok(LoopControl::Exit);
            }
            return Ok(LoopControl::Continue);
        }
        if let Some(notice) = self.cancel_frame_limit_notice(limit)? {
            self.notice = Some(notice);
            self.render_pending = true;
            return Ok(LoopControl::Continue);
        }
        self.dispatch(UiAction::Cancel, writer, limit).await
    }

    fn prompt_editable(&self) -> bool {
        self.state.input_ready
            && self.state.current_command.is_none()
            && self.state.view_status == ViewStatus::Idle
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

    async fn handle_input(
        &mut self,
        input: Input,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        match input {
            Input::Key(key) if is_ctrl_c(key) => self.interrupt(writer, limit, true).await,
            Input::Key(key) if is_escape(key) => self.interrupt(writer, limit, false).await,
            Input::Key(key) if self.state.view_status == ViewStatus::WaitingForApproval => {
                match approval_decision(key, self.state.pending_approval.as_ref()) {
                    Some(action) => {
                        self.notice = None;
                        self.dispatch(action, writer, limit).await
                    }
                    None if key_can_edit(key) => {
                        self.notice =
                            Some("Approve with y once, t tool, a all, or deny with n/Esc.".into());
                        self.render_pending = true;
                        Ok(LoopControl::Continue)
                    }
                    None => Ok(LoopControl::Continue),
                }
            }
            Input::Key(key) if self.state.view_status == ViewStatus::WaitingForTrust => {
                match trust_decision(key, self.state.pending_trust_request_id.as_deref()) {
                    Some(action) => {
                        self.notice = None;
                        self.dispatch(action, writer, limit).await
                    }
                    None if key_can_edit(key) => {
                        self.notice = Some("Trust with y, or deny with n/Esc.".into());
                        self.render_pending = true;
                        Ok(LoopControl::Continue)
                    }
                    None => Ok(LoopControl::Continue),
                }
            }
            Input::Key(key) if self.prompt_editable() => match self.editor.handle_key(key) {
                EditorAction::Submit => {
                    let prompt = self.editor.text().to_owned();
                    if prompt.trim().is_empty() {
                        self.notice = Some("Enter a non-empty prompt before sending.".into());
                        self.render_pending = true;
                        return Ok(LoopControl::Continue);
                    }
                    if let Some(notice) = self.prompt_frame_limit_notice(&prompt, limit)? {
                        self.notice = Some(notice);
                        self.render_pending = true;
                        return Ok(LoopControl::Continue);
                    }
                    let effects =
                        reducer::reduce(&mut self.state, UiAction::Submit(prompt), &mut self.ids)?;
                    let control = self.apply_effects(effects, writer, limit).await?;
                    self.editor.clear();
                    self.notice = None;
                    Ok(control)
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
            Input::Paste(pasted) if self.prompt_editable() => {
                let outcome = self.editor.insert_paste(&pasted);
                let notice_changed = self.update_edit_notice(outcome);
                if outcome.changed || notice_changed {
                    self.render_pending = true;
                }
                Ok(LoopControl::Continue)
            }
            Input::Key(key) if key_can_edit(key) => {
                self.notice = Some("Prompt input is unavailable while Wisp is busy.".into());
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            Input::Paste(_) => {
                self.notice = Some("Prompt input is unavailable while Wisp is busy.".into());
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            Input::Redraw => {
                self.render_pending = true;
                Ok(LoopControl::Continue)
            }
            Input::Error(error) => Err(Error::Io(error)),
            Input::Key(_) => Ok(LoopControl::Continue),
        }
    }
}

fn is_ctrl_c(key: KeyEvent) -> bool {
    key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL)
}

fn is_escape(key: KeyEvent) -> bool {
    key.code == KeyCode::Esc
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
        let mut redraw = interval(FRAME_INTERVAL);
        redraw.set_missed_tick_behavior(MissedTickBehavior::Skip);
        let loop_result = loop {
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
                            let event = BackendEvent::from_live(&event.event)?;
                            if live_ui.dispatch(
                                UiAction::BackendEvent(event),
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
                            break Ok(());
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
        };
        let _ = input_stop_tx.send(true);
        drop(input_rx);
        input.await??;
        drop(terminal);
        if let Some(diagnostic) = transport_closed_diagnostic {
            eprintln!("{diagnostic}");
        }
        loop_result?;

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

    let _ = writer_tx.send(WriterMessage::Close).await;
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

#[cfg(test)]
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
        .send(WriterMessage::Frame { payload, limit })
        .await
        .map_err(|_| Error::WriterStopped)
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
            WriterMessage::Frame { payload, limit } => {
                if payload.len() > limit {
                    return Err(Error::FrameTooLarge { limit });
                }
                writer.write_all(&payload).await?;
                writer.write_all(b"\n").await?;
                writer.flush().await?;
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
    use crate::reducer::{ActiveCommand, ActiveCommandType};
    use ratatui::backend::TestBackend;
    use serde_json::json;
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
        assert!(
            event
                .event
                .successful_command_finished("rust-tui-shutdown", "shutdown")
        );
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
        assert_eq!(started_event.event.event_type(), "rpc.command.started");
        assert!(
            finished_event
                .event
                .successful_command_finished(SHUTDOWN_COMMAND_ID, "shutdown")
        );
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
        state.retained_text =
            Some("partial\u{1b}]0;owned\u{7}\u{202e}\n".repeat(TOP_LEVEL_ERROR_MAX_CHARS));
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
            .observe_event(&parsed_event(shutdown_event(SHUTDOWN_COMMAND_ID)))
            .unwrap();
        assert!(!shutdown.completed());
        shutdown.observe_exit(exit_status(0)).unwrap();
        assert!(shutdown.completed());
    }

    #[test]
    fn failed_or_missing_shutdown_completion_is_an_error() {
        let mut shutdown = ShutdownObservation::default();
        assert!(matches!(
            shutdown.observe_event(&parsed_event(failed_shutdown_event(SHUTDOWN_COMMAND_ID))),
            Err(Error::ShutdownCommandFailed { .. })
        ));

        let mut missing = ShutdownObservation::default();
        missing
            .observe_event(&parsed_event(shutdown_event("different-command")))
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
            .observe_event(&parsed_event(shutdown_event(SHUTDOWN_COMMAND_ID)))
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

    #[tokio::test]
    async fn transport_close_draws_retained_error_state_before_exit() {
        let (writer_tx, mut writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.last_submitted_prompt = Some("hello".into());
        state.retained_text = Some("partial response".into());
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };
        let mut terminal = Terminal::new(TestBackend::new(80, 18)).unwrap();

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
        assert!(rendered.contains("partial response"));
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
            live_ui.state.last_submitted_prompt.as_deref(),
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
            safety: "read".into(),
        });
        let mut live_ui = LiveUi {
            state,
            render_pending: false,
            ..LiveUi::default()
        };

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
        let control = live_ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
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
                "trusted": false,
                "reason": "Denied from TUI",
                "transient": false
            })
        );
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
        assert_eq!(live_ui.state.last_submitted_prompt, None);
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
        assert_eq!(live_ui.state.last_submitted_prompt, None);
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
    async fn oversized_automatic_stats_refresh_is_non_terminal() {
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
                .is_some_and(|notice| notice.contains("stats refresh"))
        );
        assert!(live_ui.render_pending);
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
