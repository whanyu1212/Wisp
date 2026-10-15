//! Nonblocking skill and MCP snapshots, independent of the active runtime command.

use super::*;
use std::sync::Arc;
use wisp_protocol::events::{McpServerPreview, McpStatusSnapshot, SkillCatalogSnapshot};

pub type SkillReport = Result<Arc<SkillCatalogSnapshot>, String>;
pub type McpReport = Result<Arc<McpStatusSnapshot>, String>;
pub type McpPreviewReport = Result<Arc<Vec<McpServerPreview>>, String>;

/// Commands answered with an `rpc.mcp` status report under their own ID. A
/// refresh reports nothing when it fails, so treating it as a change is harmless.
const MCP_STATUS_COMMANDS: [&str; 7] = [
    "get_mcp_status",
    "add_mcp_server",
    "remove_mcp_server",
    "set_mcp_server_enabled",
    "reconnect_mcp",
    "login_mcp_server",
    "logout_mcp_server",
];

/// Longest sign-in page URL shown; the backend sends at most this many characters.
const MCP_SIGN_IN_URL_MAX_BYTES: usize = 8_192 * 4;

/// An MCP sign-in waiting for the user to finish it in their browser.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct McpSignInState {
    pub command_id: String,
    pub name: String,
    /// The sign-in page, once the backend opened it.
    pub url: Option<String>,
    pub cancel_requested: bool,
}

#[derive(Clone, Debug, PartialEq)]
struct PendingInspection<T> {
    id: String,
    report: Option<Result<Arc<T>, String>>,
    superseded: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Inspection<T> {
    pub snapshot: Option<Arc<T>>,
    pub error: Option<String>,
    pending: Option<PendingInspection<T>>,
}

impl<T> Default for Inspection<T> {
    fn default() -> Self {
        Self {
            snapshot: None,
            error: None,
            pending: None,
        }
    }
}

impl<T> Inspection<T> {
    pub fn loading(&self) -> bool {
        self.pending.is_some()
    }

    fn start(&mut self, id: String) {
        self.pending = Some(PendingInspection {
            id,
            report: None,
            superseded: false,
        });
        self.error = None;
    }

    fn report(&mut self, id: &str, report: &Result<Arc<T>, String>) {
        if let Some(pending) = self.pending.as_mut().filter(|pending| pending.id == id) {
            pending.report = Some(if pending.report.is_some() {
                Err("Discovery returned duplicate reports. Press r to retry.".into())
            } else {
                report.clone()
            });
        }
    }

    /// Finish a change that may have reported the state it reached before
    /// failing: an MCP change can save its settings and then fail to restart.
    /// That state replaces the older snapshot, alongside the error.
    fn finish_change(&mut self, id: &str, ok: bool, error: Option<&str>) -> bool {
        if !ok {
            if let Some(Ok(snapshot)) = self
                .pending
                .as_ref()
                .filter(|pending| pending.id == id && !pending.superseded)
                .and_then(|pending| pending.report.clone())
            {
                self.snapshot = Some(snapshot);
            }
        }
        self.finish(id, ok, error)
    }

    fn finish(&mut self, id: &str, ok: bool, error: Option<&str>) -> bool {
        if self.pending.as_ref().is_none_or(|pending| pending.id != id) {
            return false;
        }
        let pending = self.pending.take().expect("matched inspection");
        // A broadcast describes newer backend state than this request can confirm.
        if pending.superseded {
            return true;
        }
        let report = if ok {
            pending
                .report
                .unwrap_or_else(|| Err("Discovery returned no report. Press r to retry.".into()))
        } else {
            Err(bounded_session_text(
                error.unwrap_or("Discovery failed. Press r to retry."),
                SESSION_NOTICE_MAX_BYTES,
            ))
        };
        match report {
            Ok(snapshot) => {
                self.snapshot = Some(snapshot);
                self.error = None;
            }
            Err(error) => self.error = Some(error),
        }
        true
    }

    pub(super) fn close(&mut self) {
        if self.pending.take().is_some() {
            self.error = Some("Connection closed before discovery finished.".into());
        }
    }
}

pub(super) fn load_skills(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.skills.loading() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetSkills);
    let command = WispTypedClientRpcCommands::get_skills(&id)?;
    state.skills.start(id);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn load_mcp(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.mcp.loading() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetMcpStatus);
    let command = WispTypedClientRpcCommands::get_mcp_status(&id)?;
    state.mcp.start(id);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

/// Ask the backend to parse a pasted snippet. The snippet travels only in a
/// secret command and is not kept; the reply replaces any earlier preview.
pub(super) fn preview_mcp_snippet(
    state: &mut UiState,
    snippet: SecretText,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.mcp_preview.loading() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::PreviewMcpSnippet);
    let command = WispTypedClientRpcCommands::preview_mcp_snippet(&id, snippet.as_str())?;
    drop(snippet);
    state.mcp_preview.start(id);
    state.mcp_preview.snapshot = None;
    Ok(vec![
        UiEffect::SendSecretCommand(SecretCommand(command)),
        UiEffect::RequestRender,
    ])
}

/// Send one MCP server change. The reply is the MCP status after the backend
/// restarted its servers, reported like a refresh.
pub(super) fn change_mcp(
    state: &mut UiState,
    change: McpChange,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.exit_requested {
        return Ok(Vec::new());
    }
    if state.mcp.loading() {
        return Ok(vec![UiEffect::Notice(
            "Wait for the current MCP update to finish.".into(),
        )]);
    }
    // The backend would queue the change behind the prompt; refusing here keeps
    // the panel from appearing to hang.
    if !state.can_select_model() || super::session_sync_pending(state) {
        return Ok(vec![UiEffect::Notice(
            "Finish the current operation before changing MCP servers.".into(),
        )]);
    }
    let id = ids.next_id(CommandKind::ChangeMcpServers);
    let effect = match change {
        McpChange::Add {
            name,
            launch,
            env,
            headers,
            replace,
        } => {
            let reveal = |values: &[(String, SecretText)]| -> Vec<(String, String)> {
                values
                    .iter()
                    .map(|(key, value)| (key.clone(), value.as_str().to_owned()))
                    .collect()
            };
            let (env, headers) = (reveal(&env), reveal(&headers));
            let command = WispTypedClientRpcCommands::add_mcp_server(
                &id, &name, &launch, &env, &headers, replace,
            )?;
            drop((env, headers));
            UiEffect::SendSecretCommand(SecretCommand(command))
        }
        McpChange::Remove(name) => {
            UiEffect::SendCommand(WispTypedClientRpcCommands::remove_mcp_server(&id, &name)?)
        }
        McpChange::SetEnabled {
            name,
            enabled,
            expected,
        } => UiEffect::SendCommand(WispTypedClientRpcCommands::set_mcp_server_enabled(
            &id,
            &name,
            enabled,
            expected.as_ref(),
        )?),
        McpChange::Reconnect => {
            UiEffect::SendCommand(WispTypedClientRpcCommands::reconnect_mcp(&id)?)
        }
        McpChange::Login(name) => {
            let command = WispTypedClientRpcCommands::login_mcp_server(&id, &name)?;
            state.mcp_sign_in = Some(McpSignInState {
                command_id: id.clone(),
                name,
                url: None,
                cancel_requested: false,
            });
            UiEffect::SendCommand(command)
        }
        McpChange::Logout(name) => {
            UiEffect::SendCommand(WispTypedClientRpcCommands::logout_mcp_server(&id, &name)?)
        }
    };
    state.mcp.start(id);
    Ok(vec![effect, UiEffect::RequestRender])
}

/// Stop waiting for the MCP sign-in in progress, if any.
pub(super) fn cancel_mcp_sign_in(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let Some(sign_in) = state
        .mcp_sign_in
        .as_mut()
        .filter(|sign_in| !sign_in.cancel_requested)
    else {
        return Ok(Vec::new());
    };
    let id = ids.next_id(CommandKind::Cancel);
    let command = WispTypedClientRpcCommands::cancel(&id, &sign_in.command_id)?;
    sign_in.cancel_requested = true;
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn permissions(
    state: &mut UiState,
    mode: Option<wisp_protocol::commands::PermissionMode>,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.permissions.loading() || state.exit_requested {
        return Ok(Vec::new());
    }
    if mode.is_some() && (!state.can_select_model() || super::session_sync_pending(state)) {
        return Ok(vec![UiEffect::Notice(
            "Finish the current operation before changing permissions.".into(),
        )]);
    }
    let id = ids.next_id(CommandKind::Permissions);
    let command = WispTypedClientRpcCommands::permissions(&id, mode)?;
    state.permissions.start(id);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn observe(state: &mut UiState, event: &BackendEvent) -> Option<Vec<UiEffect>> {
    match event {
        BackendEvent::PermissionsReported {
            command_id,
            permissions,
        } => {
            state.permissions.report(command_id, permissions);
            Some(Vec::new())
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if matches!(command_type.as_str(), "get_permissions" | "set_permissions")
            && state.permissions.finish(command_id, *ok, error.as_deref()) =>
        {
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::SkillCatalogReported {
            command_id,
            catalog,
        } => {
            state.skills.report(command_id, catalog);
            Some(Vec::new())
        }
        BackendEvent::McpStatusReported { command_id, status } => {
            state.mcp.report(command_id, status);
            Some(Vec::new())
        }
        BackendEvent::McpSnippetPreviewed {
            command_id,
            servers,
        } => {
            state.mcp_preview.report(command_id, servers);
            Some(Vec::new())
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_type == "preview_mcp_snippet"
            && state.mcp_preview.finish(command_id, *ok, error.as_deref()) =>
        {
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::SkillCatalogUpdated(catalog) => {
            if let Some(pending) = &mut state.skills.pending {
                pending.superseded = true;
            }
            match catalog {
                Ok(catalog) => {
                    state.skills.snapshot = Some(Arc::clone(catalog));
                    state.skills.error = None;
                }
                Err(error) => {
                    // Old choices no longer describe the active catalog.
                    state.skills.snapshot = None;
                    state.skills.error = Some(error.clone());
                }
            }
            Some(vec![UiEffect::SkillCatalogChanged, UiEffect::RequestRender])
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_type == "get_skills"
            && state.skills.finish(command_id, *ok, error.as_deref()) =>
        {
            Some(vec![UiEffect::SkillCatalogChanged, UiEffect::RequestRender])
        }
        BackendEvent::McpLoginStarted {
            command_id,
            authorization_url,
            ..
        } => {
            if let Some(sign_in) = state
                .mcp_sign_in
                .as_mut()
                .filter(|sign_in| &sign_in.command_id == command_id)
            {
                sign_in.url = Some(super::bounded_session_text(
                    authorization_url,
                    MCP_SIGN_IN_URL_MAX_BYTES,
                ));
            }
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if MCP_STATUS_COMMANDS.contains(&command_type.as_str()) => {
            if state
                .mcp_sign_in
                .as_ref()
                .is_some_and(|sign_in| &sign_in.command_id == command_id)
            {
                state.mcp_sign_in = None;
            }
            state
                .mcp
                .finish_change(command_id, *ok, error.as_deref())
                .then(|| vec![UiEffect::RequestRender])
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Ids(usize);
    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }
    fn catalog(trusted: bool) -> Arc<SkillCatalogSnapshot> {
        Arc::new(SkillCatalogSnapshot {
            entries: vec![],
            diagnostics: vec![],
            project_trusted: trusted,
        })
    }
    fn finished(id: &str, kind: &str, ok: bool) -> BackendEvent {
        BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: kind.into(),
            ok,
            error: Some("inspection failed".into()),
        }
    }
    fn fixture(kind: &str) -> Value {
        serde_json::from_str::<Value>(include_str!(
            "../../../../tests/fixtures/rust_tui_discovery.json"
        ))
        .unwrap()[kind]
            .clone()
    }

    #[test]
    fn inspections_refresh_during_prompt_without_taking_command_or_queue_ownership() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::Submit("work".into()), &mut ids).unwrap();
        reduce(&mut state, UiAction::FollowUp("later".into()), &mut ids).unwrap();
        let active = state.current_command.clone();
        let queue = state.queue.clone();
        for (action, kind, event_kind) in [
            (UiAction::LoadSkills, "get_skills", "rpc.skills"),
            (UiAction::LoadMcpStatus, "get_mcp_status", "rpc.mcp"),
        ] {
            let effects = reduce(&mut state, action.clone(), &mut ids).unwrap();
            let UiEffect::SendCommand(command) = &effects[0] else {
                panic!("expected inspection");
            };
            let value = command.to_value().unwrap();
            assert_eq!(value["type"], kind);
            let id = value["id"].as_str().unwrap();
            assert!(reduce(&mut state, action, &mut ids).unwrap().is_empty());
            let mut report = fixture(event_kind);
            report["command_id"] = id.into();
            reduce(
                &mut state,
                UiAction::BackendEvent(BackendEvent::from_projection_value(&report).unwrap()),
                &mut ids,
            )
            .unwrap();
            reduce(
                &mut state,
                UiAction::BackendEvent(finished(id, kind, true)),
                &mut ids,
            )
            .unwrap();
            assert_eq!(state.current_command, active);
            assert_eq!(state.queue, queue);
            assert_eq!(state.view_status, ViewStatus::Running);
            assert!(state.editor_editable());
        }
        assert!(state.skills.snapshot.is_some());
        assert!(state.mcp.snapshot.is_some());
        reduce(&mut state, UiAction::Cancel, &mut ids).unwrap();
        assert!(state.cancel_requested);
        assert_eq!(state.current_command, active);
    }

    #[test]
    fn authoritative_update_wins_over_older_report_and_terminal_in_either_order() {
        for report_first in [false, true] {
            let mut state = UiState::unconfigured();
            let mut ids = Ids::default();
            load_skills(&mut state, &mut ids).unwrap();
            let report = BackendEvent::SkillCatalogReported {
                command_id: "get_skills-1".into(),
                catalog: Ok(catalog(false)),
            };
            if report_first {
                observe(&mut state, &report);
            }
            observe(
                &mut state,
                &BackendEvent::SkillCatalogUpdated(Ok(catalog(true))),
            );
            if !report_first {
                observe(&mut state, &report);
            }
            observe(&mut state, &finished("get_skills-1", "get_skills", true));
            assert!(state.skills.snapshot.as_ref().unwrap().project_trusted);
            assert!(!state.skills.loading());
            assert!(state.skills.error.is_none());
        }
    }

    #[test]
    fn failed_missing_duplicate_and_mismatched_reports_preserve_last_snapshot_and_release_on_terminal()
     {
        for failure in ["failed", "missing", "duplicate", "wrong-id", "oversized"] {
            let mut state = UiState::unconfigured();
            let old = catalog(false);
            state.skills.snapshot = Some(old.clone());
            let mut ids = Ids::default();
            load_skills(&mut state, &mut ids).unwrap();
            if failure != "missing" {
                let event = BackendEvent::SkillCatalogReported {
                    command_id: if failure == "wrong-id" {
                        "other"
                    } else {
                        "get_skills-1"
                    }
                    .into(),
                    catalog: if failure == "oversized" {
                        Err("oversized".into())
                    } else {
                        Ok(catalog(true))
                    },
                };
                observe(&mut state, &event);
                if failure == "duplicate" {
                    observe(&mut state, &event);
                }
            }
            observe(&mut state, &finished("other", "get_skills", true));
            observe(
                &mut state,
                &finished("get_skills-1", "get_mcp_status", true),
            );
            assert!(state.skills.loading());
            observe(
                &mut state,
                &finished("get_skills-1", "get_skills", failure != "failed"),
            );
            assert!(!state.skills.loading(), "{failure}");
            assert!(
                Arc::ptr_eq(state.skills.snapshot.as_ref().unwrap(), &old),
                "{failure}"
            );
            assert!(state.skills.error.is_some(), "{failure}");
            assert!(
                load_skills(&mut state, &mut ids)
                    .unwrap()
                    .iter()
                    .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
            );
            observe(&mut state, &finished("get_skills-1", "get_skills", true));
            assert!(state.skills.loading());
            reduce(
                &mut state,
                UiAction::TransportClosed { error: None },
                &mut ids,
            )
            .unwrap();
            assert!(!state.skills.loading());
            assert!(
                state
                    .skills
                    .error
                    .as_ref()
                    .unwrap()
                    .contains("Connection closed")
            );
        }
    }

    #[test]
    fn oversized_reports_are_recoverable_and_oversized_broadcast_retires_old_choices() {
        let mut value = fixture("rpc.skills");
        value["catalog"]["entries"][0]["description"] = "x"
            .repeat(super::super::event_projection::DISCOVERY_REPORT_MAX_BYTES)
            .into();
        let BackendEvent::SkillCatalogReported {
            catalog: report, ..
        } = BackendEvent::from_projection_value(&value).unwrap()
        else {
            panic!("expected catalog");
        };
        assert!(report.unwrap_err().contains("1 MiB"));
        let mut state = UiState::unconfigured();
        state.skills.snapshot = Some(catalog(true));
        value["type"] = "skill.catalog.updated".into();
        value.as_object_mut().unwrap().remove("command_id");
        observe(
            &mut state,
            &BackendEvent::from_projection_value(&value).unwrap(),
        );
        assert!(state.skills.snapshot.is_none());
        assert!(state.skills.error.as_ref().unwrap().contains("1 MiB"));
        assert_eq!(state.view_status, ViewStatus::Idle);
    }

    fn ready_state() -> UiState {
        let mut state = UiState::unconfigured();
        state.input_ready = true;
        state
    }

    fn sent(effects: &[UiEffect]) -> Value {
        match &effects[0] {
            UiEffect::SendCommand(command) => command.to_value().unwrap(),
            UiEffect::SendSecretCommand(command) => {
                assert!(!format!("{command:?}").contains("sentinel-value"));
                serde_json::to_value(command.0.clone()).unwrap()
            }
            effect => panic!("expected a command, got {effect:?}"),
        }
    }

    #[test]
    fn mcp_changes_report_status_under_their_own_id() {
        let mut state = ready_state();
        let mut ids = Ids::default();
        let launch = wisp_protocol::events::McpServerLaunch {
            command: Some("npx".into()),
            args: vec!["server".into()],
            ..Default::default()
        };
        let secret = SecretText::new("sentinel-value".into(), 64).unwrap();
        let add = UiAction::ChangeMcpServers(McpChange::Add {
            name: "docs".into(),
            launch: launch.clone(),
            env: vec![("TOKEN".into(), secret)],
            headers: vec![],
            replace: false,
        });
        assert!(!format!("{add:?}").contains("sentinel-value"));
        let effects = reduce(&mut state, add, &mut ids).unwrap();
        assert!(matches!(effects[0], UiEffect::SendSecretCommand(_)));
        let value = sent(&effects);
        assert_eq!(value["type"], "add_mcp_server");
        assert_eq!(value["env"]["TOKEN"], "sentinel-value");
        let id = value["id"].as_str().unwrap().to_owned();
        assert!(state.mcp.loading());

        // A second change waits for the first.
        let effects = reduce(
            &mut state,
            UiAction::ChangeMcpServers(McpChange::Reconnect),
            &mut ids,
        )
        .unwrap();
        assert!(matches!(&effects[0], UiEffect::Notice(notice) if notice.contains("Wait")));

        let mut report = fixture("rpc.mcp");
        report["command_id"] = id.as_str().into();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::from_projection_value(&report).unwrap()),
            &mut ids,
        )
        .unwrap();
        let mut done = finished(&id, "add_mcp_server", true);
        if let BackendEvent::CommandFinished { error, .. } = &mut done {
            *error = None;
        }
        reduce(&mut state, UiAction::BackendEvent(done), &mut ids).unwrap();
        assert!(!state.mcp.loading());
        assert_eq!(state.mcp.snapshot.as_ref().unwrap().servers.len(), 4);

        for (change, kind) in [
            (McpChange::Remove("docs".into()), "remove_mcp_server"),
            (
                McpChange::SetEnabled {
                    name: "docs".into(),
                    enabled: true,
                    expected: Some(launch.clone()),
                },
                "set_mcp_server_enabled",
            ),
            (McpChange::Reconnect, "reconnect_mcp"),
        ] {
            let effects = reduce(&mut state, UiAction::ChangeMcpServers(change), &mut ids).unwrap();
            let value = sent(&effects);
            assert_eq!(value["type"], kind);
            let id = value["id"].as_str().unwrap();
            reduce(
                &mut state,
                UiAction::BackendEvent(finished(id, kind, false)),
                &mut ids,
            )
            .unwrap();
            assert!(!state.mcp.loading());
            assert_eq!(state.mcp.error.as_deref(), Some("inspection failed"));
        }
    }

    #[test]
    fn mcp_changes_wait_for_the_current_operation() {
        let mut state = ready_state();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::Submit("work".into()), &mut ids).unwrap();
        let effects = reduce(
            &mut state,
            UiAction::ChangeMcpServers(McpChange::Remove("docs".into())),
            &mut ids,
        )
        .unwrap();
        assert!(
            matches!(&effects[..], [UiEffect::Notice(notice)] if notice.contains("Finish the current operation"))
        );
        assert!(!state.mcp.loading());
    }

    #[test]
    fn a_snippet_preview_is_sent_secretly_and_replaces_the_last_one() {
        let mut state = ready_state();
        let mut ids = Ids::default();
        let snippet = SecretText::new("{\"sentinel-value\": 1}".into(), 1024).unwrap();
        let action = UiAction::PreviewMcpSnippet(snippet);
        assert!(!format!("{action:?}").contains("sentinel-value"));
        let effects = reduce(&mut state, action, &mut ids).unwrap();
        assert!(matches!(effects[0], UiEffect::SendSecretCommand(_)));
        let value = sent(&effects);
        assert_eq!(value["type"], "preview_mcp_snippet");
        let id = value["id"].as_str().unwrap().to_owned();
        let mut report = fixture("rpc.mcp.preview");
        report["command_id"] = id.as_str().into();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::from_projection_value(&report).unwrap()),
            &mut ids,
        )
        .unwrap();
        let mut done = finished(&id, "preview_mcp_snippet", true);
        if let BackendEvent::CommandFinished { error, .. } = &mut done {
            *error = None;
        }
        reduce(&mut state, UiAction::BackendEvent(done), &mut ids).unwrap();
        let preview = state.mcp_preview.snapshot.as_deref().unwrap();
        assert_eq!(preview[0].name, "github");

        // A new preview drops the old result before its own reply arrives.
        let snippet = SecretText::new("{}".into(), 1024).unwrap();
        reduce(&mut state, UiAction::PreviewMcpSnippet(snippet), &mut ids).unwrap();
        assert!(state.mcp_preview.snapshot.is_none());
        assert!(state.mcp_preview.loading());
    }

    #[test]
    fn a_change_that_failed_after_reporting_shows_the_reported_state() {
        let mut state = ready_state();
        let mut ids = Ids::default();
        let effects = reduce(
            &mut state,
            UiAction::ChangeMcpServers(McpChange::Reconnect),
            &mut ids,
        )
        .unwrap();
        let id = sent(&effects)["id"].as_str().unwrap().to_owned();
        let mut report = fixture("rpc.mcp");
        report["command_id"] = id.as_str().into();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::from_projection_value(&report).unwrap()),
            &mut ids,
        )
        .unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(finished(&id, "reconnect_mcp", false)),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.mcp.snapshot.as_ref().unwrap().servers.len(), 4);
        assert_eq!(state.mcp.error.as_deref(), Some("inspection failed"));
    }

    #[test]
    fn a_sign_in_shows_its_page_until_it_finishes_and_can_be_cancelled() {
        let mut state = ready_state();
        let mut ids = Ids::default();

        let effects = reduce(
            &mut state,
            UiAction::ChangeMcpServers(McpChange::Login("linear".into())),
            &mut ids,
        )
        .unwrap();
        let value = sent(&effects);
        assert_eq!(value["type"], "login_mcp_server");
        assert_eq!(value["name"], "linear");
        let id = value["id"].as_str().unwrap().to_owned();
        assert_eq!(state.mcp_sign_in.as_ref().unwrap().url, None);

        let started = serde_json::json!({
            "type": "rpc.mcp.login",
            "timestamp": "2026-10-16T00:00:00Z",
            "command_id": id,
            "name": "linear",
            "authorization_url": "https://auth.example.test/authorize?state=abc",
        });
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::from_projection_value(&started).unwrap()),
            &mut ids,
        )
        .unwrap();
        assert_eq!(
            state.mcp_sign_in.as_ref().unwrap().url.as_deref(),
            Some("https://auth.example.test/authorize?state=abc")
        );

        let effects = reduce(&mut state, UiAction::CancelMcpSignIn, &mut ids).unwrap();
        let cancel = sent(&effects);
        assert_eq!(cancel["type"], "cancel");
        assert_eq!(cancel["target_id"], id.as_str());
        // A second Esc sends nothing more.
        assert!(
            reduce(&mut state, UiAction::CancelMcpSignIn, &mut ids)
                .unwrap()
                .is_empty()
        );

        reduce(
            &mut state,
            UiAction::BackendEvent(finished(&id, "login_mcp_server", false)),
            &mut ids,
        )
        .unwrap();
        assert!(state.mcp_sign_in.is_none());
        assert!(!state.mcp.loading());

        let effects = reduce(
            &mut state,
            UiAction::ChangeMcpServers(McpChange::Logout("linear".into())),
            &mut ids,
        )
        .unwrap();
        assert_eq!(sent(&effects)["type"], "logout_mcp_server");
    }

    #[test]
    fn a_prompt_waits_for_a_sign_in_instead_of_queueing_behind_it() {
        let mut state = ready_state();
        let mut ids = Ids::default();
        reduce(
            &mut state,
            UiAction::ChangeMcpServers(McpChange::Login("linear".into())),
            &mut ids,
        )
        .unwrap();

        let effects = reduce(&mut state, UiAction::Submit("hello".into()), &mut ids).unwrap();

        assert!(
            matches!(&effects[0], UiEffect::Notice(notice) if notice.contains("Finish signing in")),
            "{effects:?}"
        );
        assert!(
            !effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
    }
}
