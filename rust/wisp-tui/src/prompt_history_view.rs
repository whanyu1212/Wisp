//! Search and selection over process-local prompt history; the draft stays in LiveUi.

use crate::{
    prompt_editor::PromptEditor, prompt_history::PromptHistory, session_picker::terminal_row,
    theme::Palette,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph},
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
    ) {
        self.invalidate_selection();
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(" Prompt history ")
            .title_bottom(" Enter restore · Esc/Ctrl-R close ");
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [query_area, list_area, status_area] = Layout::vertical([
            Constraint::Length(2),
            Constraint::Min(1),
            Constraint::Length(1),
        ])
        .areas(inner);
        let width = usize::from(inner.width);
        let query_width = width.saturating_sub(2);
        let column = self.query.cursor_column();
        let start = column.saturating_sub(query_width.saturating_sub(1));
        let query = crate::ui::source_display_column_window(self.query.text(), start, query_width);
        let cursor_column = column.saturating_sub(query.effective_start);
        frame.render_widget(Paragraph::new(format!("> {}", query.text)), query_area);
        if query_area.width > 2 && query_area.height > 0 {
            frame.set_cursor_position((query_area.x + 2 + cursor_column as u16, query_area.y));
        }
        self.page_size = usize::from(list_area.height);
        let status = if self.query_limit_reached {
            "Search is limited to 1024 bytes; input kept.".to_owned()
        } else {
            format!(
                "{} matches · ↑↓ PgUp/PgDn · current TUI run",
                self.matches.len()
            )
        };
        frame.render_widget(Paragraph::new(terminal_row(&status, width)), status_area);
        if self.matches.is_empty() {
            frame.render_widget(
                Paragraph::new(if history.is_empty() {
                    "No prompts submitted in this TUI run."
                } else {
                    "No matching prompts."
                }),
                list_area,
            );
            return;
        }
        if list_area.height == 0 || list_area.width < 3 {
            return;
        }
        let rows = self
            .matches
            .iter()
            .filter_map(|id| history.entry(*id))
            .map(|entry| ListItem::new(terminal_row(&entry.preview, width.saturating_sub(2))))
            .collect::<Vec<_>>();
        let selected = self
            .matches
            .iter()
            .position(|id| Some(*id) == self.selected);
        let mut state = ListState::default().with_selected(selected);
        frame.render_stateful_widget(
            List::new(rows)
                .highlight_symbol("› ")
                .highlight_style(palette.selection()),
            list_area,
            &mut state,
        );
        self.rendered = self.selected;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn draw(view: &mut PromptHistoryView, history: &PromptHistory) -> String {
        let mut terminal = Terminal::new(TestBackend::new(40, 10)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), history, Palette::default()))
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
