//! Startup-logo preview has no persistence or runtime effects until explicitly applied.

use crate::mouse::Rows;
use crate::startup_logo::{self, LogoChoice};
use crate::theme::Palette;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    text::Line,
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph},
};

const MAX_VISIBLE_CHOICES: usize = 6;

pub(crate) enum LogoPickerAction {
    None,
    Close,
    Apply {
        choice: LogoChoice,
        resolved: LogoChoice,
    },
}

pub(crate) struct LogoPicker {
    selected: usize,
    rendered: Option<usize>,
    random_preview: LogoChoice,
}

impl LogoPicker {
    pub fn new(current: LogoChoice, resolved: LogoChoice) -> Self {
        Self {
            selected: LogoChoice::ALL
                .iter()
                .position(|choice| *choice == current)
                .unwrap_or(0),
            rendered: None,
            random_preview: if current == LogoChoice::Random {
                resolved
            } else {
                LogoChoice::choose_random()
            },
        }
    }

    /// The scheduler preserves this painted choice while draining a finite event prefix.
    pub fn rendered_selection(&self) -> Option<usize> {
        self.rendered
    }

    pub fn preview(&self) -> LogoChoice {
        LogoChoice::ALL[self.selected]
    }

    fn preview_logo(&self) -> LogoChoice {
        match self.preview() {
            LogoChoice::Random => self.random_preview,
            selected => selected,
        }
    }

    pub fn invalidate(&mut self) {
        self.rendered = None;
    }

    pub fn select_mouse(&mut self, index: usize) -> bool {
        if self.rendered.is_none() || index >= LogoChoice::ALL.len() {
            return false;
        }
        self.selected = index;
        self.invalidate();
        true
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> LogoPickerAction {
        if key.code == KeyCode::Esc
            || (key.code == KeyCode::Char('c') && key.modifiers == KeyModifiers::CONTROL)
        {
            return LogoPickerAction::Close;
        }
        if key.modifiers != KeyModifiers::NONE {
            return LogoPickerAction::None;
        }
        let last = LogoChoice::ALL.len() - 1;
        match key.code {
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => self.selected = (self.selected + 1).min(last),
            KeyCode::PageUp => self.selected = self.selected.saturating_sub(5),
            KeyCode::PageDown => self.selected = (self.selected + 5).min(last),
            KeyCode::Home => self.selected = 0,
            KeyCode::End => self.selected = last,
            KeyCode::Enter if self.rendered == Some(self.selected) => {
                return LogoPickerAction::Apply {
                    choice: self.preview(),
                    resolved: self.preview_logo(),
                };
            }
            _ => return LogoPickerAction::None,
        }
        self.invalidate();
        LogoPickerAction::None
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        palette: Palette,
        no_color: bool,
    ) -> Rows {
        self.invalidate();
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(" Startup logo ")
            .title_bottom(" ↑↓ preview • Enter apply • Esc cancel ");
        let inner = block.inner(area);
        frame.render_widget(block, area);
        if inner.height < 2 || inner.width == 0 {
            return Rows::default();
        }

        frame.render_widget(
            Paragraph::new(if no_color {
                "NO_COLOR: monochrome; Enter still applies."
            } else {
                "Preview only until Enter."
            }),
            Rect { height: 1, ..inner },
        );

        let content = Rect {
            y: inner.y + 1,
            height: inner.height - 1,
            ..inner
        };
        let list_height = content.height.min(
            u16::try_from(LogoChoice::ALL.len().min(MAX_VISIBLE_CHOICES))
                .expect("bounded logo catalog height"),
        );
        let list_area = Rect {
            height: list_height,
            ..content
        };
        let offset = self
            .selected
            .saturating_sub(usize::from(list_area.height) - 1);
        let rows = LogoChoice::ALL
            .iter()
            .skip(offset)
            .take(list_area.height.into())
            .map(|choice| ListItem::new(Line::raw(choice.label())))
            .collect::<Vec<_>>();
        let mut state = ListState::default().with_selected(Some(self.selected - offset));
        frame.render_stateful_widget(
            List::new(rows)
                .highlight_symbol("› ")
                .highlight_style(palette.selection()),
            list_area,
            &mut state,
        );

        let preview_area = Rect {
            y: content.y + list_height,
            height: content.height - list_height,
            ..content
        };
        if preview_area.height > 0 {
            frame.render_widget(
                Paragraph::new(startup_logo::preview_lines(
                    self.preview_logo(),
                    preview_area.width,
                    preview_area.height,
                    palette,
                    no_color,
                )),
                preview_area,
            );
        }

        self.rendered = Some(self.selected);
        Rows::new(list_area, offset + state.offset(), LogoChoice::ALL.len(), 1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    #[test]
    fn navigation_stays_within_the_catalog() {
        let mut picker = LogoPicker::new(LogoChoice::Random, LogoChoice::Classic);

        picker.handle_key(key(KeyCode::Up));
        assert_eq!(picker.preview(), LogoChoice::Random);

        picker.handle_key(key(KeyCode::End));
        assert_eq!(picker.preview(), LogoChoice::AdalMarkBraille);

        picker.handle_key(key(KeyCode::Down));
        assert_eq!(picker.preview(), LogoChoice::AdalMarkBraille);

        picker.handle_key(key(KeyCode::Home));
        assert_eq!(picker.preview(), LogoChoice::Random);
    }

    #[test]
    fn enter_only_applies_the_choice_that_was_painted() {
        let mut picker = LogoPicker::new(LogoChoice::AdalBraille, LogoChoice::AdalBraille);

        assert!(matches!(
            picker.handle_key(key(KeyCode::Enter)),
            LogoPickerAction::None
        ));
        picker.rendered = Some(picker.selected);
        assert!(matches!(
            picker.handle_key(key(KeyCode::Enter)),
            LogoPickerAction::Apply {
                choice: LogoChoice::AdalBraille,
                resolved: LogoChoice::AdalBraille,
            }
        ));
    }

    #[test]
    fn mouse_selection_requires_a_painted_catalog() {
        let mut picker = LogoPicker::new(LogoChoice::Random, LogoChoice::Classic);

        assert!(!picker.select_mouse(1));
        picker.rendered = Some(0);
        assert!(picker.select_mouse(1));
        assert_eq!(picker.preview(), LogoChoice::Classic);
        assert_eq!(picker.rendered_selection(), None);
    }

    #[test]
    fn random_preview_stays_resolved_to_the_logo_that_will_be_applied() {
        let mut picker = LogoPicker::new(LogoChoice::Random, LogoChoice::AdalMarkBraille);
        assert_eq!(picker.preview_logo(), LogoChoice::AdalMarkBraille);
        picker.rendered = Some(0);
        assert!(matches!(
            picker.handle_key(key(KeyCode::Enter)),
            LogoPickerAction::Apply {
                choice: LogoChoice::Random,
                resolved: LogoChoice::AdalMarkBraille,
            }
        ));
    }
}
