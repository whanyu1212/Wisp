//! Searchable persisted sessions; backend pages and painted identities are authoritative.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Direction, Layout, Rect},
    style::Style,
    text::{Line, Span, Text},
    widgets::{Block, Borders, Paragraph},
};
use tokio::time::{Duration, Instant};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::{
    mouse::Rows,
    reducer::{CatalogNavigation, SessionSummary},
    theme::Palette,
};

pub const SESSION_PICKER_LIMIT: usize = 50;
const QUERY_BYTES: usize = 1024;
const DEBOUNCE: Duration = Duration::from_millis(200);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionPicker {
    sessions: Vec<SessionSummary>,
    selected_id: Option<String>,
    query: String,
    owner_id: Option<String>,
    navigation: CatalogNavigation,
    revision: u64,
    painted: Option<(u64, String)>,
    painted_revision: Option<u64>,
    inflight: Option<(String, u64)>,
    pending: Option<(Option<String>, Instant)>,
    page_cursor: Option<String>,
    error: Option<String>,
    pub selecting: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionPickerAction {
    None,
    Cancelled,
    Selected(String),
}

impl SessionPicker {
    pub fn new(mut sessions: Vec<SessionSummary>, selected_session_id: Option<&str>) -> Self {
        sessions.truncate(SESSION_PICKER_LIMIT);
        let selected_id = selected_session_id
            .filter(|id| sessions.iter().any(|s| s.session_id == *id))
            .map(str::to_owned)
            .or_else(|| sessions.first().map(|s| s.session_id.clone()));
        Self {
            sessions,
            selected_id,
            query: String::new(),
            owner_id: None,
            navigation: CatalogNavigation::default(),
            revision: 0,
            painted: None,
            painted_revision: None,
            inflight: None,
            pending: None,
            page_cursor: None,
            error: None,
            selecting: false,
        }
    }

    pub fn loading() -> Self {
        let mut picker = Self::new(Vec::new(), None);
        picker.schedule(None, Duration::ZERO);
        picker
    }

    fn invalidate(&mut self) {
        self.revision = self.revision.wrapping_add(1);
        self.painted = None;
        self.painted_revision = None;
    }

    fn schedule(&mut self, cursor: Option<String>, delay: Duration) {
        self.invalidate();
        self.error = None;
        self.pending = Some((cursor, Instant::now() + delay));
    }

    pub fn request_deadline(&self) -> Option<Instant> {
        if self.inflight.is_some() || self.selecting {
            return None;
        }
        self.pending.as_ref().map(|(_, due)| *due)
    }

    pub fn request_due(&self) -> Option<(String, Option<String>)> {
        let (cursor, due) = self.pending.as_ref()?;
        (self.inflight.is_none() && !self.selecting && Instant::now() >= *due)
            .then(|| (self.query.clone(), cursor.clone()))
    }

    pub fn editing_identity(&self) -> Option<(String, String)> {
        self.owner_id
            .as_ref()
            .map(|id| (id.clone(), self.query.clone()))
    }

    pub fn started(&mut self, command_id: String) {
        self.owner_id.get_or_insert_with(|| command_id.clone());
        self.page_cursor = self.pending.take().and_then(|(cursor, _)| cursor);
        self.inflight = Some((command_id, self.revision));
    }

    fn settle(&mut self, command_id: &str) -> bool {
        let Some((id, revision)) = &self.inflight else {
            return false;
        };
        if id != command_id {
            return false;
        }
        let current = *revision == self.revision;
        self.inflight = None;
        current
    }

    pub fn loaded(
        &mut self,
        command_id: &str,
        mut sessions: Vec<SessionSummary>,
        navigation: CatalogNavigation,
        selected: Option<&str>,
    ) {
        if !self.settle(command_id) {
            return;
        }
        sessions.truncate(SESSION_PICKER_LIMIT);
        let preferred = self.selected_id.as_deref().or(selected);
        self.selected_id = preferred
            .filter(|id| sessions.iter().any(|s| s.session_id == *id))
            .map(str::to_owned)
            .or_else(|| sessions.first().map(|s| s.session_id.clone()));
        self.sessions = sessions;
        self.navigation = navigation;
        self.error = None;
        self.invalidate();
    }

    pub fn failed(&mut self, command_id: &str, error: String) {
        if self.settle(command_id) {
            self.error = Some(error);
            self.invalidate();
        }
    }

    pub fn selection_failed(&mut self, error: String) {
        self.selecting = false;
        self.error = Some(error);
        self.invalidate();
    }

    fn ready(&self) -> bool {
        self.pending.is_none() && self.inflight.is_none() && self.error.is_none() && !self.selecting
    }

    pub fn mark_rendered(&mut self) {
        if self.ready() {
            self.painted_revision = Some(self.revision);
            self.painted = self.selected_id.clone().map(|id| (self.revision, id));
        }
    }

    pub fn rendered_selection(&self) -> Option<&str> {
        self.painted
            .as_ref()
            .filter(|(revision, id)| {
                self.ready() && *revision == self.revision && self.selected_id.as_ref() == Some(id)
            })
            .map(|(_, id)| id.as_str())
    }

    pub fn insert_paste(&mut self, text: &str) {
        if self.selecting {
            return;
        }
        let mut changed = false;
        for ch in text.chars().filter(|c| !c.is_control()) {
            if self.query.len() + ch.len_utf8() > QUERY_BYTES {
                break;
            }
            self.query.push(ch);
            changed = true;
        }
        if changed {
            self.schedule(None, DEBOUNCE);
        }
    }

    fn selected(&self) -> Option<usize> {
        self.selected_id
            .as_ref()
            .and_then(|id| self.sessions.iter().position(|s| &s.session_id == id))
    }

    fn select_index(&mut self, index: usize) {
        if let Some(session) = self.sessions.get(index) {
            if self.selected_id.as_ref() != Some(&session.session_id) {
                self.selected_id = Some(session.session_id.clone());
                self.painted = None;
            }
        }
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> SessionPickerAction {
        if key.code == KeyCode::Esc {
            return SessionPickerAction::Cancelled;
        }
        if self.selecting {
            return SessionPickerAction::None;
        }
        if key.modifiers == KeyModifiers::CONTROL {
            match key.code {
                KeyCode::Char('r') => self.schedule(None, Duration::ZERO),
                KeyCode::Char('y') => self.schedule(self.page_cursor.clone(), Duration::ZERO),
                KeyCode::Left if self.ready() => {
                    if let Some(cursor) = self.navigation.previous_cursor.clone() {
                        self.schedule(Some(cursor), Duration::ZERO);
                    }
                }
                KeyCode::Right if self.ready() => {
                    if let Some(cursor) = self.navigation.next_cursor.clone() {
                        self.schedule(Some(cursor), Duration::ZERO);
                    }
                }
                KeyCode::Char('u') => {
                    self.query.clear();
                    self.schedule(None, DEBOUNCE);
                }
                _ => {}
            }
            return SessionPickerAction::None;
        }
        if key
            .modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SUPER)
        {
            return SessionPickerAction::None;
        }
        match key.code {
            KeyCode::Char(ch) => self.insert_paste(&ch.to_string()),
            KeyCode::Backspace => {
                if let Some((index, _)) = self.query.grapheme_indices(true).next_back() {
                    self.query.truncate(index);
                    self.schedule(None, DEBOUNCE);
                }
            }
            KeyCode::Enter => {
                return self
                    .rendered_selection()
                    .map(|id| SessionPickerAction::Selected(id.into()))
                    .unwrap_or(SessionPickerAction::None);
            }
            code if self.ready() => {
                let current = self.selected().unwrap_or(0);
                let last = self.sessions.len().saturating_sub(1);
                let index = match code {
                    KeyCode::Up => current.saturating_sub(1),
                    KeyCode::Down => current.saturating_add(1).min(last),
                    KeyCode::PageUp => current.saturating_sub(10),
                    KeyCode::PageDown => current.saturating_add(10).min(last),
                    KeyCode::Home => 0,
                    KeyCode::End => last,
                    _ => current,
                };
                self.select_index(index);
            }
            _ => {}
        }
        SessionPickerAction::None
    }

    pub fn select_mouse(&mut self, index: usize) -> bool {
        if !self.ready()
            || self.painted_revision != Some(self.revision)
            || index >= self.sessions.len()
        {
            return false;
        }
        self.select_index(index);
        true
    }
}

pub fn render(frame: &mut Frame<'_>, area: Rect, picker: &SessionPicker, palette: Palette) -> Rows {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(2),
            Constraint::Length(2),
        ])
        .split(area);
    frame.render_widget(
        Paragraph::new(terminal_row(
            &format!("Search: {}", picker.query),
            chunks[0].width as usize,
        )),
        chunks[0],
    );
    let height = usize::from(chunks[1].height.saturating_sub(2)).max(1);
    let width = usize::from(chunks[1].width.saturating_sub(2)).max(1);
    let start = picker
        .selected()
        .map(|i| i.saturating_sub(height.saturating_sub(1)))
        .unwrap_or(0);
    let status = if picker.selecting {
        Some("Selecting session…")
    } else if let Some(error) = &picker.error {
        Some(error.as_str())
    } else if !picker.ready() {
        Some("Loading sessions…")
    } else if picker.sessions.is_empty() {
        Some("No matching sessions.")
    } else {
        None
    };
    let lines = if let Some(status) = status {
        vec![Line::raw(terminal_row(status, width))]
    } else {
        picker
            .sessions
            .iter()
            .enumerate()
            .skip(start)
            .take(height)
            .map(|(index, session)| {
                let content = terminal_row(
                    &format!(
                        "{} [{}] {} · {} entries · {}",
                        if picker.selected() == Some(index) {
                            ">"
                        } else {
                            " "
                        },
                        session.session_id.chars().take(12).collect::<String>(),
                        session.name.as_deref().unwrap_or("Unnamed"),
                        session.entry_count,
                        session.updated_at
                    ),
                    width,
                );
                Line::from(Span::styled(
                    content,
                    if picker.selected() == Some(index) {
                        palette.selection()
                    } else {
                        Style::default()
                    },
                ))
            })
            .collect()
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(" resume session ")
                .border_style(palette.border())
                .borders(Borders::ALL),
        ),
        chunks[1],
    );
    let hints = if area.width < 60 {
        "↑↓ move · Enter · Esc\n^←/→ page ^R fresh ^Y retry".to_owned()
    } else {
        format!(
            "↑/↓ move · Enter select · Esc close\nCtrl+←/→ page · Ctrl+R refresh · Ctrl+Y retry{}{}",
            if picker.navigation.previous_cursor.is_some() {
                " · prev"
            } else {
                ""
            },
            if picker.navigation.next_cursor.is_some() {
                " · next"
            } else {
                ""
            }
        )
    };
    frame.render_widget(
        Paragraph::new(hints).style(Style::default().fg(palette.muted)),
        chunks[2],
    );
    Rows::new(
        chunks[1].inner(ratatui::layout::Margin::new(1, 1)),
        start,
        if status.is_none() {
            picker.sessions.len()
        } else {
            0
        },
        1,
    )
}

pub(crate) fn terminal_row(content: &str, width: usize) -> String {
    let content = crate::ui::sanitize_for_terminal(content).replace('\n', " ");
    if content.width() <= width {
        return content;
    }
    if width <= 1 {
        return "…".chars().take(width).collect();
    }
    let mut row = String::new();
    let mut columns = 0;
    for grapheme in content.graphemes(true) {
        let next = columns + grapheme.width();
        if next > width - 1 {
            break;
        }
        row.push_str(grapheme);
        columns = next;
    }
    row.push('…');
    row
}

#[cfg(test)]
mod tests {
    use super::*;
    fn summary(id: &str) -> SessionSummary {
        SessionSummary {
            session_id: id.into(),
            session_path: format!("/{id}"),
            name: Some("duplicate".into()),
            updated_at: "today".into(),
            entry_count: 1,
        }
    }
    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn activation_requires_painted_identity() {
        let mut p = SessionPicker::new(vec![summary("one"), summary("two")], None);
        assert_eq!(p.handle_key(key(KeyCode::Enter)), SessionPickerAction::None);
        p.mark_rendered();
        assert_eq!(
            p.handle_key(key(KeyCode::Enter)),
            SessionPickerAction::Selected("one".into())
        );
        p.handle_key(key(KeyCode::Down));
        assert_eq!(p.handle_key(key(KeyCode::Enter)), SessionPickerAction::None);
        p.mark_rendered();
        assert_eq!(
            p.handle_key(key(KeyCode::Enter)),
            SessionPickerAction::Selected("two".into())
        );
    }

    #[test]
    fn superseded_query_and_stale_mouse_cannot_activate() {
        let mut p = SessionPicker::loading();
        p.started("old".into());
        p.insert_paste("new");
        p.loaded(
            "old",
            vec![summary("wrong")],
            CatalogNavigation::default(),
            None,
        );
        assert!(p.sessions.is_empty());
        assert!(p.inflight.is_none());
        assert!(p.pending.is_some());
        assert!(!p.select_mouse(0));
        p.started("new".into());
        p.loaded(
            "old",
            vec![summary("wrong")],
            CatalogNavigation::default(),
            None,
        );
        assert!(p.inflight.is_some());
        p.loaded(
            "new",
            vec![summary("right")],
            CatalogNavigation::default(),
            None,
        );
        assert!(!p.select_mouse(0));
        p.mark_rendered();
        assert!(p.select_mouse(0));
    }

    #[test]
    fn failed_page_keeps_query_and_retry_boundary() {
        let mut p = SessionPicker::new(vec![summary("one")], None);
        p.navigation.next_cursor = Some("next".into());
        p.handle_key(KeyEvent::new(KeyCode::Right, KeyModifiers::CONTROL));
        assert_eq!(p.request_due(), Some((String::new(), Some("next".into()))));
        p.started("request".into());
        p.failed("request", "failed".into());
        assert_eq!(p.sessions[0].session_id, "one");
        p.handle_key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::CONTROL));
        assert_eq!(p.request_due(), Some((String::new(), Some("next".into()))));
    }

    #[test]
    fn paste_is_bounded_and_backspace_removes_a_grapheme() {
        let mut p = SessionPicker::new(vec![], None);
        p.insert_paste("e\u{301}");
        p.handle_key(key(KeyCode::Backspace));
        assert!(p.query.is_empty());
        p.insert_paste(&"界".repeat(2000));
        assert!(p.query.len() <= QUERY_BYTES);
        assert!(!p.query.contains('\n'));
    }
}
