//! Theme preview has no persistence or runtime effects until explicitly applied.

use crate::mouse::Rows;
use crate::theme::{self, Palette, Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    text::Line,
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph},
};

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
    ) -> Rows {
        self.invalidate();
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(" Themes ")
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
        let rows_area = Rect {
            y: inner.y + 1,
            height: inner.height - 1,
            ..inner
        };
        let offset = self
            .selected
            .saturating_sub(usize::from(rows_area.height) - 1);
        let rows = theme::themes()
            .iter()
            .skip(offset)
            .take(rows_area.height.into())
            .map(|theme| {
                ListItem::new(Line::raw(format!(
                    "{:<8} {}",
                    theme.label, theme.description
                )))
            })
            .collect::<Vec<_>>();
        let mut state = ListState::default().with_selected(Some(self.selected - offset));
        frame.render_stateful_widget(
            List::new(rows)
                .highlight_symbol("› ")
                .highlight_style(palette.selection()),
            rows_area,
            &mut state,
        );
        self.rendered = Some(self.selected);
        Rows::new(rows_area, offset + state.offset(), theme::themes().len(), 1)
    }
}
