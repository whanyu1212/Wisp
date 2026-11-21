//! The top bar: one row across the top of every frame, with the session's title at
//! its left and a tab for each side-panel view at its right. Everything else lays out
//! in the body beneath it.

use crate::reducer::UiState;
use crate::side_panel::{self, PanelMode, SidePanel};
use crate::theme::Palette;
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use unicode_width::UnicodeWidthStr;

/// Blank columns at each end of the bar.
const MARGIN: u16 = 1;

/// A painted tab, which a left click on `area` activates.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Tab {
    pub area: Rect,
    pub mode: PanelMode,
}

/// Split a frame into the top bar's row and the body beneath it, where the
/// conversation, the side panel, and popups lay out.
pub(crate) fn split(frame: Rect) -> (Rect, Rect) {
    let height = frame.height.min(1);
    (
        Rect { height, ..frame },
        Rect {
            y: frame.y + height,
            height: frame.height - height,
            ..frame
        },
    )
}

/// Draw the bar into `area` and return the tabs it painted. A tab is marked active
/// while the panel shows its view; the title is clipped so every tab fits.
pub(crate) fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    panel: &SidePanel,
    palette: Palette,
) -> Vec<Tab> {
    frame.render_widget(Paragraph::new(Line::default()).style(palette.base()), area);
    let inner = Rect {
        x: area.x + MARGIN.min(area.width),
        width: area.width.saturating_sub(2 * MARGIN),
        ..area
    };
    let active = panel.active_tab();
    // Each tab pads its label by a column on both sides, which its click target
    // includes, and tabs sit a column apart against the right margin.
    let mut spans = Vec::new();
    let mut targets = Vec::new();
    let mut offset = 0u16;
    for mode in PanelMode::ALL {
        if !spans.is_empty() {
            spans.push(Span::raw(" "));
            offset += 1;
        }
        let label = format!(" {} ", mode.label());
        let width = label.width() as u16;
        let style = if active == Some(mode) {
            Style::default()
                .fg(palette.primary)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(palette.muted)
        };
        spans.push(Span::styled(label, style));
        targets.push((offset, width, mode));
        offset += width;
    }
    // `spread` clips the title to leave room for the tabs.
    let title = Span::styled(
        side_panel::session_title(state),
        Style::default().add_modifier(Modifier::BOLD),
    );
    frame.render_widget(
        Paragraph::new(side_panel::spread(
            vec![title],
            spans,
            usize::from(inner.width),
        )),
        inner,
    );
    // The tabs end at the right margin; at the 30-column minimum the title keeps
    // several columns beside them.
    let start = inner.right().saturating_sub(offset);
    targets
        .into_iter()
        .map(|(offset, width, mode)| Tab {
            area: Rect {
                x: start + offset,
                width,
                ..area
            }
            .intersection(inner),
            mode,
        })
        .filter(|tab| !tab.area.is_empty())
        .collect()
}
