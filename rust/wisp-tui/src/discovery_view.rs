//! Skill selection, MCP server management, and the `/permissions` and `/codemode`
//! pickers. The `/subagents` panel is drawn by `subagents_view`.

use crate::{
    dialog::{self, Dialog, Hint, PickerRow},
    mcp_manager::McpView,
    mouse::Rows,
    reducer::{UiAction, UiState},
    session_picker::terminal_row,
    subagents_view::SubagentsView,
    theme::Palette,
    ui::sanitize_for_terminal,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::Style,
    text::Line,
    widgets::Paragraph,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::commands::{CodemodePresentation, CodemodeSetting, PermissionMode};
use wisp_protocol::events::SkillCatalogSnapshot;

// Returned once per key event and consumed immediately, so its size does not matter;
// boxing `UiAction` would only complicate the nested patterns callers match on.
#[allow(clippy::large_enum_variant)]
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
    Permissions(ChoiceView),
    Codemode(ChoiceView),
    Subagents(SubagentsView),
}

/// A picker of a few saved choices, such as `/permissions`. Like a pending
/// decision, it has no default choice: Enter applies only a choice the user
/// highlighted and that has been painted, because saving a permission mode also
/// clears temporary grants. A choice's number applies it directly.
#[derive(Default)]
pub(crate) struct ChoiceView {
    painted: bool,
    selected: Option<usize>,
    rendered: Option<usize>,
}

impl ChoiceView {
    /// Move the highlight, or return the index of the choice `code` applies once
    /// the view is painted and its data is `ready`.
    fn choose(&mut self, code: KeyCode, count: usize, ready: bool) -> Option<usize> {
        if matches!(code, KeyCode::Up | KeyCode::Down) {
            self.selected = Some(match (self.selected, code) {
                (None, KeyCode::Down) => 0,
                (None, _) => count - 1,
                (Some(index), KeyCode::Down) => (index + 1) % count,
                (Some(index), _) => (index + count - 1) % count,
            });
            return None;
        }
        if !self.painted || !ready {
            return None;
        }
        let choice = match code {
            KeyCode::Enter => self.selected.filter(|index| self.rendered == Some(*index)),
            KeyCode::Char(key) => key
                .to_digit(10)
                .and_then(|digit| usize::try_from(digit).ok()?.checked_sub(1))
                .filter(|index| *index < count),
            _ => None,
        }?;
        self.painted = false;
        self.rendered = None;
        Some(choice)
    }

    fn invalidate(&mut self) {
        self.painted = false;
        self.rendered = None;
    }
}

const PERMISSION_CHOICES: [(char, &str, PermissionMode); 4] = [
    ('1', "Ask before changes", PermissionMode::Ask),
    (
        '2',
        "Auto edits — workspace edits; ask before commands",
        PermissionMode::Auto,
    ),
    (
        '3',
        "Auto commands — edits + macOS sandboxed commands",
        PermissionMode::AutoCommands,
    ),
    (
        '4',
        "YOLO — allow tools without asking",
        PermissionMode::Yolo,
    ),
];

const CODEMODE_CHOICES: [(char, &str, CodemodeSetting); 3] = [
    (
        '1',
        "Automatic — with MCP tools marked read",
        CodemodeSetting::Automatic,
    ),
    (
        '2',
        "Always — every run, with its read-only tools",
        CodemodeSetting::Always,
    ),
    ('3', "Never", CodemodeSetting::Never),
];

const PRESENTATION_CHOICES: [(char, &str, CodemodePresentation); 2] = [
    (
        '4',
        "Direct tools + codemode",
        CodemodePresentation::DirectAndCodemode,
    ),
    ('5', "Codemode only", CodemodePresentation::CodemodeOnly),
];
const CODEMODE_CHOICE_COUNT: usize = CODEMODE_CHOICES.len() + PRESENTATION_CHOICES.len();

impl DiscoveryView {
    // The scheduler preserves this painted choice while draining a finite event prefix.
    pub fn rendered_selection(&self) -> Option<&str> {
        match self {
            Self::Skills(view) => view.rendered.as_deref(),
            Self::Permissions(view) => view
                .rendered
                .and_then(|index| PERMISSION_CHOICES.get(index).map(|choice| choice.1)),
            Self::Codemode(view) => view.rendered.map(|index| {
                if index < CODEMODE_CHOICES.len() {
                    CODEMODE_CHOICES[index].1
                } else {
                    PRESENTATION_CHOICES[index - CODEMODE_CHOICES.len()].1
                }
            }),
            Self::Subagents(view) => view.rendered_selection(),
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
        Self::Permissions(ChoiceView::default())
    }

    pub fn codemode() -> Self {
        Self::Codemode(ChoiceView::default())
    }

    pub fn subagents() -> Self {
        Self::Subagents(SubagentsView::default())
    }

    pub fn key_activates(&self, key: KeyEvent) -> bool {
        if let Self::Mcp(view) = self {
            return view.key_activates(key);
        }
        key.code == KeyCode::Enter
            || (matches!(self, Self::Permissions(_))
                && matches!(
                    key.code,
                    KeyCode::Char('1'..='4') | KeyCode::Char('d') | KeyCode::Backspace
                ))
            || (matches!(self, Self::Codemode(_)) && matches!(key.code, KeyCode::Char('1'..='5')))
    }

    pub fn invalidate_selection(&mut self) {
        if let Self::Mcp(view) = self {
            view.invalidate();
        }
        if let Self::Permissions(view) | Self::Codemode(view) = self {
            view.invalidate();
        }
        if let Self::Subagents(view) = self {
            view.invalidate();
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
        if key.code == KeyCode::Esc {
            // Esc first leaves the subagents panel's model list or instructions.
            if let Self::Subagents(view) = self {
                if view.back() {
                    return DiscoveryAction::None;
                }
            }
            return DiscoveryAction::Close;
        }
        if key.code == KeyCode::Char('c') && key.modifiers == KeyModifiers::CONTROL {
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
                Self::Codemode(_) => UiAction::Codemode(None),
                Self::Subagents(_) => UiAction::Subagents(None),
            });
        }
        match self {
            Self::Permissions(view) => {
                let ready = !state.permissions.loading() && state.permissions.snapshot.is_some();
                let rules: &[String] = state
                    .permissions
                    .snapshot
                    .as_deref()
                    .map_or(&[], |snapshot| snapshot.command_allow_rules.as_slice());
                let total = PERMISSION_CHOICES.len() + rules.len();
                if matches!(key.code, KeyCode::Char('d') | KeyCode::Backspace) {
                    if ready
                        && view.painted
                        && let Some(index) = view.selected
                        && let Some(rule) = index
                            .checked_sub(PERMISSION_CHOICES.len())
                            .and_then(|i| rules.get(i))
                    {
                        let prefix = rule.clone();
                        view.invalidate();
                        return DiscoveryAction::Refresh(UiAction::RevokeCommandRule(prefix));
                    }
                    return DiscoveryAction::None;
                }
                if let KeyCode::Char(digit @ '1'..='4') = key.code {
                    let index = usize::from(digit as u8 - b'1');
                    if ready {
                        view.invalidate();
                        return DiscoveryAction::Refresh(UiAction::Permissions(Some(
                            PERMISSION_CHOICES[index].2,
                        )));
                    }
                    return DiscoveryAction::None;
                }
                view.choose(key.code, total, ready)
                    .map_or(DiscoveryAction::None, |choice| {
                        if let Some(mode) = PERMISSION_CHOICES.get(choice) {
                            DiscoveryAction::Refresh(UiAction::Permissions(Some(mode.2)))
                        } else if let Some(rule) = rules.get(choice - PERMISSION_CHOICES.len()) {
                            DiscoveryAction::Refresh(UiAction::RevokeCommandRule(rule.clone()))
                        } else {
                            DiscoveryAction::None
                        }
                    })
            }
            Self::Codemode(view) => {
                let ready = !state.codemode.loading() && state.codemode.snapshot.is_some();
                view.choose(key.code, CODEMODE_CHOICE_COUNT, ready).map_or(
                    DiscoveryAction::None,
                    |choice| {
                        let action = if choice < CODEMODE_CHOICES.len() {
                            UiAction::Codemode(Some(CODEMODE_CHOICES[choice].2))
                        } else {
                            UiAction::CodemodePresentation(
                                PRESENTATION_CHOICES[choice - CODEMODE_CHOICES.len()].2,
                            )
                        };
                        DiscoveryAction::Refresh(action)
                    },
                )
            }
            Self::Subagents(view) => view.handle_key(key.code, state),
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
    ) -> dialog::Rendered {
        match self {
            Self::Permissions(view) => dialog::Rendered {
                rows: Rows::default(),
                controls: render_permissions(view, frame, area, state, palette),
            },
            Self::Codemode(view) => dialog::Rendered {
                rows: Rows::default(),
                controls: render_codemode(view, frame, area, state, palette),
            },
            Self::Subagents(view) => dialog::Rendered {
                rows: Rows::default(),
                controls: view.render(frame, area, state, palette),
            },
            Self::Skills(view) => view.render(frame, area, state, notice, palette),
            Self::Mcp(view) => dialog::Rendered {
                rows: Rows::default(),
                controls: view.render(frame, area, state, palette),
            },
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
    ) -> dialog::Rendered {
        self.rendered = None;
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let hints = if self.diagnostics {
            vec![
                Hint {
                    label: "d",
                    description: "skills",
                    key: key(KeyCode::Char('d')),
                },
                Hint {
                    label: "r",
                    description: "refresh",
                    key: key(KeyCode::Char('r')),
                },
                Hint {
                    label: "Esc",
                    description: "close",
                    key: key(KeyCode::Esc),
                },
                Hint {
                    label: "↑",
                    description: "up",
                    key: key(KeyCode::Up),
                },
                Hint {
                    label: "↓",
                    description: "down",
                    key: key(KeyCode::Down),
                },
            ]
        } else {
            vec![
                Hint {
                    label: "Enter",
                    description: "insert",
                    key: key(KeyCode::Enter),
                },
                Hint {
                    label: "d",
                    description: "diagnostics",
                    key: key(KeyCode::Char('d')),
                },
                Hint {
                    label: "r",
                    description: "refresh",
                    key: key(KeyCode::Char('r')),
                },
                Hint {
                    label: "Esc",
                    description: "close",
                    key: key(KeyCode::Esc),
                },
            ]
        };
        let layout = Dialog {
            title: if self.diagnostics {
                "Skill diagnostics"
            } else {
                "Skills"
            },
            tabs: &[],
            filter: None,
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
        let text_area = Rect::new(
            inner.x + 1,
            inner.y,
            inner.width.saturating_sub(2),
            inner.height,
        );
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
            self.report.render(frame, text_area, &status);
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        let header_rows = u16::try_from(status.len())
            .unwrap_or(u16::MAX)
            .min(inner.height);
        let [header, list] =
            Layout::vertical([Constraint::Length(header_rows), Constraint::Min(0)]).areas(inner);
        let header_inner = Rect::new(
            header.x + 1,
            header.y,
            header.width.saturating_sub(2),
            header.height,
        );
        frame.render_widget(
            Paragraph::new(
                status
                    .iter()
                    .map(|row| {
                        Line::styled(
                            terminal_row(row, usize::from(header_inner.width)),
                            Style::default().fg(palette.muted),
                        )
                    })
                    .collect::<Vec<_>>(),
            ),
            header_inner,
        );
        self.page_size = usize::from(list.height / 2);
        let list_text_area = Rect::new(
            list.x + 1,
            list.y,
            list.width.saturating_sub(2),
            list.height,
        );
        let Some(catalog) = catalog else {
            frame.render_widget(Paragraph::new("Skill catalog unavailable."), list_text_area);
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        };
        if catalog.entries.is_empty() {
            frame.render_widget(Paragraph::new("No skills discovered."), list_text_area);
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        // A two-line choice must fit completely before Enter can use its identity.
        if list.height < 2 || list.width < 3 {
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        let visible_items = usize::from(list.height / 2);
        let offset = self
            .selected
            .saturating_sub(visible_items.saturating_sub(1));
        for (visible_index, (index, entry)) in catalog
            .entries
            .iter()
            .enumerate()
            .skip(offset)
            .take(visible_items)
            .enumerate()
        {
            let label = format!("/skill:{}", entry.name);
            let tag = format!(
                "[{}]{}",
                entry.source.as_str(),
                if entry.is_invocable() {
                    ""
                } else {
                    " (invalid name)"
                }
            );
            let row = Rect::new(list.x, list.y + (visible_index * 2) as u16, list.width, 2);
            PickerRow {
                label: &label,
                description: Some(&entry.description),
                tag: Some(&tag),
                selected: index == self.selected,
                muted: !entry.is_invocable(),
            }
            .render(frame, row, palette);
        }
        self.rendered = catalog
            .entries
            .get(self.selected)
            .filter(|entry| entry.is_invocable())
            .map(|entry| entry.name.clone());
        dialog::Rendered {
            rows: Rows::new(list, offset, catalog.entries.len(), 2),
            controls: layout.controls,
        }
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

fn choice_panel_hints(width: u16) -> Vec<Hint<'static>> {
    let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
    let mut hints = vec![
        Hint {
            label: "Enter",
            description: "apply",
            key: key(KeyCode::Enter),
        },
        Hint {
            label: "Esc",
            description: "close",
            key: key(KeyCode::Esc),
        },
    ];
    if width >= 50 {
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
    hints
}

fn render_permissions(
    view: &mut ChoiceView,
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    palette: Palette,
) -> dialog::Controls {
    let snapshot = state.permissions.snapshot.as_ref();
    let rules: &[String] = snapshot.map_or(&[], |s| s.command_allow_rules.as_slice());
    let total = PERMISSION_CHOICES.len() + rules.len();
    if view.selected.is_some_and(|index| index >= total) {
        view.selected = total.checked_sub(1);
    }
    let mut hints = choice_panel_hints(area.width);
    if !rules.is_empty() && area.width >= 56 {
        let insert_at = hints.len().saturating_sub(1);
        hints.insert(
            insert_at,
            Hint {
                label: "d",
                description: "revoke rule",
                key: KeyEvent::new(KeyCode::Char('d'), KeyModifiers::NONE),
            },
        );
    }
    let layout = Dialog {
        title: "Project permissions",
        tabs: &[],
        filter: None,
        hints: &hints,
    }
    .render(frame, area, palette);
    let body = layout.body;
    let inner = Rect::new(
        body.x.saturating_add(1),
        body.y,
        body.width.saturating_sub(2),
        body.height,
    );
    let width = usize::from(inner.width);
    let muted = Style::default().fg(palette.muted);
    let mut lines = choice_lines(
        &PERMISSION_CHOICES,
        view.selected,
        snapshot.map(|snapshot| snapshot.mode),
        width,
        palette,
    );
    if !rules.is_empty() {
        lines.push(Line::default());
        lines.push(Line::styled(
            terminal_row("Saved command rules (this project)", width),
            Style::default()
                .fg(palette.foreground)
                .add_modifier(ratatui::style::Modifier::BOLD),
        ));
        for (offset, rule) in rules.iter().enumerate() {
            let index = PERMISSION_CHOICES.len() + offset;
            let selected = view.selected == Some(index);
            let suffix = selected.then_some("  d/Enter revoke");
            lines.push(crate::ui::choice_line_with_suffix(
                "•", rule, suffix, selected, width, palette,
            ));
        }
    }
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
            .map(|snapshot| {
                let text = if snapshot.session_only_yolo && snapshot.mode != PermissionMode::Ask {
                    "This mode lasts for this session; it is not saved here."
                } else {
                    "Current mode is not saved; choose one to save it."
                };
                Line::styled(
                    terminal_row(text, width),
                    Style::default().fg(palette.warning),
                )
            })
    };
    lines.extend(status);
    let scope = if snapshot.is_some_and(|snapshot| snapshot.session_only_yolo) {
        "Elevated modes are not saved here. Changing mode clears grants."
    } else {
        "Saved for this project. Changing mode clears temporary grants."
    };
    lines.push(Line::styled(terminal_row(scope, width), muted));
    if let Some(project) = snapshot.and_then(|snapshot| snapshot.project_path.as_deref()) {
        lines.push(Line::styled(
            crate::ui::tail_row(&format!("Project: {project}"), width),
            muted,
        ));
    }
    frame.render_widget(Paragraph::new(lines), inner);
    view.painted = inner.height >= 4 && inner.width >= 22 && !state.permissions.loading();
    view.rendered = if view.painted { view.selected } else { None };
    layout.controls
}

/// One row per choice, the highlighted one marked and the one in effect labelled.
fn choice_lines<T: PartialEq>(
    choices: &[(char, &str, T)],
    selected: Option<usize>,
    current: Option<T>,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    choices
        .iter()
        .enumerate()
        .map(|(index, (key, label, value))| {
            let suffix = (current.as_ref() == Some(value)).then_some("  current");
            crate::ui::choice_line_with_suffix(
                &key.to_string(),
                label,
                suffix,
                selected == Some(index),
                width,
                palette,
            )
        })
        .collect()
}

fn render_codemode(
    view: &mut ChoiceView,
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    palette: Palette,
) -> dialog::Controls {
    let hints = choice_panel_hints(area.width);
    let layout = Dialog {
        title: "Codemode",
        tabs: &[],
        filter: None,
        hints: &hints,
    }
    .render(frame, area, palette);
    let body = layout.body;
    let inner = Rect::new(
        body.x.saturating_add(1),
        body.y,
        body.width.saturating_sub(2),
        body.height,
    );
    let width = usize::from(inner.width);
    let snapshot = state.codemode.snapshot.as_deref();
    let muted = Style::default().fg(palette.muted);
    let warning = Style::default().fg(palette.warning);
    let mut lines = vec![Line::styled("Availability", muted)];
    lines.extend(choice_lines(
        &CODEMODE_CHOICES,
        view.selected,
        snapshot.map(|snapshot| snapshot.setting),
        width,
        palette,
    ));
    lines.push(Line::styled("Presentation", muted));
    lines.extend(choice_lines(
        &PRESENTATION_CHOICES,
        view.selected
            .and_then(|index| index.checked_sub(CODEMODE_CHOICES.len())),
        snapshot.map(|snapshot| snapshot.presentation),
        width,
        palette,
    ));
    lines.push(Line::default());
    for (text, style) in codemode_notes(state) {
        lines.push(Line::styled(
            terminal_row(&text, width),
            if style { warning } else { muted },
        ));
    }
    frame.render_widget(Paragraph::new(lines), inner);
    view.painted = inner.height >= 7 && inner.width >= 22 && !state.codemode.loading();
    view.rendered = if view.painted { view.selected } else { None };
    layout.controls
}

/// The `/codemode` picker's rows below its choices, each flagged when it warns.
fn codemode_notes(state: &UiState) -> Vec<(String, bool)> {
    let mut notes = Vec::new();
    let snapshot = state.codemode.snapshot.as_deref();
    if let Some(error) = &state.codemode.error {
        notes.push((format!("Error: {error}"), true));
    } else if state.codemode.loading() {
        let text = if snapshot.is_some() {
            "Updating codemode…"
        } else {
            "Loading codemode…"
        };
        notes.push((text.to_owned(), false));
    }
    if let Some(snapshot) = snapshot {
        if !snapshot.available {
            notes.push((
                "Needs the codemode extra: pip install 'wisp-ai[codemode]'".to_owned(),
                true,
            ));
        } else {
            let offered = if snapshot.offered { "On" } else { "Off" };
            notes.push((format!("{offered} for the next prompt."), false));
            notes.extend(scriptable_mcp_note(snapshot));
        }
        if snapshot.effective_presentation == CodemodePresentation::CodemodeOnly {
            notes.push(("Scripts cannot return images.".to_owned(), false));
            notes.push(("Scripts cannot spawn subagents.".to_owned(), false));
        }
        if snapshot.presentation != snapshot.effective_presentation {
            notes.push((
                "Using direct tools: codemode-only is unavailable.".to_owned(),
                true,
            ));
        }
        if let Some(env) = snapshot.env_setting {
            notes.push((
                format!(
                    "WISP_CODEMODE sets {} when Wisp starts.",
                    codemode_label(env)
                ),
                true,
            ));
        }
    }
    notes.push((
        "Saved for you. Applies from the next prompt.".to_owned(),
        false,
    ));
    notes
}

/// How many of the next run's MCP tools a script may call, flagged when it is none.
/// Selection, policy, plan mode, and callable schemas determine eligibility.
/// Counts come first so they remain visible in a narrow panel.
fn scriptable_mcp_note(snapshot: &wisp_protocol::events::CodemodeState) -> Option<(String, bool)> {
    let (scriptable, total) = (snapshot.scriptable_mcp_tools, snapshot.mcp_tools);
    if total == 0 || snapshot.setting == CodemodeSetting::Never {
        return None;
    }
    let tools = if total == 1 { "tool" } else { "tools" };
    if scriptable >= total {
        return Some((format!("Scripts can call all {total} MCP {tools}."), false));
    }
    Some((
        format!("Scripts can call {scriptable} of {total} MCP {tools} under normal tool policy."),
        scriptable == 0,
    ))
}

/// The setting's name as the side panel and picker notes show it.
pub(crate) fn codemode_label(setting: CodemodeSetting) -> &'static str {
    match setting {
        CodemodeSetting::Automatic => "automatic",
        CodemodeSetting::Always => "always",
        CodemodeSetting::Never => "never",
    }
}

/// Rows the docked `/codemode` panel needs for `state`, borders and footer included.
pub(crate) fn codemode_panel_height(state: &UiState) -> u16 {
    // Borders, footer, two headings, five choices and a gap, then the notes.
    let notes = u16::try_from(codemode_notes(state).len()).unwrap_or(u16::MAX);
    11_u16.saturating_add(notes)
}

/// Rows the docked `/permissions` panel needs for `state`, borders and footer included.
pub(crate) fn permissions_panel_height(state: &UiState) -> u16 {
    let snapshot = state.permissions.snapshot.as_ref();
    let status = state.permissions.error.is_some()
        || state.permissions.loading()
        || snapshot.is_some_and(|snapshot| snapshot.saved_mode != Some(snapshot.mode));
    let project = snapshot.is_some_and(|snapshot| snapshot.project_path.is_some());
    let rules = snapshot.map_or(0, |snapshot| snapshot.command_allow_rules.len());
    let rule_rows = if rules > 0 {
        u16::try_from(rules.min(8) + 2).unwrap_or(10)
    } else {
        0
    };
    // Borders, footer, four choices, a gap and the grants note, then optional rows.
    9 + u16::from(status) + u16::from(project) + rule_rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};
    use std::sync::Arc;
    use wisp_protocol::events::{McpServerSnapshot, McpServerStatus, McpStatusSnapshot};

    fn draw(view: &mut DiscoveryView, state: &UiState, width: u16, height: u16) -> String {
        draw_with_palette(view, state, width, height, Palette::default())
    }

    fn draw_with_palette(
        view: &mut DiscoveryView,
        state: &UiState,
        width: u16,
        height: u16,
        palette: Palette,
    ) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| {
                view.render(frame, frame.area(), state, None, palette);
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
    fn codemode_picker_applies_a_number() {
        let mut state = UiState::unconfigured();
        let mut view = DiscoveryView::codemode();
        // Nothing applies before the setting has loaded and been painted.
        assert!(matches!(
            view.handle_key(key(KeyCode::Char('2')), &state),
            DiscoveryAction::None
        ));
        state.codemode.snapshot = Some(Arc::new(wisp_protocol::events::CodemodeState {
            setting: CodemodeSetting::Automatic,
            presentation: Default::default(),
            effective_presentation: Default::default(),
            available: false,
            offered: false,
            env_setting: Some(CodemodeSetting::Never),
            mcp_tools: 12,
            scriptable_mcp_tools: 0,
        }));
        let rendered = draw(&mut view, &state, 72, codemode_panel_height(&state));
        assert!(rendered.contains("current"));
        assert!(rendered.contains("pip install 'wisp-ai[codemode]'"));
        // Without the extra no script runs, so what one could call is not the news.
        assert!(!rendered.contains("Scripts can call"));
        assert!(rendered.contains("WISP_CODEMODE sets never when Wisp starts."));
        assert!(rendered.contains("Applies from the next prompt."));
        assert!(matches!(
            view.handle_key(key(KeyCode::Char('2')), &state),
            DiscoveryAction::Refresh(UiAction::Codemode(Some(CodemodeSetting::Always)))
        ));
        // A number past the choices does nothing.
        draw(&mut view, &state, 72, codemode_panel_height(&state));
        assert!(matches!(
            view.handle_key(key(KeyCode::Char('6')), &state),
            DiscoveryAction::None
        ));
    }

    #[test]
    fn presentation_picker_shows_fallback() {
        for theme in [crate::theme::default_theme(), crate::theme::paper_theme()] {
            for width in [39, 144] {
                let mut state = UiState::unconfigured();
                state.codemode.snapshot = Some(Arc::new(wisp_protocol::events::CodemodeState {
                    setting: CodemodeSetting::Never,
                    presentation: CodemodePresentation::CodemodeOnly,
                    effective_presentation: CodemodePresentation::DirectAndCodemode,
                    available: true,
                    offered: false,
                    env_setting: None,
                    mcp_tools: 0,
                    scriptable_mcp_tools: 0,
                }));
                let height = codemode_panel_height(&state);
                let mut view = DiscoveryView::codemode();
                let rendered =
                    draw_with_palette(&mut view, &state, width, height, theme.palette(false));
                for expected in [
                    "Availability",
                    "Presentation",
                    "Codemode only  current",
                    "Using direct tools",
                ] {
                    assert!(
                        rendered.contains(expected),
                        "{} {width}: {expected}",
                        theme.name
                    );
                }
                assert!(matches!(
                    view.handle_key(key(KeyCode::Char('4')), &state),
                    DiscoveryAction::Refresh(UiAction::CodemodePresentation(
                        CodemodePresentation::DirectAndCodemode
                    ))
                ));
                draw_with_palette(&mut view, &state, width, height, theme.palette(false));
                assert!(matches!(
                    view.handle_key(key(KeyCode::Char('5')), &state),
                    DiscoveryAction::Refresh(UiAction::CodemodePresentation(
                        CodemodePresentation::CodemodeOnly
                    ))
                ));
                // A clipped panel must not apply an unseen choice.
                draw_with_palette(&mut view, &state, width, 8, theme.palette(false));
                assert!(matches!(
                    view.handle_key(key(KeyCode::Char('5')), &state),
                    DiscoveryAction::None
                ));
            }
        }
    }

    #[test]
    fn presentation_limits_follow_effective_choice() {
        for (offered, active) in [(false, false), (true, true), (true, false)] {
            let mut state = UiState::unconfigured();
            state.codemode.snapshot = Some(Arc::new(wisp_protocol::events::CodemodeState {
                setting: CodemodeSetting::Always,
                presentation: CodemodePresentation::CodemodeOnly,
                effective_presentation: if active {
                    CodemodePresentation::CodemodeOnly
                } else {
                    CodemodePresentation::DirectAndCodemode
                },
                available: offered,
                offered,
                env_setting: None,
                mcp_tools: 0,
                scriptable_mcp_tools: 0,
            }));
            let rendered = draw(
                &mut DiscoveryView::codemode(),
                &state,
                39,
                codemode_panel_height(&state),
            );
            assert_eq!(rendered.contains("Scripts cannot return images."), active);
            assert_eq!(rendered.contains("Scripts cannot spawn subagents."), active);
            assert_eq!(rendered.contains("Using direct tools"), !active);
        }
    }

    #[test]
    fn codemode_picker_counts_scriptable_mcp_tools() {
        let cases = [
            // No MCP tools, or codemode never offered: nothing to say.
            (CodemodeSetting::Automatic, 0, 0, None),
            (CodemodeSetting::Never, 0, 12, None),
            (
                CodemodeSetting::Automatic,
                0,
                12,
                Some((
                    "Scripts can call 0 of 12 MCP tools under normal tool policy.",
                    true,
                )),
            ),
            (
                CodemodeSetting::Always,
                5,
                102,
                Some((
                    "Scripts can call 5 of 102 MCP tools under normal tool policy.",
                    false,
                )),
            ),
            (
                CodemodeSetting::Automatic,
                1,
                1,
                Some(("Scripts can call all 1 MCP tool.", false)),
            ),
        ];
        for (setting, scriptable_mcp_tools, mcp_tools, expected) in cases {
            let mut state = UiState::unconfigured();
            state.codemode.snapshot = Some(Arc::new(wisp_protocol::events::CodemodeState {
                setting,
                presentation: Default::default(),
                effective_presentation: Default::default(),
                available: true,
                offered: scriptable_mcp_tools > 0,
                env_setting: None,
                mcp_tools,
                scriptable_mcp_tools,
            }));
            let notes = codemode_notes(&state);
            let note = notes
                .iter()
                .find(|(text, _)| text.starts_with("Scripts can call"))
                .map(|(text, warns)| (text.as_str(), *warns));
            assert_eq!(
                note, expected,
                "{setting:?} {scriptable_mcp_tools}/{mcp_tools}"
            );
            // The docked panel grows by the note's row, and at 72 columns the whole
            // note fits, a three-digit count included.
            let rendered = draw(
                &mut DiscoveryView::codemode(),
                &state,
                72,
                codemode_panel_height(&state),
            );
            if let Some((text, _)) = expected {
                assert!(rendered.contains(text), "{text}");
            } else {
                assert!(!rendered.contains("Scripts can call"));
            }
        }
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
                    read_only_tool_names: Vec::new(),
                    read_only_hint_tool_names: Vec::new(),
                    error: None,
                    launch: None,
                    sign_in: None,
                },
                McpServerSnapshot {
                    name: "disconnected-server".into(),
                    status: McpServerStatus::Disconnected,
                    tool_names: vec![format!("{} final-tool", "wide界".repeat(100))],
                    read_only_tool_names: Vec::new(),
                    read_only_hint_tool_names: Vec::new(),
                    error: None,
                    launch: None,
                    sign_in: None,
                },
                McpServerSnapshot {
                    name: "offline".into(),
                    status: McpServerStatus::Unavailable,
                    tool_names: vec![],
                    read_only_tool_names: Vec::new(),
                    read_only_hint_tool_names: Vec::new(),
                    error: Some("\u{1b}[31mbackend failed".into()),
                    launch: None,
                    sign_in: None,
                },
                McpServerSnapshot {
                    name: "paused".into(),
                    status: McpServerStatus::Disabled,
                    tool_names: vec![],
                    read_only_tool_names: Vec::new(),
                    read_only_hint_tool_names: Vec::new(),
                    error: None,
                    launch: None,
                    sign_in: None,
                },
            ],
        }));
        let mut view = DiscoveryView::mcp();
        assert!(draw(&mut view, &state, 80, 24).contains("connected-server  connected · 1 tools"));
        // A long tool name stays reachable by scrolling the selected server's details.
        view.handle_key(key(KeyCode::Down), &state);
        draw(&mut view, &state, 30, 8);
        view.handle_key(key(KeyCode::End), &state);
        // The name wraps after the read-only mark's column, so its end spans two rows.
        let end = draw(&mut view, &state, 30, 8);
        assert!(end.contains("final-t│") && end.contains("│ool "), "{end}");
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

    #[test]
    fn permissions_view_lists_and_revokes_saved_command_rules() {
        let mut state = UiState::new("fake".into(), None, None);
        state.permissions.snapshot = Some(Arc::new(wisp_protocol::events::PermissionState {
            mode: wisp_protocol::commands::PermissionMode::Ask,
            saved_mode: Some(wisp_protocol::commands::PermissionMode::Ask),
            project_path: Some("/projects/wisp".into()),
            session_only_yolo: false,
            command_allow_rules: vec!["cargo test".into(), "uv run pytest".into()],
        }));
        let mut view = DiscoveryView::permissions();
        let text = draw(&mut view, &state, 80, 22);
        assert!(
            text.contains("Saved command rules (this project)"),
            "{text}"
        );
        assert!(text.contains("cargo test"), "{text}");
        assert!(text.contains("uv run pytest"), "{text}");

        // Move past the 4 modes onto the first saved rule (`cargo test`), then press `d`.
        for _ in 0..5 {
            assert!(matches!(
                view.handle_key(key(KeyCode::Down), &state),
                DiscoveryAction::None
            ));
        }
        let _ = draw(&mut view, &state, 80, 22);
        let action = view.handle_key(key(KeyCode::Char('d')), &state);
        assert!(matches!(
            action,
            DiscoveryAction::Refresh(UiAction::RevokeCommandRule(ref prefix))
                if prefix == "cargo test"
        ));
    }
}
