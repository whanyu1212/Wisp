//! One policy-versioned discovery request, independent of prompt execution.

use super::{BackendEvent, CommandIdSource, CommandKind, UiEffect, UiState};
use std::sync::Arc;
use wisp_protocol::{ProtocolDecodeError, commands::WispTypedClientRpcCommands};

pub type GitDiffReport = Result<Arc<GitDiffSnapshot>, String>;

#[derive(Clone, Debug, PartialEq)]
struct Pending {
    id: String,
    report: Option<GitDiffReport>,
    duplicate: bool,
    superseded: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub(crate) struct GitDiff {
    pub snapshot: Option<Arc<GitDiffSnapshot>>,
    pub error: Option<String>,
    generation: u64,
    open: bool,
    refresh: bool,
    pending: Option<Pending>,
}

impl GitDiff {
    pub fn refresh(&mut self) {
        self.refresh = true;
        self.retire_snapshot();
    }

    pub fn loading(&self) -> bool {
        self.pending.is_some() || self.refresh
    }

    pub fn set_open(&mut self, open: bool) {
        if self.open == open {
            return;
        }
        self.open = open;
        self.refresh = open;
        if open {
            self.retire_snapshot();
        } else if let Some(pending) = &mut self.pending {
            pending.superseded = true;
            pending.report = None;
        }
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
                    return None;
                }
                self.generation = *generation;
                // Nothing painted under the previous authority may remain selectable.
                self.retire_snapshot();
                self.refresh = self.open;
                return None;
            }
            BackendEvent::GitDiffReported {
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
            } if command_type == "get_git_diff" => {
                if self.pending.as_ref().is_none_or(|p| p.id != *command_id) {
                    return Some(Vec::new());
                }
                let pending = self.pending.take().expect("matched Git diff");
                if !pending.superseded && self.open {
                    let result = if !ok {
                        Err(super::bounded_session_text(
                            error.as_deref().unwrap_or("Git diff failed."),
                            super::SESSION_NOTICE_MAX_BYTES,
                        ))
                    } else if pending.duplicate {
                        Err("Git diff returned duplicate reports.".into())
                    } else {
                        pending
                            .report
                            .unwrap_or_else(|| Err("Git diff returned no current snapshot.".into()))
                            .and_then(|snapshot| {
                                if snapshot.generation >= self.generation {
                                    Ok(snapshot)
                                } else {
                                    Err("Git diff returned an obsolete snapshot.".into())
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
        Some(vec![UiEffect::RequestRender])
    }
}

pub(super) fn refresh_if_ready(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let files = &mut state.git_diff;
    if !files.open || !files.refresh || files.pending.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetGitDiff);
    let command = WispTypedClientRpcCommands::get_git_diff(&id)?;
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

#[derive(Clone, Debug, PartialEq, serde::Deserialize)]
pub struct GitDiffSnapshot {
    pub generation: u64,
    pub head: String,
    pub files: Vec<GitDiffFile>,
    pub truncated: bool,
}

#[derive(Clone, Debug, PartialEq, serde::Deserialize)]
pub struct GitDiffFile {
    pub path: String,
    pub status: String,
    pub before: String,
    pub after: String,
    pub notice: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::{UiAction, reduce};

    #[derive(Default)]
    struct Ids(usize);
    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }
    #[test]
    fn publishes_only_correlated_success_and_retires_on_policy_change() {
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::SetGitDiffOpen(true), &mut ids).unwrap();
        let id = state.git_diff.pending.as_ref().unwrap().id.clone();
        let report = Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "abc".into(),
            files: vec![],
            truncated: false,
        });
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::GitDiffReported {
                command_id: "wrong".into(),
                snapshot: Ok(report.clone()),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(state.git_diff.pending.as_ref().unwrap().report.is_none());
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::GitDiffReported {
                command_id: id.clone(),
                snapshot: Ok(report),
            }),
            &mut ids,
        )
        .unwrap();
        assert!(state.git_diff.snapshot.is_none());
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: id,
                command_type: "get_git_diff".into(),
                ok: true,
                error: None,
            }),
            &mut ids,
        )
        .unwrap();
        assert!(state.git_diff.snapshot.is_some());
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ProjectFilesInvalidated(2)),
            &mut ids,
        )
        .unwrap();
        assert!(state.git_diff.snapshot.is_none());
        assert!(state.git_diff.loading());
    }
}
