//! Read-only presentation of backend context budgets and session totals.

use crate::context_split::{self, Split, abbreviated, approximate, budget_tokens};
use crate::reducer::UiState;
use crate::side_panel::{clip_spans, spread};
use crate::theme::Palette;
use crate::ui::{clip_columns, sanitize_for_terminal};
use crate::web_search::CostTotal;
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{ContextAccountingMethod, ContextBreakdown, ContextSource};

/// The session's estimated cost, such as `$1.84` or `≥$1.84` when some usage had no
/// price, or `None` before any usage was priced.
pub(crate) fn session_cost(state: &UiState) -> Option<String> {
    let cost = &state.context.stats.as_ref()?.cost;
    if cost.priced_record_count == 0 {
        return None;
    }
    let amount = cost.known_usd.parse::<f64>().map_or_else(
        |_| format!("${}", sanitize_for_terminal(&cost.known_usd)),
        format_usd,
    );
    Some(format!("{}{amount}", if cost.complete { "" } else { "≥" }))
}

/// What the latest run's replies cost, from the estimate each reply carries. `None`
/// until a reply in it is priced. A reply that searched the web, or one with text that
/// could not be priced, makes the total a floor (`≥$0.31`); an unpriced reply that
/// only called tools carries no cost and is not counted.
pub(crate) fn turn_cost(state: &UiState) -> Option<String> {
    // `/compact` runs no turn of its own, so the finished run's total stays shown.
    let running = state
        .current_command
        .as_ref()
        .is_some_and(|command| command.command_type != crate::reducer::ActiveCommandType::Compact);
    let total = if running {
        // A run in progress spans the steers and follow-ups it takes in.
        CostTotal::of(
            crate::ui::current_turn(state)
                .iter()
                .filter_map(|entry| entry.cost()),
        )
    } else if let Some(total) = state.finished_run_cost {
        total
    } else {
        // A session opened from history: its last run's steers are prompts like any
        // other, so the turn is what follows the latest one.
        let entries = state.transcript.entries();
        let start = entries
            .iter()
            .rposition(|entry| entry.role == crate::transcript::TranscriptRole::User)
            .map_or(0, |index| index + 1);
        CostTotal::of(entries[start..].iter().filter_map(|entry| entry.cost()))
    };
    total.priced.then(|| {
        format!(
            "{}{}",
            if total.floor { "≥" } else { "" },
            format_usd(total.nano_usd as f64 / 1e9)
        )
    })
}

/// Round a cost for display like the backend's `format_usd`, keeping more places
/// for small amounts so a short session never reads as `$0.00`.
fn format_usd(usd: f64) -> String {
    if usd == 0.0 {
        "$0.00".into()
    } else if usd < 0.0001 {
        "<$0.0001".into()
    } else if usd < 0.01 {
        format!("${usd:.4}")
    } else if usd < 1.0 {
        format!("${usd:.3}")
    } else {
        format!("${usd:.2}")
    }
}

/// Cells in the footer's context meter.
pub(crate) const METER_CELLS: usize = 10;

/// How full the context window is, as a fraction, when the window is known.
pub(crate) fn fill(state: &UiState) -> Option<f64> {
    let budget = state.context.budget.as_ref()?;
    let window = budget.context_window.filter(|window| *window > 0)?;
    Some(budget_tokens(budget) as f64 / window as f64)
}

/// The footer's context reading: `ctx ▰▰▰▰▰▰▰▱▱▱ 68%`, or `ctx ~68%` when the meter
/// does not fit in `width`. The meter caps at full; the percent does not. Without a
/// known window it reads the token count, and `ctx ~…`/`ctx ~?` before a budget.
pub(crate) fn indicator(state: &UiState, width: usize) -> String {
    let Some(budget) = &state.context.budget else {
        return if state.context.loading() {
            "ctx ~…"
        } else {
            "ctx ~?"
        }
        .into();
    };
    let prefix = if approximate(budget) { "~" } else { "" };
    let Some(fill) = fill(state) else {
        return format!("ctx {prefix}{}", abbreviated(budget_tokens(budget)));
    };
    let percent = format!("{prefix}{:.0}%", fill * 100.0);
    let filled = ((fill * METER_CELLS as f64).round() as usize).min(METER_CELLS);
    let meter = format!(
        "ctx {}{} {percent}",
        "▰".repeat(filled),
        "▱".repeat(METER_CELLS - filled)
    );
    if meter.width() <= width {
        meter
    } else {
        format!("ctx {percent}")
    }
}

/// Every text row the view shows without a split, in order.
#[cfg(test)]
fn rows(state: &UiState) -> Vec<String> {
    let mut rows = status_rows(state);
    rows.extend(budget_rows(state));
    rows.extend(session_rows(state));
    rows
}

fn status_rows(state: &UiState) -> Vec<String> {
    let mut rows = Vec::new();
    if state.context.loading() {
        rows.push("Refreshing context…".into());
    }
    if let Some(error) = &state.context.error {
        rows.push(error.clone());
    }
    rows
}

/// The budget as text, which the split replaces when the backend sends one.
fn budget_rows(state: &UiState) -> Vec<String> {
    let mut rows = Vec::new();
    if let Some(budget) = &state.context.budget {
        let current = budget_tokens(budget);
        rows.push(format!(
            "Context: {current} / {} tokens",
            budget
                .context_window
                .map(|v| v.to_string())
                .unwrap_or_else(|| "unknown".into())
        ));
        rows.push(format!(
            "Source: {}",
            match budget.accounting_method {
                ContextAccountingMethod::ProviderObservedPlusEstimate =>
                    "provider observation + trailing estimate",
                _ if !approximate(budget) => "provider observation",
                _ => "deterministic estimate (approximate)",
            }
        ));
        if state.current_command.is_some() {
            rows.push("Context is from the latest request boundary.".into());
        }
        rows.push(format!("Reserved: {} tokens", budget.reserve_tokens));
        if let Some(window) = budget.context_window {
            let remaining =
                i128::from(window) - i128::from(budget.reserve_tokens) - i128::from(current);
            rows.push(format!(
                "Usage: {:.1}% · remaining: {remaining} tokens{}",
                current as f64 / window as f64 * 100.0,
                if remaining <= 0 {
                    " · over budget"
                } else {
                    ""
                }
            ));
            rows.push(format!(
                "Compaction trigger: {}",
                window
                    .checked_sub(budget.reserve_tokens)
                    .filter(|n| *n > 0)
                    .map(|n| format!(">{n} tokens"))
                    .unwrap_or_else(|| "unavailable".into())
            ));
        } else {
            rows.push("Remaining / compaction trigger: unknown".into());
        }
    } else {
        rows.push("Context budget unavailable.".into());
    }
    rows.push(String::new());
    rows
}

/// Session totals, the compaction policy, and the context commands.
fn session_rows(state: &UiState) -> Vec<String> {
    let mut rows = Vec::new();
    if state.context.stats_stale {
        rows.push("Session totals: last refreshed snapshot (may be stale).".into());
    }
    if let Some(stats) = &state.context.stats {
        rows.push(format!(
            "Session tokens: {} input · {} output · {} total",
            stats.usage.input_tokens, stats.usage.output_tokens, stats.usage.total_tokens
        ));
        rows.push(format!(
            "Prompt cache: {} read · {} written",
            optional_tokens(stats.usage.cache_read_input_tokens),
            optional_tokens(stats.usage.cache_write_input_tokens)
        ));
        let cost = &stats.cost;
        let amount = if cost.priced_record_count == 0 {
            if cost.unpriced_record_count == 0 {
                "unavailable".into()
            } else {
                "unknown".into()
            }
        } else {
            format!(
                "{}${} USD",
                if cost.complete { "" } else { "≥" },
                cost.known_usd
            )
        };
        rows.push(format!("Estimated session cost: {amount}"));
        rows.push(format!(
            "Active messages: {} · compactions: {}",
            stats.active_message_count, stats.compaction_count
        ));
        if let Some(policy) = &stats.compaction {
            rows.push(format!(
                "Automatic compaction: {}",
                on_off(policy.auto_compaction_enabled)
            ));
            rows.push(format!(
                "Threshold eligibility: {}",
                if policy.threshold_eligible {
                    "eligible"
                } else {
                    policy
                        .threshold_ineligible_reason
                        .as_deref()
                        .unwrap_or("unavailable")
                }
            ));
            rows.push(format!(
                "Overflow recovery: {}",
                on_off(policy.overflow_recovery_enabled)
            ));
        } else {
            rows.push("Compaction policy: unavailable".into());
        }
    } else {
        rows.push("Session totals and compaction policy unavailable.".into());
    }
    if let Some(reason) = state.context.compaction {
        rows.push(format!("Compacting ({})…", reason.as_str()));
    }
    if let Some(notice) = &state.context.compaction_notice {
        rows.push(notice.clone());
    }
    rows.push(String::new());
    rows.push("/context auto on|off · /compact [instructions] (when idle)".into());
    rows
}

fn on_off(enabled: bool) -> &'static str {
    if enabled { "on" } else { "off" }
}
fn optional_tokens(value: Option<u64>) -> String {
    value
        .map(|n| n.to_string())
        .unwrap_or_else(|| "unreported".into())
}

/// The `/context` overlay: the window split by source, with rows that expand into
/// their parts, the largest items and growth per turn, then the session totals.
/// Without a split from the backend it shows the totals alone.
pub(crate) struct ContextView {
    offset: usize,
    row_count: usize,
    page_height: usize,
    /// The highlighted source row, as an index into `sources`.
    selected: usize,
    expanded: Vec<ContextSource>,
    /// Source rows in the order last drawn, so keys can name them.
    sources: Vec<ContextSource>,
    /// Line the highlighted row was last drawn on.
    selected_line: Option<usize>,
    /// The highlight moved, so the next draw scrolls it into view.
    follow: bool,
}

impl Default for ContextView {
    fn default() -> Self {
        Self {
            offset: 0,
            row_count: 0,
            page_height: 0,
            selected: 0,
            // Messages usually dominates, so its parts start open.
            expanded: vec![ContextSource::Messages],
            sources: Vec::new(),
            selected_line: None,
            follow: false,
        }
    }
}

impl ContextView {
    /// ↑↓ move between source rows (or scroll, without a split), Enter expands or
    /// collapses the highlighted row, and page keys scroll.
    pub fn key(&mut self, key: KeyCode) {
        let rows = self.sources.len();
        match key {
            KeyCode::Up if rows > 0 => {
                self.selected = self.selected.saturating_sub(1);
                self.follow = true;
                return;
            }
            KeyCode::Down if rows > 0 => {
                self.selected = (self.selected + 1).min(rows - 1);
                self.follow = true;
                return;
            }
            KeyCode::Enter => {
                self.follow = true;
                if let Some(source) = self.sources.get(self.selected).copied() {
                    if let Some(index) = self.expanded.iter().position(|open| *open == source) {
                        self.expanded.remove(index);
                    } else {
                        self.expanded.push(source);
                    }
                }
                return;
            }
            _ => {}
        }
        self.offset = match key {
            KeyCode::Up => self.offset.saturating_sub(1),
            KeyCode::Down => self.offset.saturating_add(1),
            KeyCode::PageUp => self.offset.saturating_sub(self.page_height.max(1)),
            KeyCode::PageDown => self.offset.saturating_add(self.page_height.max(1)),
            KeyCode::Home => 0,
            KeyCode::End => self.row_count.saturating_sub(self.page_height),
            _ => self.offset,
        }
        .min(self.row_count.saturating_sub(self.page_height));
    }

    pub fn render(&mut self, frame: &mut Frame<'_>, area: Rect, state: &UiState, palette: Palette) {
        let split = Split::of(state).filter(|split| split.breakdown.is_some());
        let hint = if split.is_some() {
            " ↑↓ move · Enter expand · PgUp/PgDn · r refresh · Esc close "
        } else {
            " ↑↓ PgUp/PgDn · r refresh · Esc close "
        };
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(" Context ")
            .title_bottom(hint);
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let width = usize::from(inner.width);
        // Refresh progress and errors come first, so a failed refresh is never
        // hidden below a split that is now stale.
        let mut lines = wrapped(status_rows(state), width);
        match &split {
            Some(split) => {
                let start = lines.len();
                lines.extend(self.split_lines(split, state, width, palette));
                self.selected_line = self.selected_line.map(|line| start + line);
                lines.extend(wrapped(session_rows(state), width));
            }
            None => {
                self.sources.clear();
                self.selected_line = None;
                lines.extend(wrapped(budget_rows(state), width));
                lines.extend(wrapped(session_rows(state), width));
            }
        }
        self.row_count = lines.len();
        self.page_height = usize::from(inner.height);
        if let Some(line) = self
            .selected_line
            .filter(|_| std::mem::take(&mut self.follow))
        {
            if line < self.offset {
                self.offset = line;
            } else if line >= self.offset + self.page_height {
                self.offset = line + 1 - self.page_height;
            }
        }
        self.offset = self
            .offset
            .min(self.row_count.saturating_sub(self.page_height));
        let visible: Vec<Line<'static>> = lines
            .into_iter()
            .skip(self.offset)
            .take(self.page_height)
            .collect();
        frame.render_widget(Paragraph::new(visible), inner);
    }

    /// The split's header and bar, the source table, the largest items and growth
    /// per turn, then the footnotes. Records the line the highlighted row is on.
    fn split_lines(
        &mut self,
        split: &Split<'_>,
        state: &UiState,
        width: usize,
        palette: Palette,
    ) -> Vec<Line<'static>> {
        let breakdown = split.breakdown.expect("split has a breakdown");
        let dim = Style::default().fg(palette.rail());
        let mut lines = split_header(split, state, width, palette);
        lines.push(Line::default());
        let table = self.source_table(split, breakdown, width, palette);
        self.selected_line = self.selected_line.map(|line| lines.len() + line);
        lines.extend(table);
        lines.extend(largest_lines(breakdown, width, palette));
        lines.extend(growth_lines(breakdown, width, palette));
        lines.push(Line::default());
        let mut notes = vec![
            "~ estimated. Provider overhead is spread across the rows.",
            "The first listed turn includes the prompt and tools.",
        ];
        if split.growth != 0 {
            notes.push("During a run, what it adds counts as messages until the next refresh.");
        }
        for note in notes {
            lines.push(Line::styled(clip_columns(note, width), dim));
        }
        lines.push(Line::default());
        lines
    }

    /// A row per source, its parts under it when expanded, then the compaction
    /// reserve and free space. Records the highlighted row's line within the table.
    fn source_table(
        &mut self,
        split: &Split<'_>,
        breakdown: &ContextBreakdown,
        width: usize,
        palette: Palette,
    ) -> Vec<Line<'static>> {
        let muted = Style::default().fg(palette.muted);
        let dim = Style::default().fg(palette.rail());
        let percent = |tokens: u64| {
            split
                .percent(tokens)
                .map_or_else(String::new, |percent| format!("{percent:.1}%"))
        };
        let mut lines = vec![table_row(
            Span::raw("  "),
            vec![Span::styled("source", dim)],
            ("tokens".into(), "window".into(), "detail".into()),
            dim,
            width,
        )];
        self.sources = breakdown.sources.iter().map(|usage| usage.source).collect();
        self.selected = self.selected.min(self.sources.len().saturating_sub(1));
        self.selected_line = None;
        for (index, (usage, (source, tokens))) in
            breakdown.sources.iter().zip(split.sources()).enumerate()
        {
            let open = self.expanded.contains(&source);
            let marker = if usage.children.is_empty() {
                " "
            } else if open {
                "▾"
            } else {
                "▸"
            };
            let mut row = table_row(
                Span::styled(format!("{marker} "), muted),
                vec![
                    Span::styled(
                        "■ ",
                        Style::default().fg(context_split::source_color(source, palette)),
                    ),
                    Span::raw(context_split::source_label(source)),
                ],
                (
                    context_split::estimated(tokens),
                    percent(tokens),
                    context_split::source_detail(usage).unwrap_or_default(),
                ),
                dim,
                width,
            );
            if index == self.selected {
                self.selected_line = Some(lines.len());
                let padding = width.saturating_sub(row.width());
                row.spans.push(Span::raw(" ".repeat(padding)));
                row = row.patch_style(palette.selection());
            }
            lines.push(row);
            if open {
                for child in &usage.children {
                    lines.push(table_row(
                        Span::raw("  "),
                        vec![Span::styled(
                            format!("   {}", sanitize_for_terminal(&child.label)),
                            muted,
                        )],
                        (
                            context_split::estimated(child.tokens),
                            percent(child.tokens),
                            child_detail(source, child.count),
                        ),
                        dim,
                        width,
                    ));
                }
            }
        }
        if let Some(free) = split.free() {
            let reserve_detail = if split.threshold().is_some() {
                "kept free for the next reply"
            } else {
                "automatic compaction unavailable"
            };
            let over = split
                .window
                .is_some_and(|window| split.total.saturating_add(split.reserve) > window);
            lines.push(table_row(
                Span::raw("  "),
                vec![
                    Span::styled("╱ ", muted),
                    Span::styled("Compaction reserve", muted),
                ],
                (
                    abbreviated(split.reserve),
                    percent(split.reserve),
                    reserve_detail.into(),
                ),
                dim,
                width,
            ));
            lines.push(table_row(
                Span::raw("  "),
                vec![
                    Span::styled("■ ", Style::default().fg(palette.panel)),
                    Span::styled("Free space", muted),
                ],
                (
                    abbreviated(free),
                    percent(free),
                    if over {
                        "over budget"
                    } else {
                        "before compaction"
                    }
                    .into(),
                ),
                dim,
                width,
            ));
        }
        lines
    }
}

/// The title and total, what the total and split are measured from, then the bar
/// with an axis naming where compaction starts.
fn split_header(
    split: &Split<'_>,
    state: &UiState,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let breakdown = split.breakdown.expect("split has a breakdown");
    let muted = Style::default().fg(palette.muted);
    let dim = Style::default().fg(palette.rail());
    let prefix = if split.approximate { "~" } else { "" };
    let mut title = vec![Span::styled(
        "Context",
        Style::default()
            .fg(palette.primary)
            .add_modifier(Modifier::BOLD),
    )];
    if let Some(model) = state.model.as_deref() {
        title.push(Span::styled(
            format!(" · {}", sanitize_for_terminal(model)),
            muted,
        ));
    }
    if let Some(provider) = state.provider.as_deref() {
        title.push(Span::styled(
            format!(" {}", sanitize_for_terminal(provider)),
            dim,
        ));
    }
    let mut total = vec![Span::raw(format!("{prefix}{}", abbreviated(split.total)))];
    if let Some(window) = split.window {
        total.push(Span::styled(format!(" / {}", abbreviated(window)), dim));
    }
    if let Some(percent) = split.percent(split.total) {
        total.push(Span::styled(format!(" {prefix}{percent:.0}%"), muted));
    }
    let source = if split.growth != 0 {
        "latest request's total · split from the last refresh, scaled to it"
    } else if breakdown.scaled {
        "total reported by the provider · split estimated (UTF-8 bytes ÷ 4) and scaled to it"
    } else {
        "total and split estimated from the request (UTF-8 bytes ÷ 4)"
    };
    let mut lines = vec![
        spread(title, total, width),
        Line::styled(clip_columns(source, width), muted),
    ];
    if split.window.is_some() {
        lines.push(context_split::bar(split, width, palette));
        let threshold = split
            .threshold()
            .and_then(|tokens| Some((tokens, split.percent(tokens)?)))
            .map(|(tokens, percent)| {
                vec![
                    Span::styled("▏", Style::default().fg(palette.error)),
                    Span::styled(
                        format!("compacts at {percent:.0}% ({})", abbreviated(tokens)),
                        dim,
                    ),
                ]
            })
            .unwrap_or_default();
        lines.push(spread(vec![Span::styled("0", dim)], threshold, width));
    }
    lines
}

/// The largest single messages, ranked, with the turn each came from.
fn largest_lines(
    breakdown: &ContextBreakdown,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    if breakdown.largest.is_empty() {
        return Vec::new();
    }
    let dim = Style::default().fg(palette.rail());
    let mut lines = vec![
        Line::default(),
        Line::styled(
            "Largest items",
            Style::default()
                .fg(palette.muted)
                .add_modifier(Modifier::BOLD),
        ),
    ];
    for (rank, item) in breakdown.largest.iter().enumerate() {
        lines.push(spread(
            vec![
                Span::styled(format!("{} ", rank + 1), dim),
                Span::raw(sanitize_for_terminal(&item.label)),
                Span::styled(format!(" · turn {}", item.turn), dim),
            ],
            vec![Span::raw(abbreviated(item.tokens))],
            width,
        ));
    }
    lines
}

/// Each turn's growth with a block scaled to the largest; the latest in the
/// warning colour.
fn growth_lines(
    breakdown: &ContextBreakdown,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    if breakdown.turns.is_empty() {
        return Vec::new();
    }
    let mut lines = vec![
        Line::default(),
        Line::styled(
            "Growth per turn",
            Style::default()
                .fg(palette.muted)
                .add_modifier(Modifier::BOLD),
        ),
    ];
    let tokens: Vec<u64> = breakdown.turns.iter().map(|turn| turn.tokens).collect();
    let blocks = context_split::spark(&tokens);
    for (position, (turn, block)) in breakdown.turns.iter().zip(blocks).enumerate() {
        let latest = position + 1 == breakdown.turns.len();
        let style = if latest {
            Style::default().fg(palette.warning)
        } else {
            Style::default()
        };
        let color = if latest {
            palette.warning
        } else {
            palette.secondary
        };
        lines.push(spread(
            vec![
                Span::styled(
                    format!("{:>3} ", turn.turn),
                    Style::default().fg(palette.rail()),
                ),
                Span::styled(block, Style::default().fg(color)),
            ],
            vec![Span::styled(
                format!("+{}", abbreviated(turn.tokens)),
                style,
            )],
            width,
        ));
    }
    lines
}

/// Text rows wrapped to `width`, so long policy explanations stay reachable on
/// narrow screens.
fn wrapped(rows: Vec<String>, width: usize) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    for row in rows {
        let sanitized = sanitize_for_terminal(&row);
        for physical_line in sanitized.split('\n') {
            let mut line = String::new();
            let mut line_width = 0;
            for grapheme in physical_line.graphemes(true) {
                let next = grapheme.width();
                if line_width + next > width && !line.is_empty() {
                    lines.push(Line::raw(std::mem::take(&mut line)));
                    line_width = 0;
                }
                line.push_str(grapheme);
                line_width += next;
            }
            lines.push(Line::raw(line));
        }
    }
    lines
}

/// What a source's part holds, such as `21 tools` for an MCP server.
fn child_detail(source: ContextSource, count: Option<u64>) -> String {
    let Some(count) = count else {
        return String::new();
    };
    let noun = match source {
        ContextSource::McpTools => "tool",
        ContextSource::Messages => "message",
        _ => "item",
    };
    format!("{count} {noun}{}", if count == 1 { "" } else { "s" })
}

/// A table row: the marker and label on the left, then tokens, share of the window
/// and a detail in fixed columns. The detail is dropped first when space runs out.
fn table_row(
    marker: Span<'static>,
    label: Vec<Span<'static>>,
    (tokens, share, detail): (String, String, String),
    detail_style: Style,
    width: usize,
) -> Line<'static> {
    const TOKENS: usize = 10;
    const SHARE: usize = 8;
    let detail_width = (width / 3).min(32);
    let show_detail = width >= 60;
    let figures = TOKENS + SHARE + if show_detail { detail_width + 3 } else { 0 };
    let mut left = vec![marker];
    left.extend(label);
    let mut right = vec![
        Span::raw(format!("{tokens:>TOKENS$}")),
        Span::styled(format!("{share:>SHARE$}"), detail_style),
    ];
    if show_detail {
        right.push(Span::styled(
            format!("   {:<detail_width$}", clip_columns(&detail, detail_width)),
            detail_style,
        ));
    }
    spread_fixed(left, right, figures, width)
}

/// `left` clipped to leave `right_width` columns, then `right`, so the right
/// side keeps fixed columns from row to row.
fn spread_fixed(
    left: Vec<Span<'static>>,
    right: Vec<Span<'static>>,
    right_width: usize,
    width: usize,
) -> Line<'static> {
    let room = width.saturating_sub(right_width);
    let mut spans = clip_spans(left, room);
    let used: usize = spans.iter().map(|span| span.content.width()).sum();
    spans.push(Span::raw(" ".repeat(room.saturating_sub(used))));
    spans.extend(right);
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::Value;
    fn state() -> UiState {
        let mut state = UiState::new("fake".into(), None, None);
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../tests/fixtures/rust_tui_context.json"
        ))
        .unwrap();
        let stats: wisp_protocol::events::SessionStats =
            serde_json::from_value(fixture["session.stats"]["stats"].clone()).unwrap();
        state.context.budget = Some(stats.context.clone());
        state.context.stats = Some(Box::new(stats));
        state
    }

    #[test]
    fn display_distinguishes_estimated_observed_hybrid_unknown_and_over_budget() {
        let mut state = state();
        assert_eq!(indicator(&state, 80), "ctx ▰▰▰▱▱▱▱▱▱▱ ~30%");
        assert_eq!(indicator(&state, 10), "ctx ~30%");
        assert!(rows(&state).join("\n").contains("≥$0.0123 USD"));
        let budget = state.context.budget.as_mut().unwrap();
        budget.observed_is_current = true;
        budget.observed_tokens = Some(4000);
        budget.accounting_method = ContextAccountingMethod::ProviderObserved;
        assert_eq!(indicator(&state, 80), "ctx ▰▰▰▰▱▱▱▱▱▱ 40%");
        let budget = state.context.budget.as_mut().unwrap();
        budget.effective_tokens = Some(11000);
        budget.accounting_method = ContextAccountingMethod::ProviderObservedPlusEstimate;
        // The meter stops at full; the percent still says how far over.
        assert!(indicator(&state, 80).ends_with("▰▰▰▰▰▰▰▰▰▰ ~110%"));
        let text = rows(&state).join("\n");
        assert!(text.contains("110.0% · remaining: -2000 tokens · over budget"));
        assert!(text.contains("provider observation + trailing estimate"));
        state.context.budget.as_mut().unwrap().context_window = None;
        assert!(rows(&state).join("\n").contains("11000 / unknown"));
        assert_eq!(indicator(&state, 80), "ctx ~11.0k");
        let stats = state.context.stats.as_mut().unwrap();
        stats.compaction = None;
        stats.cost.priced_record_count = 0;
        stats.usage.cache_read_input_tokens = None;
        let text = rows(&state).join("\n");
        assert!(text.contains("cost: unknown"));
        assert!(text.contains("policy: unavailable"));
        assert!(text.contains("unreported read"));
    }

    #[test]
    fn report_scrolls_wrapped_explanations_and_recovers_after_resize() {
        let mut state = state();
        // The text rows alone, as before the backend sent a split.
        state.context.stats.as_mut().unwrap().context_breakdown = None;
        state
            .context
            .stats
            .as_mut()
            .unwrap()
            .compaction
            .as_mut()
            .unwrap()
            .threshold_ineligible_reason = Some("Untrusted context 候選 ".repeat(15));
        state
            .context
            .stats
            .as_mut()
            .unwrap()
            .compaction
            .as_mut()
            .unwrap()
            .threshold_eligible = false;
        let mut view = ContextView::default();
        let mut terminal = Terminal::new(TestBackend::new(30, 8)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), &state, Palette::default()))
            .unwrap();
        assert!(view.row_count > rows(&state).len());
        view.key(KeyCode::End);
        terminal
            .draw(|frame| view.render(frame, frame.area(), &state, Palette::default()))
            .unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        let joined: String = text
            .chars()
            .filter(|ch| !ch.is_whitespace() && *ch != '│')
            .collect();
        assert!(joined.contains("whenidle)"), "{text:?}");
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), &state, Palette::default()))
            .unwrap();
        assert_eq!(view.offset, 0);
    }

    fn screen(view: &mut ContextView, state: &UiState, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|frame| view.render(frame, frame.area(), state, Palette::default()))
            .unwrap();
        let buffer = terminal.backend().buffer();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>()
                    .trim_end()
                    .to_owned()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn split_view_rows() {
        let text = screen(&mut ContextView::default(), &state(), 100, 50);
        for expected in [
            "~3.0k / 10.0k ~30%",
            "┃╱╱╱╱╱╱╱╱╱│",
            "▏compacts at 90% (9.0k)",
            "▾ ■ Messages                                      ~2.4k   24.0%   4 messages",
            "     tool output                                  ~1.6k   16.0%   1 message",
            "▸ ■ MCP tools                                      ~150    1.5%   1 server · 3 tools",
            "  ╱ Compaction reserve                             1.0k   10.0%",
            "  ■ Free space                                     6.0k   60.0%",
            "1 bash gh pr checks 766 output · turn 2",
            "  2 █",
            "Session tokens: 2900 input",
        ] {
            assert!(text.contains(expected), "{expected}\n{text}");
        }
        // The split replaces the budget's text rows.
        assert!(!text.contains("Compaction trigger"), "{text}");
    }

    #[test]
    fn enter_toggles_row() {
        let state = state();
        let mut view = ContextView::default();
        screen(&mut view, &state, 100, 50);
        view.key(KeyCode::Up);
        view.key(KeyCode::Down);
        view.key(KeyCode::Down);
        view.key(KeyCode::Enter);
        assert!(screen(&mut view, &state, 100, 50).contains("     github"));
        view.key(KeyCode::Enter);
        assert!(!screen(&mut view, &state, 100, 50).contains("     github"));
        view.key(KeyCode::Up);
        view.key(KeyCode::Up);
        view.key(KeyCode::Enter);
        assert!(!screen(&mut view, &state, 100, 50).contains("tool output"));
    }

    #[test]
    fn selection_stays_visible() {
        let state = state();
        let mut view = ContextView::default();
        screen(&mut view, &state, 60, 10);
        for _ in 0..6 {
            view.key(KeyCode::Down);
        }
        let text = screen(&mut view, &state, 60, 10);
        assert!(text.contains("Project context"), "{text}");
        assert!(view.offset > 0);
    }

    #[test]
    fn refresh_error_comes_first() {
        let mut state = state();
        state.context.error = Some("Context refresh failed.".into());
        let text = screen(&mut ContextView::default(), &state, 100, 50);
        assert!(
            text.lines()
                .nth(1)
                .unwrap()
                .contains("Context refresh failed."),
            "{text}"
        );
    }

    #[test]
    fn view_without_split() {
        let mut state = state();
        state.context.stats.as_mut().unwrap().context_breakdown = None;
        let text = screen(&mut ContextView::default(), &state, 100, 50);
        assert!(text.contains("Context: 3000 / 10000 tokens"), "{text}");
        assert!(!text.contains("Messages"), "{text}");
    }
}
