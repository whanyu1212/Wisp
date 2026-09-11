//! Context snapshots and compaction presentation; execution policy stays in Python.

use super::*;
use wisp_protocol::events::{CompactionOutcome, CompactionReason, ContextBudget, SessionStats};

#[derive(Clone, Debug, Default, PartialEq)]
pub(crate) struct ContextState {
    pub budget: Option<ContextBudget>,
    pub stats: Option<Box<SessionStats>>,
    pub stats_stale: bool,
    pub error: Option<String>,
    pub compaction: Option<CompactionReason>,
    pub compaction_notice: Option<String>,
    generation: u64,
    refresh_needed: bool,
    read: Option<StatsRead>,
    auto_change: Option<(String, bool)>,
}

#[derive(Clone, Debug, PartialEq)]
struct StatsRead {
    id: String,
    generation: u64,
    session_id: Option<String>,
    report: Option<Box<SessionStats>>,
    invalid_report: bool,
    history_sync_follows: bool,
}

impl ContextState {
    pub fn loading(&self) -> bool {
        self.read.is_some()
    }
    pub fn configuring(&self) -> bool {
        self.auto_change.is_some()
    }

    /// Retire displayed values without forgetting ownership of an in-flight read.
    pub(super) fn invalidate(&mut self) {
        self.generation = self
            .generation
            .checked_add(1)
            .expect("context generation exhausted");
        self.budget = None;
        self.stats = None;
        self.stats_stale = true;
        self.error = None;
        self.compaction = None;
        self.compaction_notice = None;
        self.refresh_needed = true;
    }

    pub(super) fn operation_started(&mut self) {
        self.stats_stale = true;
        self.compaction = None;
        self.compaction_notice = None;
    }

    pub(super) fn close(&mut self) {
        self.read = None;
        self.auto_change = None;
        self.refresh_needed = false;
    }
}

pub(super) fn request(state: &mut UiState) -> Vec<UiEffect> {
    if !state.context.loading() {
        state.context.refresh_needed = true;
    }
    vec![UiEffect::RequestRender]
}

/// Called after reducer transitions, so refreshes wait for committed hydration/configuration.
pub(super) fn refresh_if_ready(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.context.refresh_needed
        || state.context.loading()
        || state.exit_requested
        || !state.input_ready
        || state.current_command.is_some()
        || state.configuration_active()
        || state.session_operation.is_some()
        || state.connection_request_active()
        || state.history_request.is_some()
        || state.post_prompt_session_sync_pending
    {
        return Ok(Vec::new());
    }
    start_refresh(state, false, ids)
}

pub(super) fn start_refresh(
    state: &mut UiState,
    history_sync_follows: bool,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let id = ids.next_id(CommandKind::GetSessionStats);
    let command = WispTypedClientRpcCommands::get_session_stats(&id)?;
    state.context.read = Some(StatsRead {
        id,
        generation: state.context.generation,
        session_id: effective_session(state).map(str::to_owned),
        report: None,
        invalid_report: false,
        history_sync_follows,
    });
    state.context.refresh_needed = false;
    state.context.error = None;
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn skip(
    state: &mut UiState,
    id: &str,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    finish_refresh(
        state,
        id,
        false,
        Some("Context refresh exceeds the negotiated frame limit."),
        ids,
    )
}

fn finish_refresh(
    state: &mut UiState,
    id: &str,
    ok: bool,
    error: Option<&str>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.context.read.as_ref().is_none_or(|read| read.id != id) {
        return Ok(Vec::new());
    }
    let read = state.context.read.take().expect("matched statistics read");
    let current = read.generation == state.context.generation;
    let report = read.report.filter(|report| {
        !read.invalid_report
            && (read.session_id.as_deref() == report.session_id.as_deref()
                || (read.history_sync_follows && read.session_id.is_none()))
    });
    if let Some(report) = report.filter(|_| ok && current) {
        state.context.budget = Some(report.context.clone());
        state.context.stats = Some(report);
        state.context.stats_stale = false;
        state.context.error = None;
    } else if current {
        state.context.stats_stale = true;
        state.context.error = Some(bounded_session_text(error.unwrap_or(
            "Context statistics unavailable: missing or mismatched report. Use /context to retry."),
            SESSION_NOTICE_MAX_BYTES));
    }
    if read.history_sync_follows {
        start_post_prompt_session_sync(state, ids)
    } else {
        Ok(vec![UiEffect::RequestRender])
    }
}

pub(super) fn configure_auto(
    state: &mut UiState,
    enabled: bool,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.can_select_model() || session_sync_pending(state) {
        return Ok(vec![
            UiEffect::Notice(
                "Wait for the current operation before changing automatic compaction.".into(),
            ),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(CommandKind::Configure);
    let command = WispTypedClientRpcCommands::configure_auto_compaction(&id, enabled)?;
    state.context.auto_change = Some((id, enabled));
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn compact(
    state: &mut UiState,
    instructions: Option<String>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.can_select_model() || session_sync_pending(state) {
        return Ok(vec![
            UiEffect::Notice("Wait for the current operation before compacting.".into()),
            UiEffect::RequestRender,
        ]);
    }
    if effective_session(state).is_none() {
        return Ok(vec![
            UiEffect::Notice("Compaction requires an existing persisted session.".into()),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(CommandKind::Compact);
    let command = WispTypedClientRpcCommands::compact(&id, instructions.as_deref())?;
    state.context.operation_started();
    state.view_status = ViewStatus::Running;
    state.interaction_status = InteractionStatus::Compacting;
    state.current_command = Some(ActiveCommand {
        id,
        command_type: ActiveCommandType::Compact,
    });
    state.cancel_requested = false;
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn effective_session(state: &UiState) -> Option<&str> {
    state
        .selected_session
        .as_ref()
        .map(|session| session.session_id.as_str())
        .or(state.last_session.as_deref())
}

fn compaction_matches(state: &UiState, session_id: &str, reason: CompactionReason) -> bool {
    effective_session(state).is_none_or(|current| current == session_id)
        && state
            .current_command
            .as_ref()
            .is_some_and(|command| match reason {
                CompactionReason::Manual => command.command_type == ActiveCommandType::Compact,
                _ => matches!(
                    command.command_type,
                    ActiveCommandType::Prompt | ActiveCommandType::Init
                ),
            })
}

pub(super) fn observe(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    match event {
        BackendEvent::SessionStatsReported { command_id, stats } => {
            if let Some(read) = state
                .context
                .read
                .as_mut()
                .filter(|read| read.id == *command_id)
            {
                // A duplicate report is a failed refresh, not permission to overwrite a snapshot.
                if read.report.is_some() {
                    read.invalid_report = true;
                }
                read.report = Some(stats.clone());
            }
            return Ok(Some(Vec::new()));
        }
        BackendEvent::ContextEstimated(estimate) => {
            if state.current_command.as_ref().is_some_and(|command| {
                matches!(
                    command.command_type,
                    ActiveCommandType::Prompt | ActiveCommandType::Init
                )
            }) && (state.model_selection_stale
                || state
                    .provider
                    .as_deref()
                    .is_none_or(|provider| provider == estimate.provider))
            {
                state.context.budget = Some(estimate.budget.clone());
                state.context.stats_stale = true;
                state.context.error = None;
            }
            return Ok(Some(vec![UiEffect::RequestRender]));
        }
        BackendEvent::CompactionStarted(started) => {
            if compaction_matches(state, &started.session_id, started.reason) {
                state.context.compaction = Some(started.reason);
                state.context.compaction_notice = None;
                if let Some(budget) = &started.trigger_budget {
                    state.context.budget = Some(budget.clone());
                }
            }
            return Ok(Some(vec![UiEffect::RequestRender]));
        }
        BackendEvent::CompactionCompleted(completed) => {
            if compaction_matches(state, &completed.session_id, completed.reason) {
                state.context.compaction = None;
                if completed.outcome == CompactionOutcome::Completed {
                    state.context.budget = None;
                }
                let outcome = match completed.outcome {
                    CompactionOutcome::Completed => format!(
                        "completed: {} entries replaced, {} retained",
                        completed.replaced_entry_count, completed.retained_entry_count
                    ),
                    CompactionOutcome::Failed => "failed".into(),
                    CompactionOutcome::Cancelled => "cancelled".into(),
                };
                let mut notice = format!("{} compaction {outcome}.", completed.reason.as_str());
                if completed.will_retry {
                    notice.push_str(" Retrying request.");
                }
                if let Some(error) = &completed.error {
                    notice.push(' ');
                    notice.push_str(error);
                }
                state.context.compaction_notice =
                    Some(bounded_session_text(&notice, SESSION_NOTICE_MAX_BYTES));
                state.context.stats_stale = true;
            }
            return Ok(Some(vec![UiEffect::RequestRender]));
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } => {
            if command_type == "get_session_stats"
                && state
                    .context
                    .read
                    .as_ref()
                    .is_some_and(|read| read.id == *command_id)
            {
                return Ok(Some(finish_refresh(
                    state,
                    command_id,
                    *ok,
                    error.as_deref(),
                    ids,
                )?));
            }
            if command_type == "configure"
                && state
                    .context
                    .auto_change
                    .as_ref()
                    .is_some_and(|(id, _)| id == command_id)
            {
                let (_, enabled) = state
                    .context
                    .auto_change
                    .take()
                    .expect("matched configuration");
                let mut effects = vec![UiEffect::RequestRender];
                if *ok {
                    state.context.invalidate();
                    effects.push(UiEffect::AutoCompactionConfigured);
                    effects.push(UiEffect::Notice(format!(
                        "Automatic compaction {}.",
                        if enabled { "enabled" } else { "disabled" }
                    )));
                } else {
                    effects.push(UiEffect::Notice(bounded_session_text(
                        error
                            .as_deref()
                            .unwrap_or("Automatic compaction unchanged; input was kept."),
                        SESSION_NOTICE_MAX_BYTES,
                    )));
                }
                return Ok(Some(effects));
            }
        }
        _ => {}
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Default)]
    struct Ids(std::collections::BTreeMap<&'static str, usize>);
    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            let count = self.0.entry(kind.prefix()).or_default();
            *count += 1;
            format!("{}-{count}", kind.prefix())
        }
    }
    use serde_json::{Value, json};

    fn fixture(kind: &str) -> Value {
        serde_json::from_str::<Value>(include_str!(
            "../../../../tests/fixtures/rust_tui_context.json"
        ))
        .unwrap()[kind]
            .clone()
    }
    fn event(kind: &str) -> BackendEvent {
        BackendEvent::from_projection_value(&fixture(kind)).unwrap()
    }
    fn state() -> UiState {
        let mut state = UiState::new("fake".into(), Some("model-1".into()), None);
        state.selected_session = Some(SessionIdentity {
            session_id: "session-1".into(),
            session_path: "/session-1.jsonl".into(),
            session_name: None,
        });
        state
    }
    fn finish(id: &str, kind: &str, ok: bool) -> UiAction {
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: kind.into(),
            ok,
            error: (!ok).then(|| "backend failure".into()),
        })
    }
    fn commands(effects: &[UiEffect]) -> Vec<Value> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                UiEffect::SendCommand(command) | UiEffect::SendPostPromptSessionSync(command) => {
                    Some(command.to_value().unwrap())
                }
                _ => None,
            })
            .collect()
    }
    fn report(state: &mut UiState, ids: &mut Ids, id: &str) {
        let mut value = fixture("session.stats");
        value["command_id"] = json!(id);
        reduce(
            state,
            UiAction::BackendEvent(BackendEvent::from_projection_value(&value).unwrap()),
            ids,
        )
        .unwrap();
    }

    #[test]
    fn explicit_refresh_is_correlated_coalesced_and_gates_submission_until_terminal() {
        let mut state = state();
        let mut ids = Ids::default();
        let effects = reduce(&mut state, UiAction::LoadContext, &mut ids).unwrap();
        let id = commands(&effects)[0]["id"].as_str().unwrap().to_owned();
        assert!(state.editor_editable());
        assert!(commands(&reduce(&mut state, UiAction::LoadContext, &mut ids).unwrap()).is_empty());
        assert!(
            commands(&reduce(&mut state, UiAction::Submit("draft".into()), &mut ids).unwrap())
                .is_empty()
        );
        report(&mut state, &mut ids, "wrong-id");
        assert!(state.context.stats.is_none());
        report(&mut state, &mut ids, &id);
        assert!(state.context.stats.is_none());
        assert!(
            commands(
                &reduce(&mut state, finish(&id, "get_session_stats", true), &mut ids).unwrap()
            )
            .is_empty()
        );
        assert!(state.context.stats.is_some());
        assert!(!state.context.loading());
        assert!(!state.context.stats_stale);
        assert_eq!(
            commands(&reduce(&mut state, UiAction::Submit("draft".into()), &mut ids).unwrap())[0]["type"],
            "prompt"
        );
    }

    #[test]
    fn failed_missing_duplicate_and_stale_stats_release_ownership_on_terminal() {
        for scenario in [
            "failed",
            "missing",
            "duplicate",
            "stale",
            "wrong-session",
            "skip",
        ] {
            let mut state = state();
            let mut ids = Ids::default();
            reduce(&mut state, UiAction::LoadContext, &mut ids).unwrap();
            let id = "get_session_stats-1";
            if !matches!(scenario, "missing" | "skip") {
                report(&mut state, &mut ids, id);
            }
            if scenario == "duplicate" {
                report(&mut state, &mut ids, id);
                assert!(state.context.loading());
            }
            if scenario == "stale" {
                state.context.invalidate();
                // Scope invalidation cannot release the still-running backend command.
                assert!(state.context.loading());
            }
            if scenario == "wrong-session" {
                state
                    .context
                    .read
                    .as_mut()
                    .unwrap()
                    .report
                    .as_mut()
                    .unwrap()
                    .session_id = Some("other".into());
            }
            let action = if scenario == "skip" {
                UiAction::SkipStatsRefresh {
                    command_id: id.into(),
                }
            } else {
                finish(id, "get_session_stats", scenario != "failed")
            };
            let effects = reduce(&mut state, action, &mut ids).unwrap();
            assert!(state.context.stats.is_none(), "{scenario}");
            assert!(
                commands(&effects)
                    .iter()
                    .all(|command| command["type"] != "get_messages")
            );
            assert_eq!(state.context.loading(), scenario == "stale");
            // A late report from a completed read is inert, including after a replacement read.
            report(&mut state, &mut ids, id);
            assert!(state.context.stats.is_none());
        }
    }

    #[test]
    fn post_operation_refresh_always_hands_off_once_and_keeps_stats_through_metadata_sync() {
        for outcome in ["ok", "failed", "missing", "skip"] {
            let mut state = state();
            let mut ids = Ids::default();
            reduce(
                &mut state,
                UiAction::Submit("keep conversation".into()),
                &mut ids,
            )
            .unwrap();
            reduce(&mut state, finish("prompt-1", "prompt", true), &mut ids).unwrap();
            if outcome == "ok" {
                report(&mut state, &mut ids, "get_session_stats-1");
            }
            let action = if outcome == "skip" {
                UiAction::SkipStatsRefresh {
                    command_id: "get_session_stats-1".into(),
                }
            } else {
                finish(
                    "get_session_stats-1",
                    "get_session_stats",
                    outcome != "failed",
                )
            };
            let effects = reduce(&mut state, action, &mut ids).unwrap();
            assert_eq!(commands(&effects).len(), 1);
            assert_eq!(commands(&effects)[0]["type"], "get_messages");
            assert!(
                commands(
                    &reduce(
                        &mut state,
                        finish("get_session_stats-1", "get_session_stats", true),
                        &mut ids
                    )
                    .unwrap()
                )
                .is_empty()
            );
            let before = state.context.stats.clone();
            let messages = SessionMessages {
                session: state.selected_session.clone(),
                active_leaf_id: Some("new-leaf".into()),
                truncated: false,
                next_before_entry_id: None,
                next_after_entry_id: None,
                durable_entry_ids: vec![],
                exact_tool_result: None,
                transcript: SharedTranscript::default(),
            };
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::MessagesReported {
                    command_id: "get_messages-1".into(),
                    messages,
                }),
                &mut ids,
            )
            .unwrap();
            assert!(
                commands(
                    &reduce(
                        &mut state,
                        finish("get_messages-1", "get_messages", true),
                        &mut ids
                    )
                    .unwrap()
                )
                .is_empty()
            );
            assert_eq!(state.context.stats, before);
            assert_eq!(
                state.transcript.latest_user_text(),
                Some("keep conversation")
            );
            assert!(!state.post_prompt_session_sync_pending);
        }
    }

    #[test]
    fn automatic_compaction_and_cached_context_keep_prompt_and_queue_ownership() {
        for reason in ["threshold", "overflow"] {
            let mut state = state();
            let mut ids = Ids::default();
            reduce(&mut state, UiAction::Submit("prompt".into()), &mut ids).unwrap();
            let active = state.current_command.clone();
            let before = state.transcript.clone();
            assert!(
                commands(&reduce(&mut state, UiAction::LoadContext, &mut ids).unwrap()).is_empty()
            );
            reduce(
                &mut state,
                UiAction::BackendEvent(event("context.estimated")),
                &mut ids,
            )
            .unwrap();
            assert!(state.context.budget.is_some());
            let mut started = fixture("compaction.started");
            started["reason"] = json!(reason);
            assert!(
                commands(
                    &reduce(
                        &mut state,
                        UiAction::BackendEvent(
                            BackendEvent::from_projection_value(&started).unwrap()
                        ),
                        &mut ids
                    )
                    .unwrap()
                )
                .is_empty()
            );
            assert!(state.active_prompt_editable());
            assert_eq!(state.current_command, active);
            let mut completed = fixture("compaction.completed");
            completed["reason"] = json!(reason);
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::from_projection_value(&completed).unwrap()),
                &mut ids,
            )
            .unwrap();
            assert_eq!(state.current_command, active);
            assert_eq!(state.transcript, before);
            assert!(state.context.budget.is_none());
            assert!(
                state
                    .context
                    .compaction_notice
                    .as_deref()
                    .unwrap()
                    .contains("Retry setup failed")
            );
            assert!(
                !state
                    .context
                    .compaction_notice
                    .as_deref()
                    .unwrap()
                    .contains("Retrying")
            );
            let effects = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
            assert_eq!(commands(&effects)[0]["target_id"], "prompt-1");
        }
    }

    #[test]
    fn manual_compaction_can_fail_before_started_or_cancel_during_trust_and_recover() {
        let mut state = state();
        let mut ids = Ids::default();
        let effects = reduce(
            &mut state,
            UiAction::Compact(Some("Keep constraints".into())),
            &mut ids,
        )
        .unwrap();
        assert_eq!(commands(&effects)[0]["instructions"], "Keep constraints");
        assert_eq!(state.interaction_status, InteractionStatus::Compacting);
        assert!(!state.editor_editable());
        reduce(&mut state, finish("compact-1", "compact", false), &mut ids).unwrap();
        assert_eq!(state.view_status, ViewStatus::Idle);
        assert!(state.current_command.is_none());
        assert!(
            state
                .context
                .compaction_notice
                .as_ref()
                .unwrap()
                .contains("backend failure")
        );
        let mut state = super::tests::state();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::Compact(None), &mut ids).unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::TrustRequested {
                request_id: "trust-1".into(),
                project_path: "/project".into(),
            }),
            &mut ids,
        )
        .unwrap();
        let effects = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        assert_eq!(commands(&effects)[0]["type"], "trust");
        assert_eq!(commands(&effects)[0]["trusted"], false);
        assert_eq!(state.interaction_status, InteractionStatus::Compacting);
        let effects = reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        assert!(
            commands(&effects)
                .iter()
                .any(|command| command["type"] == "cancel" && command["target_id"] == "compact-1")
        );
        assert_eq!(state.current_command.as_ref().unwrap().id, "compact-1");
    }

    #[test]
    fn session_change_clears_compaction_and_rejects_previous_session_stats() {
        let mut state = state();
        let mut ids = Ids::default();
        state.context.compaction_notice = Some("old session compaction failed".into());
        reduce(&mut state, UiAction::NewSession, &mut ids).unwrap();
        let effects = reduce(
            &mut state,
            finish("new_session-1", "new_session", true),
            &mut ids,
        )
        .unwrap();
        assert!(state.context.compaction_notice.is_none());
        assert!(state.context.budget.is_none());
        assert_eq!(
            commands(&effects)
                .iter()
                .filter(|command| command["type"] == "get_session_stats")
                .count(),
            1
        );
        report(&mut state, &mut ids, "get_session_stats-1");
        reduce(
            &mut state,
            finish("get_session_stats-1", "get_session_stats", true),
            &mut ids,
        )
        .unwrap();
        assert!(state.context.stats.is_none());
        assert!(!state.context.loading());
    }

    #[test]
    fn first_prompt_and_unavailable_model_catalog_still_receive_live_context() {
        let mut state = state();
        let mut ids = Ids::default();
        state.selected_session = None;
        state.provider = Some("old-provider".into());
        state.model_selection_stale = true;
        reduce(
            &mut state,
            UiAction::Submit("first prompt".into()),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(event("context.estimated")),
            &mut ids,
        )
        .unwrap();
        assert!(state.context.budget.is_some());
        assert_eq!(state.provider.as_deref(), Some("old-provider"));
        reduce(
            &mut state,
            UiAction::BackendEvent(event("compaction.started")),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.context.compaction, Some(CompactionReason::Overflow));
        reduce(&mut state, finish("prompt-1", "prompt", true), &mut ids).unwrap();
        report(&mut state, &mut ids, "get_session_stats-1");
        reduce(
            &mut state,
            finish("get_session_stats-1", "get_session_stats", true),
            &mut ids,
        )
        .unwrap();
        assert_eq!(
            state.context.stats.as_ref().unwrap().session_id.as_deref(),
            Some("session-1")
        );
    }

    #[test]
    fn manual_success_does_not_release_operation_before_rpc_terminal() {
        let mut state = state();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::Compact(None), &mut ids).unwrap();
        let mut completed = fixture("compaction.completed");
        completed["reason"] = json!("manual");
        completed["error"] = Value::Null;
        let effects = reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::from_projection_value(&completed).unwrap()),
            &mut ids,
        )
        .unwrap();
        assert!(commands(&effects).is_empty());
        assert_eq!(state.current_command.as_ref().unwrap().id, "compact-1");
        assert!(
            state
                .context
                .compaction_notice
                .as_ref()
                .unwrap()
                .contains("2 entries replaced")
        );
        let effects = reduce(&mut state, finish("compact-1", "compact", true), &mut ids).unwrap();
        assert!(state.current_command.is_none());
        assert_eq!(commands(&effects)[0]["type"], "get_session_stats");
    }

    #[test]
    fn compaction_toggle_waits_for_ack_and_failed_change_keeps_confirmed_snapshot() {
        for ok in [false, true] {
            let mut state = state();
            let mut ids = Ids::default();
            state.context.stats = Some(Box::new(
                serde_json::from_value(fixture("session.stats")["stats"].clone()).unwrap(),
            ));
            reduce(
                &mut state,
                UiAction::ConfigureAutoCompaction(false),
                &mut ids,
            )
            .unwrap();
            assert!(
                state
                    .context
                    .stats
                    .as_ref()
                    .unwrap()
                    .compaction
                    .as_ref()
                    .unwrap()
                    .auto_compaction_enabled
            );
            assert!(state.configuration_active());
            let effects =
                reduce(&mut state, finish("configure-1", "configure", ok), &mut ids).unwrap();
            assert!(!state.configuration_active());
            assert_eq!(state.context.loading(), ok);
            assert_eq!(commands(&effects).len(), usize::from(ok));
            assert_eq!(state.context.stats.is_none(), ok);
        }
    }
}
