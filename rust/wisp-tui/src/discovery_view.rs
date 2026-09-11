//! Skill selection and read-only MCP inspection over backend snapshots.

use crate::{
    reducer::{UiAction, UiState},
    session_picker::terminal_row,
    theme::Palette,
    ui::sanitize_for_terminal,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    text::Line,
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::SkillCatalogSnapshot;

pub(crate) enum DiscoveryAction {
    None,
    Close,
    Refresh(UiAction),
    InsertSkill(String),
}

pub(crate) enum DiscoveryView {
    Skills(SkillsView),
    Mcp(ReportView),
}

impl DiscoveryView {
    pub fn skills(catalog: Option<&SkillCatalogSnapshot>) -> Self {
        let mut view = SkillsView::default();
        view.sync(catalog);
        Self::Skills(view)
    }

    pub fn mcp() -> Self {
        Self::Mcp(ReportView::default())
    }

    pub fn invalidate_selection(&mut self) {
        if let Self::Skills(view) = self {
            view.rendered = None;
        }
    }

    pub fn sync_skills(&mut self, catalog: Option<&SkillCatalogSnapshot>) {
        if let Self::Skills(view) = self {
            view.sync(catalog);
        }
    }

    pub fn handle_key(&mut self, key: KeyEvent, state: &UiState) -> DiscoveryAction {
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
                Self::Mcp(_) => UiAction::LoadMcpStatus,
            });
        }
        match self {
            Self::Skills(view) => view.handle_key(key.code, state.skills.snapshot.as_deref()),
            Self::Mcp(view) => {
                view.scroll(key.code);
                DiscoveryAction::None
            }
        }
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        state: &UiState,
        notice: Option<&str>,
        palette: Palette,
    ) {
        match self {
            Self::Skills(view) => view.render(frame, area, state, notice, palette),
            Self::Mcp(view) => {
                let block = Block::default()
                    .borders(Borders::ALL)
                    .border_style(palette.border())
                    .title(" MCP servers ")
                    .title_bottom(" ↑↓ PgUp/PgDn · r refresh · Esc close ");
                let inner = block.inner(area);
                frame.render_widget(block, area);
                let mut rows = Vec::new();
                if state.mcp.loading() {
                    rows.push("Refreshing MCP status…".into());
                }
                if let Some(error) = &state.mcp.error {
                    rows.push(format!("Refresh failed: {error}"));
                }
                if let Some(status) = &state.mcp.snapshot {
                    rows.push("Status at last refresh (r to update).".into());
                    if status.servers.is_empty() {
                        rows.push("No MCP servers configured.".into());
                    }
                    for server in &status.servers {
                        rows.push(format!(
                            "{}: {} ({} registered tools)",
                            server.name,
                            server.status.as_str(),
                            server.tool_names.len()
                        ));
                        if let Some(error) = &server.error {
                            rows.push(error.clone());
                        }
                        rows.extend(server.tool_names.iter().map(|name| format!("  {name}")));
                        rows.push(String::new());
                    }
                } else {
                    rows.push("MCP status unavailable.".into());
                }
                view.render(frame, inner, &rows);
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
    ) {
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
            return;
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
            return;
        };
        if catalog.entries.is_empty() {
            frame.render_widget(Paragraph::new("No skills discovered."), list);
            return;
        }
        // A two-line choice must fit completely before Enter can use its identity.
        if list.height < 2 || list.width < 2 {
            return;
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
    fn scroll(&mut self, key: KeyCode) {
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

    fn render(&mut self, frame: &mut Frame<'_>, area: Rect, rows: &[String]) {
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

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};
    use std::sync::Arc;
    use wisp_protocol::events::{McpServerSnapshot, McpServerStatus, McpStatusSnapshot};

    fn draw(view: &mut DiscoveryView, state: &UiState, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), state, None, Palette::default()))
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
                },
                McpServerSnapshot {
                    name: "disconnected-server".into(),
                    status: McpServerStatus::Disconnected,
                    tool_names: vec![format!("{} final-tool", "wide界".repeat(100))],
                    error: None,
                },
                McpServerSnapshot {
                    name: "offline".into(),
                    status: McpServerStatus::Unavailable,
                    tool_names: vec![],
                    error: Some("\u{1b}[31mbackend failed".into()),
                },
            ],
        }));
        let mut view = DiscoveryView::mcp();
        assert!(draw(&mut view, &state, 80, 24).contains("connected (1 registered tools)"));
        draw(&mut view, &state, 30, 8);
        view.handle_key(key(KeyCode::End), &state);
        let rendered = draw(&mut view, &state, 30, 8);
        assert!(rendered.contains("unavailable"));
        assert!(rendered.contains("backend failed"));
        assert!(!rendered.contains('\u{1b}'));
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
        assert!(rendered.contains("Refresh failed: request failed"));
        for (width, height) in [(0, 0), (1, 1), (10, 3)] {
            draw(&mut skills, &state, width, height);
        }
    }
}
