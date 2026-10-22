//! The Reading layout's compact context and named inventories.

use super::*;
use unicode_segmentation::UnicodeSegmentation;

fn toggle(disclosure: Disclosure) -> Option<PanelAction> {
    Some(PanelAction::Toggle(disclosure))
}

fn disclosure_title(title: &str, open: bool, palette: Palette) -> Vec<Span<'static>> {
    vec![
        Span::styled(
            if open { "▾ " } else { "▸ " },
            Style::default().fg(palette.muted),
        ),
        heading(title, palette),
    ]
}

pub(super) fn reading_context(
    rows: &mut PanelRows,
    state: &UiState,
    panel: &SidePanel,
    width: usize,
    palette: Palette,
) {
    let muted = Style::default().fg(palette.muted);
    let split = Split::of(state);
    let percent = split.as_ref().and_then(|split| {
        split
            .window
            .map(|window| split.total as f64 / window as f64 * 100.0)
    });
    rows.section(
        Section::Context,
        vec![spread(
            vec![heading("Context", palette)],
            vec![Span::styled(
                percent.map_or_else(
                    || {
                        if state.context.loading() {
                            "reading…"
                        } else {
                            "unknown"
                        }
                        .to_owned()
                    },
                    |percent| format!("{percent:.0}%"),
                ),
                if percent.is_some_and(|p| p >= ui::CONTEXT_WARNING_FILL * 100.0) {
                    Style::default().fg(palette.warning)
                } else {
                    muted
                },
            )],
            width,
        )],
    );
    if let Some(split) = &split {
        if let Some(window) = split.window {
            // One calm meter; the detailed source split belongs behind Breakdown.
            let filled = ((split.total as f64 / window as f64) * width as f64)
                .round()
                .clamp(0.0, width as f64) as usize;
            rows.push(
                Line::from(vec![
                    Span::styled("━".repeat(filled), Style::default().fg(palette.secondary)),
                    Span::styled(
                        "─".repeat(width - filled),
                        Style::default().fg(palette.rail()),
                    ),
                ]),
                None,
            );
        }
        let prefix = if split.approximate { "~" } else { "" };
        let usage = format!(
            "{prefix}{}{}",
            abbreviated(split.total),
            split
                .window
                .map(|w| format!(" / {}", abbreviated(w)))
                .unwrap_or_default()
        );
        let growth = if state.context.compaction.is_some() {
            None
        } else if ui::run_in_status_line(state) {
            split
                .running_turn()
                .map(|(_, n)| format!("+{} so far", abbreviated(n)))
        } else {
            split
                .finished_turn()
                .map(|(_, n)| format!("+{} last turn", abbreviated(n)))
        };
        rows.push(
            spread(
                vec![Span::styled(usage, muted)],
                growth
                    .map(|s| vec![Span::styled(s, muted)])
                    .unwrap_or_default(),
                width,
            ),
            None,
        );
    }
    rows.push(
        Line::from(disclosure_title(
            "Breakdown · /context",
            panel.context_expanded,
            palette,
        )),
        toggle(Disclosure::Context),
    );
    if panel.context_expanded {
        if let Some(split) = &split {
            if let Some(breakdown) = split.breakdown {
                let mut other = 0;
                for source in &breakdown.sources {
                    match source.source {
                        wisp_protocol::events::ContextSource::Messages
                        | wisp_protocol::events::ContextSource::McpTools => {
                            rows.push(
                                spread(
                                    vec![Span::styled(
                                        format!(
                                            "  {}",
                                            if source.source
                                                == wisp_protocol::events::ContextSource::Messages
                                            {
                                                "Messages"
                                            } else {
                                                "MCP tools"
                                            }
                                        ),
                                        muted,
                                    )],
                                    vec![Span::styled(
                                        format!("~{}", abbreviated(source.tokens)),
                                        muted,
                                    )],
                                    width,
                                ),
                                None,
                            );
                        }
                        _ => other += source.tokens,
                    }
                }
                rows.push(
                    spread(
                        vec![Span::styled("  Other instructions/tools", muted)],
                        vec![Span::styled(format!("~{}", abbreviated(other)), muted)],
                        width,
                    ),
                    None,
                );
            }
            if split.reserve > 0 {
                rows.push(
                    spread(
                        vec![Span::styled("  Compaction reserve", muted)],
                        vec![Span::styled(abbreviated(split.reserve), muted)],
                        width,
                    ),
                    None,
                );
            }
            if let Some(percent) = split.threshold().and_then(|n| split.percent(n)) {
                rows.push(
                    Line::styled(
                        format!("  Compacts at {percent:.0}% · estimated split"),
                        muted,
                    ),
                    None,
                );
            }
        }
        if let Some(cost) = crate::context_view::session_cost(state) {
            rows.push(Line::styled(format!("  {cost} session"), muted), None);
        }
        if let Some(cost) = crate::context_view::turn_cost(state) {
            rows.push(Line::styled(format!("  {cost} this turn"), muted), None);
        }
        rows.push(
            Line::styled("  Open full context details →", muted),
            Some(PanelAction::Open(Target::Context)),
        );
    }
}

pub(super) fn inventory_rows(
    rows: &mut PanelRows,
    state: &UiState,
    panel: &SidePanel,
    width: usize,
    palette: Palette,
) {
    let muted = Style::default().fg(palette.muted);
    let servers = state
        .mcp
        .snapshot
        .as_ref()
        .map(|snapshot| snapshot.servers.as_slice());
    let status = servers
        .map(|servers| {
            format!(
                "{}/{} connected",
                servers
                    .iter()
                    .filter(|s| s.status == McpServerStatus::Connected)
                    .count(),
                servers.len()
            )
        })
        .unwrap_or_else(|| {
            if state.mcp.loading() {
                "reading…"
            } else {
                "unavailable"
            }
            .to_owned()
        });
    rows.section(
        Section::Mcp,
        vec![spread(
            disclosure_title("MCP servers", !panel.mcp_collapsed, palette),
            vec![Span::styled(status, muted)],
            width,
        )],
    );
    rows.action(0, PanelAction::Toggle(Disclosure::Mcp));
    if panel.mcp_collapsed {
        if let Some(servers) = servers {
            rows.push(
                Line::styled(
                    ui::clip_columns(
                        &format!(
                            "  {}",
                            servers
                                .iter()
                                .map(|s| sanitize_for_terminal(&s.name))
                                .collect::<Vec<_>>()
                                .join(" · ")
                        ),
                        width,
                    ),
                    muted,
                ),
                None,
            );
        }
    } else if let Some(servers) = servers {
        if servers.is_empty() {
            rows.push(
                Line::styled("  No servers configured · /mcp", muted),
                Some(PanelAction::Open(Target::Mcp)),
            );
        }
        for server in servers {
            let expanded = panel.server_details.contains(&server.name);
            let (marker, color) = match server.status {
                McpServerStatus::Connected => ("●", palette.success),
                McpServerStatus::Disabled => ("○", palette.muted),
                McpServerStatus::Disconnected | McpServerStatus::Unavailable => {
                    ("●", palette.error)
                }
            };
            let status = if server.status == McpServerStatus::Connected {
                if expanded { "▾" } else { "▸" }
            } else {
                server.status.as_str()
            };
            rows.push(
                spread(
                    vec![
                        Span::styled(format!("{marker} "), Style::default().fg(color)),
                        Span::raw(sanitize_for_terminal(&server.name)),
                    ],
                    vec![Span::styled(status, muted)],
                    width,
                ),
                toggle(Disclosure::Server(server.name.clone())),
            );
            if expanded {
                rows.push(
                    Line::styled(
                        format!(
                            "  {} · {} tools",
                            server.status.as_str(),
                            server.tool_names.len()
                        ),
                        muted,
                    ),
                    None,
                );
                if let Some(error) = &server.error {
                    detail(rows, error, width, palette);
                }
                rows.push(
                    Line::styled("  Manage server · /mcp →", muted),
                    Some(PanelAction::Open(Target::Mcp)),
                );
            }
        }
    } else {
        rows.push(
            Line::styled("  Open /mcp to inspect or retry →", muted),
            Some(PanelAction::Open(Target::Mcp)),
        );
    }
    let catalog = state.skills.snapshot.as_ref();
    let status = catalog
        .map(|c| format!("{} available", c.entries.len()))
        .unwrap_or_else(|| {
            if state.skills.loading() {
                "reading…"
            } else {
                "unavailable"
            }
            .to_owned()
        });
    rows.section(
        Section::Skills,
        vec![spread(
            disclosure_title("Skills", !panel.skills_collapsed, palette),
            vec![Span::styled(status, muted)],
            width,
        )],
    );
    rows.action(0, PanelAction::Toggle(Disclosure::Skills));
    let used = state
        .context
        .stats
        .as_ref()
        .map(|stats| stats.used_skill_names.as_slice())
        .unwrap_or_default();
    if panel.skills_collapsed {
        if let Some(catalog) = catalog {
            rows.push(
                Line::styled(
                    ui::clip_columns(
                        &format!(
                            "  {}",
                            catalog
                                .entries
                                .iter()
                                .map(|s| sanitize_for_terminal(&s.name))
                                .collect::<Vec<_>>()
                                .join(" · ")
                        ),
                        width,
                    ),
                    muted,
                ),
                None,
            );
        }
    } else if let Some(catalog) = catalog {
        if catalog.entries.is_empty() {
            rows.push(
                Line::styled("  No skills available · /skills", muted),
                Some(PanelAction::Open(Target::Skills)),
            );
        }
        for skill in &catalog.entries {
            let is_used = used.contains(&skill.name);
            rows.push(
                spread(
                    vec![
                        Span::styled(
                            if is_used { "◆ " } else { "◇ " },
                            Style::default().fg(if is_used {
                                palette.primary
                            } else {
                                palette.muted
                            }),
                        ),
                        Span::raw(sanitize_for_terminal(&skill.name)),
                    ],
                    vec![Span::styled(
                        if is_used { "used" } else { "available" },
                        muted,
                    )],
                    width,
                ),
                toggle(Disclosure::Skill(skill.name.clone())),
            );
            if panel.skill_details.contains(&skill.name) {
                rows.push(
                    Line::styled(
                        if is_used {
                            "  Used on this session branch"
                        } else {
                            "  Available to invoke"
                        },
                        muted,
                    ),
                    None,
                );
                detail(rows, &skill.description, width, palette);
                rows.push(
                    Line::styled(format!("  {} · /skills →", skill.source.as_str()), muted),
                    Some(PanelAction::Open(Target::Skills)),
                );
            }
        }
        if !catalog.project_trusted {
            rows.push(Line::styled("  Project skills await trust", muted), None);
        }
        if !catalog.diagnostics.is_empty() {
            rows.push(
                Line::styled(
                    format!("  {} diagnostics · /skills →", catalog.diagnostics.len()),
                    muted,
                ),
                Some(PanelAction::Open(Target::Skills)),
            );
        }
    } else {
        rows.push(
            Line::styled("  Open /skills to inspect or retry →", muted),
            Some(PanelAction::Open(Target::Skills)),
        );
    }
}

fn detail(rows: &mut PanelRows, text: &str, width: usize, palette: Palette) {
    let safe = sanitize_for_terminal(text).replace('\n', " ");
    let mut lines = vec![String::new()];
    for grapheme in safe.graphemes(true) {
        if lines.last().unwrap().width() + grapheme.width() > width.saturating_sub(2).max(1) {
            if lines.len() == 4 {
                let last = lines.last_mut().unwrap();
                *last = ui::clip_columns(
                    &format!("{last}{grapheme}…"),
                    width.saturating_sub(2).max(1),
                );
                break;
            }
            lines.push(String::new());
        }
        lines.last_mut().unwrap().push_str(grapheme);
    }
    for line in lines {
        rows.push(
            Line::styled(format!("  {line}"), Style::default().fg(palette.muted)),
            None,
        );
    }
}
