//! Nonblocking skill and MCP snapshots, independent of the active runtime command.

use super::*;
use std::sync::Arc;
use wisp_protocol::events::{McpStatusSnapshot, SkillCatalogSnapshot};

pub type SkillReport = Result<Arc<SkillCatalogSnapshot>, String>;
pub type McpReport = Result<Arc<McpStatusSnapshot>, String>;

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

pub(super) fn observe(state: &mut UiState, event: &BackendEvent) -> Option<Vec<UiEffect>> {
    match event {
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
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_type == "get_mcp_status"
            && state.mcp.finish(command_id, *ok, error.as_deref()) =>
        {
            Some(vec![UiEffect::RequestRender])
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
}
