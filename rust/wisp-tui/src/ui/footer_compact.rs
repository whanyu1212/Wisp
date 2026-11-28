//! Single-row status for short terminals and narrow conversation columns.
use super::{
    FooterNotice, decision_pending, footer_model, footer_permission, notice_ticker_window,
    sanitize_for_terminal, status_chip, status_label,
};
use crate::{
    context_view,
    keybindings::Bindings,
    reducer::{AgentMode, UiState},
    theme::Palette,
    transcript_view::TranscriptViewport,
};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    text::{Line, Span},
    widgets::Paragraph,
};
use unicode_width::UnicodeWidthStr;

fn permission_style(state: &UiState, palette: Palette) -> Style {
    let color = match state
        .permissions
        .snapshot
        .as_ref()
        .map(|snapshot| snapshot.mode)
    {
        Some(wisp_protocol::commands::PermissionMode::Yolo) => palette.warning,
        _ => palette.accent,
    };
    palette.footer_segment(color).bold()
}

/// Prefer the full chip and scope. At the minimum width, shorten both labels
/// before letting them displace a notice or the context reading.
fn leading(state: &UiState, palette: Palette, budget: usize) -> Line<'static> {
    let mut line = Line::from(status_chip(state, palette));
    let permission = footer_permission(state);
    if let Some(label) = permission {
        line.push_span(" ");
        line.spans.extend(super::footer_pill(
            [Span::raw(label)],
            permission_style(state, palette),
        ));
    }
    if line.width() <= budget {
        return line;
    }

    let status = status_label(state);
    let mut short_status = status.as_str();
    let mut scope = permission.unwrap_or("");
    if short_status.width() + 1 + scope.width() > budget {
        short_status = match short_status {
            "compacting" => "compact",
            "configuring" => "config",
            other => other,
        };
        scope = match scope {
            "Auto (commands, session)" => "auto cmds/session",
            "Auto (edits, session)" => "auto edits/session",
            "Auto (commands)" => "auto cmds",
            "Auto (edits)" => "auto edits",
            "YOLO (session)" => "YOLO/session",
            other => other,
        };
    }
    // In the most crowded case, the scope still names the action and lifetime.
    if short_status.width() + 1 + scope.width() > budget {
        scope = scope.strip_prefix("auto ").unwrap_or(scope);
    }
    let mut line = Line::styled(
        short_status.to_owned(),
        if decision_pending(state) {
            palette.footer_segment(palette.warning).bold()
        } else {
            palette.footer_chip(super::status_color(state, palette))
        },
    );
    if !scope.is_empty() {
        line.push_span(" ");
        line.push_span(Span::styled(scope, permission_style(state, palette)));
    }
    line
}

fn append_if_fits(line: &mut Line<'static>, span: Span<'static>, budget: usize) {
    if line.width() + 1 + span.width() <= budget {
        line.push_span(" ");
        line.push_span(span);
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    state: &UiState,
    viewport: &TranscriptViewport,
    notice: Option<FooterNotice<'_>>,
    palette: Palette,
    bindings: &Bindings,
    jump_painted: bool,
    menu: bool,
    column_width: u16,
) -> bool {
    let width = usize::from(area.width);
    let ctx_text = context_view::indicator(state, 0);
    let notice_active = notice.is_some() || state.context.compaction_notice_is_warning;
    // Prefer unpadded status badges over dropping the context reading altogether.
    let leading_room = width.saturating_sub(if notice_active {
        12
    } else {
        ctx_text.width() + 1
    });
    let mut left = leading(state, palette, leading_room);
    let room = width.saturating_sub(left.width() + 1);
    let warning = state
        .context
        .compaction_notice
        .as_deref()
        .filter(|_| state.context.compaction_notice_is_warning);
    let notice_text = notice.map(|n| n.text).or(warning);
    if let Some(text) = notice_text.filter(|_| room > 0) {
        let text = sanitize_for_terminal(text);
        let ticker = notice.filter(|_| text.width() > room);
        let text = match ticker {
            Some(n) => notice_ticker_window(&text, room, n.shown_for),
            None => super::clip_columns(&text, room),
        };
        left.push_span(" ");
        left.push_span(Span::styled(text, Style::default().fg(palette.warning)));
        frame.render_widget(Paragraph::new(left), area);
        return ticker.is_some();
    }

    let ctx_fits = ctx_text.width() <= room;
    let ctx_style = palette.footer_chip(super::context_fill_color(
        context_view::fill(state),
        palette,
    ));
    let ctx = if ctx_text.width() + 2 <= room {
        Line::from(super::footer_pill([Span::raw(ctx_text)], ctx_style))
    } else {
        Line::from(Span::styled(ctx_text, ctx_style))
    };
    let left_budget = if ctx_fits {
        width - ctx.width() - 1
    } else {
        width
    };
    if state.mode_confirmed {
        let color = if state.mode == AgentMode::Plan {
            palette.warning
        } else {
            palette.primary
        };
        append_if_fits(
            &mut left,
            Span::styled(state.mode.as_str(), Style::default().fg(color)),
            left_budget,
        );
    }
    if let Some(marker) = super::scroll_marker(state, viewport, jump_painted) {
        append_if_fits(
            &mut left,
            Span::styled(marker, Style::default().fg(palette.muted)),
            left_budget,
        );
    }

    // At wide-but-short sizes keep identity if the whole label fits. At narrow
    // sizes reclaim its space for the draft and its available actions.
    if column_width >= 80 {
        let model = Line::from(footer_model(state));
        if model.width() > 0 && left.width() + 1 + model.width() <= left_budget.saturating_sub(16) {
            left.push_span(" ");
            left.spans.extend(model.spans);
        }
    }
    let hint_room = left_budget.saturating_sub(left.width() + 2);
    let mut right = Line::from(super::footer_keys(
        state,
        bindings,
        menu,
        column_width < frame.area().width,
        hint_room,
        palette,
    ));
    if ctx_fits {
        if right.width() > 0 {
            right.push_span(" ");
        }
        right.spans.extend(ctx.spans);
    }
    let right_width = right.width() as u16;
    frame.render_widget(Paragraph::new(left), area);
    frame.render_widget(
        Paragraph::new(right),
        Rect::new(area.right() - right_width, area.y, right_width, 1),
    );
    false
}
