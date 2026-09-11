//! Composer-local reference editing and bounded views of backend-authorized paths.

use crate::prompt_editor::PromptEditor;
use crate::reducer::project_files::ProjectFiles;
use crate::theme::Palette;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    text::Line,
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Wrap},
};
use std::{
    borrow::Cow,
    collections::{BTreeMap, BTreeSet},
    ops::Range,
    sync::Arc,
};
use wisp_protocol::events::{ProjectFileEntry, ProjectFileKind, ProjectFileSnapshot};

const RESULT_LIMIT: usize = 30;
const QUERY_BYTES_LIMIT: usize = 4096;

#[cfg(test)]
#[path = "file_picker_tests.rs"]
mod tests;

#[derive(Clone, Debug, Eq, PartialEq)]
struct Reference {
    range: Range<usize>,
    query: String,
    overlong: bool,
}

/// Find a reference on the cursor's line, including quoted paths and token suffixes.
fn reference(editor: &PromptEditor) -> Option<Reference> {
    let text = editor.text();
    let cursor = editor.cursor_offset();
    let line_start = text[..cursor].rfind('\n').map_or(0, |offset| offset + 1);
    let line_end = text[cursor..]
        .find('\n')
        .map_or(text.len(), |offset| cursor + offset);
    let mut start = line_start;
    while start < cursor {
        let rest = &text[start..line_end];
        let first = rest.chars().next()?;
        if first.is_whitespace() {
            start += first.len_utf8();
            continue;
        }
        let is_reference = first == '@';
        let quoted = is_reference && rest.starts_with("@\"");
        let mut end = line_end;
        if quoted {
            let mut escaped = false;
            for (offset, character) in rest[2..].char_indices() {
                if character == '"' && !escaped {
                    end = start + 2 + offset + 1;
                    while end < line_end && !text[end..].starts_with(char::is_whitespace) {
                        end += text[end..].chars().next()?.len_utf8();
                    }
                    break;
                }
                escaped = character == '\\' && !escaped;
            }
        } else if let Some(offset) = rest.find(char::is_whitespace) {
            end = start + offset;
        }
        if is_reference && (start + 1..=end).contains(&cursor) {
            let prefix = &text[start + 1..cursor];
            if prefix.len() > QUERY_BYTES_LIMIT {
                return Some(Reference {
                    range: start..end,
                    query: String::new(),
                    overlong: true,
                });
            }
            let query = if quoted && prefix.starts_with('"') {
                serde_json::from_str::<String>(prefix)
                    .or_else(|_| serde_json::from_str(&format!("{prefix}\"")))
                    .unwrap_or_else(|_| prefix[1..].to_owned())
            } else {
                prefix.to_owned()
            };
            return Some(Reference {
                range: start..end,
                query,
                overlong: false,
            });
        }
        // A token after a closing quote without whitespace is not a fresh reference.
        start = end;
        while start < line_end && !text[start..].starts_with(char::is_whitespace) {
            start += text[start..].chars().next()?.len_utf8();
        }
    }
    None
}

pub(crate) fn format_reference(path: &str) -> String {
    if path
        .chars()
        .any(|c| c.is_whitespace() || matches!(c, '"' | '\\') || c.is_control())
    {
        format!(
            "@{}",
            serde_json::to_string(path).expect("string serialization")
        )
    } else {
        format!("@{path}")
    }
}

/// The same directory spelling is used for matching, display, and insertion.
fn entry_path(entry: &ProjectFileEntry) -> Cow<'_, str> {
    if entry.kind == ProjectFileKind::Directory {
        Cow::Owned(format!("{}/", entry.path))
    } else {
        Cow::Borrowed(&entry.path)
    }
}

/// Linear smart-case subsequence scoring; prefer runs, boundaries, and basenames.
/// Unlike exhaustive alignments, work stays proportional to the bounded snapshot.
fn score(path: &str, query: &str, sensitive: bool) -> Option<i64> {
    if query.is_empty() {
        return Some(0);
    }
    let haystack = if sensitive {
        path.to_owned()
    } else {
        path.to_lowercase()
    };
    let mut query = query.chars();
    let mut wanted = query.next()?;
    let basename = haystack.rfind('/').map_or(0, |offset| offset + 1);
    let mut previous = '/';
    let mut consecutive = 0;
    let mut score = 0;
    for (offset, character) in haystack.char_indices() {
        if character == wanted {
            consecutive += 1;
            score += 8 * consecutive;
            if "/_-. ".contains(previous) {
                score += 12;
            }
            if offset >= basename {
                score += 6;
            }
            if let Some(next) = query.next() {
                wanted = next;
            } else {
                return Some(score);
            }
        } else {
            consecutive = 0;
        }
        previous = character;
    }
    None
}

#[derive(Default)]
pub(crate) struct FilePicker {
    context: Option<Reference>,
    dismissed: bool,
    snapshot: Option<Arc<ProjectFileSnapshot>>,
    tree: bool,
    parents: Vec<Option<usize>>,
    expanded: BTreeSet<usize>,
    rows: Vec<usize>,
    selected: usize,
    rendered: Option<usize>,
}

pub(crate) enum PickerAction {
    Ignored,
    Consumed,
    Replace(Range<usize>, String),
}

impl FilePicker {
    pub fn is_open(&self) -> bool {
        self.context.is_some() && !self.dismissed
    }

    pub fn sync_editor(&mut self, editor: &PromptEditor) {
        let context = reference(editor);
        if self.context == context {
            return;
        }
        if self.context.is_none() {
            self.tree = false;
            self.expanded.clear();
        }
        self.context = context;
        self.dismissed = false;
        self.selected = 0;
        self.rebuild_rows();
    }

    pub fn dismiss(&mut self) {
        self.dismissed = true;
        self.invalidate();
        self.snapshot = None;
        self.rows.clear();
        self.parents.clear();
        self.expanded.clear();
        self.tree = false;
    }

    pub fn invalidate(&mut self) {
        self.rendered = None;
    }

    pub fn sync_snapshot(&mut self, snapshot: Option<&Arc<ProjectFileSnapshot>>) {
        if match (&self.snapshot, snapshot) {
            (Some(old), Some(new)) => Arc::ptr_eq(old, new),
            (None, None) => true,
            _ => false,
        } {
            return;
        }
        self.snapshot = snapshot.cloned();
        self.expanded.clear();
        self.parents.clear();
        if let Some(snapshot) = &self.snapshot {
            let directories: BTreeMap<_, _> = snapshot
                .entries
                .iter()
                .enumerate()
                .filter(|(_, entry)| entry.kind == ProjectFileKind::Directory)
                .map(|(index, entry)| (entry.path.as_str(), index))
                .collect();
            self.parents = snapshot
                .entries
                .iter()
                .map(|entry| {
                    entry
                        .path
                        .rsplit_once('/')
                        .and_then(|(parent, _)| directories.get(parent).copied())
                })
                .collect();
        }
        self.selected = 0;
        self.rebuild_rows();
    }

    fn rebuild_rows(&mut self) {
        self.invalidate();
        self.rows.clear();
        let (Some(snapshot), Some(context)) = (&self.snapshot, &self.context) else {
            return;
        };
        if context.overlong {
            return;
        }
        if self.tree {
            self.rows
                .extend((0..snapshot.entries.len()).filter(|&index| {
                    let mut parent = self.parents[index];
                    // The validated snapshot is parent-before-child. Never synthesize
                    // absent directories or imply a truncated directory is empty.
                    while let Some(index) = parent {
                        if !self.expanded.contains(&index) {
                            return false;
                        }
                        parent = self.parents[index];
                    }
                    true
                }));
        } else {
            let sensitive = context.query.chars().any(char::is_uppercase);
            let query = if sensitive {
                context.query.clone()
            } else {
                context.query.to_lowercase()
            };
            let mut ranked: Vec<_> = snapshot
                .entries
                .iter()
                .enumerate()
                .filter_map(|(index, entry)| {
                    score(&entry_path(entry), &query, sensitive).map(|score| (index, score))
                })
                .collect();
            if !context.query.is_empty() {
                ranked.sort_by(|&(a, a_score), &(b, b_score)| {
                    b_score
                        .cmp(&a_score)
                        .then_with(|| {
                            snapshot.entries[a]
                                .path
                                .len()
                                .cmp(&snapshot.entries[b].path.len())
                        })
                        .then_with(|| snapshot.entries[a].path.cmp(&snapshot.entries[b].path))
                });
            }
            self.rows.extend(
                ranked
                    .into_iter()
                    .take(RESULT_LIMIT)
                    .map(|(index, _)| index),
            );
        }
        self.selected = self.selected.min(self.rows.len().saturating_sub(1));
    }

    fn toggle_tree(&mut self) {
        let selected = self.rows.get(self.selected).copied();
        self.tree = !self.tree;
        if self.tree {
            let mut parent = selected.and_then(|index| self.parents[index]);
            while let Some(index) = parent {
                self.expanded.insert(index);
                parent = self.parents[index];
            }
        }
        self.rebuild_rows();
        self.selected = selected
            .and_then(|index| self.rows.iter().position(|row| *row == index))
            .unwrap_or(0);
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> PickerAction {
        if self.dismissed
            && self.context.is_some()
            && key.code == KeyCode::Tab
            && key.modifiers == KeyModifiers::NONE
        {
            self.dismissed = false;
            return PickerAction::Consumed;
        }
        if !self.is_open() || key.modifiers != KeyModifiers::NONE {
            return PickerAction::Ignored;
        }
        match key.code {
            KeyCode::Esc => self.dismiss(),
            KeyCode::Tab => self.toggle_tree(),
            KeyCode::Up => {
                self.selected = self.selected.saturating_sub(1);
                self.invalidate();
            }
            KeyCode::Down => {
                self.selected = (self.selected + 1).min(self.rows.len().saturating_sub(1));
                self.invalidate();
            }
            KeyCode::Enter | KeyCode::Left | KeyCode::Right
                if key.code == KeyCode::Enter || self.tree =>
            {
                let Some(index) = self.rows.get(self.selected).copied() else {
                    return PickerAction::Consumed;
                };
                if self.rendered != Some(index) {
                    return PickerAction::Consumed;
                }
                let snapshot = self.snapshot.as_ref().expect("row has snapshot");
                let entry = &snapshot.entries[index];
                if self.tree && key.code == KeyCode::Left {
                    if !self.expanded.remove(&index) {
                        if let Some(parent) = self.parents[index] {
                            self.selected =
                                self.rows.iter().position(|row| *row == parent).unwrap_or(0);
                        }
                    }
                    self.rebuild_rows();
                } else if self.tree && entry.kind == ProjectFileKind::Directory {
                    if key.code == KeyCode::Right || !self.expanded.remove(&index) {
                        self.expanded.insert(index);
                    }
                    self.rebuild_rows();
                } else if key.code == KeyCode::Enter {
                    let reference = format_reference(&entry_path(entry));
                    let range = self.context.as_ref().expect("open reference").range.clone();
                    return PickerAction::Replace(range, reference);
                }
            }
            _ => return PickerAction::Ignored,
        }
        PickerAction::Consumed
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        files: &ProjectFiles,
        palette: Palette,
    ) {
        self.invalidate();
        crate::ui::clear_overlay(frame, area, palette);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(if self.tree {
                " @ files: tree "
            } else {
                " @ files: fuzzy "
            })
            .title_bottom(" ↑↓ Enter Tab Esc ");
        let inner = block.inner(area);
        frame.render_widget(block, area);
        if inner.height == 0 || inner.width == 0 {
            return;
        }
        let message = if self
            .context
            .as_ref()
            .is_some_and(|context| context.overlong)
        {
            Some("Reference query exceeds 4096 bytes; shorten it to browse.".into())
        } else if files.loading() {
            Some("Loading project files…".to_owned())
        } else if let Some(error) = &files.error {
            Some(format!(
                "{} Close/reopen to retry.",
                crate::ui::sanitize_for_terminal(error)
            ))
        } else if self.rows.is_empty() {
            Some(
                if files.snapshot.as_ref().is_some_and(|s| s.truncated) {
                    "No matches in limited snapshot."
                } else {
                    "No matching project files."
                }
                .to_owned(),
            )
        } else {
            None
        };
        if let Some(message) = message {
            frame.render_widget(Paragraph::new(message).wrap(Wrap { trim: true }), inner);
            return;
        }
        let limited = files.snapshot.as_ref().is_some_and(|s| s.truncated);
        frame.render_widget(
            Paragraph::new(if limited {
                "Limited snapshot • paths omitted"
            } else if self.tree {
                "←/→ folders • Tab fuzzy"
            } else {
                "Top 30 matches • Tab tree"
            }),
            Rect { height: 1, ..inner },
        );
        let list_area = Rect {
            y: inner.y + 1,
            height: inner.height - 1,
            ..inner
        };
        if list_area.height == 0 {
            return;
        }
        let snapshot = self.snapshot.as_ref().expect("rows have snapshot");
        let offset = self
            .selected
            .saturating_sub(usize::from(list_area.height) - 1);
        let rows = self
            .rows
            .iter()
            .skip(offset)
            .take(usize::from(list_area.height))
            .map(|&index| {
                let entry = &snapshot.entries[index];
                let label = if self.tree {
                    let depth = entry.path.matches('/').count().min(12);
                    let marker = if entry.kind == ProjectFileKind::Directory {
                        if self.expanded.contains(&index) {
                            "▾ "
                        } else {
                            "▸ "
                        }
                    } else {
                        "  "
                    };
                    format!(
                        "{}{marker}{}",
                        " ".repeat(depth * 2),
                        entry.path.rsplit('/').next().unwrap_or(&entry.path)
                    )
                } else {
                    entry_path(entry).into_owned()
                };
                ListItem::new(Line::raw(crate::ui::sanitize_for_terminal(&label)))
            })
            .collect::<Vec<_>>();
        let mut selection = ListState::default().with_selected(Some(self.selected - offset));
        frame.render_stateful_widget(
            List::new(rows)
                .highlight_symbol("› ")
                .highlight_style(palette.selection()),
            list_area,
            &mut selection,
        );
        self.rendered = self.rows.get(self.selected).copied();
    }
}
