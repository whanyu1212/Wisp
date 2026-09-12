//! Read-only contextual help layered over, without closing, the focused workflow.

use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    text::Line,
    widgets::{Block, Borders, Paragraph},
};

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::{
    OverlayKind, RenderedDecisionContext, commands::Help, keybindings::Bindings, theme::Palette,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum Owner {
    Composer,
    Browse,
    Files,
    Completion,
    Overlay(OverlayKind),
    Decision(RenderedDecisionContext),
}

pub(crate) struct KeyHelp {
    pub owner: Owner,
    pub scroll: Help,
    update_guidance: bool,
    pub rendered_rows: usize,
}

impl KeyHelp {
    pub fn new(owner: Owner) -> Self {
        Self {
            owner,
            scroll: Help::default(),
            update_guidance: false,
            rendered_rows: 0,
        }
    }

    pub fn updates(owner: Owner) -> Self {
        Self {
            update_guidance: true,
            ..Self::new(owner)
        }
    }

    pub fn rows(&self, bindings: &Bindings) -> Vec<Line<'static>> {
        if self.update_guidance {
            return [
                "External update only; this command preserves your draft and runs no update.",
                "1. Finish your work and quit Wisp.",
                "2. Run `wisp update --check` in your shell to check availability.",
                "3. Eligible uv tool installations can run `wisp update` to install.",
                "4. Source installations: follow the development guide's update/build steps.",
                "5. Rebuild or select a Rust TUI binary matching the updated Python version before relaunching.",
                "The Rust TUI never installs updates or restarts itself. Binary distribution and rollback remain separate work.",
                "Up/Down/Page/Home/End scroll. Ctrl+G / Esc closes; Ctrl+C retains cancellation behavior.",
            ].into_iter().map(Line::raw).collect();
        }
        let mut rows = vec![Line::raw(
            "Ctrl+G / Esc closes help; Ctrl+C retains cancellation behavior.",
        )];
        let fixed: &[&str] = match &self.owner {
            Owner::Decision(RenderedDecisionContext::Approval(_)) => &[
                "Approval: y once, t tool/session, a all/session; n denies.",
                "Positive decisions are disabled while this help covers the request.",
                "n denies immediately; close help to read and approve the request.",
            ],
            Owner::Decision(RenderedDecisionContext::Trust(_)) => &[
                "Project trust: y trusts, n denies. Close help before trusting.",
                "n denies immediately; Ctrl+C cancels the waiting operation.",
            ],
            Owner::Files => &[
                "Files: Up/Down select, Enter inserts, Tab switches fuzzy/tree.",
                "Tree: Left/Right navigate folders. Esc dismisses; modified submit keeps its meaning.",
            ],
            Owner::Completion => &[
                "Completion: Up/Down select, Tab or submit fills a partial command.",
                "Enter activates an exact command; Escape dismisses suggestions.",
            ],
            Owner::Browse => {
                &["Details: Tab/Shift+Tab select, Enter/Space opens, Esc returns to prompt."]
            }
            Owner::Overlay(OverlayKind::Connection) => &[
                "Connections: Up/Down select, Enter starts/submits, d disconnects, r refreshes.",
                "API-key input is masked. Escape closes/cancels the connection workflow.",
            ],
            Owner::Overlay(OverlayKind::Model) => &[
                "Models: Up/Down/Page/Home/End select; Left/Right choose effort.",
                "Enter applies, r refreshes, Esc/Ctrl+C closes.",
            ],
            Owner::Overlay(OverlayKind::Theme) => &[
                "Themes: Up/Down/Page/Home/End preview; Enter applies.",
                "Esc/Ctrl+C restores the committed theme; theme toggle is ignored during preview.",
            ],
            Owner::Overlay(OverlayKind::PromptHistory) => &[
                "History: type to search; Up/Down/Page/Home/End select.",
                "Enter restores without submitting; Esc/Ctrl+C closes.",
            ],
            Owner::Overlay(OverlayKind::SessionTree) => &[
                "Tree: Up/Down/Page/Home/End select; Enter navigates; f forks; Esc closes.",
                "Ctrl+C retains normal cancellation/exit behavior.",
            ],
            Owner::Overlay(OverlayKind::Session) => &[
                "Sessions: Up/Down/Page/Home/End select; Enter resumes; Esc closes.",
                "Ctrl+C retains normal cancellation/exit behavior.",
            ],
            Owner::Overlay(OverlayKind::Discovery) => &[
                "Skills/MCP: arrows and Page/Home/End navigate; r refreshes.",
                "Enter inserts a selected skill without submitting. Esc/Ctrl+C closes.",
            ],
            Owner::Overlay(OverlayKind::Context | OverlayKind::Help) => {
                &["Up/Down/Page/Home/End scroll; r refreshes; Esc/Ctrl+C closes."]
            }
            Owner::Overlay(OverlayKind::Detail) => &[
                "Details: arrows/Page/Home/End scroll; Esc returns to card browse.",
                "Ctrl+C retains normal cancellation/exit behavior.",
            ],
            Owner::Composer => &[
                "Editor: arrows, Home/End, Ctrl+A/E, Backspace/Delete and Tab retain editing behavior.",
                "Large-paste markers expand on first cursor/edit interaction.",
                "Esc cancels active work; Ctrl+C cancels or exits when idle.",
            ],
        };
        rows.extend(fixed.iter().map(|text| Line::raw(*text)));
        if !matches!(self.owner, Owner::Decision(_)) {
            rows.push(Line::raw(
                "Application shortcuts (focused popup controls take precedence):",
            ));
            rows.extend(
                bindings
                    .help_entries()
                    .into_iter()
                    .map(|entry| Line::raw(format!("{} — {}", entry.label, entry.description))),
            );
        }
        rows
    }

    pub fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        bindings: &Bindings,
        palette: Palette,
    ) {
        crate::ui::clear_overlay(frame, area, palette);
        let block = Block::default()
            .borders(Borders::ALL)
            .title(if self.update_guidance {
                " Update instructions · Ctrl+G / Esc close "
            } else {
                " Keys · Ctrl+G / Esc close "
            })
            .style(
                Style::default()
                    .fg(palette.foreground)
                    .bg(palette.background),
            );
        let inner = block.inner(area);
        frame.render_widget(block, area);
        // Materialize visual rows so even a long configured alias list can be
        // scrolled a row at a time at the minimum supported terminal size.
        let mut rows = Vec::new();
        for line in self.rows(bindings) {
            let source = line.to_string();
            let mut row = String::new();
            let mut columns = 0;
            for grapheme in source.graphemes(true) {
                let cells = grapheme.width();
                if columns + cells > usize::from(inner.width) && !row.is_empty() {
                    rows.push(Line::raw(std::mem::take(&mut row)));
                    columns = 0;
                }
                row.push_str(grapheme);
                columns += cells;
            }
            rows.push(Line::raw(row));
        }
        self.rendered_rows = rows.len();
        self.scroll.offset = self.scroll.offset.min(rows.len().saturating_sub(1));
        frame.render_widget(
            Paragraph::new(
                rows.into_iter()
                    .skip(self.scroll.offset)
                    .collect::<Vec<_>>(),
            ),
            inner,
        );
    }
}
