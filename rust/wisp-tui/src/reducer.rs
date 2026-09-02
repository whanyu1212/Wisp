//! Deterministic, terminal-independent state transitions for the native TUI.

use serde_json::Value;
use thiserror::Error;
use wisp_protocol::ProtocolDecodeError;
use wisp_protocol::events::{ConnectionCatalogSnapshot, DeviceCodeChallenge, DeviceCodeProgress};

use crate::tool_cards::{BoundedText, ToolCallInput, ToolResultInput, bounded_identity};
pub use crate::tool_detail::ToolDetailSource;
use crate::transcript::SharedTranscript;
use wisp_protocol::commands::{ApprovalScope, QueueKind, WispTypedClientRpcCommands};

mod event_projection;

pub use event_projection::EventProjectionError;

const DEFAULT_DENIAL_REASON: &str = "Denied from TUI";
const CANCELLED_APPROVAL_REASON: &str = "Denied from TUI: cancelled";
const CANCELLING_APPROVAL_REASON: &str = "Denied from TUI: cancelling";
const CANCELLED_TRUST_REASON: &str = "Trust prompt cancelled";
const RPC_CANCELLED_PREFIX: &str = "RPC command cancelled:";
pub const SESSION_CATALOG_LIMIT: usize = 50;
pub const SESSION_TREE_PAGE_LIMIT: usize = 200;
pub const SESSION_TREE_RETAINED_LIMIT: usize = 400;
pub const SESSION_ID_MAX_BYTES: usize = 4 * 1024;
const SESSION_PATH_MAX_BYTES: usize = 4 * 1024;
const SESSION_LABEL_MAX_BYTES: usize = 512;
const SESSION_UPDATED_AT_MAX_BYTES: usize = 128;
const SESSION_ENTRY_COUNT_MAX: u32 = 1_000_000_000;
const SESSION_NOTICE_MAX_BYTES: usize = 1024;
pub const API_KEY_MAX_BYTES: usize = 8_192;
pub const QUEUE_MESSAGE_LIMIT: usize = 100;
pub const QUEUE_CONTENT_BYTES_LIMIT: usize = 8 * 1024 * 1024;

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionTreeNodeKind {
    Message,
    Event,
    Compaction,
}

impl SessionTreeNodeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Message => "message",
            Self::Event => "event",
            Self::Compaction => "compaction",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionTreeNode {
    pub entry_id: String,
    pub parent_id: Option<String>,
    pub created_at: String,
    pub kind: SessionTreeNodeKind,
    pub role: Option<String>,
    pub preview: String,
    pub preview_truncated: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionTreePage {
    pub session: Option<SessionIdentity>,
    pub active_leaf_id: Option<String>,
    pub total_node_count: u32,
    pub nodes: Vec<SessionTreeNode>,
    pub truncated: bool,
    pub next_after_entry_id: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionDerivation {
    pub source: SessionIdentity,
    pub source_active_leaf_id: Option<String>,
    pub session: SessionIdentity,
    pub active_leaf_id: Option<String>,
    pub entry_count: u32,
    pub selected_entry_id: Option<String>,
    pub selected_prompt: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionNameChange {
    pub session: SessionIdentity,
    pub previous_name: Option<String>,
    pub entry_count: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionTreeNavigation {
    pub session: SessionIdentity,
    pub selected_entry_id: String,
    pub previous_active_leaf_id: Option<String>,
    pub active_leaf_id: Option<String>,
    pub editor_text: Option<String>,
    pub changed: bool,
    pub entry_count: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionTreeUnrevert {
    pub session: SessionIdentity,
    pub source_transition_id: String,
    pub previous_active_leaf_id: Option<String>,
    pub active_leaf_id: Option<String>,
    pub entry_count: u32,
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
    PostPromptSync,
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

#[derive(Clone, Eq, PartialEq)]
pub struct ApiKey(String);

impl std::fmt::Debug for ApiKey {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ApiKey(<redacted>)")
    }
}

impl ApiKey {
    pub fn new(value: String) -> Option<Self> {
        (!value.trim().is_empty() && value.len() <= API_KEY_MAX_BYTES).then_some(Self(value))
    }

    fn as_str(&self) -> &str {
        &self.0
    }
}

pub struct SecretCommand(WispTypedClientRpcCommands);

impl std::fmt::Debug for SecretCommand {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("SecretCommand(<redacted>)")
    }
}

impl SecretCommand {
    pub(crate) fn into_inner(self) -> WispTypedClientRpcCommands {
        self.0
    }
}

#[derive(Clone, Debug, PartialEq)]
enum ConnectionOperation {
    Catalog {
        command_id: String,
        command_type: &'static str,
        report: Option<ConnectionCatalogSnapshot>,
        completion: Option<SessionCompletion>,
    },
    DeviceCode {
        command_id: String,
        provider: String,
        report: Option<ConnectionCatalogSnapshot>,
        challenge: Option<DeviceCodeChallenge>,
        progress: Option<DeviceCodeProgress>,
        completion: Option<SessionCompletion>,
        cancel_requested: bool,
    },
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
        restore_editor_text: Option<String>,
        committed_operation: &'static str,
        report: Option<SessionMessages>,
        completion: Option<SessionCompletion>,
    },
    NamingSession {
        command_id: String,
        changed: Option<SessionNameChange>,
        completion: Option<SessionCompletion>,
    },
    CloningSession {
        command_id: String,
        derived: Option<SessionDerivation>,
        completion: Option<SessionCompletion>,
    },
    ForkingSession {
        command_id: String,
        requested_entry_id: String,
        derived: Option<SessionDerivation>,
        completion: Option<SessionCompletion>,
    },
    LoadingTree {
        command_id: String,
        after_entry_id: Option<String>,
        page: Option<SessionTreePage>,
        completion: Option<SessionCompletion>,
    },
    NavigatingTree {
        command_id: String,
        requested_entry_id: String,
        navigation: Option<SessionTreeNavigation>,
        completion: Option<SessionCompletion>,
    },
    UnrevertingTree {
        command_id: String,
        unreverted: Option<SessionTreeUnrevert>,
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
            Self::NamingSession { .. } => "Naming session…",
            Self::CloningSession { .. } => "Cloning session…",
            Self::ForkingSession { .. } => "Forking session…",
            Self::LoadingTree { .. } => "Loading session tree…",
            Self::NavigatingTree { .. } => "Navigating session tree…",
            Self::UnrevertingTree { .. } => "Restoring session tree…",
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

/// One queue item retained by the reducer.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct QueuedMessage {
    pub content: String,
    pub local_order: Option<u64>,
    identity: u64,
}

/// Authoritative queue contents plus local identity and ordering metadata.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct QueueState {
    pub steering: Vec<QueuedMessage>,
    pub follow_up: Vec<QueuedMessage>,
    next_identity: u64,
}

impl QueueState {
    fn messages(&self, kind: QueueKind) -> &[QueuedMessage] {
        match kind {
            QueueKind::Steering => &self.steering,
            QueueKind::FollowUp => &self.follow_up,
        }
    }

    fn messages_mut(&mut self, kind: QueueKind) -> &mut Vec<QueuedMessage> {
        match kind {
            QueueKind::Steering => &mut self.steering,
            QueueKind::FollowUp => &mut self.follow_up,
        }
    }

    fn replace(&mut self, kind: QueueKind, contents: Vec<String>) -> Vec<u64> {
        let previous = std::mem::take(self.messages_mut(kind));
        let mut previous = previous.into_iter().map(Some).collect::<Vec<_>>();
        let mut replacement = Vec::with_capacity(contents.len());
        let mut new_identities = Vec::new();

        for content in contents {
            if let Some(index) = previous.iter().position(|message| {
                message
                    .as_ref()
                    .is_some_and(|message| message.content == content)
            }) {
                replacement.push(previous[index].take().expect("matched queue item exists"));
            } else {
                let identity = self.next_identity;
                self.next_identity = self
                    .next_identity
                    .checked_add(1)
                    .expect("queue item identity exhausted");
                replacement.push(QueuedMessage {
                    content,
                    local_order: None,
                    identity,
                });
                new_identities.push(identity);
            }
        }
        *self.messages_mut(kind) = replacement;
        new_identities
    }

    fn assign_local_order(&mut self, kind: QueueKind, identity: u64, local_order: u64) {
        if let Some(message) = self
            .messages_mut(kind)
            .iter_mut()
            .find(|message| message.identity == identity)
        {
            message.local_order = Some(local_order);
        }
    }

    fn item_matches(&self, kind: QueueKind, identity: u64, content: &str) -> bool {
        self.messages(kind)
            .iter()
            .any(|message| message.identity == identity && message.content == content)
    }

    fn remove_first(&mut self, kind: QueueKind, content: &str) -> bool {
        let messages = self.messages_mut(kind);
        let Some(index) = messages
            .iter()
            .position(|message| message.content == content)
        else {
            return false;
        };
        messages.remove(index);
        true
    }

    fn remove_last(&mut self, kind: QueueKind, content: &str) -> bool {
        let messages = self.messages_mut(kind);
        let Some(index) = messages
            .iter()
            .rposition(|message| message.content == content)
        else {
            return false;
        };
        messages.remove(index);
        true
    }

    fn newest_kind(&self) -> Option<QueueKind> {
        let newest = self
            .steering
            .iter()
            .map(|message| (QueueKind::Steering, message))
            .chain(
                self.follow_up
                    .iter()
                    .map(|message| (QueueKind::FollowUp, message)),
            )
            .filter_map(|(kind, message)| message.local_order.map(|order| (kind, order)))
            .max_by_key(|(_, order)| *order)
            .map(|(kind, _)| kind);
        newest.or_else(|| {
            self.follow_up
                .last()
                .map(|_| QueueKind::FollowUp)
                .or_else(|| self.steering.last().map(|_| QueueKind::Steering))
        })
    }

    fn newest(&self) -> Option<(QueueKind, &QueuedMessage)> {
        let kind = self.newest_kind()?;
        self.messages(kind).last().map(|message| (kind, message))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QueueRemovalOperation {
    Pop,
    Clear,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PendingQueueSubmission {
    kind: QueueKind,
    content: String,
    local_order: u64,
    observed_queue_update: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PendingQueueRestore {
    command_id: String,
    kind: QueueKind,
    removal_received: bool,
    removed_content: Option<String>,
    completion: Option<bool>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum QueueSubmissionPreflightError {
    Empty,
    Full,
}

impl QueueSubmissionPreflightError {
    pub(crate) const fn notice(self) -> &'static str {
        match self {
            Self::Empty => "Enter non-empty text before queueing.",
            Self::Full => "Queue is full (100 messages or 8 MiB); kept your draft.",
        }
    }
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
    pub connection_catalog: ConnectionCatalogSnapshot,
    connection_operation: Option<ConnectionOperation>,
    connection_catalog_reload_pending: bool,
    pub queue: QueueState,
    pub current_command: Option<ActiveCommand>,
    pub pending_approval: Option<PendingApproval>,
    pub pending_trust_request_id: Option<String>,
    pub pending_trust_project_path: Option<String>,
    pub cancel_requested: bool,
    pub exit_requested: bool,
    pub transcript: SharedTranscript,
    pub history: HistoryWindow,
    history_request: Option<HistoryRequest>,
    post_prompt_session_sync_pending: bool,
    post_prompt_stats_command_id: Option<String>,
    pending_queue_submissions: std::collections::BTreeMap<String, PendingQueueSubmission>,
    pending_queue_restore: Option<PendingQueueRestore>,
    next_queue_order: u64,
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
            connection_catalog: ConnectionCatalogSnapshot {
                providers: Vec::new(),
            },
            connection_operation: None,
            connection_catalog_reload_pending: false,
            queue: QueueState::default(),
            current_command: None,
            pending_approval: None,
            pending_trust_request_id: None,
            pending_trust_project_path: None,
            cancel_requested: false,
            exit_requested: false,
            transcript: SharedTranscript::default(),
            history: HistoryWindow::default(),
            history_request: None,
            post_prompt_session_sync_pending: false,
            post_prompt_stats_command_id: None,
            pending_queue_submissions: std::collections::BTreeMap::new(),
            pending_queue_restore: None,
            next_queue_order: 0,
        }
    }

    pub fn queued_steering(&self) -> usize {
        self.queue.steering.len()
    }

    pub fn queued_follow_ups(&self) -> usize {
        self.queue.follow_up.len()
    }

    pub(crate) fn editor_editable(&self) -> bool {
        !self.exit_requested
            && self.input_ready
            && self.session_operation.is_none()
            && match self.current_command.as_ref() {
                None => self.view_status == ViewStatus::Idle,
                Some(ActiveCommand {
                    command_type: ActiveCommandType::Prompt,
                    ..
                }) => {
                    self.view_status == ViewStatus::Running
                        && self.interaction_status == InteractionStatus::Running
                        && !self.cancel_requested
                }
                Some(_) => false,
            }
    }

    pub(crate) fn active_prompt_editable(&self) -> bool {
        self.editor_editable() && self.current_command.is_some()
    }

    pub(crate) fn queue_restore_candidate(&self) -> Option<(QueueKind, &str)> {
        self.pending_queue_restore
            .is_none()
            .then(|| self.queue.newest())
            .flatten()
            .map(|(kind, message)| (kind, message.content.as_str()))
    }

    pub(crate) fn queue_submission_preflight(
        &self,
        content: &str,
    ) -> Result<(), QueueSubmissionPreflightError> {
        if content.trim().is_empty() {
            return Err(QueueSubmissionPreflightError::Empty);
        }
        let (count, bytes) = self
            .queue_items()
            .map(|(_, _, content)| content)
            .chain(
                self.unobserved_queue_submissions()
                    .map(|(_, content)| content),
            )
            .fold((0usize, 0usize), |(count, bytes), content| {
                (count.saturating_add(1), bytes.saturating_add(content.len()))
            });
        if count.saturating_add(1) > QUEUE_MESSAGE_LIMIT
            || bytes.saturating_add(content.len()) > QUEUE_CONTENT_BYTES_LIMIT
        {
            return Err(QueueSubmissionPreflightError::Full);
        }
        Ok(())
    }

    pub(crate) fn queue_items(&self) -> impl Iterator<Item = (QueueKind, u64, &str)> {
        self.queue
            .steering
            .iter()
            .map(|message| {
                (
                    QueueKind::Steering,
                    message.identity,
                    message.content.as_str(),
                )
            })
            .chain(self.queue.follow_up.iter().map(|message| {
                (
                    QueueKind::FollowUp,
                    message.identity,
                    message.content.as_str(),
                )
            }))
    }

    pub(crate) fn unobserved_queue_submissions(&self) -> impl Iterator<Item = (QueueKind, &str)> {
        self.pending_queue_submissions
            .values()
            .filter(|pending| !pending.observed_queue_update)
            .map(|pending| (pending.kind, pending.content.as_str()))
    }

    pub(crate) fn pending_queue_restore_item(&self) -> Option<(QueueKind, &str)> {
        let pending = self.pending_queue_restore.as_ref()?;
        pending
            .removed_content
            .as_deref()
            .map(|content| (pending.kind, content))
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
    Steer,
    FollowUp,
    PopQueue,
    GetQueueState,
    Approval,
    Cancel,
    Trust,
    GetSessionStats,
    GetSessions,
    NewSession,
    SelectSession,
    GetMessages,
    SetSessionName,
    CloneSession,
    ForkSession,
    GetSessionTree,
    NavigateSessionTree,
    UnrevertSessionTree,
    GetConnectionCatalog,
    StoreApiKey,
    DisconnectProvider,
    BeginDeviceCode,
}

impl CommandKind {
    pub fn prefix(self) -> &'static str {
        match self {
            Self::Prompt => "prompt",
            Self::Steer => "steer",
            Self::FollowUp => "follow_up",
            Self::PopQueue => "pop_queue",
            Self::GetQueueState => "get_queue_state",
            Self::Approval => "approval",
            Self::Cancel => "cancel",
            Self::Trust => "trust",
            Self::GetSessionStats => "get_session_stats",
            Self::GetSessions => "get_sessions",
            Self::NewSession => "new_session",
            Self::SelectSession => "select_session",
            Self::GetMessages => "get_messages",
            Self::SetSessionName => "set_session_name",
            Self::CloneSession => "clone_session",
            Self::ForkSession => "fork_session",
            Self::GetSessionTree => "get_session_tree",
            Self::NavigateSessionTree => "navigate_session_tree",
            Self::UnrevertSessionTree => "unrevert_session_tree",
            Self::GetConnectionCatalog => "get_connection_catalog",
            Self::StoreApiKey => "store_api_key",
            Self::DisconnectProvider => "disconnect_provider",
            Self::BeginDeviceCode => "begin_device_code",
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
    ConnectionCatalogReported {
        command_id: String,
        catalog: ConnectionCatalogSnapshot,
    },
    DeviceCodeReported {
        command_id: String,
        challenge: DeviceCodeChallenge,
    },
    DeviceCodeProgress {
        command_id: String,
        progress: DeviceCodeProgress,
    },
    QueueUpdated {
        steering: Vec<String>,
        follow_up: Vec<String>,
    },
    QueueItemsRemoved {
        command_id: String,
        operation: QueueRemovalOperation,
        kind: Option<QueueKind>,
        steering: Vec<String>,
        follow_up: Vec<String>,
    },
    QueueMessageInjected {
        kind: QueueKind,
        content: String,
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
    SessionNameChanged {
        command_id: String,
        changed: SessionNameChange,
    },
    SessionCloned {
        command_id: String,
        derived: SessionDerivation,
    },
    SessionForked {
        command_id: String,
        derived: SessionDerivation,
    },
    SessionTreeReported {
        command_id: String,
        page: SessionTreePage,
    },
    SessionTreeNavigated {
        command_id: String,
        navigation: SessionTreeNavigation,
    },
    SessionTreeUnreverted {
        command_id: String,
        unreverted: SessionTreeUnrevert,
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
    Steer(String),
    FollowUp(String),
    RestoreNewestQueueDraft,
    RefreshQueueState,
    StartupHydration,
    OpenConnectionPanel,
    LoadConnectionCatalog,
    StoreApiKey {
        provider: String,
        api_key: ApiKey,
    },
    DisconnectProvider {
        provider: String,
    },
    BeginDeviceCode {
        provider: String,
    },
    CancelDeviceCode,
    LoadSessionCatalog,
    SelectSession {
        session_id: String,
    },
    NewSession,
    SetSessionName(String),
    CloneSession,
    ForkSession {
        entry_id: String,
    },
    LoadSessionTree {
        after_entry_id: Option<String>,
    },
    NavigateSessionTree {
        entry_id: String,
    },
    UnrevertSessionTree,
    RejectCommittedHydration {
        command_id: String,
        limit: usize,
    },
    RejectPostPromptSessionSync {
        command_id: String,
        limit: usize,
    },
    SkipPostPromptStats {
        command_id: String,
    },
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
    SendSecretCommand(SecretCommand),
    ShowConnectionPanel(ConnectionCatalogSnapshot),
    ConnectionCatalogUpdated(ConnectionCatalogSnapshot),
    ShowDeviceCode(DeviceCodeChallenge),
    DeviceCodeProgress(DeviceCodeProgress),
    FinishDeviceCode,
    RestoreDraft {
        content: String,
        local_order: Option<u64>,
    },
    ShowSessionPicker {
        sessions: Vec<SessionSummary>,
        selected_session_id: Option<String>,
    },
    ShowSessionTreePage {
        page: SessionTreePage,
        append: bool,
    },
    CloseSessionTree,
    RestoreSessionDraft(String),
    SendCommittedHydration {
        command: WispTypedClientRpcCommands,
        session_id: String,
    },
    SendPostPromptSessionSync(WispTypedClientRpcCommands),
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
        UiAction::Steer(content) => queue_submission(state, QueueKind::Steering, content, ids),
        UiAction::FollowUp(content) => queue_submission(state, QueueKind::FollowUp, content, ids),
        UiAction::RestoreNewestQueueDraft => restore_newest_queue_draft(state, ids),
        UiAction::RefreshQueueState => refresh_queue_state(ids),
        UiAction::StartupHydration => start_startup_hydration(state, ids),
        UiAction::OpenConnectionPanel => Ok(vec![
            UiEffect::ShowConnectionPanel(state.connection_catalog.clone()),
            UiEffect::RequestRender,
        ]),
        UiAction::LoadConnectionCatalog => load_connection_catalog(state, ids),
        UiAction::StoreApiKey { provider, api_key } => store_api_key(state, provider, api_key, ids),
        UiAction::DisconnectProvider { provider } => disconnect_provider(state, provider, ids),
        UiAction::BeginDeviceCode { provider } => begin_device_code(state, provider, ids),
        UiAction::CancelDeviceCode => cancel_device_code(state, ids),
        UiAction::LoadSessionCatalog => load_session_catalog(state, ids),
        UiAction::SelectSession { session_id } => select_session(state, session_id, ids),
        UiAction::NewSession => new_session(state, ids),
        UiAction::SetSessionName(name) => set_session_name(state, name, ids),
        UiAction::CloneSession => clone_session(state, ids),
        UiAction::ForkSession { entry_id } => fork_session(state, entry_id, ids),
        UiAction::LoadSessionTree { after_entry_id } => {
            load_session_tree(state, after_entry_id, ids)
        }
        UiAction::NavigateSessionTree { entry_id } => navigate_session_tree(state, entry_id, ids),
        UiAction::UnrevertSessionTree => unrevert_session_tree(state, ids),
        UiAction::RejectCommittedHydration { command_id, limit } => {
            reject_committed_hydration(state, &command_id, limit)
        }
        UiAction::RejectPostPromptSessionSync { command_id, limit } => {
            reject_post_prompt_session_sync(state, &command_id, limit)
        }
        UiAction::SkipPostPromptStats { command_id } => {
            skip_post_prompt_stats(state, &command_id, ids)
        }
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
    if session_sync_pending(state) {
        return Ok(vec![
            UiEffect::Notice("Wait for the current session metadata refresh to finish.".into()),
            UiEffect::RequestRender,
        ]);
    }
    if state.session_operation.is_some() {
        return Err(ReduceError::SessionOperationActive);
    }
    if state.history_request.is_some() {
        return Ok(vec![
            UiEffect::Notice("Wait for the current history request to finish.".into()),
            UiEffect::RequestRender,
        ]);
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

fn queue_submission(
    state: &mut UiState,
    kind: QueueKind,
    content: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Err(error) = state.queue_submission_preflight(&content) {
        return Ok(vec![
            UiEffect::Notice(error.notice().into()),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(match kind {
        QueueKind::Steering => CommandKind::Steer,
        QueueKind::FollowUp => CommandKind::FollowUp,
    });
    let command = match kind {
        QueueKind::Steering => WispTypedClientRpcCommands::steer(&id, &content)?,
        QueueKind::FollowUp => WispTypedClientRpcCommands::follow_up(&id, &content)?,
    };
    let local_order = state.next_queue_order;
    state.next_queue_order = state
        .next_queue_order
        .checked_add(1)
        .expect("queue submission order exhausted");
    state.pending_queue_submissions.insert(
        id,
        PendingQueueSubmission {
            kind,
            content,
            local_order,
            observed_queue_update: false,
        },
    );
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn restore_newest_queue_draft(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.pending_queue_restore.is_some() {
        return Ok(vec![
            UiEffect::Notice("A queued item is already being restored.".into()),
            UiEffect::RequestRender,
        ]);
    }
    let Some(kind) = state.queue.newest_kind() else {
        return Ok(vec![
            UiEffect::Notice("No queued steering or follow-up to restore.".into()),
            UiEffect::RequestRender,
        ]);
    };
    let id = ids.next_id(CommandKind::PopQueue);
    let command = WispTypedClientRpcCommands::pop_queue(&id, kind)?;
    state.pending_queue_restore = Some(PendingQueueRestore {
        command_id: id,
        kind,
        removal_received: false,
        removed_content: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn refresh_queue_state(ids: &mut impl CommandIdSource) -> Result<Vec<UiEffect>, ReduceError> {
    Ok(vec![queue_state_effect(ids)?, UiEffect::RequestRender])
}

fn queue_state_effect(ids: &mut impl CommandIdSource) -> Result<UiEffect, ProtocolDecodeError> {
    let id = ids.next_id(CommandKind::GetQueueState);
    Ok(UiEffect::SendCommand(
        WispTypedClientRpcCommands::get_queue_state(&id)?,
    ))
}

fn clear_queue_cache(state: &mut UiState) {
    state.queue = QueueState::default();
    state.pending_queue_submissions.clear();
    state.pending_queue_restore = None;
}

fn begin_session_operation(state: &UiState) -> Result<Option<Vec<UiEffect>>, ReduceError> {
    if session_sync_pending(state) {
        return Ok(Some(vec![
            UiEffect::Notice("Wait for the current session metadata refresh to finish.".into()),
            UiEffect::RequestRender,
        ]));
    }
    if state.history_request.is_some() {
        return Ok(Some(vec![
            UiEffect::Notice("Wait for the current history request to finish.".into()),
            UiEffect::RequestRender,
        ]));
    }
    if state.session_operation.is_some() {
        return Err(ReduceError::SessionOperationActive);
    }
    if let Some(current) = &state.current_command {
        return Err(ReduceError::PromptAlreadyActive(current.id.clone()));
    }
    Ok(None)
}

fn start_startup_hydration(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
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

fn connection_busy() -> Vec<UiEffect> {
    vec![
        UiEffect::Notice("Wait for the current connection request to finish.".into()),
        UiEffect::RequestRender,
    ]
}

fn valid_connection_provider(provider: &str) -> bool {
    !provider.is_empty() && provider.len() <= 128
}

fn connection_provider_error() -> Vec<UiEffect> {
    vec![
        UiEffect::Notice("Provider ID is empty or exceeds the 128-byte limit.".into()),
        UiEffect::RequestRender,
    ]
}

fn start_connection_catalog(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<UiEffect, ProtocolDecodeError> {
    let id = ids.next_id(CommandKind::GetConnectionCatalog);
    let command = WispTypedClientRpcCommands::get_connection_catalog(&id)?;
    state.connection_operation = Some(ConnectionOperation::Catalog {
        command_id: id,
        command_type: "get_connection_catalog",
        report: None,
        completion: None,
    });
    Ok(UiEffect::SendCommand(command))
}

fn load_connection_catalog(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.connection_operation.is_some() {
        return Ok(connection_busy());
    }
    Ok(vec![
        start_connection_catalog(state, ids)?,
        UiEffect::RequestRender,
    ])
}

fn reload_connection_catalog_after_configuration(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.connection_operation.is_some() {
        state.connection_catalog_reload_pending = true;
        return Ok(vec![UiEffect::RequestRender]);
    }
    state.connection_catalog_reload_pending = false;
    let empty = ConnectionCatalogSnapshot {
        providers: Vec::new(),
    };
    state.connection_catalog = empty.clone();
    Ok(vec![
        start_connection_catalog(state, ids)?,
        UiEffect::ConnectionCatalogUpdated(empty),
        UiEffect::RequestRender,
    ])
}

fn store_api_key(
    state: &mut UiState,
    provider: String,
    api_key: ApiKey,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.connection_operation.is_some() {
        return Ok(connection_busy());
    }
    if !valid_connection_provider(&provider) {
        return Ok(connection_provider_error());
    }
    let id = ids.next_id(CommandKind::StoreApiKey);
    let command = WispTypedClientRpcCommands::store_api_key(&id, &provider, api_key.as_str())?;
    drop(api_key);
    state.connection_operation = Some(ConnectionOperation::Catalog {
        command_id: id,
        command_type: "store_api_key",
        report: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendSecretCommand(SecretCommand(command)),
        UiEffect::RequestRender,
    ])
}

fn disconnect_provider(
    state: &mut UiState,
    provider: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.connection_operation.is_some() {
        return Ok(connection_busy());
    }
    if !valid_connection_provider(&provider) {
        return Ok(connection_provider_error());
    }
    let id = ids.next_id(CommandKind::DisconnectProvider);
    let command = WispTypedClientRpcCommands::disconnect_provider(&id, &provider)?;
    state.connection_operation = Some(ConnectionOperation::Catalog {
        command_id: id,
        command_type: "disconnect_provider",
        report: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn begin_device_code(
    state: &mut UiState,
    provider: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.connection_operation.is_some() {
        return Ok(connection_busy());
    }
    if !valid_connection_provider(&provider) {
        return Ok(connection_provider_error());
    }
    let id = ids.next_id(CommandKind::BeginDeviceCode);
    let command = WispTypedClientRpcCommands::begin_device_code(&id, &provider)?;
    state.connection_operation = Some(ConnectionOperation::DeviceCode {
        command_id: id,
        provider,
        report: None,
        challenge: None,
        progress: None,
        completion: None,
        cancel_requested: false,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn cancel_device_code(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    let Some(target_id) =
        state
            .connection_operation
            .as_ref()
            .and_then(|operation| match operation {
                ConnectionOperation::DeviceCode {
                    command_id,
                    cancel_requested: false,
                    ..
                } => Some(command_id.clone()),
                _ => None,
            })
    else {
        return Ok(Vec::new());
    };
    let id = ids.next_id(CommandKind::Cancel);
    let command = WispTypedClientRpcCommands::cancel(&id, &target_id)?;
    if let Some(ConnectionOperation::DeviceCode {
        cancel_requested, ..
    }) = state.connection_operation.as_mut()
    {
        *cancel_requested = true;
    }
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn load_session_catalog(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
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
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
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
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn new_session(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::NewSession);
    let command = WispTypedClientRpcCommands::new_session(&id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::CreatingSession { command_id: id });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn selected_session(state: &UiState) -> Result<&SessionIdentity, Vec<UiEffect>> {
    if session_sync_pending(state) {
        return Err(vec![
            UiEffect::Notice("Wait for the current session metadata refresh to finish.".into()),
            UiEffect::RequestRender,
        ]);
    }
    state.selected_session.as_ref().ok_or_else(|| {
        vec![
            UiEffect::Notice("No persisted session is selected.".into()),
            UiEffect::RequestRender,
        ]
    })
}

fn set_session_name(
    state: &mut UiState,
    name: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Err(effects) = selected_session(state) {
        return Ok(effects);
    }
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::SetSessionName);
    let command = WispTypedClientRpcCommands::set_session_name(&id, &name)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::NamingSession {
        command_id: id,
        changed: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn clone_session(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Err(effects) = selected_session(state) {
        return Ok(effects);
    }
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::CloneSession);
    let command = WispTypedClientRpcCommands::clone_session(&id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::CloningSession {
        command_id: id,
        derived: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn fork_session(
    state: &mut UiState,
    entry_id: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !valid_session_id(&entry_id) {
        return Ok(vec![
            UiEffect::Notice("Tree entry ID is empty or exceeds the 4096-byte limit.".into()),
            UiEffect::RequestRender,
        ]);
    }
    if let Err(effects) = selected_session(state) {
        return Ok(effects);
    }
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::ForkSession);
    let command = WispTypedClientRpcCommands::fork_session(&id, &entry_id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::ForkingSession {
        command_id: id,
        requested_entry_id: entry_id,
        derived: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn load_session_tree(
    state: &mut UiState,
    after_entry_id: Option<String>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if after_entry_id
        .as_deref()
        .is_some_and(|entry_id| !valid_session_id(entry_id))
    {
        return Ok(vec![
            UiEffect::Notice("Tree cursor is empty or exceeds the 4096-byte limit.".into()),
            UiEffect::RequestRender,
        ]);
    }
    if let Err(effects) = selected_session(state) {
        return Ok(effects);
    }
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::GetSessionTree);
    let command = WispTypedClientRpcCommands::get_session_tree(&id, after_entry_id.as_deref())?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::LoadingTree {
        command_id: id,
        after_entry_id,
        page: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn navigate_session_tree(
    state: &mut UiState,
    entry_id: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if !valid_session_id(&entry_id) {
        return Ok(vec![
            UiEffect::Notice("Tree entry ID is empty or exceeds the 4096-byte limit.".into()),
            UiEffect::RequestRender,
        ]);
    }
    if let Err(effects) = selected_session(state) {
        return Ok(effects);
    }
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::NavigateSessionTree);
    let command = WispTypedClientRpcCommands::navigate_session_tree(&id, &entry_id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::NavigatingTree {
        command_id: id,
        requested_entry_id: entry_id,
        navigation: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn unrevert_session_tree(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Err(effects) = selected_session(state) {
        return Ok(effects);
    }
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::UnrevertSessionTree);
    let command = WispTypedClientRpcCommands::unrevert_session_tree(&id)?;
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::UnrevertingTree {
        command_id: id,
        unreverted: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::CloseSessionTree,
        UiEffect::RequestRender,
    ])
}

fn reject_committed_hydration(
    state: &mut UiState,
    command_id: &str,
    limit: usize,
) -> Result<Vec<UiEffect>, ReduceError> {
    let Some(SessionOperation::HydratingSelection {
        command_id: pending,
        selected,
        committed_operation,
        ..
    }) = state.session_operation.as_ref()
    else {
        return Ok(Vec::new());
    };
    if pending != command_id {
        return Ok(Vec::new());
    }
    let session_id = selected.session_id.clone();
    let committed_operation = *committed_operation;
    state.session_operation = None;
    state.input_ready = true;
    Ok(vec![
        UiEffect::Notice(format!(
            "{committed_operation} committed session {session_id}, but its history request exceeds the negotiated {limit}-byte RPC frame limit. Reopen the session after reconnecting with a larger limit."
        )),
        UiEffect::RequestRender,
    ])
}

fn reject_post_prompt_session_sync(
    state: &mut UiState,
    command_id: &str,
    limit: usize,
) -> Result<Vec<UiEffect>, ReduceError> {
    let Some(request) = state.history_request.as_ref() else {
        return Ok(Vec::new());
    };
    if request.command_id != command_id
        || !matches!(request.kind, HistoryRequestKind::PostPromptSync)
    {
        return Ok(Vec::new());
    }
    state.history_request = None;
    state.post_prompt_session_sync_pending = false;
    Ok(vec![
        UiEffect::Notice(format!(
            "Session metadata refresh exceeds the negotiated {limit}-byte RPC frame limit; reopen the session to refresh its active branch."
        )),
        UiEffect::RequestRender,
    ])
}

fn skip_post_prompt_stats(
    state: &mut UiState,
    command_id: &str,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    if state.post_prompt_stats_command_id.as_deref() != Some(command_id) {
        return Ok(Vec::new());
    }
    state.post_prompt_stats_command_id = None;
    Ok(start_post_prompt_session_sync(state, ids)?)
}

fn history_session_id(state: &UiState) -> Option<&str> {
    state
        .history
        .session
        .as_ref()
        .or(state.selected_session.as_ref())
        .map(|session| session.session_id.as_str())
}

fn session_sync_pending(state: &UiState) -> bool {
    state.post_prompt_session_sync_pending
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
    ) || state.transcript.has_live_entries();
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

fn start_post_prompt_session_sync(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.history_request.is_some() {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetMessages);
    let command = WispTypedClientRpcCommands::get_messages(&id, history_session_id(state))?;
    state.history_request = Some(HistoryRequest {
        command_id: id,
        kind: HistoryRequestKind::PostPromptSync,
        active_leaf_may_advance: true,
        report: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SendPostPromptSessionSync(command),
        UiEffect::RequestRender,
    ])
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

fn capture_session_completion(
    event: &BackendEvent,
    command_id: &str,
    command_type: &str,
    completion: &mut Option<SessionCompletion>,
) -> bool {
    let BackendEvent::CommandFinished {
        command_id: received,
        command_type: received_type,
        ok,
        error,
    } = event
    else {
        return false;
    };
    if received != command_id || received_type != command_type {
        return false;
    }
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

fn commit_session_and_hydrate(
    state: &mut UiState,
    selected: SessionIdentity,
    restore_editor_text: Option<String>,
    committed_operation: &'static str,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let id = ids.next_id(CommandKind::GetMessages);
    let command = WispTypedClientRpcCommands::get_messages(&id, Some(&selected.session_id))?;
    state.selected_session = Some(selected.clone());
    state.last_session = Some(selected.session_id.clone());
    state.transcript = SharedTranscript::default();
    state.history = HistoryWindow::default();
    clear_queue_cache(state);
    state.history_request = None;
    state.session_operation = Some(SessionOperation::HydratingSelection {
        command_id: id,
        selected: selected.clone(),
        restore_editor_text,
        committed_operation,
        report: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::ReplaceTranscript,
        UiEffect::SendCommittedHydration {
            command,
            session_id: selected.session_id,
        },
        queue_state_effect(ids)?,
        UiEffect::RequestRender,
    ])
}

fn handle_session_backend_event(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    let Some(mut operation) = state.session_operation.take() else {
        return Ok(None);
    };

    match (&operation, event) {
        (
            SessionOperation::HydratingSelection {
                command_id,
                selected,
                committed_operation,
                ..
            },
            BackendEvent::MessagesProjectionFailed {
                command_id: received,
                error,
            },
        ) if received == command_id => {
            return Ok(Some(committed_hydration_failure(
                state,
                selected,
                committed_operation,
                error.clone(),
            )));
        }
        _ => {}
    }

    let projection_failure = match (&operation, event) {
        (
            SessionOperation::StartupHydration { command_id, .. },
            BackendEvent::MessagesProjectionFailed {
                command_id: received,
                error,
            },
        ) if received == command_id => Some(("startup history", error.clone())),
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
            _ => capture_session_completion(event, command_id, "get_messages", completion),
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
            _ => capture_session_completion(event, command_id, "get_sessions", completion),
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
                if session.session_id != *requested_session_id {
                    return Ok(Some(session_failure(
                        state,
                        "session selection",
                        Some("backend returned another session".into()),
                    )));
                }
                if selected.is_none() {
                    *selected = Some(session.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "select_session", completion),
        },
        SessionOperation::HydratingSelection {
            command_id,
            selected,
            report,
            completion,
            committed_operation,
            ..
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
                        return Ok(Some(committed_hydration_failure(
                            state,
                            selected,
                            committed_operation,
                            "backend returned another session".into(),
                        )));
                    }
                    *report = Some(messages.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "get_messages", completion),
        },
        SessionOperation::NamingSession {
            command_id,
            changed,
            completion,
        } => match event {
            BackendEvent::SessionNameChanged {
                command_id: received,
                changed: received_change,
            } if received == command_id => {
                if !state
                    .selected_session
                    .as_ref()
                    .is_some_and(|selected| same_session(selected, &received_change.session))
                {
                    return Ok(Some(session_failure(
                        state,
                        "session naming",
                        Some("backend returned another session".into()),
                    )));
                }
                if changed.is_none() {
                    *changed = Some(received_change.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "set_session_name", completion),
        },
        SessionOperation::CloningSession {
            command_id,
            derived,
            completion,
        } => match event {
            BackendEvent::SessionCloned {
                command_id: received,
                derived: received_derivation,
            } if received == command_id => {
                if !state
                    .selected_session
                    .as_ref()
                    .is_some_and(|selected| same_session(selected, &received_derivation.source))
                    || received_derivation.source_active_leaf_id != state.history.active_leaf_id
                {
                    return Ok(Some(session_failure(
                        state,
                        "session clone",
                        Some("backend returned another source session".into()),
                    )));
                }
                if derived.is_none() {
                    *derived = Some(received_derivation.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "clone_session", completion),
        },
        SessionOperation::ForkingSession {
            command_id,
            requested_entry_id,
            derived,
            completion,
        } => match event {
            BackendEvent::SessionForked {
                command_id: received,
                derived: received_derivation,
            } if received == command_id => {
                if !state
                    .selected_session
                    .as_ref()
                    .is_some_and(|selected| same_session(selected, &received_derivation.source))
                    || received_derivation.source_active_leaf_id != state.history.active_leaf_id
                    || received_derivation.selected_entry_id.as_ref() != Some(requested_entry_id)
                {
                    return Ok(Some(session_failure(
                        state,
                        "session fork",
                        Some("backend returned another session or tree entry".into()),
                    )));
                }
                if derived.is_none() {
                    *derived = Some(received_derivation.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "fork_session", completion),
        },
        SessionOperation::LoadingTree {
            command_id,
            after_entry_id,
            page,
            completion,
        } => match event {
            BackendEvent::SessionTreeReported {
                command_id: received,
                page: received_page,
            } if received == command_id => {
                let requested_session = state.selected_session.as_ref();
                let invalid_scope = !matches!(
                    (requested_session, received_page.session.as_ref()),
                    (Some(selected), Some(received)) if same_session(selected, received)
                );
                let invalid_cursor = after_entry_id.as_ref().is_some_and(|cursor| {
                    received_page
                        .nodes
                        .iter()
                        .any(|node| &node.entry_id == cursor)
                        || received_page.next_after_entry_id.as_ref() == Some(cursor)
                });
                if invalid_scope
                    || received_page.active_leaf_id != state.history.active_leaf_id
                    || invalid_cursor
                {
                    return Ok(Some(session_failure(
                        state,
                        "session tree",
                        Some("backend returned a stale or malformed page".into()),
                    )));
                }
                if page.is_none() {
                    *page = Some(received_page.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "get_session_tree", completion),
        },
        SessionOperation::NavigatingTree {
            command_id,
            requested_entry_id,
            navigation,
            completion,
        } => match event {
            BackendEvent::SessionTreeNavigated {
                command_id: received,
                navigation: received_navigation,
            } if received == command_id => {
                if received_navigation.selected_entry_id != *requested_entry_id
                    || !state.selected_session.as_ref().is_some_and(|selected| {
                        same_session(selected, &received_navigation.session)
                    })
                    || received_navigation.previous_active_leaf_id != state.history.active_leaf_id
                {
                    return Ok(Some(session_failure(
                        state,
                        "session tree navigation",
                        Some("backend returned another session, entry, or active leaf".into()),
                    )));
                }
                if navigation.is_none() {
                    *navigation = Some(received_navigation.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "navigate_session_tree", completion),
        },
        SessionOperation::UnrevertingTree {
            command_id,
            unreverted,
            completion,
        } => match event {
            BackendEvent::SessionTreeUnreverted {
                command_id: received,
                unreverted: received_unrevert,
            } if received == command_id => {
                if !state
                    .selected_session
                    .as_ref()
                    .is_some_and(|selected| same_session(selected, &received_unrevert.session))
                    || received_unrevert.previous_active_leaf_id != state.history.active_leaf_id
                {
                    return Ok(Some(session_failure(
                        state,
                        "session tree unrevert",
                        Some("backend returned another session or active leaf".into()),
                    )));
                }
                if unreverted.is_none() {
                    *unreverted = Some(received_unrevert.clone());
                }
                true
            }
            _ => capture_session_completion(event, command_id, "unrevert_session_tree", completion),
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
            clear_queue_cache(state);
            install_history_snapshot(state, report);
            state.input_ready = true;
            vec![
                UiEffect::ReplaceTranscript,
                queue_state_effect(ids)?,
                UiEffect::RequestRender,
            ]
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
        } => commit_session_and_hydrate(state, selected, None, "Session selection", ids)?,
        SessionOperation::SelectingSession { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::HydratingSelection {
            completion: Some(completion),
            selected,
            committed_operation,
            ..
        } if !completion.ok => session_failure(
            state,
            "session history hydration",
            Some(format!(
                "{committed_operation} committed session {}, but history loading failed: {}. Use /resume {} to retry",
                selected.session_id,
                completion
                    .error
                    .unwrap_or_else(|| "backend reported failure".into()),
                selected.session_id,
            )),
        ),
        SessionOperation::HydratingSelection {
            report: Some(mut report),
            completion: Some(_),
            restore_editor_text,
            selected,
            ..
        } => {
            if let Some(history_session) = report
                .session
                .as_mut()
                .filter(|history_session| same_session(history_session, &selected))
            {
                history_session.session_name = selected.session_name;
            }
            install_history_snapshot(state, report);
            state.input_ready = true;
            let mut effects = vec![UiEffect::ReplaceTranscript];
            if let Some(text) = restore_editor_text {
                effects.push(UiEffect::RestoreSessionDraft(text));
            }
            effects.push(UiEffect::RequestRender);
            effects
        }
        SessionOperation::HydratingSelection { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::NamingSession {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session naming", completion.error),
        SessionOperation::NamingSession {
            changed: Some(changed),
            completion: Some(_),
            ..
        } => {
            if let Some(selected) = state.selected_session.as_mut() {
                selected.session_name = changed.session.session_name.clone();
            }
            if let Some(history) = state
                .history
                .session
                .as_mut()
                .filter(|history| same_session(history, &changed.session))
            {
                history.session_name = changed.session.session_name;
            }
            state.input_ready = true;
            vec![UiEffect::RequestRender]
        }
        SessionOperation::NamingSession { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::CloningSession {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session clone", completion.error),
        SessionOperation::CloningSession {
            derived: Some(derived),
            completion: Some(_),
            ..
        } => commit_session_and_hydrate(state, derived.session, None, "Session clone", ids)?,
        SessionOperation::CloningSession { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::ForkingSession {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session fork", completion.error),
        SessionOperation::ForkingSession {
            derived: Some(derived),
            completion: Some(_),
            ..
        } => commit_session_and_hydrate(
            state,
            derived.session,
            derived.selected_prompt,
            "Session fork",
            ids,
        )?,
        SessionOperation::ForkingSession { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::LoadingTree {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session tree", completion.error),
        SessionOperation::LoadingTree {
            after_entry_id,
            page: Some(page),
            completion: Some(_),
            ..
        } => {
            state.input_ready = true;
            vec![
                UiEffect::ShowSessionTreePage {
                    page,
                    append: after_entry_id.is_some(),
                },
                UiEffect::RequestRender,
            ]
        }
        SessionOperation::LoadingTree { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::NavigatingTree {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session tree navigation", completion.error),
        SessionOperation::NavigatingTree {
            navigation: Some(navigation),
            completion: Some(_),
            ..
        } if !navigation.changed => {
            state.input_ready = true;
            let mut effects = Vec::new();
            if let Some(text) = navigation.editor_text {
                effects.push(UiEffect::RestoreSessionDraft(text));
            }
            effects.push(UiEffect::RequestRender);
            effects
        }
        SessionOperation::NavigatingTree {
            navigation: Some(navigation),
            completion: Some(_),
            ..
        } => {
            let mut selected = navigation.session;
            selected.session_name = state
                .selected_session
                .as_ref()
                .and_then(|session| session.session_name.clone());
            commit_session_and_hydrate(
                state,
                selected,
                navigation.editor_text,
                "Session tree navigation",
                ids,
            )?
        }
        SessionOperation::NavigatingTree { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::UnrevertingTree {
            completion: Some(completion),
            ..
        } if !completion.ok => session_failure(state, "session tree unrevert", completion.error),
        SessionOperation::UnrevertingTree {
            unreverted: Some(unreverted),
            completion: Some(_),
            ..
        } => {
            let mut selected = unreverted.session;
            selected.session_name = state
                .selected_session
                .as_ref()
                .and_then(|session| session.session_name.clone());
            commit_session_and_hydrate(state, selected, None, "Session tree unrevert", ids)?
        }
        SessionOperation::UnrevertingTree { .. } => {
            state.session_operation = Some(operation);
            return Ok(Some(Vec::new()));
        }
        SessionOperation::CreatingSession { command_id } => match event {
            BackendEvent::CommandFinished { ok: true, .. } => {
                state.transcript = SharedTranscript::default();
                state.selected_session = None;
                state.last_session = None;
                state.history = HistoryWindow::default();
                clear_queue_cache(state);
                state.history_request = None;
                state.input_ready = true;
                vec![
                    UiEffect::ReplaceTranscript,
                    queue_state_effect(ids)?,
                    UiEffect::RequestRender,
                ]
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

fn committed_hydration_failure(
    state: &mut UiState,
    selected: &SessionIdentity,
    committed_operation: &str,
    error: String,
) -> Vec<UiEffect> {
    session_failure(
        state,
        "session history hydration",
        Some(format!(
            "{committed_operation} committed session {}, but history loading failed: {error}. Use /resume {} to retry",
            selected.session_id, selected.session_id,
        )),
    )
}

fn install_history_snapshot(state: &mut UiState, report: SessionMessages) {
    state.transcript = report.transcript;
    let prefix_evicted = !state
        .transcript
        .retain_historical_entries_in_order(
            TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT,
            false,
            &report.durable_entry_ids,
        )
        .unwrap_or_default()
        .is_empty();
    let represented_durable_entry_ids = state.transcript.represented_durable_entry_ids();
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
    let retained = state.transcript.represented_durable_entry_ids();
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

fn finish_history_request_failure(
    state: &mut UiState,
    kind: &HistoryRequestKind,
    error: String,
) -> Vec<UiEffect> {
    if matches!(kind, HistoryRequestKind::PostPromptSync) {
        state.post_prompt_session_sync_pending = false;
    }
    history_request_failure(error)
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
                return Some(finish_history_request_failure(
                    state,
                    &request.kind,
                    "duplicate history report".into(),
                ));
            }
            request.report = Some(messages.clone());
            true
        }
        BackendEvent::MessagesProjectionFailed { command_id, error }
            if command_id == &request.command_id =>
        {
            return Some(finish_history_request_failure(
                state,
                &request.kind,
                error.clone(),
            ));
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_id == &request.command_id && command_type == "get_messages" => {
            if !ok {
                return Some(finish_history_request_failure(
                    state,
                    &request.kind,
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
        HistoryRequestKind::PostPromptSync => {
            state.post_prompt_session_sync_pending = false;
            let Some(mut refreshed) = report.session else {
                return Some(history_request_failure(
                    "completed prompt did not report its persisted session".into(),
                ));
            };
            let existing = state
                .selected_session
                .as_ref()
                .or(state.history.session.as_ref());
            if existing.is_some_and(|selected| !same_session(selected, &refreshed)) {
                return Some(history_request_failure(
                    "completed prompt reported another persisted session".into(),
                ));
            }
            refreshed.session_name = existing.and_then(|session| session.session_name.clone());
            state.last_session = Some(refreshed.session_id.clone());
            state.selected_session = Some(refreshed.clone());
            state.history.session = Some(refreshed);
            state.history.active_leaf_id = report.active_leaf_id;
            Some(vec![UiEffect::RequestRender])
        }
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
                .retain_historical_entries_in_order(
                    TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT,
                    true,
                    &state.history.represented_durable_entry_order,
                )
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
                .retain_historical_entries_in_order(
                    TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT,
                    false,
                    &state.history.represented_durable_entry_order,
                )
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

fn apply_queue_update(state: &mut UiState, steering: Vec<String>, follow_up: Vec<String>) {
    let mut new_steering = state.queue.replace(QueueKind::Steering, steering);
    let mut new_follow_up = state.queue.replace(QueueKind::FollowUp, follow_up);
    let mut pending = state
        .pending_queue_submissions
        .iter()
        .filter(|(_, pending)| !pending.observed_queue_update)
        .map(|(command_id, pending)| {
            (
                command_id.clone(),
                pending.kind,
                pending.content.clone(),
                pending.local_order,
            )
        })
        .collect::<Vec<_>>();
    pending.sort_by_key(|(_, _, _, local_order)| *local_order);

    for (command_id, kind, content, local_order) in pending {
        let candidates = match kind {
            QueueKind::Steering => &mut new_steering,
            QueueKind::FollowUp => &mut new_follow_up,
        };
        let Some(index) = candidates
            .iter()
            .position(|identity| state.queue.item_matches(kind, *identity, &content))
        else {
            continue;
        };
        let identity = candidates.remove(index);
        state.queue.assign_local_order(kind, identity, local_order);
        if let Some(pending) = state.pending_queue_submissions.get_mut(&command_id) {
            pending.observed_queue_update = true;
        }
    }
}

fn finish_pending_queue_restore(state: &mut UiState) -> Vec<UiEffect> {
    let Some(pending) = state.pending_queue_restore.as_ref() else {
        return Vec::new();
    };
    if pending.completion == Some(false) {
        state.pending_queue_restore = None;
        return Vec::new();
    }
    if pending.completion != Some(true) || !pending.removal_received {
        return Vec::new();
    }
    let pending = state
        .pending_queue_restore
        .take()
        .expect("checked pending queue restore");
    pending
        .removed_content
        .map(|content| UiEffect::RestoreDraft {
            content,
            local_order: None,
        })
        .into_iter()
        .collect::<Vec<_>>()
}

fn handle_queue_items_removed(
    state: &mut UiState,
    command_id: String,
    operation: QueueRemovalOperation,
    kind: Option<QueueKind>,
    steering: Vec<String>,
    follow_up: Vec<String>,
) -> Vec<UiEffect> {
    let pending_kind = {
        let Some(pending) = state.pending_queue_restore.as_ref() else {
            return Vec::new();
        };
        if pending.command_id != command_id
            || operation != QueueRemovalOperation::Pop
            || kind != Some(pending.kind)
        {
            return Vec::new();
        }
        pending.kind
    };
    let removed_content = match pending_kind {
        QueueKind::Steering => steering.into_iter().next(),
        QueueKind::FollowUp => follow_up.into_iter().next(),
    };
    if let Some(content) = removed_content.as_deref() {
        state.queue.remove_last(pending_kind, content);
    }
    let pending = state
        .pending_queue_restore
        .as_mut()
        .expect("checked pending queue restore");
    pending.removal_received = true;
    pending.removed_content = removed_content;
    finish_pending_queue_restore(state)
}

fn handle_queue_command_finished(
    state: &mut UiState,
    command_id: String,
    command_type: String,
    ok: bool,
) -> Option<Vec<UiEffect>> {
    if let Some(pending) = state.pending_queue_submissions.remove(&command_id) {
        let expected_command_type = match pending.kind {
            QueueKind::Steering => "steer",
            QueueKind::FollowUp => "follow_up",
        };
        if expected_command_type != command_type {
            state.pending_queue_submissions.insert(command_id, pending);
            return None;
        }
        return Some(if ok {
            Vec::new()
        } else {
            vec![UiEffect::RestoreDraft {
                content: pending.content,
                local_order: Some(pending.local_order),
            }]
        });
    }
    let pending = state.pending_queue_restore.as_mut()?;
    if pending.command_id != command_id || command_type != "pop_queue" {
        return None;
    }
    pending.completion = Some(ok);
    Some(finish_pending_queue_restore(state))
}

fn connection_failure(operation: &str, error: Option<String>) -> Vec<UiEffect> {
    let detail = error.unwrap_or_else(|| "backend reported failure".into());
    vec![
        UiEffect::Notice(bounded_session_text(
            &format!("{operation} failed: {detail}"),
            SESSION_NOTICE_MAX_BYTES,
        )),
        UiEffect::RequestRender,
    ]
}

fn adopt_connection_catalog(
    state: &mut UiState,
    catalog: ConnectionCatalogSnapshot,
) -> Vec<UiEffect> {
    state.connection_catalog = catalog;
    vec![
        UiEffect::ConnectionCatalogUpdated(state.connection_catalog.clone()),
        UiEffect::RequestRender,
    ]
}

fn handle_connection_backend_event(
    state: &mut UiState,
    event: &BackendEvent,
) -> Option<Vec<UiEffect>> {
    let operation = state.connection_operation.as_ref()?;
    let belongs_to_operation = match (operation, event) {
        (
            ConnectionOperation::Catalog { command_id, .. }
            | ConnectionOperation::DeviceCode { command_id, .. },
            BackendEvent::ConnectionCatalogReported {
                command_id: received,
                ..
            }
            | BackendEvent::DeviceCodeReported {
                command_id: received,
                ..
            }
            | BackendEvent::DeviceCodeProgress {
                command_id: received,
                ..
            },
        ) => received == command_id,
        (
            ConnectionOperation::Catalog {
                command_id,
                command_type,
                ..
            },
            BackendEvent::CommandFinished {
                command_id: received,
                command_type: received_type,
                ..
            },
        ) => received == command_id && received_type == command_type,
        (
            ConnectionOperation::DeviceCode { command_id, .. },
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ..
            },
        ) => received == command_id && command_type == "begin_device_code",
        _ => false,
    };
    if !belongs_to_operation {
        return None;
    }
    let mut operation = state.connection_operation.take().expect("checked above");
    let mut effects = Vec::new();
    match &mut operation {
        ConnectionOperation::Catalog {
            command_id,
            command_type,
            report,
            completion,
        } => match event {
            BackendEvent::ConnectionCatalogReported {
                command_id: received,
                catalog,
            } if received == command_id && report.is_none() => *report = Some(catalog.clone()),
            BackendEvent::CommandFinished {
                command_id: received,
                command_type: received_type,
                ok,
                error,
            } if received == command_id
                && received_type == command_type
                && completion.is_none() =>
            {
                *completion = Some(SessionCompletion {
                    ok: *ok,
                    error: error.clone(),
                });
            }
            _ => {}
        },
        ConnectionOperation::DeviceCode {
            command_id,
            provider,
            report,
            challenge,
            progress,
            completion,
            ..
        } => match event {
            BackendEvent::ConnectionCatalogReported {
                command_id: received,
                catalog,
            } if received == command_id && report.is_none() => *report = Some(catalog.clone()),
            BackendEvent::DeviceCodeReported {
                command_id: received,
                challenge: received_challenge,
            } if received == command_id
                && received_challenge.provider == *provider
                && challenge.is_none() =>
            {
                *challenge = Some(received_challenge.clone());
                effects.push(UiEffect::ShowDeviceCode(received_challenge.clone()));
                effects.push(UiEffect::RequestRender);
            }
            BackendEvent::DeviceCodeProgress {
                command_id: received,
                progress: received_progress,
            } if received == command_id
                && received_progress.provider == *provider
                && progress
                    .as_ref()
                    .is_none_or(|previous| received_progress.attempt > previous.attempt) =>
            {
                *progress = Some(received_progress.clone());
                effects.push(UiEffect::DeviceCodeProgress(received_progress.clone()));
                effects.push(UiEffect::RequestRender);
            }
            BackendEvent::CommandFinished {
                command_id: received,
                command_type,
                ok,
                error,
            } if received == command_id
                && command_type == "begin_device_code"
                && completion.is_none() =>
            {
                *completion = Some(SessionCompletion {
                    ok: *ok,
                    error: error.clone(),
                });
            }
            _ => {}
        },
    }

    match operation {
        ConnectionOperation::Catalog {
            completion: Some(SessionCompletion { ok: false, error }),
            ..
        } => Some(connection_failure("Connection request", error)),
        ConnectionOperation::Catalog {
            report: Some(report),
            completion: Some(SessionCompletion { ok: true, .. }),
            ..
        } => {
            effects.extend(adopt_connection_catalog(state, report));
            Some(effects)
        }
        ConnectionOperation::Catalog {
            command_type: "store_api_key" | "disconnect_provider",
            completion: Some(SessionCompletion { ok: true, .. }),
            ..
        } => {
            effects.push(UiEffect::Notice(
                "Credentials updated, but connection status could not be refreshed.".into(),
            ));
            effects.push(UiEffect::RequestRender);
            Some(effects)
        }
        ConnectionOperation::DeviceCode {
            completion: Some(SessionCompletion { ok: false, error }),
            cancel_requested,
            ..
        } => {
            effects.push(UiEffect::FinishDeviceCode);
            if cancel_requested {
                effects.push(UiEffect::Notice("Device login cancelled.".into()));
                effects.push(UiEffect::RequestRender);
            } else {
                effects.extend(connection_failure("Device login", error));
            }
            Some(effects)
        }
        ConnectionOperation::DeviceCode {
            provider,
            report: Some(report),
            completion: Some(SessionCompletion { ok: true, .. }),
            ..
        } => {
            effects.extend(adopt_connection_catalog(state, report));
            effects.push(UiEffect::FinishDeviceCode);
            effects.push(UiEffect::Notice(bounded_session_text(
                &format!("Connected: {provider}"),
                SESSION_NOTICE_MAX_BYTES,
            )));
            effects.push(UiEffect::RequestRender);
            Some(effects)
        }
        ConnectionOperation::DeviceCode {
            provider,
            completion: Some(SessionCompletion { ok: true, .. }),
            ..
        } => {
            effects.push(UiEffect::FinishDeviceCode);
            effects.push(UiEffect::Notice(bounded_session_text(
                &format!("Connected: {provider}. Connection status could not be refreshed."),
                SESSION_NOTICE_MAX_BYTES,
            )));
            effects.push(UiEffect::RequestRender);
            Some(effects)
        }
        operation => {
            state.connection_operation = Some(operation);
            Some(effects)
        }
    }
}

fn handle_backend_event(
    state: &mut UiState,
    event: BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if let Some(mut effects) = handle_history_backend_event(state, &event) {
        if state.post_prompt_session_sync_pending
            && state.post_prompt_stats_command_id.is_none()
            && state.history_request.is_none()
        {
            effects.extend(start_post_prompt_session_sync(state, ids)?);
        }
        return Ok(effects);
    }
    if let Some(effects) = handle_session_backend_event(state, &event, ids)? {
        return Ok(effects);
    }
    if let Some(mut effects) = handle_connection_backend_event(state, &event) {
        if state.connection_operation.is_none() && state.connection_catalog_reload_pending {
            effects.extend(reload_connection_catalog_after_configuration(state, ids)?);
        }
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
            reload_connection_catalog_after_configuration(state, ids)
        }
        BackendEvent::QueueUpdated {
            steering,
            follow_up,
        } => {
            apply_queue_update(state, steering, follow_up);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::QueueItemsRemoved {
            command_id,
            operation,
            kind,
            steering,
            follow_up,
        } => Ok(handle_queue_items_removed(
            state, command_id, operation, kind, steering, follow_up,
        )),
        BackendEvent::QueueMessageInjected { kind, content } => {
            state.transcript.append_prompt(content.clone());
            state.queue.remove_first(kind, &content);
            Ok(vec![UiEffect::RequestRender])
        }
        BackendEvent::ConnectionCatalogReported { .. }
        | BackendEvent::DeviceCodeReported { .. }
        | BackendEvent::DeviceCodeProgress { .. }
        | BackendEvent::SessionsReported { .. }
        | BackendEvent::SessionSelected { .. }
        | BackendEvent::SessionNameChanged { .. }
        | BackendEvent::SessionCloned { .. }
        | BackendEvent::SessionForked { .. }
        | BackendEvent::SessionTreeReported { .. }
        | BackendEvent::SessionTreeNavigated { .. }
        | BackendEvent::SessionTreeUnreverted { .. }
        | BackendEvent::MessagesReported { .. }
        | BackendEvent::MessagesProjectionFailed { .. } => Ok(Vec::new()),
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } => {
            if state.post_prompt_stats_command_id.as_deref() == Some(command_id.as_str())
                && command_type == "get_session_stats"
            {
                state.post_prompt_stats_command_id = None;
                return start_post_prompt_session_sync(state, ids);
            }
            if let Some(effects) =
                handle_queue_command_finished(state, command_id.clone(), command_type.clone(), ok)
            {
                return Ok(effects);
            }
            let matches_current = state.current_command.as_ref().is_some_and(|current| {
                current.id == command_id && current.command_type.as_str() == command_type
            });
            if !matches_current {
                return Ok(Vec::new());
            }
            let stats_id = ids.next_id(CommandKind::GetSessionStats);
            let stats = WispTypedClientRpcCommands::get_session_stats(&stats_id)?;
            state.post_prompt_session_sync_pending = true;
            state.post_prompt_stats_command_id = Some(stats_id);
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
            UiEffect::SendCommittedHydration { command, .. } => Some(command.to_value().unwrap()),
            UiEffect::SendPostPromptSessionSync(command) => Some(command.to_value().unwrap()),
            UiEffect::SendSecretCommand(_)
            | UiEffect::ShowConnectionPanel(_)
            | UiEffect::ConnectionCatalogUpdated(_)
            | UiEffect::ShowDeviceCode(_)
            | UiEffect::DeviceCodeProgress(_)
            | UiEffect::FinishDeviceCode
            | UiEffect::RestoreDraft { .. }
            | UiEffect::ShowSessionPicker { .. }
            | UiEffect::ShowSessionTreePage { .. }
            | UiEffect::CloseSessionTree
            | UiEffect::RestoreSessionDraft(_)
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
    fn prompt_completion_syncs_the_selected_session_and_active_leaf_in_either_order() {
        for result_first in [true, false] {
            let selected = session("active");
            let mut state = UiState::new("fake".into(), None, None);
            state.selected_session = Some(selected.clone());
            state.history.session = Some(selected.clone());
            state.history.active_leaf_id = Some("old-leaf".into());
            state.current_command = Some(ActiveCommand {
                id: "prompt-1".into(),
                command_type: ActiveCommandType::Prompt,
            });
            let mut ids = DeterministicIds::default();

            let effects = reduce(
                &mut state,
                UiAction::BackendEvent(finished("prompt-1", "prompt", true)),
                &mut ids,
            )
            .unwrap();
            assert_eq!(
                command_value(&effects[0]).unwrap()["type"],
                "get_session_stats"
            );
            assert!(state.post_prompt_session_sync_pending);

            let blocked = reduce(&mut state, UiAction::CloneSession, &mut ids).unwrap();
            assert!(matches!(
                blocked.as_slice(),
                [UiEffect::Notice(message), UiEffect::RequestRender]
                    if message.contains("metadata refresh")
            ));

            let effects = reduce(
                &mut state,
                UiAction::BackendEvent(finished("get_session_stats-1", "get_session_stats", true)),
                &mut ids,
            )
            .unwrap();
            assert_eq!(command_value(&effects[0]).unwrap()["type"], "get_messages");

            let report = BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(SessionIdentity {
                        session_name: None,
                        ..selected.clone()
                    }),
                    active_leaf_id: Some("new-leaf".into()),
                    truncated: false,
                    next_before_entry_id: None,
                    next_after_entry_id: None,
                    durable_entry_ids: Vec::new(),
                    exact_tool_result: None,
                    transcript: SharedTranscript::default(),
                },
            };
            let completion = finished("get_messages-1", "get_messages", true);
            let ordered = if result_first {
                [report, completion]
            } else {
                [completion, report]
            };
            for event in ordered {
                reduce(&mut state, UiAction::BackendEvent(event), &mut ids).unwrap();
            }

            assert!(!state.post_prompt_session_sync_pending);
            assert_eq!(state.selected_session, Some(selected.clone()));
            assert_eq!(state.history.session, Some(selected));
            assert_eq!(state.history.active_leaf_id.as_deref(), Some("new-leaf"));
            let effects = reduce(&mut state, UiAction::CloneSession, &mut ids).unwrap();
            assert_eq!(command_value(&effects[0]).unwrap()["type"], "clone_session");
        }
    }

    #[test]
    fn first_prompt_adopts_the_backend_created_session() {
        let mut state = UiState::new("fake".into(), None, None);
        state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        let mut ids = DeterministicIds::default();

        reduce(
            &mut state,
            UiAction::BackendEvent(finished("prompt-1", "prompt", true)),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_session_stats-1", "get_session_stats", true)),
            &mut ids,
        )
        .unwrap();
        let created = SessionIdentity {
            session_id: "created".into(),
            session_path: "/sessions/created.jsonl".into(),
            session_name: None,
        };
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(created.clone()),
                    active_leaf_id: Some("created-leaf".into()),
                    truncated: false,
                    next_before_entry_id: None,
                    next_after_entry_id: None,
                    durable_entry_ids: Vec::new(),
                    exact_tool_result: None,
                    transcript: SharedTranscript::default(),
                },
            }),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.selected_session, Some(created.clone()));
        assert_eq!(state.history.session, Some(created));
        assert_eq!(
            state.history.active_leaf_id.as_deref(),
            Some("created-leaf")
        );
        assert_eq!(state.last_session.as_deref(), Some("created"));
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
            "schema_version": 36,
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
    fn connection_events_project_sanitized_catalog_and_monotonic_progress() {
        let catalog = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "rpc.connection_catalog",
            "command_id": "connection-1",
            "catalog": {"providers": [{
                "id": "openai",
                "label": "OpenAI",
                "methods": [{
                    "provider": "openai",
                    "label": "API key",
                    "kind": "api_key",
                    "source": "environment",
                    "environment_variable": "OPENAI_API_KEY",
                    "oauth_expires_at": null,
                    "has_stored_credential": false
                }]
            }]}
        }))
        .unwrap();
        assert!(matches!(
            catalog,
            BackendEvent::ConnectionCatalogReported { command_id, ref catalog }
            if command_id == "connection-1" && catalog.providers[0].methods[0].environment_variable.as_deref() == Some("OPENAI_API_KEY")
        ));
        let live = wisp_protocol::events::deserialize(serde_json::json!({
            "type": "rpc.device_code.progress",
            "schema_version": 36,
            "timestamp": "2026-01-01T00:00:00Z",
            "command_id": "device-1",
            "provider": "openai-codex",
            "attempt": 2
        }))
        .unwrap();
        assert!(matches!(
            BackendEvent::from_live(&live).unwrap(),
            BackendEvent::DeviceCodeProgress { command_id, progress }
            if command_id == "device-1" && progress.attempt == 2
        ));
        assert!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.device_code.progress",
                "command_id": "device-1",
                "provider": "openai-codex",
                "attempt": 10_001
            }))
            .is_err()
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
    fn session_workflow_event_projection_is_bounded_and_rejects_duplicate_tree_nodes() {
        let cloned = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "rpc.session.cloned",
            "command_id": "clone-1",
            "source_session_id": "source",
            "source_session_path": "/sessions/source.jsonl",
            "source_session_name": "Source",
            "source_active_leaf_id": "entry-1",
            "session_id": "clone",
            "session_path": "/sessions/clone.jsonl",
            "session_name": "Clone",
            "active_leaf_id": "entry-1",
            "entry_count": 1
        }))
        .unwrap();
        assert!(matches!(
            cloned,
            BackendEvent::SessionCloned { derived, .. }
                if derived.source.session_id == "source"
                    && derived.session.session_name.as_deref() == Some("Clone")
        ));

        let node = serde_json::json!({
            "entry_id": "entry-1",
            "parent_id": null,
            "operation_id": "prompt-1",
            "created_at": "2026-01-02T03:04:05Z",
            "kind": "message",
            "role": "user",
            "preview": "x".repeat(SESSION_LABEL_MAX_BYTES + 20),
            "preview_truncated": false
        });
        let page = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "rpc.session.tree",
            "command_id": "tree-1",
            "session_id": "source",
            "session_path": "/sessions/source.jsonl",
            "active_leaf_id": "entry-1",
            "total_node_count": 1,
            "nodes": [node.clone()],
            "truncated": false,
            "next_after_entry_id": null
        }))
        .unwrap();
        assert!(matches!(
            page,
            BackendEvent::SessionTreeReported { page, .. }
                if page.nodes[0].preview.len() <= SESSION_LABEL_MAX_BYTES
                    && page.nodes[0].preview_truncated
        ));
        assert!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "rpc.session.tree",
                "command_id": "tree-2",
                "session_id": "source",
                "session_path": "/sessions/source.jsonl",
                "active_leaf_id": "entry-1",
                "total_node_count": 2,
                "nodes": [node.clone(), node],
                "truncated": false,
                "next_after_entry_id": null
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
            "schema_version": 36,
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
            "schema_version": 36,
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
        assert!(state.connection_catalog.providers.is_empty());
        assert!(matches!(
            effects.as_slice(),
            [
                UiEffect::SendCommand(_),
                UiEffect::ConnectionCatalogUpdated(_),
                UiEffect::RequestRender
            ]
        ));
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

    fn derivation(source: &str, target: &str) -> SessionDerivation {
        SessionDerivation {
            source: session(source),
            source_active_leaf_id: Some("source-leaf".into()),
            session: session(target),
            active_leaf_id: Some("target-leaf".into()),
            entry_count: 2,
            selected_entry_id: None,
            selected_prompt: None,
        }
    }

    #[test]
    fn clone_waits_for_both_events_in_either_order_then_hydrates_the_clone() {
        for finish_first in [false, true] {
            let mut state = UiState::unconfigured();
            state.selected_session = Some(session("source"));
            state.history.session = Some(session("source"));
            state.history.active_leaf_id = Some("source-leaf".into());
            state.transcript.append_prompt("source transcript".into());
            let mut ids = DeterministicIds::default();
            reduce(&mut state, UiAction::CloneSession, &mut ids).unwrap();
            let result = BackendEvent::SessionCloned {
                command_id: "clone_session-1".into(),
                derived: derivation("source", "clone"),
            };
            let finish = finished("clone_session-1", "clone_session", true);
            let events = if finish_first {
                [finish, result]
            } else {
                [result, finish]
            };
            assert!(
                reduce(
                    &mut state,
                    UiAction::BackendEvent(events[0].clone()),
                    &mut ids
                )
                .unwrap()
                .is_empty()
            );
            let effects = reduce(
                &mut state,
                UiAction::BackendEvent(events[1].clone()),
                &mut ids,
            )
            .unwrap();
            assert_eq!(state.selected_session, Some(session("clone")));
            assert!(state.transcript.entries().is_empty());
            assert!(effects.iter().any(|effect| matches!(
                effect,
                UiEffect::SendCommittedHydration { session_id, .. } if session_id == "clone"
            )));

            reduce(
                &mut state,
                UiAction::BackendEvent(history(
                    "get_messages-1",
                    Some(session("clone")),
                    "clone transcript",
                )),
                &mut ids,
            )
            .unwrap();
            reduce(
                &mut state,
                UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
                &mut ids,
            )
            .unwrap();
            assert_eq!(
                state.transcript.latest_user_text(),
                Some("clone transcript")
            );
        }
    }

    #[test]
    fn fork_and_navigation_restore_prompts_only_after_authoritative_hydration() {
        let mut state = UiState::unconfigured();
        state.selected_session = Some(session("source"));
        state.history.session = Some(session("source"));
        state.history.active_leaf_id = Some("source-leaf".into());
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::ForkSession {
                entry_id: "user-2".into(),
            },
            &mut ids,
        )
        .unwrap();
        let mut forked = derivation("source", "fork");
        forked.selected_entry_id = Some("user-2".into());
        forked.selected_prompt = Some("restore me\u{1b}[31m".into());
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("fork_session-1", "fork_session", true)),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionForked {
                command_id: "fork_session-1".into(),
                derived: forked,
            }),
            &mut ids,
        )
        .unwrap();
        assert!(
            !effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::RestoreSessionDraft(_)))
        );
        let mut unnamed_fork = session("fork");
        unnamed_fork.session_name = None;
        reduce(
            &mut state,
            UiAction::BackendEvent(history("get_messages-1", Some(unnamed_fork), "forked")),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::RestoreSessionDraft(text) if text == "restore me\u{1b}[31m"
        )));
        assert_eq!(
            state
                .history
                .session
                .as_ref()
                .and_then(|session| session.session_name.as_deref()),
            Some("fork name")
        );

        state.history.active_leaf_id = Some("leaf-2".into());
        reduce(
            &mut state,
            UiAction::NavigateSessionTree {
                entry_id: "user-1".into(),
            },
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionTreeNavigated {
                command_id: "navigate_session_tree-1".into(),
                navigation: SessionTreeNavigation {
                    session: session("fork"),
                    selected_entry_id: "user-1".into(),
                    previous_active_leaf_id: Some("leaf-2".into()),
                    active_leaf_id: Some("leaf-1".into()),
                    editor_text: Some("edit again".into()),
                    changed: true,
                    entry_count: 4,
                },
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished(
                "navigate_session_tree-1",
                "navigate_session_tree",
                true,
            )),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(history("get_messages-2", Some(session("fork")), "older")),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-2", "get_messages", true)),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::RestoreSessionDraft(text) if text == "edit again"
        )));
    }

    #[test]
    fn naming_and_tree_pages_commit_only_correlated_bounded_results() {
        let mut state = UiState::unconfigured();
        state.selected_session = Some(session("source"));
        state.history.session = Some(session("source"));
        state.history.active_leaf_id = Some("leaf-2".into());
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::SetSessionName("renamed".into()),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionNameChanged {
                command_id: "set_session_name-1".into(),
                changed: SessionNameChange {
                    session: SessionIdentity {
                        session_name: Some("renamed".into()),
                        ..session("source")
                    },
                    previous_name: Some("source name".into()),
                    entry_count: 3,
                },
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(
            state
                .selected_session
                .as_ref()
                .unwrap()
                .session_name
                .as_deref(),
            Some("source name")
        );
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("set_session_name-1", "set_session_name", true)),
            &mut ids,
        )
        .unwrap();
        assert_eq!(
            state
                .selected_session
                .as_ref()
                .unwrap()
                .session_name
                .as_deref(),
            Some("renamed")
        );
        assert_eq!(
            state
                .history
                .session
                .as_ref()
                .unwrap()
                .session_name
                .as_deref(),
            Some("renamed")
        );

        reduce(
            &mut state,
            UiAction::LoadSessionTree {
                after_entry_id: None,
            },
            &mut ids,
        )
        .unwrap();
        let page = SessionTreePage {
            session: Some(SessionIdentity {
                session_name: None,
                ..session("source")
            }),
            active_leaf_id: Some("leaf-2".into()),
            total_node_count: 1,
            nodes: vec![SessionTreeNode {
                entry_id: "leaf-2".into(),
                parent_id: None,
                created_at: "2026-01-02T03:04:05Z".into(),
                kind: SessionTreeNodeKind::Message,
                role: Some("user".into()),
                preview: "hello".into(),
                preview_truncated: false,
            }],
            truncated: false,
            next_after_entry_id: None,
        };
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_session_tree-1", "get_session_tree", true)),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionTreeReported {
                command_id: "get_session_tree-1".into(),
                page: page.clone(),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::ShowSessionTreePage { page: shown, append: false }, UiEffect::RequestRender]
                if shown == &page
        ));
    }

    #[test]
    fn wrong_tree_entry_preserves_history_and_unrevert_rehydrates_the_committed_leaf() {
        let mut state = UiState::unconfigured();
        state.selected_session = Some(session("source"));
        state.history.session = Some(session("source"));
        state.history.active_leaf_id = Some("leaf-1".into());
        state.transcript.append_prompt("keep me".into());
        let before = state.transcript.clone();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::NavigateSessionTree {
                entry_id: "requested".into(),
            },
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionTreeNavigated {
                command_id: "navigate_session_tree-1".into(),
                navigation: SessionTreeNavigation {
                    session: session("source"),
                    selected_entry_id: "wrong".into(),
                    previous_active_leaf_id: Some("leaf-1".into()),
                    active_leaf_id: Some("leaf-2".into()),
                    editor_text: None,
                    changed: true,
                    entry_count: 3,
                },
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.transcript, before);
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );

        reduce(&mut state, UiAction::UnrevertSessionTree, &mut ids).unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished(
                "unrevert_session_tree-1",
                "unrevert_session_tree",
                true,
            )),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionTreeUnreverted {
                command_id: "unrevert_session_tree-1".into(),
                unreverted: SessionTreeUnrevert {
                    session: session("source"),
                    source_transition_id: "transition-1".into(),
                    previous_active_leaf_id: Some("leaf-1".into()),
                    active_leaf_id: Some("leaf-2".into()),
                    entry_count: 4,
                },
            }),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::SendCommittedHydration { session_id, .. } if session_id == "source"
        )));
        assert!(state.transcript.entries().is_empty());

        reduce(
            &mut state,
            UiAction::BackendEvent(history(
                "get_messages-1",
                Some(session("source")),
                "restored",
            )),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.transcript.latest_user_text(), Some("restored"));
    }

    #[test]
    fn no_op_user_navigation_restores_the_prompt_without_rehydrating() {
        let mut state = UiState::unconfigured();
        state.selected_session = Some(session("source"));
        state.history.session = Some(session("source"));
        state.history.active_leaf_id = Some("leaf-1".into());
        state.transcript.append_prompt("kept".into());
        let before = state.transcript.clone();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::NavigateSessionTree {
                entry_id: "user-2".into(),
            },
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished(
                "navigate_session_tree-1",
                "navigate_session_tree",
                true,
            )),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::SessionTreeNavigated {
                command_id: "navigate_session_tree-1".into(),
                navigation: SessionTreeNavigation {
                    session: session("source"),
                    selected_entry_id: "user-2".into(),
                    previous_active_leaf_id: Some("leaf-1".into()),
                    active_leaf_id: Some("leaf-1".into()),
                    editor_text: Some("edit me".into()),
                    changed: false,
                    entry_count: 2,
                },
            }),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.transcript, before);
        assert!(state.session_operation.is_none());
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::RestoreSessionDraft(text) if text == "edit me"
        )));
        assert!(
            effects
                .iter()
                .all(|effect| !matches!(effect, UiEffect::SendCommittedHydration { .. }))
        );
    }

    #[test]
    fn queue_event_projection_preserves_contents_and_enforces_runtime_bounds() {
        let updated = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "queue.updated",
            "steering": ["first", "duplicate"],
            "follow_up": ["duplicate"]
        }))
        .unwrap();
        assert!(matches!(
            updated,
            BackendEvent::QueueUpdated { steering, follow_up }
                if steering == ["first", "duplicate"] && follow_up == ["duplicate"]
        ));

        let removed = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "queue.items.removed",
            "command_id": "pop-1",
            "operation": "pop",
            "kind": "follow_up",
            "steering": [],
            "follow_up": ["draft"]
        }))
        .unwrap();
        assert!(matches!(
            removed,
            BackendEvent::QueueItemsRemoved {
                operation: QueueRemovalOperation::Pop,
                kind: Some(QueueKind::FollowUp),
                follow_up,
                ..
            } if follow_up == ["draft"]
        ));

        let injected = BackendEvent::from_projection_value(&serde_json::json!({
            "type": "queue.message.injected",
            "kind": "steering",
            "content": "expanded provider content",
            "skill_invocation": {"original_content": "/skill request"}
        }))
        .unwrap();
        assert_eq!(
            injected,
            BackendEvent::QueueMessageInjected {
                kind: QueueKind::Steering,
                content: "/skill request".into(),
            }
        );

        assert!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "queue.updated",
                "steering": vec!["x"; QUEUE_MESSAGE_LIMIT / 2],
                "follow_up": vec!["y"; QUEUE_MESSAGE_LIMIT / 2]
            }))
            .is_ok()
        );
        assert!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "queue.updated",
                "steering": ["x".repeat(QUEUE_CONTENT_BYTES_LIMIT)],
                "follow_up": []
            }))
            .is_ok()
        );

        for invalid in [
            serde_json::json!({
                "type": "queue.items.removed",
                "command_id": "pop-1",
                "operation": "pop",
                "kind": null,
                "steering": [],
                "follow_up": []
            }),
            serde_json::json!({
                "type": "queue.items.removed",
                "command_id": "pop-1",
                "operation": "pop",
                "kind": "steering",
                "steering": ["first", "second"],
                "follow_up": []
            }),
            serde_json::json!({
                "type": "queue.items.removed",
                "command_id": "pop-1",
                "operation": "pop",
                "kind": "steering",
                "steering": [],
                "follow_up": ["wrong queue"]
            }),
        ] {
            assert!(matches!(
                BackendEvent::from_projection_value(&invalid),
                Err(EventProjectionError::InvalidField { .. })
            ));
        }

        assert!(matches!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "queue.updated",
                "steering": vec!["x"; QUEUE_MESSAGE_LIMIT + 1],
                "follow_up": []
            })),
            Err(EventProjectionError::TooManyQueueMessages { .. })
        ));
        assert!(matches!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "queue.items.removed",
                "command_id": "pop-1",
                "operation": "clear",
                "kind": null,
                "steering": vec!["x"; QUEUE_MESSAGE_LIMIT + 1],
                "follow_up": []
            })),
            Err(EventProjectionError::TooManyQueueMessages { .. })
        ));
        assert!(matches!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "queue.message.injected",
                "kind": "steering",
                "content": "x".repeat(QUEUE_CONTENT_BYTES_LIMIT + 1),
                "skill_invocation": null
            })),
            Err(EventProjectionError::OversizedQueueContent { .. })
        ));
    }

    #[test]
    fn queue_updates_replace_authoritatively_and_preserve_duplicate_identities() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["first".into(), "duplicate".into(), "duplicate".into()],
                follow_up: vec!["follow-up".into()],
            }),
            &mut ids,
        )
        .unwrap();
        let identities = state
            .queue
            .steering
            .iter()
            .map(|message| message.identity)
            .collect::<Vec<_>>();

        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["duplicate".into(), "first".into(), "duplicate".into()],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.queued_steering(), 3);
        assert_eq!(state.queued_follow_ups(), 0);
        assert_eq!(
            state
                .queue
                .steering
                .iter()
                .map(|message| message.identity)
                .collect::<Vec<_>>(),
            vec![identities[1], identities[0], identities[2]]
        );
    }

    #[test]
    fn queue_injection_uses_visible_content_and_removes_the_first_duplicate() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["duplicate".into(), "duplicate".into()],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();
        let second_identity = state.queue.steering[1].identity;

        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueMessageInjected {
                kind: QueueKind::Steering,
                content: "duplicate".into(),
            }),
            &mut ids,
        )
        .unwrap();

        assert_eq!(state.transcript.latest_user_text(), Some("duplicate"));
        assert_eq!(state.queue.steering.len(), 1);
        assert_eq!(state.queue.steering[0].identity, second_identity);
    }

    #[test]
    fn queue_restore_prefers_newest_local_order_over_fallback_queue_order() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: Vec::new(),
                follow_up: vec!["older follow-up".into()],
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::Steer("newer steering".into()),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["newer steering".into()],
                follow_up: vec!["older follow-up".into()],
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished("steer-1", "steer", true)),
            &mut ids,
        )
        .unwrap();

        let effects = reduce(&mut state, UiAction::RestoreNewestQueueDraft, &mut ids).unwrap();
        assert_eq!(command_value(&effects[0]).unwrap()["kind"], "steering");
    }

    #[test]
    fn failed_queue_submission_restores_its_saved_draft() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, UiAction::Steer("saved draft".into()), &mut ids).unwrap();
        assert_eq!(command_value(&effects[0]).unwrap()["type"], "steer");

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("steer-1", "steer", false)),
            &mut ids,
        )
        .unwrap();
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::RestoreDraft { content, local_order: Some(0) }] if content == "saved draft"
        ));
        assert!(state.pending_queue_submissions.is_empty());
    }

    #[test]
    fn queue_restore_waits_for_matching_pop_removal_in_either_event_order() {
        let mut fallback = UiState::new("fake".into(), None, None);
        let mut fallback_ids = DeterministicIds::default();
        reduce(
            &mut fallback,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["steering".into()],
                follow_up: vec!["follow-up".into()],
            }),
            &mut fallback_ids,
        )
        .unwrap();
        let effects = reduce(
            &mut fallback,
            UiAction::RestoreNewestQueueDraft,
            &mut fallback_ids,
        )
        .unwrap();
        assert_eq!(command_value(&effects[0]).unwrap()["kind"], "follow_up");

        for removal_first in [true, false] {
            let mut state = UiState::new("fake".into(), None, None);
            let mut ids = DeterministicIds::default();
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::QueueUpdated {
                    steering: Vec::new(),
                    follow_up: vec!["draft".into()],
                }),
                &mut ids,
            )
            .unwrap();
            let effects = reduce(&mut state, UiAction::RestoreNewestQueueDraft, &mut ids).unwrap();
            assert_eq!(command_value(&effects[0]).unwrap()["kind"], "follow_up");

            let removed = BackendEvent::QueueItemsRemoved {
                command_id: "pop_queue-1".into(),
                operation: QueueRemovalOperation::Pop,
                kind: Some(QueueKind::FollowUp),
                steering: Vec::new(),
                follow_up: vec!["draft".into()],
            };
            let finished = finished("pop_queue-1", "pop_queue", true);
            let (first, second) = if removal_first {
                (removed, finished)
            } else {
                (finished, removed)
            };
            assert!(
                reduce(&mut state, UiAction::BackendEvent(first), &mut ids)
                    .unwrap()
                    .is_empty()
            );
            let effects = reduce(&mut state, UiAction::BackendEvent(second), &mut ids).unwrap();
            assert!(matches!(
                effects.as_slice(),
                [UiEffect::RestoreDraft { content, local_order: None }] if content == "draft"
            ));
            assert!(state.pending_queue_restore.is_none());
        }
    }

    #[test]
    fn stale_queue_removal_and_cancel_leave_queue_state_intact() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["steering".into()],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();
        let before = state.queue.clone();
        reduce(&mut state, UiAction::RestoreNewestQueueDraft, &mut ids).unwrap();
        assert!(
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::QueueItemsRemoved {
                    command_id: "stale-pop".into(),
                    operation: QueueRemovalOperation::Pop,
                    kind: Some(QueueKind::Steering),
                    steering: vec!["steering".into()],
                    follow_up: Vec::new(),
                }),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        assert!(
            reduce(
                &mut state,
                UiAction::BackendEvent(finished("pop_queue-1", "pop_queue", true)),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        assert_eq!(state.queue, before);

        assert!(
            reduce(&mut state, UiAction::Cancel, &mut ids)
                .unwrap()
                .is_empty()
        );
        assert_eq!(state.queue, before);
    }

    #[test]
    fn queue_submission_rejects_blank_and_combined_capacity_overflow() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, UiAction::Steer(" \n\t".into()), &mut ids).unwrap();
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::Notice(_), UiEffect::RequestRender]
        ));
        assert!(state.pending_queue_submissions.is_empty());

        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["x".into(); QUEUE_MESSAGE_LIMIT - 2],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(state.queue_submission_preflight("x").is_ok());
        reduce(&mut state, UiAction::Steer("x".into()), &mut ids).unwrap();
        assert!(state.queue_submission_preflight("x").is_ok());
        reduce(&mut state, UiAction::FollowUp("x".into()), &mut ids).unwrap();
        assert_eq!(
            state.queue_submission_preflight("x"),
            Err(QueueSubmissionPreflightError::Full)
        );
        let effects = reduce(&mut state, UiAction::Steer("x".into()), &mut ids).unwrap();
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::Notice(_), UiEffect::RequestRender]
        ));
        assert_eq!(state.pending_queue_submissions.len(), 2);
    }

    #[test]
    fn queue_submission_accepts_exact_byte_capacity_and_rejects_one_over() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["x".repeat(QUEUE_CONTENT_BYTES_LIMIT - 1)],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(state.queue_submission_preflight("x").is_ok());
        reduce(&mut state, UiAction::Steer("x".into()), &mut ids).unwrap();
        assert_eq!(
            state.queue_submission_preflight("x"),
            Err(QueueSubmissionPreflightError::Full)
        );
        let effects = reduce(&mut state, UiAction::FollowUp("x".into()), &mut ids).unwrap();
        assert!(matches!(
            effects.as_slice(),
            [UiEffect::Notice(_), UiEffect::RequestRender]
        ));
    }

    #[test]
    fn queue_pop_removes_the_last_matching_duplicate() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec!["duplicate".into(), "between".into(), "duplicate".into()],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();
        let first_identity = state.queue.steering[0].identity;
        let last_identity = state.queue.steering[2].identity;
        reduce(&mut state, UiAction::RestoreNewestQueueDraft, &mut ids).unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::QueueItemsRemoved {
                command_id: "pop_queue-1".into(),
                operation: QueueRemovalOperation::Pop,
                kind: Some(QueueKind::Steering),
                steering: vec!["duplicate".into()],
                follow_up: Vec::new(),
            }),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.queue.steering.len(), 2);
        assert_eq!(state.queue.steering[0].identity, first_identity);
        assert_ne!(state.queue.steering[1].identity, last_identity);
    }

    #[test]
    fn session_commits_clear_queue_cache_and_refresh_queue_state() {
        let mut startup = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(&mut startup, UiAction::StartupHydration, &mut ids).unwrap();
        reduce(
            &mut startup,
            UiAction::BackendEvent(history("get_messages-1", None, "history")),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut startup,
            UiAction::BackendEvent(finished("get_messages-1", "get_messages", true)),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| {
            command_value(effect).is_some_and(|command| command["type"] == "get_queue_state")
        }));

        for action in [
            UiAction::SelectSession {
                session_id: "next".into(),
            },
            UiAction::NewSession,
        ] {
            let mut state = UiState::new("fake".into(), None, None);
            let mut ids = DeterministicIds::default();
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::QueueUpdated {
                    steering: vec!["stale".into()],
                    follow_up: vec!["stale follow-up".into()],
                }),
                &mut ids,
            )
            .unwrap();
            let effects = reduce(&mut state, action, &mut ids).unwrap();
            let command = command_value(&effects[0]).unwrap();
            let effects = if command["type"] == "select_session" {
                reduce(
                    &mut state,
                    UiAction::BackendEvent(BackendEvent::SessionSelected {
                        command_id: "select_session-1".into(),
                        session: session("next"),
                    }),
                    &mut ids,
                )
                .unwrap();
                reduce(
                    &mut state,
                    UiAction::BackendEvent(finished("select_session-1", "select_session", true)),
                    &mut ids,
                )
                .unwrap()
            } else {
                reduce(
                    &mut state,
                    UiAction::BackendEvent(finished("new_session-1", "new_session", true)),
                    &mut ids,
                )
                .unwrap()
            };
            assert!(state.queue.steering.is_empty());
            assert!(state.queue.follow_up.is_empty());
            assert!(effects.iter().any(|effect| {
                command_value(effect).is_some_and(|command| command["type"] == "get_queue_state")
            }));
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
        assert!(effects.iter().any(|effect| {
            command_value(effect).is_some_and(|command| command["type"] == "get_queue_state")
        }));
    }

    fn connection_catalog(label: &str, source: &str) -> ConnectionCatalogSnapshot {
        ConnectionCatalogSnapshot {
            providers: vec![wisp_protocol::events::ConnectionProviderSnapshot {
                id: "openai".into(),
                label: label.into(),
                methods: vec![wisp_protocol::events::ConnectionMethodSnapshot {
                    provider: "openai".into(),
                    label: "API key".into(),
                    kind: "api_key".into(),
                    source: source.into(),
                    environment_variable: Some("OPENAI_API_KEY".into()),
                    oauth_expires_at: None,
                    has_stored_credential: source == "stored",
                }],
            }],
        }
    }

    #[test]
    fn startup_and_refresh_commit_only_matching_connection_reports_and_finishes() {
        let old = connection_catalog("Old", "missing");
        let fresh = connection_catalog("Fresh", "environment");
        let mut startup = UiState::unconfigured();
        startup.connection_catalog = old.clone();
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut startup, UiAction::StartupHydration, &mut ids).unwrap();
        assert!(effects.iter().any(|effect| {
            command_value(effect).is_some_and(|command| command["type"] == "get_messages")
        }));
        let effects = reduce(&mut startup, UiAction::LoadConnectionCatalog, &mut ids).unwrap();
        assert!(effects.iter().any(|effect| {
            command_value(effect).is_some_and(|command| command["type"] == "get_connection_catalog")
        }));

        for finish_first in [false, true] {
            let mut state = UiState::unconfigured();
            state.connection_catalog = old.clone();
            let mut ids = DeterministicIds::default();
            reduce(&mut state, UiAction::LoadConnectionCatalog, &mut ids).unwrap();
            let report = BackendEvent::ConnectionCatalogReported {
                command_id: "get_connection_catalog-1".into(),
                catalog: fresh.clone(),
            };
            let finished = finished("get_connection_catalog-1", "get_connection_catalog", true);
            let events = if finish_first {
                [finished, report]
            } else {
                [report, finished]
            };
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::ConnectionCatalogReported {
                    command_id: "stale".into(),
                    catalog: connection_catalog("Stale", "stored"),
                }),
                &mut ids,
            )
            .unwrap();
            assert_eq!(state.connection_catalog, old);
            reduce(
                &mut state,
                UiAction::BackendEvent(events[0].clone()),
                &mut ids,
            )
            .unwrap();
            assert_eq!(state.connection_catalog, old);
            let effects = reduce(
                &mut state,
                UiAction::BackendEvent(events[1].clone()),
                &mut ids,
            )
            .unwrap();
            assert_eq!(state.connection_catalog, fresh);
            assert!(effects.iter().any(|effect| matches!(
                effect,
                UiEffect::ConnectionCatalogUpdated(catalog) if catalog == &fresh
            )));
        }

        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(&mut state, UiAction::LoadConnectionCatalog, &mut ids).unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ConnectionCatalogReported {
                command_id: "get_connection_catalog-1".into(),
                catalog: fresh,
            }),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished(
                "get_connection_catalog-1",
                "get_connection_catalog",
                true,
            )),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ProjectConfigApplied {
                provider: "openai".into(),
                model: None,
                effort: None,
            }),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| {
            command_value(effect).is_some_and(|command| {
                command["type"] == "get_connection_catalog"
                    && command["id"] == "get_connection_catalog-2"
            })
        }));
    }

    #[test]
    fn successful_connection_mutations_finish_without_a_catalog_report() {
        let actions = [
            (
                UiAction::StoreApiKey {
                    provider: "openai".into(),
                    api_key: ApiKey::new("secret".into()).unwrap(),
                },
                "store_api_key-1",
                "store_api_key",
            ),
            (
                UiAction::DisconnectProvider {
                    provider: "openai".into(),
                },
                "disconnect_provider-1",
                "disconnect_provider",
            ),
        ];

        for (action, command_id, command_type) in actions {
            let mut state = UiState::unconfigured();
            let mut ids = DeterministicIds::default();
            reduce(&mut state, action, &mut ids).unwrap();

            let effects = reduce(
                &mut state,
                UiAction::BackendEvent(finished(command_id, command_type, true)),
                &mut ids,
            )
            .unwrap();

            assert!(state.connection_operation.is_none());
            assert!(effects.iter().any(|effect| matches!(
                effect,
                UiEffect::Notice(notice)
                    if notice
                        == "Credentials updated, but connection status could not be refreshed."
            )));
        }

        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BeginDeviceCode {
                provider: "openai-codex".into(),
            },
            &mut ids,
        )
        .unwrap();
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("begin_device_code-1", "begin_device_code", true)),
            &mut ids,
        )
        .unwrap();

        assert!(state.connection_operation.is_none());
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::FinishDeviceCode))
        );
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::Notice(notice)
                if notice
                    == "Connected: openai-codex. Connection status could not be refreshed."
        )));
    }

    #[test]
    fn project_config_applied_defers_catalog_reload_during_device_login() {
        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut state,
            UiAction::BeginDeviceCode {
                provider: "openai-codex".into(),
            },
            &mut ids,
        )
        .unwrap();

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ProjectConfigApplied {
                provider: "openai".into(),
                model: None,
                effort: None,
            }),
            &mut ids,
        )
        .unwrap();
        assert!(state.connection_catalog_reload_pending);
        assert!(
            effects
                .iter()
                .all(|effect| !matches!(effect, UiEffect::SendCommand(_)))
        );

        let challenge = DeviceCodeChallenge {
            provider: "openai-codex".into(),
            verification_uri: "https://example.test/device".into(),
            user_code: "ABCD".into(),
        };
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::DeviceCodeReported {
                command_id: "begin_device_code-1".into(),
                challenge: challenge.clone(),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::ShowDeviceCode(received) if received == &challenge
        )));

        let effects = reduce(&mut state, UiAction::CancelDeviceCode, &mut ids).unwrap();
        assert_eq!(
            command_value(&effects[0]).unwrap(),
            serde_json::json!({
                "type": "cancel",
                "id": "cancel-1",
                "target_id": "begin_device_code-1",
            })
        );

        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(finished("begin_device_code-1", "begin_device_code", false)),
            &mut ids,
        )
        .unwrap();
        assert!(!state.connection_catalog_reload_pending);
        assert!(effects.iter().any(|effect| {
            command_value(effect).is_some_and(|command| {
                command["type"] == "get_connection_catalog"
                    && command["id"] == "get_connection_catalog-1"
            })
        }));
    }

    #[test]
    fn connection_secrets_are_redacted_and_device_progress_is_monotonic() {
        let secret = "key-do-not-retain";
        let action = UiAction::StoreApiKey {
            provider: "openai".into(),
            api_key: ApiKey::new(secret.into()).unwrap(),
        };
        assert!(!format!("{action:?}").contains(secret));
        let mut state = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        let effects = reduce(&mut state, action, &mut ids).unwrap();
        assert!(matches!(
            effects.first(),
            Some(UiEffect::SendSecretCommand(_))
        ));
        assert!(!format!("{state:?}").contains(secret));
        assert!(!format!("{effects:?}").contains(secret));
        assert!(state.transcript.entries().is_empty());

        let effects = reduce(
            &mut state,
            UiAction::BeginDeviceCode {
                provider: "openai-codex".into(),
            },
            &mut ids,
        )
        .unwrap();
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );

        let mut device = UiState::unconfigured();
        let mut ids = DeterministicIds::default();
        reduce(
            &mut device,
            UiAction::BeginDeviceCode {
                provider: "openai-codex".into(),
            },
            &mut ids,
        )
        .unwrap();
        let challenge = DeviceCodeChallenge {
            provider: "openai-codex".into(),
            verification_uri: "https://example.test/device".into(),
            user_code: "ABCD".into(),
        };
        let effects = reduce(
            &mut device,
            UiAction::BackendEvent(BackendEvent::DeviceCodeReported {
                command_id: "begin_device_code-1".into(),
                challenge: challenge.clone(),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::ShowDeviceCode(received) if received == &challenge
        )));
        let progress = DeviceCodeProgress {
            provider: "openai-codex".into(),
            attempt: 2,
        };
        assert!(reduce(
            &mut device,
            UiAction::BackendEvent(BackendEvent::DeviceCodeProgress {
                command_id: "begin_device_code-1".into(),
                progress: progress.clone(),
            }),
            &mut ids,
        )
        .unwrap()
        .iter()
        .any(|effect| matches!(effect, UiEffect::DeviceCodeProgress(received) if received == &progress)));
        assert!(
            reduce(
                &mut device,
                UiAction::BackendEvent(BackendEvent::DeviceCodeProgress {
                    command_id: "begin_device_code-1".into(),
                    progress,
                }),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        assert!(
            reduce(
                &mut device,
                UiAction::BackendEvent(BackendEvent::DeviceCodeProgress {
                    command_id: "stale".into(),
                    progress: DeviceCodeProgress {
                        provider: "openai-codex".into(),
                        attempt: 3,
                    },
                }),
                &mut ids,
            )
            .unwrap()
            .is_empty()
        );
        let effects = reduce(&mut device, UiAction::CancelDeviceCode, &mut ids).unwrap();
        assert_eq!(
            command_value(&effects[0]).unwrap(),
            serde_json::json!({
                "type": "cancel",
                "id": "cancel-1",
                "target_id": "begin_device_code-1",
            })
        );
        let effects = reduce(
            &mut device,
            UiAction::BackendEvent(finished("begin_device_code-1", "begin_device_code", false)),
            &mut ids,
        )
        .unwrap();
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::FinishDeviceCode))
        );
        assert!(effects.iter().any(|effect| matches!(
            effect,
            UiEffect::Notice(notice) if notice == "Device login cancelled."
        )));
    }

    #[test]
    fn session_commands_during_history_requests_return_a_notice() {
        let mut state = UiState::new("fake".into(), None, None);
        state.history_request = Some(HistoryRequest {
            command_id: "history-1".into(),
            kind: HistoryRequestKind::Latest,
            active_leaf_may_advance: false,
            report: None,
            completion: None,
        });
        let mut ids = DeterministicIds::default();

        let effects = reduce(&mut state, UiAction::Submit("keep me".into()), &mut ids).unwrap();
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::Notice(_)))
        );
        assert!(
            effects
                .iter()
                .all(|effect| !matches!(effect, UiEffect::SendCommand(_)))
        );
        assert!(state.current_command.is_none());
        assert!(state.transcript.entries().is_empty());

        for action in [UiAction::LoadSessionCatalog, UiAction::NewSession] {
            let effects = reduce(&mut state, action, &mut ids).unwrap();
            assert!(
                effects
                    .iter()
                    .any(|effect| matches!(effect, UiEffect::Notice(_)))
            );
            assert!(
                effects
                    .iter()
                    .all(|effect| !matches!(effect, UiEffect::SendCommand(_)))
            );
            assert!(state.session_operation.is_none());
            assert!(state.history_request.is_some());
        }
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
    fn older_history_allows_leaf_advancement_after_a_completed_live_prompt() {
        let selected = session("active");
        let mut state = UiState::new("fake".into(), None, None);
        state.history.session = Some(selected.clone());
        state.history.active_leaf_id = Some("old-leaf".into());
        state.history.oldest_cursor = Some("current-entry".into());
        state
            .history
            .represented_durable_entry_ids
            .insert("current-entry".into());
        state.transcript.append_prompt("historical".into());
        state.transcript.mark_history_entries(0, "current-entry");
        state
            .transcript
            .append_prompt("completed live prompt".into());
        let mut ids = DeterministicIds::default();

        reduce(&mut state, UiAction::LoadOlderHistory, &mut ids).unwrap();
        assert!(
            state
                .history_request
                .as_ref()
                .is_some_and(|request| request.active_leaf_may_advance)
        );
        let mut older = SharedTranscript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::MessagesReported {
                command_id: "get_messages-1".into(),
                messages: SessionMessages {
                    session: Some(selected),
                    active_leaf_id: Some("advanced-leaf".into()),
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

        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::HistoryWindowChanged))
        );
        assert_eq!(
            state.history.active_leaf_id.as_deref(),
            Some("advanced-leaf")
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
            state.transcript.represented_durable_entry_ids();
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
        assert_eq!(state.history.newest_cursor.as_deref(), Some("entry-1196"));
        assert_eq!(state.transcript.entries().len(), 1_198);
        assert_eq!(state.history.represented_durable_entry_ids.len(), 1_198);
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
        state.transcript.append_prompt("later live prompt".into());
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
                    active_leaf_id: Some("advanced-leaf".into()),
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
        assert_eq!(
            state.history.active_leaf_id.as_deref(),
            Some("advanced-leaf")
        );
    }
}
