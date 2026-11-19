//! Search and selection over process-local prompt history; the draft stays in LiveUi.

use crate::{
    dialog::{self, Dialog, Hint, PickerRow},
    mouse::Rows,
    prompt_editor::PromptEditor,
    prompt_history::PromptHistory,
    session_picker::terminal_row,
    theme::Palette,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::Style,
    widgets::Paragraph,
};

const QUERY_BYTES_LIMIT: usize = 1024;

pub(crate) enum HistoryAction {
    None,
    Close,
    Restore(u64),
}

#[derive(Default)]
pub(crate) struct PromptHistoryView {
    query: PromptEditor,
    matches: Vec<u64>,
    selected: Option<u64>,
    rendered: Option<u64>,
    page_size: usize,
    query_limit_reached: bool,
}

impl PromptHistoryView {
    // The scheduler preserves this painted choice while draining a finite event prefix.
    pub fn rendered_selection(&self) -> Option<u64> {
        self.rendered
    }

    pub fn new(history: &PromptHistory) -> Self {
        let mut view = Self::default();
        view.sync(history);
        view
    }

    /// Preserve the selected identity when new accepted prompts change the ordering.
    pub fn sync(&mut self, history: &PromptHistory) {
        self.matches = history.search(self.query.text());
        if !self.selected.is_some_and(|id| self.matches.contains(&id)) {
            self.selected = self.matches.first().copied();
        }
        self.invalidate_selection();
    }

    pub fn invalidate_selection(&mut self) {
        self.rendered = None;
    }

    pub fn select_mouse(&mut self, index: usize) -> bool {
        let Some(id) = self
            .matches
            .get(index)
            .copied()
            .filter(|_| self.rendered.is_some())
        else {
            return false;
        };
        self.selected = Some(id);
        self.invalidate_selection();
        true
    }

    pub fn handle_key(&mut self, key: KeyEvent, history: &PromptHistory) -> HistoryAction {
        if key.code == KeyCode::Esc
            || (key.modifiers == KeyModifiers::CONTROL
                && matches!(key.code, KeyCode::Char('c' | 'r')))
        {
            return HistoryAction::Close;
        }
        if key.modifiers == KeyModifiers::NONE {
            if key.code == KeyCode::Enter {
                return self
                    .selected
                    .filter(|id| self.rendered == Some(*id))
                    .map_or(HistoryAction::None, HistoryAction::Restore);
            }
            let index = self
                .matches
                .iter()
                .position(|id| Some(*id) == self.selected)
                .unwrap_or(0);
            let next = match key.code {
                KeyCode::Up => Some(index.saturating_sub(1)),
                KeyCode::Down => Some(index.saturating_add(1)),
                KeyCode::PageUp => Some(index.saturating_sub(self.page_size.max(1))),
                KeyCode::PageDown => Some(index.saturating_add(self.page_size.max(1))),
                KeyCode::Home => Some(0),
                KeyCode::End => Some(self.matches.len().saturating_sub(1)),
                _ => None,
            };
            if let Some(next) = next {
                self.selected = self
                    .matches
                    .get(next.min(self.matches.len().saturating_sub(1)))
                    .copied();
                self.invalidate_selection();
                return HistoryAction::None;
            }
        }
        let edits_query = match key.code {
            KeyCode::Char('a' | 'e') if key.modifiers == KeyModifiers::CONTROL => true,
            KeyCode::Char(_) => matches!(key.modifiers, KeyModifiers::NONE | KeyModifiers::SHIFT),
            KeyCode::Backspace | KeyCode::Delete | KeyCode::Left | KeyCode::Right => {
                key.modifiers == KeyModifiers::NONE
            }
            _ => false,
        };
        if edits_query {
            let mut query = self.query.clone();
            query.handle_key(key);
            self.update_query(query, history);
        }
        HistoryAction::None
    }

    pub fn paste(&mut self, text: &str, history: &PromptHistory) {
        if text.len() > QUERY_BYTES_LIMIT {
            self.query_limit_reached = true;
            return;
        }
        let mut query = self.query.clone();
        query.insert_paste(&text.replace(['\r', '\n'], " "));
        self.update_query(query, history);
    }

    fn update_query(&mut self, query: PromptEditor, history: &PromptHistory) {
        self.query_limit_reached = query.text().len() > QUERY_BYTES_LIMIT;
        if self.query_limit_reached {
            return;
        }
        let changed = query.text() != self.query.text();
        self.query = query;
        if changed {
            self.selected = None;
            self.sync(history);
        }
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        history: &PromptHistory,
        palette: Palette,
    ) -> dialog::Rendered {
        self.invalidate_selection();
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let mut hints = vec![
            Hint {
                label: "Enter",
                description: "restore",
                key: key(KeyCode::Enter),
            },
            Hint {
                label: "Esc",
                description: "close",
                key: key(KeyCode::Esc),
            },
        ];
        if area.width >= 56 {
            hints.extend([
                Hint {
                    label: "↑",
                    description: "prev",
                    key: key(KeyCode::Up),
                },
                Hint {
                    label: "↓",
                    description: "next",
                    key: key(KeyCode::Down),
                },
            ]);
        }
        let width = usize::from(area.width.saturating_sub(2));
        let query_width = width.saturating_sub(2);
        let column = self.query.cursor_column();
        let start = column.saturating_sub(query_width.saturating_sub(1));
        let query = crate::ui::source_display_column_window(self.query.text(), start, query_width);
        let cursor_column = column.saturating_sub(query.effective_start);
        let filter_line = format!("> {}", query.text);
        let layout = Dialog {
            title: "Prompt history",
            tabs: &[],
            filter: Some(&filter_line),
            hints: &hints,
        }
        .render(frame, area, palette);
        let inner = layout.body;
        if inner.width == 0 || inner.height == 0 {
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        if inner.y > area.y + 1 && inner.width > 2 {
            frame.set_cursor_position((area.x + 1 + 2 + cursor_column as u16, area.y + 1));
        }
        let [list_area, status_area] =
            Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).areas(inner);
        self.page_size = usize::from(list_area.height);
        let status = if self.query_limit_reached {
            "Search is limited to 1024 bytes; input kept.".to_owned()
        } else {
            format!("{} matches · current TUI run", self.matches.len())
        };
        let status_rect = Rect::new(
            status_area.x + 1,
            status_area.y,
            status_area.width.saturating_sub(2),
            1,
        );
        frame.render_widget(
            Paragraph::new(terminal_row(&status, usize::from(status_rect.width)))
                .style(Style::default().fg(palette.muted)),
            status_rect,
        );
        if self.matches.is_empty() {
            let empty_area = Rect::new(
                list_area.x + 1,
                list_area.y,
                list_area.width.saturating_sub(2),
                list_area.height,
            );
            frame.render_widget(
                Paragraph::new(if history.is_empty() {
                    "No prompts submitted in this TUI run."
                } else {
                    "No matching prompts."
                })
                .style(Style::default().fg(palette.muted)),
                empty_area,
            );
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        if list_area.height == 0 || list_area.width < 3 {
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        let selected = self
            .matches
            .iter()
            .position(|id| Some(*id) == self.selected);
        let height = usize::from(list_area.height);
        let offset = selected.map_or(0, |index| index.saturating_sub(height.saturating_sub(1)));
        for (visible_index, (index, id)) in self
            .matches
            .iter()
            .enumerate()
            .skip(offset)
            .take(height)
            .enumerate()
        {
            let Some(entry) = history.entry(*id) else {
                continue;
            };
            let row = Rect::new(
                list_area.x,
                list_area.y + visible_index as u16,
                list_area.width,
                1,
            );
            PickerRow {
                label: &entry.preview,
                description: None,
                tag: None,
                selected: selected == Some(index),
                muted: false,
            }
            .render(frame, row, palette);
        }
        self.rendered = self.selected;
        dialog::Rendered {
            rows: Rows::new(list_area, offset, self.matches.len(), 1),
            controls: layout.controls,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn draw(view: &mut PromptHistoryView, history: &PromptHistory) -> String {
        let mut terminal = Terminal::new(TestBackend::new(40, 10)).unwrap();
        terminal
            .draw(|frame| {
                view.render(frame, frame.area(), history, Palette::default());
            })
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn search_edits_unicode_and_rejects_oversized_pastes_atomically() {
        let mut history = PromptHistory::default();
        history.record("👩‍💻Straße next".into());
        let mut view = PromptHistoryView::new(&history);
        view.paste("👩‍💻", &history);
        view.handle_key(key(KeyCode::Backspace), &history);
        assert!(view.query.text().is_empty());
        view.paste("STRASSE\nnext", &history);
        assert_eq!(view.matches, [0]);
        let before = view.query.clone();
        view.paste(&"界".repeat(QUERY_BYTES_LIMIT), &history);
        assert_eq!(view.query, before);
        assert!(view.query_limit_reached);
        assert!(draw(&mut view, &history).contains("1024 bytes"));
        view.handle_key(key(KeyCode::Char('!')), &history);
        assert!(draw(&mut view, &history).contains("No matching prompts."));
        // Full-width input scrolls by terminal columns and leaves the cursor in bounds.
        let mut view = PromptHistoryView::new(&history);
        view.paste(&"界".repeat(QUERY_BYTES_LIMIT / 3), &history);
        let before = view.query.clone();
        view.handle_key(key(KeyCode::Char('界')), &history);
        assert_eq!(view.query, before);
        draw(&mut view, &history);
    }

    #[test]
    fn navigation_and_eviction_require_a_visible_choice() {
        let mut history = PromptHistory::default();
        for index in 0..100 {
            history.record(format!("prompt {index}"));
        }
        let mut view = PromptHistoryView::new(&history);
        draw(&mut view, &history);
        view.handle_key(key(KeyCode::End), &history);
        assert_eq!(view.selected, Some(0));
        assert!(matches!(
            view.handle_key(key(KeyCode::Enter), &history),
            HistoryAction::None
        ));
        draw(&mut view, &history);
        assert!(matches!(
            view.handle_key(key(KeyCode::Enter), &history),
            HistoryAction::Restore(0)
        ));
        history.record("evicts selected oldest".into());
        view.sync(&history);
        assert!(matches!(
            view.handle_key(key(KeyCode::Enter), &history),
            HistoryAction::None
        ));
        draw(&mut view, &history);
        view.handle_key(key(KeyCode::PageDown), &history);
        assert_eq!(view.selected, Some(95));
        view.handle_key(key(KeyCode::PageUp), &history);
        assert_eq!(view.selected, Some(100));
        view.handle_key(key(KeyCode::Down), &history);
        view.handle_key(key(KeyCode::Home), &history);
        assert_eq!(view.selected, Some(100));
    }

    #[test]
    fn previews_are_literal_and_terminal_controls_are_never_rendered() {
        let mut history = PromptHistory::default();
        history.record("[red] **literal** \u{1b}[31m \u{202e}end".into());
        let mut view = PromptHistoryView::new(&history);
        let text = draw(&mut view, &history);
        assert!(text.contains("[red] **literal**"));
        assert!(!text.contains(['\u{1b}', '\u{202e}']));
    }
}
