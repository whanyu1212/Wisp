//! Deterministic, terminal-independent state transitions for the native TUI.

use serde_json::Value;
use thiserror::Error;
use wisp_protocol::ProtocolDecodeError;

use crate::tool_cards::{BoundedText, ToolCallInput, ToolResultInput, bounded_identity};
pub use crate::tool_detail::ToolDetailSource;
use crate::transcript::SharedTranscript;
use wisp_protocol::commands::{ApprovalScope, WispTypedClientRpcCommands};

mod event_projection;

pub use event_projection::EventProjectionError;

const DEFAULT_DENIAL_REASON: &str = "Denied from TUI";
const CANCELLED_APPROVAL_REASON: &str = "Denied from TUI: cancelled";
const CANCELLING_APPROVAL_REASON: &str = "Denied from TUI: cancelling";
const CANCELLED_TRUST_REASON: &str = "Trust prompt cancelled";
const RPC_CANCELLED_PREFIX: &str = "RPC command cancelled:";
pub const SESSION_CATALOG_LIMIT: usize = 50;
pub const SESSION_ID_MAX_BYTES: usize = 4 * 1024;
const SESSION_PATH_MAX_BYTES: usize = 4 * 1024;
const SESSION_LABEL_MAX_BYTES: usize = 512;
const SESSION_UPDATED_AT_MAX_BYTES: usize = 128;
const SESSION_ENTRY_COUNT_MAX: u32 = 1_000_000_000;
const SESSION_NOTICE_MAX_BYTES: usize = 1024;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionIdentity {
    pub session_id: String,
    pub session_path: String,
    pub session_name: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionSummary {
    pub session_id: String,
    pub session_path: String,
    pub name: Option<String>,
    pub updated_at: String,
    pub entry_count: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SessionMessages {
    pub session: Option<SessionIdentity>,
    pub active_leaf_id: Option<String>,
    pub truncated: bool,
    pub next_before_entry_id: Option<String>,
    pub next_after_entry_id: Option<String>,
    pub durable_entry_ids: Vec<String>,
    pub exact_tool_result: Option<Box<ToolResultInput>>,
    pub transcript: SharedTranscript,
}

pub const TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT: usize = 1_200;

#[derive(Clone, Debug, PartialEq)]
pub struct ActiveExactDetail {
    pub target: crate::transcript::TranscriptEntryId,
    pub presentation: crate::tool_detail::ToolDetailPresentation,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct HistoryWindow {
    pub session: Option<SessionIdentity>,
    pub active_leaf_id: Option<String>,
    pub oldest_cursor: Option<String>,
    pub newest_cursor: Option<String>,
    pub represented_durable_entry_ids: std::collections::BTreeSet<String>,
    pub represented_durable_entry_order: Vec<String>,
    pub tail_evicted: bool,
    pub active_exact_detail: Option<ActiveExactDetail>,
}

#[derive(Clone, Debug, PartialEq)]
struct HistoryRequest {
    command_id: String,
    kind: HistoryRequestKind,
    active_leaf_may_advance: bool,
    report: Option<SessionMessages>,
    completion: Option<SessionCompletion>,
}

#[derive(Clone, Debug, PartialEq)]
enum HistoryRequestKind {
    Older {
        cursor: String,
    },
    Newer {
        cursor: String,
    },
    Latest,
    ExactDetail {
        target: crate::transcript::TranscriptEntryId,
        entry_id: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionCompletion {
    pub ok: bool,
    pub error: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum SessionOperation {
    StartupHydration {
        command_id: String,
        report: Option<SessionMessages>,
        completion: Option<SessionCompletion>,
    },
    LoadingCatalog {
        command_id: String,
        sessions: Option<Vec<SessionSummary>>,
        selected_session_id: Option<Option<String>>,
        completion: Option<SessionCompletion>,
    },
    SelectingSession {
        command_id: String,
        requested_session_id: String,
        selected: Option<SessionIdentity>,
        completion: Option<SessionCompletion>,
    },
    HydratingSelection {
        command_id: String,
        selected: SessionIdentity,
        report: Option<SessionMessages>,
        completion: Option<SessionCompletion>,
    },
    CreatingSession {
        command_id: String,
    },
}

impl SessionOperation {
    pub fn label(&self) -> &'static str {
        match self {
            Self::StartupHydration { .. } => "Loading latest session history…",
            Self::LoadingCatalog { .. } => "Loading sessions…",
            Self::SelectingSession { .. } => "Selecting session…",
            Self::HydratingSelection { .. } => "Loading session history…",
            Self::CreatingSession { .. } => "Creating new session…",
        }
    }
}

pub fn valid_session_id(session_id: &str) -> bool {
    !session_id.is_empty() && session_id.len() <= SESSION_ID_MAX_BYTES
}

fn bounded_session_text(value: &str, max_bytes: usize) -> String {
    BoundedText::head(value, max_bytes, 8).text
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum AgentMode {
    #[default]
    Build,
    Plan,
}

impl AgentMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Build => "build",
            Self::Plan => "plan",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum ViewStatus {
    #[default]
    Idle,
    Running,
    WaitingForApproval,
    WaitingForTrust,
    Error,
}

impl ViewStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Running => "running",
            Self::WaitingForApproval => "waiting_for_approval",
            Self::WaitingForTrust => "waiting_for_trust",
            Self::Error => "error",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum InteractionStatus {
    #[default]
    Idle,
    Running,
    Compacting,
    WaitingForApproval,
    WaitingForTrust,
    Exiting,
}

impl InteractionStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Running => "running",
            Self::Compacting => "compacting",
            Self::WaitingForApproval => "waiting_for_approval",
            Self::WaitingForTrust => "waiting_for_trust",
            Self::Exiting => "exiting",
        }
    }

    pub fn input_mode(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Running | Self::Compacting => "running",
            Self::WaitingForApproval => "approval",
            Self::WaitingForTrust => "trust",
            Self::Exiting => "exiting",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActiveCommandType {
    Prompt,
    Init,
    Compact,
}

impl ActiveCommandType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Prompt => "prompt",
            Self::Init => "init",
            Self::Compact => "compact",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ActiveCommand {
    pub id: String,
    pub command_type: ActiveCommandType,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PendingApproval {
    pub call_id: String,
    pub name: String,
    pub arguments: Value,
    pub detail_source: ToolDetailSource,
    pub safety: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct UiState {
    pub view_status: ViewStatus,
    pub interaction_status: InteractionStatus,
    pub input_ready: bool,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub mode: AgentMode,
    pub last_session: Option<String>,
    pub selected_session: Option<SessionIdentity>,
    pub session_operation: Option<SessionOperation>,
    pub queued_steering: usize,
    pub queued_follow_ups: usize,
    pub current_command: Option<ActiveCommand>,
    pub pending_approval: Option<PendingApproval>,
    pub pending_trust_request_id: Option<String>,
    pub pending_trust_project_path: Option<String>,
    pub cancel_requested: bool,
    pub exit_requested: bool,
    pub transcript: SharedTranscript,
    pub history: HistoryWindow,
    history_request: Option<HistoryRequest>,
}

impl UiState {
    pub fn new(provider: String, model: Option<String>, effort: Option<String>) -> Self {
        Self::with_provider(Some(provider), model, effort)
    }

    pub fn unconfigured() -> Self {
        Self::with_provider(None, None, None)
    }

    fn with_provider(
        provider: Option<String>,
        model: Option<String>,
        effort: Option<String>,
    ) -> Self {
        Self {
            view_status: ViewStatus::Idle,
            interaction_status: InteractionStatus::Idle,
            input_ready: true,
            provider,
            model,
            effort,
            mode: AgentMode::Build,
            last_session: None,
            selected_session: None,
            session_operation: None,
            queued_steering: 0,
            queued_follow_ups: 0,
            current_command: None,
            pending_approval: None,
            pending_trust_request_id: None,
            pending_trust_project_path: None,
            cancel_requested: false,
            exit_requested: false,
            transcript: SharedTranscript::default(),
            history: HistoryWindow::default(),
            history_request: None,
        }
    }

    pub fn input_mode(&self) -> &'static str {
        self.interaction_status.input_mode()
    }

    pub fn is_streaming_text(&self) -> bool {
        self.transcript.is_streaming_text()
    }

    pub fn latest_assistant_text(&self) -> Option<&str> {
        self.transcript.latest_assistant_text()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommandKind {
    Prompt,
    Approval,
    Cancel,
    Trust,
    GetSessionStats,
    GetSessions,
    NewSession,
    SelectSession,
    GetMessages,
}

impl CommandKind {
    pub fn prefix(self) -> &'static str {
        match self {
            Self::Prompt => "prompt",
            Self::Approval => "approval",
            Self::Cancel => "cancel",
            Self::Trust => "trust",
            Self::GetSessionStats => "get_session_stats",
            Self::GetSessions => "get_sessions",
            Self::NewSession => "new_session",
            Self::SelectSession => "select_session",
            Self::GetMessages => "get_messages",
        }
    }
}

pub trait CommandIdSource {
    fn next_id(&mut self, kind: CommandKind) -> String;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MessageContentKind {
    Text,
    Thinking,
    Other(String),
}

#[derive(Clone, Debug, PartialEq)]
pub enum BackendEvent {
    MessageStarted {
        turn: u64,
    },
    MessageDelta {
        turn: u64,
        delta: String,
        content_kind: MessageContentKind,
    },
    MessageCompleted {
        turn: u64,
        content: String,
    },
    ToolCall(ToolCallInput),
    ToolApprovalRequested(PendingApproval),
    ToolApprovalResolved {
        call_id: String,
        name: String,
        approved: bool,
        reason: Option<String>,
    },
    ToolResult(Box<ToolResultInput>),
    TrustRequested {
        request_id: String,
        project_path: String,
    },
    ProjectConfigApplied {
        provider: String,
        model: Option<String>,
        effort: Option<String>,
    },
    SessionsReported {
        command_id: String,
        sessions: Vec<SessionSummary>,
        selected_session: Option<SessionIdentity>,
    },
    SessionSelected {
        command_id: String,
        session: SessionIdentity,
    },
    MessagesReported {
        command_id: String,
        messages: SessionMessages,
    },
    MessagesProjectionFailed {
        command_id: String,
        error: String,
    },
    CommandFinished {
        command_id: String,
        command_type: String,
        ok: bool,
        error: Option<String>,
    },
    Other {
        event_type: String,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum UiAction {
    Submit(String),
    StartupHydration,
    LoadSessionCatalog,
    SelectSession {
        session_id: String,
    },
    NewSession,
    LoadOlderHistory,
    LoadNewerHistory,
    ReloadLatestHistory,
    LoadExactDetail {
        target: crate::transcript::TranscriptEntryId,
    },
    ReleaseExactDetail,
    ApprovalDecision {
        call_id: String,
        approved: bool,
        reason: Option<String>,
        scope: Option<ApprovalScope>,
    },
    TrustDecision {
        request_id: String,
        trusted: bool,
        reason: Option<String>,
        transient: Option<bool>,
    },
    Cancel,
    BackendEvent(BackendEvent),
    TransportClosed {
        error: Option<String>,
    },
}

#[derive(Debug)]
pub enum UiEffect {
    SendCommand(WispTypedClientRpcCommands),
    ShowSessionPicker {
        sessions: Vec<SessionSummary>,
        selected_session_id: Option<String>,
    },
    ReplaceTranscript,
    HistoryWindowChanged,
    OpenExactDetail(crate::transcript::TranscriptEntryId),
    Notice(String),
    RequestRender,
    Exit,
}

#[derive(Debug, Error)]
pub enum ReduceError {
    #[error("cannot submit a prompt while command {0:?} is active")]
    PromptAlreadyActive(String),
    #[error("no pending approval matches call {0:?}")]
    NoMatchingApproval(String),
    #[error("no pending trust request matches {0:?}")]
    NoMatchingTrust(String),
    #[error("invalid generated RPC command: {0}")]
    Protocol(#[from] ProtocolDecodeError),
    #[error("session operation is active")]
    SessionOperationActive,
}

pub fn reduce(
    state: &mut UiState,
    action: UiAction,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    match action {
        UiAction::Submit(content) => submit(state, content, ids),
        UiAction::StartupHydration => start_startup_hydration(state, ids),
        UiAction::LoadSessionCatalog => load_session_catalog(state, ids),
        UiAction::SelectSession { session_id } => select_session(state, session_id, ids),
        UiAction::NewSession => new_session(state, ids),
        UiAction::LoadOlderHistory => load_older_history(state, ids),
        UiAction::LoadNewerHistory => load_newer_history(state, ids),
        UiAction::ReloadLatestHistory => reload_latest_history(state, ids),
        UiAction::LoadExactDetail { target } => load_exact_detail(state, target, ids),
        UiAction::ReleaseExactDetail => {
            state.history.active_exact_detail = None;
            Ok(vec![UiEffect::RequestRender])
        }
        UiAction::ApprovalDecision {
            call_id,
            approved,
            reason,
            scope,
        } => answer_approval(state, call_id, approved, reason, scope, ids),
        UiAction::TrustDecision {
            request_id,
            trusted,
            reason,
            transient,
        } => answer_trust(state, request_id, trusted, reason, transient, ids),
        UiAction::Cancel => cancel(state, ids),
        UiAction::BackendEvent(event) => Ok(handle_backend_event(state, event, ids)?),
        UiAction::TransportClosed { .. } => {
            state.view_status = ViewStatus::Error;
            state.transcript.finish_active_response();
            state
                .transcript
                .settle_unresolved_tools("event stream closed");
            Ok(vec![UiEffect::RequestRender, UiEffect::Exit])
        }
    }
}

fn submit(
    state: &mut UiState,
    content: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if content.trim().is_empty() {
        return Ok(Vec::new());
    }
    if state.session_operation.is_some() {
        return Err(ReduceError::SessionOperationActive);
    }
    if let Some(current) = &state.current_command {
        return Err(ReduceError::PromptAlreadyActive(current.id.clone()));
    }
    let id = ids.next_id(CommandKind::Prompt);
    let command = WispTypedClientRpcCommands::prompt(&id, &content)?;
    state.view_status = ViewStatus::Running;
    state.interaction_status = InteractionStatus::Running;
    state.current_command = Some(ActiveCommand {
        id,
        command_type: ActiveCommandType::Prompt,
    });
    state.pending_approval = None;
    state.cancel_requested = false;
    state.transcript.append_prompt(content);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn begin_session_operation(state: &UiState) -> Result<(), ReduceError> {
    if state.session_operation.is_some() || state.history_request.is_some() {
        return Err(ReduceError::SessionOperationActive);
    }
    if let Some(current) = &state.current_command {
        return Err(ReduceError::PromptAlreadyActive(current.id.clone()));
    }
    Ok(())
}

fn start_startup_hydration(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    begin_session_operation(state)?;
    let id = ids.next_id(CommandKind::GetMessages);
    let command = WispTypedClientRpcCommands::get_messages(&id, None)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::StartupHydration {
        command_id: id,
        report: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn load_session_catalog(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    begin_session_operation(state)?;
    let id = ids.next_id(CommandKind::GetSessions);
    let command = WispTypedClientRpcCommands::get_sessions(&id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::LoadingCatalog {
        command_id: id,
        sessions: None,
        selected_session_id: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn select_session(
    state: &mut UiState,
    session_id: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !valid_session_id(&session_id) {
        return Ok(vec![
            UiEffect::Notice("Session ID is empty or exceeds the 4096-byte limit.".into()),
            UiEffect::RequestRender,
        ]);
    }
    begin_session_operation(state)?;
    let id = ids.next_id(CommandKind::SelectSession);
    let command = WispTypedClientRpcCommands::select_session(&id, &session_id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::SelectingSession {
        command_id: id,
        requested_session_id: session_id,
        selected: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn new_session(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    begin_session_operation(state)?;
    let id = ids.next_id(CommandKind::NewSession);
    let command = WispTypedClientRpcCommands::new_session(&id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::CreatingSession { command_id: id });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn history_session_id(state: &UiState) -> Option<&str> {
    state
        .history
        .session
        .as_ref()
        .or(state.selected_session.as_ref())
        .map(|session| session.session_id.as_str())
}

fn can_request_history(state: &UiState, allow_during_prompt: bool) -> bool {
    state.history_request.is_none()
        && state.session_operation.is_none()
        && match state.current_command.as_ref() {
            None => true,
            Some(ActiveCommand {
                command_type: ActiveCommandType::Prompt,
                ..
            }) => allow_during_prompt,
            Some(_) => false,
        }
}

fn begin_history_request(
    state: &mut UiState,
    command_id: String,
    kind: HistoryRequestKind,
    command: WispTypedClientRpcCommands,
) -> Vec<UiEffect> {
    let active_leaf_may_advance = matches!(
        state.current_command,
        Some(ActiveCommand {
            command_type: ActiveCommandType::Prompt,
            ..
        })
    ) || matches!(kind, HistoryRequestKind::Latest)
        && state.transcript.has_live_entries();
    state.history_request = Some(HistoryRequest {
        command_id,
        kind,
        active_leaf_may_advance,
        report: None,
        completion: None,
    });
    vec![UiEffect::SendCommand(command), UiEffect::RequestRender]
}

fn load_older_history(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !can_request_history(state, true) {
        return Ok(Vec::new());
    }
    let Some(cursor) = state.history.oldest_cursor.clone() else {
        return Ok(Vec::new());
    };
    let id = ids.next_id(CommandKind::GetMessages);
    let command =
        WispTypedClientRpcCommands::get_messages_older(&id, history_session_id(state), &cursor)?;
    Ok(begin_history_request(
        state,
        id,
        HistoryRequestKind::Older { cursor },
        command,
    ))
}

fn load_newer_history(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !state.history.tail_evicted {
        return Ok(Vec::new());
    }
    if !can_request_history(state, false) {
        return Ok(Vec::new());
    }
    if state.transcript.has_live_entries() {
        return reload_latest_history(state, ids);
    }
    let Some(cursor) = state.history.newest_cursor.clone() else {
        return reload_latest_history(state, ids);
    };
    let id = ids.next_id(CommandKind::GetMessages);
    let command =
        WispTypedClientRpcCommands::get_messages_newer(&id, history_session_id(state), &cursor)?;
    Ok(begin_history_request(
        state,
        id,
        HistoryRequestKind::Newer { cursor },
        command,
    ))
}

fn reload_latest_history(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !can_request_history(state, false) {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetMessages);
    let command = WispTypedClientRpcCommands::get_messages(&id, history_session_id(state))?;
    Ok(begin_history_request(
        state,
        id,
        HistoryRequestKind::Latest,
        command,
    ))
}

fn load_exact_detail(
    state: &mut UiState,
    target: crate::transcript::TranscriptEntryId,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !can_request_history(state, true) {
        return Ok(Vec::new());
    }
    let Some(target) = state.transcript.exact_historical_detail_target(target) else {
        return Ok(Vec::new());
    };
    let Some(entry_id) = state.transcript.durable_entry_id(target).map(str::to_owned) else {
        return Ok(Vec::new());
    };
    let id = ids.next_id(CommandKind::GetMessages);
    let command =
        WispTypedClientRpcCommands::get_message_detail(&id, history_session_id(state), &entry_id)?;
    Ok(begin_history_request(
        state,
        id,
        HistoryRequestKind::ExactDetail { target, entry_id },
        command,
    ))
}

fn answer_approval(
    state: &mut UiState,
    call_id: String,
    approved: bool,
    reason: Option<String>,
    scope: Option<ApprovalScope>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state
        .pending_approval
        .as_ref()
        .map(|pending| &pending.call_id)
        != Some(&call_id)
    {
        return Err(ReduceError::NoMatchingApproval(call_id));
    }
    let id = ids.next_id(CommandKind::Approval);
    let selected_reason = if approved {
        None
    } else {
        Some(reason.as_deref().unwrap_or(DEFAULT_DENIAL_REASON))
    };
    let selected_scope = if approved {
        match scope {
            None | Some(ApprovalScope::Once) => None,
            other => other,
        }
    } else {
        None
    };
    let command = WispTypedClientRpcCommands::approval(
        &id,
        &call_id,
        approved,
        selected_reason,
        selected_scope,
    )?;
    state.pending_approval = None;
    restore_active_or_idle(state);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn answer_trust(
    state: &mut UiState,
    request_id: String,
    trusted: bool,
    reason: Option<String>,
    transient: Option<bool>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.pending_trust_request_id.as_ref() != Some(&request_id) {
        return Err(ReduceError::NoMatchingTrust(request_id));
    }
    let id = ids.next_id(CommandKind::Trust);
    let selected_reason = if trusted {
        None
    } else {
        Some(reason.as_deref().unwrap_or(DEFAULT_DENIAL_REASON))
    };
    let selected_transient = Some(if trusted {
        false
    } else {
        transient.unwrap_or(false)
    });
    let command = WispTypedClientRpcCommands::trust(
        &id,
        &request_id,
        trusted,
        selected_reason,
        selected_transient,
    )?;
    state.pending_trust_request_id = None;
    state.pending_trust_project_path = None;
    restore_active_or_idle(state);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn cancel(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Some(request_id) = state.pending_trust_request_id.clone() {
        return answer_trust(
            state,
            request_id,
            false,
            Some(CANCELLED_TRUST_REASON.into()),
            Some(true),
            ids,
        );
    }
    if let Some(call_id) = state
        .pending_approval
        .as_ref()
        .map(|pending| pending.call_id.clone())
    {
        return answer_approval(
            state,
            call_id,
            false,
            Some(CANCELLED_APPROVAL_REASON.into()),
            None,
            ids,
        );
    }
    let Some(current) = state.current_command.as_ref() else {
        return Ok(Vec::new());
    };
    if state.cancel_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::Cancel);
    let command = WispTypedClientRpcCommands::cancel(&id, &current.id)?;
    state.cancel_requested = true;
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn restore_active_or_idle(state: &mut UiState) {
    if state.current_command.is_some() {
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
    } else {
        state.view_status = ViewStatus::Idle;
        state.interaction_status = InteractionStatus::Idle;
    }
}

fn handle_session_backend_event(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    let Some(mut operation) = state.session_operation.take() else {
        return Ok(None);
    };

    let projection_failure = match (&operation, event) {
        (
            SessionOperation::StartupHydration { command_id, .. },
            BackendEvent::MessagesProjectionFailed {
                command_id: received,
                error,
            },
        ) if received == command_id => Some(("startup history", error.clone())),
        (
            SessionOperation::HydratingSelection { command_id, .. },
            BackendEvent::MessagesProjectionFailed {
                command_id: received,
                error,
            },
        ) if received == command_id => Some(("session history", error.clone())),
        _ => None,
    };
    if let Some((operation, error)) = projection_failure {
        return Ok(Some(session_failure(state, operation, Some(error))));
    }

    let handled = match &mut operation {
        SessionOperation::StartupHydration {
            command_id,
            report,
            completion,
        } => match event {
            BackendEvent::MessagesReported {
                command_id: received,
                messages,
            } if received == command_id => {
                if report.is_none() {
                    *report = Some(messages.clone());
                }
                true
            }
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ok,
                error,
            } if received == command_id && command_type == "get_messages" => {
                if completion.is_none() {
                    *completion = Some(SessionCompletion {
                        ok: *ok,
                        error: error
                            .as_deref()
                            .map(|error| bounded_session_text(error, SESSION_NOTICE_MAX_BYTES)),
                    });
                }
                true
            }
            _ => false,
        },
        SessionOperation::LoadingCatalog {
            command_id,
            sessions,
            selected_session_id,
            completion,
        } => match event {
            BackendEvent::SessionsReported {
                command_id: received,
                sessions: received_sessions,
                selected_session,
            } if received == command_id => {
                if sessions.is_none() {
                    *sessions = Some(received_sessions.clone());
                    *selected_session_id = Some(
                        selected_session
                            .as_ref()
                            .map(|session| session.session_id.clone()),
                    );
                }
                true
            }
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ok,
                error,
            } if received == command_id && command_type == "get_sessions" => {
                if completion.is_none() {
                    *completion = Some(SessionCompletion {
                        ok: *ok,
                        error: error
                            .as_deref()
                            .map(|error| bounded_session_text(error, SESSION_NOTICE_MAX_BYTES)),
                    });
                }
                true
            }
            _ => false,
        },
        SessionOperation::SelectingSession {
            command_id,
            requested_session_id,
            selected,
            completion,
        } => match event {
            BackendEvent::SessionSelected {
                command_id: received,
                session,
            } if received == command_id => {
                if selected.is_none() && session.session_id == *requested_session_id {
                    *selected = Some(session.clone());
                }
                true
            }
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ok,
                error,
            } if received == command_id && command_type == "select_session" => {
                if completion.is_none() {
                    *completion = Some(SessionCompletion {
                        ok: *ok,
                        error: error
                            .as_deref()
                            .map(|error| bounded_session_text(error, SESSION_NOTICE_MAX_BYTES)),
                    });
                }
                true
            }
            _ => false,
        },
        SessionOperation::HydratingSelection {
            command_id,
            selected,
            report,
            completion,
        } => match event {
            BackendEvent::MessagesReported {
                command_id: received,
                messages,
            } if received == command_id => {
                if report.is_none() {
                    if !messages
                        .session
                        .as_ref()
                        .is_some_and(|session| same_session(session, selected))
                    {
                        return Ok(Some(session_failure(
                            state,
                            "session history hydration",
                            Some("backend returned another session".into()),
                        )));
                    }
                    *report = Some(messages.clone());
                }
                true
            }
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ok,
                error,
            } if received == command_id && command_type == "get_messages" => {
                if completion.is_none() {
                    *completion = Some(SessionCompletion {
                        ok: *ok,
                        error: error
                            .as_deref()
                            .map(|error| bounded_session_text(error, SESSION_NOTICE_MAX_BYTES)),
                    });
                }
                true
            }
            _ => false,
        },
        SessionOperation::CreatingSession { command_id } => matches!(
            event,
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ..
            } if received == command_id && command_type == "new_session"
        ),
    };
    if !handled {
        state.session_operation = Some(operation);
        return Ok(None);
    }

    let effects = match operation {
        SessionOperation::StartupHydration {
            report: _,
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "startup history", completion.error),
        SessionOperation::StartupHydration {
            report: Some(report),
            completion: Some(_),
            ..
        } => {
            state.selected_session = report.session.clone();
            state.last_session = state
                .selected_session
                .as_ref()
                .map(|session| session.session_id.clone());
            install_history_snapshot(state, report);
            state.input_ready = true;
            vec![UiEffect::ReplaceTranscript, UiEffect::RequestRender]
        }
        SessionOperation::StartupHydration { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::LoadingCatalog {
            sessions: _,
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session catalog", completion.error),
        SessionOperation::LoadingCatalog {
            sessions: Some(sessions),
            selected_session_id,
            completion: Some(_),
            ..
        } => {
            state.input_ready = true;
            vec![
                UiEffect::ShowSessionPicker {
                    sessions,
                    selected_session_id: selected_session_id.flatten().or_else(|| {
                        state
                            .selected_session
                            .as_ref()
                            .map(|session| session.session_id.clone())
                    }),
                },
                UiEffect::RequestRender,
            ]
        }
        SessionOperation::LoadingCatalog { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::SelectingSession {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session selection", completion.error),
        SessionOperation::SelectingSession {
            selected: Some(selected),
            completion: Some(_),
            ..
        } => {
            let id = ids.next_id(CommandKind::GetMessages);
            let command =
                WispTypedClientRpcCommands::get_messages(&id, Some(&selected.session_id))?;
            state.selected_session = Some(selected.clone());
            state.last_session = Some(selected.session_id.clone());
            state.transcript = SharedTranscript::default();
            state.history = HistoryWindow::default();
            state.history_request = None;
            state.session_operation = Some(SessionOperation::HydratingSelection {
                command_id: id,
                selected,
                report: None,
                completion: None,
            });
            vec![
                UiEffect::ReplaceTranscript,
                UiEffect::SendCommand(command),
                UiEffect::RequestRender,
            ]
        }
        SessionOperation::SelectingSession { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::HydratingSelection {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session history", completion.error),
        SessionOperation::HydratingSelection {
            report: Some(report),
            completion: Some(_),
            ..
        } => {
            install_history_snapshot(state, report);
            state.input_ready = true;
            vec![UiEffect::ReplaceTranscript, UiEffect::RequestRender]
        }
        SessionOperation::HydratingSelection { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::CreatingSession { command_id } => match event {
            BackendEvent::CommandFinished { ok: true, .. } => {
                state.transcript = SharedTranscript::default();
                state.selected_session = None;
                state.last_session = None;
                state.history = HistoryWindow::default();
                state.history_request = None;
                state.input_ready = true;
                vec![UiEffect::ReplaceTranscript, UiEffect::RequestRender]
            }
            BackendEvent::CommandFinished { error, .. } => session_failure(
                state,
                "new session",
                error
                    .as_deref()
                    .map(|error| bounded_session_text(error, SESSION_NOTICE_MAX_BYTES)),
            ),
            _ => {
                state.session_operation = Some(SessionOperation::CreatingSession { command_id });
                return Ok(Some(Vec::new()));
            }
        },
    };
    Ok(Some(effects))
}

fn same_session(left: &SessionIdentity, right: &SessionIdentity) -> bool {
    left.session_id == right.session_id && left.session_path == right.session_path
}

fn session_failure(state: &mut UiState, operation: &str, error: Option<String>) -> Vec<UiEffect> {
    state.input_ready = true;
    let detail = error.unwrap_or_else(|| "backend reported failure".into());
    vec![
        UiEffect::Notice(bounded_session_text(
            &format!("{operation} failed: {detail}"),
            SESSION_NOTICE_MAX_BYTES,
        )),
        UiEffect::RequestRender,
    ]
}

fn install_history_snapshot(state: &mut UiState, report: SessionMessages) {
    state.transcript = report.transcript;
    let prefix_evicted = !state
        .transcript
        .retain_historical_entries(TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT, false)
        .unwrap_or_default()
        .is_empty();
    let represented_durable_entry_ids = state.transcript.historical_durable_entry_ids();
    let represented_durable_entry_order = report
        .durable_entry_ids
        .into_iter()
        .filter(|entry_id| represented_durable_entry_ids.contains(entry_id))
        .collect::<Vec<_>>();
    let oldest_cursor = prefix_evicted
        .then(|| represented_durable_entry_order.first().cloned())
        .flatten()
        .or(report.next_before_entry_id);
    state.history = HistoryWindow {
        session: report.session,
        active_leaf_id: report.active_leaf_id,
        oldest_cursor,
        newest_cursor: report.next_after_entry_id,
        represented_durable_entry_ids,
        represented_durable_entry_order,
        tail_evicted: false,
        active_exact_detail: None,
    };
    state
        .transcript
        .replace_history_omission_marker(state.history.oldest_cursor.is_some());
}

fn same_optional_session(left: &Option<SessionIdentity>, right: &Option<SessionIdentity>) -> bool {
    match (left, right) {
        (Some(left), Some(right)) => same_session(left, right),
        (None, None) => true,
        _ => false,
    }
}

fn same_history_scope(
    history: &HistoryWindow,
    report: &SessionMessages,
    active_leaf_may_advance: bool,
) -> bool {
    same_optional_session(&history.session, &report.session)
        && (active_leaf_may_advance || history.active_leaf_id == report.active_leaf_id)
}

fn sync_represented_history(state: &mut UiState) {
    let retained = state.transcript.historical_durable_entry_ids();
    state
        .history
        .represented_durable_entry_order
        .retain(|entry_id| retained.contains(entry_id));
    state.history.represented_durable_entry_ids = retained;
}

fn clear_evicted_exact_detail(state: &mut UiState) {
    if state
        .history
        .active_exact_detail
        .as_ref()
        .is_some_and(|detail| state.transcript.entry(detail.target).is_none())
    {
        state.history.active_exact_detail = None;
    }
}

fn history_request_failure(error: String) -> Vec<UiEffect> {
    vec![
        UiEffect::Notice(bounded_session_text(
            &format!("Session history request failed: {error}"),
            SESSION_NOTICE_MAX_BYTES,
        )),
        UiEffect::RequestRender,
    ]
}

fn handle_history_backend_event(
    state: &mut UiState,
    event: &BackendEvent,
) -> Option<Vec<UiEffect>> {
    let mut request = state.history_request.take()?;
    let handled = match event {
        BackendEvent::MessagesReported {
            command_id,
            messages,
        } if command_id == &request.command_id => {
            if request.report.is_some() {
                return Some(history_request_failure("duplicate history report".into()));
            }
            request.report = Some(messages.clone());
            true
        }
        BackendEvent::MessagesProjectionFailed { command_id, error }
            if command_id == &request.command_id =>
        {
            return Some(history_request_failure(error.clone()));
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_id == &request.command_id && command_type == "get_messages" => {
            if !ok {
                return Some(history_request_failure(
                    error
                        .as_deref()
                        .map(|error| bounded_session_text(error, SESSION_NOTICE_MAX_BYTES))
                        .unwrap_or_else(|| "backend reported failure".into()),
                ));
            }
            if request.completion.is_none() {
                request.completion = Some(SessionCompletion {
                    ok: true,
                    error: None,
                });
            }
            true
        }
        _ => false,
    };
    if !handled {
        state.history_request = Some(request);
        return None;
    }
    if request.completion.is_none() || request.report.is_none() {
        state.history_request = Some(request);
        return Some(Vec::new());
    }
    let report = request.report.take().expect("checked above");
    match request.kind {
        HistoryRequestKind::Older { cursor } | HistoryRequestKind::Newer { cursor }
            if !same_history_scope(&state.history, &report, request.active_leaf_may_advance)
                || report.durable_entry_ids.is_empty()
                || report.durable_entry_ids.iter().any(|entry_id| {
                    entry_id == &cursor
                        || state
                            .history
                            .represented_durable_entry_ids
                            .contains(entry_id)
                }) =>
        {
            Some(history_request_failure(
                "stale, duplicate, or malformed history page".into(),
            ))
        }
        HistoryRequestKind::Older { .. } => {
            if !state.transcript.prepend_history_page(&report.transcript) {
                return Some(history_request_failure(
                    "history page cannot be merged safely".into(),
                ));
            }
            state.history.active_leaf_id = report.active_leaf_id;
            state
                .history
                .represented_durable_entry_order
                .splice(0..0, report.durable_entry_ids);
            state.history.oldest_cursor = report.next_before_entry_id;
            let tail_evicted = !state
                .transcript
                .retain_historical_entries(TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT, true)
                .unwrap_or_default()
                .is_empty();
            sync_represented_history(state);
            if tail_evicted {
                state.history.tail_evicted = true;
                state.history.newest_cursor = state
                    .history
                    .represented_durable_entry_order
                    .last()
                    .cloned();
            }
            state
                .transcript
                .replace_history_omission_marker(state.history.oldest_cursor.is_some());
            clear_evicted_exact_detail(state);
            Some(vec![
                UiEffect::HistoryWindowChanged,
                UiEffect::RequestRender,
            ])
        }
        HistoryRequestKind::Newer { .. } => {
            if !state.transcript.append_history_page(&report.transcript) {
                return Some(history_request_failure(
                    "history page cannot be merged safely".into(),
                ));
            }
            state.history.active_leaf_id = report.active_leaf_id;
            state
                .history
                .represented_durable_entry_order
                .extend(report.durable_entry_ids);
            state.history.newest_cursor = report.next_after_entry_id;
            state.history.tail_evicted = state.history.newest_cursor.is_some();
            let prefix_evicted = !state
                .transcript
                .retain_historical_entries(TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT, false)
                .unwrap_or_default()
                .is_empty();
            sync_represented_history(state);
            if prefix_evicted {
                state.history.oldest_cursor = state
                    .history
                    .represented_durable_entry_order
                    .first()
                    .cloned();
            }
            state
                .transcript
                .replace_history_omission_marker(state.history.oldest_cursor.is_some());
            clear_evicted_exact_detail(state);
            Some(vec![
                UiEffect::HistoryWindowChanged,
                UiEffect::RequestRender,
            ])
        }
        HistoryRequestKind::Latest => {
            if !same_history_scope(&state.history, &report, request.active_leaf_may_advance) {
                return Some(history_request_failure(
                    "latest history belongs to another session or branch".into(),
                ));
            }
            install_history_snapshot(state, report);
            Some(vec![UiEffect::ReplaceTranscript, UiEffect::RequestRender])
        }
        HistoryRequestKind::ExactDetail { target, entry_id } => {
            if !same_history_scope(&state.history, &report, request.active_leaf_may_advance)
                || report.durable_entry_ids.len() != 1
                || report.durable_entry_ids.first() != Some(&entry_id)
                || state.transcript.durable_entry_id(target) != Some(entry_id.as_str())
                || state
                    .transcript
                    .exact_historical_detail_target(target)
                    .is_none()
            {
                return Some(history_request_failure(
                    "exact detail did not match the selected row".into(),
                ));
            }
            let Some(result) = report.exact_tool_result.as_ref() else {
                return Some(history_request_failure(
                    "selected persisted row has no tool detail".into(),
                ));
            };
            let Some(presentation) = state.transcript.exact_historical_detail(target, result)
            else {
                return Some(history_request_failure(
                    "selected tool detail is unavailable".into(),
                ));
            };
            state.history.active_leaf_id = report.active_leaf_id;
            state.history.active_exact_detail = Some(ActiveExactDetail {
                target,
                presentation,
            });
            Some(vec![
                UiEffect::OpenExactDetail(target),
                UiEffect::RequestRender,
            ])
        }
    }
}

fn handle_backend_event(
    state: &mut UiState,
    event: BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if let Some(effects) = handle_history_backend_event(state, &event) {
        return Ok(effects);
    }
    if let Some(effects) = handle_session_backend_event(state, &event, ids)? {
        return Ok(effects);
    }
    match event {
        BackendEvent::MessageStarted { turn } => {
            state.transcript.begin_message(turn);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::MessageDelta {
            turn,
            delta,
            content_kind: MessageContentKind::Text,
        } => {
            state.transcript.append_message_delta(turn, &delta);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::MessageDelta { .. } | BackendEvent::Other { .. } => Ok(Vec::new()),
        BackendEvent::MessageCompleted { turn, content } => {
            state.transcript.complete_message(turn, content);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::ToolCall(input) => {
            state.transcript.observe_tool_call(input);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::ToolApprovalRequested(pending) => {
            state.transcript.observe_approval_requested(ToolCallInput {
                call_id: bounded_identity(&pending.call_id),
                name: pending.name.clone(),
                arguments: pending.arguments.clone(),
                detail_source: pending.detail_source.clone(),
            });
            if state.cancel_requested {
                let id = ids.next_id(CommandKind::Approval);
                let command = WispTypedClientRpcCommands::approval(
                    &id,
                    &pending.call_id,
                    false,
                    Some(CANCELLING_APPROVAL_REASON),
                    None,
                )?;
                state.pending_approval = None;
                return Ok(vec![
                    UiEffect::SendCommand(command),
                    UiEffect::RequestRender,
                ]);
            }
            state.pending_approval = Some(pending);
            state.view_status = ViewStatus::WaitingForApproval;
            state.interaction_status = InteractionStatus::WaitingForApproval;
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::ToolApprovalResolved {
            call_id,
            name: _,
            approved,
            reason,
        } => {
            state
                .transcript
                .observe_approval_resolved(&call_id, approved, reason.as_deref());
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::ToolResult(result) => {
            state.transcript.observe_tool_result(*result);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::TrustRequested {
            request_id,
            project_path,
        } => {
            if state.cancel_requested {
                let id = ids.next_id(CommandKind::Trust);
                let command = WispTypedClientRpcCommands::trust(
                    &id,
                    &request_id,
                    false,
                    Some(CANCELLED_TRUST_REASON),
                    Some(true),
                )?;
                state.pending_trust_request_id = None;
                state.pending_trust_project_path = None;
                return Ok(vec![
                    UiEffect::SendCommand(command),
                    UiEffect::RequestRender,
                ]);
            }
            state.pending_trust_request_id = Some(request_id);
            state.pending_trust_project_path = Some(project_path);
            state.view_status = ViewStatus::WaitingForTrust;
            state.interaction_status = InteractionStatus::WaitingForTrust;
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::ProjectConfigApplied {
            provider,
            model,
            effort,
        } => {
            state.provider = Some(provider);
            state.model = model;
            state.effort = effort;
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::SessionsReported { .. }
        | BackendEvent::SessionSelected { .. }
        | BackendEvent::MessagesReported { .. }
        | BackendEvent::MessagesProjectionFailed { .. } => Ok(Vec::new()),
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } => {
            let matches_current = state.current_command.as_ref().is_some_and(|current| {
                current.id == command_id && current.command_type.as_str() == command_type
            });
            if !matches_current {
                return Ok(Vec::new());
            }
            let stats_id = ids.next_id(CommandKind::GetSessionStats);
            let stats = WispTypedClientRpcCommands::get_session_stats(&stats_id)?;
            state.current_command = None;
            state.pending_approval = None;
            state.pending_trust_request_id = None;
            state.pending_trust_project_path = None;
            state.cancel_requested = false;
            state.transcript.finish_active_response();
            state.transcript.settle_unresolved_tools(if ok {
                "tool result missing"
            } else if error
                .as_deref()
                .is_some_and(|message| message.starts_with(RPC_CANCELLED_PREFIX))
            {
                "prompt cancelled"
            } else {
                "command failed"
            });
            state.interaction_status = InteractionStatus::Idle;
            let was_cancelled = !ok
                && error
                    .as_deref()
                    .is_some_and(|message| message.starts_with(RPC_CANCELLED_PREFIX));
            state.view_status = if ok || was_cancelled {
                ViewStatus::Idle
            } else {
                ViewStatus::Error
            };
            Ok(vec![UiEffect::SendCommand(stats), UiEffect::RequestRender])
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    #[derive(Default)]
    struct DeterministicIds(BTreeMap<&'static str, usize>);

    impl CommandIdSource for DeterministicIds {
        fn next_id(&mut self, kind: CommandKind) -> String {
            let count = self.0.entry(kind.prefix()).or_default();
            *count += 1;
            format!("{}-{count}", kind.prefix())
        }
    }

    fn command_value(effect: &UiEffect) -> Option<Value> {
        match effect {
            UiEffect::SendCommand(command) => Some(command.to_value().unwrap()),
            UiEffect::ShowSessionPicker { .. }
            | UiEffect::ReplaceTranscript
            | UiEffect::HistoryWindowChanged
            | UiEffect::OpenExactDetail(_)
            | UiEffect::Notice(_)
            | UiEffect::RequestRender
            | UiEffect::Exit => None,
        }
    }

    #[test]
    fn submission_stream_and_completion_are_deterministic() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.complete_message(0, "stale answer".into());
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, UiAction::Submit("hello".into()), &mut ids).unwrap();
        assert_eq!(command_value(&effects[0]).unwrap()["id"], "prompt-1");
        assert_eq!(state.interaction_status, InteractionStatus::Running);
        assert_eq!(state.transcript.latest_user_text(), Some("hello"));
        assert_eq!(state.latest_assistant_text(), Some("stale answer"));

        for delta in ["hel", "lo"] {
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::MessageDelta {
                    turn: 1,
                    delta: delta.into(),
                    content_kind: MessageContentKind::Text,
                }),
                &mut ids,
            )
            .unwrap();
        }
        assert_eq!(state.latest_assistant_text(), Some("hello"));

        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessageCompleted {
                turn: 1,
                content: "authoritative".into(),
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.latest_assistant_text(), Some("authoritative"));
    }

    #[test]
    fn thinking_is_ignored_and_a_new_text_turn_appends_a_response() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.complete_message(1, "older answer".into());
        let mut ids = DeterministicIds::default();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessageDelta {
                turn: 2,
                delta: "private thought".into(),
                content_kind: MessageContentKind::Thinking,
            }),
            &mut ids,
        )
        .unwrap();
        assert!(effects.is_empty());
        assert_eq!(state.latest_assistant_text(), Some("older answer"));

        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessageDelta {
                turn: 2,
                delta: "new answer".into(),
                content_kind: MessageContentKind::Text,
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.latest_assistant_text(), Some("new answer"));
        assert_eq!(state.transcript.entries().len(), 2);
    }

    #[test]
    fn failed_matching_completion_clears_the_command_and_sets_error_view() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut ids = DeterministicIds::default();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: "prompt-1".into(),
                command_type: "prompt".into(),
                ok: false,
                error: Some("provider failed".into()),
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.view_status, ViewStatus::Error);
        assert_eq!(state.interaction_status, InteractionStatus::Idle);
        assert!(state.current_command.is_none());
        assert_eq!(
            command_value(&effects[0]).unwrap()["type"],
            "get_session_stats"
        );
    }

    #[test]
    fn cancelled_matching_completion_returns_to_idle() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.cancel_requested = true;
        let mut ids = DeterministicIds::default();

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: "prompt-1".into(),
                command_type: "prompt".into(),
                ok: false,
                error: Some("RPC command cancelled: requested by user".into()),
            }),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.view_status, ViewStatus::Idle);
        assert_eq!(state.interaction_status, InteractionStatus::Idle);
        assert!(!state.cancel_requested);
        assert!(state.current_command.is_none());
        assert_eq!(
            command_value(&effects[0]).unwrap()["type"],
            "get_session_stats"
        );
    }

    #[test]
    fn approval_once_is_omitted_and_denial_has_the_tui_reason() {
        for approved in [true, false] {
            let mut state = UiState::new("fake".into(), None, None);
            state.current_command = Some(ActiveCommand {
                id: "prompt-1".into(),
                command_type: ActiveCommandType::Prompt,
            });
            state.pending_approval = Some(PendingApproval {
                call_id: "call-1".into(),
                name: "read".into(),
                arguments: serde_json::json!({}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                safety: "read".into(),
            });
            let mut ids = DeterministicIds::default();
            let effects = reduce(
                &mut state,
                UiAction::ApprovalDecision {
                    call_id: "call-1".into(),
                    approved,
                    reason: None,
                    scope: Some(ApprovalScope::Once),
                },
                &mut ids,
            )
            .unwrap();
            let command = command_value(&effects[0]).unwrap();
            assert!(command.get("scope").is_none());
            if approved {
                assert!(command.get("reason").is_none());
            } else {
                assert_eq!(command["reason"], DEFAULT_DENIAL_REASON);
            }
        }
    }

    #[test]
    fn stale_approval_and_late_completion_do_not_mutate_state() {
        let mut state = UiState::new("fake".into(), None, None);
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: serde_json::json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let before = state.clone();
        let mut ids = DeterministicIds::default();
        assert!(
            reduce(
                &mut state,
                UiAction::ApprovalDecision {
                    call_id: "wrong".into(),
                    approved: true,
                    reason: None,
                    scope: None,
                },
                &mut ids,
            )
            .is_err()
        );
        assert_eq!(state, before);

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: "late".into(),
                command_type: "prompt".into(),
                ok: true,
                error: None,
            }),
            &mut ids,
        )
        .unwrap();
        assert!(effects.is_empty());
        assert_eq!(state, before);
    }

    #[test]
    fn submission_during_an_active_command_is_rejected_without_mutation() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::WaitingForApproval;
        state.interaction_status = InteractionStatus::WaitingForApproval;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: serde_json::json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        let before = state.clone();
        let mut ids = DeterministicIds::default();

        assert!(matches!(
            reduce(
                &mut state,
                UiAction::Submit("second prompt".into()),
                &mut ids,
            ),
            Err(ReduceError::PromptAlreadyActive(id)) if id == "prompt-1"
        ));
        assert_eq!(state, before);
        assert!(
            ids.0.is_empty(),
            "a rejected submission must not consume an ID"
        );
    }

    #[test]
    fn transport_close_retains_partial_and_active_interaction() {
        let mut state = UiState::new("fake".into(), None, None);
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.transcript.append_message_delta(1, "partial");
        let mut ids = DeterministicIds::default();
        let effects = reduce(
            &mut state,
            UiAction::TransportClosed { error: None },
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.view_status, ViewStatus::Error);
        assert_eq!(state.interaction_status, InteractionStatus::Running);
        assert_eq!(state.latest_assistant_text(), Some("partial"));
        assert!(!state.is_streaming_text());
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::RequestRender, UiEffect::Exit]
        ));
    }

    #[test]
    fn live_event_projection_uses_validated_wire_fields() {
        let value = serde_json::json!({
            "type": "message.delta",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "turn": 1,
            "role": "assistant",
            "content_index": 0,
            "content_kind": "text",
            "delta": "hello"
        });
        let live = wisp_protocol::events::deserialize(value).unwrap();
        assert_eq!(
            BackendEvent::from_live(&live).unwrap(),
            BackendEvent::MessageDelta {
                turn: 1,
                delta: "hello".into(),
                content_kind: MessageContentKind::Text,
            }
        );
    }

    #[test]
    fn session_event_projection_is_bounded_and_keeps_backend_order() {
        let event = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "rpc.sessions",
            "command_id": "sessions-1",
            "sessions": [
                {"session_id": "second", "session_path": "/sessions/second.jsonl", "name": null, "updated_at": "later", "entry_count": 2},
                {"session_id": "first", "session_path": "/sessions/first.jsonl", "name": "first name", "updated_at": "earlier", "entry_count": 1}
            ],
            "selected_session_id": "first",
            "selected_session_path": "/sessions/first.jsonl",
            "selected_session_name": "first name"
        }))
        .unwrap();
        assert!(matches!(
            event,
            BackendEvent::SessionsReported { sessions, selected_session: Some(selected), .. }
                if sessions.iter().map(|session| session.session_id.as_str()).collect::<Vec<_>>() == ["second", "first"]
                    && selected.session_id == "first"
        ));
        assert!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.session.selected", "command_id": "select-1",
                "session_id": "x".repeat(SESSION_ID_MAX_BYTES + 1),
                "session_path": "/sessions/x.jsonl", "session_name": null
            }))
            .is_err()
        );
    }

    #[test]
    fn tool_event_projection_preserves_structured_fields_and_trace_defaults() {
        assert!(matches!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "call-1",
                "name": "read",
                "arguments": {"path": "README.md"}
            }))
            .unwrap(),
            BackendEvent::ToolCall(ToolCallInput { call_id, name, arguments, .. })
                if call_id == bounded_identity("call-1")
                    && name == "read"
                    && arguments["path"] == "README.md"
        ));
        assert_eq!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.approval.resolved",
                "call_id": "call-1",
                "name": "read",
                "approved": true
            }))
            .unwrap(),
            BackendEvent::ToolApprovalResolved {
                call_id: bounded_identity("call-1"),
                name: "read".into(),
                approved: true,
                reason: None,
            }
        );
        let projected = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "tool.result",
            "call_id": "call-1",
            "name": "bash",
            "output": "still running",
            "is_error": false,
            "process_id": "process-1",
            "process_state": "running",
            "stdout": "chunk",
            "stdout_dropped_bytes": 7
        }))
        .unwrap();
        assert!(matches!(
            projected,
            BackendEvent::ToolResult(result) if result.call_id == bounded_identity("call-1")
                && result.process_id.as_deref() == Some(bounded_identity("process-1").as_str())
                && result.process_state.as_deref() == Some("running")
                && result.stdout.as_deref() == Some("chunk")
                && result.stdout_dropped_bytes == 7
                && !result.truncated
        ));
    }

    #[test]
    fn tool_argument_projection_drops_payload_bodies_and_bounds_generic_maps() {
        let BackendEvent::ToolCall(write) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "write-1",
                "name": "write",
                "arguments": {"path": "file.txt", "content": "secret".repeat(200_000)}
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        assert_eq!(write.arguments, serde_json::json!({"path": "file.txt"}));
        assert_eq!(
            write.detail_source,
            ToolDetailSource::Unavailable(
                crate::tool_detail::DetailUnavailableReason::SourceOverBudget
            )
        );

        let BackendEvent::ToolCall(edit) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "edit-1",
                "name": "edit",
                "arguments": {
                    "path": "file.txt",
                    "edits": [{"oldText": "old secret", "newText": "new secret"}]
                }
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        assert_eq!(edit.arguments, serde_json::json!({"path": "file.txt"}));
        assert!(matches!(edit.detail_source, ToolDetailSource::Edit(_)));
        assert!(!edit.arguments.to_string().contains("secret"));

        let BackendEvent::ToolApprovalRequested(approval) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.approval.requested",
                "call_id": "write-approval",
                "name": "write",
                "arguments": {"path": "file.txt", "content": "bounded secret"},
                "safety": "mutating"
            }))
            .unwrap()
        else {
            panic!("tool approval expected");
        };
        assert_eq!(approval.arguments, serde_json::json!({"path": "file.txt"}));
        assert!(matches!(approval.detail_source, ToolDetailSource::Write(_)));

        let arguments = (0..1_000)
            .map(|index| {
                (
                    format!("key-{index:04}"),
                    serde_json::json!("x".repeat(1_000)),
                )
            })
            .collect::<serde_json::Map<_, _>>();
        let BackendEvent::ToolCall(generic) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "generic-1",
                "name": "extension",
                "arguments": arguments
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        let generic_wrapper = generic.arguments.as_object().unwrap();
        assert_eq!(generic_wrapper.len(), 2);
        assert_eq!(
            generic_wrapper["\0wisp.items"].as_object().unwrap().len(),
            8
        );
        assert_eq!(generic_wrapper["\0wisp.omitted"], 992);
        assert!(generic.arguments.to_string().len() < 1_000);
    }

    #[test]
    fn tool_name_projection_is_nonempty_and_character_bounded() {
        let BackendEvent::ToolCall(unnamed) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "empty-name",
                "name": "",
                "arguments": {}
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        assert_eq!(unnamed.name, "(unnamed)");

        let BackendEvent::ToolCall(multibyte) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "multibyte-name",
                "name": "🦀".repeat(129),
                "arguments": {}
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        assert_eq!(multibyte.name.chars().count(), 128);
        assert!(multibyte.name.ends_with('…'));
    }

    #[test]
    fn tool_result_projection_bounds_large_strings_before_reducer_queueing() {
        let output = format!("head{}tail", "x".repeat(200_000));
        let stdout = format!("old{}new", "y".repeat(200_000));
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "call".repeat(5_000),
                "name": "tool".repeat(5_000),
                "output": output,
                "is_error": false,
                "recovery_hint": "hint".repeat(5_000),
                "before_text": "before".repeat(50_000),
                "summary": "summary".repeat(5_000),
                "process_id": "process".repeat(5_000),
                "process_error": "error".repeat(5_000),
                "stdout": stdout
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };

        assert!(result.output.len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert!(result.stdout.as_ref().unwrap().len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert_eq!(result.output_source_bytes, 200_008);
        assert_eq!(result.stdout_source_bytes, 200_006);
        assert!(result.output.starts_with("head"));
        assert!(result.stdout.as_deref().unwrap().ends_with("new"));
        assert!(result.call_id.starts_with("h:"));
        assert!(result.process_id.as_deref().unwrap().starts_with("h:"));
        assert!(result.name.len() <= 512);
        assert!(result.recovery_hint.as_ref().unwrap().len() <= 512);
        assert!(result.summary.as_ref().unwrap().len() <= 512);
        assert!(result.process_error.as_ref().unwrap().len() <= 512);
        assert!(result.before_text.is_none());
    }

    #[test]
    fn write_before_snapshot_is_bounded_before_reducer_queueing() {
        let BackendEvent::ToolResult(retained) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "write-1",
                "name": "write",
                "output": "Wrote file.txt",
                "is_error": false,
                "before_text": "before\n",
                "created": false
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        assert_eq!(retained.before_text.as_deref(), Some("before\n"));

        let BackendEvent::ToolResult(over_budget) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "write-2",
                "name": "write",
                "output": "Wrote file.txt",
                "is_error": false,
                "before_text": "x".repeat(crate::tool_detail::DETAIL_SOURCE_MAX_BYTES + 1),
                "created": false
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        assert!(over_budget.before_text.is_none());

        let BackendEvent::ToolResult(mismatched) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "write-3",
                "name": "read",
                "output": "done",
                "is_error": false,
                "before_text": "foreign",
                "created": true
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        assert!(mismatched.before_text.is_none());
        assert!(!mismatched.created);
    }

    #[test]
    fn failed_tool_result_projection_retains_diagnostic_tail_before_queueing() {
        let output = format!(
            "EARLY PROGRESS\n{}ASSERTION FAILED AT TAIL",
            "progress\n".repeat(20_000)
        );
        let source_bytes = output.len() as u64;
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "call-failed",
                "name": "bash",
                "output": output,
                "is_error": false,
                "exit_code": 1
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };

        assert_eq!(result.output_source_bytes, source_bytes);
        assert!(result.output.len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert!(result.output.ends_with("ASSERTION FAILED AT TAIL"));
        assert!(!result.output.contains("EARLY PROGRESS"));
    }

    #[test]
    fn prebounded_output_counts_normalized_source_bytes() {
        let output = format!("{}tail\r\n", "line\r\n".repeat(20_000));
        let normalized = output.replace("\r\n", "\n");
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "call-crlf",
                "name": "read",
                "output": output,
                "is_error": false
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };

        assert_eq!(result.output_source_bytes, normalized.len() as u64);
        assert!(!result.output.contains('\r'));
        let call = crate::tool_cards::ToolCallInput {
            call_id: result.call_id.clone(),
            name: "read".into(),
            arguments: serde_json::json!({}),
            detail_source: ToolDetailSource::None,
        };
        let mut card = crate::tool_cards::ToolCardSnapshot::requested(
            &call,
            crate::tool_cards::ToolStatus::Requested,
        );
        assert!(card.apply_result(&result));
        assert_eq!(
            card.retained_output.dropped_bytes,
            normalized.len() as u64 - card.retained_output.text.len() as u64
        );
    }

    #[test]
    fn successful_result_preserves_both_directions_for_the_canonical_call_name() {
        let output = format!(
            "STARTING BUILD\n{}BUILD FINISHED SUCCESSFULLY",
            "compiling\n".repeat(20_000)
        );
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "call-success",
                "name": "read",
                "output": output,
                "is_error": false,
                "exit_code": 0
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };

        assert!(result.output.len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert!(result.output.starts_with("STARTING BUILD"));
        let output_tail = result.output_tail.as_deref().unwrap();
        assert!(output_tail.ends_with("BUILD FINISHED SUCCESSFULLY"));
        assert!(!output_tail.contains("STARTING BUILD"));

        let call = crate::tool_cards::ToolCallInput {
            call_id: result.call_id.clone(),
            name: "bash".into(),
            arguments: serde_json::json!({"command": "build"}),
            detail_source: ToolDetailSource::None,
        };
        let mut card = crate::tool_cards::ToolCardSnapshot::requested(
            &call,
            crate::tool_cards::ToolStatus::Requested,
        );
        assert!(card.apply_result(&result));
        assert!(
            card.retained_output
                .text
                .ends_with("BUILD FINISHED SUCCESSFULLY")
        );
        assert!(!card.retained_output.text.contains("STARTING BUILD"));
    }

    #[test]
    fn cancelled_bash_projection_preserves_output_head_before_queueing() {
        let output = format!(
            "CANCELLATION CONTEXT AT HEAD\n{}LATE OUTPUT",
            "running\n".repeat(20_000)
        );
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "call-cancelled",
                "name": "bash",
                "output": output,
                "is_error": true,
                "process_state": "cancelled"
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };

        assert!(result.output.len() <= crate::tool_cards::TOOL_OUTPUT_MAX_BYTES);
        assert!(result.output.starts_with("CANCELLATION CONTEXT AT HEAD"));
        assert!(!result.output.contains("LATE OUTPUT"));
    }

    #[test]
    fn validated_live_tool_result_projects_all_promoted_metadata() {
        let value = serde_json::json!({
            "type": "tool.result",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "call_id": "call-7",
            "name": "bash",
            "output": "output",
            "is_error": true,
            "failure_code": "internal_error",
            "retryable": true,
            "recovery_hint": "retry",
            "exit_code": 1,
            "output_has_exit_status": true,
            "before_text": null,
            "created": false,
            "summary": null,
            "truncated": true,
            "process_id": "process-7",
            "process_state": "failed",
            "process_error": null,
            "stdout": "out",
            "stderr": "err",
            "stdout_truncated": true,
            "stderr_truncated": false,
            "stdout_dropped_bytes": 11,
            "stderr_dropped_bytes": 12
        });
        let live = wisp_protocol::events::deserialize(value).unwrap();
        let BackendEvent::ToolResult(result) = BackendEvent::from_live(&live).unwrap() else {
            panic!("tool result expected");
        };
        assert_eq!(result.call_id, bounded_identity("call-7"));
        assert_eq!(result.exit_code, Some(1));
        assert_eq!(result.failure_code.as_deref(), Some("internal_error"));
        assert_eq!(result.recovery_hint.as_deref(), Some("retry"));
        assert_eq!(result.process_state.as_deref(), Some("failed"));
        assert_eq!(result.stderr.as_deref(), Some("err"));
        assert_eq!(result.stdout_dropped_bytes, 11);
        assert!(result.truncated);
    }

    #[test]
    fn reducer_updates_one_card_across_approval_call_and_result() {
        use crate::tool_cards::ToolStatus;

        let mut state = UiState::new("fake".into(), None, None);
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.transcript.append_prompt("read".into());
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
                call_id: "call-1".into(),
                name: "read".into(),
                arguments: serde_json::json!({"path": "README.md"}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                safety: "read".into(),
            })),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ToolCall(ToolCallInput {
                call_id: bounded_identity("call-1"),
                name: "read".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: serde_json::json!({"path": "README.md"}),
            })),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ToolApprovalResolved {
                call_id: bounded_identity("call-1"),
                name: "read".into(),
                approved: true,
                reason: None,
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(
                BackendEvent::from_projection_value(&serde_json::json!({
                    "type": "tool.result",
                    "call_id": "call-1",
                    "name": "read",
                    "output": "contents",
                    "is_error": false,
                    "summary": "Read README.md"
                }))
                .unwrap(),
            ),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.transcript.entries().len(), 2);
        let card = state.transcript.entries()[1].tool_card().unwrap();
        assert_eq!(card.status, ToolStatus::Done);
        assert_eq!(card.detail, "Read README.md");
    }

    #[test]
    fn built_in_file_results_build_structured_retained_detail() {
        for (call_id, name, arguments, output, summary) in [
            (
                "read-detail",
                "read",
                serde_json::json!({"path": "file.txt", "offset": 4}),
                "alpha\nbeta\n",
                "read 2 lines from file.txt",
            ),
            (
                "grep-detail",
                "grep",
                serde_json::json!({"path": "src", "pattern": "needle"}),
                "src/main.rs:2:needle\n",
                "grep: 1 match",
            ),
            (
                "find-detail",
                "find",
                serde_json::json!({"path": ".", "pattern": "*.rs"}),
                "src/lib.rs\nsrc/main.rs\n",
                "find: 2 files",
            ),
        ] {
            let BackendEvent::ToolCall(call) =
                BackendEvent::from_projection_value(&serde_json::json!({
                    "type": "tool.call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }))
                .unwrap()
            else {
                panic!("tool call expected");
            };
            let mut transcript = crate::transcript::Transcript::default();
            let card_id = transcript.observe_tool_call(call);
            let BackendEvent::ToolResult(result) =
                BackendEvent::from_projection_value(&serde_json::json!({
                    "type": "tool.result",
                    "call_id": call_id,
                    "name": name,
                    "output": output,
                    "is_error": false,
                    "summary": summary,
                }))
                .unwrap()
            else {
                panic!("tool result expected");
            };
            transcript.observe_tool_result(*result);
            assert!(
                transcript
                    .entry(card_id)
                    .unwrap()
                    .tool_card()
                    .unwrap()
                    .has_retained_detail(),
                "{name} should retain structured detail"
            );
        }

        let BackendEvent::ToolCall(call) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "projected-read",
                "name": "read",
                "arguments": {"path": "large.txt"},
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        let mut transcript = crate::transcript::Transcript::default();
        let card_id = transcript.observe_tool_call(call);
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "projected-read",
                "name": "read",
                "output": "line\n".repeat(600),
                "is_error": false,
                "summary": "read 600 lines from large.txt",
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        transcript.observe_tool_result(*result);
        let card = transcript.entry(card_id).unwrap().tool_card().unwrap();
        let crate::tool_detail::DetailAvailability::LiveRetained(detail) = &card.structured_detail
        else {
            panic!("structured detail expected");
        };
        assert!(detail.truncated);
        assert!(detail.rows.iter().any(|row| {
            row.kind == crate::tool_detail::DetailRowKind::Omission
                && row.hidden_rows > 0
                && row.hidden_bytes > 0
        }));

        let BackendEvent::ToolCall(call) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "mid-line-read",
                "name": "read",
                "arguments": {"path": "partial.txt"},
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        let mut transcript = crate::transcript::Transcript::default();
        let card_id = transcript.observe_tool_call(call);
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "mid-line-read",
                "name": "read",
                "output": format!("complete\n{}\nlast\n", "x".repeat(70_000)),
                "is_error": false,
                "summary": "read 3 lines from partial.txt",
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        assert!(result.output_projection_cut_mid_line);
        transcript.observe_tool_result(*result);
        let card = transcript.entry(card_id).unwrap().tool_card().unwrap();
        let crate::tool_detail::DetailAvailability::LiveRetained(detail) = &card.structured_detail
        else {
            panic!("structured detail expected");
        };
        assert!(detail.rows.iter().any(|row| {
            row.kind == crate::tool_detail::DetailRowKind::ReadLine && row.text == "complete"
        }));
        assert!(!detail.rows.iter().any(|row| {
            row.kind == crate::tool_detail::DetailRowKind::ReadLine && row.text.contains('x')
        }));
        assert!(detail.rows.iter().any(|row| {
            row.kind == crate::tool_detail::DetailRowKind::Omission && row.hidden_rows >= 2
        }));
    }

    #[test]
    fn long_approval_identity_remains_exact_for_command_and_bounded_for_card_pairing() {
        let long_call_id = "call".repeat(5_000);
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
                call_id: long_call_id.clone(),
                name: "read".into(),
                arguments: serde_json::json!({"path": "README.md"}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                safety: "read".into(),
            })),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::ApprovalDecision {
                call_id: long_call_id.clone(),
                approved: true,
                reason: None,
                scope: None,
            },
            &mut ids,
        )
        .unwrap();
        assert_eq!(command_value(&effects[0]).unwrap()["call_id"], long_call_id);
        let hashed = bounded_identity(&long_call_id);
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ToolCall(ToolCallInput {
                call_id: hashed.clone(),
                name: "read".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: serde_json::json!({"path": "README.md"}),
            })),
            &mut ids,
        )
        .unwrap();
        assert_eq!(
            state
                .transcript
                .entries()
                .iter()
                .filter(|entry| entry.tool_card().is_some())
                .count(),
            1
        );
        assert_eq!(
            state.transcript.entries()[0]
                .tool_card()
                .map(|card| card.call_id.as_str()),
            Some(hashed.as_str())
        );
    }

    #[test]
    fn trust_request_is_a_visible_blocking_state() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::TrustRequested {
                request_id: "trust-1".into(),
                project_path: "/workspace".into(),
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.view_status, ViewStatus::WaitingForTrust);
        assert_eq!(state.interaction_status, InteractionStatus::WaitingForTrust);
        assert_eq!(state.pending_trust_request_id.as_deref(), Some("trust-1"));
        assert_eq!(
            state.pending_trust_project_path.as_deref(),
            Some("/workspace")
        );
        assert!(matches!(effects.as_slice(), [UiEffect::RequestRender]));
    }

    #[test]
    fn live_trust_request_projection_preserves_request_identity() {
        let value = serde_json::json!({
            "type": "trust.requested",
            "schema_version": 34,
            "timestamp": "2026-01-02T03:04:05Z",
            "request_id": "trust-7",
            "project_path": "/workspace"
        });
        let live = wisp_protocol::events::deserialize(value).unwrap();
        assert_eq!(
            BackendEvent::from_live(&live).unwrap(),
            BackendEvent::TrustRequested {
                request_id: "trust-7".into(),
                project_path: "/workspace".into(),
            }
        );
    }

    #[test]
    fn project_config_applied_refreshes_authoritative_header_state() {
        let mut state = UiState::new("fake".into(), Some("old-model".into()), Some("low".into()));
        let mut ids = DeterministicIds::default();

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ProjectConfigApplied {
                provider: "anthropic".into(),
                model: Some("claude-test".into()),
                effort: Some("high".into()),
            }),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.provider.as_deref(), Some("anthropic"));
        assert_eq!(state.model.as_deref(), Some("claude-test"));
        assert_eq!(state.effort.as_deref(), Some("high"));
        assert!(matches!(effects.as_slice(), [UiEffect::RequestRender]));
    }

    #[test]
    fn stale_trust_does_not_mutate_state() {
        let mut state = UiState::new("fake".into(), None, None);
        state.pending_trust_request_id = Some("trust-1".into());
        let before = state.clone();
        let mut ids = DeterministicIds::default();
        assert!(matches!(
            reduce(
                &mut state,
                UiAction::TrustDecision {
                    request_id: "wrong".into(),
                    trusted: true,
                    reason: None,
                    transient: None,
                },
                &mut ids,
            ),
            Err(ReduceError::NoMatchingTrust(id)) if id == "wrong"
        ));
        assert_eq!(state, before);
        assert!(ids.0.is_empty());
    }

    #[test]
    fn trust_allow_and_deny_emit_typed_commands() {
        let mut state = UiState::new("fake".into(), None, None);
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_trust_request_id = Some("trust-1".into());
        let mut ids = DeterministicIds::default();
        let effects = reduce(
            &mut state,
            UiAction::TrustDecision {
                request_id: "trust-1".into(),
                trusted: true,
                reason: None,
                transient: None,
            },
            &mut ids,
        )
        .unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["type"], "trust");
        assert_eq!(command["id"], "trust-1");
        assert_eq!(command["trusted"], true);
        assert_eq!(command["transient"], false);
        assert!(command.get("reason").is_none());
        assert_eq!(state.view_status, ViewStatus::Running);
        assert!(state.pending_trust_request_id.is_none());
        assert!(state.pending_trust_project_path.is_none());
    }

    #[test]
    fn cancel_active_prompt_emits_once() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        assert_eq!(
            command_value(&effects[0]).unwrap(),
            serde_json::json!({"type": "cancel", "id": "cancel-1", "target_id": "prompt-1"})
        );
        assert!(state.cancel_requested);
        let repeated = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        assert!(repeated.is_empty());
        assert_eq!(ids.0.get("cancel"), Some(&1));
    }

    #[test]
    fn late_approval_after_cancel_is_denied_without_reopening_the_prompt() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.cancel_requested = true;
        let mut ids = DeterministicIds::default();

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
                call_id: "call-1".into(),
                name: "shell".into(),
                arguments: serde_json::json!({"command": "rm -rf /tmp/example"}),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                safety: "ask".into(),
            })),
            &mut ids,
        )
        .unwrap();

        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["type"], "approval");
        assert_eq!(command["call_id"], "call-1");
        assert_eq!(command["approved"], false);
        assert_eq!(command["reason"], CANCELLING_APPROVAL_REASON);
        assert!(state.cancel_requested);
        assert!(state.pending_approval.is_none());
        assert_eq!(state.view_status, ViewStatus::Running);
        assert_eq!(state.interaction_status, InteractionStatus::Running);
    }

    #[test]
    fn cancel_denies_pending_approval_instead_of_cancelling() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::WaitingForApproval;
        state.interaction_status = InteractionStatus::WaitingForApproval;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_approval = Some(PendingApproval {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: serde_json::json!({}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            safety: "read".into(),
        });
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["type"], "approval");
        assert_eq!(command["approved"], false);
        assert_eq!(command["reason"], CANCELLED_APPROVAL_REASON);
        assert!(state.pending_approval.is_none());
        assert!(!state.cancel_requested);
        assert_eq!(state.view_status, ViewStatus::Running);
    }

    #[test]
    fn late_trust_after_cancel_is_transiently_denied_without_reopening_the_prompt() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::Running;
        state.interaction_status = InteractionStatus::Running;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.cancel_requested = true;
        let mut ids = DeterministicIds::default();

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::TrustRequested {
                request_id: "trust-1".into(),
                project_path: "/workspace".into(),
            }),
            &mut ids,
        )
        .unwrap();

        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["type"], "trust");
        assert_eq!(command["request_id"], "trust-1");
        assert_eq!(command["trusted"], false);
        assert_eq!(command["reason"], CANCELLED_TRUST_REASON);
        assert_eq!(command["transient"], true);
        assert!(state.cancel_requested);
        assert!(state.pending_trust_request_id.is_none());
        assert!(state.pending_trust_project_path.is_none());
        assert_eq!(state.view_status, ViewStatus::Running);
        assert_eq!(state.interaction_status, InteractionStatus::Running);
    }

    #[test]
    fn cancel_denies_pending_trust_instead_of_cancelling() {
        let mut state = UiState::new("fake".into(), None, None);
        state.view_status = ViewStatus::WaitingForTrust;
        state.interaction_status = InteractionStatus::WaitingForTrust;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        state.pending_trust_request_id = Some("trust-1".into());
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["type"], "trust");
        assert_eq!(command["trusted"], false);
        assert_eq!(command["reason"], CANCELLED_TRUST_REASON);
        assert_eq!(command["transient"], true);
        assert!(state.pending_trust_request_id.is_none());
        assert!(!state.cancel_requested);
        assert_eq!(state.view_status, ViewStatus::Running);
    }

    fn session(id: &str) -> SessionIdentity {
        SessionIdentity {
            session_id: id.into(),
            session_path: format!("/sessions/{id}.jsonl"),
            session_name: Some(format!("{id} name")),
        }
    }

    fn history(command_id: &str, selected: Option<SessionIdentity>, text: &str) -> BackendEvent {
        let mut transcript = SharedTranscript::default();
        if !text.is_empty() {
            transcript.append_prompt(text.into());
        }
        BackendEvent::MessagesReported {
            command_id: command_id.into(),
            messages: SessionMessages {
                session: selected,
                active_leaf_id: None,
                truncated: false,
                next_before_entry_id: None,
                next_after_entry_id: None,
                durable_entry_ids: Vec::new(),
                exact_tool_result: None,
                transcript,
            },
        }
    }

    fn finished(id: &str, command_type: &str, ok: bool) -> BackendEvent {
        BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: command_type.into(),
            ok,
            error: (!ok).then(|| "backend refused".into()),
        }
    }

    #[test]
    fn startup_hydration_commits_only_after_a_matching_report_and_finish_in_any_order() {
        for report_first in [true, false] {
            let mut state = UiState::unconfigured();
            let mut ids = DeterministicIds::default();
            let effects = reduce(&mut state, UiAction::StartupHydration, &mut ids).unwrap();
            assert_eq!(command_value(&effects[0]).unwrap()["type"], "get_messages");
            assert!(!state.input_ready);
            let report = history("get_messages-1", None, "startup history");
            let finish = finished("get_messages-1", "get_messages", true);
            for event in if report_first {
                [report, finish]
            } else {
                [finish, report]
            } {
                reduce(&mut state, UiAction::BackendEvent(event), &mut ids).unwrap();
            }
            assert!(state.session_operation.is_none());
            assert!(state.input_ready);
            assert_eq!(state.transcript.latest_user_text(), Some("startup history"));
            assert!(state.selected_session.is_none());
        }
    }

    #[test]
    fn startup_failure_is_recoverable_and_wrong_or_duplicate_reports_are_ignored() {
        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(&mut state, UiAction::StartupHydration, &mut ids).unwrap();
        let before = state.clone();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(history("late", None, "wrong")),
            &mut ids,
        )
        .unwrap();
        assert!(effects.is_empty());
        assert_eq!(state, before);
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", false)),
            &mut ids,
        )
        .unwrap();
        assert!(state.session_operation.is_none());
        assert!(state.input_ready);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );
        let after_failure = state.clone();
        assert!(
            reduce(
                &mut state,
                UiAction::BackendEvent(history("get_messages-1", None, "late")),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        assert_eq!(state, after_failure);
    }

    #[test]
    fn history_projection_failure_is_a_correlated_hydration_failure() {
        let event = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "rpc.messages",
            "command_id": "get_messages-1",
            "session_id": null,
            "session_path": null,
            "truncated": false,
            "messages": [{
                "role": "assistant",
                "content": "",
                "content_truncated": false,
                "tool_calls": [{"call_id": "call", "name": "read", "arguments": []}],
            }],
        }))
        .unwrap();
        assert!(matches!(
            event,
            BackendEvent::MessagesProjectionFailed { ref command_id, .. }
                if command_id == "get_messages-1"
        ));

        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(&mut state, UiAction::StartupHydration, &mut ids).unwrap();
        let effects = reduce(&mut state, UiAction::BackendEvent(event), &mut ids).unwrap();
        assert!(state.session_operation.is_none());
        assert!(state.input_ready);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(notice) if notice.contains("startup history failed")))
        );
    }

    #[test]
    fn invisible_system_entries_are_not_retained_as_window_members() {
        let mut transcript = SharedTranscript::default();
        transcript.append_prompt("visible".into());
        transcript.mark_history_entries(0, "user-entry");
        let mut state = UiState::unconfigured();

        install_history_snapshot(
            &mut state,
            SessionMessages {
                session: Some(session("active")),
                active_leaf_id: Some("leaf".into()),
                truncated: true,
                next_before_entry_id: Some("system-entry".into()),
                next_after_entry_id: None,
                durable_entry_ids: vec!["system-entry".into(), "user-entry".into()],
                exact_tool_result: None,
                transcript,
            },
        );

        assert_eq!(
            state.history.represented_durable_entry_order,
            vec!["user-entry"]
        );
        assert_eq!(
            state.history.represented_durable_entry_ids,
            ["user-entry".to_owned()].into_iter().collect()
        );
    }

    #[test]
    fn startup_history_with_an_older_cursor_installs_one_omission_marker() {
        let event = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "rpc.messages",
            "command_id": "get_messages-1",
            "session_id": null,
            "session_path": null,
            "truncated": true,
            "next_before_entry_id": "entry-1",
            "messages": [{
                "entry_id": "entry-1",
                "role": "user",
                "content": "retained",
                "content_truncated": false,
            }],
        }))
        .unwrap();

        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(&mut state, UiAction::StartupHydration, &mut ids).unwrap();
        reduce(&mut state, UiAction::BackendEvent(event), &mut ids).unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.transcript.history_omission_count(), 1);
        assert_eq!(state.transcript.entries().len(), 2);
        assert_eq!(
            state.transcript.entries()[0].content,
            "[earlier session history omitted]"
        );
        assert_eq!(state.transcript.entries()[1].content, "retained");
    }

    #[test]
    fn catalog_preserves_backend_order_and_opens_after_both_correlated_events() {
        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(&mut state, UiAction::LoadSessionCatalog, &mut ids).unwrap();
        let sessions = vec![
            SessionSummary {
                session_id: "second".into(),
                session_path: "/sessions/second.jsonl".into(),
                name: None,
                updated_at: "later".into(),
                entry_count: 2,
            },
            SessionSummary {
                session_id: "first".into(),
                session_path: "/sessions/first.jsonl".into(),
                name: None,
                updated_at: "earlier".into(),
                entry_count: 1,
            },
        ];
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionsReported {
                command_id: "get_sessions-1".into(),
                sessions: sessions.clone(),
                selected_session: Some(session("first")),
            }),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_sessions-1", "get_sessions", true)),
            &mut ids,
        )
        .unwrap();
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::ShowSessionPicker { sessions: shown, selected_session_id: Some(selected) }, ..]
                if shown == &sessions && selected == "first"
        ));
        assert!(state.input_ready);
    }

    #[test]
    fn selection_failure_preserves_old_content_but_committed_hydration_failure_clears_it() {
        let mut state = UiState::unconfigured();
        state.selected_session = Some(session("old"));
        state.transcript.append_prompt("old content".into());
        let before = state.clone();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::SelectSession {
                session_id: "new".into(),
            },
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionSelected {
                command_id: "select_session-1".into(),
                session: session("new"),
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("select_session-1", "select_session", false)),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.selected_session, before.selected_session);
        assert_eq!(state.transcript, before.transcript);

        reduce(
            &mut state,
            UiAction::SelectSession {
                session_id: "new".into(),
            },
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionSelected {
                command_id: "select_session-2".into(),
                session: session("new"),
            }),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("select_session-2", "select_session", true)),
            &mut ids,
        )
        .unwrap();
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::ReplaceTranscript))
        );
        assert_eq!(state.selected_session, Some(session("new")));
        assert!(state.transcript.entries().is_empty());
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", false)),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.selected_session, Some(session("new")));
        assert!(state.transcript.entries().is_empty());
        assert!(state.input_ready);
    }

    #[test]
    fn selection_hydration_rejects_a_correlated_wrong_session_report() {
        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::SelectSession {
                session_id: "new".into(),
            },
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionSelected {
                command_id: "select_session-1".into(),
                session: session("new"),
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("select_session-1", "select_session", true)),
            &mut ids,
        )
        .unwrap();

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(history(
                "get_messages-1",
                Some(session("wrong")),
                "wrong content",
            )),
            &mut ids,
        )
        .unwrap();

        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );
        assert!(state.session_operation.is_none());
        assert!(state.input_ready);
        assert!(state.transcript.entries().is_empty());
    }

    #[test]
    fn new_session_is_transactional_and_never_hydrates() {
        let mut state = UiState::unconfigured();
        state.selected_session = Some(session("old"));
        state.transcript.append_prompt("old content".into());
        let before = state.clone();
        let mut ids = DeterministicIds::default();
        reduce(&mut state, UiAction::NewSession, &mut ids).unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("new_session-1", "new_session", false)),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.selected_session, before.selected_session);
        assert_eq!(state.transcript, before.transcript);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );

        reduce(&mut state, UiAction::NewSession, &mut ids).unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("new_session-2", "new_session", true)),
            &mut ids,
        )
        .unwrap();
        assert!(state.selected_session.is_none());
        assert!(state.transcript.entries().is_empty());
        assert!(
            effects
                .iter()
                .all(|effect| !matches!(effect, UiEffect::SendCommand(_)))
        );
    }

    #[test]
    fn oversized_session_ids_do_not_mutate_state_and_a_later_request_is_recoverable() {
        let mut state = UiState::unconfigured();
        let before = state.clone();
        let mut ids = DeterministicIds::default();
        let effects = reduce(
            &mut state,
            UiAction::SelectSession {
                session_id: "x".repeat(SESSION_ID_MAX_BYTES + 1),
            },
            &mut ids,
        )
        .unwrap();
        assert_eq!(state, before);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );
        let effects = reduce(
            &mut state,
            UiAction::SelectSession {
                session_id: "valid".into(),
            },
            &mut ids,
        )
        .unwrap();
        assert_eq!(
            command_value(&effects[0]).unwrap()["type"],
            "select_session"
        );
    }

    #[test]
    fn older_history_waits_for_both_events_and_allows_the_active_leaf_to_advance() {
        let selected = session("active");
        let mut state = UiState::new("fake".into(), None, None);
        state.selected_session = Some(selected.clone());
        state.history.session = Some(selected.clone());
        state.history.active_leaf_id = Some("old-leaf".into());
        state.history.oldest_cursor = Some("current-entry".into());
        state
            .history
            .represented_durable_entry_ids
            .insert("current-entry".into());
        state.transcript.append_prompt("current".into());
        state.transcript.mark_history_entries(0, "current-entry");
        state.transcript.replace_history_omission_marker(true);
        let marker_id = state.transcript.entries()[0].id;
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut ids = DeterministicIds::default();

        let effects = reduce(&mut state, UiAction::LoadOlderHistory, &mut ids).unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["before_entry_id"], "current-entry");
        assert_eq!(command["allow_during_prompt"], true);

        assert!(
            reduce(
                &mut state,
                UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        assert!(state.history_request.is_some());

        let mut older = SharedTranscript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(selected),
                    active_leaf_id: Some("new-leaf".into()),
                    truncated: true,
                    next_before_entry_id: Some("older-entry".into()),
                    next_after_entry_id: None,
                    durable_entry_ids: vec!["older-entry".into()],
                    exact_tool_result: None,
                    transcript: older,
                },
            }),
            &mut ids,
        )
        .unwrap();

        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::HistoryWindowChanged))
        );
        assert!(state.history_request.is_none());
        assert_eq!(state.history.active_leaf_id.as_deref(), Some("new-leaf"));
        assert_eq!(state.history.oldest_cursor.as_deref(), Some("older-entry"));
        assert_eq!(state.transcript.entries()[0].id, marker_id);
        assert_eq!(state.transcript.entries()[1].content, "older");
        assert_eq!(state.transcript.entries()[2].content, "current");
        assert_eq!(
            state
                .transcript
                .entries()
                .iter()
                .filter(|entry| entry.content == "[earlier session history omitted]")
                .count(),
            1
        );
    }

    #[test]
    fn newer_history_uses_the_retained_tail_cursor_and_clears_eviction_state() {
        let selected = session("active");
        let mut state = UiState::new("fake".into(), None, None);
        state.history.session = Some(selected.clone());
        state.history.active_leaf_id = Some("leaf".into());
        state.history.newest_cursor = Some("current-entry".into());
        state.history.tail_evicted = true;
        state
            .history
            .represented_durable_entry_ids
            .insert("current-entry".into());
        state.transcript.append_prompt("current".into());
        state.transcript.mark_history_entries(0, "current-entry");
        let mut ids = DeterministicIds::default();

        let effects = reduce(&mut state, UiAction::LoadNewerHistory, &mut ids).unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["after_entry_id"], "current-entry");
        let mut newer = SharedTranscript::default();
        newer.append_prompt("newer".into());
        newer.mark_history_entries(0, "newer-entry");
        assert!(
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::MessagesReported {
                    command_id: "get_messages-1".into(),
                    messages: SessionMessages {
                        session: Some(selected),
                        active_leaf_id: Some("leaf".into()),
                        truncated: false,
                        next_before_entry_id: None,
                        next_after_entry_id: None,
                        durable_entry_ids: vec!["newer-entry".into()],
                        exact_tool_result: None,
                        transcript: newer,
                    },
                }),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();

        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::HistoryWindowChanged))
        );
        assert!(!state.history.tail_evicted);
        assert!(state.history.newest_cursor.is_none());
        assert_eq!(state.transcript.entries()[0].content, "current");
        assert_eq!(state.transcript.entries()[1].content, "newer");
    }

    #[test]
    fn tail_eviction_uses_persisted_order_for_parallel_tool_results() {
        let selected = session("active");
        let mut state = UiState::new("fake".into(), None, None);
        state.history.session = Some(selected.clone());
        state.history.active_leaf_id = Some("leaf".into());
        let mut order = Vec::new();
        for index in 0..1_197 {
            let start = state.transcript.entries().len();
            let entry_id = format!("entry-{index}");
            state.transcript.append_prompt(format!("message-{index}"));
            state.transcript.mark_history_entries(start, &entry_id);
            order.push(entry_id);
        }
        let start = state.transcript.entries().len();
        let first = state.transcript.observe_tool_call(ToolCallInput {
            call_id: "first".into(),
            name: "read".into(),
            arguments: serde_json::json!({"path": "first.txt"}),
            detail_source: ToolDetailSource::None,
        });
        let second = state.transcript.observe_tool_call(ToolCallInput {
            call_id: "second".into(),
            name: "read".into(),
            arguments: serde_json::json!({"path": "second.txt"}),
            detail_source: ToolDetailSource::None,
        });
        state
            .transcript
            .mark_history_entries(start, "assistant-entry");
        state.transcript.add_history_origin(second, "result-second");
        state.transcript.add_history_origin(first, "result-first");
        order.extend([
            "assistant-entry".into(),
            "result-second".into(),
            "result-first".into(),
        ]);
        let start = state.transcript.entries().len();
        state.transcript.append_prompt("tail".into());
        state.transcript.mark_history_entries(start, "tail-entry");
        order.push("tail-entry".into());
        state.history.represented_durable_entry_ids =
            state.transcript.historical_durable_entry_ids();
        state.history.represented_durable_entry_order = order;
        state.history.oldest_cursor = Some("entry-0".into());
        let mut ids = DeterministicIds::default();

        reduce(&mut state, UiAction::LoadOlderHistory, &mut ids).unwrap();
        let mut older = SharedTranscript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(selected),
                    active_leaf_id: Some("leaf".into()),
                    truncated: false,
                    next_before_entry_id: None,
                    next_after_entry_id: None,
                    durable_entry_ids: vec!["older-entry".into()],
                    exact_tool_result: None,
                    transcript: older,
                },
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();

        assert!(state.history.tail_evicted);
        assert_eq!(state.history.newest_cursor.as_deref(), Some("result-first"));
        assert_eq!(state.transcript.entries().len(), 1_200);
    }

    #[test]
    fn evicted_history_with_a_live_suffix_reloads_latest_instead_of_duplicating_rows() {
        let mut state = UiState::new("fake".into(), None, None);
        state.history.session = Some(session("active"));
        state.history.active_leaf_id = Some("old-leaf".into());
        state.history.newest_cursor = Some("historical-tail".into());
        state.history.tail_evicted = true;
        state.transcript.append_prompt("live prompt".into());
        let mut ids = DeterministicIds::default();

        let effects = reduce(&mut state, UiAction::LoadNewerHistory, &mut ids).unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["type"], "get_messages");
        assert!(command.get("after_entry_id").is_none());
        assert!(matches!(
            state.history_request.as_ref().map(|request| &request.kind),
            Some(HistoryRequestKind::Latest)
        ));
        assert!(
            state
                .history_request
                .as_ref()
                .is_some_and(|request| request.active_leaf_may_advance)
        );
    }

    #[test]
    fn stale_history_branch_is_rejected_without_mutating_the_window() {
        let selected = session("active");
        let mut state = UiState::new("fake".into(), None, None);
        state.history.session = Some(selected.clone());
        state.history.active_leaf_id = Some("expected-leaf".into());
        state.history.oldest_cursor = Some("current-entry".into());
        state
            .history
            .represented_durable_entry_ids
            .insert("current-entry".into());
        state.transcript.append_prompt("current".into());
        state.transcript.mark_history_entries(0, "current-entry");
        let before = state.transcript.clone();
        let mut ids = DeterministicIds::default();

        reduce(&mut state, UiAction::LoadOlderHistory, &mut ids).unwrap();
        let mut older = SharedTranscript::default();
        older.append_prompt("wrong branch".into());
        older.mark_history_entries(0, "older-entry");
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(selected),
                    active_leaf_id: Some("other-leaf".into()),
                    truncated: false,
                    next_before_entry_id: None,
                    next_after_entry_id: None,
                    durable_entry_ids: vec!["older-entry".into()],
                    exact_tool_result: None,
                    transcript: older,
                },
            }),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.transcript, before);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );
    }

    #[test]
    fn exact_history_detail_fetches_only_backend_projected_file_results() {
        let selected = session("active");
        let mut state = UiState::new("fake".into(), None, None);
        state.history.session = Some(selected.clone());
        state.history.active_leaf_id = Some("leaf".into());
        let BackendEvent::ToolCall(call) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "read-1",
                "name": "read",
                "arguments": {"path": "large.txt"},
            }))
            .unwrap()
        else {
            panic!("tool call expected");
        };
        let target = state.transcript.observe_tool_call(call);
        state.transcript.mark_history_entries(0, "call-entry");
        let BackendEvent::ToolResult(result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "read-1",
                "name": "read",
                "output": "partial",
                "is_error": false,
                "truncated": true,
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        state.transcript.observe_tool_result(*result);
        state.transcript.add_history_origin(target, "result-entry");
        state
            .history
            .represented_durable_entry_ids
            .extend(["call-entry".into(), "result-entry".into()]);
        let mut ids = DeterministicIds::default();

        assert!(
            reduce(&mut state, UiAction::LoadExactDetail { target }, &mut ids,)
                .unwrap()
                .is_empty()
        );
        state
            .transcript
            .mark_history_result_projection(target, true);
        let effects = reduce(&mut state, UiAction::LoadExactDetail { target }, &mut ids).unwrap();
        let command = command_value(&effects[0]).unwrap();
        assert_eq!(command["entry_ids"], serde_json::json!(["result-entry"]));
        assert_eq!(command["full_content"], true);

        let BackendEvent::ToolResult(full_result) =
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.result",
                "call_id": "read-1",
                "name": "read",
                "output": "alpha\nbeta\n",
                "is_error": false,
                "summary": "read 2 lines from large.txt",
                "truncated": false,
            }))
            .unwrap()
        else {
            panic!("tool result expected");
        };
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(selected),
                    active_leaf_id: Some("leaf".into()),
                    truncated: false,
                    next_before_entry_id: None,
                    next_after_entry_id: None,
                    durable_entry_ids: vec!["result-entry".into()],
                    exact_tool_result: Some(full_result),
                    transcript: SharedTranscript::default(),
                },
            }),
            &mut ids,
        )
        .unwrap();

        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::OpenExactDetail(id) if *id == target)),
            "{effects:?}"
        );
        assert_eq!(
            state
                .history
                .active_exact_detail
                .as_ref()
                .map(|detail| detail.target),
            Some(target)
        );
    }
}
