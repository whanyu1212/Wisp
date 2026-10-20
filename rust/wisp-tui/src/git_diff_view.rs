//! Changed-file navigation over a backend-owned read-only snapshot.
use crate::{
    diff_review::DiffReview,
    reducer::git_diff::{GitDiff, GitDiffSnapshot},
    theme::Palette,
    tool_detail::{ToolDetailPresentation, git_presentation},
    ui::sanitize_for_terminal,
};
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    text::Line,
    widgets::{Block, Borders, Clear, Paragraph},
};
use std::{collections::BTreeSet, sync::Arc};

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
}

impl GitDiffView {
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

    pub fn key(&mut self, key: KeyCode) {
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
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect, state: &GitDiff, palette: Palette) {
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
            format!(
                "HEAD {} · snapshot may be stale · r refresh{}",
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
            Paragraph::new(message).style(Style::default().fg(palette.muted)),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let body = Rect::new(inner.x, inner.y + 2, inner.width, inner.height - 4);
        let wide = body.width >= 90;
        let tree_width = if wide {
            (body.width / 3).min(44)
        } else {
            body.width
        };
        if wide || self.tree_focus {
            let mut lines = Vec::new();
            let start = self
                .selected
                .saturating_sub(usize::from(body.height).saturating_sub(2));
            for (position, (path, index)) in self
                .rows
                .iter()
                .enumerate()
                .skip(start)
                .take(usize::from(body.height))
            {
                let label = if let Some(index) = index {
                    let file = &self.snapshot.as_ref().expect("snapshot rows").files[*index];
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
                        }
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
                lines.push(Line::styled(sanitize_for_terminal(&label), style));
            }
            if self.rows.is_empty() && !state.loading() && state.error.is_none() {
                lines.push(Line::from("No visible changes"));
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
                    Paragraph::new(sanitize_for_terminal(&header))
                        .style(Style::default().fg(palette.primary)),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::git_diff::GitDiffFile;
    use ratatui::{Terminal, backend::TestBackend};

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
                },
                GitDiffFile {
                    path: "notes.txt".into(),
                    status: "untracked".into(),
                    before: "".into(),
                    after: "".into(),
                    notice: "Untracked file; excluded from tracked diff totals".into(),
                },
            ],
        }));
        let mut view = GitDiffView::default();
        for width in [50, 160] {
            let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
            terminal
                .draw(|frame| view.render(frame, frame.area(), &state, Palette::default()))
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
