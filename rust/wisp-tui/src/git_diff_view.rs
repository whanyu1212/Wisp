//! Changed-file navigation over a backend-owned read-only snapshot.
//!
//! `/diff` reads top to bottom as a header row (what is compared, the totals and the
//! snapshot's age), a file tree beside the selected file's diff on a wide terminal,
//! and a row of keys. Narrow terminals show the tree or the diff, switched by Tab.
use crate::{
    diff_review::DiffReview,
    reducer::git_diff::{GitDiff, GitDiffEdit, GitDiffFile, GitDiffSnapshot},
    side_panel::spread,
    theme::Palette,
    tool_detail::{ToolDetailPresentation, git_presentation},
    transcript::{Transcript, TranscriptEntryId, TranscriptRole},
    ui::sanitize_for_terminal,
};
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Clear, Paragraph},
};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
    time::Duration,
};
use unicode_width::UnicodeWidthStr;

/// The tree's width on a wide terminal, its rule included.
const TREE_WIDTH: u16 = 46;
/// The cells of a file's churn bar.
const CHURN_CELLS: usize = 6;

/// The transcript card of the session's latest edit or write call on a file.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct EditCard {
    pub call_id: String,
    /// The session entry holding the call's result.
    pub entry_id: String,
    /// The file, so `/diff` can open at it when the card is not in the transcript.
    pub path: String,
}

impl EditCard {
    pub(crate) fn of(edit: &GitDiffEdit, path: &str) -> Self {
        Self {
            call_id: edit.call_id.clone(),
            entry_id: edit.entry_id.clone(),
            path: path.to_owned(),
        }
    }
}

pub(crate) struct GitDiffView {
    snapshot: Option<Arc<GitDiffSnapshot>>,
    /// Each file's diff with three lines of context, which the tree counts from.
    details: Vec<ToolDetailPresentation>,
    /// The active file's diff with every unchanged line, while `c` shows context.
    full_context: Option<(usize, ToolDetailPresentation)>,
    collapsed: BTreeSet<String>,
    rows: Vec<TreeRow>,
    selected: usize,
    active: usize,
    tree_focus: bool,
    review: DiffReview,
    /// The file to select when a snapshot arrives, until the user moves. Opening
    /// `/diff` reads a fresh snapshot, so the file is found again in that one.
    opened_at: Option<String>,
}

impl Default for GitDiffView {
    fn default() -> Self {
        Self {
            snapshot: None,
            details: Vec::new(),
            full_context: None,
            collapsed: BTreeSet::new(),
            rows: Vec::new(),
            selected: 0,
            active: 0,
            tree_focus: false,
            review: DiffReview::working_tree(),
            opened_at: None,
        }
    }
}

/// One selectable row of the tree: a folder, or a file of the snapshot.
#[derive(Clone, Debug, Eq, PartialEq)]
struct TreeRow {
    /// A folder's collapse key (`tracked:src/wisp`), or a file's path.
    key: String,
    file: Option<usize>,
    /// A folder's path from its parent row, single-child folders joined
    /// (`src/wisp/providers/`), or a file's name.
    name: String,
    depth: usize,
    untracked: bool,
    /// A folder's files, at any depth, by snapshot index.
    files: Vec<usize>,
}

/// A folder's files and subfolders while the tree is built.
#[derive(Default)]
struct Folder<'a> {
    folders: BTreeMap<&'a str, Folder<'a>>,
    files: Vec<(&'a str, usize)>,
}

impl<'a> Folder<'a> {
    fn insert(&mut self, parts: &[&'a str], index: usize) {
        match parts {
            [name] => self.files.push((name, index)),
            [folder, rest @ ..] => self.folders.entry(folder).or_default().insert(rest, index),
            [] => {}
        }
    }

    fn file_indexes(&self, into: &mut Vec<usize>) {
        into.extend(self.files.iter().map(|(_, index)| *index));
        for folder in self.folders.values() {
            folder.file_indexes(into);
        }
    }
}

impl GitDiffView {
    /// `/diff` with `path` selected in the tree, when the snapshot has it.
    pub(crate) fn opened_at(path: Option<String>) -> Self {
        Self {
            opened_at: path,
            ..Self::default()
        }
    }

    fn sync(&mut self, state: &GitDiff) {
        let changed = match (&self.snapshot, &state.snapshot) {
            (Some(a), Some(b)) => !Arc::ptr_eq(a, b),
            (None, None) => false,
            _ => true,
        };
        if !changed {
            return;
        }
        self.snapshot = state.snapshot.clone();
        self.details = self
            .snapshot
            .as_ref()
            .map(|s| {
                s.files
                    .iter()
                    .map(|f| git_presentation(&f.path, &f.before, &f.after, false))
                    .collect()
            })
            .unwrap_or_default();
        self.full_context = None;
        self.active = 0;
        self.selected = 0;
        self.tree_focus = true;
        self.review.reset();
        self.rebuild();
        if let Some(path) = &self.opened_at {
            if let Some(position) = self
                .rows
                .iter()
                .position(|row| row.file.is_some() && row.key == *path)
            {
                self.selected = position;
                self.active = self.rows[position].file.expect("a file row");
            }
        }
    }

    /// Lay the snapshot out as tracked files, then untracked ones, each as a tree.
    fn rebuild(&mut self) {
        self.rows.clear();
        let Some(snapshot) = self.snapshot.clone() else {
            return;
        };
        for untracked in [false, true] {
            let mut root = Folder::default();
            for (index, file) in snapshot.files.iter().enumerate() {
                if (file.status == "untracked") == untracked {
                    root.insert(&file.path.split('/').collect::<Vec<_>>(), index);
                }
            }
            self.emit(&root, "", 0, untracked);
        }
        self.selected = self.selected.min(self.rows.len().saturating_sub(1));
    }

    fn emit(&mut self, folder: &Folder<'_>, prefix: &str, depth: usize, untracked: bool) {
        // Folders and files interleave by name, as Git lists paths.
        let mut entries: Vec<(&str, Option<&Folder<'_>>, Option<usize>)> = folder
            .folders
            .iter()
            .map(|(name, child)| (*name, Some(child), None))
            .chain(
                folder
                    .files
                    .iter()
                    .map(|(name, index)| (*name, None, Some(*index))),
            )
            .collect();
        entries.sort_by_key(|(name, ..)| *name);
        for (name, child, file) in entries {
            if let Some(index) = file {
                self.rows.push(TreeRow {
                    key: format!("{prefix}{name}"),
                    file: Some(index),
                    name: name.to_owned(),
                    depth,
                    untracked,
                    files: Vec::new(),
                });
                continue;
            }
            // A folder holding only one folder joins it: `src/wisp/providers/`.
            let mut child = child.expect("a folder entry");
            let mut joined = format!("{name}/");
            while child.files.is_empty() && child.folders.len() == 1 {
                let (name, only) = child.folders.iter().next().expect("one folder");
                joined.push_str(&format!("{name}/"));
                child = only;
            }
            let path = format!("{prefix}{joined}");
            let key = format!(
                "{}:{}",
                if untracked { "untracked" } else { "tracked" },
                path.trim_end_matches('/')
            );
            let collapsed = self.collapsed.contains(&key);
            let mut files = Vec::new();
            child.file_indexes(&mut files);
            self.rows.push(TreeRow {
                key,
                file: None,
                name: joined,
                depth,
                untracked,
                files,
            });
            if !collapsed {
                self.emit(child, &path, depth + 1, untracked);
            }
        }
    }

    /// Handle a key. Enter on the diff of a file Wisp edited returns the card of the
    /// latest edit, for the app to jump to; every other key stays in the view.
    pub fn key(&mut self, key: KeyCode) -> Option<EditCard> {
        // Only moving in the tree gives up the file `/diff` was opened at; switching
        // focus or changing how the diff reads keeps it for the snapshot on its way.
        if self.tree_focus
            && matches!(
                key,
                KeyCode::Up
                    | KeyCode::Down
                    | KeyCode::Home
                    | KeyCode::End
                    | KeyCode::PageUp
                    | KeyCode::PageDown
                    | KeyCode::Enter
                    | KeyCode::Left
                    | KeyCode::Right
            )
        {
            self.opened_at = None;
        }
        match key {
            KeyCode::Tab | KeyCode::BackTab => self.tree_focus = !self.tree_focus,
            KeyCode::Char('s') => self.review.split = !self.review.split,
            KeyCode::Char('c') => self.toggle_context(),
            KeyCode::Char(']') => self.review.jump_hunk(true),
            KeyCode::Char('[') => self.review.jump_hunk(false),
            KeyCode::Up if self.tree_focus => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down if self.tree_focus => {
                self.selected = (self.selected + 1).min(self.rows.len().saturating_sub(1))
            }
            KeyCode::Home if self.tree_focus => self.selected = 0,
            KeyCode::End if self.tree_focus => self.selected = self.rows.len().saturating_sub(1),
            KeyCode::PageUp if self.tree_focus => self.selected = self.selected.saturating_sub(10),
            KeyCode::PageDown if self.tree_focus => {
                self.selected = (self.selected + 10).min(self.rows.len().saturating_sub(1))
            }
            KeyCode::Enter | KeyCode::Left | KeyCode::Right if self.tree_focus => {
                if let Some(row) = self.rows.get(self.selected).cloned() {
                    if let Some(index) = row.file {
                        self.active = index;
                        self.tree_focus = false;
                        self.review.reset();
                    } else {
                        match key {
                            KeyCode::Left => {
                                self.collapsed.insert(row.key);
                            }
                            KeyCode::Right => {
                                self.collapsed.remove(&row.key);
                            }
                            _ => {
                                if !self.collapsed.remove(&row.key) {
                                    self.collapsed.insert(row.key);
                                }
                            }
                        }
                        self.rebuild();
                    }
                }
            }
            KeyCode::Enter => {
                let file = self.snapshot.as_ref()?.files.get(self.active)?;
                return file
                    .latest_edit()
                    .map(|edit| EditCard::of(edit, &file.path));
            }
            KeyCode::Up => self.review.scroll(-1),
            KeyCode::Down => self.review.scroll(1),
            KeyCode::PageUp => self.review.page(false),
            KeyCode::PageDown => self.review.page(true),
            KeyCode::Home => self.review.home(),
            KeyCode::End => self.review.end(),
            _ => {}
        }
        if self.tree_focus {
            if let Some(index) = self.rows.get(self.selected).and_then(|row| row.file) {
                if self.active != index {
                    self.active = index;
                    self.review.reset();
                }
            }
        }
        None
    }

    /// How many of the snapshot's files a Wisp edit in this session changed.
    fn edited_by_wisp(&self) -> usize {
        self.snapshot.as_ref().map_or(0, |snapshot| {
            snapshot
                .files
                .iter()
                .filter(|file| file.edited_by_wisp())
                .count()
        })
    }

    /// The active file's diff as the pane shows it, with the context `c` chose.
    fn shown_detail(&self) -> Option<&ToolDetailPresentation> {
        match &self.full_context {
            Some((index, detail)) if self.review.context && *index == self.active => Some(detail),
            _ => self.details.get(self.active),
        }
    }

    /// `c`: switch between three lines of context and every line, staying at the
    /// source line at the top of the view. The two presentations number their rows
    /// differently, so the line is found again by its line number.
    fn toggle_context(&mut self) {
        let line = self.review.top_key().and_then(|key| {
            let rows = &self.shown_detail()?.rows;
            let at = rows.iter().position(|row| row.key == key)?;
            rows[at..]
                .iter()
                .find_map(|row| row.new_line.or(row.old_line))
        });
        self.review.context = !self.review.context;
        self.load_full_context();
        let key = line.and_then(|line| {
            self.shown_detail()?
                .rows
                .iter()
                .find(|row| row.new_line.or(row.old_line).is_some_and(|at| at >= line))
                .map(|row| row.key)
        });
        if let Some(key) = key {
            self.review.reopen_at(key);
        }
    }

    /// Build the active file's diff with every unchanged line once `c` asks for it.
    fn load_full_context(&mut self) {
        let Some(file) = self
            .snapshot
            .as_ref()
            .and_then(|s| s.files.get(self.active))
        else {
            return;
        };
        if self.review.context
            && self
                .full_context
                .as_ref()
                .is_none_or(|(index, _)| *index != self.active)
        {
            self.full_context = Some((
                self.active,
                git_presentation(&file.path, &file.before, &file.after, true),
            ));
        }
    }

    /// Draw `/diff`. The transcript numbers the turn each Wisp edit was made in.
    pub fn render(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        state: &GitDiff,
        transcript: &Transcript,
        palette: Palette,
    ) {
        self.sync(state);
        frame.render_widget(Clear, area);
        frame.render_widget(Paragraph::new("").style(palette.base()), area);
        if area.height < 6 || area.width < 20 {
            return;
        }
        let rule = Style::default().fg(palette.rail());
        let width = usize::from(area.width);
        let line_at = |y: u16| Rect::new(area.x, area.y + y, area.width, 1);
        frame.render_widget(
            Paragraph::new(self.header_line(state, width, palette)),
            line_at(0),
        );
        frame.render_widget(
            Paragraph::new(Line::styled("─".repeat(width), rule)),
            line_at(1),
        );
        frame.render_widget(
            Paragraph::new(Line::styled("─".repeat(width), rule)),
            line_at(area.height - 2),
        );
        let body = Rect::new(area.x, area.y + 2, area.width, area.height - 4);
        let wide = body.width >= 90;
        let tree_width = if wide {
            (body.width / 3).min(TREE_WIDTH)
        } else {
            body.width
        };
        if wide || self.tree_focus {
            let tree = Rect::new(
                body.x,
                body.y,
                tree_width.saturating_sub(u16::from(wide)),
                body.height,
            );
            let lines = self.tree_lines(state, tree, palette);
            frame.render_widget(Paragraph::new(lines), tree);
            if wide {
                let divider: Vec<Line> =
                    (0..body.height).map(|_| Line::styled("│", rule)).collect();
                frame.render_widget(
                    Paragraph::new(divider),
                    Rect::new(body.x + tree_width - 1, body.y, 1, body.height),
                );
            }
        }
        if wide || !self.tree_focus {
            // The diff sits two columns in from the tree's rule.
            let pane = if wide {
                Rect::new(
                    body.x + tree_width + 2,
                    body.y,
                    body.width.saturating_sub(tree_width + 3),
                    body.height,
                )
            } else {
                body
            };
            self.render_pane(frame, pane, transcript, palette);
        }
        // Last, so the keys name the layout the pane just drew.
        frame.render_widget(
            Paragraph::new(self.keys_line(width, palette)),
            line_at(area.height - 1),
        );
    }

    /// `/diff · working tree ↔ HEAD 4e32d60c3a · read only`, with the totals, the
    /// Wisp edits and the snapshot's age at the right.
    fn header_line(&self, state: &GitDiff, width: usize, palette: Palette) -> Line<'static> {
        let muted = Style::default().fg(palette.muted);
        let dim = Style::default().fg(palette.dim());
        let mut left = vec![
            Span::styled(
                "/diff",
                Style::default()
                    .fg(palette.primary)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(" · ", dim),
            Span::raw("working tree ↔ HEAD"),
        ];
        if let Some(snapshot) = &self.snapshot {
            left.push(Span::styled(
                format!(
                    " {}",
                    sanitize_for_terminal(&snapshot.head.chars().take(10).collect::<String>())
                ),
                dim,
            ));
        }
        left.extend([Span::styled(" · ", dim), Span::styled("read only", muted)]);
        let right = if state.loading() {
            vec![Span::styled("Refreshing Git snapshot…", muted)]
        } else if let Some(error) = &state.error {
            vec![Span::styled(sanitize_for_terminal(error), muted)]
        } else if let Some(snapshot) = &self.snapshot {
            let count = snapshot.files.len();
            let (added, deleted) = self.totals(&(0..count).collect::<Vec<_>>());
            let mut right = vec![Span::styled(
                format!(
                    "{count}{} file{} ",
                    if snapshot.truncated { "+" } else { "" },
                    if count == 1 { "" } else { "s" }
                ),
                muted,
            )];
            right.extend(counts(added, deleted, palette));
            right.push(Span::styled(" · ", dim));
            match self.edited_by_wisp() {
                0 => right.push(Span::styled("no Wisp edits this session", muted)),
                edited => right.extend([
                    Span::styled("◆", Style::default().fg(palette.primary)),
                    Span::styled(format!(" {edited} edited by Wisp"), muted),
                ]),
            }
            if snapshot.truncated {
                right.push(Span::styled(" · partial", muted));
            }
            right.push(Span::styled(" · ", dim));
            right.push(Span::styled(
                format!(
                    "snapshot {} ",
                    age(state.received_at.map_or(Duration::ZERO, |at| at.elapsed()))
                ),
                muted,
            ));
            right.push(Span::styled("r", palette.keycap()));
            right
        } else {
            vec![
                Span::styled("no snapshot · ", muted),
                Span::styled("r", palette.keycap()),
                Span::styled(" refresh", muted),
            ]
        };
        let left_width: usize = left.iter().map(|span| span.content.width()).sum();
        let right_width: usize = right.iter().map(|span| span.content.width()).sum();
        if left_width + right_width + 1 > width {
            // Too narrow for both: what is compared comes first, then as much as fits.
            left.push(Span::styled(" · ", dim));
            left.extend(right);
            return Line::from(crate::side_panel::clip_spans(left, width));
        }
        spread(left, right, width)
    }

    /// Lines added and deleted across files, untracked ones excluded: their content
    /// is never read.
    fn totals(&self, files: &[usize]) -> (usize, usize) {
        let Some(snapshot) = &self.snapshot else {
            return (0, 0);
        };
        files
            .iter()
            .filter(|index| counted(&snapshot.files[**index]))
            .fold((0, 0), |(added, deleted), index| {
                let detail = &self.details[*index];
                (
                    added + detail.additions as usize,
                    deleted + detail.deletions as usize,
                )
            })
    }

    fn tree_lines(&self, state: &GitDiff, area: Rect, palette: Palette) -> Vec<Line<'static>> {
        let width = usize::from(area.width);
        let dim = Style::default().fg(palette.dim());
        let by_wisp = self.edited_by_wisp();
        // The legend takes the tree's last row once anything carries a mark.
        let rows = usize::from(area.height).saturating_sub(usize::from(by_wisp > 0));
        let Some(snapshot) = &self.snapshot else {
            return Vec::new();
        };
        if self.rows.is_empty() {
            return if state.loading() || state.error.is_some() {
                Vec::new()
            } else {
                vec![Line::styled(" No visible changes", dim)]
            };
        }
        let largest = (0..snapshot.files.len())
            .filter(|index| counted(&snapshot.files[*index]))
            .map(|index| self.details[index].additions + self.details[index].deletions)
            .max()
            .unwrap_or(0) as usize;
        // Each section starts with its label, and the second a blank row before it.
        let mut lines: Vec<(Option<usize>, Line<'static>)> = Vec::new();
        let mut section = None;
        for (position, row) in self.rows.iter().enumerate() {
            if section != Some(row.untracked) {
                if section.is_some() {
                    lines.push((None, Line::default()));
                }
                lines.push((
                    None,
                    Line::styled(
                        if row.untracked {
                            " untracked"
                        } else {
                            " tracked"
                        },
                        dim,
                    ),
                ));
                section = Some(row.untracked);
            }
            lines.push((
                Some(position),
                self.tree_row(row, position, largest, by_wisp, width, palette),
            ));
        }
        let selected_line = lines
            .iter()
            .position(|(position, _)| *position == Some(self.selected))
            .unwrap_or(0);
        let start = selected_line.saturating_sub(rows.saturating_sub(2));
        let mut shown: Vec<Line<'static>> = lines
            .into_iter()
            .skip(start)
            .take(rows)
            .map(|(_, line)| line)
            .collect();
        if by_wisp > 0 {
            shown.resize(rows, Line::default());
            shown.push(Line::from(vec![
                Span::styled(" ◆", Style::default().fg(palette.primary)),
                Span::styled(" changed by a Wisp edit in this session", dim),
            ]));
        }
        shown
    }

    /// A folder row, `▾ docs/ (1)   +16 −1`, or a file row, `◆ M codex.py  ▰▰▰▰▰▱`.
    fn tree_row(
        &self,
        row: &TreeRow,
        position: usize,
        largest: usize,
        by_wisp: usize,
        width: usize,
        palette: Palette,
    ) -> Line<'static> {
        let snapshot = self.snapshot.as_ref().expect("tree rows have a snapshot");
        let selected = position == self.selected;
        let dim = Style::default().fg(palette.dim());
        let muted = Style::default().fg(palette.muted);
        let indent = "  ".repeat(row.depth);
        let name = sanitize_for_terminal(&row.name);
        let (left, right) = if let Some(index) = row.file {
            let file = &snapshot.files[index];
            let detail = &self.details[index];
            let (status, color) = match file.status.as_str() {
                "untracked" => ("?", palette.success),
                "added" => ("A", palette.success),
                "deleted" => ("D", palette.error),
                _ => ("M", palette.warning),
            };
            // Files sit under their folder's name: past the pad and the arrow column.
            let mut left = vec![Span::raw(format!("   {indent}"))];
            if by_wisp > 0 {
                left.push(if file.edited_by_wisp() {
                    Span::styled("◆ ", Style::default().fg(palette.primary))
                } else {
                    Span::raw("  ")
                });
            }
            left.push(Span::styled(
                format!("{status} "),
                Style::default().fg(color),
            ));
            left.push(if selected {
                Span::styled(
                    name,
                    Style::default()
                        .fg(palette.primary)
                        .add_modifier(Modifier::BOLD),
                )
            } else {
                Span::raw(name)
            });
            let right = if !file.notice.is_empty() && file.status != "untracked" {
                vec![Span::styled("unavailable", dim)]
            } else if counted(file) {
                churn_bar(
                    detail.additions as usize,
                    detail.deletions as usize,
                    largest,
                    palette,
                )
            } else {
                Vec::new()
            };
            (left, right)
        } else {
            let files = &row.files;
            let edited = files
                .iter()
                .any(|index| snapshot.files[*index].edited_by_wisp());
            let mut left = vec![
                Span::styled(
                    format!(
                        " {}",
                        if self.collapsed.contains(&row.key) {
                            "▸"
                        } else {
                            "▾"
                        }
                    ),
                    muted,
                ),
                Span::raw(format!(" {indent}{name}")),
                Span::styled(format!(" ({})", files.len()), dim),
            ];
            if by_wisp > 0 && !edited {
                left.push(Span::styled(" · not from this session", dim));
            }
            let (added, deleted) = self.totals(files);
            (left, counts(added, deleted, palette))
        };
        let mut line = spread(left, right, width.saturating_sub(1));
        line.spans.push(Span::raw(" "));
        if selected {
            // The selected row is raised, with an accent at its left edge.
            let raised = Style::default().bg(palette.panel);
            for span in &mut line.spans {
                span.style = raised.patch(span.style);
            }
            if let Some(first) = line.spans.first_mut() {
                let rest: String = first.content.chars().skip(1).collect();
                let style = first.style;
                *first = Span::styled(rest, style);
                line.spans
                    .insert(0, Span::styled("▎", raised.fg(palette.primary)));
            }
        }
        line
    }

    /// The selected file's diff under a header naming it, its counts, the hunk in
    /// view and, for a file Wisp edited, the edit Enter jumps to.
    fn render_pane(
        &mut self,
        frame: &mut Frame,
        pane: Rect,
        transcript: &Transcript,
        palette: Palette,
    ) {
        let Some(file) = self
            .snapshot
            .as_ref()
            .and_then(|s| s.files.get(self.active))
            .cloned()
        else {
            return;
        };
        if pane.height < 2 {
            return;
        }
        let width = usize::from(pane.width);
        let content = Rect::new(
            pane.x,
            pane.y + 2,
            pane.width,
            pane.height.saturating_sub(2),
        );
        let lines = if file.notice.is_empty() {
            self.load_full_context();
            let detail = match &self.full_context {
                Some((_, detail)) if self.review.context => detail,
                _ => &self.details[self.active],
            };
            self.review
                .lines(detail, width, usize::from(content.height), palette)
        } else {
            vec![Line::styled(
                sanitize_for_terminal(&file.notice),
                Style::default().fg(palette.muted),
            )]
        };
        let detail = &self.details[self.active];
        let muted = Style::default().fg(palette.muted);
        let dim = Style::default().fg(palette.dim());
        let mut left = vec![Span::styled(
            sanitize_for_terminal(&file.path),
            Style::default()
                .fg(palette.primary)
                .add_modifier(Modifier::BOLD),
        )];
        if counted(&file) {
            left.push(Span::raw(" "));
            left.extend(counts(
                detail.additions as usize,
                detail.deletions as usize,
                palette,
            ));
        }
        if file.notice.is_empty() {
            if let Some((hunk, hunks)) = self.review.hunk_position() {
                left.extend([
                    Span::styled(" · ", dim),
                    Span::styled(format!("hunk {hunk} of {hunks}"), muted),
                ]);
            }
        }
        if detail.truncated {
            left.push(Span::styled(" · preview truncated", muted));
        }
        let attribution = match file.latest_edit() {
            // Named by the latest edit, the card Enter jumps to.
            Some(edit) => {
                let turn = turn_of_edit(transcript, edit)
                    .map(|turn| format!(" in turn {turn}"))
                    .unwrap_or_default();
                let tool = edit.tool_label();
                let tool_style =
                    Style::default().fg(palette.chip(tool).fg.unwrap_or(palette.accent));
                vec![
                    Span::styled("◆", Style::default().fg(palette.primary)),
                    Span::styled(format!(" edited{turn} by "), muted),
                    Span::styled(tool, tool_style),
                    Span::styled(" · Enter jumps to the card", dim),
                ]
            }
            // The tree's folder labels and legend say what is not Wisp's.
            None => Vec::new(),
        };
        frame.render_widget(
            Paragraph::new(spread(left, attribution, width)),
            Rect::new(pane.x, pane.y, pane.width, 1),
        );
        frame.render_widget(Paragraph::new(lines), content);
    }

    /// `Tab files/diff · Enter open · [ ] hunks · s unified · c context · r refresh ·
    /// Esc back to chat`, with which side has focus at the right.
    fn keys_line(&self, width: usize, palette: Palette) -> Line<'static> {
        let muted = Style::default().fg(palette.muted);
        let key = |label: &str| Span::styled(label.to_owned(), palette.keycap());
        let text = |label: &str| Span::styled(label.to_owned(), muted);
        let left = vec![
            text(" "),
            key("Tab"),
            text(" files/diff · "),
            key("Enter"),
            text(" open · "),
            key("["),
            text(" "),
            key("]"),
            text(" hunks · "),
            key("s"),
            text(if self.review.split_shown() {
                " unified · "
            } else {
                " split · "
            }),
            key("c"),
            text(" context · "),
            key("r"),
            text(" refresh · "),
            key("Esc"),
            text(" back to chat"),
        ];
        let focus = if self.tree_focus { "files" } else { "diff" };
        spread(
            left,
            vec![Span::styled(
                format!("focus: {focus} "),
                Style::default().fg(palette.dim()),
            )],
            width,
        )
    }
}

/// Whether a file's line counts are known: untracked content is never read, and
/// Git could not read some files.
fn counted(file: &GitDiffFile) -> bool {
    file.status != "untracked" && file.notice.is_empty()
}

/// `+16 −1`, leaving out a side with no lines.
fn counts(added: usize, deleted: usize, palette: Palette) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    if added > 0 || deleted == 0 {
        spans.push(Span::styled(
            format!("+{added}"),
            Style::default().fg(palette.success),
        ));
    }
    if deleted > 0 {
        if !spans.is_empty() {
            spans.push(Span::raw(" "));
        }
        spans.push(Span::styled(
            format!("−{deleted}"),
            Style::default().fg(palette.error),
        ));
    }
    spans
}

/// A file's share of the largest file's churn in six cells, filled with its
/// additions then its deletions: `▰▰▰▰▰▱`.
fn churn_bar(added: usize, deleted: usize, largest: usize, palette: Palette) -> Vec<Span<'static>> {
    let churn = added + deleted;
    if churn == 0 || largest == 0 {
        return vec![Span::styled(
            "▱".repeat(CHURN_CELLS),
            Style::default().fg(palette.dim()),
        )];
    }
    // Any change fills a cell; the rest scale to the largest file.
    let filled = (churn * CHURN_CELLS)
        .div_ceil(largest)
        .clamp(1, CHURN_CELLS);
    let mut deletions = (deleted * filled).div_ceil(churn);
    if added > 0 && deletions == filled && filled > 1 {
        deletions -= 1;
    }
    let additions = filled - deletions;
    vec![
        Span::styled("▰".repeat(additions), Style::default().fg(palette.success)),
        Span::styled("▰".repeat(deletions), Style::default().fg(palette.error)),
        Span::styled(
            "▱".repeat(CHURN_CELLS - filled),
            Style::default().fg(palette.dim()),
        ),
    ]
}

/// `12s ago`, `4m ago`, `2h ago`.
fn age(elapsed: Duration) -> String {
    let seconds = elapsed.as_secs();
    match seconds {
        0..60 => format!("{seconds}s ago"),
        60..3600 => format!("{}m ago", seconds / 60),
        _ => format!("{}h ago", seconds / 3600),
    }
}

/// The transcript card of an edit: the card its result entry was drawn into, then,
/// for a card known only by its call (a live one before its entry arrived), the
/// call index or a scan once the index has let a resolved call go.
pub(crate) fn edit_card_entry(
    transcript: &Transcript,
    entry_id: &str,
    call_id: &str,
) -> Option<TranscriptEntryId> {
    let by_entry = (!entry_id.is_empty())
        .then(|| transcript.tool_entry_with_origin(entry_id))
        .flatten();
    by_entry
        .or_else(|| transcript.tool_entry_for_call(call_id))
        .or_else(|| {
            transcript
                .entries()
                .iter()
                .find(|entry| {
                    entry
                        .tool_card()
                        .is_some_and(|card| card.call_id == call_id)
                })
                .map(|entry| entry.id)
        })
}

/// The turn an edit ran in, numbered as the side panel's turn list numbers prompts;
/// `None` when its card is not in the transcript.
fn turn_of_edit(transcript: &Transcript, edit: &GitDiffEdit) -> Option<usize> {
    let card = edit_card_entry(transcript, &edit.entry_id, &edit.call_id)?;
    let mut turn = 0;
    for entry in transcript.entries() {
        if entry.id == card {
            return (turn > 0).then_some(turn);
        }
        if entry.role == TranscriptRole::User {
            turn += 1;
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::git_diff::{GitDiffEdit, GitDiffFile};
    use ratatui::{Terminal, backend::TestBackend};
    use std::time::Instant;

    fn changed(path: &str, before: &str, after: &str) -> GitDiffFile {
        GitDiffFile {
            path: path.into(),
            status: "modified".into(),
            before: before.into(),
            after: after.into(),
            ..Default::default()
        }
    }

    fn edit(id: &str) -> GitDiffEdit {
        GitDiffEdit {
            call_id: id.into(),
            entry_id: String::new(),
            tool: "edit".into(),
        }
    }

    fn state_of(files: Vec<GitDiffFile>) -> GitDiff {
        let mut state = GitDiff::default();
        state.snapshot = Some(Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "4e32d60c3a11".into(),
            truncated: false,
            files,
        }));
        state.received_at = Instant::now().checked_sub(Duration::from_secs(12));
        state
    }

    fn screen(
        view: &mut GitDiffView,
        state: &GitDiff,
        transcript: &Transcript,
        width: u16,
        height: u16,
    ) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), state, transcript, Palette::default()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..buffer.area.height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
                    .trim_end()
                    .to_owned()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// A Python file with a change in a method of a class and one in a later method.
    fn python() -> (String, String) {
        let mut before = vec!["import os", "", "class Transport:", "    name = 'x'", ""];
        before.extend(["    def client(self):", "        return 1"]);
        before.extend(std::iter::repeat_n("    # filler", 20));
        before.extend([
            "    async def stream(self):",
            "        try:",
            "            pass",
        ]);
        before.extend(["        except OSError:", "            raise ValueError()"]);
        let after: Vec<_> = before
            .iter()
            .map(|line| match *line {
                "        return 1" => "        return 2",
                "            raise ValueError()" => "            raise ValueError('read')",
                line => line,
            })
            .collect();
        (before.join("\n") + "\n", after.join("\n") + "\n")
    }

    #[test]
    fn opened_file_survives_refresh() {
        let snapshot = |generation| {
            Some(Arc::new(GitDiffSnapshot {
                generation,
                head: "abc".into(),
                truncated: false,
                files: ["a.rs", "b.rs", "c.rs"]
                    .into_iter()
                    .map(|path| changed(path, "old\n", "new\n"))
                    .collect(),
            }))
        };
        let mut state = GitDiff::default();
        let mut view = GitDiffView::opened_at(Some("b.rs".into()));
        state.snapshot = snapshot(1);
        view.sync(&state);
        assert_eq!(view.rows[view.selected].key, "b.rs");
        assert_eq!(view.active, 1);
        // Opening /diff reads a fresh snapshot; the file stays selected in it.
        state.snapshot = snapshot(2);
        view.sync(&state);
        assert_eq!(view.rows[view.selected].key, "b.rs");
        // Changing how the diff reads, or scrolling it, while a snapshot loads keeps
        // the file.
        let mut view = GitDiffView::opened_at(Some("b.rs".into()));
        state.snapshot = None;
        view.sync(&state);
        for key in [KeyCode::Char('s'), KeyCode::Char('c'), KeyCode::Down] {
            view.key(key);
        }
        state.snapshot = snapshot(4);
        view.sync(&state);
        assert_eq!(view.rows[view.selected].key, "b.rs");
        // Once the user moves in the tree, a later snapshot no longer pulls the
        // selection back.
        view.key(KeyCode::Down);
        state.snapshot = snapshot(5);
        view.sync(&state);
        assert_eq!(view.selected, 0);
    }

    #[test]
    fn wide_header_sums_snapshot() {
        let mut edited = changed("src/codex.py", "a\nb\n", "a\nc\nd\n");
        edited.edits = vec![edit("edit-1")];
        let mut untracked = changed("notes.md", "", "");
        untracked.status = "untracked".into();
        untracked.notice = "Untracked file; excluded from tracked diff totals".into();
        let state = state_of(vec![edited, changed("docs/a.md", "x\n", "y\n"), untracked]);
        let text = screen(
            &mut GitDiffView::default(),
            &state,
            &Transcript::default(),
            200,
            24,
        );
        let header = text.lines().next().unwrap();
        assert!(
            header.starts_with("/diff · working tree ↔ HEAD 4e32d60c3a · read only"),
            "{header}"
        );
        // Untracked content is never read, so the totals are the tracked files'.
        assert!(
            header.ends_with("3 files +3 −2 · ◆ 1 edited by Wisp · snapshot 12s ago r"),
            "{header}"
        );
        let keys = text.lines().last().unwrap();
        assert!(
            keys.starts_with(
                " Tab files/diff · Enter open · [ ] hunks · s unified · c context · r refresh · Esc back to chat"
            ),
            "{keys}"
        );
        assert!(keys.ends_with("focus: files"), "{keys}");
    }

    #[test]
    fn tree_draws_churn_bars() {
        let lines = |count: usize| "x\n".repeat(count);
        let mut big = changed("src/wisp/providers/codex.py", "", &lines(10));
        big.status = "added".into();
        let small = changed("src/wisp/providers/retry.py", "a\nb\n", "a\nc\n");
        let state = state_of(vec![
            big,
            small,
            changed("docs/configuration.md", "a\n", "b\n"),
        ]);
        let text = screen(
            &mut GitDiffView::default(),
            &state,
            &Transcript::default(),
            200,
            24,
        );
        let row = |name: &str| {
            text.lines()
                .find(|line| line.contains(name))
                .unwrap_or_else(|| panic!("{name}\n{text}"))
                .chars()
                .take(usize::from(TREE_WIDTH))
                .collect::<String>()
        };
        assert!(row(" tracked").starts_with(" tracked"));
        // A folder holding one folder joins it, with its files' counts.
        assert!(
            row("src/wisp/providers/ (2)").ends_with("+11 −1 │"),
            "{text}"
        );
        assert!(row("A codex.py").ends_with("▰▰▰▰▰▰ │"), "{text}");
        assert!(row("M retry.py").ends_with("▰▰▱▱▱▱ │"), "{text}");
        assert_eq!(
            churn_bar(9, 3, 12, Palette::default())
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>(),
            "▰▰▰▰▰▰"
        );
    }

    #[test]
    fn hunk_bars_name_definitions() {
        let (before, after) = python();
        let state = state_of(vec![changed("src/transport.py", &before, &after)]);
        let mut view = GitDiffView::default();
        view.key(KeyCode::Enter);
        let text = screen(&mut view, &state, &Transcript::default(), 200, 40);
        assert!(
            text.contains("src/transport.py +2 −2 · hunk 1 of 2"),
            "{text}"
        );
        let bar = |label: &str| {
            text.lines()
                .find(|line| line.contains(label))
                .unwrap_or_else(|| panic!("{label}\n{text}"))
                .to_owned()
        };
        assert!(
            bar("class Transport").contains("@@ -4,7 +4,7 @@ class Transport"),
            "{text}"
        );
        assert!(
            bar("class Transport").ends_with("split · s unified"),
            "{text}"
        );
        assert!(bar("async def stream").ends_with("2 of 2"), "{text}");
        // The unchanged lines between hunks fold under the line numbers.
        assert!(
            text.contains("       ⋯ 18 unchanged lines · c show context"),
            "{text}"
        );
        // The sides sit a column apart, old on the left.
        let changed_row = bar("return 2");
        assert!(changed_row.contains("return 1"), "{changed_row}");
        // `c` shows every line as one hunk, without folds, at the same source line.
        view.key(KeyCode::Char(']'));
        view.key(KeyCode::Char('c'));
        let text = screen(&mut view, &state, &Transcript::default(), 200, 40);
        assert!(!text.contains("unchanged lines"), "{text}");
        assert!(text.contains("hunk 1 of 1"), "{text}");
        let top = text.lines().nth(4).unwrap_or_default();
        assert!(top.contains("  29 "), "{text}");
    }

    #[test]
    fn tree_marks_wisp_edits() {
        let mut codex = changed("src/codex.py", "old\n", "new\n");
        codex.edits = vec![edit("edit-1"), edit("edit-2")];
        let state = state_of(vec![
            codex,
            changed("src/retry.py", "old\n", "new\n"),
            changed("site/index.html", "old\n", "new\n"),
        ]);
        let mut view = GitDiffView::opened_at(Some("src/codex.py".into()));
        // The latest edit ran in the second turn.
        let mut transcript = Transcript::default();
        transcript.append_prompt("first".into());
        transcript.append_prompt("second".into());
        transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "edit-2".into(),
            name: "edit".into(),
            arguments: serde_json::json!({"path": "src/codex.py", "edits": []}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
        });
        let text = screen(&mut view, &state, &transcript, 200, 24);
        assert!(text.contains("◆ 1 edited by Wisp"), "{text}");
        assert!(text.contains("  ◆ M codex.py"), "{text}");
        assert!(text.contains("    M retry.py"), "{text}");
        assert!(
            text.contains("▾ site/ (1) · not from this session"),
            "{text}"
        );
        assert!(!text.contains("src/ (2) ·"), "{text}");
        assert!(
            text.contains("◆ edited in turn 2 by Edit · Enter jumps to the card"),
            "{text}"
        );
        assert!(
            text.contains(" ◆ changed by a Wisp edit in this session"),
            "{text}"
        );
        // In the diff pane, Enter on a ◆ file names its latest edit's card.
        view.key(KeyCode::Tab);
        assert_eq!(
            view.key(KeyCode::Enter),
            Some(EditCard {
                call_id: "edit-2".into(),
                entry_id: String::new(),
                path: "src/codex.py".into(),
            })
        );
        view.key(KeyCode::Tab);
        view.key(KeyCode::Down);
        let text = screen(&mut view, &state, &transcript, 200, 24);
        assert!(text.contains("src/retry.py +1 −1 · hunk 1 of 1"), "{text}");
        assert!(!text.contains("Enter jumps"), "{text}");
        view.key(KeyCode::Tab);
        assert_eq!(view.key(KeyCode::Enter), None);
    }

    #[test]
    fn tree_collapses_and_narrow_switches() {
        let mut untracked = changed("notes.txt", "", "");
        untracked.status = "untracked".into();
        untracked.notice = "Untracked file; excluded from tracked diff totals".into();
        let state = state_of(vec![changed("src/example.py", "old\n", "new\n"), untracked]);
        let mut view = GitDiffView::default();
        for width in [50, 160] {
            let text = screen(&mut view, &state, &Transcript::default(), width, 24);
            assert!(text.contains("working tree"), "{text}");
            assert!(text.contains("example.py"), "{text}");
        }
        // Sections are labelled, untracked last, with no counts.
        let text = screen(&mut view, &state, &Transcript::default(), 160, 24);
        assert!(text.contains(" untracked"), "{text}");
        assert!(text.contains("? notes.txt"), "{text}");
        view.selected = 0;
        view.key(KeyCode::Enter);
        assert!(!view.rows.iter().any(|row| row.file == Some(0)));
        view.key(KeyCode::Enter);
        view.key(KeyCode::Down);
        view.key(KeyCode::Enter);
        assert!(!view.tree_focus);
        assert_eq!(view.active, 0);
    }
}
