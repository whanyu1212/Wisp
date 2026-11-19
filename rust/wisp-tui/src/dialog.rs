//! Shared picker chrome and painted controls. Views keep their own navigation and actions.

use crossterm::event::KeyEvent;
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph},
};
use unicode_width::UnicodeWidthStr;

use crate::{mouse::Rows, session_picker::terminal_row, theme::Palette};

/// One completely painted tab or footer hint, routed through the owning view's key handler.
#[derive(Clone, Debug, Eq, PartialEq)]
struct Control {
    area: Rect,
    key: KeyEvent,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct Controls(Vec<Control>);

impl Controls {
    pub fn key_at(&self, column: u16, row: u16) -> Option<KeyEvent> {
        self.0
            .iter()
            .find(|control| control.area.contains((column, row).into()))
            .map(|control| control.key)
    }
}

/// Hit regions for one frame, published only after the draw succeeds.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct Rendered {
    pub rows: Rows,
    pub controls: Controls,
}

pub(crate) struct Hint<'a> {
    pub label: &'a str,
    pub description: &'a str,
    pub key: KeyEvent,
}

pub(crate) struct Tab<'a> {
    pub label: &'a str,
    pub selected: bool,
    pub key: KeyEvent,
}

pub(crate) struct Dialog<'a> {
    pub title: &'a str,
    pub tabs: &'a [Tab<'a>],
    pub filter: Option<&'a str>,
    pub hints: &'a [Hint<'a>],
}

pub(crate) struct Layout {
    pub body: Rect,
    pub controls: Controls,
}

impl Dialog<'_> {
    pub fn render(&self, frame: &mut Frame<'_>, area: Rect, palette: Palette) -> Layout {
        self.render_with_border(frame, area, palette, palette.border())
    }

    pub fn render_with_border(
        &self,
        frame: &mut Frame<'_>,
        area: Rect,
        palette: Palette,
        border_style: Style,
    ) -> Layout {
        let block = Block::default()
            .title(format!(
                "─ {} ─",
                terminal_row(self.title, usize::from(area.width.saturating_sub(6)))
            ))
            .borders(Borders::ALL)
            .border_style(border_style);
        let mut body = block.inner(area);
        frame.render_widget(block, area);
        let mut controls = Controls::default();
        if body.width == 0 || body.height == 0 {
            return Layout { body, controls };
        }

        // Reserve at most two footer rows, keeping at least one row for the view's content.
        let footer_height = body.height.saturating_sub(1).min(2);
        let footer = Rect::new(
            body.x,
            body.bottom() - footer_height,
            body.width,
            footer_height,
        );
        let used = render_hints(frame, footer, self.hints, palette, &mut controls);
        body.height -= used;

        // Optional sections never displace the last content row. Omitted or clipped tabs
        // publish no target, so a click cannot select a control that was not visible.
        if !self.tabs.is_empty() && body.height >= 3 {
            let tabs = Rect::new(body.x, body.y, body.width, 1);
            render_tabs(frame, tabs, self.tabs, palette, &mut controls);
            frame.render_widget(
                Paragraph::new("─".repeat(usize::from(body.width))).style(palette.border()),
                Rect::new(body.x, body.y + 1, body.width, 1),
            );
            body.y += 2;
            body.height -= 2;
        }
        if let Some(filter) = self.filter.filter(|_| body.height >= 2) {
            frame.render_widget(
                Paragraph::new(terminal_row(filter, usize::from(body.width)))
                    .style(Style::default().fg(palette.muted)),
                Rect::new(body.x, body.y, body.width, 1),
            );
            body.y += 1;
            body.height -= 1;
        }
        Layout { body, controls }
    }
}

/// Paint whole hints only. Each target covers its key and description, never a separator.
pub(crate) fn render_hints(
    frame: &mut Frame<'_>,
    area: Rect,
    hints: &[Hint<'_>],
    palette: Palette,
    controls: &mut Controls,
) -> u16 {
    let start = controls.0.len();
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut column = 0_u16;
    for hint in hints {
        let label = crate::ui::sanitize_for_terminal(hint.label).replace('\n', " ");
        let description = crate::ui::sanitize_for_terminal(hint.description).replace('\n', " ");
        let width = label.width() + 1 + description.width();
        let Ok(width) = u16::try_from(width) else {
            continue;
        };
        if width > area.width || area.height == 0 {
            continue;
        }
        let separator = if column == 0 { 0 } else { 3 };
        if column.saturating_add(separator).saturating_add(width) > area.width {
            column = 0;
            if lines.len() >= usize::from(area.height) {
                break;
            }
        }
        if lines.is_empty() || column == 0 {
            lines.push(Line::default());
        }
        let line = lines.last_mut().expect("a footer line");
        if column > 0 {
            line.spans
                .push(Span::styled(" │ ", Style::default().fg(palette.muted)));
            column += 3;
        }
        line.spans.push(Span::styled(label, palette.keycap()));
        line.spans.push(Span::styled(
            format!(" {description}"),
            Style::default().fg(palette.muted),
        ));
        controls.0.push(Control {
            // Align one-row footers with the bottom of the frame too.
            area: Rect::new(area.x + column, area.y + lines.len() as u16 - 1, width, 1),
            key: hint.key,
        });
        column += width;
    }
    let height = lines.len() as u16;
    let offset = area.height.saturating_sub(height);
    for control in &mut controls.0[start..] {
        control.area.y += offset;
    }
    for (index, line) in lines.into_iter().enumerate() {
        frame.render_widget(
            Paragraph::new(line),
            Rect::new(area.x, area.y + offset + index as u16, area.width, 1),
        );
    }
    height
}

fn render_tabs(
    frame: &mut Frame<'_>,
    area: Rect,
    tabs: &[Tab<'_>],
    palette: Palette,
    controls: &mut Controls,
) {
    let mut column = 0_u16;
    for tab in tabs {
        let label = crate::ui::sanitize_for_terminal(tab.label).replace('\n', " ");
        let Ok(width) = u16::try_from(label.width().saturating_add(2)) else {
            break;
        };
        if column.saturating_add(width) > area.width {
            break;
        }
        let target = Rect::new(area.x + column, area.y, width, 1);
        let style = if tab.selected {
            palette.selection()
        } else {
            Style::default().fg(palette.muted)
        };
        frame.render_widget(Paragraph::new(format!(" {label} ")).style(style), target);
        controls.0.push(Control {
            area: target,
            key: tab.key,
        });
        column += width;
    }
}

/// A label with a right-aligned tag and an optional quiet description underneath.
/// Consumers choose a one-row compact layout when the terminal cannot fit two rows.
pub(crate) struct PickerRow<'a> {
    pub label: &'a str,
    pub description: Option<&'a str>,
    pub tag: Option<&'a str>,
    pub selected: bool,
    pub muted: bool,
}

impl PickerRow<'_> {
    pub fn render(&self, frame: &mut Frame<'_>, area: Rect, palette: Palette) {
        if area.width == 0 || area.height == 0 {
            return;
        }
        let base = if self.selected {
            palette.selection()
        } else {
            palette.base()
        };
        frame.render_widget(Block::default().style(base), area);
        let label_style = if self.muted {
            Style::default().fg(palette.muted)
        } else {
            Style::default().fg(palette.foreground)
        };
        let label_style = if self.selected {
            palette
                .selected_row(label_style)
                .add_modifier(Modifier::BOLD)
        } else {
            label_style
        };
        let detail_style = if self.selected {
            palette
                .selected_row(Style::default().fg(palette.muted))
                .remove_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(palette.muted)
        };
        // One column of padding belongs to the band, not to the label or its hit target.
        let content = Rect::new(
            area.x + 1,
            area.y,
            area.width.saturating_sub(2),
            area.height,
        );
        let tag = self
            .tag
            .map(|tag| crate::ui::sanitize_for_terminal(tag).replace('\n', " "));
        let tag_width = tag.as_ref().map_or(0, |tag| tag.width());
        let tag = tag
            .filter(|_| tag_width > 0 && tag_width.saturating_add(4) <= usize::from(content.width));
        let label_width = usize::from(content.width).saturating_sub(if tag.is_some() {
            tag_width + 2
        } else {
            0
        });
        frame.render_widget(
            Paragraph::new(Span::styled(
                terminal_row(self.label, label_width),
                label_style,
            )),
            content,
        );
        if let Some(tag) = tag {
            frame.render_widget(
                Paragraph::new(Span::styled(tag, detail_style)),
                Rect::new(
                    content.right() - tag_width as u16,
                    content.y,
                    tag_width as u16,
                    1,
                ),
            );
        }
        if area.height > 1 {
            if let Some(description) = self.description {
                frame.render_widget(
                    Paragraph::new(Span::styled(
                        terminal_row(description, usize::from(content.width)),
                        detail_style,
                    )),
                    Rect::new(content.x, content.y + 1, content.width, 1),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests;
