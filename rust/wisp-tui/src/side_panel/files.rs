//! Agent-aware `Files` side-panel tab: session-touched files, compacted project tree,
//! inline `/` filter, and safe syntax-highlighted file preview in the expanded overlay.

use super::*;
use crate::file_picker::format_reference;
use crate::reducer::git_diff::{GitDiffFile, GitDiffSnapshot};
use crate::syntax::{self, SyntaxClass};
use crate::tool_detail::git_presentation;
use crate::transcript::{TranscriptEntryKind, normalize_lexical_path};
use crate::ui::sanitize_for_terminal;
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs::File;
use std::io::Read;
use std::path::{Component, Path};
use unicode_segmentation::UnicodeSegmentation;
use wisp_protocol::events::{ProjectFileEntry, ProjectFileKind, ProjectFileSnapshot};

const MAX_TOUCHED_FILES: usize = 12;
const AUTO_EXPAND_FILE_THRESHOLD: usize = 40;
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
    TouchedFile { path: String },
    TreeDir { dir_path: String, expanded: bool },
    TreeFile { path: String },
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
    initialized_generation: Option<u64>,
    collapsed_dirs: BTreeSet<String>,
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
        !self.filter.is_empty()
    }

    pub(super) fn reset_for_workspace(&mut self) {
        self.filter.clear();
        self.filter_active = false;
        self.selected_index = 0;
        self.selected_key = None;
        self.list_scroll.set(0);
        self.preview_scroll.set(0);
        self.initialized_generation = None;
        self.last_workspace_cwd = None;
        self.reveal_selection.set(true);
        self.collapsed_dirs.clear();
        self.expanded_dirs.clear();
        *self.diff_cache.borrow_mut() = None;
        *self.preview_cache.borrow_mut() = None;
        self.painted_rows.borrow_mut().clear();
    }

    fn sync_workspace_cwd(&mut self, cwd: Option<&str>) {
        if let Some(prev) = &self.last_workspace_cwd
            && cwd != Some(prev.as_str())
        {
            self.reset_for_workspace();
        }
        self.last_workspace_cwd = cwd.map(str::to_owned);
    }

    /// Initialize default fold state when a new snapshot arrives: compact large trees
    /// while keeping touched and git-changed branches expanded.
    fn sync_snapshot_defaults(
        &mut self,
        snapshot: Option<&ProjectFileSnapshot>,
        touched: &[String],
        badges: &BTreeMap<String, GitBadge>,
    ) {
        let Some(snapshot) = snapshot else {
            return;
        };
        if self.initialized_generation == Some(snapshot.generation) {
            return;
        }
        self.initialized_generation = Some(snapshot.generation);
        let file_count = snapshot
            .entries
            .iter()
            .filter(|entry| entry.kind == ProjectFileKind::File)
            .count();
        self.collapsed_dirs.clear();
        self.expanded_dirs.clear();
        if file_count <= AUTO_EXPAND_FILE_THRESHOLD {
            return;
        }

        let mut active_dirs = HashSet::<String>::new();
        for path in touched.iter().chain(badges.keys()) {
            let mut cur = Path::new(path);
            while let Some(parent) = cur.parent() {
                let s = parent.to_string_lossy();
                if s.is_empty() {
                    break;
                }
                active_dirs.insert(s.into_owned());
                cur = parent;
            }
        }
        for entry in &snapshot.entries {
            let rel = entry.path.trim_matches('/');
            if rel.is_empty() {
                continue;
            }
            if entry.kind == ProjectFileKind::Directory
                && rel.contains('/')
                && !active_dirs.contains(rel)
            {
                self.collapsed_dirs.insert(rel.to_owned());
            }
        }
    }

    fn is_dir_expanded(&self, dir_path: &str) -> bool {
        if !self.filter.is_empty() {
            return true;
        }
        if self.expanded_dirs.contains(dir_path) {
            return true;
        }
        !self.collapsed_dirs.contains(dir_path)
    }

    fn set_dir_expanded(&mut self, dir_path: &str, expanded: bool) {
        self.reveal_selection.set(true);
        if expanded {
            self.collapsed_dirs.remove(dir_path);
            self.expanded_dirs.insert(dir_path.to_owned());
        } else {
            self.expanded_dirs.remove(dir_path);
            self.collapsed_dirs.insert(dir_path.to_owned());
        }
    }

    fn selected_row(&self) -> Option<SelectableRow> {
        let rows = self.painted_rows.borrow();
        if let Some(key) = &self.selected_key
            && let Some(found) = rows.iter().find(|row| row.key() == *key)
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
        if self.filter_active {
            match key {
                KeyCode::Esc => {
                    self.filter.clear();
                    self.filter_active = false;
                    self.list_scroll.set(0);
                    self.reveal_selection.set(true);
                    return (OverlayKey::Moved, false);
                }
                KeyCode::Enter => {
                    self.filter_active = false;
                    return (OverlayKey::Moved, false);
                }
                KeyCode::Backspace => {
                    if self.filter.pop().is_some() {
                        self.list_scroll.set(0);
                        self.reveal_selection.set(true);
                    }
                    return (OverlayKey::Moved, false);
                }
                KeyCode::Up => return (self.move_selection(-1), false),
                KeyCode::Down => return (self.move_selection(1), false),
                KeyCode::Char(ch) if !ch.is_control() => {
                    self.filter.push(ch);
                    self.list_scroll.set(0);
                    self.reveal_selection.set(true);
                    return (OverlayKey::Moved, false);
                }
                _ => return (OverlayKey::Ignored, false),
            }
        }

        match key {
            KeyCode::Esc if !self.filter.is_empty() => {
                self.filter.clear();
                self.list_scroll.set(0);
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
            KeyCode::Right => (self.expand_selected(), false),
            KeyCode::Enter | KeyCode::Char(' ') => self.activate_selected(expanded),
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
                *self.preview_cache.borrow_mut() = None;
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
        let was_selected = self.selected_row().as_ref().is_some_and(|cur| {
            self.painted_rows
                .borrow()
                .get(idx)
                .is_some_and(|next| cur.key() == next.key())
        });
        self.filter_active = false;
        self.select_index(idx);
        let Some(selected) = self.painted_rows.borrow().get(idx).cloned() else {
            return (OverlayKey::Moved, false);
        };
        match selected.kind {
            SelectableRowKind::TreeDir {
                dir_path,
                expanded: dir_expanded,
            } => {
                self.set_dir_expanded(&dir_path, !dir_expanded);
                (OverlayKey::Moved, false)
            }
            SelectableRowKind::TouchedFile { .. } | SelectableRowKind::TreeFile { .. } => {
                (OverlayKey::Moved, was_selected && !expanded)
            }
        }
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
            && let Some(pos) = rows.iter().position(|r| r.key() == *key)
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
            SelectableRowKind::TreeDir {
                ref dir_path,
                expanded: true,
            } => {
                self.set_dir_expanded(dir_path, false);
            }
            SelectableRowKind::TreeDir { ref dir_path, .. } => {
                if let Some(parent) = parent_dir(dir_path) {
                    self.select_matching_dir_ancestor(parent);
                }
            }
            SelectableRowKind::TreeFile { ref path } => {
                if let Some(parent) = parent_dir(path) {
                    self.select_matching_dir_ancestor(parent);
                }
            }
            SelectableRowKind::TouchedFile { .. } => {}
        }
        OverlayKey::Moved
    }

    fn expand_selected(&mut self) -> OverlayKey {
        if let Some(SelectableRowKind::TreeDir { dir_path, .. }) =
            self.selected_row().map(|row| row.kind)
        {
            self.set_dir_expanded(&dir_path, true);
        }
        OverlayKey::Moved
    }

    fn activate_selected(&mut self, expanded: bool) -> (OverlayKey, bool) {
        let Some(row) = self.selected_row() else {
            return (OverlayKey::Ignored, false);
        };
        match row.kind {
            SelectableRowKind::TreeDir {
                dir_path,
                expanded: dir_expanded,
            } => {
                self.set_dir_expanded(&dir_path, !dir_expanded);
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
            if let Some(idx) = rows.iter().position(|row| row.key() == key) {
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
            SelectableRowKind::TreeDir { dir_path, .. } => format!("dir:{dir_path}"),
            SelectableRowKind::TreeFile { path } => format!("file:{path}"),
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

    let badges = panel.files_view.borrow().badge_map(state);
    let touched = collect_touched_files(state);
    let snapshot = state.project_files.snapshot();
    {
        let mut files_view = panel.files_view.borrow_mut();
        files_view.sync_workspace_cwd(state.workspace_cwd.as_deref());
        files_view.sync_snapshot_defaults(snapshot, &touched, &badges);
    }

    let split_expanded = panel.expanded() && width >= EXPANDED_SPLIT_MIN_WIDTH;
    let footer_height = usize::from(panel.overlay_open() || split_expanded || height >= 6);
    let content_area = Rect {
        height: inner.height.saturating_sub(footer_height as u16),
        ..inner
    };

    if split_expanded {
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
        render_tree_pane(
            frame, tree_area, surface, state, panel, snapshot, &touched, &badges, palette, true,
        );
        render_divider(frame, divider_area, palette);
        render_preview_pane(
            frame,
            preview_area,
            state,
            panel,
            snapshot,
            &badges,
            palette,
        );
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
            snapshot,
            &touched,
            &badges,
            palette,
            false,
        );
    }

    if footer_height > 0 {
        let footer = Rect {
            y: content_area.bottom(),
            height: footer_height as u16,
            ..inner
        };
        let files_view = panel.files_view.borrow();
        let hint_text = if files_view.filter_active {
            "Type to filter · ↑↓ select · Enter keep · Esc clear"
        } else if split_expanded {
            "↑↓ select · ←→ fold · PgUp/PgDn scroll · @ attach · d /diff · / filter · f restore · Esc"
        } else if panel.expanded() {
            "↑↓ select · ←→ fold · @ attach · d diff · / filter · f restore · Esc"
        } else if panel.overlay_open() {
            "Enter preview · @ attach · d diff · / filter · [ ] tab · f expand · Esc"
        } else {
            "F8 focus · / filter · @ attach · d diff · f expand"
        };
        frame.render_widget(Paragraph::new(ui::keycap_hints(hint_text, palette)), footer);
    }
}

#[allow(clippy::too_many_arguments)]
fn render_tree_pane(
    frame: &mut Frame<'_>,
    area: Rect,
    surface: Rect,
    state: &UiState,
    panel: &SidePanel,
    snapshot: Option<&ProjectFileSnapshot>,
    touched: &[String],
    badges: &BTreeMap<String, GitBadge>,
    palette: Palette,
    is_split: bool,
) {
    let width = usize::from(area.width);
    let height = usize::from(area.height);
    if width == 0 || height == 0 {
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
    if filter_rect.height > 0 {
        let files_view = panel.files_view.borrow();
        let line = filter_line(&files_view.filter, files_view.filter_active, width, palette);
        frame.render_widget(Paragraph::new(line), filter_rect);
        files_view.painted_filter_rect.set(filter_rect);
    }

    let list_rect = Rect {
        y: filter_rect.bottom(),
        height: remaining_after_controls
            .height
            .saturating_sub(filter_rect.height),
        ..remaining_after_controls
    };

    let (lines, rows) = {
        let files_view = panel.files_view.borrow();
        build_tree_rows(
            state,
            snapshot,
            touched,
            badges,
            &files_view,
            width,
            palette,
        )
    };

    let mut rendered_lines = lines;
    let total = rendered_lines.len();
    let list_height = usize::from(list_rect.height);
    let selected_idx = {
        let mut files_view = panel.files_view.borrow_mut();
        files_view.painted_list_rect.set(list_rect);
        files_view.painted_list_total.set(total);
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
        && (panel.overlay_open() || is_split)
    {
        let line_idx = panel.files_view.borrow().painted_rows.borrow()[idx].line;
        if let Some(line) = rendered_lines.get_mut(line_idx) {
            let padding = width.saturating_sub(line.width());
            line.spans.push(Span::raw(" ".repeat(padding)));
            *line = palette.selected_line(std::mem::take(line));
        }
    }

    let max_scroll = total.saturating_sub(list_height.max(1));
    let offset = panel.files_view.borrow().list_scroll.get().min(max_scroll);
    panel.files_view.borrow().list_scroll.set(offset);
    let visible: Vec<_> = rendered_lines
        .into_iter()
        .skip(offset)
        .take(list_height)
        .collect();
    frame.render_widget(Paragraph::new(visible), list_rect);

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
    if active {
        let query = sanitize_single_line(filter);
        let text = format!("/ {query}▏");
        return Line::styled(
            ui::clip_columns(&text, width),
            Style::default().fg(palette.foreground),
        );
    }
    if filter.is_empty() {
        return Line::from(vec![
            Span::styled("/ ", accent),
            Span::styled(
                ui::clip_columns("filter files…", width.saturating_sub(2)),
                muted,
            ),
        ]);
    }
    let query = sanitize_single_line(filter);
    spread(
        vec![
            Span::styled("/ ", accent),
            Span::styled(query, Style::default().fg(palette.foreground)),
        ],
        vec![Span::styled("Esc clear", muted)],
        width,
    )
}

fn build_tree_rows(
    state: &UiState,
    snapshot: Option<&ProjectFileSnapshot>,
    touched: &[String],
    badges: &BTreeMap<String, GitBadge>,
    files_view: &FilesState,
    width: usize,
    palette: Palette,
) -> (Vec<Line<'static>>, Vec<SelectableRow>) {
    let mut lines = Vec::new();
    let mut rows = Vec::new();
    let muted = Style::default().fg(palette.muted);
    let dim = Style::default().fg(palette.dim());
    let query = files_view.filter.trim().to_ascii_lowercase();

    let filtered_touched: Vec<&String> = touched
        .iter()
        .filter(|path| query.is_empty() || path.to_ascii_lowercase().contains(&query))
        .collect();
    if !filtered_touched.is_empty() {
        lines.push(Line::from(heading(
            &format!("TOUCHED THIS SESSION · {}", filtered_touched.len()),
            palette,
        )));
        for path in filtered_touched {
            let line_idx = lines.len();
            let name = file_name(path);
            let badge = badges.get(path.as_str()).copied();
            lines.push(format_file_entry_line(
                0, &name, badge, true, width, palette,
            ));
            rows.push(SelectableRow {
                line: line_idx,
                kind: SelectableRowKind::TouchedFile { path: path.clone() },
            });
        }
        lines.push(Line::default());
    }

    let Some(snapshot) = snapshot else {
        if state.project_files.loading() {
            lines.push(Line::styled("Loading project files…", muted));
        } else if let Some(err) = state.project_files.error.as_deref() {
            lines.push(Line::styled(
                ui::clip_columns(&sanitize_single_line(err), width),
                Style::default().fg(palette.error),
            ));
        } else {
            lines.push(Line::styled("Project file list unavailable.", dim));
        }
        return (lines, rows);
    };

    let filtered_entries: Vec<&ProjectFileEntry> = snapshot
        .entries
        .iter()
        .filter(|entry| {
            query.is_empty()
                || (entry.kind == ProjectFileKind::File
                    && entry.path.to_ascii_lowercase().contains(&query))
        })
        .collect();

    let total_files = filtered_entries
        .iter()
        .filter(|entry| entry.kind == ProjectFileKind::File)
        .count();
    let suffix = if snapshot.truncated { "+" } else { "" };
    lines.push(Line::from(heading(
        &format!("PROJECT TREE · {total_files}{suffix} files"),
        palette,
    )));

    let root = build_dir_trie(&filtered_entries);
    if root.subdirs.is_empty() && root.files.is_empty() {
        lines.push(Line::styled(
            if query.is_empty() {
                "No project files reported."
            } else {
                "No matching files."
            },
            dim,
        ));
        return (lines, rows);
    }

    emit_dir_children(
        &root, "", 0, files_view, badges, width, palette, &mut lines, &mut rows,
    );
    (lines, rows)
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

#[allow(clippy::too_many_arguments)]
fn emit_dir_children(
    node: &DirNode,
    prefix: &str,
    depth: usize,
    files_view: &FilesState,
    badges: &BTreeMap<String, GitBadge>,
    width: usize,
    palette: Palette,
    lines: &mut Vec<Line<'static>>,
    rows: &mut Vec<SelectableRow>,
) {
    for (name, child) in &node.subdirs {
        let mut label = name.clone();
        let mut dir_path = join_rel(prefix, name);
        let mut target = child;
        // Single-child directory compaction keeps narrow 44–52 col trees readable.
        while target.files.is_empty() && target.subdirs.len() == 1 {
            let (sub_name, sub_node) = target.subdirs.iter().next().expect("checked len == 1");
            label.push('/');
            label.push_str(sub_name);
            dir_path = join_rel(&dir_path, sub_name);
            target = sub_node;
        }
        let expanded = files_view.is_dir_expanded(&dir_path);
        let line_idx = lines.len();
        lines.push(format_dir_line(depth, &label, expanded, width, palette));
        rows.push(SelectableRow {
            line: line_idx,
            kind: SelectableRowKind::TreeDir {
                dir_path: dir_path.clone(),
                expanded,
            },
        });
        if expanded {
            emit_dir_children(
                target,
                &dir_path,
                depth + 1,
                files_view,
                badges,
                width,
                palette,
                lines,
                rows,
            );
        }
    }

    for path in &node.files {
        let line_idx = lines.len();
        let name = file_name(path);
        let badge = badges.get(path.as_str()).copied();
        lines.push(format_file_entry_line(
            depth, &name, badge, false, width, palette,
        ));
        rows.push(SelectableRow {
            line: line_idx,
            kind: SelectableRowKind::TreeFile { path: path.clone() },
        });
    }
}

fn format_dir_line(
    depth: usize,
    label: &str,
    expanded: bool,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let indent = "  ".repeat(depth.min(6));
    let arrow = if expanded { "▾ " } else { "▸ " };
    let text = format!("{indent}{arrow}{}/", sanitize_single_line(label));
    Line::from(vec![Span::styled(
        ui::clip_columns(&text, width),
        Style::default()
            .fg(palette.foreground)
            .add_modifier(Modifier::BOLD),
    )])
}

fn format_file_entry_line(
    depth: usize,
    name: &str,
    badge: Option<GitBadge>,
    is_touched: bool,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let indent = "  ".repeat(depth.min(6));
    let marker = if is_touched { "▸ " } else { "  " };
    let mut left = vec![Span::styled(
        format!("{indent}{marker}"),
        Style::default().fg(palette.muted),
    )];
    if let Some(badge) = badge {
        left.push(Span::styled(
            format!("{} ", badge.status),
            Style::default()
                .fg(status_color(badge.status, palette))
                .add_modifier(Modifier::BOLD),
        ));
    }
    left.push(Span::styled(
        sanitize_single_line(name),
        Style::default().fg(palette.foreground),
    ));

    let mut right = Vec::new();
    if let Some(badge) = badge {
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
    snapshot: Option<&ProjectFileSnapshot>,
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

    let generation = snapshot.map_or(0, |s| s.generation);
    let preview = load_cached_preview(
        &panel.files_view,
        &path,
        state.workspace_cwd.as_deref(),
        snapshot,
        generation,
        palette,
    );

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

    // Fail closed: only read files explicitly exposed by the backend's current snapshot,
    // which already excludes protected_paths and unindexed private locations.
    let Some(snapshot) = snapshot else {
        return CachedPreview {
            body: PreviewBody::Notice("Waiting for project file snapshot…".into()),
            ..base
        };
    };
    let in_snapshot = snapshot
        .entries
        .iter()
        .any(|entry| entry.kind == ProjectFileKind::File && entry.path == path);
    if !in_snapshot {
        return CachedPreview {
            body: PreviewBody::Notice("File is not in the active project snapshot.".into()),
            ..base
        };
    }

    let Some(root_str) = workspace_cwd else {
        return CachedPreview {
            body: PreviewBody::Notice("Workspace root is unknown; preview unavailable.".into()),
            ..base
        };
    };

    let rel = Path::new(path);
    if rel.as_os_str().is_empty()
        || rel.is_absolute()
        || !rel
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return CachedPreview {
            body: PreviewBody::Notice("Unsafe file path refused.".into()),
            ..base
        };
    }

    let Ok(canonical_root) = std::fs::canonicalize(root_str) else {
        return CachedPreview {
            body: PreviewBody::Notice("Workspace root unavailable.".into()),
            ..base
        };
    };
    let Ok(canonical_target) = std::fs::canonicalize(canonical_root.join(rel)) else {
        return CachedPreview {
            body: PreviewBody::Notice("File could not be opened.".into()),
            ..base
        };
    };
    if !canonical_target.starts_with(&canonical_root) {
        return CachedPreview {
            body: PreviewBody::Notice("Path outside workspace root refused.".into()),
            ..base
        };
    }

    let Ok(mut file) = File::open(&canonical_target) else {
        return CachedPreview {
            body: PreviewBody::Notice("File could not be read.".into()),
            ..base
        };
    };
    let Ok(meta) = file.metadata() else {
        return CachedPreview {
            body: PreviewBody::Notice("File metadata unavailable.".into()),
            ..base
        };
    };
    if !meta.is_file() {
        return CachedPreview {
            body: PreviewBody::Notice("Not a regular file.".into()),
            ..base
        };
    }

    let mut raw = Vec::new();
    if file
        .by_ref()
        .take((PREVIEW_MAX_BYTES + 1) as u64)
        .read_to_end(&mut raw)
        .is_err()
    {
        return CachedPreview {
            body: PreviewBody::Notice("Failed to read file.".into()),
            ..base
        };
    }
    if raw.contains(&0) {
        return CachedPreview {
            body: PreviewBody::Binary(meta.len()),
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
                    path: (*path).to_owned(),
                    kind: ProjectFileKind::File,
                })
                .collect(),
            truncated: false,
        }
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
    fn single_child_directories_compact_and_filter_narrows_tree() {
        let mut state = UiState::new("fake".into(), None, None);
        state.project_files.snapshot = Some(Arc::new(sample_snapshot(
            1,
            &[
                "src/wisp/rpc/handlers/shell.py",
                "src/wisp/coding/session.py",
                "rust/wisp-tui/src/side_panel.rs",
            ],
        )));
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
        panel.width = 160;

        let mut term = Terminal::new(TestBackend::new(48, 18)).unwrap();
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
        let text = buffer_text(term.backend().buffer());
        assert!(text.contains("src/wisp/"), "{text}");
        assert!(text.contains("rpc/handlers/"), "{text}");
        assert!(text.contains("rust/wisp-tui/src/"), "{text}");

        // Typing '/' then filter chars narrows rows and ignores shortcut letters like 'f'.
        panel.files_view.borrow_mut().filter_active = true;
        let expanded = panel.expanded();
        assert_eq!(
            panel
                .files_view
                .borrow_mut()
                .handle_key(KeyCode::Char('s'), expanded, &state)
                .0,
            OverlayKey::Moved
        );
        assert_eq!(
            panel
                .files_view
                .borrow_mut()
                .handle_key(KeyCode::Char('f'), expanded, &state)
                .0,
            OverlayKey::Moved
        );
        panel.files_view.borrow_mut().filter = "shell".into();
        panel.files_view.borrow_mut().filter_active = false;
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
        let filtered = buffer_text(term.backend().buffer());
        assert!(filtered.contains("shell.py"), "{filtered}");
        assert!(!filtered.contains("session.py"), "{filtered}");
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

        // Out-of-root symlinks are refused after canonicalization even if listed.
        #[cfg(unix)]
        {
            let escaped = build_preview("src/escape.txt", cwd, Some(&snap), 1, Palette::default());
            assert!(
                matches!(escaped.body, PreviewBody::Notice(ref msg) if msg.contains("outside"))
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
        state.project_files.snapshot = Some(Arc::new(sample_snapshot(1, &["src/app.rs"])));

        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
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
        let text = buffer_text(term.backend().buffer());
        assert!(text.contains("TOUCHED THIS SESSION · 1"), "{text}");
        assert!(text.contains("M app.rs"), "{text}");
        assert!(text.contains("+1"), "{text}");
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
    fn collapse_or_parent_selects_compacted_ancestor_directory() {
        let mut state = UiState::new("fake".into(), None, None);
        state.project_files.snapshot =
            Some(Arc::new(sample_snapshot(1, &["a/x.rs", "a/b/c/y.rs"])));
        let mut panel = SidePanel::default();
        panel.show(PanelMode::Files);
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
        state.project_files.snapshot = Some(Arc::new(sample_snapshot(1, &path_refs)));
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
        assert_eq!(panel.files_view.borrow().list_scroll.get(), 0);
        panel.files_view.borrow().scroll_list_by(5);
        assert_eq!(panel.files_view.borrow().list_scroll.get(), 5);
        draw_once(&mut term);
        assert_eq!(panel.files_view.borrow().list_scroll.get(), 5);
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
