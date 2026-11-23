//! Independent, policy-versioned requests for folder browsing and project-wide search.

use super::{BackendEvent, CommandIdSource, CommandKind, UiEffect, UiState};
use std::collections::{BTreeMap, VecDeque};
use std::sync::Arc;
use wisp_protocol::{
    ProtocolDecodeError,
    commands::WispTypedClientRpcCommands,
    events::{ProjectFileKind, ProjectFileSnapshot},
};

pub type ProjectFilesReport = Result<Arc<ProjectFileSnapshot>, String>;

const MAX_VISIBLE_FOLDERS: usize = 64;

#[derive(Clone, Debug, PartialEq)]
struct Pending {
    id: String,
    report: Option<ProjectFilesReport>,
    invalidated: bool,
}

impl Pending {
    fn new(id: String) -> Self {
        Self {
            id,
            report: None,
            invalidated: false,
        }
    }

    fn record(&mut self, report: ProjectFilesReport) {
        self.report = Some(if self.report.is_some() {
            Err("Project discovery returned duplicate or out-of-order reports.".into())
        } else {
            report
        });
    }

    fn finish(self, ok: bool, error: Option<&str>) -> ProjectFilesReport {
        if ok {
            self.report
                .unwrap_or_else(|| Err("Project discovery returned no current snapshot.".into()))
        } else {
            Err(super::bounded_session_text(
                error.unwrap_or("Project discovery failed."),
                super::SESSION_NOTICE_MAX_BYTES,
            ))
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
struct Directory {
    snapshot: Option<Arc<ProjectFileSnapshot>>,
    error: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub(crate) struct ProjectFiles {
    pub snapshot: Option<Arc<ProjectFileSnapshot>>,
    pub error: Option<String>,
    partial_snapshot: Option<Arc<ProjectFileSnapshot>>,
    generation: u64,
    open: bool,
    panel: bool,
    filter: bool,
    search_refresh: bool,
    search_pending: Option<Pending>,
    folders: Vec<String>,
    directory_queue: VecDeque<String>,
    directory_pending: Option<(String, Pending)>,
    directories: BTreeMap<String, Directory>,
}

impl ProjectFiles {
    pub fn is_open(&self) -> bool {
        self.open
    }

    pub fn loading(&self) -> bool {
        self.search_pending.is_some() || self.search_refresh
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn partial(&self) -> bool {
        self.snapshot.is_none() && self.partial_snapshot.is_some()
    }

    pub(crate) fn search_snapshot(&self) -> Option<&Arc<ProjectFileSnapshot>> {
        self.snapshot.as_ref().or(self.partial_snapshot.as_ref())
    }

    pub(crate) fn snapshot(&self) -> Option<&ProjectFileSnapshot> {
        self.search_snapshot().map(Arc::as_ref)
    }

    pub fn directory(&self, path: &str) -> Option<&ProjectFileSnapshot> {
        self.directories.get(path)?.snapshot.as_deref()
    }

    pub fn directory_loading(&self, path: &str) -> bool {
        self.directory_pending
            .as_ref()
            .is_some_and(|(active, pending)| active == path && !pending.invalidated)
            || self.directory_queue.iter().any(|queued| queued == path)
    }

    pub fn directory_error(&self, path: &str) -> Option<&str> {
        if self.panel
            && self.folders.len() == MAX_VISIBLE_FOLDERS
            && !path.is_empty()
            && !self.folders.iter().any(|folder| folder == path)
        {
            return Some("Too many folders open.");
        }
        self.directories.get(path)?.error.as_deref()
    }

    pub fn file_snapshot(&self, path: &str) -> Option<&ProjectFileSnapshot> {
        let parent = parent_directory(path);
        let contains_file = |snapshot: &&ProjectFileSnapshot| {
            snapshot
                .entries
                .iter()
                .any(|entry| entry.kind == ProjectFileKind::File && entry.path == path)
        };
        if let Some(listing) = self.directory(parent)
            && !listing.truncated
        {
            return Some(listing).filter(contains_file);
        }
        self.directory(parent)
            .filter(contains_file)
            .or_else(|| self.snapshot().filter(contains_file))
    }

    fn search_wanted(&self) -> bool {
        self.open || (self.panel && self.filter)
    }

    pub fn set_open(&mut self, open: bool) {
        let opened = open && !self.open;
        self.open = open;
        self.sync_search_interest(opened);
    }

    pub fn set_panel(&mut self, panel: bool) {
        if panel && !self.panel {
            self.queue_folder_refresh();
        } else if !panel && self.panel {
            self.directory_queue.clear();
            self.filter = false;
        }
        self.panel = panel;
        self.sync_search_interest(false);
    }

    pub fn view_matches(&self, filter: bool, folders: &[String]) -> bool {
        !self.panel
            || (self.filter == filter
                && self
                    .folders
                    .iter()
                    .eq(folders.iter().take(MAX_VISIBLE_FOLDERS)))
    }

    pub fn set_view(&mut self, filter: bool, mut folders: Vec<String>) {
        if !self.panel {
            return;
        }
        let opened = filter && !self.filter;
        self.filter = filter;
        self.sync_search_interest(opened);

        folders.truncate(MAX_VISIBLE_FOLDERS);
        self.directory_queue
            .retain(|path| path.is_empty() || folders.contains(path));
        self.directories
            .retain(|path, _| path.is_empty() || folders.contains(path));
        // Explicit expansions go ahead of the rest of a scheduled refresh.
        for path in folders.iter().rev() {
            if !self.folders.contains(path) && !self.directory_loading(path) {
                self.directory_queue.push_front(path.clone());
            }
        }
        self.folders = folders;
    }

    fn sync_search_interest(&mut self, opened: bool) {
        if self.search_wanted() {
            if opened && self.search_pending.is_none() {
                self.search_refresh = true;
            }
        } else {
            self.search_refresh = false;
        }
    }

    pub fn refresh(&mut self) {
        if self.panel {
            self.queue_folder_refresh();
        }
        self.search_refresh = self.search_wanted();
    }

    pub fn refresh_for_panel(&mut self) {
        if self.panel {
            self.queue_folder_refresh();
            if self.filter && !self.open {
                self.search_refresh = true;
            }
        }
    }

    fn queue_folder_refresh(&mut self) {
        self.directory_queue = std::iter::once(String::new())
            .chain(self.folders.iter().cloned())
            .collect();
    }

    pub fn retry_directory(&mut self, path: String) {
        if self.panel && (path.is_empty() || self.folders.contains(&path)) {
            self.directory_queue.retain(|queued| queued != &path);
            self.directory_queue.push_front(path);
        }
    }

    pub(super) fn close(&mut self) {
        *self = Self::default();
    }

    fn invalidate(&mut self, generation: u64) {
        self.generation = generation;
        self.snapshot = None;
        self.partial_snapshot = None;
        self.error = None;
        self.filter = false;
        self.search_refresh = self.search_wanted();
        self.directories.clear();
        self.folders.clear();
        self.directory_queue.clear();
        if self.panel {
            self.directory_queue.push_back(String::new());
        }
        if let Some(pending) = &mut self.search_pending {
            pending.invalidated = true;
        }
        if let Some((_, pending)) = &mut self.directory_pending {
            pending.invalidated = true;
        }
    }

    fn record_report(
        &mut self,
        id: &str,
        directory: Option<&str>,
        in_progress: bool,
        snapshot: &ProjectFilesReport,
    ) -> bool {
        if let Some((path, pending)) = &mut self.directory_pending
            && pending.id == id
        {
            if !pending.invalidated {
                let report = validate_report(snapshot, self.generation, directory, Some(path));
                pending.record(if in_progress {
                    Err("Folder listing is incomplete.".into())
                } else {
                    report
                });
            }
            return false;
        }
        let Some(pending) = self
            .search_pending
            .as_mut()
            .filter(|pending| pending.id == id && !pending.invalidated)
        else {
            return false;
        };
        let report = validate_report(snapshot, self.generation, directory, None);
        if in_progress && pending.report.is_none() {
            match report {
                Ok(snapshot) => {
                    // Keep a complete catalog useful during a refresh.
                    if self.snapshot.is_none() {
                        self.partial_snapshot = Some(snapshot);
                        return true;
                    }
                }
                Err(error) => pending.record(Err(error)),
            }
        } else {
            pending.record(report);
        }
        false
    }

    fn settle_directory(&mut self, path: String, pending: Pending, ok: bool, error: Option<&str>) {
        if pending.invalidated || (!path.is_empty() && !self.folders.contains(&path)) {
            return;
        }
        let directory = self.directories.entry(path).or_default();
        match pending.finish(ok, error) {
            Ok(snapshot) => {
                directory.snapshot = Some(snapshot);
                directory.error = None;
            }
            Err(error) => directory.error = Some(error),
        }
    }

    pub(super) fn observe(&mut self, event: &BackendEvent) -> Option<Vec<UiEffect>> {
        match event {
            BackendEvent::ProjectFilesInvalidated(generation) => {
                if *generation <= self.generation {
                    return Some(Vec::new());
                }
                self.invalidate(*generation);
            }
            BackendEvent::ProjectFilesReported {
                command_id,
                directory,
                in_progress,
                snapshot,
            } => {
                if !self.record_report(command_id, directory.as_deref(), *in_progress, snapshot) {
                    return Some(Vec::new());
                }
            }
            BackendEvent::CommandFinished {
                command_id,
                command_type,
                ok,
                error,
            } if command_type == "get_project_files" => {
                if self
                    .directory_pending
                    .as_ref()
                    .is_some_and(|(_, p)| p.id == *command_id)
                {
                    let (path, pending) =
                        self.directory_pending.take().expect("matched folder read");
                    self.settle_directory(path, pending, *ok, error.as_deref());
                    return Some(vec![UiEffect::RequestRender]);
                }
                if self
                    .search_pending
                    .as_ref()
                    .is_none_or(|p| p.id != *command_id)
                {
                    return Some(Vec::new());
                }
                let pending = self
                    .search_pending
                    .take()
                    .expect("matched project discovery");
                if !pending.invalidated {
                    match pending.finish(*ok, error.as_deref()) {
                        Ok(snapshot) => {
                            self.snapshot = Some(snapshot);
                            self.partial_snapshot = None;
                            self.error = None;
                        }
                        Err(error) => self.error = Some(error),
                    }
                }
            }
            _ => return None,
        }
        Some(vec![UiEffect::ProjectFilesChanged, UiEffect::RequestRender])
    }

    #[cfg(test)]
    pub(crate) fn set_directory_for_test(&mut self, path: &str, snapshot: ProjectFileSnapshot) {
        self.directories.entry(path.into()).or_default().snapshot = Some(Arc::new(snapshot));
    }

    #[cfg(test)]
    pub(crate) fn set_directory_loading_for_test(&mut self, path: &str) {
        self.directory_queue.push_back(path.into());
    }

    #[cfg(test)]
    pub(crate) fn set_directory_error_for_test(&mut self, path: &str, error: &str) {
        self.directories.entry(path.into()).or_default().error = Some(error.into());
    }

    #[cfg(test)]
    pub(crate) fn set_partial_for_test(&mut self, snapshot: ProjectFileSnapshot) {
        self.partial_snapshot = Some(Arc::new(snapshot));
    }
}

fn parent_directory(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(parent, _)| parent)
}

fn validate_report(
    report: &ProjectFilesReport,
    generation: u64,
    directory: Option<&str>,
    expected: Option<&str>,
) -> ProjectFilesReport {
    let snapshot = report.clone()?;
    if snapshot.generation < generation {
        return Err("Project discovery returned an obsolete snapshot.".into());
    }
    if directory != expected
        || directory.is_some_and(|path| {
            snapshot
                .entries
                .iter()
                .any(|entry| parent_directory(&entry.path) != path)
        })
    {
        return Err("Project discovery returned a report for another folder.".into());
    }
    Ok(snapshot)
}

pub(super) fn refresh_if_ready(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    let files = &mut state.project_files;
    let mut effects = Vec::new();
    if state.exit_requested {
        return Ok(effects);
    }
    if files.panel && files.directory_pending.is_none() {
        while let Some(path) = files.directory_queue.pop_front() {
            let id = ids.next_id(CommandKind::GetProjectFiles);
            let Ok(command) = WispTypedClientRpcCommands::get_project_directory(&id, &path) else {
                // Session-touched paths come from tool history and can contain
                // names that the discovery protocol refuses to display.
                files.directories.entry(path).or_default().error =
                    Some("Folder path cannot be displayed.".into());
                effects.push(UiEffect::RequestRender);
                continue;
            };
            if let Some(directory) = files.directories.get_mut(&path) {
                directory.error = None;
            }
            files.directory_pending = Some((path, Pending::new(id)));
            effects.push(UiEffect::SendCommand(command));
            effects.push(UiEffect::RequestRender);
            break;
        }
    }
    if files.search_wanted() && files.search_refresh && files.search_pending.is_none() {
        let id = ids.next_id(CommandKind::GetProjectFiles);
        let command = WispTypedClientRpcCommands::get_project_file_index(&id)?;
        files.search_refresh = false;
        files.error = None;
        files.search_pending = Some(Pending::new(id));
        effects.push(UiEffect::SendCommand(command));
        effects.push(UiEffect::RequestRender);
    }
    Ok(effects)
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
            directory: None,
            in_progress: false,
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
    fn completed_snapshot_survives_picker_close_until_policy_invalidation() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
        event(&mut state, &mut ids, report("get_project_files-1", 1));
        event(&mut state, &mut ids, finished("get_project_files-1", true));
        reduce(&mut state, UiAction::SetProjectFilesOpen(false), &mut ids).unwrap();

        assert_eq!(state.project_files.snapshot().unwrap().generation, 1);
        event(
            &mut state,
            &mut ids,
            BackendEvent::ProjectFilesInvalidated(2),
        );
        assert!(state.project_files.snapshot().is_none());
    }

    #[test]
    fn scan_survives_reopening_picker() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
        reduce(&mut state, UiAction::SetProjectFilesOpen(false), &mut ids).unwrap();
        event(&mut state, &mut ids, report("get_project_files-1", 1));
        reduce(&mut state, UiAction::SetProjectFilesOpen(true), &mut ids).unwrap();
        assert_eq!(ids.0, 1);
        event(&mut state, &mut ids, finished("get_project_files-1", true));
        assert_eq!(ids.0, 1, "reuse the existing index");
        assert!(state.project_files.snapshot.is_some());
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

    fn folder_report(id: &str, path: &str, entries: &[(&str, ProjectFileKind)]) -> BackendEvent {
        BackendEvent::ProjectFilesReported {
            command_id: id.into(),
            directory: Some(path.into()),
            in_progress: false,
            snapshot: Ok(Arc::new(ProjectFileSnapshot {
                generation: 1,
                entries: entries
                    .iter()
                    .map(|(path, kind)| ProjectFileEntry {
                        path: (*path).into(),
                        kind: *kind,
                    })
                    .collect(),
                truncated: false,
            })),
        }
    }

    fn action(state: &mut UiState, ids: &mut Ids, action: UiAction) -> Vec<UiEffect> {
        reduce(state, action, ids).unwrap()
    }

    fn command(effects: &[UiEffect]) -> serde_json::Value {
        let [command] = effects
            .iter()
            .filter_map(|effect| match effect {
                UiEffect::SendCommand(command) => Some(command),
                _ => None,
            })
            .collect::<Vec<_>>()[..]
        else {
            panic!("expected one discovery command: {effects:?}");
        };
        serde_json::to_value(command).unwrap()
    }

    #[test]
    fn listing_runs_during_indexing() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();

        let open = action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesPanelOpen(true),
        );
        assert_eq!(command(&open)["directory"], "");
        event(
            &mut state,
            &mut ids,
            folder_report(
                "get_project_files-1",
                "",
                &[("src", ProjectFileKind::Directory)],
            ),
        );
        event(&mut state, &mut ids, finished("get_project_files-1", true));
        assert!(state.project_files.directory("").is_some());
        assert!(state.project_files.snapshot().is_none());

        let search = action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesView {
                filter: true,
                folders: Vec::new(),
            },
        );
        assert_eq!(command(&search)["progressive"], true);
        let expand = action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesView {
                filter: true,
                folders: vec!["src".into()],
            },
        );
        assert_eq!(command(&expand)["directory"], "src");
        event(
            &mut state,
            &mut ids,
            folder_report(
                "get_project_files-3",
                "src",
                &[("src/lib.rs", ProjectFileKind::File)],
            ),
        );
        event(&mut state, &mut ids, finished("get_project_files-3", true));
        assert!(state.project_files.file_snapshot("src/lib.rs").is_some());
        assert!(state.project_files.loading());
    }

    #[test]
    fn failed_refresh_retains_listing() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesPanelOpen(true),
        );
        event(
            &mut state,
            &mut ids,
            folder_report(
                "get_project_files-1",
                "",
                &[("README.md", ProjectFileKind::File)],
            ),
        );
        event(&mut state, &mut ids, finished("get_project_files-1", true));

        state.project_files.refresh();
        let refresh = refresh_if_ready(&mut state, &mut ids).unwrap();
        assert_eq!(command(&refresh)["directory"], "");
        assert!(state.project_files.directory_loading(""));
        assert!(state.project_files.file_snapshot("README.md").is_some());

        event(&mut state, &mut ids, finished("get_project_files-2", false));
        assert!(state.project_files.directory_error("").is_some());
        assert!(state.project_files.file_snapshot("README.md").is_some());
        assert!(!state.project_files.directory_loading(""));
    }

    #[test]
    fn invalid_touched_folder_skips_request() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesPanelOpen(true),
        );
        event(
            &mut state,
            &mut ids,
            folder_report("get_project_files-1", "", &[]),
        );
        event(&mut state, &mut ids, finished("get_project_files-1", true));

        let effects = action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesView {
                filter: false,
                folders: vec!["a\tb".into(), "src".into()],
            },
        );
        assert_eq!(command(&effects)["directory"], "src");
        assert!(state.project_files.directory_error("a\tb").is_some());
    }

    #[test]
    fn invalidation_discards_both_results() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        action(
            &mut state,
            &mut ids,
            UiAction::SetProjectFilesPanelOpen(true),
        );
        action(&mut state, &mut ids, UiAction::SetProjectFilesOpen(true));
        event(
            &mut state,
            &mut ids,
            folder_report(
                "get_project_files-1",
                "",
                &[("old.rs", ProjectFileKind::File)],
            ),
        );
        event(
            &mut state,
            &mut ids,
            BackendEvent::ProjectFilesInvalidated(2),
        );
        event(&mut state, &mut ids, report("get_project_files-2", 1));
        event(&mut state, &mut ids, finished("get_project_files-1", true));
        event(&mut state, &mut ids, finished("get_project_files-2", true));

        assert!(state.project_files.directory("").is_none());
        assert!(state.project_files.snapshot().is_none());
        assert!(state.project_files.directory_loading(""));
        assert!(state.project_files.loading());
        assert_eq!(ids.0, 4);
    }

    #[test]
    fn interim_matches_survive_failure() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        action(&mut state, &mut ids, UiAction::SetProjectFilesOpen(true));
        let BackendEvent::ProjectFilesReported {
            command_id,
            snapshot,
            ..
        } = report("get_project_files-1", 1)
        else {
            unreachable!();
        };
        event(
            &mut state,
            &mut ids,
            BackendEvent::ProjectFilesReported {
                command_id,
                directory: None,
                in_progress: true,
                snapshot,
            },
        );
        assert!(state.project_files.partial());
        assert_eq!(
            state.project_files.snapshot().unwrap().entries[0].path,
            "public.rs"
        );
        event(&mut state, &mut ids, finished("get_project_files-1", false));

        assert!(state.project_files.partial());
        assert_eq!(
            state.project_files.snapshot().unwrap().entries[0].path,
            "public.rs"
        );
        assert!(state.project_files.error.is_some());
        assert!(!state.project_files.loading());
    }

    #[test]
    fn automatic_refresh_keeps_picker_stable() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        action(&mut state, &mut ids, UiAction::SetProjectFilesOpen(true));
        event(&mut state, &mut ids, report("get_project_files-1", 1));
        event(&mut state, &mut ids, finished("get_project_files-1", true));

        state.project_files.refresh_for_panel();
        assert!(refresh_if_ready(&mut state, &mut ids).unwrap().is_empty());
        assert!(!state.project_files.loading());
        assert!(state.project_files.snapshot().is_some());
    }
}
