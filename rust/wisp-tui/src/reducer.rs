//! Deterministic, terminal-independent state transitions for the native TUI.

use serde_json::Value;
use thiserror::Error;
use wisp_protocol::ProtocolDecodeError;

use crate::tool_cards::{ToolCallInput, ToolResultInput, bounded_identity};
use crate::transcript::SharedTranscript;
use wisp_protocol::commands::{ApprovalScope, WispTypedClientRpcCommands};

mod event_projection;

pub use event_projection::EventProjectionError;

const DEFAULT_DENIAL_REASON: &str = "Denied from TUI";
const CANCELLED_APPROVAL_REASON: &str = "Denied from TUI: cancelled";
const CANCELLING_APPROVAL_REASON: &str = "Denied from TUI: cancelling";
const CANCELLED_TRUST_REASON: &str = "Trust prompt cancelled";
const RPC_CANCELLED_PREFIX: &str = "RPC command cancelled:";

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
    pub queued_steering: usize,
    pub queued_follow_ups: usize,
    pub current_command: Option<ActiveCommand>,
    pub pending_approval: Option<PendingApproval>,
    pub pending_trust_request_id: Option<String>,
    pub pending_trust_project_path: Option<String>,
    pub cancel_requested: bool,
    pub exit_requested: bool,
    pub transcript: SharedTranscript,
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
            queued_steering: 0,
            queued_follow_ups: 0,
            current_command: None,
            pending_approval: None,
            pending_trust_request_id: None,
            pending_trust_project_path: None,
            cancel_requested: false,
            exit_requested: false,
            transcript: SharedTranscript::default(),
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
}

impl CommandKind {
    pub fn prefix(self) -> &'static str {
        match self {
            Self::Prompt => "prompt",
            Self::Approval => "approval",
            Self::Cancel => "cancel",
            Self::Trust => "trust",
            Self::GetSessionStats => "get_session_stats",
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
}

pub fn reduce(
    state: &mut UiState,
    action: UiAction,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ReduceError> {
    match action {
        UiAction::Submit(content) => submit(state, content, ids),
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

fn handle_backend_event(
    state: &mut UiState,
    event: BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
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
            UiEffect::RequestRender | UiEffect::Exit => None,
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
    fn tool_event_projection_preserves_structured_fields_and_trace_defaults() {
        assert!(matches!(
            BackendEvent::from_projection_value(&serde_json::json!({
                "type": "tool.call",
                "call_id": "call-1",
                "name": "read",
                "arguments": {"path": "README.md"}
            }))
            .unwrap(),
            BackendEvent::ToolCall(ToolCallInput { call_id, name, arguments })
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
        assert_eq!(generic.arguments.as_object().unwrap().len(), 8);
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
}
