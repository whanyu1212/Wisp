//! Fixed, keyboard-only persisted-session picker.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::reducer::SessionSummary;

pub const SESSION_PICKER_LIMIT: usize = 50;
const PAGE_STEP: usize = 10;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionPicker {
    sessions: Vec<SessionSummary>,
    selected: Option<usize>,
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
        let selected = selected_session_id
            .and_then(|id| sessions.iter().position(|session| session.session_id == id))
            .or_else(|| (!sessions.is_empty()).then_some(0));
        Self { sessions, selected }
    }

    #[cfg(test)]
    pub fn sessions(&self) -> &[SessionSummary] {
        &self.sessions
    }

    #[cfg(test)]
    pub fn selected(&self) -> Option<usize> {
        self.selected
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> SessionPickerAction {
        if key.modifiers != KeyModifiers::NONE {
            return SessionPickerAction::None;
        }
        match key.code {
            KeyCode::Esc => return SessionPickerAction::Cancelled,
            KeyCode::Enter => {
                return self
                    .selected
                    .and_then(|index| self.sessions.get(index))
                    .map(|session| SessionPickerAction::Selected(session.session_id.clone()))
                    .unwrap_or(SessionPickerAction::None);
            }
            KeyCode::Up => self.move_by(-1),
            KeyCode::Down => self.move_by(1),
            KeyCode::PageUp => self.move_by(-(PAGE_STEP as isize)),
            KeyCode::PageDown => self.move_by(PAGE_STEP as isize),
            KeyCode::Home => self.selected = (!self.sessions.is_empty()).then_some(0),
            KeyCode::End => self.selected = self.sessions.len().checked_sub(1),
            _ => {}
        }
        SessionPickerAction::None
    }

    fn move_by(&mut self, delta: isize) {
        let Some(selected) = self.selected else {
            return;
        };
        let maximum = self.sessions.len().saturating_sub(1) as isize;
        self.selected = Some((selected as isize).saturating_add(delta).clamp(0, maximum) as usize);
    }
}

pub fn render(frame: &mut Frame<'_>, area: Rect, picker: &SessionPicker) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(2), Constraint::Length(1)])
        .split(area);
    let height = usize::from(chunks[0].height.saturating_sub(2)).max(1);
    let width = usize::from(chunks[0].width.saturating_sub(2)).max(1);
    let start = picker
        .selected
        .map(|selected| selected.saturating_sub(height.saturating_sub(1)))
        .unwrap_or(0);
    let lines = if picker.sessions.is_empty() {
        vec![Line::styled(
            "No persisted sessions.",
            Style::default().fg(Color::DarkGray),
        )]
    } else {
        picker
            .sessions
            .iter()
            .enumerate()
            .skip(start)
            .take(height)
            .map(|(index, session)| session_line(session, picker.selected == Some(index), width))
            .collect()
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(" resume session ")
                .borders(Borders::ALL),
        ),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new(if picker.sessions.is_empty() {
            "Esc close"
        } else {
            "↑/↓ move · PgUp/PgDn · Home/End · Enter select · Esc cancel"
        })
        .alignment(Alignment::Center)
        .style(Style::default().fg(Color::DarkGray)),
        chunks[1],
    );
}

fn session_line(session: &SessionSummary, selected: bool, width: usize) -> Line<'static> {
    let label = session
        .name
        .as_deref()
        .filter(|name| !name.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| compact_path(&session.session_path));
    let content = terminal_row(
        &format!(
            "{} {}  · {} entries · {}",
            if selected { ">" } else { " " },
            label,
            session.entry_count,
            session.updated_at,
        ),
        width,
    );
    let style = if selected {
        Style::default()
            .fg(Color::White)
            .bg(Color::Blue)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default()
    };
    Line::from(Span::styled(content, style))
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
    let mut columns = 0_usize;
    for grapheme in content.graphemes(true) {
        let next = columns.saturating_add(grapheme.width());
        if next > width - 1 {
            break;
        }
        row.push_str(grapheme);
        columns = next;
    }
    row.push('…');
    row
}

fn compact_path(path: &str) -> String {
    const MAX_CHARS: usize = 48;
    let count = path.chars().count();
    if count <= MAX_CHARS {
        return path.into();
    }
    let tail = path
        .chars()
        .skip(count.saturating_sub(MAX_CHARS - 1))
        .collect::<String>();
    format!("…{tail}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn summary(index: usize) -> SessionSummary {
        SessionSummary {
            session_id: format!("session-{index}"),
            session_path: format!("/very/long/path/session-{index}.jsonl"),
            name: (index == 1).then(|| "named".into()),
            updated_at: "2026-01-02T03:04:05Z".into(),
            entry_count: index as u32,
        }
    }

    #[test]
    fn empty_single_and_capped_catalogs_are_navigable() {
        let mut empty = SessionPicker::new(vec![], None);
        assert_eq!(
            empty.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
            SessionPickerAction::None
        );
        assert_eq!(
            empty.handle_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
            SessionPickerAction::Cancelled
        );

        let mut single = SessionPicker::new(vec![summary(0)], None);
        assert_eq!(single.selected(), Some(0));
        assert_eq!(
            single.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
            SessionPickerAction::Selected("session-0".into())
        );

        let mut picker = SessionPicker::new((0..55).map(summary).collect(), Some("session-1"));
        assert_eq!(picker.sessions().len(), SESSION_PICKER_LIMIT);
        assert_eq!(picker.selected(), Some(1));
        picker.handle_key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        assert_eq!(picker.selected(), Some(49));
        picker.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        assert_eq!(picker.selected(), Some(0));
        picker.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(picker.selected(), Some(PAGE_STEP));
        picker.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(picker.selected(), Some(0));
    }

    #[test]
    fn long_names_and_paths_render_as_one_terminal_row() {
        let mut named = summary(0);
        named.name = Some(format!("name\n{}", "界".repeat(100)));
        let mut path = summary(0);
        path.name = None;
        path.session_path = format!("/{}\n{}", "prefix".repeat(20), "界".repeat(100));

        for session in [named, path] {
            let line = session_line(&session, true, 12);
            let content = line
                .spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>();
            assert!(!content.contains('\n'));
            assert!(content.width() <= 12);
        }
    }
}
