//! Backend-catalog-driven model and effort selection. No configuration changes on navigation.

use std::collections::BTreeMap;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph};
use wisp_protocol::commands::ModelConfiguration;
use wisp_protocol::events::{ModelCatalogSnapshot, ModelLifecycle};

use crate::session_picker::terminal_row;
use crate::theme::Palette;

#[derive(Debug, Eq, PartialEq)]
pub enum ModelPickerAction {
    None,
    Close,
    Refresh,
    Apply(ModelConfiguration),
}

#[derive(Debug, Eq, PartialEq)]
pub enum ModelCommand {
    Picker,
    ProviderStatus,
    Configure(ModelConfiguration),
    Invalid(&'static str),
}

/// Recognize model-selection commands without reinterpreting other slash input.
pub fn command(input: &str) -> Option<ModelCommand> {
    if !input.trim_start().starts_with('/') {
        return None;
    }
    let parts: Vec<_> = input.split_whitespace().take(4).collect();
    match parts.as_slice() {
        ["/model"] => Some(ModelCommand::Picker),
        ["/provider"] => Some(ModelCommand::ProviderStatus),
        ["/provider", provider] => Some(ModelCommand::Configure(ModelConfiguration {
            provider: Some((*provider).into()),
            persist_model_selection: true,
            ..Default::default()
        })),
        ["/provider", ..] => Some(ModelCommand::Invalid("Usage: /provider [name]")),
        ["/model", model, rest @ ..] if rest.len() <= 1 => {
            let (provider, model) = match model.split_once("::") {
                Some(("", _)) | Some((_, "")) => {
                    return Some(ModelCommand::Invalid(
                        "Usage: /model [provider::]model [effort|-]",
                    ));
                }
                Some((provider, model)) => (Some(provider.into()), model),
                None => (None, *model),
            };
            let effort = rest.first().copied();
            Some(ModelCommand::Configure(ModelConfiguration {
                provider,
                model: Some(model.into()),
                effort: effort.filter(|value| *value != "-").map(str::to_owned),
                clear_effort: effort == Some("-"),
                persist_model_selection: true,
            }))
        }
        ["/model", ..] => Some(ModelCommand::Invalid(
            "Usage: /model [provider::]model [effort|-]",
        )),
        _ => None,
    }
}

#[derive(Clone, Debug)]
enum Row {
    Provider(usize),
    Model { provider: usize, model: usize },
}

#[derive(Clone, Debug, Default)]
pub struct ModelPicker {
    catalog: Option<ModelCatalogSnapshot>,
    rows: Vec<Row>,
    selected: Option<usize>,
    efforts: BTreeMap<(String, String), Option<String>>,
    loading: bool,
}

impl ModelPicker {
    pub fn loading() -> Self {
        Self {
            loading: true,
            ..Self::default()
        }
    }

    pub fn invalidate(&mut self) {
        self.loading = true;
    }

    pub fn unavailable(&mut self) {
        self.catalog = None;
        self.rows.clear();
        self.selected = None;
        self.loading = false;
    }

    pub fn update_catalog(&mut self, catalog: ModelCatalogSnapshot) {
        let previous = self.selected_key();
        self.rows.clear();
        self.efforts.retain(|(provider, model), effort| {
            catalog
                .providers
                .iter()
                .find(|entry| &entry.name == provider)
                .and_then(|entry| entry.models.iter().find(|entry| &entry.id == model))
                .is_some_and(|entry| {
                    effort
                        .as_ref()
                        .is_none_or(|level| entry.effort_levels.contains(level))
                })
        });
        let current = (
            catalog.selection.provider.clone(),
            catalog.selection.catalog_model.clone(),
        );
        let mut initial = None;
        let mut retained = None;
        let mut first = None;
        for (provider_index, provider) in catalog.providers.iter().enumerate() {
            if provider.name == "fake" {
                continue;
            }
            self.rows.push(Row::Provider(provider_index));
            for (model_index, model) in provider.models.iter().enumerate() {
                let index = self.rows.len();
                self.rows.push(Row::Model {
                    provider: provider_index,
                    model: model_index,
                });
                if !provider.available {
                    continue;
                }
                first.get_or_insert(index);
                let key = (provider.name.clone(), model.id.clone());
                if previous.as_ref() == Some(&key) {
                    retained = Some(index);
                }
                if provider.name == current.0 && Some(&model.id) == current.1.as_ref() {
                    initial = Some(index);
                    self.efforts.entry(key).or_insert_with(|| {
                        catalog
                            .selection
                            .effort
                            .clone()
                            .filter(|level| model.effort_levels.contains(level))
                    });
                }
            }
        }
        self.selected = retained.or(initial).or(first);
        self.catalog = Some(catalog);
        self.loading = false;
    }

    fn selected_key(&self) -> Option<(String, String)> {
        let (provider, model) = self.selected_model()?;
        Some((provider.name.clone(), model.id.clone()))
    }

    fn selected_model(
        &self,
    ) -> Option<(
        &wisp_protocol::events::ModelCatalogProvider,
        &wisp_protocol::events::ModelCatalogEntry,
    )> {
        let Row::Model { provider, model } = self.rows.get(self.selected?)? else {
            return None;
        };
        let provider = self.catalog.as_ref()?.providers.get(*provider)?;
        provider
            .available
            .then_some((provider, provider.models.get(*model)?))
    }

    fn move_by(&mut self, delta: isize) {
        let Some(catalog) = &self.catalog else {
            return;
        };
        let selectable: Vec<_> = self
            .rows
            .iter()
            .enumerate()
            .filter_map(|(index, row)| match row {
                Row::Model { provider, .. } if catalog.providers[*provider].available => {
                    Some(index)
                }
                _ => None,
            })
            .collect();
        if selectable.is_empty() {
            return;
        }
        let current = selectable
            .iter()
            .position(|index| Some(*index) == self.selected)
            .unwrap_or(0);
        let target = (current as isize)
            .saturating_add(delta)
            .clamp(0, selectable.len() as isize - 1);
        self.selected = Some(selectable[target as usize]);
    }

    fn change_effort(&mut self, delta: isize) {
        let Some((provider, model)) = self.selected_model() else {
            return;
        };
        let key = (provider.name.clone(), model.id.clone());
        let choice = self.efforts.get(&key).and_then(Option::as_ref);
        let index = choice
            .and_then(|level| model.effort_levels.iter().position(|value| value == level))
            .map_or(0, |index| index + 1);
        let next = (index as isize + delta).clamp(0, model.effort_levels.len() as isize) as usize;
        let value = next
            .checked_sub(1)
            .map(|index| model.effort_levels[index].clone());
        self.efforts.insert(key, value);
    }

    pub fn handle_key(&mut self, key: KeyEvent, applying: bool) -> ModelPickerAction {
        if key.code == KeyCode::Esc
            || (key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL))
        {
            return ModelPickerAction::Close;
        }
        if applying || key.modifiers != KeyModifiers::NONE {
            return ModelPickerAction::None;
        }
        if key.code == KeyCode::Char('r') {
            return ModelPickerAction::Refresh;
        }
        if self.loading {
            return ModelPickerAction::None;
        }
        match key.code {
            KeyCode::Up => self.move_by(-1),
            KeyCode::Down => self.move_by(1),
            KeyCode::PageUp => self.move_by(-10),
            KeyCode::PageDown => self.move_by(10),
            KeyCode::Home => self.move_by(isize::MIN),
            KeyCode::End => self.move_by(isize::MAX),
            KeyCode::Left => self.change_effort(-1),
            KeyCode::Right => self.change_effort(1),
            KeyCode::Enter => {
                if let Some((provider, model)) = self.selected_model() {
                    let effort = self
                        .efforts
                        .get(&(provider.name.clone(), model.id.clone()))
                        .cloned()
                        .flatten();
                    return ModelPickerAction::Apply(ModelConfiguration {
                        provider: Some(provider.name.clone()),
                        model: Some(model.id.clone()),
                        clear_effort: effort.is_none(),
                        effort,
                        persist_model_selection: true,
                    });
                }
            }
            _ => {}
        }
        ModelPickerAction::None
    }
}

pub fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    picker: &ModelPicker,
    applying: bool,
    notice: Option<&str>,
    palette: Palette,
) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(3),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .split(area);
    let current = picker
        .catalog
        .as_ref()
        .map(|catalog| {
            format!(
                "Current: {} / {} · {}",
                catalog.selection.provider,
                catalog
                    .selection
                    .effective_model
                    .as_deref()
                    .unwrap_or("provider default"),
                catalog
                    .selection
                    .effort
                    .as_deref()
                    .unwrap_or("default effort")
            )
        })
        .unwrap_or_else(|| "Select a model".into());
    frame.render_widget(
        Paragraph::new(terminal_row(&current, usize::from(chunks[0].width))),
        chunks[0],
    );
    let height = usize::from(chunks[1].height.saturating_sub(2));
    let width = usize::from(chunks[1].width.saturating_sub(2));
    let start = picker.selected.map_or(0, |selected| {
        selected.saturating_sub(height.saturating_sub(1))
    });
    let lines = if picker.loading {
        vec![Line::from("Loading model catalog…")]
    } else if let Some(catalog) = &picker.catalog {
        if picker.rows.is_empty() {
            vec![Line::from("No models available.")]
        } else {
            picker
                .rows
                .iter()
                .enumerate()
                .skip(start)
                .take(height)
                .map(|(index, row)| {
                    let (label, available) = match row {
                        Row::Provider(provider) => {
                            let provider = &catalog.providers[*provider];
                            (
                                format!(
                                    "{}{}",
                                    provider.display_name,
                                    if provider.available {
                                        ""
                                    } else {
                                        " (unavailable)"
                                    }
                                ),
                                false,
                            )
                        }
                        Row::Model { provider, model } => {
                            let provider = &catalog.providers[*provider];
                            let model = &provider.models[*model];
                            let lifecycle = match model.lifecycle {
                                Some(ModelLifecycle::Preview) => " (preview)",
                                Some(ModelLifecycle::Legacy) => " (legacy)",
                                _ => "",
                            };
                            (
                                format!("  {}::{}{}", provider.name, model.id, lifecycle),
                                provider.available,
                            )
                        }
                    };
                    let style = if picker.selected == Some(index) {
                        palette.selection()
                    } else if !available {
                        Style::default().fg(palette.muted)
                    } else {
                        Style::default()
                    };
                    Line::from(Span::styled(terminal_row(&label, width), style))
                })
                .collect()
        }
    } else {
        vec![Line::from("Catalog unavailable. Press r to retry.")]
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(" model ")
                .borders(Borders::ALL)
                .border_style(palette.border()),
        ),
        chunks[1],
    );
    let effort = picker
        .selected_model()
        .map(|(provider, model)| {
            let chosen = picker
                .efforts
                .get(&(provider.name.clone(), model.id.clone()))
                .and_then(Option::as_deref)
                .unwrap_or("Default");
            if model.effort_levels.is_empty() {
                "Effort: provider default".into()
            } else {
                format!("Effort: {chosen}  ←/→ change")
            }
        })
        .unwrap_or_default();
    frame.render_widget(
        Paragraph::new(terminal_row(&effort, usize::from(chunks[2].width))),
        chunks[2],
    );
    let hint = if applying {
        "Applying… · Esc close"
    } else {
        "↑/↓ model · Enter apply · Esc close · r refresh"
    };
    frame.render_widget(
        Paragraph::new(terminal_row(hint, usize::from(chunks[3].width))),
        chunks[3],
    );
    frame.render_widget(
        Paragraph::new(terminal_row(
            notice.unwrap_or(""),
            usize::from(chunks[4].width),
        )),
        chunks[4],
    );
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;

    pub(crate) fn catalog() -> ModelCatalogSnapshot {
        serde_json::from_value(json!({
            "selection": {"provider":"alpha", "model":"alias", "effective_model":"one",
                "catalog_model":"one", "effort":"high"},
            "providers": [
                {"name":"fake", "display_name":"Development", "default_model":"fake",
                    "available":true, "models":[{"id":"fake", "effort_levels":[]}]},
                {"name":"alpha", "display_name":"Alpha", "default_model":"one",
                    "available":true, "models":[
                        {"id":"one", "effort_levels":["low", "high"]},
                        {"id":"two", "lifecycle":"preview", "effort_levels":[]}]},
                {"name":"missing", "display_name":"Missing", "default_model":"one",
                    "available":false, "models":[{"id":"one", "effort_levels":[]}]},
                {"name":"beta", "display_name":"Beta", "default_model":"one",
                    "available":true, "models":[{"id":"one", "lifecycle":"legacy", "effort_levels":["medium"]}]}
            ]
        })).unwrap()
    }

    fn press(picker: &mut ModelPicker, code: KeyCode) -> ModelPickerAction {
        picker.handle_key(KeyEvent::new(code, KeyModifiers::NONE), false)
    }

    #[test]
    fn commands_preserve_custom_names_and_default_effort_intent() {
        assert_eq!(command("/model"), Some(ModelCommand::Picker));
        assert_eq!(command("/provider"), Some(ModelCommand::ProviderStatus));
        for (input, provider, model, effort, clear) in [
            ("/model custom/new", None, Some("custom/new"), None, false),
            (
                " /model beta::alias high ",
                Some("beta"),
                Some("alias"),
                Some("high"),
                false,
            ),
            ("/model alias -", None, Some("alias"), None, true),
            ("/provider beta", Some("beta"), None, None, false),
        ] {
            let Some(ModelCommand::Configure(value)) = command(input) else {
                panic!("{input}")
            };
            assert_eq!(value.provider.as_deref(), provider);
            assert_eq!(value.model.as_deref(), model);
            assert_eq!(value.effort.as_deref(), effort);
            assert_eq!(value.clear_effort, clear);
            assert!(value.persist_model_selection);
        }
        for input in [
            "/model ::name",
            "/model alpha::",
            "/model a b c d",
            "/provider a b",
        ] {
            assert!(matches!(command(input), Some(ModelCommand::Invalid(_))));
        }
        for input in ["/skill test", "/models", "describe /model"] {
            assert_eq!(command(input), None);
        }
    }

    #[test]
    fn alias_initial_selection_navigation_and_effort_are_staged() {
        let mut picker = ModelPicker::loading();
        picker.update_catalog(catalog());
        assert_eq!(picker.selected_key(), Some(("alpha".into(), "one".into())));
        assert!(
            matches!(press(&mut picker, KeyCode::Enter), ModelPickerAction::Apply(value) if value.effort.as_deref() == Some("high"))
        );
        press(&mut picker, KeyCode::Left);
        press(&mut picker, KeyCode::Down);
        press(&mut picker, KeyCode::Down); // Skips unavailable provider.
        assert_eq!(picker.selected_key(), Some(("beta".into(), "one".into())));
        press(&mut picker, KeyCode::PageDown); // Clamps; never wraps.
        assert_eq!(picker.selected_key(), Some(("beta".into(), "one".into())));
        press(&mut picker, KeyCode::Home); // Skips development provider.
        assert!(
            matches!(press(&mut picker, KeyCode::Enter), ModelPickerAction::Apply(value) if value.effort.as_deref() == Some("low"))
        );
        picker.update_catalog(catalog()); // Refresh preserves staged effort.
        press(&mut picker, KeyCode::Left);
        press(&mut picker, KeyCode::Left);
        assert!(
            matches!(press(&mut picker, KeyCode::Enter), ModelPickerAction::Apply(value) if value.clear_effort && value.effort.is_none())
        );
        press(&mut picker, KeyCode::End);
        press(&mut picker, KeyCode::PageUp);
        assert_eq!(picker.selected_key(), Some(("alpha".into(), "one".into())));
    }

    #[test]
    fn loading_and_applying_are_dismissible_but_cannot_apply() {
        let mut picker = ModelPicker::loading();
        assert_eq!(press(&mut picker, KeyCode::Enter), ModelPickerAction::None);
        assert_eq!(press(&mut picker, KeyCode::Esc), ModelPickerAction::Close);
        picker.update_catalog(catalog());
        for code in [KeyCode::Enter, KeyCode::Down, KeyCode::Char('r')] {
            assert_eq!(
                picker.handle_key(KeyEvent::new(code, KeyModifiers::NONE), true),
                ModelPickerAction::None
            );
        }
        assert_eq!(
            picker.handle_key(
                KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL),
                true
            ),
            ModelPickerAction::Close
        );
        let mut unavailable = catalog();
        for provider in &mut unavailable.providers {
            provider.available = false;
        }
        picker.update_catalog(unavailable);
        assert_eq!(press(&mut picker, KeyCode::Enter), ModelPickerAction::None);
    }

    #[test]
    fn narrow_and_large_catalog_rendering_is_bounded_and_sanitized() {
        let mut catalog = catalog();
        catalog.providers[1].models[0].id = "unsafe\n\u{1b}[31m\t".repeat(1000);
        catalog.selection.catalog_model = Some(catalog.providers[1].models[0].id.clone());
        let row = catalog.providers[1].models[0].clone();
        catalog.providers[1]
            .models
            .extend(std::iter::repeat_n(row, 500));
        let mut picker = ModelPicker::loading();
        picker.update_catalog(catalog);
        for (width, height) in [(30, 8), (80, 24)] {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal
                .draw(|frame| {
                    render(
                        frame,
                        frame.area(),
                        &picker,
                        false,
                        None,
                        Palette::default(),
                    )
                })
                .unwrap();
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer.content.len(), usize::from(width * height));
            assert!(
                buffer
                    .content
                    .iter()
                    .all(|cell| !cell.symbol().chars().any(char::is_control))
            );
            assert!(
                buffer
                    .content
                    .iter()
                    .any(|cell| cell.bg == Palette::default().primary)
            );
        }
    }
}
