//! Compact live-work summaries for context, items needing attention, and
//! inventories (`7 MCP · 12 skills · 2 changed`).

use super::*;
use crate::context_split::compact_tokens_upper;

/// Items that need attention, stated in words (`Disconnected · /mcp`), right-aligned.
/// Emits nothing when every server, skill catalog, and diff snapshot is healthy.
pub(super) fn attention_rows(
    rows: &mut PanelRows,
    state: &UiState,
    panel: &SidePanel,
    width: usize,
    palette: Palette,
) {
    let muted = Style::default().fg(palette.muted);
    let mut entries: Vec<(Line<'static>, PanelAction)> = Vec::new();

    if let Some(snapshot) = state.mcp.snapshot.as_ref() {
        for server in &snapshot.servers {
            let (label, color) = match server.status {
                McpServerStatus::Disconnected => ("Disconnected · /mcp", palette.error),
                McpServerStatus::Unavailable => ("Unavailable · /mcp", palette.error),
                McpServerStatus::Connecting => ("Connecting · /mcp", palette.muted),
                McpServerStatus::Connected | McpServerStatus::Disabled => continue,
            };
            entries.push((
                spread(
                    vec![Span::raw(sanitize_for_terminal(&server.name))],
                    vec![Span::styled(label, Style::default().fg(color))],
                    width,
                ),
                PanelAction::Open(Target::Mcp),
            ));
        }
    } else if state.mcp.error.is_some() {
        entries.push((
            spread(
                vec![Span::raw("MCP")],
                vec![Span::styled(
                    "Unavailable · /mcp",
                    Style::default().fg(palette.error),
                )],
                width,
            ),
            PanelAction::Open(Target::Mcp),
        ));
    }

    if let Some(catalog) = state.skills.snapshot.as_ref() {
        if !catalog.diagnostics.is_empty() {
            let count = catalog.diagnostics.len();
            let word = if count == 1 {
                "diagnostic"
            } else {
                "diagnostics"
            };
            entries.push((
                spread(
                    vec![Span::raw("Skills")],
                    vec![Span::styled(
                        format!("{count} {word} · /skills"),
                        Style::default().fg(palette.warning),
                    )],
                    width,
                ),
                PanelAction::Open(Target::Skills),
            ));
        } else if !catalog.project_trusted {
            entries.push((
                spread(
                    vec![Span::raw("Project skills")],
                    vec![Span::styled("Awaiting trust · /skills", muted)],
                    width,
                ),
                PanelAction::Open(Target::Skills),
            ));
        }
    } else if state.skills.error.is_some() {
        entries.push((
            spread(
                vec![Span::raw("Skills")],
                vec![Span::styled(
                    "Unavailable · /skills",
                    Style::default().fg(palette.error),
                )],
                width,
            ),
            PanelAction::Open(Target::Skills),
        ));
    }

    if panel.files.is_none() && state.git_diff.error.is_some() {
        entries.push((
            spread(
                vec![Span::raw("Changed files")],
                vec![Span::styled(
                    "Unavailable · /diff",
                    Style::default().fg(palette.warning),
                )],
                width,
            ),
            PanelAction::Open(Target::Diff(None)),
        ));
    }

    if entries.is_empty() {
        return;
    }

    rows.section(
        Section::Attention,
        vec![Line::from(heading("Attention", palette))],
    );
    for (line, action) in entries {
        rows.push(line, Some(action));
    }
}

/// Context as one compact text line (`55.5K · 14% context`) that turns amber, then
/// red, as it fills. Hidden until context stats are known.
pub(super) fn context_line(rows: &mut PanelRows, state: &UiState, width: usize, palette: Palette) {
    let Some(split) = Split::of(state) else {
        return;
    };
    let percent = split
        .window
        .map(|window| split.total as f64 / window as f64 * 100.0);
    let prefix = if split.approximate { "~" } else { "" };
    let tokens = compact_tokens_upper(split.total);
    let summary = match percent {
        Some(pct) => format!("{prefix}{tokens} · {pct:.0}% context"),
        None => format!("{prefix}{tokens} context"),
    };
    let color = ui::context_fill_color(percent.map(|p| p / 100.0), palette);
    let muted = Style::default().fg(palette.muted);
    let right = match crate::context_view::session_cost(state) {
        Some(cost) => format!("{cost} · /context"),
        None => "/context".to_owned(),
    };
    rows.section(
        Section::Context,
        vec![spread(
            vec![Span::styled(summary, Style::default().fg(color))],
            vec![Span::styled(right, muted)],
            width,
        )],
    );
    rows.action(0, PanelAction::Open(Target::Context));
}

/// Inventories condensed onto one summary line (`7 MCP · 12 skills · 2 changed`),
/// where each part opens its dialog (`/mcp`, `/skills`, `/diff`). Empty or unloaded
/// inventories are omitted; if none have items, the line disappears.
pub(super) fn inventory_summary(
    rows: &mut PanelRows,
    state: &UiState,
    panel: &SidePanel,
    width: usize,
    palette: Palette,
) {
    let mut parts: Vec<(String, Target)> = Vec::new();
    if let Some(snapshot) = state.mcp.snapshot.as_ref() {
        let count = snapshot.servers.len();
        if count > 0 {
            parts.push((format!("{count} MCP"), Target::Mcp));
        }
    }
    if let Some(catalog) = state.skills.snapshot.as_ref() {
        let count = catalog.entries.len();
        if count > 0 {
            let word = if count == 1 { "skill" } else { "skills" };
            parts.push((format!("{count} {word}"), Target::Skills));
        }
    }
    if let Some(summary) = &panel.files {
        let count = summary.snapshot.files.len();
        let partial = summary.snapshot.truncated;
        if count > 0 || partial {
            let plus = if partial { "+" } else { "" };
            parts.push((format!("{count}{plus} changed"), Target::Diff(None)));
        }
    }
    if parts.is_empty() {
        return;
    }

    let muted = Style::default().fg(palette.muted);
    let sep = " · ";
    let mut spans = Vec::new();
    let mut hits: Vec<(u16, u16, PanelAction)> = Vec::new();
    let mut col = 0usize;

    for (index, (label, target)) in parts.into_iter().enumerate() {
        if index > 0 {
            if col + sep.width() >= width {
                break;
            }
            spans.push(Span::styled(sep, muted));
            col += sep.width();
        }
        let room = width.saturating_sub(col);
        if room == 0 {
            break;
        }
        let clipped = ui::clip_columns(&label, room);
        let span_width = clipped.width();
        if span_width == 0 {
            break;
        }
        let start = u16::try_from(col).unwrap_or(u16::MAX);
        let end = u16::try_from(col + span_width).unwrap_or(u16::MAX);
        spans.push(Span::styled(clipped, muted));
        hits.push((start, end, PanelAction::Open(target)));
        col += span_width;
    }

    if spans.is_empty() {
        return;
    }
    rows.section(Section::Inventory, vec![Line::from(spans)]);
    for (start, end, action) in hits {
        rows.action_range(0, start, end, action);
    }
}
