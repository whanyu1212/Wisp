//! Theme preview has no persistence or runtime effects until explicitly applied.

use crate::dialog::{self, Dialog, Hint, PickerRow};
use crate::mouse::Rows;
use crate::session_picker::terminal_row;
use crate::theme::{self, Palette, Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{Frame, layout::Rect, style::Style, widgets::Paragraph};

pub(crate) enum ThemePickerAction {
    None,
    Close,
    Apply(&'static Theme),
}

pub(crate) struct ThemePicker {
    selected: usize,
    rendered: Option<usize>,
}

impl ThemePicker {
    // The scheduler preserves this painted choice while draining a finite event prefix.
    pub fn rendered_selection(&self) -> Option<usize> {
        self.rendered
    }

    pub fn new(current: &Theme) -> Self {
        Self {
            selected: theme::themes()
                .iter()
                .position(|theme| theme.name == current.name)
                .unwrap_or(0),
            rendered: None,
        }
    }

    pub fn preview(&self) -> &'static Theme {
        &theme::themes()[self.selected]
    }

    pub fn invalidate(&mut self) {
        self.rendered = None;
    }

    pub fn select_mouse(&mut self, index: usize) -> bool {
        if self.rendered.is_none() || index >= theme::themes().len() {
            return false;
        }
        self.selected = index;
        self.invalidate();
        true
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> ThemePickerAction {
        if key.code == KeyCode::Esc
            || (key.code == KeyCode::Char('c') && key.modifiers == KeyModifiers::CONTROL)
        {
            return ThemePickerAction::Close;
        }
        if key.modifiers != KeyModifiers::NONE {
            return ThemePickerAction::None;
        }
        let last = theme::themes().len() - 1;
        match key.code {
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => self.selected = (self.selected + 1).min(last),
            KeyCode::PageUp => self.selected = self.selected.saturating_sub(5),
            KeyCode::PageDown => self.selected = (self.selected + 5).min(last),
            KeyCode::Home => self.selected = 0,
            KeyCode::End => self.selected = last,
            KeyCode::Enter if self.rendered == Some(self.selected) => {
                return ThemePickerAction::Apply(self.preview());
            }
            _ => return ThemePickerAction::None,
        }
        self.invalidate();
        ThemePickerAction::None
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        palette: Palette,
        no_color: bool,
    ) -> dialog::Rendered {
        self.invalidate();
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let hints = [
            Hint {
                label: "Enter",
                description: "apply",
                key: key(KeyCode::Enter),
            },
            Hint {
                label: "Esc",
                description: "cancel",
                key: key(KeyCode::Esc),
            },
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
        ];
        let layout = Dialog {
            title: "Themes",
            tabs: &[],
            filter: None,
            hints: &hints,
        }
        .render(frame, area, palette);
        let inner = layout.body;
        if inner.height < 2 || inner.width == 0 {
            return dialog::Rendered {
                rows: Rows::default(),
                controls: layout.controls,
            };
        }
        let note = if no_color {
            "NO_COLOR: monochrome; Enter still applies."
        } else {
            "Preview only until Enter."
        };
        let note_area = Rect::new(inner.x + 1, inner.y, inner.width.saturating_sub(2), 1);
        frame.render_widget(
            Paragraph::new(terminal_row(note, usize::from(note_area.width)))
                .style(Style::default().fg(palette.muted)),
            note_area,
        );
        let rows_area = Rect {
            y: inner.y + 1,
            height: inner.height - 1,
            ..inner
        };
        let height = usize::from(rows_area.height);
        let offset = self.selected.saturating_sub(height.saturating_sub(1));
        for (visible_index, (index, theme)) in theme::themes()
            .iter()
            .enumerate()
            .skip(offset)
            .take(height)
            .enumerate()
        {
            let row = Rect::new(
                rows_area.x,
                rows_area.y + visible_index as u16,
                rows_area.width,
                1,
            );
            PickerRow {
                label: &theme.label,
                description: None,
                tag: Some(&theme.description),
                selected: index == self.selected,
                muted: false,
            }
            .render(frame, row, palette);
        }
        self.rendered = Some(self.selected);
        dialog::Rendered {
            rows: Rows::new(rows_area, offset, theme::themes().len(), 1),
            controls: layout.controls,
        }
    }
}
