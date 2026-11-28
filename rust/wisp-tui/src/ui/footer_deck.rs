//! Two-row status footer for the wide conversation surface.

use super::{
    FooterNotice, clip_columns, context_fill_color, display_workspace, footer_model,
    footer_permission, notice_ticker_window, sanitize_for_terminal, spans_width, status_chip,
};
use crate::context_view;
use crate::keybindings::Bindings;
use crate::reducer::{AgentMode, UiState};
use crate::theme::{Palette, contrast_ratio};
use crate::transcript_view::{TranscriptViewport, Verbosity};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::McpServerStatus;

const ROW_GAP: usize = 2;
const CHIP_PADDING: usize = 2;

/// Reports whether the notice is scrolling so its lifetime can cover a full lap.
#[allow(clippy::too_many_arguments)]
pub(super) fn render_footer_deck(
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
    verbosity: Verbosity,
) -> bool {
    let width = usize::from(area.width);
    let mut badge = status_chip(state, palette);
    let room = beside(width, spans_width(&badge));
    let permission = footer_permission(state);
    let (notice, notice_scrolls) = activity_notice(state, notice, room, permission, palette);
    let identity_room = beside(room, spans_width(&notice));
    let mut left = identity(state, permission, identity_room, palette);
    append_activity(
        &mut left,
        activity(state, viewport, jump_painted, column_width, palette),
        identity_room,
        palette,
    );
    if !notice.is_empty() {
        let mut right = notice;
        right.push(Span::raw(" ".repeat(ROW_GAP)));
        right.append(&mut badge);
        badge = right;
    }
    render_row(frame, Rect { height: 1, ..area }, left, badge);

    let cost = context_view::session_cost(state);
    let cost_width = cost
        .as_deref()
        .map_or(0, |cost| cost.width() + CHIP_PADDING + 1);
    let minimum = context_view::indicator(state, 0).width() + CHIP_PADDING;
    let reserved = minimum + cost_width + ROW_GAP;
    let hint_room = if reserved + 16 <= width {
        width - reserved
    } else {
        width.saturating_sub(minimum + ROW_GAP)
    };
    let hint_room = if column_width < 120 {
        hint_room.min(56)
    } else {
        hint_room
    };
    let right = super::footer_keys(
        state,
        bindings,
        menu,
        column_width < frame.area().width,
        hint_room,
        palette,
    );
    let left = telemetry(
        state,
        cost,
        verbosity,
        beside(width, spans_width(&right)),
        palette,
    );
    render_row(
        frame,
        Rect {
            y: area.y + 1,
            height: 1,
            ..area
        },
        left,
        right,
    );
    notice_scrolls
}

fn beside(width: usize, occupied: usize) -> usize {
    width.saturating_sub(occupied + usize::from(occupied > 0) * ROW_GAP)
}

fn render_row(
    frame: &mut Frame<'_>,
    area: Rect,
    mut left: Vec<Span<'static>>,
    right: Vec<Span<'static>>,
) {
    let used = spans_width(&left) + spans_width(&right);
    left.push(Span::raw(
        " ".repeat(usize::from(area.width).saturating_sub(used)),
    ));
    left.extend(right);
    frame.render_widget(Paragraph::new(Line::from(left)), area);
}

fn activity_notice(
    state: &UiState,
    notice: Option<FooterNotice<'_>>,
    room: usize,
    permission: Option<&str>,
    palette: Palette,
) -> (Vec<Span<'static>>, bool) {
    let (text, shown_for) = match notice {
        Some(notice) => (Some(notice.text), Some(notice.shown_for)),
        None => (
            state
                .context
                .compaction_notice
                .as_deref()
                .filter(|_| state.context.compaction_notice_is_warning),
            None,
        ),
    };
    // Reserve the permission and space for a short identity even while a notice scrolls.
    let permission_minimum = permission.map_or(0, |label| label.width() + CHIP_PADDING + 1);
    let model_minimum = beside(room, permission_minimum + 8).min(12);
    let identity_minimum = permission_minimum + model_minimum;
    let notice_room = beside(room, identity_minimum).min(room / 2);
    let Some(text) = text.filter(|text| !text.is_empty() && notice_room > 0) else {
        return (Vec::new(), false);
    };
    let clean = sanitize_for_terminal(text);
    let ticker = shown_for.filter(|_| clean.width() > notice_room);
    let shown = match ticker {
        Some(shown_for) => {
            let mut window = notice_ticker_window(&clean, notice_room, shown_for);
            window.push_str(&" ".repeat(notice_room.saturating_sub(window.width())));
            window
        }
        None => clip_columns(&clean, notice_room),
    };
    (
        vec![Span::styled(shown, Style::default().fg(palette.warning))],
        ticker.is_some(),
    )
}

fn identity(
    state: &UiState,
    permission: Option<&'static str>,
    room: usize,
    palette: Palette,
) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let permission_width = permission.map_or(0, |label| label.width() + CHIP_PADDING);
    let mut model_room = room
        .saturating_sub(permission_width + usize::from(permission_width > 0))
        .saturating_sub(CHIP_PADDING);
    // Leave a little room for a folder or branch even when the model name is long.
    if state
        .workspace_cwd
        .as_deref()
        .is_some_and(|cwd| !cwd.is_empty())
        || state
            .git_branch
            .as_deref()
            .is_some_and(|branch| !branch.is_empty())
    {
        model_room -= model_room.saturating_sub(24).min(12);
    }
    let model = model_and_mode(state, model_room, palette);
    append_chip(&mut spans, model, palette.primary, palette);

    if let Some(label) = permission.filter(|_| permission_width <= room) {
        let style = if label == "Ask" {
            Style::default().fg(palette.foreground)
        } else {
            Style::default()
                .fg(palette.warning)
                .add_modifier(Modifier::BOLD)
        };
        append_chip(
            &mut spans,
            vec![Span::styled(label, style)],
            if state
                .permissions
                .snapshot
                .as_ref()
                .is_some_and(|p| p.mode == wisp_protocol::commands::PermissionMode::Yolo)
            {
                palette.warning
            } else {
                palette.accent
            },
            palette,
        );
    }
    let workspace_room =
        room.saturating_sub(spans_width(&spans) + usize::from(!spans.is_empty()) + CHIP_PADDING);
    if workspace_room >= 4 {
        let workspace = workspace(state, workspace_room, palette);
        append_chip(&mut spans, workspace, palette.selected, palette);
    }
    // Reuse the last segment's padding cell, including for a single segment.
    if let Some(end) = spans.last_mut() {
        *end = tip(end.style, Style::default(), palette);
    }
    spans
}

fn tip(from: Style, next: Style, palette: Palette) -> Span<'static> {
    if let Some(fill) = from.bg {
        let mut style = Style::default().fg(fill);
        style.bg = next.bg;
        Span::styled("\u{e0b0}", style)
    } else {
        Span::styled("❯", Style::default().fg(palette.muted).bold())
    }
}

fn model_and_mode(state: &UiState, room: usize, palette: Palette) -> Vec<Span<'static>> {
    let mode = state.mode_confirmed.then(|| state.mode.as_str());
    let mode_room = mode.map_or(0, |mode| mode.width() + 3);
    let mut model = footer_model(state);
    let model_room = room.saturating_sub(mode_room);
    if spans_width(&model) > model_room {
        if spans_width(&model[1..]) + 3 > model_room {
            // Effort can drop before the qualifier on a stale model name.
            let qualifier = state
                .model_selection_stale
                .then(|| model.remove(model.len() - 1));
            model.truncate(1);
            model.extend(qualifier);
        }
        let suffix_width = spans_width(&model[1..]);
        if suffix_width + 3 <= model_room {
            model[0].content = clip_columns(&model[0].content, model_room - suffix_width).into();
        } else {
            model.clear();
        }
    }
    if let Some(mode) = mode.filter(|mode| mode.width() <= room) {
        if !model.is_empty() {
            model.push(Span::styled(" · ", Style::default().fg(palette.muted)));
        }
        let fg = if state.mode == AgentMode::Plan {
            palette.warning
        } else {
            palette.primary
        };
        model.push(Span::styled(
            mode,
            Style::default().fg(fg).add_modifier(Modifier::BOLD),
        ));
    }
    model
}

fn workspace(state: &UiState, room: usize, palette: Palette) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    if let Some(cwd) = state.workspace_cwd.as_deref().filter(|cwd| !cwd.is_empty()) {
        let cwd = display_workspace(&sanitize_for_terminal(cwd));
        spans.push(Span::styled(
            clip_columns(&cwd, room),
            Style::default().fg(palette.foreground),
        ));
    }
    let remaining = room.saturating_sub(spans_width(&spans) + usize::from(!spans.is_empty()) * 3);
    if remaining >= 4
        && let Some(branch) = state
            .git_branch
            .as_deref()
            .filter(|branch| !branch.is_empty())
    {
        if !spans.is_empty() {
            spans.push(Span::styled(" · ", Style::default().fg(palette.muted)));
        }
        spans.push(Span::styled(
            clip_columns(
                &sanitize_for_terminal(branch),
                remaining.min(super::BRANCH_MAX_COLUMNS),
            ),
            Style::default().fg(palette.primary),
        ));
    }
    spans
}

fn append_chip(
    row: &mut Vec<Span<'static>>,
    content: Vec<Span<'static>>,
    background: Color,
    palette: Palette,
) {
    if spans_width(&content) == 0 {
        return;
    }
    let base = palette.footer_segment(background);
    let style = |style: Style| {
        let preferred = style
            .fg
            .filter(|fg| base.bg.is_none_or(|fill| contrast_ratio(*fg, fill) >= 4.5));
        let ink = preferred.or(base.fg).unwrap_or(palette.foreground);
        style.patch(base).fg(ink)
    };
    if let Some(previous) = row.last() {
        row.push(tip(previous.style, base, palette));
    }
    row.push(Span::styled(" ", style(Style::default())));
    row.extend(content.into_iter().map(|span| {
        let span_style = style(span.style);
        span.style(span_style)
    }));
    row.push(Span::styled(" ", style(Style::default())));
}

fn append_activity(
    left: &mut Vec<Span<'static>>,
    metadata: Vec<Span<'static>>,
    room: usize,
    palette: Palette,
) {
    let mut used = spans_width(left);
    for span in metadata {
        let separator = if used == 0 { "" } else { " · " };
        let needed = separator.width() + span.width();
        if used + needed <= room {
            left.push(Span::styled(separator, Style::default().fg(palette.muted)));
            left.push(span);
            used += needed;
        }
    }
}

fn activity(
    state: &UiState,
    viewport: &TranscriptViewport,
    jump_painted: bool,
    column_width: u16,
    palette: Palette,
) -> Vec<Span<'static>> {
    let muted = Style::default().fg(palette.muted);
    let mut spans = Vec::new();
    let (degraded, connected) = mcp_health_counts(state);
    if degraded > 0 {
        spans.push(Span::styled(
            format!("{degraded} MCP off"),
            Style::default()
                .fg(palette.error)
                .add_modifier(Modifier::BOLD),
        ));
    }
    if state.active_prompt_editable() {
        let steering = state.queued_steering();
        let follow_up = state.queued_follow_ups();
        let omitted = steering.saturating_add(follow_up).saturating_sub(3);
        let mut text = format!("s:{steering}/l:{follow_up}");
        if omitted > 0 {
            text.push_str(&format!(" +{omitted}"));
        }
        spans.push(Span::styled(text, muted));
    }
    if let Some(reason) = state.context.compaction {
        spans.push(Span::styled(
            format!("compacting ({})", reason.as_str()),
            Style::default().fg(palette.primary),
        ));
    }
    if let Some(marker) = super::scroll_marker(state, viewport, jump_painted) {
        spans.push(Span::styled(marker, muted));
    }
    let tasks = match (
        state.background_processes.running().len(),
        active_subagent_count(state),
    ) {
        (0, 0) => None,
        (bg, 0) => Some(format!("{bg} bg")),
        (0, sub) => Some(format!("{sub} sub")),
        (bg, sub) => Some(format!("{bg} bg · {sub} sub")),
    };
    if let Some(tasks) = tasks {
        spans.push(Span::styled(
            tasks,
            Style::default()
                .fg(palette.primary)
                .add_modifier(Modifier::BOLD),
        ));
    }
    if degraded == 0 && connected > 0 {
        spans.push(Span::styled(format!("{connected} MCP"), muted));
    }
    if column_width >= 120
        && let Some(session) = state.selected_session.as_ref()
    {
        let short: String = sanitize_for_terminal(&session.session_id)
            .chars()
            .take(8)
            .collect();
        if !short.is_empty() {
            spans.push(Span::styled(format!("#{short}"), muted));
        }
    }
    spans
}

fn telemetry(
    state: &UiState,
    cost: Option<String>,
    verbosity: Verbosity,
    room: usize,
    palette: Palette,
) -> Vec<Span<'static>> {
    let minimum = context_view::indicator(state, 0).width() + CHIP_PADDING;
    let cost = cost.filter(|cost| minimum + 1 + cost.width() + CHIP_PADDING <= room);
    let cost_width = cost
        .as_deref()
        .map_or(0, |cost| cost.width() + CHIP_PADDING + 1);
    let ctx_room = room.saturating_sub(cost_width + CHIP_PADDING);
    let indicator = context_view::indicator(state, ctx_room);
    let mut spans = Vec::new();
    if indicator.width() <= ctx_room {
        let fill = context_view::fill(state);
        let fg = if fill.is_some() && context_fill_color(fill, palette) == palette.muted {
            palette.accent
        } else {
            context_fill_color(fill, palette)
        };
        let style = palette.footer_chip(fg);
        let used = indicator.width();
        let mut body = vec![Span::raw(indicator)];
        if fill.is_some() {
            let ratio = format!(" ({})", context_view::status_text(state));
            if used + ratio.width() <= ctx_room {
                body.push(Span::styled(ratio, palette.footer_chip(palette.muted)));
            }
        }
        spans.extend(super::footer_pill(body, style));
    }
    if let Some(cost) = cost {
        spans.push(Span::raw(" "));
        spans.extend(super::footer_pill(
            [Span::raw(cost)],
            palette.footer_chip(palette.code),
        ));
    }
    if verbosity != Verbosity::Medium {
        let label = format!("verbosity: {}", verbosity.name());
        let remaining = room.saturating_sub(spans_width(&spans) + 2);
        let text = if label.width() <= remaining {
            &label
        } else {
            verbosity.name()
        };
        if text.width() <= remaining {
            spans.push(Span::styled(
                format!("  {text}"),
                Style::default().fg(palette.secondary),
            ));
        }
    }
    spans
}

fn active_subagent_count(state: &UiState) -> usize {
    state
        .transcript
        .entries()
        .iter()
        .filter(|entry| {
            entry.tool_card().is_some_and(|card| {
                !card.status.terminal()
                    && card.script.as_deref().is_some_and(|script| {
                        script.host == crate::script_card::NestedHost::Subagent
                    })
            })
        })
        .count()
}

fn mcp_health_counts(state: &UiState) -> (usize, usize) {
    let Some(snapshot) = state.mcp.snapshot.as_ref() else {
        return (0, 0);
    };
    let mut degraded = 0;
    let mut connected = 0;
    for server in &snapshot.servers {
        match server.status {
            McpServerStatus::Disconnected | McpServerStatus::Unavailable => degraded += 1,
            McpServerStatus::Connected => connected += 1,
            McpServerStatus::Connecting | McpServerStatus::Disabled => {}
        }
    }
    (degraded, connected)
}

#[cfg(test)]
mod tests {
    use super::render_footer_deck;
    use crate::color_depth::ColorDepth;
    use crate::keybindings::Bindings;
    use crate::reducer::{AgentMode, UiState, ViewStatus};
    use crate::theme::{self, Palette, contrast_ratio};
    use crate::transcript_view::{TranscriptViewport, Verbosity};
    use crate::ui::FooterNotice;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::layout::Rect;
    use std::sync::Arc;
    use std::time::Duration;
    use wisp_protocol::commands::PermissionMode;
    use wisp_protocol::events::PermissionState;

    #[test]
    fn docked_notice_keeps_scope() {
        let mut state = UiState::new("fake".into(), Some("long-model".repeat(8)), None);
        state.mode = AgentMode::Plan;
        state.mode_confirmed = true;
        state.view_status = ViewStatus::Error;
        state.permissions.snapshot = Some(Arc::new(PermissionState {
            mode: PermissionMode::AutoCommands,
            saved_mode: Some(PermissionMode::Ask),
            project_path: None,
            session_only_yolo: false,
            command_allow_rules: Vec::new(),
        }));
        let mut terminal = Terminal::new(TestBackend::new(100, 2)).unwrap();
        let area = Rect::new(2, 0, 58, 2);
        terminal
            .draw(|frame| {
                let scrolls = render_footer_deck(
                    frame,
                    area,
                    &state,
                    &TranscriptViewport::default(),
                    Some(FooterNotice {
                        text: "Credentials expired. Reconnect the provider to continue.",
                        shown_for: Duration::ZERO,
                    }),
                    Palette::default(),
                    &Bindings::default(),
                    false,
                    false,
                    62,
                    Verbosity::Medium,
                );
                assert!(scrolls);
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        let row: String = (area.left()..area.right())
            .map(|x| buffer[(x, 0)].symbol())
            .collect();
        assert!(row.contains("Auto (commands, session)"), "{row}");
        assert!(row.contains("plan"), "{row}");
        assert!(row.contains("Cred"), "{row}");
        assert!(row.trim_end().ends_with("\u{e0b6}✗ error\u{e0b4}"), "{row}");
    }

    #[test]
    fn identity_fill_reads_across_themes() {
        let mut state = UiState::new("fake".into(), Some("demo-model".into()), None);
        state.workspace_cwd = Some("/src/demo".into());
        for theme in theme::themes() {
            for (depth, no_color) in [
                (ColorDepth::TrueColor, false),
                (ColorDepth::Ansi256, false),
                (ColorDepth::Ansi16, false),
                (ColorDepth::TrueColor, true),
            ] {
                let palette = theme.palette(no_color).for_depth(depth);
                let mut terminal = Terminal::new(TestBackend::new(80, 2)).unwrap();
                terminal
                    .draw(|frame| {
                        render_footer_deck(
                            frame,
                            frame.area(),
                            &state,
                            &TranscriptViewport::default(),
                            None,
                            palette,
                            &Bindings::default(),
                            false,
                            false,
                            80,
                            Verbosity::Medium,
                        );
                    })
                    .unwrap();
                let buffer = terminal.backend().buffer();
                let row = |y| (0..80).map(|x| buffer[(x, y)].symbol()).collect::<String>();
                let identity = row(0);
                let metrics = row(1);
                let filled = !no_color && depth != ColorDepth::Ansi16;
                let (arrow, status, context) = if filled {
                    (
                        '\u{e0b0}',
                        "\u{e0b6}• idle\u{e0b4}",
                        "\u{e0b6}ctx ~?\u{e0b4}",
                    )
                } else {
                    ('❯', "◖• idle◗", "◖ctx ~?◗")
                };
                assert!(identity.contains(status), "{}: {identity}", theme.label);
                assert_eq!(buffer[(40, 0)].bg, ratatui::style::Color::Reset);
                assert!(metrics.contains(context), "{metrics}");
                assert_eq!(identity.matches(arrow).count(), 2, "{identity}");
                let model = &buffer[(1, 0)];
                assert_eq!(model.symbol(), "d");
                if filled {
                    assert_eq!(model.bg, palette.primary);
                    assert!(contrast_ratio(model.fg, model.bg) >= 4.5, "{model:?}");
                } else {
                    assert_eq!(model.bg, ratatui::style::Color::Reset);
                }
            }
        }
    }
}
