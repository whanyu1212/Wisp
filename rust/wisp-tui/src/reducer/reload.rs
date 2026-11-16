//! Idle-only settings reload, correlated to the backend's acknowledged report.

use super::*;

#[derive(Clone, Debug, PartialEq, serde::Deserialize)]
pub struct ReloadSelection {
    pub provider: String,
    pub model: Option<String>,
    pub effort: Option<String>,
}

#[derive(Clone, Debug, PartialEq, serde::Deserialize)]
pub struct ReloadReport {
    pub applied: Vec<String>,
    pub unchanged: Vec<String>,
    pub overridden: Vec<String>,
    pub restart_required: Vec<String>,
    pub state: ReloadSelection,
}

pub(super) fn start(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.can_select_model() || super::session_sync_pending(state) {
        return Ok(vec![
            UiEffect::Notice(
                "Settings reload is idle-only; wait for the current operation.".into(),
            ),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(CommandKind::Reload);
    let command = WispTypedClientRpcCommands::reload(&id)?;
    state.settings_reload = Some((id, None));
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn observe(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    let Some((pending, report)) = &mut state.settings_reload else {
        return Ok(None);
    };
    match event {
        BackendEvent::SettingsReloaded {
            command_id,
            report: received,
        } if command_id == pending => {
            *report = Some(received.clone());
            Ok(Some(vec![]))
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_type == "reload" && command_id == pending => {
            let (_, report) = state.settings_reload.take().expect("matched reload");
            let Some(report) = report.filter(|_| *ok) else {
                return Ok(Some(vec![
                    UiEffect::Notice(bounded_session_text(
                        error
                            .as_deref()
                            .unwrap_or("Settings reload did not return a report."),
                        SESSION_NOTICE_MAX_BYTES,
                    )),
                    UiEffect::RequestRender,
                ]));
            };
            state.context.invalidate();
            state.codemode = Default::default();
            state.subagents = Default::default();
            let mut summary = vec!["Settings reloaded.".to_owned()];
            for (label, fields) in [
                ("Applied", &report.applied),
                ("Unchanged", &report.unchanged),
                ("Retained session choices", &report.overridden),
                ("Restart required", &report.restart_required),
            ] {
                if !fields.is_empty() {
                    summary.push(format!("{label}: {}.", fields.join(", ")));
                }
            }
            let mut effects = apply_project_config(
                state,
                report.state.provider,
                report.state.model,
                report.state.effort,
                ids,
            )?;
            effects.push(UiEffect::Notice(bounded_session_text(
                &summary.join(" "),
                SESSION_NOTICE_MAX_BYTES,
            )));
            effects.push(UiEffect::RequestRender);
            Ok(Some(effects))
        }
        _ => Ok(None),
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

    fn event(state: &mut UiState, ids: &mut Ids, mut value: serde_json::Value) -> Vec<UiEffect> {
        value["timestamp"] = "2026-01-02T03:04:05Z".into();
        let live = wisp_protocol::events::deserialize(value).unwrap();
        let event = BackendEvent::from_live(&live).unwrap();
        reduce(state, UiAction::BackendEvent(event), ids).unwrap()
    }

    fn report(id: &str) -> serde_json::Value {
        serde_json::json!({
            "type":"rpc.settings.reloaded", "command_id":id,
            "state": {"provider":"fake", "model":"after", "effort":null,
                "auto_compaction_enabled":false, "web_search":true,
                "steering_mode":"one_at_a_time", "follow_up_mode":"one_at_a_time",
                "pending_steering_count":0, "pending_follow_up_count":0},
            "applied":["model", "web_search"], "unchanged":["codemode"],
            "overridden":["effort"], "restart_required":["mcp_servers"],
        })
    }

    fn finished(ok: bool) -> serde_json::Value {
        serde_json::json!({"type":"rpc.command.finished", "command_id":"reload-1",
            "command_type":"reload", "ok":ok,
            "error":if ok { None } else { Some("Invalid settings; current configuration kept.") }})
    }

    fn notices(effects: &[UiEffect]) -> Vec<&str> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                UiEffect::Notice(text) => Some(text.as_str()),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn reload_waits_for_success_before_adopting_the_report() {
        let mut state = UiState::unconfigured();
        state.model = Some("before".into());
        let mut ids = Ids::default();
        let effects = reduce(&mut state, UiAction::ReloadSettings, &mut ids).unwrap();
        let command = effects
            .iter()
            .find_map(|e| match e {
                UiEffect::SendCommand(command) => Some(serde_json::to_value(command).unwrap()),
                _ => None,
            })
            .unwrap();
        assert_eq!(command["type"], "reload");
        assert!(state.configuration_active());
        event(&mut state, &mut ids, report("reload-1"));
        assert_eq!(state.model.as_deref(), Some("before"));
        let effects = event(&mut state, &mut ids, finished(true));
        assert_eq!(state.provider.as_deref(), Some("fake"));
        assert_eq!(state.model.as_deref(), Some("after"));
        assert!(!state.configuration_active());
        let text = notices(&effects).join(" ");
        assert!(text.contains("Applied: model, web_search"));
        assert!(text.contains("Restart required: mcp_servers"));
        assert!(text.contains("Retained session choices: effort"));
        assert!(state.context.stats_stale);
    }

    #[test]
    fn failed_or_missing_report_keeps_the_existing_selection() {
        for received in [false, true] {
            let mut state = UiState::unconfigured();
            state.model = Some("before".into());
            let mut ids = Ids::default();
            reduce(&mut state, UiAction::ReloadSettings, &mut ids).unwrap();
            if received {
                event(&mut state, &mut ids, report("reload-1"));
            }
            let effects = event(&mut state, &mut ids, finished(!received));
            assert_eq!(state.model.as_deref(), Some("before"));
            assert!(!state.configuration_active());
            assert!(!notices(&effects).is_empty());
        }
    }

    #[test]
    fn foreign_reports_are_ignored_and_a_second_reload_waits() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        event(&mut state, &mut ids, report("foreign"));
        assert!(state.model.is_none());
        reduce(&mut state, UiAction::ReloadSettings, &mut ids).unwrap();
        let effects = reduce(&mut state, UiAction::ReloadSettings, &mut ids).unwrap();
        assert!(notices(&effects)[0].contains("idle-only"));
        event(&mut state, &mut ids, report("foreign"));
        assert!(state.model.is_none());
    }

    #[test]
    fn active_run_cannot_reload() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        let effects = reduce(&mut state, UiAction::ReloadSettings, &mut Ids::default()).unwrap();
        assert!(notices(&effects)[0].contains("idle-only"));
        assert!(state.settings_reload.is_none());
    }
}
