//! Agent-aware `Files` side-panel tab: session-touched files, compacted project tree,
//! inline `/` filter, and safe syntax-highlighted file preview in the expanded overlay.

use super::*;
use crate::file_picker::format_reference;
use crate::reducer::git_diff::{GitDiffFile, GitDiffSnapshot};
use crate::syntax::{self, SyntaxClass};
use crate::tool_detail::git_presentation;
use crate::transcript::{TranscriptEntryKind, normalize_lexical_path};
use crate::ui::sanitize_for_terminal;
use ratatui::widgets::{Scrollbar, ScrollbarOrientation, ScrollbarState};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::path::{Component, Path};
use unicode_segmentation::UnicodeSegmentation;
use wisp_protocol::events::{ProjectFileEntry, ProjectFileKind, ProjectFileSnapshot};

const MAX_TOUCHED_FILES: usize = 12;
// Each expanded directory has its own snapshot. Bound their combined UI work.
const MAX_TREE_ROWS: usize = 10_000;
const PREVIEW_MAX_BYTES: usize = 64 * 1024;
const PREVIEW_MAX_LINES: usize = 512;
const EXPANDED_SPLIT_MIN_WIDTH: usize = 72;
const EXPANDED_TREE_MIN_WIDTH: usize = 30;
const EXPANDED_TREE_MAX_WIDTH: usize = 40;

fn sanitize_single_line(text: &str) -> String {
    sanitize_for_terminal(text).replace('\n', " ")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct GitBadge {
    status: char,
    added: u64,
    deleted: u64,
    edited_by_wisp: bool,
}

#[derive(Debug)]
struct CachedDiffBadges {
    snapshot: Arc<GitDiffSnapshot>,
    by_path: BTreeMap<String, GitBadge>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum SelectableRowKind {
    TouchedFile {
        path: String,
    },
    TreeDir {
        fold_path: String,
        dir_path: String,
        retry: bool,
    },
    TreeFile {
        path: String,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct SelectableRow {
    line: usize,
    kind: SelectableRowKind,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum PreviewBody {
    Lines(Vec<Line<'static>>),
    Binary(u64),
    Notice(String),
}

#[derive(Clone, Debug)]
struct CachedPreview {
    path: String,
    workspace_cwd: Option<String>,
    generation: u64,
    snapshot_ptr: usize,
    palette: Palette,
    truncated: bool,
    total_lines: usize,
    body: PreviewBody,
}

#[derive(Debug, Default)]
struct DirNode {
    subdirs: BTreeMap<String, DirNode>,
    files: Vec<String>,
}

#[derive(Debug)]
struct SavedTreeSelection {
    index: usize,
    key: Option<String>,
    scroll: usize,
}

#[derive(Debug, Default)]
pub(super) struct FilesState {
    filter: String,
    filter_active: bool,
    selected_index: usize,
    selected_key: Option<String>,
    last_workspace_cwd: Option<String>,
    reveal_selection: Cell<bool>,
    list_scroll: Cell<usize>,
    preview_scroll: Cell<usize>,
    generation: Option<u64>,
    tree_selection: Option<SavedTreeSelection>,
    painted_search: bool,
    expanded_dirs: BTreeSet<String>,
    diff_cache: RefCell<Option<CachedDiffBadges>>,
    preview_cache: RefCell<Option<CachedPreview>>,
    painted_rows: RefCell<Vec<SelectableRow>>,
    painted_list_rect: Cell<Rect>,
    painted_filter_rect: Cell<Rect>,
    painted_preview_rect: Cell<Rect>,
    painted_preview_max_scroll: Cell<usize>,
    painted_list_total: Cell<usize>,
}

impl FilesState {
    pub(super) fn filter_active(&self) -> bool {
        self.filter_active
    }

    pub(super) fn has_filter(&self) -> bool {
        !self.filter.trim().is_empty()
    }

    pub(super) fn reset_for_workspace(&mut self) {
        self.filter.clear();
        self.filter_active = false;
        self.selected_index = 0;
        self.selected_key = None;
        self.list_scroll.set(0);
        self.preview_scroll.set(0);
        self.generation = None;
        self.tree_selection = None;
        self.painted_search = false;
        self.last_workspace_cwd = None;
        self.reveal_selection.set(true);
        self.expanded_dirs.clear();
        *self.diff_cache.borrow_mut() = None;
        self.invalidate_preview();
        self.painted_rows.borrow_mut().clear();
        self.painted_list_rect.set(Rect::default());
        self.painted_filter_rect.set(Rect::default());
        self.painted_preview_rect.set(Rect::default());
        self.painted_list_total.set(0);
        self.painted_preview_max_scroll.set(0);
    }

    pub(super) fn invalidate_preview(&self) {
        *self.preview_cache.borrow_mut() = None;
    }

    pub(super) fn sync_context(&mut self, state: &UiState) {
        if !self.is_current(state) {
            if self.generation.is_some() {
                self.reset_for_workspace();
            }
            self.generation = Some(state.project_files.generation());
            self.last_workspace_cwd.clone_from(&state.workspace_cwd);
        }
    }

    fn is_current(&self, state: &UiState) -> bool {
        self.generation == Some(state.project_files.generation())
            && self.last_workspace_cwd == state.workspace_cwd
    }

    /// The open tree stays live while a filter is temporarily showing search results.
    pub(super) fn requested_directories(&self, state: &UiState) -> Vec<String> {
        if !self.is_current(state) {
            return Vec::new();
        }
        let mut directories = Vec::new();
        let mut visible = HashSet::new();
        // The fold set is sorted by path, so a parent is processed before its children.
        for path in &self.expanded_dirs {
            let parent = parent_dir(path).unwrap_or_default();
            if !parent.is_empty() && !visible.contains(parent) {
                continue;
            }
            if state
                .project_files
                .directory(parent)
                .is_some_and(|snapshot| {
                    snapshot.entries.iter().any(|entry| {
                        entry.kind == ProjectFileKind::Directory && entry.path == *path
                    })
                })
            {
                visible.insert(path.as_str());
                directories.push(path.clone());
            }
        }
        if self.painted_search == self.has_filter()
            && let Some(SelectableRowKind::TouchedFile { path }) =
                self.selected_row().map(|row| row.kind)
            && let Some(parent) = parent_dir(&path)
            && !visible.contains(parent)
        {
            directories.push(parent.to_owned());
        }
        directories
    }

    fn is_dir_expanded(&self, dir_path: &str) -> bool {
        self.expanded_dirs.contains(dir_path)
    }

    fn set_dir_expanded(&mut self, dir_path: &str, expanded: bool) {
        self.reveal_selection.set(true);
        if expanded {
            self.expanded_dirs.insert(dir_path.to_owned());
        } else {
            self.expanded_dirs.remove(dir_path);
        }
    }

    fn update_filter(&mut self, update: impl FnOnce(&mut String)) {
        let had_filter = self.has_filter();
        update(&mut self.filter);
        if !had_filter && self.has_filter() {
            self.tree_selection = Some(SavedTreeSelection {
                index: self.selected_index,
                key: self.selected_key.clone(),
                scroll: self.list_scroll.get(),
            });
        }
        if had_filter
            && !self.has_filter()
            && let Some(saved) = self.tree_selection.take()
        {
            self.selected_index = saved.index;
            self.selected_key = saved.key;
            self.list_scroll.set(saved.scroll);
        } else {
            self.list_scroll.set(0);
        }
        self.reveal_selection.set(true);
    }

    fn selected_row(&self) -> Option<SelectableRow> {
        let rows = self.painted_rows.borrow();
        if let Some(key) = &self.selected_key
            && let Some(found) = rows.iter().find(|row| row.matches_key(key))
        {
            return Some(found.clone());
        }
        rows.get(self.selected_index)
            .cloned()
            .or_else(|| rows.first().cloned())
    }

    fn select_index(&mut self, index: usize) {
        let rows = self.painted_rows.borrow();
        let Some(row) = rows.get(index) else {
            self.selected_index = 0;
            self.selected_key = None;
            return;
        };
        let next_key = row.key().to_owned();
        if self.selected_key.as_deref() != Some(next_key.as_str()) {
            self.preview_scroll.set(0);
        }
        self.selected_index = index;
        self.selected_key = Some(next_key);
        self.reveal_selection.set(true);
        self.ensure_selected_visible(row.line);
    }

    fn ensure_selected_visible(&self, line: usize) {
        let height = usize::from(self.painted_list_rect.get().height).max(1);
        let mut offset = self.list_scroll.get();
        if line < offset {
            offset = line;
        } else if line >= offset + height {
            offset = line + 1 - height;
        }
        let max_scroll = self.painted_list_total.get().saturating_sub(height);
        self.list_scroll.set(offset.min(max_scroll));
    }

    fn selected_file_path(&self) -> Option<String> {
        match self.selected_row()?.kind {
            SelectableRowKind::TouchedFile { path } | SelectableRowKind::TreeFile { path } => {
                Some(path)
            }
            SelectableRowKind::TreeDir { .. } => None,
        }
    }

    fn selected_reference_path(&self) -> Option<String> {
        match self.selected_row()?.kind {
            SelectableRowKind::TouchedFile { path } | SelectableRowKind::TreeFile { path } => {
                Some(path)
            }
            SelectableRowKind::TreeDir { dir_path, .. } => Some(format!("{dir_path}/")),
        }
    }

    fn badge_map(&self, state: &UiState) -> Arc<BTreeMap<String, GitBadge>> {
        let Some(snapshot) = state.git_diff.snapshot.as_ref() else {
            *self.diff_cache.borrow_mut() = None;
            return Arc::new(BTreeMap::new());
        };
        if let Some(cached) = self.diff_cache.borrow().as_ref()
            && Arc::ptr_eq(&cached.snapshot, snapshot)
        {
            return Arc::new(cached.by_path.clone());
        }
        let mut by_path = BTreeMap::new();
        for file in &snapshot.files {
            let badge = compute_git_badge(file);
            by_path.insert(normalize_rel_path(&file.path), badge);
        }
        *self.diff_cache.borrow_mut() = Some(CachedDiffBadges {
            snapshot: snapshot.clone(),
            by_path: by_path.clone(),
        });
        Arc::new(by_path)
    }

    pub(super) fn handle_key(
        &mut self,
        key: KeyCode,
        expanded: bool,
        state: &UiState,
    ) -> (OverlayKey, bool) {
        self.sync_context(state);
        if self.filter_active {
            match key {
                KeyCode::Esc => {
                    self.update_filter(String::clear);
                    self.filter_active = false;
                    return (OverlayKey::Moved, false);
                }
                KeyCode::Enter => {
                    self.filter_active = false;
                    return (OverlayKey::Moved, false);
                }
                KeyCode::Backspace => {
                    self.update_filter(|filter| {
                        filter.pop();
                    });
                    return (OverlayKey::Moved, false);
                }
                KeyCode::Up => return (self.move_selection(-1), false),
                KeyCode::Down => return (self.move_selection(1), false),
                KeyCode::Char(ch) if !ch.is_control() => {
                    self.update_filter(|filter| filter.push(ch));
                    return (OverlayKey::Moved, false);
                }
                _ => return (OverlayKey::Ignored, false),
            }
        }

        match key {
            KeyCode::Esc if self.has_filter() => {
                self.update_filter(String::clear);
                (OverlayKey::Moved, false)
            }
            KeyCode::Char('/') => {
                self.filter_active = true;
                (OverlayKey::Moved, false)
            }
            KeyCode::Up | KeyCode::Char('k') => (self.move_selection(-1), false),
            KeyCode::Down | KeyCode::Char('j') => (self.move_selection(1), false),
            KeyCode::Home => {
                self.select_index(0);
                (OverlayKey::Moved, false)
            }
            KeyCode::End => {
                let len = self.painted_rows.borrow().len();
                if len > 0 {
                    self.select_index(len - 1);
                }
                (OverlayKey::Moved, false)
            }
            KeyCode::Left => (self.collapse_or_parent(), false),
            KeyCode::Right => (self.expand_selected(state), false),
            KeyCode::Enter | KeyCode::Char(' ') => self.activate_selected(expanded, Some(state)),
            KeyCode::Char('e') => match self.selected_file_path() {
                Some(path) => (OverlayKey::EditFile(path), false),
                None => (OverlayKey::Ignored, false),
            },
            KeyCode::Char('@') => match self.selected_reference_path() {
                Some(path) => (
                    OverlayKey::InsertFileReference(format_reference(&path)),
                    false,
                ),
                None => (OverlayKey::Ignored, false),
            },
            KeyCode::Char('d') => {
                let target = self.selected_file_path().filter(|path| {
                    state.git_diff.snapshot.as_ref().is_some_and(|s| {
                        s.files.iter().any(|f| normalize_rel_path(&f.path) == *path)
                    })
                });
                (OverlayKey::Open(Target::Diff(target)), false)
            }
            KeyCode::Char('r') => {
                self.invalidate_preview();
                (OverlayKey::RefreshFiles, false)
            }
            KeyCode::PageUp => {
                if expanded {
                    let page = usize::from(self.painted_preview_rect.get().height).max(1);
                    self.preview_scroll
                        .set(self.preview_scroll.get().saturating_sub(page));
                } else {
                    let page = i32::from(self.painted_list_rect.get().height.max(1));
                    self.scroll_list_by(-page);
                }
                (OverlayKey::Moved, false)
            }
            KeyCode::PageDown => {
                if expanded {
                    let page = usize::from(self.painted_preview_rect.get().height).max(1);
                    let max = self.painted_preview_max_scroll.get();
                    self.preview_scroll
                        .set((self.preview_scroll.get() + page).min(max));
                } else {
                    let page = i32::from(self.painted_list_rect.get().height.max(1));
                    self.scroll_list_by(page);
                }
                (OverlayKey::Moved, false)
            }
            _ => (OverlayKey::Ignored, false),
        }
    }

    pub(super) fn click(&mut self, column: u16, row: u16, expanded: bool) -> (OverlayKey, bool) {
        let filter_rect = self.painted_filter_rect.get();
        if contains_point(filter_rect, column, row) {
            self.filter_active = true;
            return (OverlayKey::Moved, false);
        }
        let list_rect = self.painted_list_rect.get();
        if !contains_point(list_rect, column, row) {
            return (OverlayKey::Ignored, false);
        }
        let line = usize::from(row - list_rect.y) + self.list_scroll.get();
        let found = self
            .painted_rows
            .borrow()
            .iter()
            .position(|entry| entry.line == line);
        let Some(idx) = found else {
            return (OverlayKey::Ignored, false);
        };
        self.filter_active = false;
        self.select_index(idx);
        self.activate_selected(expanded, None)
    }

    pub(super) fn scroll_list_by(&self, delta: i32) {
        let height = usize::from(self.painted_list_rect.get().height).max(1);
        let max = self.painted_list_total.get().saturating_sub(height);
        let next = if delta < 0 {
            self.list_scroll
                .get()
                .saturating_sub(delta.unsigned_abs() as usize)
        } else {
            self.list_scroll.get().saturating_add(delta as usize)
        };
        self.list_scroll.set(next.min(max));
    }

    pub(super) fn scroll_preview_by(&self, delta: i32) -> bool {
        let rect = self.painted_preview_rect.get();
        if rect.is_empty() {
            return false;
        }
        let max = self.painted_preview_max_scroll.get();
        let next = if delta < 0 {
            self.preview_scroll
                .get()
                .saturating_sub(delta.unsigned_abs() as usize)
        } else {
            self.preview_scroll.get().saturating_add(delta as usize)
        };
        self.preview_scroll.set(next.min(max));
        true
    }

    pub(super) fn preview_contains(&self, column: u16, row: u16) -> bool {
        contains_point(self.painted_preview_rect.get(), column, row)
    }

    fn move_selection(&mut self, delta: isize) -> OverlayKey {
        let rows_len = self.painted_rows.borrow().len();
        if rows_len == 0 {
            return OverlayKey::Moved;
        }
        let current = self.selected_resolved_index().unwrap_or(0) as isize;
        let next = (current + delta).clamp(0, (rows_len - 1) as isize) as usize;
        self.select_index(next);
        OverlayKey::Moved
    }

    fn selected_resolved_index(&self) -> Option<usize> {
        let rows = self.painted_rows.borrow();
        if rows.is_empty() {
            return None;
        }
        if let Some(key) = &self.selected_key
            && let Some(pos) = rows.iter().position(|r| r.matches_key(key))
        {
            return Some(pos);
        }
        Some(self.selected_index.min(rows.len() - 1))
    }

    fn collapse_or_parent(&mut self) -> OverlayKey {
        let Some(row) = self.selected_row() else {
            return OverlayKey::Moved;
        };
        match row.kind {
            SelectableRowKind::TreeDir { fold_path, .. } => {
                if !self.has_filter() && self.is_dir_expanded(&fold_path) {
                    self.set_dir_expanded(&fold_path, false);
                } else if let Some(parent) = parent_dir(&fold_path) {
                    self.select_matching_dir_ancestor(parent);
                }
            }
            SelectableRowKind::TreeFile { path } => {
                if let Some(parent) = parent_dir(&path) {
                    self.select_matching_dir_ancestor(parent);
                }
            }
            SelectableRowKind::TouchedFile { .. } => {}
        }
        OverlayKey::Moved
    }

    fn expand_selected(&mut self, state: &UiState) -> OverlayKey {
        if !self.has_filter()
            && let Some(SelectableRowKind::TreeDir {
                fold_path,
                dir_path,
                ..
            }) = self.selected_row().map(|row| row.kind)
        {
            let was_expanded = self.is_dir_expanded(&fold_path);
            self.set_dir_expanded(&fold_path, true);
            if was_expanded && directory_failure(state, &dir_path).is_some() {
                return OverlayKey::RetryProjectFileDirectory(dir_path);
            }
        }
        OverlayKey::Moved
    }

    fn activate_selected(&mut self, expanded: bool, state: Option<&UiState>) -> (OverlayKey, bool) {
        let Some(row) = self.selected_row() else {
            return (OverlayKey::Ignored, false);
        };
        match row.kind {
            SelectableRowKind::TreeDir {
                fold_path,
                dir_path,
                retry,
            } => {
                if !self.has_filter() {
                    let failed =
                        state.map_or(retry, |state| directory_failure(state, &dir_path).is_some());
                    let dir_expanded = self.is_dir_expanded(&fold_path);
                    if dir_expanded && failed {
                        return (OverlayKey::RetryProjectFileDirectory(dir_path), false);
                    }
                    self.set_dir_expanded(&fold_path, !dir_expanded);
                }
                (OverlayKey::Moved, false)
            }
            SelectableRowKind::TouchedFile { .. } | SelectableRowKind::TreeFile { .. } => {
                (OverlayKey::Moved, !expanded)
            }
        }
    }

    fn select_matching_dir_ancestor(&mut self, parent: &str) {
        let rows = self.painted_rows.borrow();
        let mut candidate = parent;
        loop {
            let key = format!("dir:{candidate}");
            if let Some(idx) = rows.iter().position(|row| row.matches_key(&key)) {
                drop(rows);
                self.select_index(idx);
                return;
            }
            let Some(next) = parent_dir(candidate) else {
                break;
            };
            candidate = next;
        }
    }
}

impl SelectableRow {
    fn key(&self) -> String {
        match &self.kind {
            SelectableRowKind::TouchedFile { path } => format!("touched:{path}"),
            SelectableRowKind::TreeDir { fold_path, .. } => format!("dir:{fold_path}"),
            SelectableRowKind::TreeFile { path } => format!("file:{path}"),
        }
    }

    fn matches_key(&self, key: &str) -> bool {
        match &self.kind {
            SelectableRowKind::TreeDir {
                fold_path,
                dir_path,
                ..
            } => key.strip_prefix("dir:").is_some_and(|path| {
                (path == fold_path
                    || path
                        .strip_prefix(fold_path)
                        .is_some_and(|tail| tail.starts_with('/')))
                    && (dir_path == path
                        || dir_path
                            .strip_prefix(path)
                            .is_some_and(|tail| tail.starts_with('/')))
            }),
            _ => self.key() == key,
        }
    }
}

pub(super) fn render(
    frame: &mut Frame<'_>,
    inner: Rect,
    surface: Rect,
    state: &UiState,
    panel: &SidePanel,
    palette: Palette,
) {
    let width = usize::from(inner.width);
    let height = usize::from(inner.height);
    if width == 0 || height == 0 {
        return;
    }

    panel.sync_files_context(state);
    let badges = panel.files_view.borrow().badge_map(state);
    let touched = collect_touched_files(state);

    let footer_height = usize::from(panel.overlay_open() || panel.expanded() || height >= 6);
    let content_area = Rect {
        height: inner.height.saturating_sub(footer_height as u16),
        ..inner
    };

    let preview_layout = if panel.expanded() && width >= EXPANDED_SPLIT_MIN_WIDTH {
        let tree_width = (width / 3)
            .clamp(EXPANDED_TREE_MIN_WIDTH, EXPANDED_TREE_MAX_WIDTH)
            .min(width.saturating_sub(20)) as u16;
        let tree_area = Rect {
            width: tree_width,
            ..content_area
        };
        let divider_area = Rect {
            x: tree_area.right(),
            width: 1,
            ..content_area
        };
        let preview_area = Rect {
            x: divider_area.right(),
            width: content_area
                .width
                .saturating_sub(tree_width.saturating_add(1)),
            ..content_area
        };
        Some((tree_area, preview_area, Some(divider_area)))
    } else if panel.expanded() && content_area.height >= 4 {
        // Keep both the tree and the preview on terminals too narrow to split side by side.
        let tree_area = Rect {
            height: content_area.height / 2,
            ..content_area
        };
        let preview_area = Rect {
            y: tree_area.bottom(),
            height: content_area.height - tree_area.height,
            ..content_area
        };
        Some((tree_area, preview_area, None))
    } else {
        None
    };
    if let Some((tree_area, preview_area, divider)) = preview_layout {
        render_tree_pane(
            frame, tree_area, surface, state, panel, &touched, &badges, palette,
        );
        if let Some(divider) = divider {
            render_divider(frame, divider, palette);
        }
        render_preview_pane(frame, preview_area, state, panel, &badges, palette);
    } else {
        {
            let files_view = panel.files_view.borrow();
            files_view.painted_preview_rect.set(Rect::default());
            files_view.painted_preview_max_scroll.set(0);
        }
        render_tree_pane(
            frame,
            content_area,
            surface,
            state,
            panel,
            &touched,
            &badges,
            palette,
        );
    }

    if footer_height > 0 {
        let footer = Rect {
            y: content_area.bottom(),
            height: footer_height as u16,
            ..inner
        };
        let files_view = panel.files_view.borrow();
        let hints: &[&str] = if files_view.filter_active {
            &["Enter keep", "Esc clear", "↑↓ select"]
        } else if preview_layout.is_some() {
            &[
                "e edit",
                "/ filter",
                "@ attach",
                "d diff",
                "f restore",
                "PgUp/PgDn scroll",
                "Esc",
            ]
        } else if panel.expanded() {
            &[
                "e edit",
                "/ filter",
                "@ attach",
                "d diff",
                "f restore",
                "Esc",
            ]
        } else if panel.overlay_open() {
            &[
                "/ filter",
                "Enter preview",
                "e edit",
                "@ attach",
                "d diff",
                "f expand",
                "Esc",
            ]
        } else {
            &["F8 focus", "/ filter"]
        };
        let mut used = 0;
        let text = hints
            .iter()
            .copied()
            .take_while(|hint| {
                used += hint.width() + if used == 0 { 0 } else { 3 };
                used <= width
            })
            .collect::<Vec<_>>()
            .join(" · ");
        frame.render_widget(Paragraph::new(ui::keycap_hints(&text, palette)), footer);
    }
}

#[allow(clippy::too_many_arguments)]
fn render_tree_pane(
    frame: &mut Frame<'_>,
    area: Rect,
    surface: Rect,
    state: &UiState,
    panel: &SidePanel,
    touched: &[String],
    badges: &BTreeMap<String, GitBadge>,
    palette: Palette,
) {
    let width = usize::from(area.width);
    let height = usize::from(area.height);
    if width == 0 || height == 0 {
        let files_view = panel.files_view.borrow();
        files_view.painted_filter_rect.set(Rect::default());
        files_view.painted_list_rect.set(Rect::default());
        return;
    }

    let mut top_rows = PanelRows::default();
    if panel.overlay_open() || panel.expanded() {
        browser::push_controls(&mut top_rows, panel, width, palette);
    }
    let controls_lines_len = top_rows.lines.len();
    let controls_rect = Rect {
        height: (controls_lines_len as u16).min(area.height),
        ..area
    };
    if !top_rows.lines.is_empty() {
        frame.render_widget(Paragraph::new(top_rows.lines), controls_rect);
    }

    let remaining_after_controls = Rect {
        y: area.y + controls_rect.height,
        height: area.height.saturating_sub(controls_rect.height),
        ..area
    };
    let filter_rect = Rect {
        height: u16::from(remaining_after_controls.height > 0),
        ..remaining_after_controls
    };
    {
        let files_view = panel.files_view.borrow();
        files_view.painted_filter_rect.set(filter_rect);
        if !filter_rect.is_empty() {
            let line = filter_line(&files_view.filter, files_view.filter_active, width, palette);
            frame.render_widget(Paragraph::new(line).style(palette.composer()), filter_rect);
        }
    }

    let list_rect = Rect {
        y: filter_rect.bottom(),
        height: remaining_after_controls
            .height
            .saturating_sub(filter_rect.height),
        ..remaining_after_controls
    };

    // Keep a stable scrollbar gutter so filtering never shifts the text or badges.
    let gutter = u16::from(list_rect.width > 1);
    let row_rect = Rect {
        width: list_rect.width - gutter,
        ..list_rect
    };
    let row_width = usize::from(row_rect.width);
    let (mut rendered_lines, rows) = {
        let files_view = panel.files_view.borrow();
        build_tree_rows(state, touched, badges, &files_view, row_width, palette)
    };

    let total = rendered_lines.len();
    let list_height = usize::from(list_rect.height);
    let selected_idx = {
        let mut files_view = panel.files_view.borrow_mut();
        files_view.painted_list_rect.set(row_rect);
        files_view.painted_list_total.set(total);
        files_view.painted_search = files_view.has_filter();
        *files_view.painted_rows.borrow_mut() = rows;
        let prev_index = files_view.selected_index;
        let reveal = files_view.reveal_selection.replace(false);
        let idx = files_view.selected_resolved_index();
        if let Some(idx) = idx {
            let key = files_view.painted_rows.borrow()[idx].key();
            let line = files_view.painted_rows.borrow()[idx].line;
            files_view.selected_index = idx;
            files_view.selected_key = Some(key);
            if reveal || prev_index != idx {
                files_view.ensure_selected_visible(line);
            }
        }
        idx
    };

    if let Some(idx) = selected_idx
        && (panel.overlay_open() || panel.expanded())
    {
        let line_idx = panel.files_view.borrow().painted_rows.borrow()[idx].line;
        let line = &mut rendered_lines[line_idx];
        line.spans[0] = Span::styled("▌", Style::default().fg(palette.primary));
        let padding = row_width.saturating_sub(line.width());
        line.spans.push(Span::raw(" ".repeat(padding)));
        *line = palette.selected_line(std::mem::take(line));
    }

    let max_scroll = total.saturating_sub(list_height.max(1));
    let offset = panel.files_view.borrow().list_scroll.get().min(max_scroll);
    panel.files_view.borrow().list_scroll.set(offset);
    let visible: Vec<_> = rendered_lines
        .into_iter()
        .skip(offset)
        .take(list_height)
        .collect();
    frame.render_widget(Paragraph::new(visible), row_rect);
    if gutter > 0 && max_scroll > 0 && list_height > 0 {
        let mut scrollbar_state = ScrollbarState::new(max_scroll + 1)
            .position(offset)
            .viewport_content_length(list_height);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .track_symbol(Some("│"))
            .track_style(Style::default().fg(palette.dim()))
            .thumb_symbol("┃")
            .thumb_style(Style::default().fg(palette.muted));
        frame.render_stateful_widget(scrollbar, list_rect, &mut scrollbar_state);
    }

    *panel.painted.borrow_mut() = Painted {
        surface,
        area: controls_rect,
        total: controls_lines_len,
        actions: top_rows.actions,
        ..Painted::default()
    };
}

fn filter_line(filter: &str, active: bool, width: usize, palette: Palette) -> Line<'static> {
    let muted = Style::default().fg(palette.muted);
    let accent = Style::default()
        .fg(palette.primary)
        .add_modifier(Modifier::BOLD);
    if width < 4 {
        return Line::from(Span::styled("/", accent));
    }

    let key = Span::styled(" / ", accent);
    let query = sanitize_single_line(filter);
    let text = Style::default().fg(palette.foreground);
    if active {
        let tail = elide_start(&query, width - 4);
        return Line::from(vec![
            key,
            Span::styled(tail, text),
            Span::styled("▏", accent),
        ]);
    }
    if filter.is_empty() {
        return Line::from(vec![
            key,
            Span::styled(ui::clip_columns("filter files…", width - 3), muted),
        ]);
    }
    let clear = "Esc clear ";
    if width < 3 + 1 + clear.width() + 8 {
        return Line::from(vec![
            key,
            Span::styled(elide_start(&query, width - 3), text),
        ]);
    }
    spread(
        vec![
            key,
            Span::styled(elide_start(&query, width - 4 - clear.width()), text),
        ],
        vec![Span::styled(clear, muted)],
        width,
    )
}

fn build_tree_rows(
    state: &UiState,
    touched: &[String],
    badges: &BTreeMap<String, GitBadge>,
    files_view: &FilesState,
    width: usize,
    palette: Palette,
) -> (Vec<Line<'static>>, Vec<SelectableRow>) {
    let mut lines = Vec::new();
    let mut rows = Vec::new();
    let query = files_view.filter.trim().to_ascii_lowercase();

    let filtered_touched: Vec<&String> = touched
        .iter()
        .filter(|path| query.is_empty() || path.to_ascii_lowercase().contains(&query))
        .collect();
    if !filtered_touched.is_empty() {
        lines.push(section_line(
            "Touched this session",
            filtered_touched.len().to_string(),
            width,
            palette,
        ));
        for path in filtered_touched {
            let line_idx = lines.len();
            let mut left = vec![Span::raw(" ")];
            if width >= 10 {
                left.push(Span::styled("◆ ", Style::default().fg(palette.primary)));
            }
            lines.push(format_file_entry_line(
                left,
                &file_name(path),
                parent_dir(path),
                badges.get(path.as_str()).copied(),
                width,
                palette,
            ));
            rows.push(SelectableRow {
                line: line_idx,
                kind: SelectableRowKind::TouchedFile { path: path.clone() },
            });
        }
        lines.push(Line::default());
    }

    let mut tree = TreeRows {
        state,
        files_view,
        badges,
        width,
        palette,
        lines,
        rows,
        row_limit_reached: false,
    };
    if query.is_empty() {
        let heading = tree.lines.len();
        tree.directory("", &mut Vec::new());
        if tree.row_limit_reached {
            tree.lines[heading] =
                section_line("Project tree · view limited", String::new(), width, palette);
        }
    } else {
        tree.search(&query);
    }
    (tree.lines, tree.rows)
}

fn section_line(title: &str, count: String, width: usize, palette: Palette) -> Line<'static> {
    let title = Span::styled(
        format!(" {title}"),
        Style::default()
            .fg(palette.muted)
            .add_modifier(Modifier::BOLD),
    );
    let count = Span::styled(format!("{count} "), Style::default().fg(palette.muted));
    let right = if width > title.width() + count.width() {
        vec![count]
    } else {
        Vec::new()
    };
    spread(vec![title], right, width)
}

fn build_dir_trie(entries: &[&ProjectFileEntry]) -> DirNode {
    let mut root = DirNode::default();
    for entry in entries {
        let rel = entry.path.trim_matches('/');
        if rel.is_empty() {
            continue;
        }
        let parts: Vec<&str> = rel.split('/').filter(|part| !part.is_empty()).collect();
        if parts.is_empty() {
            continue;
        }
        let mut node = &mut root;
        match entry.kind {
            ProjectFileKind::Directory => {
                for part in parts {
                    node = node.subdirs.entry(part.to_owned()).or_default();
                }
            }
            ProjectFileKind::File => {
                for dir_part in &parts[..parts.len() - 1] {
                    node = node.subdirs.entry((*dir_part).to_owned()).or_default();
                }
                node.files.push(rel.to_owned());
            }
        }
    }
    sort_dir_files(&mut root);
    root
}

fn sort_dir_files(node: &mut DirNode) {
    node.files.sort();
    node.files.dedup();
    for child in node.subdirs.values_mut() {
        sort_dir_files(child);
    }
}

struct TreeRows<'a> {
    state: &'a UiState,
    files_view: &'a FilesState,
    badges: &'a BTreeMap<String, GitBadge>,
    width: usize,
    palette: Palette,
    lines: Vec<Line<'static>>,
    rows: Vec<SelectableRow>,
    row_limit_reached: bool,
}

impl TreeRows<'_> {
    fn search(&mut self, query: &str) {
        let files = &self.state.project_files;
        let snapshot = files.snapshot();
        let entries: Vec<_> = snapshot
            .into_iter()
            .flat_map(|snapshot| &snapshot.entries)
            .filter(|entry| {
                entry.kind == ProjectFileKind::File
                    && entry.path.to_ascii_lowercase().contains(query)
            })
            .collect();
        let searching = files.partial() || files.loading() || snapshot.is_none();
        let truncated = snapshot.is_some_and(|snapshot| snapshot.truncated);
        let incomplete = truncated || searching || files.error.is_some();
        let count = entries.len();
        let suffix = if incomplete { "+" } else { "" };
        let plural = if count == 1 { "match" } else { "matches" };
        let count = if count != 0 || !incomplete {
            format!("{count}{suffix} {plural}")
        } else {
            String::new()
        };
        self.heading("Project tree", count);

        if let Some(error) = files.error.as_deref() {
            self.notice(
                &[],
                false,
                &format!("Search failed · r retry · {error}"),
                self.palette.error,
            );
        } else if searching {
            self.notice(&[], false, "Searching project…", self.palette.muted);
        } else if truncated {
            self.notice(
                &[],
                false,
                "Search incomplete · result limit reached",
                self.palette.muted,
            );
        } else if entries.is_empty() {
            self.notice(&[], false, "No matching files.", self.palette.dim());
        }

        let root = build_dir_trie(&entries);
        self.search_children(&root, "", &mut Vec::new());
    }

    fn search_children(&mut self, node: &DirNode, prefix: &str, ancestors: &mut Vec<bool>) {
        for (index, (name, child)) in node.subdirs.iter().enumerate() {
            let has_next = index + 1 < node.subdirs.len() || !node.files.is_empty();
            let mut dir_path = join_rel(prefix, name);
            let mut target = child;
            // All paths in a search result are fully expanded.
            while target.files.is_empty()
                && let Some((name, child)) = target
                    .subdirs
                    .first_key_value()
                    .filter(|_| target.subdirs.len() == 1)
            {
                dir_path = join_rel(&dir_path, name);
                target = child;
            }
            self.folder(
                &dir_path, &dir_path, prefix, ancestors, has_next, true, false,
            );
            ancestors.push(has_next);
            self.search_children(target, &dir_path, ancestors);
            ancestors.pop();
        }
        for (index, path) in node.files.iter().enumerate() {
            self.file(path, ancestors, index + 1 < node.files.len());
        }
    }

    fn directory(&mut self, path: &str, ancestors: &mut Vec<bool>) {
        let state = self.state;
        let files = &state.project_files;
        let snapshot = files.directory(path);
        let entries = snapshot.map_or(&[][..], |snapshot| snapshot.entries.as_slice());
        let mut entries: Vec<_> = entries
            .iter()
            .filter(|entry| is_direct_child(&entry.path, path))
            .collect();
        // Put folders before files and make rendering independent of report order.
        entries.sort_unstable_by(|a, b| {
            (a.kind == ProjectFileKind::File, &a.path)
                .cmp(&(b.kind == ProjectFileKind::File, &b.path))
        });
        entries.dedup_by(|a, b| a.kind == b.kind && a.path == b.path);

        let root = path.is_empty();
        let truncated = snapshot.is_some_and(|snapshot| snapshot.truncated);
        if root {
            let status = snapshot
                .map(|_| {
                    if files.directory_loading(path) {
                        "Refreshing…".to_owned()
                    } else {
                        format!(
                            "{}{} {}",
                            entries.len(),
                            if truncated { "+" } else { "" },
                            if entries.len() == 1 {
                                "entry"
                            } else {
                                "entries"
                            }
                        )
                    }
                })
                .unwrap_or_default();
            self.heading("Project tree", status);
        }
        if let Some(error) = directory_failure(state, path) {
            let action = if root { "r retry" } else { "Enter retry" };
            self.notice(
                ancestors,
                !entries.is_empty(),
                &format!("Load failed · {action} · {error}"),
                self.palette.error,
            );
        } else if snapshot.is_none() {
            let label = if root {
                "Loading project files…"
            } else {
                "Loading…"
            };
            self.notice(ancestors, false, label, self.palette.muted);
        } else if truncated {
            self.notice(
                ancestors,
                !entries.is_empty(),
                "Folder listing incomplete · entry limit reached",
                self.palette.muted,
            );
        } else if entries.is_empty() {
            let label = if root {
                "No project files reported."
            } else {
                "Empty folder"
            };
            self.notice(ancestors, false, label, self.palette.dim());
        }

        let files_view = self.files_view;
        for (index, entry) in entries.iter().enumerate() {
            if self.rows.len() == MAX_TREE_ROWS {
                self.row_limit_reached = true;
                break;
            }
            let has_next = index + 1 < entries.len();
            match entry.kind {
                ProjectFileKind::File => self.file(&entry.path, ancestors, has_next),
                ProjectFileKind::Directory => {
                    let dir_path = compacted_directory(state, files_view, &entry.path);
                    let expanded = files_view.is_dir_expanded(&entry.path);
                    let retry = expanded && directory_failure(state, dir_path).is_some();
                    self.folder(
                        &entry.path,
                        dir_path,
                        path,
                        ancestors,
                        has_next,
                        expanded,
                        retry,
                    );
                    if expanded && self.rows.len() < MAX_TREE_ROWS {
                        ancestors.push(has_next);
                        self.directory(dir_path, ancestors);
                        ancestors.pop();
                    } else if expanded {
                        self.row_limit_reached = true;
                    }
                }
            }
        }
    }

    fn heading(&mut self, title: &str, count: String) {
        self.lines
            .push(section_line(title, count, self.width, self.palette));
    }

    fn notice(
        &mut self,
        ancestors: &[bool],
        has_next: bool,
        label: &str,
        color: ratatui::style::Color,
    ) {
        let mut spans = if ancestors.is_empty() {
            Vec::new()
        } else {
            tree_prefix(ancestors, has_next, self.width, self.palette)
        };
        let room = self.width.saturating_sub(spans_width(&spans));
        spans.push(Span::styled(
            ui::clip_columns(&sanitize_single_line(label), room),
            Style::default().fg(color),
        ));
        self.lines.push(Line::from(spans));
    }

    #[allow(clippy::too_many_arguments)]
    fn folder(
        &mut self,
        fold_path: &str,
        dir_path: &str,
        parent: &str,
        ancestors: &[bool],
        has_next: bool,
        expanded: bool,
        retry: bool,
    ) {
        let label = if parent.is_empty() {
            dir_path
        } else {
            &dir_path[parent.len() + 1..]
        };
        let mut left = tree_prefix(ancestors, has_next, self.width, self.palette);
        if self.width.saturating_sub(spans_width(&left)) >= 4 {
            left.push(Span::styled(
                if expanded { "▾ " } else { "▸ " },
                Style::default().fg(self.palette.primary),
            ));
        }
        let label = format!("{}/", sanitize_single_line(label));
        let room = self.width.saturating_sub(spans_width(&left));
        let refreshing = expanded
            && !self.files_view.has_filter()
            && room >= 24
            && self.state.project_files.directory(dir_path).is_some()
            && self.state.project_files.directory_loading(dir_path);
        let suffix = refreshing
            .then(|| Span::styled("Refreshing… ", Style::default().fg(self.palette.muted)));
        let room = room.saturating_sub(suffix.as_ref().map_or(0, |hint| hint.width() + 1));
        let icon = if expanded { "\u{f0770} " } else { "\u{f024b} " };
        let room = if icon_fits(&label, room) {
            left.push(Span::styled(
                icon,
                Style::default().fg(self.palette.primary),
            ));
            room - ICON_COLUMNS
        } else {
            room
        };
        left.push(Span::styled(
            elide_middle(&label, room),
            Style::default()
                .fg(self.palette.foreground)
                .add_modifier(Modifier::BOLD),
        ));
        self.rows.push(SelectableRow {
            line: self.lines.len(),
            kind: SelectableRowKind::TreeDir {
                fold_path: fold_path.to_owned(),
                dir_path: dir_path.to_owned(),
                retry,
            },
        });
        self.lines.push(if let Some(hint) = suffix {
            spread(left, vec![hint], self.width)
        } else {
            Line::from(left)
        });
    }

    fn file(&mut self, path: &str, ancestors: &[bool], has_next: bool) {
        self.rows.push(SelectableRow {
            line: self.lines.len(),
            kind: SelectableRowKind::TreeFile {
                path: path.to_owned(),
            },
        });
        self.lines.push(format_file_entry_line(
            tree_prefix(ancestors, has_next, self.width, self.palette),
            &file_name(path),
            None,
            self.badges.get(path).copied(),
            self.width,
            self.palette,
        ));
    }
}

fn directory_failure<'a>(state: &'a UiState, path: &str) -> Option<&'a str> {
    let files = &state.project_files;
    if files.directory_loading(path) {
        None
    } else {
        files.directory_error(path)
    }
}

fn compacted_directory<'a>(state: &'a UiState, files_view: &FilesState, path: &'a str) -> &'a str {
    let mut current = path;
    // A compacted row only represents a chain already opened by the user.
    // A truncated listing cannot establish that a folder has only one child.
    while let Some(snapshot) = state.project_files.directory(current)
        && !snapshot.truncated
        && directory_failure(state, current).is_none()
        && let [child] = snapshot.entries.as_slice()
        && child.kind == ProjectFileKind::Directory
        && is_direct_child(&child.path, current)
        && files_view.is_dir_expanded(&child.path)
    {
        current = &child.path;
    }
    current
}

fn is_direct_child(path: &str, parent: &str) -> bool {
    !path.is_empty() && !path.ends_with('/') && parent_dir(path).unwrap_or_default() == parent
}

#[cfg(unix)]
fn read_preview_bytes_safe(root: &Path, rel: &Path) -> Result<(u64, Vec<u8>), &'static str> {
    use nix::errno::Errno;
    use nix::fcntl::{OFlag, openat};
    use nix::sys::stat::{Mode, SFlag, fstat};
    use nix::unistd::{close, read};
    use std::os::fd::RawFd;

    struct Descriptor(RawFd);
    impl Drop for Descriptor {
        fn drop(&mut self) {
            let _ = close(self.0);
        }
    }

    let parts: Vec<_> = rel
        .components()
        .filter_map(|component| match component {
            Component::Normal(seg) => Some(seg),
            _ => None,
        })
        .collect();
    if parts.is_empty() {
        return Err("Unsafe file path refused.");
    }

    // Walk every component with O_NOFOLLOW so replacement symlinks (even pointing inside
    // the workspace to protected files like .env) and special files are rejected atomically.
    let root_fd = openat(
        None::<RawFd>,
        root,
        OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_CLOEXEC,
        Mode::empty(),
    )
    .map(Descriptor)
    .map_err(|_| "Workspace root unavailable.")?;

    let mut current = root_fd;
    for dir_seg in &parts[..parts.len() - 1] {
        current = openat(
            Some(current.0),
            *dir_seg,
            OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC,
            Mode::empty(),
        )
        .map(Descriptor)
        .map_err(|err| match err {
            Errno::ELOOP => "Symbolic links are refused.",
            Errno::ENOTDIR => "Directory component is not a directory or is a symlink; refused.",
            _ => "File could not be opened.",
        })?;
    }

    let file_fd = openat(
        Some(current.0),
        parts[parts.len() - 1],
        // O_NONBLOCK prevents replacement FIFOs or devices from blocking the render thread.
        OFlag::O_RDONLY | OFlag::O_NOFOLLOW | OFlag::O_NONBLOCK | OFlag::O_CLOEXEC,
        Mode::empty(),
    )
    .map(Descriptor)
    .map_err(|err| match err {
        Errno::ELOOP => "Symbolic links are refused.",
        _ => "File could not be read.",
    })?;

    let stat = fstat(file_fd.0).map_err(|_| "File metadata unavailable.")?;
    if SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT != SFlag::S_IFREG {
        return Err("Not a regular file.");
    }

    let limit = PREVIEW_MAX_BYTES + 1;
    let mut raw = Vec::new();
    let mut buf = [0_u8; 8192];
    while raw.len() < limit {
        let wanted = buf.len().min(limit - raw.len());
        match read(file_fd.0, &mut buf[..wanted]) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&buf[..n]),
            Err(Errno::EINTR) => {}
            Err(_) => return Err("Failed to read file."),
        }
    }
    let file_len = u64::try_from(stat.st_size).unwrap_or(raw.len() as u64);
    Ok((file_len, raw))
}

#[cfg(not(unix))]
fn read_preview_bytes_safe(root: &Path, rel: &Path) -> Result<(u64, Vec<u8>), &'static str> {
    use std::fs::File;
    use std::io::Read as _;

    let mut cur = root.to_path_buf();
    for component in rel.components() {
        let Component::Normal(seg) = component else {
            return Err("Unsafe file path refused.");
        };
        cur.push(seg);
        let sym_meta = std::fs::symlink_metadata(&cur).map_err(|_| "File could not be opened.")?;
        if sym_meta.file_type().is_symlink() {
            return Err("Symbolic links are refused.");
        }
    }
    let target = std::fs::canonicalize(&cur).map_err(|_| "File could not be opened.")?;
    if !target.starts_with(root) {
        return Err("Path outside workspace root refused.");
    }
    let mut file = File::open(&target).map_err(|_| "File could not be read.")?;
    let meta = file.metadata().map_err(|_| "File metadata unavailable.")?;
    if !meta.is_file() {
        return Err("Not a regular file.");
    }
    let mut raw = Vec::new();
    file.by_ref()
        .take((PREVIEW_MAX_BYTES + 1) as u64)
        .read_to_end(&mut raw)
        .map_err(|_| "Failed to read file.")?;
    Ok((meta.len(), raw))
}

fn tree_prefix(
    ancestors: &[bool],
    has_next: bool,
    width: usize,
    palette: Palette,
) -> Vec<Span<'static>> {
    let mut spans = vec![Span::raw(" ")]; // Drawn as a rail when selected.
    if width >= 10 {
        let mut guides = String::new();
        for &continues in ancestors.iter().take((width.saturating_sub(14) / 2).min(6)) {
            guides.push_str(if continues { "│ " } else { "  " });
        }
        guides.push_str(if has_next { "├─ " } else { "└─ " });
        spans.push(Span::styled(guides, Style::default().fg(palette.dim())));
    }
    spans
}

fn spans_width(spans: &[Span<'_>]) -> usize {
    spans.iter().map(Span::width).sum()
}

/// Clip at whole graphemes. Keep the filename suffix (or the most recent part of a query).
fn tail_columns(text: &str, mut columns: usize) -> &str {
    let mut start = text.len();
    for (index, grapheme) in text.grapheme_indices(true).rev() {
        let width = grapheme.width();
        if width > columns {
            break;
        }
        columns -= width;
        start = index;
    }
    &text[start..]
}

fn elide_start(text: &str, width: usize) -> String {
    if width == 0 {
        String::new()
    } else if text.width() <= width {
        text.to_owned()
    } else {
        format!("…{}", tail_columns(text, width - 1))
    }
}

fn elide_middle(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    if text.width() <= width {
        return text.to_owned();
    }
    let tail = tail_columns(text, width / 2);
    let mut head_room = width - 1 - tail.width();
    let mut clipped = String::new();
    for grapheme in text.graphemes(true) {
        if grapheme.width() > head_room {
            break;
        }
        head_room -= grapheme.width();
        clipped.push_str(grapheme);
    }
    clipped.push('…');
    clipped.push_str(tail);
    clipped
}

// Each Nerd Font Mono glyph is one cell; its separating space is the second.
const ICON_COLUMNS: usize = 2;

fn icon_fits(label: &str, room: usize) -> bool {
    room >= label.width().min(10) + ICON_COLUMNS
}

/// Nerd Fonts v3 Material Design glyphs. Match special basenames before extensions.
fn nerd_file_icon(name: &str, palette: Palette) -> Span<'static> {
    let name = name.to_ascii_lowercase();
    let (symbol, color) = match name.as_str() {
        ".gitignore" | ".gitattributes" | ".gitmodules" => ("\u{f02a2} ", palette.code), // git
        "dockerfile" | "containerfile" => ("\u{f0868} ", palette.primary),               // docker
        "makefile" | "justfile" | ".env" => ("\u{f107c} ", palette.code), // file-cog-outline
        name => match name.rsplit_once('.').map(|(_, extension)| extension) {
            Some("rs") => ("\u{f1617} ", palette.code), // language-rust
            Some("py" | "pyi") => ("\u{f0320} ", palette.primary), // language-python
            Some("js" | "jsx" | "mjs" | "cjs") => ("\u{f031e} ", palette.warning), // language-javascript
            Some("ts" | "tsx" | "mts" | "cts") => ("\u{f06e6} ", palette.primary), // language-typescript
            Some("html" | "htm") => ("\u{f031d} ", palette.code),                  // language-html5
            Some("css" | "scss") => ("\u{f031c} ", palette.primary),               // language-css3
            Some("md" | "mdx") => ("\u{f0354} ", palette.accent), // language-markdown
            Some("json" | "jsonc") => ("\u{f0626} ", palette.warning), // code-json
            Some("toml" | "yaml" | "yml" | "ini" | "lock") => ("\u{f107c} ", palette.code), // file-cog-outline
            Some("sh" | "bash" | "zsh" | "fish") => ("\u{f07b7} ", palette.accent), // console-line
            Some("png" | "jpg" | "jpeg" | "gif" | "webp" | "svg") => ("\u{f0eb0} ", palette.accent), // file-image-outline
            _ => ("\u{f0224} ", palette.muted), // file-outline
        },
    };
    Span::styled(symbol, Style::default().fg(color))
}

fn format_file_entry_line(
    mut left: Vec<Span<'static>>,
    name: &str,
    parent: Option<&str>,
    badge: Option<GitBadge>,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let mut right = Vec::new();
    if let Some(badge) = badge.filter(|_| width.saturating_sub(spans_width(&left)) >= 4) {
        left.push(Span::styled(
            format!("{} ", badge.status),
            Style::default()
                .fg(status_color(badge.status, palette))
                .add_modifier(Modifier::BOLD),
        ));
        if badge.added > 0 {
            right.push(Span::styled(
                format!("+{}", badge.added),
                Style::default().fg(palette.success),
            ));
        }
        if badge.deleted > 0 {
            if !right.is_empty() {
                right.push(Span::raw(" "));
            }
            right.push(Span::styled(
                format!("−{}", badge.deleted),
                Style::default().fg(palette.error),
            ));
        }
    }
    let label = sanitize_single_line(name);
    let label_room = width.saturating_sub(spans_width(&left));
    let delta_width = spans_width(&right) + usize::from(!right.is_empty());
    // Prefer the filename and status to line counts in a narrow pane.
    let mut room = if label_room >= delta_width + label.width().min(10) {
        label_room - delta_width
    } else {
        right.clear();
        label_room
    };
    // Icons are decorative: don't hide readable names or Git counts to fit one.
    if icon_fits(&label, room) {
        left.push(nerd_file_icon(name, palette));
        room -= ICON_COLUMNS;
    }
    left.push(Span::styled(
        elide_middle(&label, room),
        Style::default().fg(palette.foreground),
    ));
    let context_room = room.saturating_sub(label.width() + 2);
    if let Some(parent) = parent.filter(|_| context_room >= 4) {
        let parent = sanitize_single_line(parent);
        left.push(Span::styled(
            format!("  {}", elide_start(&parent, context_room)),
            Style::default().fg(palette.muted),
        ));
    }
    spread(left, right, width)
}

fn render_divider(frame: &mut Frame<'_>, area: Rect, palette: Palette) {
    if area.is_empty() {
        return;
    }
    let lines: Vec<Line<'static>> = (0..area.height)
        .map(|_| Line::styled("│", palette.border()))
        .collect();
    frame.render_widget(Paragraph::new(lines), area);
}

fn render_preview_pane(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    panel: &SidePanel,
    badges: &BTreeMap<String, GitBadge>,
    palette: Palette,
) {
    let width = usize::from(area.width);
    let height = usize::from(area.height);
    if width == 0 || height == 0 {
        return;
    }

    let pad_area = Rect {
        x: area.x.saturating_add(1),
        width: area.width.saturating_sub(1),
        ..area
    };
    let pad_width = usize::from(pad_area.width);
    if pad_width == 0 {
        return;
    }

    let selected = panel.files_view.borrow().selected_file_path();
    let header_area = Rect {
        height: 1,
        ..pad_area
    };
    let body_area = Rect {
        y: pad_area.y + 1,
        height: pad_area.height.saturating_sub(1),
        ..pad_area
    };
    panel
        .files_view
        .borrow()
        .painted_preview_rect
        .set(body_area);

    let Some(path) = selected else {
        panel.files_view.borrow().painted_preview_max_scroll.set(0);
        frame.render_widget(
            Paragraph::new(Line::styled(
                ui::clip_columns("Select a file to preview", pad_width),
                Style::default().fg(palette.muted),
            )),
            header_area,
        );
        return;
    };

    let snapshot = state.project_files.file_snapshot(&path);
    let generation = state.project_files.generation();
    let mut preview = load_cached_preview(
        &panel.files_view,
        &path,
        state.workspace_cwd.as_deref(),
        snapshot,
        generation,
        palette,
    );

    if snapshot.is_none() {
        preview.body = PreviewBody::Notice(file_listing_notice(state, &path).into());
    }

    let mut header_left = vec![Span::styled(
        sanitize_single_line(&path),
        Style::default()
            .fg(palette.foreground)
            .add_modifier(Modifier::BOLD),
    )];
    let mut header_right = Vec::new();
    if let Some(badge) = badges.get(&path) {
        header_left.push(Span::styled(
            format!("  {}", badge.status),
            Style::default()
                .fg(status_color(badge.status, palette))
                .add_modifier(Modifier::BOLD),
        ));
        if badge.added > 0 {
            header_right.push(Span::styled(
                format!("+{}", badge.added),
                Style::default().fg(palette.success),
            ));
        }
        if badge.deleted > 0 {
            if !header_right.is_empty() {
                header_right.push(Span::raw(" "));
            }
            header_right.push(Span::styled(
                format!("−{}", badge.deleted),
                Style::default().fg(palette.error),
            ));
        }
    }
    if preview.truncated {
        if !header_right.is_empty() {
            header_right.push(Span::raw(" "));
        }
        header_right.push(Span::styled(
            format!("(first {} lines)", preview.total_lines),
            Style::default().fg(palette.muted),
        ));
    }
    frame.render_widget(
        Paragraph::new(spread(header_left, header_right, pad_width)),
        header_area,
    );

    if body_area.is_empty() {
        return;
    }

    let body_height = usize::from(body_area.height);
    match preview.body {
        PreviewBody::Lines(lines) => {
            let max_scroll = lines.len().saturating_sub(body_height);
            let files_view = panel.files_view.borrow();
            files_view.painted_preview_max_scroll.set(max_scroll);
            let offset = files_view.preview_scroll.get().min(max_scroll);
            files_view.preview_scroll.set(offset);
            let visible: Vec<_> = lines.into_iter().skip(offset).take(body_height).collect();
            frame.render_widget(Paragraph::new(visible), body_area);
        }
        PreviewBody::Binary(bytes) => {
            panel.files_view.borrow().painted_preview_max_scroll.set(0);
            frame.render_widget(
                Paragraph::new(Line::styled(
                    ui::clip_columns(&format!("Binary file ({bytes} bytes)"), pad_width),
                    Style::default().fg(palette.muted),
                )),
                body_area,
            );
        }
        PreviewBody::Notice(message) => {
            panel.files_view.borrow().painted_preview_max_scroll.set(0);
            frame.render_widget(
                Paragraph::new(Line::styled(
                    ui::clip_columns(&sanitize_single_line(&message), pad_width),
                    Style::default().fg(palette.muted),
                )),
                body_area,
            );
        }
    }
}

fn load_cached_preview(
    cell: &RefCell<FilesState>,
    path: &str,
    workspace_cwd: Option<&str>,
    snapshot: Option<&ProjectFileSnapshot>,
    generation: u64,
    palette: Palette,
) -> CachedPreview {
    let snapshot_ptr = snapshot.map_or(0, |s| s as *const ProjectFileSnapshot as usize);
    if let Some(cached) = cell.borrow().preview_cache.borrow().as_ref()
        && cached.path == path
        && cached.workspace_cwd.as_deref() == workspace_cwd
        && cached.generation == generation
        && cached.snapshot_ptr == snapshot_ptr
        && cached.palette == palette
    {
        return cached.clone();
    }
    let loaded = build_preview(path, workspace_cwd, snapshot, generation, palette);
    *cell.borrow().preview_cache.borrow_mut() = Some(loaded.clone());
    loaded
}

/// The relative path must still be listed by the backend, whose snapshot excludes
/// protected and unindexed paths. Both previewing and editing use this check.
fn indexed_file<'a>(
    path: &'a str,
    snapshot: Option<&ProjectFileSnapshot>,
) -> Result<&'a Path, &'static str> {
    let snapshot = snapshot.ok_or("Waiting for project file snapshot…")?;
    if !snapshot
        .entries
        .iter()
        .any(|entry| entry.kind == ProjectFileKind::File && entry.path == path)
    {
        return Err("File is not in the active project snapshot.");
    }
    let rel = Path::new(path);
    if rel.as_os_str().is_empty()
        || !rel
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err("Unsafe file path refused.");
    }
    Ok(rel)
}

fn file_listing_notice(state: &UiState, path: &str) -> &'static str {
    let parent = parent_dir(path).unwrap_or_default();
    let files = &state.project_files;
    if files
        .directory(parent)
        .is_some_and(|snapshot| !snapshot.truncated)
    {
        "File is not in the active folder listing."
    } else if directory_failure(state, parent).is_some() {
        "Folder could not be loaded. Press r to retry."
    } else if files.directory(parent).is_some() {
        "File unavailable in the partial folder listing."
    } else {
        "Waiting for project file listing…"
    }
}

pub(super) fn edit_target(
    path: &str,
    state: &UiState,
) -> Result<crate::external_editor::FileTarget, &'static str> {
    let snapshot = state
        .project_files
        .file_snapshot(path)
        .ok_or_else(|| file_listing_notice(state, path))?;
    let rel = indexed_file(path, Some(snapshot))?;
    let root = state
        .workspace_cwd
        .as_deref()
        .ok_or("Workspace root is unknown.")?;
    let root = std::fs::canonicalize(root).map_err(|_| "Workspace root unavailable.")?;
    let target = root.join(rel);
    // The editor takes a pathname, not the preview's already-open descriptor. Reject
    // symlink replacements (including links inside the workspace) before handoff.
    if std::fs::canonicalize(&target).map_err(|_| "File could not be opened.")? != target {
        return Err("Symbolic links are refused.");
    }
    crate::external_editor::FileTarget::resolve(&target, None).ok_or("Not a regular file.")
}

fn build_preview(
    path: &str,
    workspace_cwd: Option<&str>,
    snapshot: Option<&ProjectFileSnapshot>,
    generation: u64,
    palette: Palette,
) -> CachedPreview {
    let snapshot_ptr = snapshot.map_or(0, |s| s as *const ProjectFileSnapshot as usize);
    let base = CachedPreview {
        path: path.to_owned(),
        workspace_cwd: workspace_cwd.map(str::to_owned),
        generation,
        snapshot_ptr,
        palette,
        truncated: false,
        total_lines: 0,
        body: PreviewBody::Notice(String::new()),
    };

    let rel = match indexed_file(path, snapshot) {
        Ok(rel) => rel,
        Err(notice) => {
            return CachedPreview {
                body: PreviewBody::Notice(notice.into()),
                ..base
            };
        }
    };

    let Some(root_str) = workspace_cwd else {
        return CachedPreview {
            body: PreviewBody::Notice("Workspace root is unknown; preview unavailable.".into()),
            ..base
        };
    };

    let Ok(canonical_root) = std::fs::canonicalize(root_str) else {
        return CachedPreview {
            body: PreviewBody::Notice("Workspace root unavailable.".into()),
            ..base
        };
    };
    let (file_len, mut raw) = match read_preview_bytes_safe(&canonical_root, rel) {
        Ok(result) => result,
        Err(notice) => {
            return CachedPreview {
                body: PreviewBody::Notice(notice.into()),
                ..base
            };
        }
    };
    if raw.contains(&0) {
        return CachedPreview {
            body: PreviewBody::Binary(file_len),
            ..base
        };
    }

    let byte_truncated = raw.len() > PREVIEW_MAX_BYTES;
    if byte_truncated {
        raw.truncate(PREVIEW_MAX_BYTES);
    }
    let decoded = String::from_utf8_lossy(&raw);
    let mut sanitized_lines: Vec<String> = decoded
        .split_inclusive('\n')
        .map(|line| line.strip_suffix('\n').unwrap_or(line))
        .map(|line| line.strip_suffix('\r').unwrap_or(line))
        .map(sanitize_single_line)
        .take(PREVIEW_MAX_LINES + 1)
        .collect();
    let line_truncated = sanitized_lines.len() > PREVIEW_MAX_LINES;
    if line_truncated {
        sanitized_lines.truncate(PREVIEW_MAX_LINES);
    }

    let lang = syntax::language_for_path(path);
    let line_refs: Vec<&str> = sanitized_lines.iter().map(String::as_str).collect();
    let highlighted = (!lang.is_empty())
        .then(|| syntax::highlight_lines(&lang, &line_refs))
        .flatten();

    let total_lines = sanitized_lines.len();
    let num_width = total_lines.max(1).to_string().len();
    let mut rendered = Vec::with_capacity(total_lines);
    let line_num_style = Style::default().fg(palette.dim());
    let base_style = Style::default().fg(palette.foreground);

    for (index, line_text) in sanitized_lines.iter().enumerate() {
        let mut spans = vec![Span::styled(
            format!("{:>width$} │ ", index + 1, width = num_width),
            line_num_style,
        )];
        let tokens = highlighted
            .as_ref()
            .and_then(|all| all.get(index))
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        spans.extend(style_preview_line(line_text, tokens, base_style, palette));
        rendered.push(Line::from(spans));
    }

    CachedPreview {
        truncated: byte_truncated || line_truncated,
        total_lines,
        body: PreviewBody::Lines(rendered),
        ..base
    }
}

fn style_preview_line(
    line: &str,
    tokens: &[syntax::SyntaxToken],
    base: Style,
    palette: Palette,
) -> Vec<Span<'static>> {
    if line.is_empty() {
        return Vec::new();
    }
    if tokens.is_empty() {
        return vec![Span::styled(line.to_owned(), base)];
    }

    let mut spans = Vec::new();
    let mut cursor = 0usize;
    let mut run_text = String::new();
    let mut run_style = Style::default();
    let mut has_run = false;

    for (pos, grapheme) in line.grapheme_indices(true) {
        while tokens.get(cursor).is_some_and(|tok| tok.range.end <= pos) {
            cursor += 1;
        }
        let class = tokens
            .get(cursor)
            .filter(|tok| tok.range.contains(&pos))
            .map_or(SyntaxClass::Plain, |tok| tok.class);
        let mut style = palette.syntax(base, class);
        if class == SyntaxClass::Comment {
            style = style.add_modifier(Modifier::ITALIC);
        } else if class == SyntaxClass::Keyword {
            style = style.add_modifier(Modifier::BOLD);
        }
        if has_run && style == run_style {
            run_text.push_str(grapheme);
        } else {
            if has_run {
                spans.push(Span::styled(std::mem::take(&mut run_text), run_style));
            }
            run_text.push_str(grapheme);
            run_style = style;
            has_run = true;
        }
    }
    if has_run {
        spans.push(Span::styled(run_text, run_style));
    }
    spans
}

fn collect_touched_files(state: &UiState) -> Vec<String> {
    let norm_root = state
        .workspace_cwd
        .as_deref()
        .map(|root| normalize_lexical_path(Path::new(root)));
    let mut seen = HashSet::new();
    let mut touched = Vec::new();

    for entry in state.transcript.entries().iter().rev() {
        let TranscriptEntryKind::Tool(card) = &entry.kind else {
            continue;
        };
        let Some(ref_path) = card.referenced_path() else {
            continue;
        };
        let normalized = normalize_lexical_path(&ref_path);
        let rel = match &norm_root {
            Some(root) => {
                let Ok(stripped) = normalized.strip_prefix(root) else {
                    continue;
                };
                normalize_rel_path(&stripped.to_string_lossy())
            }
            None if normalized.is_relative() => normalize_rel_path(&normalized.to_string_lossy()),
            None => continue,
        };
        if rel.is_empty() || !seen.insert(rel.clone()) {
            continue;
        }
        touched.push(rel);
        if touched.len() >= MAX_TOUCHED_FILES {
            break;
        }
    }
    touched
}

fn compute_git_badge(file: &GitDiffFile) -> GitBadge {
    let status = match file.status.trim() {
        s if s.starts_with('A') => 'A',
        s if s.starts_with('D') => 'D',
        s if s.starts_with('R') => 'R',
        s if s.starts_with('?') || s.starts_with('U') => '?',
        _ => 'M',
    };
    let detail = git_presentation(&file.path, &file.before, &file.after, false);
    GitBadge {
        status,
        added: detail.additions,
        deleted: detail.deletions,
        edited_by_wisp: file.edited_by_wisp(),
    }
}

fn status_color(status: char, palette: Palette) -> ratatui::style::Color {
    match status {
        'A' | '?' => palette.success,
        'D' => palette.error,
        'R' => palette.primary,
        _ => palette.warning,
    }
}

fn normalize_rel_path(path: &str) -> String {
    path.replace('\\', "/")
        .trim_start_matches("./")
        .trim_matches('/')
        .to_owned()
}

fn join_rel(prefix: &str, name: &str) -> String {
    if prefix.is_empty() {
        name.to_owned()
    } else {
        format!("{prefix}/{name}")
    }
}

fn file_name(path: &str) -> String {
    path.rsplit('/').next().unwrap_or(path).to_owned()
}

fn parent_dir(path: &str) -> Option<&str> {
    path.rsplit_once('/').map(|(parent, _)| parent)
}

fn contains_point(rect: Rect, column: u16, row: u16) -> bool {
    !rect.is_empty()
        && column >= rect.x
        && column < rect.right()
        && row >= rect.y
        && row < rect.bottom()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_cards::{ToolCallInput, ToolResultInput};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use serde_json::json;
    use std::fs;

    fn sample_snapshot(generation: u64, paths: &[&str]) -> ProjectFileSnapshot {
        ProjectFileSnapshot {
            generation,
            entries: paths
                .iter()
                .map(|path| ProjectFileEntry {
                    path: path.trim_end_matches('/').to_owned(),
                    kind: if path.ends_with('/') {
                        ProjectFileKind::Directory
                    } else {
                        ProjectFileKind::File
                    },
                })
                .collect(),
            truncated: false,
        }
    }

    fn set_directory(state: &mut UiState, path: &str, entries: &[&str]) {
        let snapshot = sample_snapshot(state.project_files.generation(), entries);
        state.project_files.set_directory_for_test(path, snapshot);
    }

    fn draw_files(term: &mut Terminal<TestBackend>, panel: &SidePanel, state: &UiState) -> String {
        term.draw(|frame| {
            render(
                frame,
                frame.area(),
                frame.area(),
                state,
                panel,
                Palette::default(),
            );
        })
        .unwrap();
        buffer_text(term.backend().buffer())
    }

    fn invalidate(state: &mut UiState) {
        let generation = state.project_files.generation() + 1;
        crate::reducer::reduce(
            state,
            crate::reducer::UiAction::BackendEvent(
                crate::reducer::BackendEvent::ProjectFilesInvalidated(generation),
            ),
            &mut crate::SequentialCommandIds::default(),
        )
        .unwrap();
    }

    fn buffer_text(buf: &ratatui::buffer::Buffer) -> String {
        (0..buf.area().height)
            .map(|y| {
                (0..buf.area().width)
                    .map(|x| buf[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    struct TempDir(std::path::PathBuf);
    impl TempDir {
        fn new(tag: &str) -> Self {
            static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
            let id = COUNTER.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let path = std::env::temp_dir()
                .join(format!("wisp-files-test-{tag}-{}-{id}", std::process::id()));
            fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn root_opens_folders_lazily() {
        let mut state = UiState::new("fake".into(), None, None);
        state.workspace_cwd = Some("/repo".into());
        state.project_files.snapshot = Some(Arc::new(sample_snapshot(1, &["src/catalog.rs"])));
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        let mut term = Terminal::new(TestBackend::new(48, 18)).unwrap();

        let loading = draw_files(&mut term, &panel, &state);
        assert!(loading.contains("Loading project files…"), "{loading}");
        assert!(!loading.contains("catalog.rs"), "{loading}");

        set_directory(&mut state, "", &["src/", "README.md"]);
        let root = draw_files(&mut term, &panel, &state);
        assert!(root.contains("▸ \u{f024b} src/"), "{root}");
        assert!(root.contains("README.md"), "{root}");
        assert!(!root.contains("catalog.rs"), "{root}");
        assert!(panel.files_requested_directories(&state).is_empty());

        assert_eq!(
            panel.handle_files_key(KeyCode::Right, &state),
            OverlayKey::Moved
        );
        assert_eq!(panel.files_requested_directories(&state), ["src"]);
        state.project_files.set_directory_loading_for_test("src");
        let expanded = draw_files(&mut term, &panel, &state);
        assert!(expanded.contains("▾ \u{f0770} src/"), "{expanded}");
        assert!(expanded.contains("└─ Loading…"), "{expanded}");
        assert!(!expanded.contains("catalog.rs"), "{expanded}");

        invalidate(&mut state);
        assert!(panel.files_requested_directories(&state).is_empty());
        set_directory(&mut state, "", &["src/", "README.md"]);
        let reset = draw_files(&mut term, &panel, &state);
        assert!(reset.contains("▸ \u{f024b} src/"), "{reset}");
        assert!(panel.files_requested_directories(&state).is_empty());
    }

    #[test]
    fn branch_refresh_keeps_children() {
        let mut state = UiState::new("fake".into(), None, None);
        set_directory(&mut state, "", &["src/", "README.md"]);
        set_directory(&mut state, "src", &["src/lib.rs"]);
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        let mut term = Terminal::new(TestBackend::new(60, 18)).unwrap();
        draw_files(&mut term, &panel, &state);
        panel.handle_files_key(KeyCode::Right, &state);

        let idle = draw_files(&mut term, &panel, &state);
        assert!(idle.contains("lib.rs"), "{idle}");
        let idle_files = state.project_files.clone();
        state.project_files.set_directory_loading_for_test("src");
        let refreshing = draw_files(&mut term, &panel, &state);
        assert!(refreshing.contains("Refreshing…"), "{refreshing}");
        assert!(refreshing.contains("lib.rs"), "{refreshing}");

        state.project_files = idle_files;
        state
            .project_files
            .set_directory_error_for_test("src", "Permission denied");
        let failed = draw_files(&mut term, &panel, &state);
        assert!(
            failed.contains("Load failed · Enter retry · Permission denied"),
            "{failed}"
        );
        assert!(failed.contains("lib.rs"), "{failed}");
        for key in [KeyCode::Enter, KeyCode::Right] {
            assert_eq!(
                panel.handle_files_key(key, &state),
                OverlayKey::RetryProjectFileDirectory("src".into())
            );
        }

        let row = {
            let view = panel.files_view.borrow();
            let folder = view.selected_row().expect("folder row selected");
            view.painted_list_rect.get().y + folder.line as u16
        };
        assert_eq!(
            panel.click(2, row),
            OverlayKey::RetryProjectFileDirectory("src".into())
        );
        assert_eq!(
            panel.handle_files_key(KeyCode::Left, &state),
            OverlayKey::Moved
        );
        assert!(panel.files_requested_directories(&state).is_empty());
        let collapsed = draw_files(&mut term, &panel, &state);
        assert!(!collapsed.contains("Permission denied"), "{collapsed}");
    }

    #[test]
    fn expanded_tree_bounds_rows() {
        let mut state = UiState::new("fake".into(), None, None);
        set_directory(&mut state, "", &["a/", "b/"]);
        for directory in ["a", "b"] {
            let entries = (0..MAX_TREE_ROWS / 2)
                .map(|index| ProjectFileEntry {
                    path: format!("{directory}/{index}.rs"),
                    kind: ProjectFileKind::File,
                })
                .collect();
            state.project_files.set_directory_for_test(
                directory,
                ProjectFileSnapshot {
                    generation: 1,
                    entries,
                    truncated: false,
                },
            );
        }
        let view = FilesState {
            expanded_dirs: ["a".into(), "b".into()].into_iter().collect(),
            ..FilesState::default()
        };
        let (lines, rows) =
            build_tree_rows(&state, &[], &BTreeMap::new(), &view, 60, Palette::default());
        assert_eq!(rows.len(), MAX_TREE_ROWS);
        assert!(
            lines[0]
                .spans
                .iter()
                .any(|span| span.content.contains("view limited"))
        );
        assert!(rows.iter().any(|row| row.key() == "dir:b"));
    }

    #[test]
    fn search_reports_partial_matches() {
        let mut state = UiState::new("fake".into(), None, None);
        set_directory(&mut state, "", &["src/", "README.md"]);
        set_directory(&mut state, "src", &["src/main.rs"]);
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        let mut term = Terminal::new(TestBackend::new(52, 18)).unwrap();
        draw_files(&mut term, &panel, &state);
        panel.handle_files_key(KeyCode::Right, &state);
        draw_files(&mut term, &panel, &state);
        panel.handle_files_key(KeyCode::Down, &state);

        panel.handle_files_key(KeyCode::Char('/'), &state);
        assert!(panel.files_search_open());
        // Shortcut letters belong to the filter while it is being edited.
        for ch in ['f', 'a', 'r'] {
            assert_eq!(
                panel.handle_files_key(KeyCode::Char(ch), &state),
                OverlayKey::Moved
            );
        }
        state
            .project_files
            .set_partial_for_test(sample_snapshot(1, &["src/main.rs"]));
        let pending = draw_files(&mut term, &panel, &state);
        assert!(pending.contains("Searching project…"), "{pending}");
        assert!(!pending.contains("No matching"), "{pending}");
        assert!(!pending.contains("0 matches"), "{pending}");

        state
            .project_files
            .set_partial_for_test(sample_snapshot(1, &["unopened/pkg/far.rs"]));
        let partial = draw_files(&mut term, &panel, &state);
        assert!(partial.contains("Searching project…"), "{partial}");
        assert!(partial.contains("far.rs"), "{partial}");
        assert!(partial.contains("1+ match"), "{partial}");
        assert!(!partial.contains("README.md"), "{partial}");

        let mut limited = sample_snapshot(1, &["src/main.rs"]);
        limited.truncated = true;
        state.project_files.snapshot = Some(Arc::new(limited));
        let truncated = draw_files(&mut term, &panel, &state);
        assert!(truncated.contains("Search incomplete"), "{truncated}");
        assert!(!truncated.contains("No matching"), "{truncated}");

        state.project_files.snapshot = Some(Arc::new(sample_snapshot(1, &["src/main.rs"])));
        let complete = draw_files(&mut term, &panel, &state);
        assert!(complete.contains("No matching files."), "{complete}");
        assert!(!complete.contains("Searching"), "{complete}");
        assert_eq!(panel.files_requested_directories(&state), ["src"]);

        panel.handle_files_key(KeyCode::Esc, &state);
        let restored = draw_files(&mut term, &panel, &state);
        assert!(!panel.files_search_open());
        assert!(restored.contains("main.rs"), "{restored}");
        assert_eq!(
            panel.files_view.borrow().selected_file_path().as_deref(),
            Some("src/main.rs")
        );
    }

    #[test]
    fn narrow_rows_keep_the_filename_suffix_and_filter_caret() {
        let palette = Palette::default();
        let badge = GitBadge {
            status: 'M',
            added: u64::MAX,
            deleted: 7,
            edited_by_wisp: false,
        };
        let row = format_file_entry_line(
            tree_prefix(&[true; 8], false, 22, palette),
            "very-long-🧑‍💻-filename.rs",
            None,
            Some(badge),
            22,
            palette,
        );
        let text = row.to_string();
        assert_eq!(row.width(), 22);
        assert!(text.contains("M "), "{text}");
        assert!(!text.contains('\u{f1617}'), "{text}");
        assert!(text.trim_end().ends_with(".rs"), "{text}");
        assert!(!text.contains('+'), "{text}");
        assert_eq!(elide_middle("ab🧑‍💻vwxyz", 8), "ab…wxyz");

        let filter = filter_line("a/long/path/🧑‍💻.rs", true, 10, palette);
        assert_eq!(filter.width(), 10);
        assert_eq!(filter.to_string(), " / …🧑‍💻.rs▏");
    }

    #[test]
    fn nerd_file_icons_use_two_columns_and_yield_to_git_counts() {
        let palette = Palette::default();
        for (name, glyph) in [
            ("LIB.RS", "\u{f1617} "),
            ("README.md", "\u{f0354} "),
            (".gitignore", "\u{f02a2} "),
            ("Cargo.toml", "\u{f107c} "),
            ("data.unknown", "\u{f0224} "),
        ] {
            let icon = nerd_file_icon(name, palette);
            assert_eq!(icon.content, glyph, "{name}");
            assert_eq!(icon.width(), ICON_COLUMNS, "{name}");
        }

        let badge = GitBadge {
            status: 'M',
            added: 4,
            deleted: 2,
            edited_by_wisp: false,
        };
        let row = |width| {
            format_file_entry_line(
                vec![Span::raw(" ")],
                "a.rs",
                None,
                Some(badge),
                width,
                palette,
            )
            .to_string()
        };
        assert_eq!(row(13), " M a.rs +4 −2");
        assert_eq!(row(15), " M \u{f1617} a.rs +4 −2");
    }

    #[test]
    fn preview_enforces_snapshot_traversal_symlink_and_binary_guards() {
        let dir = TempDir::new("preview");
        let root = dir.path().join("repo");
        fs::create_dir_all(root.join("src")).unwrap();
        fs::write(root.join("src/main.rs"), "fn main() {}\n").unwrap();
        fs::write(root.join(".env"), "SECRET=do_not_expose\n").unwrap();
        fs::write(root.join("blob.bin"), [0_u8, 1, 2, 3]).unwrap();

        let outside = dir.path().join("outside.txt");
        fs::write(&outside, "OUTSIDE_SECRET").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, root.join("src/escape.txt")).unwrap();

        let snap = sample_snapshot(1, &["src/main.rs", "blob.bin", "src/escape.txt"]);
        let cwd = Some(root.to_str().unwrap());

        // Listed file renders highlighted lines.
        let ok = build_preview("src/main.rs", cwd, Some(&snap), 1, Palette::default());
        assert!(matches!(ok.body, PreviewBody::Lines(ref lines) if lines.len() == 1));

        // `.env` absent from ProjectFileSnapshot is refused even if present on disk.
        let hidden = build_preview(".env", cwd, Some(&snap), 1, Palette::default());
        assert!(matches!(hidden.body, PreviewBody::Notice(_)));

        // Binary files report size without decoding raw NUL bytes.
        let binary = build_preview("blob.bin", cwd, Some(&snap), 1, Palette::default());
        assert_eq!(binary.body, PreviewBody::Binary(4));

        // Out-of-root symlinks and in-root replacement symlinks are both refused via O_NOFOLLOW.
        #[cfg(unix)]
        {
            let escaped = build_preview("src/escape.txt", cwd, Some(&snap), 1, Palette::default());
            assert!(
                matches!(escaped.body, PreviewBody::Notice(ref msg) if msg.contains("refused")),
                "{:?}",
                escaped.body
            );

            // Replacement symlink pointing to protected in-workspace `.env` must also be refused.
            let link_to_env = root.join("src/main.rs");
            fs::remove_file(&link_to_env).unwrap();
            std::os::unix::fs::symlink(root.join(".env"), &link_to_env).unwrap();
            let linked = build_preview("src/main.rs", cwd, Some(&snap), 1, Palette::default());
            assert!(
                matches!(linked.body, PreviewBody::Notice(ref msg) if msg.contains("refused")),
                "{:?}",
                linked.body
            );

            // Intermediate directory replaced with symlink must also be refused.
            let linked_dir = root.join("linked_dir");
            std::os::unix::fs::symlink(root.join("src"), &linked_dir).unwrap();
            let dir_snap = sample_snapshot(1, &["linked_dir/escape.txt"]);
            let via_dir = build_preview(
                "linked_dir/escape.txt",
                cwd,
                Some(&dir_snap),
                1,
                Palette::default(),
            );
            assert!(
                matches!(via_dir.body, PreviewBody::Notice(ref msg) if msg.contains("refused")),
                "{:?}",
                via_dir.body
            );

            // Replacement FIFO (named pipe) must fail closed without blocking rendering.
            let fifo_path = root.join("src/pipe.txt");
            nix::unistd::mkfifo(
                &fifo_path,
                nix::sys::stat::Mode::S_IRUSR | nix::sys::stat::Mode::S_IWUSR,
            )
            .unwrap();
            let fifo_snap = sample_snapshot(1, &["src/pipe.txt"]);
            let fifo = build_preview("src/pipe.txt", cwd, Some(&fifo_snap), 1, Palette::default());
            assert!(
                matches!(fifo.body, PreviewBody::Notice(ref msg) if msg.contains("regular file")),
                "{:?}",
                fifo.body
            );
        }

        // `..` traversal is refused cleanly.
        let snap_with_dotdot = sample_snapshot(1, &["../outside.txt"]);
        let dotdot = build_preview(
            "../outside.txt",
            cwd,
            Some(&snap_with_dotdot),
            1,
            Palette::default(),
        );
        assert!(matches!(dotdot.body, PreviewBody::Notice(_)));
    }

    #[test]
    fn edit_target_requires_a_current_indexed_regular_file() {
        let dir = TempDir::new("edit-target");
        let root = dir.path().join("repo");
        fs::create_dir_all(root.join("src")).unwrap();
        fs::write(root.join("src/main.rs"), "fn main() {}\n").unwrap();
        fs::write(root.join(".env"), "unindexed\n").unwrap();
        std::os::unix::fs::symlink(root.join(".env"), root.join("src/link.rs")).unwrap();
        std::os::unix::fs::symlink(root.join("src"), root.join("linked")).unwrap();
        let mut state = UiState::new("fake".into(), None, None);
        state.workspace_cwd = Some(root.to_string_lossy().into_owned());
        set_directory(&mut state, "src", &["src/main.rs", "src/link.rs"]);
        set_directory(&mut state, "linked", &["linked/main.rs"]);
        state.project_files.snapshot = Some(Arc::new(sample_snapshot(1, &["../out", "src"])));
        assert_eq!(
            edit_target("src/main.rs", &state).unwrap().path(),
            root.canonicalize().unwrap().join("src/main.rs"),
        );
        for refused in [".env", "src/link.rs", "linked/main.rs", "../out", "src"] {
            assert!(edit_target(refused, &state).is_err(), "{refused}");
        }
        state.project_files.refresh();
        assert!(edit_target("src/main.rs", &state).is_ok());
        invalidate(&mut state);
        assert!(edit_target("src/main.rs", &state).is_err());
    }

    #[test]
    fn touched_files_and_git_badges_render_in_docked_panel() {
        let mut state = UiState::new("fake".into(), None, None);
        state.workspace_cwd = Some("/repo".into());
        state.transcript.observe_tool_call(ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: json!({"path": "/repo/src/app.rs"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
        });
        state.transcript.observe_tool_result(ToolResultInput {
            nested: Default::default(),
            call_id: "call-1".into(),
            name: "read".into(),
            output_source_bytes: 12,
            output_source_lines: 1,
            output: "fn app() {}".into(),
            output_tail: None,
            output_projection_cut_mid_line: false,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: Some(0),
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
            duration_ms: None,
            started_at_ms: None,
            process_started_at_ms: None,
            process_ended_at_ms: None,
            edit_locations: Vec::new(),
            images: Vec::new(),
            image_parts: Vec::new(),
        });
        state.git_diff.snapshot = Some(Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "HEAD".into(),
            files: vec![GitDiffFile {
                path: "src/app.rs".into(),
                status: "M".into(),
                before: "fn app() {}\n".into(),
                after: "fn app() {}\nfn extra() {}\n".into(),
                notice: String::new(),
                edits: Vec::new(),
            }],
            truncated: false,
        }));
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        let mut term = Terminal::new(TestBackend::new(48, 16)).unwrap();
        let initial = draw_files(&mut term, &panel, &state);
        assert!(initial.contains("Touched this session"), "{initial}");
        assert!(initial.contains("Loading project files…"), "{initial}");
        assert_eq!(panel.files_requested_directories(&state), ["src"]);

        set_directory(&mut state, "", &["src/"]);
        let text = draw_files(&mut term, &panel, &state);
        assert!(text.lines().any(|line| line.contains("Touched this session") && line.trim_end().ends_with('1')), "{text}");
        assert!(
            text.lines()
                .any(|line| line.contains("◆ M \u{f1617} app.rs  src")
                    && line.trim_end().ends_with("+1")),
            "{text}"
        );
        assert!(
            text.lines()
                .any(|line| line.contains("Project tree") && line.trim_end().ends_with("1 entry")),
            "{text}"
        );
    }

    #[test]
    fn preview_strips_embedded_carriage_returns_from_single_line_spans() {
        let dir = TempDir::new("wisp-files-cr");
        std::fs::write(dir.path().join("cr.txt"), "alpha\rbeta\nline2\r\n").unwrap();
        let snap = sample_snapshot(1, &["cr.txt"]);
        let preview = load_cached_preview(
            &RefCell::new(FilesState::default()),
            "cr.txt",
            Some(dir.path().to_str().unwrap()),
            Some(&snap),
            1,
            Palette::default(),
        );
        let PreviewBody::Lines(lines) = preview.body else {
            panic!("expected text lines");
        };
        assert_eq!(lines.len(), 2);
        for line in &lines {
            for span in &line.spans {
                assert!(
                    !span.content.contains('\n') && !span.content.contains('\r'),
                    "unexpected line break in span: {:?}",
                    span.content
                );
            }
        }
    }

    #[test]
    fn collapses_to_compacted_ancestor() {
        let mut state = UiState::new("fake".into(), None, None);
        set_directory(&mut state, "", &["a/"]);
        set_directory(&mut state, "a", &["a/x.rs", "a/b/"]);
        set_directory(&mut state, "a/b", &["a/b/c/"]);
        set_directory(&mut state, "a/b/c", &["a/b/c/y.rs"]);
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        let mut files_view = panel.files_view.borrow_mut();
        for path in ["a", "a/b", "a/b/c"] {
            files_view.set_dir_expanded(path, true);
        }
        drop(files_view);
        let mut term = Terminal::new(TestBackend::new(48, 16)).unwrap();
        term.draw(|frame| {
            render(
                frame,
                frame.area(),
                frame.area(),
                &state,
                &panel,
                Palette::default(),
            );
        })
        .unwrap();

        assert!(buffer_text(term.backend().buffer()).contains("b/c/"));
        assert_eq!(
            panel.files_requested_directories(&state),
            ["a", "a/b", "a/b/c"]
        );

        {
            let mut files_view = panel.files_view.borrow_mut();
            let abc_idx = files_view
                .painted_rows
                .borrow()
                .iter()
                .position(|row| matches!(&row.kind, SelectableRowKind::TreeDir { dir_path, .. } if dir_path == "a/b/c"))
                .expect("compacted a/b/c directory row");
            files_view.select_index(abc_idx);
            // First Left collapses `a/b/c`.
            assert_eq!(files_view.collapse_or_parent(), OverlayKey::Moved);
        }
        term.draw(|frame| {
            render(
                frame,
                frame.area(),
                frame.area(),
                &state,
                &panel,
                Palette::default(),
            );
        })
        .unwrap();
        // Second Left on the now-collapsed `a/b/c` walks past unrendered `a/b` to ancestor `a`.
        let mut files_view = panel.files_view.borrow_mut();
        assert_eq!(files_view.collapse_or_parent(), OverlayKey::Moved);
        assert_eq!(files_view.selected_key.as_deref(), Some("dir:a"));
    }

    #[test]
    fn scroll_list_by_persists_across_repaints() {
        let mut state = UiState::new("fake".into(), None, None);
        let paths: Vec<String> = (0..20).map(|i| format!("file_{i:02}.rs")).collect();
        let path_refs: Vec<&str> = paths.iter().map(String::as_str).collect();
        set_directory(&mut state, "", &path_refs);
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        let mut term = Terminal::new(TestBackend::new(48, 10)).unwrap();
        let draw_once = |term: &mut Terminal<TestBackend>| {
            term.draw(|frame| {
                render(
                    frame,
                    frame.area(),
                    frame.area(),
                    &state,
                    &panel,
                    Palette::default(),
                );
            })
            .unwrap();
        };

        draw_once(&mut term);
        let top = panel.files_view.borrow().painted_list_rect.get().y;
        assert_eq!(term.backend().buffer()[(47, top)].symbol(), "┃");
        assert_eq!(panel.files_view.borrow().list_scroll.get(), 0);
        panel.files_view.borrow().scroll_list_by(5);
        draw_once(&mut term);
        assert_eq!(panel.files_view.borrow().list_scroll.get(), 5);
        assert_eq!(term.backend().buffer()[(47, top)].symbol(), "│");
    }

    #[test]
    fn preview_cache_rebuilds_on_palette_or_refresh_change() {
        let dir = TempDir::new("wisp-files-cache");
        let file_path = dir.path().join("demo.rs");
        std::fs::write(&file_path, "fn first() {}\n").unwrap();
        let snap1 = Arc::new(sample_snapshot(1, &["demo.rs"]));
        let cell = RefCell::new(FilesState::default());

        let mut alt_palette = Palette::default();
        alt_palette.foreground = ratatui::style::Color::Rgb(12, 34, 56);

        let first = load_cached_preview(
            &cell,
            "demo.rs",
            Some(dir.path().to_str().unwrap()),
            Some(&snap1),
            1,
            Palette::default(),
        );
        let recolored = load_cached_preview(
            &cell,
            "demo.rs",
            Some(dir.path().to_str().unwrap()),
            Some(&snap1),
            1,
            alt_palette,
        );
        assert_ne!(first.body, recolored.body);

        // A refreshed snapshot (even with the same policy generation) reloads edited disk bytes.
        std::fs::write(&file_path, "fn updated() {}\n").unwrap();
        let snap2 = Arc::new(sample_snapshot(1, &["demo.rs"]));
        let refreshed = load_cached_preview(
            &cell,
            "demo.rs",
            Some(dir.path().to_str().unwrap()),
            Some(&snap2),
            1,
            alt_palette,
        );
        assert_ne!(recolored.body, refreshed.body);
    }
}
