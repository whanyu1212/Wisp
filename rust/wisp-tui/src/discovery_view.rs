//! Skill selection, MCP server management, and the `/permissions` picker.

use crate::{
    mcp_manager::McpView,
    mouse::Rows,
    reducer::{UiAction, UiState},
    session_picker::terminal_row,
    theme::Palette,
    ui::sanitize_for_terminal,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::Style,
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, List, ListItem, ListState, Padding, Paragraph},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::commands::PermissionMode;
use wisp_protocol::events::SkillCatalogSnapshot;

#[derive(Debug)]
pub(crate) enum DiscoveryAction {
    None,
    Close,
    Refresh(UiAction),
    InsertSkill(String),
}

pub(crate) enum DiscoveryView {
    Skills(SkillsView),
    Mcp(Box<McpView>),
    Permissions(PermissionsView),
}

/// The `/permissions` picker. Like a pending decision, it has no default choice:
/// Enter applies only a mode the user highlighted and that has been painted,
/// because saving a mode also clears temporary grants.
#[derive(Default)]
pub(crate) struct PermissionsView {
    painted: bool,
    selected: Option<usize>,
    rendered: Option<usize>,
}

const PERMISSION_CHOICES: [(char, &str, PermissionMode); 2] = [
    ('1', "Ask before changes", PermissionMode::Ask),
    (
        '2',
        "YOLO — allow tools without asking",
        PermissionMode::Yolo,
    ),
];

impl DiscoveryView {
    // The scheduler preserves this painted choice while draining a finite event prefix.
    pub fn rendered_selection(&self) -> Option<&str> {
        match self {
            Self::Skills(view) => view.rendered.as_deref(),
            Self::Permissions(view) => view.rendered.map(|index| PERMISSION_CHOICES[index].1),
            Self::Mcp(_) => None,
        }
    }

    pub fn skills(catalog: Option<&SkillCatalogSnapshot>) -> Self {
        let mut view = SkillsView::default();
        view.sync(catalog);
        Self::Skills(view)
    }

    pub fn mcp() -> Self {
        Self::Mcp(Box::default())
    }

    pub fn permissions() -> Self {
        Self::Permissions(PermissionsView::default())
    }

    pub fn key_activates(&self, key: KeyEvent) -> bool {
        if let Self::Mcp(view) = self {
            return view.key_activates(key);
        }
        key.code == KeyCode::Enter
            || (matches!(self, Self::Permissions(_))
                && matches!(key.code, KeyCode::Char('1' | '2')))
    }

    pub fn invalidate_selection(&mut self) {
        if let Self::Mcp(view) = self {
            view.invalidate();
        }
        if let Self::Permissions(view) = self {
            view.painted = false;
            view.rendered = None;
        }
        if let Self::Skills(view) = self {
            view.rendered = None;
        }
    }

    pub fn sync_skills(&mut self, catalog: Option<&SkillCatalogSnapshot>) {
        if let Self::Skills(view) = self {
            view.sync(catalog);
        }
    }

    pub fn select_mouse(&mut self, index: usize, state: &UiState) -> bool {
        let Self::Skills(view) = self else {
            return false;
        };
        let Some(entry) = state
            .skills
            .snapshot
            .as_ref()
            .and_then(|catalog| catalog.entries.get(index))
        else {
            return false;
        };
        if view.rendered.is_none() || view.diagnostics || !entry.is_invocable() {
            return false;
        }
        view.selected = index;
        view.selected_name = Some(entry.name.clone());
        view.rendered = None;
        true
    }

    /// The MCP view, when it shows a panel docked above the composer.
    pub fn docked_mcp(&mut self) -> Option<&mut McpView> {
        match self {
            Self::Mcp(view) if view.docked() => Some(view.as_mut()),
            _ => None,
        }
    }

    /// Move the MCP view past a finished snippet preview before it is measured.
    pub fn sync_mcp(&mut self, state: &UiState) {
        if let Self::Mcp(view) = self {
            view.advance(state);
        }
    }

    /// Tell the MCP view that its last request was not sent.
    pub fn mcp_request_rejected(&mut self, notice: &str) {
        if let Self::Mcp(view) = self {
            view.request_rejected(notice);
        }
    }

    pub fn handle_paste(&mut self, text: &str, state: &UiState) -> DiscoveryAction {
        match self {
            Self::Mcp(view) => view.handle_paste(text, state),
            _ => DiscoveryAction::None,
        }
    }

    pub fn handle_key(&mut self, key: KeyEvent, state: &UiState) -> DiscoveryAction {
        // The MCP view takes typed text (hidden values), so it handles every key.
        if let Self::Mcp(view) = self {
            return view.handle_key(key, state);
        }
        if key.code == KeyCode::Esc
            || (key.code == KeyCode::Char('c') && key.modifiers == KeyModifiers::CONTROL)
        {
            return DiscoveryAction::Close;
        }
        if key.modifiers != KeyModifiers::NONE {
            return DiscoveryAction::None;
        }
        if key.code == KeyCode::Char('r') {
            return DiscoveryAction::Refresh(match self {
                Self::Skills(_) => UiAction::LoadSkills,
                Self::Mcp(_) => unreachable!("the MCP view handles its own keys"),
                Self::Permissions(_) => UiAction::Permissions(None),
            });
        }
        match self {
            Self::Permissions(view) => {
                if matches!(key.code, KeyCode::Up | KeyCode::Down) {
                    let count = PERMISSION_CHOICES.len();
                    view.selected = Some(match (view.selected, key.code) {
                        (None, KeyCode::Down) => 0,
                        (None, _) => count - 1,
                        (Some(index), KeyCode::Down) => (index + 1) % count,
                        (Some(index), _) => (index + count - 1) % count,
                    });
                    return DiscoveryAction::None;
                }
                if !view.painted
                    || state.permissions.loading()
                    || state.permissions.snapshot.is_none()
                {
                    return DiscoveryAction::None;
                }
                let choice = match key.code {
                    KeyCode::Enter => view.selected.filter(|index| view.rendered == Some(*index)),
                    KeyCode::Char(key) => PERMISSION_CHOICES
                        .iter()
                        .position(|(choice_key, ..)| *choice_key == key),
                    _ => None,
                };
                let Some(choice) = choice else {
                    return DiscoveryAction::None;
                };
                view.painted = false;
                view.rendered = None;
                DiscoveryAction::Refresh(UiAction::Permissions(Some(PERMISSION_CHOICES[choice].2)))
            }
            Self::Skills(view) => view.handle_key(key.code, state.skills.snapshot.as_deref()),
            Self::Mcp(_) => unreachable!("the MCP view handles its own keys"),
        }
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        state: &UiState,
        notice: Option<&str>,
        palette: Palette,
    ) -> Rows {
        match self {
            Self::Permissions(view) => view.render(frame, area, state, palette),
            Self::Skills(view) => view.render(frame, area, state, notice, palette),
            Self::Mcp(view) => {
                view.render(frame, area, state, palette);
                Rows::default()
            }
        }
    }
}

#[derive(Default)]
pub(crate) struct SkillsView {
    selected: usize,
    selected_name: Option<String>,
    rendered: Option<String>,
    page_size: usize,
    diagnostics: bool,
    report: ReportView,
}

impl SkillsView {
    fn sync(&mut self, catalog: Option<&SkillCatalogSnapshot>) {
        self.rendered = None;
        let entries = catalog.map_or(&[][..], |catalog| catalog.entries.as_slice());
        self.selected = entries
            .iter()
            .position(|entry| Some(&entry.name) == self.selected_name.as_ref())
            .unwrap_or(0);
        self.selected_name = entries.get(self.selected).map(|entry| entry.name.clone());
    }

    fn handle_key(
        &mut self,
        key: KeyCode,
        catalog: Option<&SkillCatalogSnapshot>,
    ) -> DiscoveryAction {
        if key == KeyCode::Char('d') {
            self.diagnostics = !self.diagnostics;
            self.rendered = None;
            return DiscoveryAction::None;
        }
        if self.diagnostics {
            self.report.scroll(key);
            return DiscoveryAction::None;
        }
        let entries = catalog.map_or(&[][..], |catalog| catalog.entries.as_slice());
        if key == KeyCode::Enter {
            if let Some(entry) = entries.get(self.selected).filter(|entry| {
                entry.is_invocable() && self.rendered.as_deref() == Some(&entry.name)
            }) {
                return DiscoveryAction::InsertSkill(format!("/skill:{} ", entry.name));
            }
            return DiscoveryAction::None;
        }
        let next = match key {
            KeyCode::Up => self.selected.saturating_sub(1),
            KeyCode::Down => self.selected.saturating_add(1),
            KeyCode::PageUp => self.selected.saturating_sub(self.page_size.max(1)),
            KeyCode::PageDown => self.selected.saturating_add(self.page_size.max(1)),
            KeyCode::Home => 0,
            KeyCode::End => entries.len().saturating_sub(1),
            _ => return DiscoveryAction::None,
        };
        self.selected = next.min(entries.len().saturating_sub(1));
        self.selected_name = entries.get(self.selected).map(|entry| entry.name.clone());
        self.rendered = None;
        DiscoveryAction::None
    }

    fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        state: &UiState,
        notice: Option<&str>,
        palette: Palette,
    ) -> Rows {
        self.rendered = None;
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(if self.diagnostics {
                " Skill diagnostics "
            } else {
                " Skills "
            })
            .title_bottom(if self.diagnostics {
                " ↑↓ scroll · d skills · r refresh · Esc close "
            } else {
                " Enter insert · d diagnostics · r refresh · Esc close "
            });
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let catalog = state.skills.snapshot.as_deref();
        let mut status = Vec::new();
        if state.skills.loading() {
            status.push("Refreshing skills…".into());
        }
        if let Some(error) = &state.skills.error {
            status.push(format!("Refresh failed: {error}"));
        }
        if let Some(notice) = notice {
            status.push(notice.into());
        }
        if let Some(catalog) = catalog {
            status.push(format!(
                "{} skills · {} diagnostics",
                catalog.entries.len(),
                catalog.diagnostics.len()
            ));
            status.push(format!(
                "Project skills: {}",
                if catalog.project_trusted {
                    "enabled"
                } else {
                    "unavailable (project not trusted)"
                }
            ));
        }
        if self.diagnostics {
            if let Some(catalog) = catalog {
                if catalog.diagnostics.is_empty() {
                    status.push("No discovery diagnostics.".into());
                }
                for diagnostic in &catalog.diagnostics {
                    status.push(format!(
                        "{} · {} · {}",
                        diagnostic.severity.as_str(),
                        diagnostic.source.as_str(),
                        diagnostic.code
                    ));
                    if let Some(path) = &diagnostic.path {
                        status.push(path.clone());
                    }
                    status.push(diagnostic.message.clone());
                    status.push(String::new());
                }
            } else {
                status.push("Skill catalog unavailable.".into());
            }
            self.report.render(frame, inner, &status);
            return Rows::default();
        }
        let [header, list] =
            Layout::vertical([Constraint::Length(status.len() as u16), Constraint::Min(0)])
                .areas(inner);
        frame.render_widget(
            Paragraph::new(
                status
                    .iter()
                    .map(|row| Line::raw(terminal_row(row, usize::from(inner.width))))
                    .collect::<Vec<_>>(),
            ),
            header,
        );
        self.page_size = usize::from(list.height / 2);
        let Some(catalog) = catalog else {
            frame.render_widget(Paragraph::new("Skill catalog unavailable."), list);
            return Rows::default();
        };
        if catalog.entries.is_empty() {
            frame.render_widget(Paragraph::new("No skills discovered."), list);
            return Rows::default();
        }
        // A two-line choice must fit completely before Enter can use its identity.
        if list.height < 2 || list.width < 2 {
            return Rows::default();
        }
        let width = usize::from(list.width.saturating_sub(2));
        let items = catalog.entries.iter().map(|entry| {
            ListItem::new(vec![
                Line::raw(terminal_row(
                    &format!(
                        "/skill:{} [{}]{}",
                        entry.name,
                        entry.source.as_str(),
                        if entry.is_invocable() {
                            ""
                        } else {
                            " (invalid name)"
                        }
                    ),
                    width,
                )),
                Line::raw(terminal_row(&entry.description, width)),
            ])
        });
        let mut selection = ListState::default().with_selected(Some(self.selected));
        frame.render_stateful_widget(
            List::new(items)
                .highlight_style(palette.selection())
                .highlight_symbol("› "),
            list,
            &mut selection,
        );
        self.rendered = catalog
            .entries
            .get(self.selected)
            .filter(|entry| entry.is_invocable())
            .map(|entry| entry.name.clone());
        Rows::new(list, selection.offset(), catalog.entries.len(), 2)
    }
}

/// Scroll fully wrapped report rows so long diagnostics and tool names remain reachable.
#[derive(Default)]
pub(crate) struct ReportView {
    offset: usize,
    rows: usize,
    page_height: usize,
}

impl ReportView {
    pub(crate) fn scroll(&mut self, key: KeyCode) {
        self.offset = match key {
            KeyCode::Up => self.offset.saturating_sub(1),
            KeyCode::Down => self.offset.saturating_add(1),
            KeyCode::PageUp => self.offset.saturating_sub(self.page_height.max(1)),
            KeyCode::PageDown => self.offset.saturating_add(self.page_height.max(1)),
            KeyCode::Home => 0,
            KeyCode::End => self.rows.saturating_sub(self.page_height),
            _ => self.offset,
        }
        .min(self.rows.saturating_sub(self.page_height));
    }

    pub(crate) fn render(&mut self, frame: &mut Frame<'_>, area: Rect, rows: &[String]) {
        if area.width == 0 || area.height == 0 {
            return;
        }
        let mut wrapped = Vec::new();
        for row in rows {
            for physical in sanitize_for_terminal(row).split('\n') {
                let mut line = String::new();
                let mut width = 0;
                for grapheme in physical.graphemes(true) {
                    let next = grapheme.width();
                    if width + next > usize::from(area.width) && !line.is_empty() {
                        wrapped.push(std::mem::take(&mut line));
                        width = 0;
                    }
                    line.push_str(grapheme);
                    width += next;
                }
                wrapped.push(line);
            }
        }
        self.rows = wrapped.len();
        self.page_height = usize::from(area.height);
        self.offset = self.offset.min(self.rows.saturating_sub(self.page_height));
        let text = wrapped
            .into_iter()
            .skip(self.offset)
            .take(self.page_height)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(Paragraph::new(text), area);
    }
}

impl PermissionsView {
    fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        state: &UiState,
        palette: Palette,
    ) -> Rows {
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .padding(Padding::horizontal(1))
            .style(palette.composer())
            .border_style(palette.border())
            .title(" Project permissions ")
            .title_bottom(
                Line::from(" ↑↓ select · Enter apply · Esc close ")
                    .style(Style::default().fg(palette.muted))
                    .right_aligned(),
            );
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let width = usize::from(inner.width);
        let snapshot = state.permissions.snapshot.as_ref();
        let muted = Style::default().fg(palette.muted);
        let mut lines: Vec<Line<'static>> = PERMISSION_CHOICES
            .iter()
            .enumerate()
            .map(|(index, (key, label, mode))| {
                let current = snapshot.is_some_and(|snapshot| snapshot.mode == *mode);
                let suffix = if current { "  current" } else { "" };
                let mut line = crate::ui::choice_line(
                    &key.to_string(),
                    label,
                    self.selected == Some(index),
                    width.saturating_sub(suffix.width()),
                    palette,
                );
                line.spans.push(Span::styled(suffix, muted));
                line
            })
            .collect();
        lines.push(Line::default());
        let status = if let Some(error) = &state.permissions.error {
            Some(Line::styled(
                terminal_row(&format!("Error: {error}"), width),
                Style::default().fg(palette.warning),
            ))
        } else if state.permissions.loading() {
            Some(Line::styled(
                terminal_row(
                    if snapshot.is_some() {
                        "Updating permissions…"
                    } else {
                        "Loading permissions…"
                    },
                    width,
                ),
                muted,
            ))
        } else {
            snapshot
                .filter(|snapshot| snapshot.saved_mode != Some(snapshot.mode))
                .map(|_| {
                    Line::styled(
                        terminal_row("Current mode is not saved; choose one to save it.", width),
                        Style::default().fg(palette.warning),
                    )
                })
        };
        lines.extend(status);
        lines.push(Line::styled(
            terminal_row(
                "Saved for this project. Changing mode clears temporary grants.",
                width,
            ),
            muted,
        ));
        if let Some(project) = snapshot.and_then(|snapshot| snapshot.project_path.as_deref()) {
            lines.push(Line::styled(
                crate::ui::tail_row(&format!("Project: {project}"), width),
                muted,
            ));
        }
        frame.render_widget(Paragraph::new(lines), inner);
        self.painted = inner.height >= 3 && inner.width >= 22 && !state.permissions.loading();
        self.rendered = if self.painted { self.selected } else { None };
        Rows::default()
    }
}

/// Rows the docked `/permissions` panel needs for `state`, borders included.
pub(crate) fn permissions_panel_height(state: &UiState) -> u16 {
    let snapshot = state.permissions.snapshot.as_ref();
    let status = state.permissions.error.is_some()
        || state.permissions.loading()
        || snapshot.is_some_and(|snapshot| snapshot.saved_mode != Some(snapshot.mode));
    let project = snapshot.is_some_and(|snapshot| snapshot.project_path.is_some());
    // Borders, two choices, a gap and the grants note, then the optional rows.
    6 + u16::from(status) + u16::from(project)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};
    use std::sync::Arc;
    use wisp_protocol::events::{McpServerSnapshot, McpServerStatus, McpStatusSnapshot};

    fn draw(view: &mut DiscoveryView, state: &UiState, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| {
                view.render(frame, frame.area(), state, None, Palette::default());
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
    fn skills_and_diagnostics_preserve_selection_and_wrap_long_untrusted_text() {
        let mut state = UiState::unconfigured();
        let mut catalog = crate::commands::tests::skills();
        catalog.project_trusted = false;
        catalog.diagnostics[0].message = format!(
            "\u{1b}[31m{} final-diagnostic",
            "long diagnostic ".repeat(30)
        );
        state.skills.snapshot = Some(Arc::new(catalog));
        let mut view = DiscoveryView::skills(state.skills.snapshot.as_deref());
        let rendered = draw(&mut view, &state, 80, 12);
        assert!(rendered.contains("project not trusted"));
        assert!(rendered.contains("/skill:review"));
        assert!(rendered.contains("project:wisp"));
        view.handle_key(key(KeyCode::Down), &state);
        assert!(matches!(
            view.handle_key(key(KeyCode::Enter), &state),
            DiscoveryAction::None
        ));
        draw(&mut view, &state, 30, 8);
        assert!(
            matches!(view.handle_key(key(KeyCode::Enter), &state), DiscoveryAction::InsertSkill(prefix) if prefix == "/skill:build ")
        );
        view.handle_key(key(KeyCode::Char('d')), &state);
        draw(&mut view, &state, 30, 8);
        view.handle_key(key(KeyCode::End), &state);
        let rendered = draw(&mut view, &state, 30, 8);
        assert!(rendered.contains("Invalid metadata"));
        assert!(!rendered.contains('\u{1b}'));
        view.handle_key(key(KeyCode::Char('d')), &state);
        let mut next = state.skills.snapshot.as_ref().unwrap().as_ref().clone();
        next.entries.reverse();
        state.skills.snapshot = Some(Arc::new(next));
        view.sync_skills(state.skills.snapshot.as_deref());
        assert!(matches!(
            view.handle_key(key(KeyCode::Enter), &state),
            DiscoveryAction::None
        ));
        draw(&mut view, &state, 80, 12);
        assert!(
            matches!(view.handle_key(key(KeyCode::Enter), &state), DiscoveryAction::InsertSkill(prefix) if prefix == "/skill:build ")
        );
    }

    #[test]
    fn mcp_status_shows_all_states_and_keeps_long_tool_names_reachable() {
        let mut state = UiState::unconfigured();
        state.mcp.snapshot = Some(Arc::new(McpStatusSnapshot {
            servers: vec![
                McpServerSnapshot {
                    name: "connected-server".into(),
                    status: McpServerStatus::Connected,
                    tool_names: vec!["find".into()],
                    error: None,
                    launch: None,
                },
                McpServerSnapshot {
                    name: "disconnected-server".into(),
                    status: McpServerStatus::Disconnected,
                    tool_names: vec![format!("{} final-tool", "wide界".repeat(100))],
                    error: None,
                    launch: None,
                },
                McpServerSnapshot {
                    name: "offline".into(),
                    status: McpServerStatus::Unavailable,
                    tool_names: vec![],
                    error: Some("\u{1b}[31mbackend failed".into()),
                    launch: None,
                },
                McpServerSnapshot {
                    name: "paused".into(),
                    status: McpServerStatus::Disabled,
                    tool_names: vec![],
                    error: None,
                    launch: None,
                },
            ],
        }));
        let mut view = DiscoveryView::mcp();
        assert!(draw(&mut view, &state, 80, 24).contains("connected-server  connected · 1 tools"));
        // A long tool name stays reachable by scrolling the selected server's details.
        view.handle_key(key(KeyCode::Down), &state);
        draw(&mut view, &state, 30, 8);
        view.handle_key(key(KeyCode::End), &state);
        assert!(draw(&mut view, &state, 30, 8).contains("final-tool"));
        view.handle_key(key(KeyCode::Down), &state);
        let rendered = draw(&mut view, &state, 30, 8);
        assert!(rendered.contains("unavailable"));
        assert!(rendered.contains("backend failed"));
        assert!(!rendered.contains('\u{1b}'));
        view.handle_key(key(KeyCode::Down), &state);
        assert!(draw(&mut view, &state, 30, 8).contains("paused  disabled"));
        assert!(matches!(
            view.handle_key(key(KeyCode::Char('r')), &state),
            DiscoveryAction::Refresh(UiAction::LoadMcpStatus)
        ));
    }

    #[test]
    fn empty_and_failed_inspection_states_are_explicit() {
        let mut state = UiState::unconfigured();
        state.skills.snapshot = Some(Arc::new(SkillCatalogSnapshot {
            entries: vec![],
            diagnostics: vec![],
            project_trusted: true,
        }));
        state.mcp.snapshot = Some(Arc::new(McpStatusSnapshot { servers: vec![] }));
        state.mcp.error = Some("request failed".into());
        let mut skills = DiscoveryView::skills(state.skills.snapshot.as_deref());
        assert!(draw(&mut skills, &state, 80, 12).contains("No skills discovered."));
        assert!(matches!(
            skills.handle_key(key(KeyCode::Enter), &state),
            DiscoveryAction::None
        ));
        let rendered = draw(&mut DiscoveryView::mcp(), &state, 80, 12);
        assert!(rendered.contains("No MCP servers configured."));
        assert!(rendered.contains("Error: request failed"));
        for (width, height) in [(0, 0), (1, 1), (10, 3)] {
            draw(&mut skills, &state, width, height);
        }
    }
}
