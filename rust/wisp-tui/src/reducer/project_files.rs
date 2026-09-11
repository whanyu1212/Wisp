//! One policy-versioned discovery request, independent of prompt execution.

use super::{BackendEvent, CommandIdSource, CommandKind, UiEffect, UiState};
use std::sync::Arc;
use wisp_protocol::{
    ProtocolDecodeError, commands::WispTypedClientRpcCommands, events::ProjectFileSnapshot,
};

pub type ProjectFilesReport = Result<Arc<ProjectFileSnapshot>, String>;

#[derive(Clone, Debug, PartialEq)]
struct Pending {
    id: String,
    report: Option<ProjectFilesReport>,
    duplicate: bool,
    superseded: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub(crate) struct ProjectFiles {
    pub snapshot: Option<Arc<ProjectFileSnapshot>>,
    pub error: Option<String>,
    generation: u64,
    open: bool,
    refresh: bool,
    pending: Option<Pending>,
}

impl ProjectFiles {
    pub fn is_open(&self) -> bool {
        self.open
    }

    pub fn loading(&self) -> bool {
        self.pending.is_some() || self.refresh
    }

    pub fn set_open(&mut self, open: bool) {
        if self.open == open {
            return;
        }
        self.open = open;
        self.retire_snapshot();
        self.refresh = open;
    }

    fn retire_snapshot(&mut self) {
        self.snapshot = None;
        self.error = None;
        if let Some(pending) = &mut self.pending {
            pending.superseded = true;
            pending.report = None;
        }
    }

    pub(super) fn close(&mut self) {
        self.set_open(false);
        self.pending = None;
        self.snapshot = None;
    }

    pub(super) fn observe(&mut self, event: &BackendEvent) -> Option<Vec<UiEffect>> {
        match event {
            BackendEvent::ProjectFilesInvalidated(generation) => {
                if *generation <= self.generation {
                    return Some(Vec::new());
                }
                self.generation = *generation;
                // Nothing painted under the previous authority may remain selectable.
                self.retire_snapshot();
                self.refresh = self.open;
            }
            BackendEvent::ProjectFilesReported {
                command_id,
                snapshot,
            } => {
                if let Some(pending) = self.pending.as_mut().filter(|p| p.id == *command_id) {
                    if !pending.superseded {
                        pending.duplicate |= pending.report.is_some();
                        pending.report = Some(snapshot.clone());
                    }
                }
                return Some(Vec::new());
            }
            BackendEvent::CommandFinished {
                command_id,
                command_type,
                ok,
                error,
            } if command_type == "get_project_files" => {
                if self.pending.as_ref().is_none_or(|p| p.id != *command_id) {
                    return Some(Vec::new());
                }
                let pending = self.pending.take().expect("matched project discovery");
                if !pending.superseded && self.open {
                    let result = if !ok {
                        Err(super::bounded_session_text(
                            error.as_deref().unwrap_or("Project discovery failed."),
                            super::SESSION_NOTICE_MAX_BYTES,
                        ))
                    } else if pending.duplicate {
                        Err("Project discovery returned duplicate reports.".into())
                    } else {
                        pending
                            .report
                            .unwrap_or_else(|| {
                                Err("Project discovery returned no current snapshot.".into())
                            })
                            .and_then(|snapshot| {
                                if snapshot.generation >= self.generation {
                                    Ok(snapshot)
                                } else {
                                    Err("Project discovery returned an obsolete snapshot.".into())
                                }
                            })
                    };
                    match result {
                        Ok(snapshot) => {
                            self.generation = snapshot.generation;
                            self.snapshot = Some(snapshot);
                        }
                        Err(error) => self.error = Some(error),
                    }
                }
            }
            _ => return None,
        }
        Some(vec![UiEffect::ProjectFilesChanged, UiEffect::RequestRender])
    }
}

pub(super) fn refresh_if_ready(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let files = &mut state.project_files;
    if !files.open || !files.refresh || files.pending.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetProjectFiles);
    let command = WispTypedClientRpcCommands::get_project_files(&id)?;
    files.refresh = false;
    files.pending = Some(Pending {
        id,
        report: None,
        duplicate: false,
        superseded: false,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::{UiAction, reduce};
    use wisp_protocol::events::{ProjectFileEntry, ProjectFileKind};

    #[derive(Default)]
    struct Ids(usize);
    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }

    fn report(id: &str, generation: u64) -> BackendEvent {
        BackendEvent::ProjectFilesReported {
            command_id: id.into(),
            snapshot: Ok(Arc::new(ProjectFileSnapshot {
                generation,
                entries: vec![ProjectFileEntry {
                    path: "public.rs".into(),
                    kind: ProjectFileKind::File,
                }],
                truncated: false,
            })),
        }
    }

    fn finished(id: &str, ok: bool) -> BackendEvent {
        BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: "get_project_files".into(),
            ok,
            error: (!ok).then(|| "Discovery busy".into()),
        }
    }

    fn event(state: &mut UiState, ids: &mut Ids, event: BackendEvent) -> Vec<UiEffect> {
        reduce(state, UiAction::BackendEvent(event), ids).unwrap()
    }

    #[test]
    fn discovery_is_lazy_correlated_and_does_not_own_the_prompt() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        assert!(refresh_if_ready(&mut state, &mut ids).unwrap().is_empty());
        reduce(&mut state, UiAction::Submit("work".into()), &mut ids).unwrap();
        let active = state.current_command.clone();
        let effects = reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
        assert_eq!(ids.0, 2);
        assert!(refresh_if_ready(&mut state, &mut ids).unwrap().is_empty());
        event(&mut state, &mut ids, report("wrong", 1));
        event(&mut state, &mut ids, finished("wrong", true));
        assert!(state.project_files.loading());
        event(&mut state, &mut ids, report("get_project_files-2", 1));
        assert!(
            state.project_files.snapshot.is_none(),
            "await terminal outcome"
        );
        event(&mut state, &mut ids, finished("get_project_files-2", true));
        assert!(state.project_files.snapshot.is_some());
        assert_eq!(state.current_command, active);
        assert!(state.active_prompt_editable());
    }

    #[test]
    fn invalidation_retires_reports_in_either_order_and_refreshes_once_after_settlement() {
        for report_first in [true, false] {
            let mut state = UiState::unconfigured();
            let mut ids = Ids::default();
            reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
            if report_first {
                event(&mut state, &mut ids, report("get_project_files-1", 1));
            }
            event(
                &mut state,
                &mut ids,
                BackendEvent::ProjectFilesInvalidated(2),
            );
            if !report_first {
                event(&mut state, &mut ids, report("get_project_files-1", 1));
            }
            assert_eq!(ids.0, 1, "no overlapping scan");
            event(&mut state, &mut ids, finished("get_project_files-1", true));
            assert!(state.project_files.snapshot.is_none());
            assert_eq!(ids.0, 2);
            event(&mut state, &mut ids, report("get_project_files-1", 1));
            event(&mut state, &mut ids, finished("get_project_files-1", true));
            event(&mut state, &mut ids, report("get_project_files-2", 2));
            event(&mut state, &mut ids, finished("get_project_files-2", true));
            assert_eq!(state.project_files.snapshot.as_ref().unwrap().generation, 2);
            event(
                &mut state,
                &mut ids,
                BackendEvent::ProjectFilesInvalidated(2),
            );
            assert!(
                state.project_files.snapshot.is_some(),
                "duplicate invalidation"
            );
            event(
                &mut state,
                &mut ids,
                BackendEvent::ProjectFilesInvalidated(3),
            );
            assert!(
                state.project_files.snapshot.is_none(),
                "retire already displayed data too"
            );
            assert_eq!(ids.0, 3);
        }
    }

    #[test]
    fn close_reopen_waits_for_obsolete_scan_and_connection_close_releases_data() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
        reduce(&mut state, UiAction::SetProjectFilesOpen(false), &mut ids).unwrap();
        event(&mut state, &mut ids, report("get_project_files-1", 1));
        reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
        assert_eq!(ids.0, 1);
        event(&mut state, &mut ids, finished("get_project_files-1", true));
        assert_eq!(ids.0, 2);
        assert!(state.project_files.snapshot.is_none());
        event(&mut state, &mut ids, report("get_project_files-2", 1));
        event(&mut state, &mut ids, finished("get_project_files-2", true));
        reduce(
            &mut state,
            UiAction::TransportClosed { error: None },
            &mut ids,
        )
        .unwrap();
        assert!(!state.project_files.is_open());
        assert!(!state.project_files.loading());
        assert!(state.project_files.snapshot.is_none());
    }

    #[test]
    fn failure_missing_duplicate_stale_and_wrong_type_never_install_or_retry_forever() {
        for failure in ["failed", "missing", "duplicate", "stale", "wrong-type"] {
            let mut state = UiState::unconfigured();
            let mut ids = Ids::default();
            event(
                &mut state,
                &mut ids,
                BackendEvent::ProjectFilesInvalidated(2),
            );
            reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
            if failure != "missing" {
                event(
                    &mut state,
                    &mut ids,
                    report(
                        "get_project_files-1",
                        if failure == "stale" { 1 } else { 2 },
                    ),
                );
            }
            if failure == "duplicate" {
                event(&mut state, &mut ids, report("get_project_files-1", 2));
            }
            if failure == "wrong-type" {
                event(
                    &mut state,
                    &mut ids,
                    BackendEvent::CommandFinished {
                        command_id: "get_project_files-1".into(),
                        command_type: "get_skills".into(),
                        ok: true,
                        error: None,
                    },
                );
                assert!(state.project_files.loading());
            }
            event(
                &mut state,
                &mut ids,
                finished(
                    "get_project_files-1",
                    !matches!(failure, "failed" | "wrong-type"),
                ),
            );
            assert!(!state.project_files.loading());
            assert!(state.project_files.snapshot.is_none(), "{failure}");
            assert!(state.project_files.error.is_some(), "{failure}");
            assert_eq!(ids.0, 1);
            reduce(&mut state, UiAction::SetProjectFilesOpen(false), &mut ids).unwrap();
            reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
            assert_eq!(ids.0, 2, "explicit retry");
        }
    }
}
