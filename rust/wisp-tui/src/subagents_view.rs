//! The `/subagents` panel: the subagents settings, and what each kind of subagent
//! would get in the next run.
//!
//! The panel has two setting rows (on or off, and the model fresh subagents use)
//! and one row for each way a subagent can start. Below them it describes the
//! highlighted kind: its model, effort, tools, limits and instructions. The model
//! row opens a list of the session provider's models; a kind row opens its full
//! instructions. Both return to the panel on Esc.

use crate::{
    discovery_view::{DiscoveryAction, ReportView},
    reducer::{UiAction, UiState},
    session_picker::terminal_row,
    theme::Palette,
};
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Padding, Paragraph},
};
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{SubagentKind, SubagentsState};

/// The panel's rows, top to bottom: two settings, then the kinds in the order the
/// backend reports them.
const ROWS: [&str; 4] = ["Subagents", "Model", "Fresh", "Fork"];
const MODEL_ROW: usize = 1;
const FIRST_KIND_ROW: usize = 2;
const LABEL_WIDTH: usize = 11;
/// Models listed at once; a longer list scrolls with the highlight.
const MODELS_SHOWN: usize = 8;
/// Rows of instructions shown at once.
const INSTRUCTION_ROWS: u16 = 10;
/// Tool names written out before the rest are counted.
const TOOLS_NAMED: usize = 6;
const SAME_AS_SESSION: &str = "Same as the session";

#[derive(Default)]
enum Page {
    #[default]
    Panel,
    Models {
        selected: usize,
    },
    Instructions {
        report: ReportView,
    },
}

/// Like the other setting pickers, Enter applies only a row the user highlighted
/// and that has been painted, so a key typed ahead of a redraw changes nothing.
#[derive(Default)]
pub(crate) struct SubagentsView {
    page: Page,
    selected: Option<usize>,
    /// The kind the detail describes: the last kind row highlighted, fresh at first.
    kind: usize,
    painted: bool,
    rendered: Option<String>,
    /// Whether the painted panel showed subagents on, so that Enter turns the
    /// setting to the opposite of what was on screen, not of a later report.
    rendered_enabled: Option<bool>,
}

impl SubagentsView {
    /// The label of the painted, highlighted row, which Enter would act on.
    pub(crate) fn rendered_selection(&self) -> Option<&str> {
        self.rendered.as_deref()
    }

    pub(crate) fn invalidate(&mut self) {
        self.painted = false;
        self.rendered = None;
        self.rendered_enabled = None;
    }

    /// Leave the model list or the instructions for the panel. Returns whether
    /// there was a page to leave; from the panel itself Esc closes the view.
    pub(crate) fn back(&mut self) -> bool {
        if matches!(self.page, Page::Panel) {
            return false;
        }
        self.page = Page::Panel;
        self.invalidate();
        true
    }

    pub(crate) fn handle_key(&mut self, code: KeyCode, state: &UiState) -> DiscoveryAction {
        match &mut self.page {
            Page::Instructions { report } => {
                report.scroll(code);
                DiscoveryAction::None
            }
            Page::Models { selected } => {
                let choices = model_choices(state);
                match code {
                    KeyCode::Down => *selected = (*selected + 1) % choices.len(),
                    KeyCode::Up => *selected = (*selected + choices.len() - 1) % choices.len(),
                    KeyCode::Enter => {
                        let choice = choices.get(*selected).cloned();
                        let painted = self.painted
                            && choice.as_ref().map(|choice| model_label(choice.as_deref()))
                                == self.rendered.as_deref().map(str::to_owned);
                        if let (true, Some(choice)) = (painted, choice) {
                            self.back();
                            return DiscoveryAction::Refresh(UiAction::SubagentModel(choice));
                        }
                    }
                    _ => {}
                }
                DiscoveryAction::None
            }
            Page::Panel => self.handle_panel_key(code, state),
        }
    }

    fn handle_panel_key(&mut self, code: KeyCode, state: &UiState) -> DiscoveryAction {
        match code {
            KeyCode::Down | KeyCode::Up => {
                let count = ROWS.len();
                let row = match (self.selected, code) {
                    (None, KeyCode::Down) => 0,
                    (None, _) => count - 1,
                    (Some(row), KeyCode::Down) => (row + 1) % count,
                    (Some(row), _) => (row + count - 1) % count,
                };
                self.selected = Some(row);
                if row >= FIRST_KIND_ROW {
                    self.kind = row - FIRST_KIND_ROW;
                }
                DiscoveryAction::None
            }
            KeyCode::Char('p') => {
                self.open_instructions();
                DiscoveryAction::None
            }
            KeyCode::Enter => {
                let Some(snapshot) = state.subagents.snapshot.as_deref() else {
                    return DiscoveryAction::None;
                };
                let row = self
                    .selected
                    .filter(|row| self.painted && self.rendered.as_deref() == Some(ROWS[*row]));
                match row {
                    Some(0) if !state.subagents.loading() => {
                        let Some(shown) = self.rendered_enabled else {
                            return DiscoveryAction::None;
                        };
                        self.invalidate();
                        DiscoveryAction::Refresh(UiAction::Subagents(Some(!shown)))
                    }
                    Some(MODEL_ROW) if !state.subagents.loading() => {
                        let current = snapshot.model.as_deref();
                        let selected = model_choices(state)
                            .iter()
                            .position(|choice| choice.as_deref() == current)
                            .unwrap_or(0);
                        self.page = Page::Models { selected };
                        self.invalidate();
                        DiscoveryAction::None
                    }
                    Some(row) if row >= FIRST_KIND_ROW => {
                        self.open_instructions();
                        DiscoveryAction::None
                    }
                    _ => DiscoveryAction::None,
                }
            }
            _ => DiscoveryAction::None,
        }
    }

    fn open_instructions(&mut self) {
        self.page = Page::Instructions {
            report: ReportView::default(),
        };
        self.invalidate();
    }

    /// Rows the docked panel needs for `state`, borders included.
    pub(crate) fn panel_height(&self, state: &UiState) -> u16 {
        let rows = match &self.page {
            Page::Panel => {
                // Two settings, a gap, the kinds, then the detail and the notes,
                // each after a gap.
                let detail = kind_detail(state, self.kind, usize::MAX).len();
                5 + 1 + detail + 1 + notes(state).len()
            }
            // The list, a gap, and the note under it.
            Page::Models { .. } => model_choices(state).len().min(MODELS_SHOWN) + 2,
            Page::Instructions { .. } => usize::from(INSTRUCTION_ROWS),
        };
        u16::try_from(rows + 2).unwrap_or(u16::MAX)
    }

    pub(crate) fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        state: &UiState,
        palette: Palette,
    ) {
        let (title, hint) = match &self.page {
            Page::Panel => (
                " Subagents ".to_owned(),
                " ↑↓ select · Enter change · p instructions · Esc close ",
            ),
            Page::Models { .. } => (
                " Subagent model ".to_owned(),
                " ↑↓ select · Enter apply · Esc back ",
            ),
            Page::Instructions { .. } => (
                format!(
                    " {} subagent instructions ",
                    ROWS[FIRST_KIND_ROW + self.kind]
                ),
                " ↑↓ scroll · Esc back ",
            ),
        };
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .padding(Padding::horizontal(1))
            .style(palette.composer())
            .border_style(palette.border())
            .title(title)
            .title_bottom(
                Line::from(hint)
                    .style(Style::default().fg(palette.muted))
                    .right_aligned(),
            );
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let width = usize::from(inner.width);
        let ready = inner.width >= 22 && !state.subagents.loading();
        match &mut self.page {
            Page::Instructions { report } => {
                let text = kind_at(state, self.kind).map_or_else(
                    || "Loading subagents…".to_owned(),
                    |kind| kind.instructions.clone(),
                );
                report.render(frame, inner, &wrap_words(&text, width));
                self.painted = false;
                self.rendered = None;
            }
            Page::Models { selected } => {
                let choices = model_choices(state);
                *selected = (*selected).min(choices.len() - 1);
                let current = state
                    .subagents
                    .snapshot
                    .as_deref()
                    .and_then(|snapshot| snapshot.model.as_deref());
                let first = selected.saturating_sub(MODELS_SHOWN - 1);
                let mut lines: Vec<Line<'static>> = choices
                    .iter()
                    .enumerate()
                    .skip(first)
                    .take(MODELS_SHOWN)
                    .map(|(index, choice)| {
                        let suffix = if choice.as_deref() == current {
                            "  current"
                        } else {
                            ""
                        };
                        let label = terminal_row(
                            &model_label(choice.as_deref()),
                            width.saturating_sub(2 + suffix.width()),
                        );
                        let line = Line::from(vec![
                            Span::raw(if index == *selected { "› " } else { "  " }),
                            Span::raw(label),
                            Span::styled(suffix, Style::default().fg(palette.muted)),
                        ]);
                        if index == *selected {
                            line.style(palette.selection())
                        } else {
                            line
                        }
                    })
                    .collect();
                lines.push(Line::default());
                let note = if state.model_catalog.is_none() {
                    "The model list is not loaded yet. Type /subagents model <name>."
                } else if choices.len() == 1 {
                    "This provider's models are not listed. Type /subagents model <name>."
                } else {
                    "Fresh subagents use this model. A fork always uses the session's."
                };
                lines.push(Line::styled(
                    terminal_row(note, width),
                    Style::default().fg(palette.muted),
                ));
                let shown = usize::from(inner.height) > selected.saturating_sub(first);
                frame.render_widget(Paragraph::new(lines), inner);
                self.painted = ready && shown;
                self.rendered = self
                    .painted
                    .then(|| model_label(choices[*selected].as_deref()));
            }
            Page::Panel => {
                let mut lines = setting_rows(state, self.selected, width, palette);
                let detail = kind_detail(state, self.kind, width);
                let notes = notes(state);
                // On a short terminal the detail gives way first, then the notes.
                let room = usize::from(inner.height);
                if !detail.is_empty() && lines.len() + 1 + detail.len() + 1 + notes.len() <= room {
                    lines.push(Line::default());
                    let muted = Style::default().fg(palette.muted);
                    lines.extend(detail.into_iter().map(|(label, text)| {
                        Line::from(vec![
                            Span::styled(format!("  {label:<LABEL_WIDTH$}"), muted),
                            Span::raw(text),
                        ])
                    }));
                }
                lines.push(Line::default());
                for (text, warns) in notes {
                    lines.push(Line::styled(
                        terminal_row(&text, width),
                        Style::default().fg(if warns {
                            palette.warning
                        } else {
                            palette.muted
                        }),
                    ));
                }
                frame.render_widget(Paragraph::new(lines), inner);
                self.painted = ready && inner.height >= 5;
                self.rendered = self
                    .selected
                    .filter(|_| self.painted)
                    .map(|row| ROWS[row].to_owned());
                self.rendered_enabled = state
                    .subagents
                    .snapshot
                    .as_deref()
                    .filter(|_| self.painted)
                    .map(|snapshot| snapshot.enabled);
            }
        }
    }
}

/// Break prose into rows of at most `width` columns at spaces, keeping its own
/// line breaks. A word wider than a row is left for the pager to split.
fn wrap_words(text: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    for line in text.lines() {
        let mut row = String::new();
        for word in line.split_whitespace() {
            if !row.is_empty() && row.width() + 1 + word.width() > width {
                rows.push(std::mem::take(&mut row));
            }
            if !row.is_empty() {
                row.push(' ');
            }
            row.push_str(word);
        }
        rows.push(row);
    }
    rows
}

fn kind_at(state: &UiState, kind: usize) -> Option<&SubagentKind> {
    state.subagents.snapshot.as_deref()?.kinds.get(kind)
}

/// What the model list offers: no choice ("same as the session") first, then the
/// session provider's models. A provider the catalog does not list offers only
/// the first; its model can still be typed.
fn model_choices(state: &UiState) -> Vec<Option<String>> {
    let models = state.model_catalog.as_ref().and_then(|catalog| {
        catalog
            .providers
            .iter()
            .find(|provider| provider.name == catalog.selection.provider)
    });
    std::iter::once(None)
        .chain(
            models
                .into_iter()
                .flat_map(|provider| provider.models.iter().map(|model| Some(model.id.clone()))),
        )
        .collect()
}

fn model_label(choice: Option<&str>) -> String {
    choice.unwrap_or(SAME_AS_SESSION).to_owned()
}

/// The setting and kind rows, the highlighted one in the selection style.
fn setting_rows(
    state: &UiState,
    selected: Option<usize>,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let snapshot = state.subagents.snapshot.as_deref();
    let warning = Style::default().fg(palette.warning);
    let values: [(String, Style); 4] = [
        (
            match snapshot.map(|snapshot| snapshot.enabled) {
                Some(true) => "On".to_owned(),
                Some(false) => "Off".to_owned(),
                None => "…".to_owned(),
            },
            Style::default(),
        ),
        snapshot.map_or_else(|| ("…".to_owned(), Style::default()), model_value(warning)),
        ("starts from its task alone".to_owned(), Style::default()),
        ("starts from this conversation".to_owned(), Style::default()),
    ];
    values
        .into_iter()
        .enumerate()
        .flat_map(|(row, (value, style))| {
            let chosen = selected == Some(row);
            let label_style = if chosen {
                Style::default().add_modifier(Modifier::BOLD)
            } else {
                Style::default()
                    .fg(palette.primary)
                    .add_modifier(Modifier::BOLD)
            };
            let line = Line::from(vec![
                Span::raw(if chosen { "› " } else { "  " }),
                Span::styled(format!("{:<LABEL_WIDTH$}", ROWS[row]), label_style),
                Span::styled(
                    terminal_row(&value, width.saturating_sub(2 + LABEL_WIDTH)),
                    // The selection style sets its own colours.
                    if chosen { Style::default() } else { style },
                ),
            ]);
            let line = if chosen {
                line.style(palette.selection())
            } else {
                line
            };
            // A gap sets the kinds apart from the settings.
            let gap = (row == FIRST_KIND_ROW).then(Line::default);
            gap.into_iter().chain(std::iter::once(line))
        })
        .collect()
}

/// The model row's value: the saved choice, flagged when the provider lacks it.
fn model_value(warning: Style) -> impl Fn(&SubagentsState) -> (String, Style) {
    move |snapshot| {
        let in_use = snapshot
            .model_in_use
            .as_deref()
            .unwrap_or("the default model");
        match snapshot.model.as_deref() {
            None => (format!("{SAME_AS_SESSION} ({in_use})"), Style::default()),
            Some(model) if snapshot.model_in_use.as_deref() == Some(model) => {
                (model.to_owned(), Style::default())
            }
            Some(model) => (
                format!("{model} is not on this provider; using {in_use}"),
                warning,
            ),
        }
    }
}

/// The labelled rows describing one kind, each cut to `width`. Empty until the
/// backend has reported the kinds.
fn kind_detail(state: &UiState, kind: usize, width: usize) -> Vec<(&'static str, String)> {
    let Some(snapshot) = state.subagents.snapshot.as_deref() else {
        return Vec::new();
    };
    let Some(described) = snapshot.kinds.get(kind) else {
        return Vec::new();
    };
    let model = described
        .model
        .as_deref()
        .unwrap_or("the provider's default");
    let model = if described.name == "fork" {
        format!("{model} (always the session's)")
    } else {
        model.to_owned()
    };
    let rows = [
        ("Runs on", model),
        (
            "Effort",
            described
                .effort
                .clone()
                .unwrap_or_else(|| "the model's default".to_owned()),
        ),
        ("Can use", tool_list(&snapshot.tools, "no tools")),
        ("Refused", tool_list(&snapshot.refused_tools, "nothing")),
        (
            "Limits",
            format!(
                "{} tool rounds · {} tasks a call · {} at once · {}-byte report",
                snapshot.max_tool_rounds,
                snapshot.max_tasks,
                snapshot.max_parallel,
                snapshot.report_max_bytes
            ),
        ),
        ("Told", described.instructions.clone()),
    ];
    let room = width.saturating_sub(2 + LABEL_WIDTH);
    rows.into_iter()
        .map(|(label, text)| (label, terminal_row(&text, room)))
        .collect()
}

/// The first few names and a count of the rest. Tool names can come from MCP
/// servers, so the caller cuts and sanitizes the row.
fn tool_list(names: &[String], none: &str) -> String {
    if names.is_empty() {
        return none.to_owned();
    }
    let named = names
        .iter()
        .take(TOOLS_NAMED)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(", ");
    match names.len().saturating_sub(TOOLS_NAMED) {
        0 => named,
        more => format!("{named}, +{more} more"),
    }
}

/// The rows below the detail, each flagged when it warns.
fn notes(state: &UiState) -> Vec<(String, bool)> {
    let mut notes = Vec::new();
    let snapshot = state.subagents.snapshot.as_deref();
    if let Some(error) = &state.subagents.error {
        notes.push((format!("Error: {error}"), true));
    } else if state.subagents.loading() {
        let text = if snapshot.is_some() {
            "Updating subagents…"
        } else {
            "Loading subagents…"
        };
        notes.push((text.to_owned(), false));
    }
    if let Some(snapshot) = snapshot {
        let offered = if snapshot.offered {
            "On for the next prompt."
        } else if snapshot.enabled {
            "On, but the next prompt has no read-only tool for a subagent to use."
        } else {
            "Off for the next prompt."
        };
        notes.push((offered.to_owned(), snapshot.enabled && !snapshot.offered));
        if let Some(env) = snapshot.env_enabled {
            notes.push((
                format!(
                    "WISP_SUBAGENTS turns subagents {} when Wisp starts.",
                    if env { "on" } else { "off" }
                ),
                true,
            ));
        }
        if let Some(model) = &snapshot.env_model {
            notes.push((
                format!("WISP_SUBAGENT_MODEL sets the model to {model} when Wisp starts."),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::discovery_view::DiscoveryView;
    use crossterm::event::{KeyEvent, KeyModifiers};
    use ratatui::{Terminal, backend::TestBackend};
    use std::sync::Arc;
    use wisp_protocol::events::{
        ModelCatalogEntry, ModelCatalogProvider, ModelCatalogSnapshot, ModelSelection,
    };

    const INSTRUCTIONS: &str = "You are a subagent. Report at the end, and stop when done.";

    fn snapshot() -> SubagentsState {
        let kind = |name: &str, model: &str, effort: Option<&str>| SubagentKind {
            name: name.to_owned(),
            model: Some(model.to_owned()),
            effort: effort.map(str::to_owned),
            instructions: INSTRUCTIONS.to_owned(),
        };
        SubagentsState {
            enabled: true,
            offered: true,
            model: Some("small".into()),
            model_in_use: Some("small".into()),
            tools: vec!["read".into(), "grep".into()],
            refused_tools: (1..=8).map(|n| format!("tool-{n}")).collect(),
            kinds: vec![
                kind("fresh", "small", None),
                kind("fork", "big", Some("high")),
            ],
            max_tool_rounds: 63,
            max_tasks: 8,
            max_parallel: 4,
            report_max_bytes: 8000,
            ..Default::default()
        }
    }

    fn state_with(snapshot: SubagentsState) -> UiState {
        let provider = |name: &str, models: &[&str]| ModelCatalogProvider {
            name: name.to_owned(),
            display_name: name.to_owned(),
            default_model: models[0].to_owned(),
            available: true,
            models: models
                .iter()
                .map(|id| ModelCatalogEntry {
                    id: (*id).to_owned(),
                    lifecycle: None,
                    effort_levels: Vec::new(),
                    default_effort: None,
                })
                .collect(),
        };
        let mut state = UiState::unconfigured();
        state.subagents.snapshot = Some(Arc::new(snapshot));
        state.model_catalog = Some(ModelCatalogSnapshot {
            selection: ModelSelection {
                provider: "here".into(),
                model: Some("big".into()),
                effective_model: Some("big".into()),
                catalog_model: Some("big".into()),
                effort: None,
                image_input: false,
                max_request_image_bytes: 0,
            },
            providers: vec![
                provider("here", &["big", "small"]),
                provider("elsewhere", &["other"]),
            ],
        });
        state
    }

    fn draw(view: &mut DiscoveryView, state: &UiState, width: u16, height: u16) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| {
                view.render(frame, frame.area(), state, None, Palette::default());
            })
            .unwrap();
        let cells: Vec<&str> = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        cells
            .chunks(usize::from(width))
            .map(|row| row.concat())
            .collect()
    }

    /// Draw at the height the docked panel asks for.
    fn draw_full(view: &mut DiscoveryView, state: &UiState, width: u16) -> Vec<String> {
        let height = match view {
            DiscoveryView::Subagents(view) => view.panel_height(state),
            _ => unreachable!("the view under test is the subagents panel"),
        };
        draw(view, state, width, height)
    }

    fn press(view: &mut DiscoveryView, state: &UiState, code: KeyCode) -> DiscoveryAction {
        view.handle_key(KeyEvent::new(code, KeyModifiers::NONE), state)
    }

    fn row<'a>(rows: &'a [String], label: &str) -> &'a str {
        rows.iter()
            .find(|row| row.contains(label))
            .unwrap_or_else(|| panic!("no row with {label:?} in:\n{}", rows.join("\n")))
    }

    #[test]
    fn panel_describes_each_kind() {
        let state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        let rows = draw_full(&mut view, &state, 90);
        assert!(row(&rows, "Subagents  ").contains("On"));
        assert!(!rows.join("").contains('›'));
        // Fresh is described until a kind is highlighted.
        assert!(row(&rows, "Runs on").contains("small"));
        assert!(row(&rows, "Effort").contains("the model's default"));
        assert!(row(&rows, "Can use").contains("read, grep"));
        assert!(row(&rows, "Refused").contains("tool-6, +2 more"));
        assert!(row(&rows, "Limits").contains("63 tool rounds · 8 tasks a call · 4 at once"));
        assert!(row(&rows, "Told").contains("You are a subagent."));

        for _ in 0..4 {
            press(&mut view, &state, KeyCode::Down);
        }
        let rows = draw_full(&mut view, &state, 90);
        assert!(row(&rows, "› Fork").contains("starts from this conversation"));
        assert!(row(&rows, "(always the session's)").contains("big"));
        assert!(row(&rows, "Effort").contains("high"));
    }

    #[test]
    fn enter_needs_painted_highlight() {
        let state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        draw(&mut view, &state, 90, 24);
        // Nothing is highlighted yet, and a highlight not yet drawn does not count.
        assert!(matches!(
            press(&mut view, &state, KeyCode::Enter),
            DiscoveryAction::None
        ));
        press(&mut view, &state, KeyCode::Down);
        assert!(matches!(
            press(&mut view, &state, KeyCode::Enter),
            DiscoveryAction::None
        ));
        draw(&mut view, &state, 90, 24);
        assert!(matches!(
            press(&mut view, &state, KeyCode::Enter),
            DiscoveryAction::Refresh(UiAction::Subagents(Some(false)))
        ));
    }

    #[test]
    fn model_list_is_this_provider() {
        let state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        press(&mut view, &state, KeyCode::Down);
        press(&mut view, &state, KeyCode::Down);
        draw(&mut view, &state, 90, 24);
        press(&mut view, &state, KeyCode::Enter);
        let rows = draw_full(&mut view, &state, 90);
        let listed = rows.join("\n");
        assert!(listed.contains("Same as the session"), "{listed}");
        assert!(listed.contains("big"), "{listed}");
        assert!(!listed.contains("other"), "{listed}");
        // The list opens on the saved choice.
        assert!(row(&rows, "› small").contains("current"));

        press(&mut view, &state, KeyCode::Up);
        draw(&mut view, &state, 90, 24);
        assert!(matches!(
            press(&mut view, &state, KeyCode::Enter),
            DiscoveryAction::Refresh(UiAction::SubagentModel(Some(model))) if model == "big"
        ));
        // Choosing returns to the panel.
        let rows = draw(&mut view, &state, 90, 24);
        assert!(rows.join("\n").contains("Fresh"));
    }

    #[test]
    fn same_as_session_clears() {
        let state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        press(&mut view, &state, KeyCode::Down);
        press(&mut view, &state, KeyCode::Down);
        draw(&mut view, &state, 90, 24);
        press(&mut view, &state, KeyCode::Enter);
        press(&mut view, &state, KeyCode::Up);
        press(&mut view, &state, KeyCode::Up);
        draw(&mut view, &state, 90, 24);
        assert!(matches!(
            press(&mut view, &state, KeyCode::Enter),
            DiscoveryAction::Refresh(UiAction::SubagentModel(None))
        ));
    }

    #[test]
    fn unloaded_catalog_says_so() {
        let mut state = state_with(snapshot());
        state.model_catalog = None;
        let mut view = DiscoveryView::subagents();
        press(&mut view, &state, KeyCode::Down);
        press(&mut view, &state, KeyCode::Down);
        draw(&mut view, &state, 90, 24);
        press(&mut view, &state, KeyCode::Enter);
        let listed = draw(&mut view, &state, 90, 24).join("\n");
        assert!(listed.contains("Same as the session"), "{listed}");
        assert!(
            listed.contains("The model list is not loaded yet."),
            "{listed}"
        );
    }

    #[test]
    fn toggle_follows_painted_setting() {
        let mut state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        press(&mut view, &state, KeyCode::Down);
        draw(&mut view, &state, 90, 24);
        // The setting changes after the panel was drawn showing it on.
        state.subagents.snapshot = Some(Arc::new(SubagentsState {
            enabled: false,
            ..snapshot()
        }));
        assert!(matches!(
            press(&mut view, &state, KeyCode::Enter),
            DiscoveryAction::Refresh(UiAction::Subagents(Some(false)))
        ));
    }

    #[test]
    fn esc_leaves_a_page_first() {
        let state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        press(&mut view, &state, KeyCode::Char('p'));
        let shown = draw(&mut view, &state, 90, 24).join("\n");
        assert!(shown.contains("Fresh subagent instructions"), "{shown}");
        assert!(shown.contains(INSTRUCTIONS), "{shown}");
        assert!(matches!(
            press(&mut view, &state, KeyCode::Esc),
            DiscoveryAction::None
        ));
        assert!(matches!(
            press(&mut view, &state, KeyCode::Esc),
            DiscoveryAction::Close
        ));
    }

    #[test]
    fn instructions_wrap_at_words() {
        assert_eq!(
            wrap_words("one two three\n\nfour", 7),
            ["one two", "three", "", "four"]
        );
    }

    #[test]
    fn short_terminal_keeps_settings() {
        let state = state_with(snapshot());
        let mut view = DiscoveryView::subagents();
        let rows = draw(&mut view, &state, 90, 10).join("\n");
        for kept in [
            "Subagents  ",
            "Model  ",
            "Fresh",
            "Fork",
            "On for the next prompt.",
        ] {
            assert!(rows.contains(kept), "{kept:?} missing from:\n{rows}");
        }
        assert!(!rows.contains("Can use"), "{rows}");
    }

    #[test]
    fn warns_about_fallback_and_env() {
        let state = state_with(SubagentsState {
            model: Some("gone".into()),
            model_in_use: Some("big".into()),
            env_model: Some("gone".into()),
            ..snapshot()
        });
        let mut view = DiscoveryView::subagents();
        let rows = draw_full(&mut view, &state, 100);
        assert!(row(&rows, "Model  ").contains("gone is not on this provider; using big"));
        row(
            &rows,
            "WISP_SUBAGENT_MODEL sets the model to gone when Wisp starts.",
        );
    }

    #[test]
    fn untrusted_names_are_sanitized() {
        let state = state_with(SubagentsState {
            tools: vec!["read\u{1b}]0;owned\u{7}".into()],
            ..snapshot()
        });
        let mut view = DiscoveryView::subagents();
        let rows = draw_full(&mut view, &state, 90).join("\n");
        assert!(
            !rows.contains('\u{1b}') && !rows.contains('\u{7}'),
            "{rows}"
        );
    }
}
