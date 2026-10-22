//! Changed-file navigation over a backend-owned read-only snapshot.
use crate::{
    diff_review::DiffReview,
    reducer::git_diff::{GitDiff, GitDiffSnapshot},
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
    style::Style,
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
};
use std::{collections::BTreeSet, sync::Arc};

/// The transcript card of the session's latest edit or write call on a file.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct EditCard {
    pub call_id: String,
    /// The file, so `/diff` can open at it when the card is not in the transcript.
    pub path: String,
}

#[derive(Default)]
pub(crate) struct GitDiffView {
    snapshot: Option<Arc<GitDiffSnapshot>>,
    details: Vec<ToolDetailPresentation>,
    collapsed: BTreeSet<String>,
    rows: Vec<(String, Option<usize>)>,
    selected: usize,
    active: usize,
    tree_focus: bool,
    review: DiffReview,
    /// The file to select when a snapshot arrives, until the user moves. Opening
    /// `/diff` reads a fresh snapshot, so the file is found again in that one.
    opened_at: Option<String>,
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
                    .map(|f| git_presentation(&f.path, &f.before, &f.after))
                    .collect()
            })
            .unwrap_or_default();
        self.active = 0;
        self.selected = 0;
        self.tree_focus = true;
        self.review.reset();
        self.rebuild();
        if let Some(path) = &self.opened_at {
            if let Some(position) = self
                .rows
                .iter()
                .position(|(row, index)| index.is_some() && row == path)
            {
                self.selected = position;
                self.active = self.rows[position].1.expect("a file row");
            }
        }
    }

    fn rebuild(&mut self) {
        self.rows.clear();
        let Some(snapshot) = &self.snapshot else {
            return;
        };
        let mut emitted = BTreeSet::new();
        for untracked in [false, true] {
            let mut files: Vec<_> = snapshot
                .files
                .iter()
                .enumerate()
                .filter(|(_, f)| (f.status == "untracked") == untracked)
                .collect();
            files.sort_by_key(|(_, f)| &f.path);
            for (index, file) in files {
                let parts: Vec<_> = file.path.split('/').collect();
                let mut hidden = false;
                for depth in 1..parts.len() {
                    let path = parts[..depth].join("/");
                    let key = format!("{}:{path}", if untracked { "untracked" } else { "tracked" });
                    if emitted.insert(key.clone()) {
                        self.rows.push((key.clone(), None));
                    }
                    if self.collapsed.contains(&key) {
                        hidden = true;
                        break;
                    }
                }
                if !hidden {
                    self.rows.push((file.path.clone(), Some(index)));
                }
            }
        }
        self.selected = self.selected.min(self.rows.len().saturating_sub(1));
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
            KeyCode::Char('c') => self.review.context = !self.review.context,
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
                if let Some((path, index)) = self.rows.get(self.selected).cloned() {
                    if let Some(index) = index {
                        self.active = index;
                        self.tree_focus = false;
                        self.review.reset();
                    } else {
                        match key {
                            KeyCode::Left => {
                                self.collapsed.insert(path);
                            }
                            KeyCode::Right => {
                                self.collapsed.remove(&path);
                            }
                            _ => {
                                if !self.collapsed.remove(&path) {
                                    self.collapsed.insert(path);
                                }
                            }
                        }
                        self.rebuild();
                    }
                }
            }
            KeyCode::Enter => {
                let file = self.snapshot.as_ref()?.files.get(self.active)?;
                return file.latest_edit().map(|edit| EditCard {
                    call_id: edit.call_id.clone(),
                    path: file.path.clone(),
                });
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
            if let Some((_, Some(index))) = self.rows.get(self.selected) {
                if self.active != *index {
                    self.active = *index;
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
        let block = Block::default()
            .borders(Borders::ALL)
            .title(" wisp /diff · Working tree ↔ HEAD · read only ")
            .style(
                Style::default()
                    .fg(palette.foreground)
                    .bg(palette.background),
            );
        let inner = block.inner(area);
        frame.render_widget(block, area);
        if inner.height < 5 || inner.width < 20 {
            return;
        }
        let message = if state.loading() {
            "Refreshing Git snapshot…".into()
        } else if let Some(error) = &state.error {
            sanitize_for_terminal(error)
        } else if let Some(snapshot) = &self.snapshot {
            let by_wisp = match self.edited_by_wisp() {
                0 => "no Wisp edits this session".to_owned(),
                count => format!("◆ {count} edited by Wisp"),
            };
            format!(
                "HEAD {} · snapshot may be stale · r refresh · {by_wisp}{}",
                snapshot.head.chars().take(10).collect::<String>(),
                if snapshot.truncated {
                    " · partial: some files omitted"
                } else {
                    ""
                }
            )
        } else {
            "No snapshot. Press r to refresh.".into()
        };
        frame.render_widget(
            Paragraph::new(with_marks(
                &message,
                Style::default().fg(palette.muted),
                palette,
            )),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let body = Rect::new(inner.x, inner.y + 2, inner.width, inner.height - 4);
        let wide = body.width >= 90;
        let tree_width = if wide {
            (body.width / 3).min(48)
        } else {
            body.width
        };
        let by_wisp = self.edited_by_wisp();
        if wide || self.tree_focus {
            let mut lines = Vec::new();
            // The legend takes the tree's last row once anything carries a mark.
            let tree_rows = usize::from(body.height).saturating_sub(usize::from(by_wisp > 0));
            let start = self.selected.saturating_sub(tree_rows.saturating_sub(2));
            for (position, (path, index)) in
                self.rows.iter().enumerate().skip(start).take(tree_rows)
            {
                // Once Wisp edited anything, every file row gets the ◆ column, and a
                // directory none of whose files Wisp edited says so.
                let mut mark = "";
                let mut note = "";
                let label = if let Some(index) = index {
                    let file = &self.snapshot.as_ref().expect("snapshot rows").files[*index];
                    if by_wisp > 0 {
                        mark = if file.edited_by_wisp() { "◆ " } else { "  " };
                    }
                    let detail = &self.details[*index];
                    let prefix = if file.status == "untracked" {
                        "?"
                    } else if file.status == "added" {
                        "A"
                    } else if file.status == "deleted" {
                        "D"
                    } else {
                        "M"
                    };
                    let counts = if !file.notice.is_empty() {
                        " · unavailable".into()
                    } else {
                        format!(" +{} -{}", detail.additions, detail.deletions)
                    };
                    format!(
                        "{}{} {}{}",
                        "  ".repeat(path.matches('/').count()),
                        prefix,
                        path.rsplit('/').next().unwrap_or(path),
                        if file.status == "untracked" {
                            " · untracked".into()
                        } else {
                            counts
                        }
                    )
                } else {
                    let (group, relative) = path.split_once(':').unwrap_or(("tracked", path));
                    let prefix = format!("{relative}/");
                    let mut count = 0;
                    let mut added = 0;
                    let mut deleted = 0;
                    let mut edited = false;
                    for (i, file) in self
                        .snapshot
                        .as_ref()
                        .expect("snapshot rows")
                        .files
                        .iter()
                        .enumerate()
                    {
                        if file.path.starts_with(&prefix)
                            && (file.status == "untracked") == (group == "untracked")
                        {
                            count += 1;
                            added += self.details[i].additions;
                            deleted += self.details[i].deletions;
                            edited |= file.edited_by_wisp();
                        }
                    }
                    if by_wisp > 0 && !edited {
                        note = " · not from this session";
                    }
                    format!(
                        "{}{} {}/ ({count}) known +{added} -{deleted}{}",
                        "  ".repeat(relative.matches('/').count()),
                        if self.collapsed.contains(path) {
                            "▸"
                        } else {
                            "▾"
                        },
                        relative.rsplit('/').next().unwrap_or(relative),
                        if group == "untracked" {
                            " untracked"
                        } else {
                            ""
                        }
                    )
                };
                let style = if position == self.selected {
                    Style::default().fg(palette.primary).bg(palette.surface)
                } else {
                    Style::default().fg(palette.foreground)
                };
                let label = sanitize_for_terminal(&label);
                // The mark sits after the indent, before the status letter.
                let indent = label.len() - label.trim_start_matches(' ').len();
                let (indent, rest) = label.split_at(indent);
                lines.push(Line::from(vec![
                    Span::styled(indent.to_owned(), style),
                    Span::styled(mark, style.fg(palette.primary)),
                    Span::styled(rest.to_owned(), style),
                    Span::styled(note, style.fg(palette.muted)),
                ]));
            }
            if self.rows.is_empty() && !state.loading() && state.error.is_none() {
                lines.push(Line::from("No visible changes"));
            }
            if by_wisp > 0 {
                lines.resize(tree_rows, Line::default());
                lines.push(with_marks(
                    "◆ changed by a Wisp edit in this session",
                    Style::default().fg(palette.muted),
                    palette,
                ));
            }
            frame.render_widget(
                Paragraph::new(lines),
                Rect::new(body.x, body.y, tree_width, body.height),
            );
        }
        if wide || !self.tree_focus {
            let pane = if wide {
                Rect::new(
                    body.x + tree_width + 1,
                    body.y,
                    body.width.saturating_sub(tree_width + 1),
                    body.height,
                )
            } else {
                body
            };
            if let Some(file) = self
                .snapshot
                .as_ref()
                .and_then(|s| s.files.get(self.active))
            {
                let detail = &self.details[self.active];
                let muted = Style::default().fg(palette.muted);
                let attribution = match file.latest_edit() {
                    // Named by the latest edit, the card Enter jumps to.
                    Some(edit) => {
                        let turn = turn_of_call(transcript, &edit.call_id)
                            .map(|turn| format!(" in turn {turn}"))
                            .unwrap_or_default();
                        let tool = edit.tool_label();
                        let tool_style =
                            Style::default().fg(palette.chip(tool).fg.unwrap_or(palette.accent));
                        vec![
                            Span::styled("◆", Style::default().fg(palette.primary)),
                            Span::styled(format!(" edited{turn} by "), muted),
                            Span::styled(tool, tool_style),
                            Span::styled(" · Enter jumps to the card", muted),
                        ]
                    }
                    // The tree's directory labels and legend say what is not Wisp's.
                    None => Vec::new(),
                };
                let header = format!(
                    "{}  +{} -{}{}",
                    file.path,
                    detail.additions,
                    detail.deletions,
                    if detail.truncated {
                        " · preview truncated"
                    } else {
                        ""
                    }
                );
                frame.render_widget(
                    Paragraph::new(spread(
                        vec![Span::styled(
                            sanitize_for_terminal(&header),
                            Style::default().fg(palette.primary),
                        )],
                        attribution,
                        usize::from(pane.width),
                    )),
                    Rect::new(pane.x, pane.y, pane.width, 1),
                );
                let content = Rect::new(
                    pane.x,
                    pane.y + 1,
                    pane.width,
                    pane.height.saturating_sub(1),
                );
                if !file.notice.is_empty() {
                    frame.render_widget(Paragraph::new(file.notice.clone()), content);
                } else {
                    let lines = self.review.lines(
                        detail,
                        usize::from(content.width),
                        usize::from(content.height),
                        palette,
                    );
                    frame.render_widget(Paragraph::new(lines), content);
                }
            }
        }
        frame.render_widget(Paragraph::new(format!("Tab focus ({}) · Enter expand/open · [] hunks · s split · c context · r refresh · Esc close", if self.tree_focus { "files" } else { "diff" })).style(Style::default().fg(palette.muted)), Rect::new(inner.x, inner.y + inner.height - 1, inner.width, 1));
    }
}

/// `text` in `base`, with each ◆ in the primary colour.
fn with_marks(text: &str, base: Style, palette: Palette) -> Line<'static> {
    let mut spans = Vec::new();
    for (index, part) in text.split('◆').enumerate() {
        if index > 0 {
            spans.push(Span::styled("◆", base.fg(palette.primary)));
        }
        if !part.is_empty() {
            spans.push(Span::styled(part.to_owned(), base));
        }
    }
    Line::from(spans)
}

/// The transcript card of a tool call: found through the call index, or by a scan
/// once the index has let a resolved call go.
pub(crate) fn edit_card_entry(transcript: &Transcript, call_id: &str) -> Option<TranscriptEntryId> {
    transcript.tool_entry_for_call(call_id).or_else(|| {
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

/// The turn a tool call ran in, numbered as the side panel's turn list numbers
/// prompts; `None` when the call's card is not in the transcript.
fn turn_of_call(transcript: &Transcript, call_id: &str) -> Option<usize> {
    let card = edit_card_entry(transcript, call_id)?;
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
    use crate::reducer::git_diff::GitDiffFile;
    use ratatui::{Terminal, backend::TestBackend};

    #[test]
    fn opened_file_survives_refresh() {
        let snapshot = |generation| {
            Some(Arc::new(GitDiffSnapshot {
                generation,
                head: "abc".into(),
                truncated: false,
                files: ["a.rs", "b.rs", "c.rs"]
                    .into_iter()
                    .map(|path| GitDiffFile {
                        path: path.into(),
                        status: "modified".into(),
                        before: "old\n".into(),
                        after: "new\n".into(),
                        notice: String::new(),
                        ..Default::default()
                    })
                    .collect(),
            }))
        };
        let mut state = GitDiff::default();
        let mut view = GitDiffView::opened_at(Some("b.rs".into()));
        state.snapshot = snapshot(1);
        view.sync(&state);
        assert_eq!(view.rows[view.selected].0, "b.rs");
        assert_eq!(view.active, 1);
        // Opening /diff reads a fresh snapshot; the file stays selected in it.
        state.snapshot = snapshot(2);
        view.sync(&state);
        assert_eq!(view.rows[view.selected].0, "b.rs");
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
        assert_eq!(view.rows[view.selected].0, "b.rs");
        // Once the user moves in the tree, a later snapshot no longer pulls the
        // selection back.
        view.key(KeyCode::Down);
        state.snapshot = snapshot(5);
        view.sync(&state);
        assert_eq!(view.selected, 0);
    }

    fn screen(
        view: &mut GitDiffView,
        state: &GitDiff,
        transcript: &Transcript,
        width: u16,
    ) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), state, transcript, Palette::default()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..buffer.area.height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn tree_marks_wisp_edits() {
        use crate::reducer::git_diff::GitDiffEdit;
        let changed = |path: &str, edits: Vec<GitDiffEdit>| GitDiffFile {
            path: path.into(),
            status: "modified".into(),
            before: "old\n".into(),
            after: "new\n".into(),
            edits,
            ..Default::default()
        };
        let edit = |id: &str| GitDiffEdit {
            call_id: id.into(),
            tool: "edit".into(),
        };
        let mut state = GitDiff::default();
        state.snapshot = Some(Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "abc".into(),
            truncated: false,
            files: vec![
                changed("src/codex.py", vec![edit("edit-1"), edit("edit-2")]),
                changed("src/retry.py", vec![]),
                changed("site/index.html", vec![]),
            ],
        }));
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
        let text = screen(&mut view, &state, &transcript, 160);
        assert!(text.contains("◆ 1 edited by Wisp"), "{text}");
        assert!(text.contains("◆ M codex.py"), "{text}");
        assert!(text.contains("  M retry.py"), "{text}");
        assert!(
            text.contains("site/ (1) known +1 -1 · not from this session"),
            "{text}"
        );
        assert!(!text.contains("src/ (2) known +2 -2 ·"), "{text}");
        assert!(
            text.contains("◆ edited in turn 2 by Edit · Enter jumps to the card"),
            "{text}"
        );
        assert!(
            text.contains("◆ changed by a Wisp edit in this session"),
            "{text}"
        );
        // In the diff pane, Enter on a ◆ file names its latest edit's card.
        view.key(KeyCode::Tab);
        assert_eq!(
            view.key(KeyCode::Enter),
            Some(EditCard {
                call_id: "edit-2".into(),
                path: "src/codex.py".into(),
            })
        );
        view.key(KeyCode::Tab);
        view.key(KeyCode::Down);
        let text = screen(&mut view, &state, &transcript, 160);
        assert!(text.contains("retry.py  +1 -1"), "{text}");
        assert!(!text.contains("Enter jumps"), "{text}");
        view.key(KeyCode::Tab);
        assert_eq!(view.key(KeyCode::Enter), None);
    }

    #[test]
    fn tree_collapses_and_review_renders_at_narrow_and_wide_sizes() {
        let mut state = GitDiff::default();
        state.snapshot = Some(Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "0123456789abcdef".into(),
            truncated: false,
            files: vec![
                GitDiffFile {
                    path: "src/example.py".into(),
                    status: "modified".into(),
                    before: "old\n".into(),
                    after: "new\n".into(),
                    notice: "".into(),
                    ..Default::default()
                },
                GitDiffFile {
                    path: "notes.txt".into(),
                    status: "untracked".into(),
                    before: "".into(),
                    after: "".into(),
                    notice: "Untracked file; excluded from tracked diff totals".into(),
                    ..Default::default()
                },
            ],
        }));
        let mut view = GitDiffView::default();
        for width in [50, 160] {
            let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
            terminal
                .draw(|frame| {
                    view.render(
                        frame,
                        frame.area(),
                        &state,
                        &Transcript::default(),
                        Palette::default(),
                    )
                })
                .unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|cell| cell.symbol())
                .collect();
            assert!(text.contains("Working tree"));
            assert!(text.contains("snapshot may be stale"));
            assert!(text.contains("example.py"));
        }
        view.selected = 0;
        view.key(KeyCode::Enter);
        assert!(!view.rows.iter().any(|(_, i)| *i == Some(0)));
        view.key(KeyCode::Enter);
        view.key(KeyCode::Down);
        view.key(KeyCode::Enter);
        assert!(!view.tree_focus);
        assert_eq!(view.active, 0);
    }
}
