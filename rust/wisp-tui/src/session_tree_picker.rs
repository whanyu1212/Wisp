//! Bounded, keyboard-only persisted-session tree picker.

use std::collections::{HashSet, VecDeque};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph};

use crate::reducer::{
    SESSION_TREE_RETAINED_LIMIT, SessionTreeNode, SessionTreeNodeKind, SessionTreePage,
};
use crate::session_picker::terminal_row;

const PAGE_STEP: usize = 10;
const RETAINED_PAGE_LIMIT: usize = 2;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionTreePicker {
    pages: VecDeque<SessionTreePage>,
    selected: Option<usize>,
    earlier_nodes_omitted: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionTreePickerAction {
    None,
    Cancelled,
    Navigate(String),
    Fork(String),
    ForkUnavailable,
    LoadNext(String),
}

impl SessionTreePicker {
    pub fn new(page: SessionTreePage) -> Self {
        let selected = (!page.nodes.is_empty()).then_some(0);
        Self {
            pages: VecDeque::from([page]),
            selected,
            earlier_nodes_omitted: false,
        }
    }

    pub fn append(&mut self, page: SessionTreePage) -> Result<(), &'static str> {
        let Some(first) = self.pages.front() else {
            *self = Self::new(page);
            return Ok(());
        };
        if first.session != page.session
            || first.active_leaf_id != page.active_leaf_id
            || first.total_node_count != page.total_node_count
        {
            return Err("Session tree page changed scope while paging; kept the current rows.");
        }
        let retained_ids = self
            .pages
            .iter()
            .flat_map(|page| page.nodes.iter().map(|node| node.entry_id.as_str()))
            .collect::<HashSet<_>>();
        if page
            .nodes
            .iter()
            .any(|node| retained_ids.contains(node.entry_id.as_str()))
        {
            return Err("Session tree page repeated an existing node; kept the current rows.");
        }

        let mut selected = self.node_count();
        self.pages.push_back(page);
        while self.pages.len() > RETAINED_PAGE_LIMIT
            || self.node_count() > SESSION_TREE_RETAINED_LIMIT
        {
            let removed = self
                .pages
                .pop_front()
                .map(|page| page.nodes.len())
                .unwrap_or(0);
            selected = selected.saturating_sub(removed);
            self.earlier_nodes_omitted = true;
        }
        self.selected = (self.node_count() > 0).then_some(selected.min(self.node_count() - 1));
        Ok(())
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> SessionTreePickerAction {
        if key.modifiers != KeyModifiers::NONE {
            return SessionTreePickerAction::None;
        }
        match key.code {
            KeyCode::Esc => SessionTreePickerAction::Cancelled,
            KeyCode::Enter => self
                .selected_node()
                .map(|node| SessionTreePickerAction::Navigate(node.entry_id.clone()))
                .unwrap_or(SessionTreePickerAction::None),
            KeyCode::Char('f') => self
                .selected_node()
                .filter(|node| {
                    node.kind == SessionTreeNodeKind::Message
                        && node.role.as_deref() == Some("user")
                })
                .map(|node| SessionTreePickerAction::Fork(node.entry_id.clone()))
                .unwrap_or(SessionTreePickerAction::ForkUnavailable),
            KeyCode::Up => {
                self.move_by(-1);
                SessionTreePickerAction::None
            }
            KeyCode::Down => {
                self.move_by(1);
                SessionTreePickerAction::None
            }
            KeyCode::PageUp => {
                self.move_by(-(PAGE_STEP as isize));
                SessionTreePickerAction::None
            }
            KeyCode::PageDown if self.selected == self.node_count().checked_sub(1) => self
                .pages
                .back()
                .and_then(|page| page.next_after_entry_id.clone())
                .map(SessionTreePickerAction::LoadNext)
                .unwrap_or(SessionTreePickerAction::None),
            KeyCode::PageDown => {
                self.move_by(PAGE_STEP as isize);
                SessionTreePickerAction::None
            }
            KeyCode::Home => {
                self.selected = (self.node_count() > 0).then_some(0);
                SessionTreePickerAction::None
            }
            KeyCode::End => {
                self.selected = self.node_count().checked_sub(1);
                SessionTreePickerAction::None
            }
            _ => SessionTreePickerAction::None,
        }
    }

    fn move_by(&mut self, delta: isize) {
        let Some(selected) = self.selected else {
            return;
        };
        let maximum = self.node_count().saturating_sub(1) as isize;
        self.selected = Some((selected as isize).saturating_add(delta).clamp(0, maximum) as usize);
    }

    fn node_count(&self) -> usize {
        self.pages.iter().map(|page| page.nodes.len()).sum()
    }

    fn nodes(&self) -> impl Iterator<Item = &SessionTreeNode> {
        self.pages.iter().flat_map(|page| page.nodes.iter())
    }

    fn selected_node(&self) -> Option<&SessionTreeNode> {
        self.selected
            .and_then(|selected| self.nodes().nth(selected))
    }
}

pub fn render(frame: &mut Frame<'_>, area: Rect, picker: &SessionTreePicker) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(2), Constraint::Length(1)])
        .split(area);
    let height = usize::from(chunks[0].height.saturating_sub(2)).max(1);
    let width = usize::from(chunks[0].width.saturating_sub(2)).max(1);
    let marker_rows = usize::from(picker.earlier_nodes_omitted);
    let visible_nodes = height.saturating_sub(marker_rows).max(1);
    let start = picker
        .selected
        .map(|selected| selected.saturating_sub(visible_nodes.saturating_sub(1)))
        .unwrap_or(0);
    let mut lines = Vec::new();
    if picker.earlier_nodes_omitted {
        lines.push(Line::styled(
            terminal_row("… earlier nodes omitted; reopen /tree to restart", width),
            Style::default().fg(Color::DarkGray),
        ));
    }
    if picker.node_count() == 0 {
        lines.push(Line::styled(
            "No persisted tree nodes.",
            Style::default().fg(Color::DarkGray),
        ));
    } else {
        let active_leaf_id = picker
            .pages
            .back()
            .and_then(|page| page.active_leaf_id.as_deref());
        lines.extend(
            picker
                .nodes()
                .enumerate()
                .skip(start)
                .take(visible_nodes)
                .map(|(index, node)| {
                    tree_line(
                        node,
                        picker.selected == Some(index),
                        active_leaf_id == Some(node.entry_id.as_str()),
                        width,
                    )
                }),
        );
    }
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(" session tree ")
                .borders(Borders::ALL),
        ),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new(
            "↑/↓ move · PgUp/PgDn · Home/End · Enter navigate · f fork user · Esc close",
        )
        .alignment(Alignment::Center)
        .style(Style::default().fg(Color::DarkGray)),
        chunks[1],
    );
}

fn tree_line(node: &SessionTreeNode, selected: bool, active: bool, width: usize) -> Line<'static> {
    let role_or_kind = node.role.as_deref().unwrap_or_else(|| node.kind.as_str());
    let parent = node
        .parent_id
        .as_deref()
        .map(compact_id)
        .unwrap_or("root".into());
    let preview = if node.preview_truncated {
        format!("{}…", node.preview)
    } else {
        node.preview.clone()
    };
    let content = terminal_row(
        &format!(
            "{}{} {:<10} {} · id {} · parent {}",
            if selected { ">" } else { " " },
            if active { "*" } else { " " },
            role_or_kind,
            preview,
            compact_id(&node.entry_id),
            parent,
        ),
        width,
    );
    let style = if selected {
        Style::default()
            .fg(Color::White)
            .bg(Color::Blue)
            .add_modifier(Modifier::BOLD)
    } else if active {
        Style::default().fg(Color::Green)
    } else {
        Style::default()
    };
    Line::from(Span::styled(content, style))
}

fn compact_id(value: &str) -> String {
    const KEEP: usize = 12;
    let count = value.chars().count();
    if count <= KEEP {
        return value.into();
    }
    let head = value.chars().take(5).collect::<String>();
    let tail = value.chars().skip(count - 6).collect::<String>();
    format!("{head}…{tail}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reducer::SessionIdentity;

    fn page(start: usize, count: usize, truncated: bool) -> SessionTreePage {
        let nodes = (start..start + count)
            .map(|index| SessionTreeNode {
                entry_id: format!("entry-{index}"),
                parent_id: index.checked_sub(1).map(|parent| format!("entry-{parent}")),
                created_at: "2026-01-02T03:04:05Z".into(),
                kind: SessionTreeNodeKind::Message,
                role: Some(if index % 2 == 0 { "user" } else { "assistant" }.into()),
                preview: format!("node {index}"),
                preview_truncated: false,
            })
            .collect::<Vec<_>>();
        SessionTreePage {
            session: Some(SessionIdentity {
                session_id: "session-1".into(),
                session_path: "session-1.jsonl".into(),
                session_name: None,
            }),
            active_leaf_id: Some("entry-399".into()),
            total_node_count: 600,
            next_after_entry_id: truncated.then(|| format!("entry-{}", start + count - 1)),
            nodes,
            truncated,
        }
    }

    #[test]
    fn pages_evict_whole_oldest_page_and_reject_duplicates() {
        let mut picker = SessionTreePicker::new(page(0, 200, true));
        picker.append(page(200, 200, true)).unwrap();
        picker.append(page(400, 200, false)).unwrap();
        assert!(picker.earlier_nodes_omitted);
        assert_eq!(picker.node_count(), 400);
        assert!(picker.append(page(400, 1, false)).is_err());
    }

    #[test]
    fn only_user_messages_can_fork() {
        let mut picker = SessionTreePicker::new(page(0, 2, false));
        assert_eq!(
            picker.handle_key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::NONE)),
            SessionTreePickerAction::Fork("entry-0".into())
        );
        picker.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(
            picker.handle_key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::NONE)),
            SessionTreePickerAction::ForkUnavailable
        );
    }

    #[test]
    fn final_page_down_requests_the_next_cursor_and_rows_are_terminal_safe() {
        let mut picker = SessionTreePicker::new(page(0, 2, true));
        picker.handle_key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        assert_eq!(
            picker.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)),
            SessionTreePickerAction::LoadNext("entry-1".into())
        );
        assert_eq!(
            picker.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)),
            SessionTreePickerAction::LoadNext("entry-1".into())
        );

        let mut node = picker.selected_node().unwrap().clone();
        node.preview = "unsafe\u{1b}[2J界界界界".into();
        let line = tree_line(&node, true, true, 16);
        let content = line
            .spans
            .iter()
            .map(|span| span.content.as_ref())
            .collect::<String>();
        assert!(!content.contains('\u{1b}'));
        assert!(content.contains('*'));
        assert!(unicode_width::UnicodeWidthStr::width(content.as_str()) <= 16);
    }

    #[test]
    fn navigation_and_tiny_terminal_render_are_bounded() {
        let mut picker = SessionTreePicker::new(page(0, 30, false));
        picker.handle_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(picker.selected, Some(PAGE_STEP));
        picker.handle_key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(picker.selected, Some(0));
        picker.handle_key(KeyEvent::new(KeyCode::End, KeyModifiers::NONE));
        assert_eq!(picker.selected, Some(29));
        picker.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        assert_eq!(picker.selected, Some(0));

        let backend = ratatui::backend::TestBackend::new(1, 1);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| render(frame, frame.area(), &picker))
            .unwrap();
    }
}
