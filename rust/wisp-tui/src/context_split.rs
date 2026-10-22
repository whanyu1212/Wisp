//! The context window split by source, shared by the side panel and `/context`.
//!
//! The backend sends the split with each statistics refresh, which happens only
//! while Wisp is idle. During a run the live total comes from each request's
//! estimate instead, so the split carries the latest statistics forward and gives
//! what the run added to Messages.

use crate::reducer::UiState;
use crate::theme::Palette;
use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};
use wisp_protocol::events::{
    ContextAccountingMethod, ContextBreakdown, ContextBudget, ContextSource, ContextSourceUsage,
};

/// Bar cells between the start of the compaction reserve and the window's end: a
/// dashed rule as heavy as the used cells, so the reserve reads as part of the bar.
pub(crate) const RESERVE_CELL: &str = "╍";
const USED_CELL: &str = "━";
const MARKER_CELL: &str = "┃";
const SPARK_LEVELS: [&str; 8] = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];
/// Tokens a budget counts as in use: the provider's figure plus an estimate of what
/// followed it, the provider's figure alone when current, else the estimate.
pub(crate) fn budget_tokens(budget: &ContextBudget) -> u64 {
    if budget.accounting_method == ContextAccountingMethod::ProviderObservedPlusEstimate {
        if let Some(effective) = budget.effective_tokens {
            return effective;
        }
    }
    budget
        .observed_tokens
        .filter(|_| budget.observed_is_current)
        .unwrap_or(budget.estimate.total_tokens)
}

/// Whether the budget's count includes an estimate rather than only what the
/// provider reported.
pub(crate) fn approximate(budget: &ContextBudget) -> bool {
    budget.accounting_method == ContextAccountingMethod::ProviderObservedPlusEstimate
        || !(budget.observed_is_current && budget.observed_tokens.is_some())
}

/// `198.8k`, `1.2m`, or the plain count under a thousand.
pub(crate) fn abbreviated(value: u64) -> String {
    if value >= 1_000_000 {
        format!("{:.1}m", value as f64 / 1_000_000.0)
    } else if value >= 1_000 {
        format!("{:.1}k", value as f64 / 1_000.0)
    } else {
        value.to_string()
    }
}

/// The current context, with its split when the backend reported one.
pub(crate) struct Split<'a> {
    /// Tokens in use, from the latest request or statistics.
    pub total: u64,
    pub window: Option<u64>,
    pub reserve: u64,
    /// The total includes an estimate rather than only what the provider reported.
    pub approximate: bool,
    pub breakdown: Option<&'a ContextBreakdown>,
    /// Tokens added since the statistics the split came from, while a run goes.
    pub growth: i128,
}

impl<'a> Split<'a> {
    pub(crate) fn of(state: &'a UiState) -> Option<Self> {
        let budget = state.context.budget.as_ref()?;
        let total = budget_tokens(budget);
        let stats = state.context.stats.as_deref();
        let mut breakdown = stats.and_then(|stats| stats.context_breakdown.as_ref());
        let mut growth = stats.map_or(0, |stats| {
            i128::from(total) - i128::from(budget_tokens(&stats.context))
        });
        if growth < 0 {
            // The context shrank since the split was reported, as a compaction
            // during a run does: the split describes what was replaced, so it is
            // dropped until the next refresh.
            breakdown = None;
            growth = 0;
        }
        Some(Self {
            total,
            window: budget.context_window.filter(|window| *window > 0),
            reserve: budget.reserve_tokens,
            approximate: approximate(budget),
            breakdown,
            growth,
        })
    }

    /// Each source's tokens, in the backend's order, with the growth since the
    /// statistics counted as messages.
    pub(crate) fn sources(&self) -> Vec<(ContextSource, u64)> {
        let Some(breakdown) = self.breakdown else {
            return Vec::new();
        };
        breakdown
            .sources
            .iter()
            .map(|usage| {
                let tokens = if usage.source == ContextSource::Messages {
                    u64::try_from((i128::from(usage.tokens) + self.growth).max(0))
                        .unwrap_or(u64::MAX)
                } else {
                    usage.tokens
                };
                (usage.source, tokens)
            })
            .collect()
    }

    /// Where compaction starts, as tokens, when the reserve leaves room for any.
    pub(crate) fn threshold(&self) -> Option<u64> {
        let window = self.window?;
        (self.reserve > 0 && self.reserve < window).then(|| window - self.reserve)
    }

    /// Room left in the window after the context and the compaction reserve.
    pub(crate) fn free(&self) -> Option<u64> {
        Some(
            self.window?
                .saturating_sub(self.reserve)
                .saturating_sub(self.total),
        )
    }

    /// The newest finished turn's number and the tokens it added; none before the
    /// first run. A run that finished before its statistics arrived is that turn,
    /// with what it added since the split.
    pub(crate) fn finished_turn(&self) -> Option<(u64, u64)> {
        if self.growth > 0 {
            return self.running_turn();
        }
        let last = self.breakdown?.turns.last()?;
        Some((last.turn, last.tokens))
    }

    /// The run in progress: the turn after the newest finished one (turn 1 in a new
    /// session) and the tokens it has added since the split was reported.
    pub(crate) fn running_turn(&self) -> Option<(u64, u64)> {
        let next = self.breakdown?.turns.last().map_or(1, |last| last.turn + 1);
        Some((next, u64::try_from(self.growth.max(0)).unwrap_or(u64::MAX)))
    }

    /// Percent of the window, when it is known.
    pub(crate) fn percent(&self, tokens: u64) -> Option<f64> {
        self.window
            .map(|window| tokens as f64 / window as f64 * 100.0)
    }
}

pub(crate) fn source_label(source: ContextSource) -> &'static str {
    match source {
        ContextSource::Messages => "Messages",
        ContextSource::BuiltInTools => "Built-in tools",
        ContextSource::McpTools => "MCP tools",
        ContextSource::CustomPrompt => "Custom prompt",
        ContextSource::SystemPrompt => "System prompt",
        ContextSource::Skills => "Skills",
        ContextSource::ProjectInstructions => "Project instructions",
        ContextSource::ProjectContext => "Project context",
    }
}

/// Each source's colour, from the theme's roles so every theme keeps them apart.
pub(crate) fn source_color(source: ContextSource, palette: Palette) -> Color {
    match source {
        ContextSource::Messages => palette.secondary,
        ContextSource::BuiltInTools => palette.primary,
        ContextSource::McpTools => palette.success,
        ContextSource::CustomPrompt => palette.foreground,
        ContextSource::SystemPrompt => palette.warning,
        ContextSource::Skills => palette.accent,
        ContextSource::ProjectInstructions => palette.muted,
        ContextSource::ProjectContext => palette.rail(),
    }
}

/// What a source holds, such as `4 servers · 38 tools`, when it says more than its
/// row.
pub(crate) fn source_detail(usage: &ContextSourceUsage) -> Option<String> {
    let count = usage.count?;
    let plural = |n: u64, one: &str, many: &str| format!("{n} {}", if n == 1 { one } else { many });
    Some(match usage.source {
        ContextSource::Messages => plural(count, "message", "messages"),
        ContextSource::BuiltInTools => plural(count, "tool", "tools"),
        ContextSource::McpTools => format!(
            "{} · {}",
            plural(usage.children.len() as u64, "server", "servers"),
            plural(count, "tool", "tools")
        ),
        ContextSource::ProjectInstructions => plural(count, "file", "files"),
        _ => return None,
    })
}

/// `~152.6k`: a source's share is always estimated, even when the total is not.
pub(crate) fn estimated(tokens: u64) -> String {
    format!("~{}", abbreviated(tokens))
}

impl Split<'_> {
    /// The free space, marked `~` when the total it is taken from is estimated.
    pub(crate) fn free_label(&self, free: u64) -> String {
        if self.approximate {
            estimated(free)
        } else {
            abbreviated(free)
        }
    }
}

/// The window as one row of `width` cells: a run of each source's colour, then
/// the free space, then the compaction reserve as a dashed rule from the
/// threshold, which a marker in the error colour starts. Without a split the used
/// part is one run.
pub(crate) fn bar(split: &Split<'_>, width: usize, palette: Palette) -> Line<'static> {
    let Some(window) = split.window else {
        return Line::default();
    };
    let mut cells: Vec<(&'static str, Color)> = vec![(USED_CELL, palette.panel); width];
    let threshold = split
        .threshold()
        .map(|threshold| column(threshold, window, width));
    if let Some(start) = threshold {
        for cell in &mut cells[start..] {
            *cell = (RESERVE_CELL, palette.rail());
        }
    }
    let sources = split.sources();
    let segments: Vec<(u64, Color)> = if sources.is_empty() {
        let pressured = split.total as f64 / window as f64 >= crate::ui::CONTEXT_WARNING_FILL;
        let color = if pressured {
            palette.warning
        } else {
            palette.secondary
        };
        vec![(split.total, color)]
    } else {
        sources
            .iter()
            .map(|(source, tokens)| (*tokens, source_color(*source, palette)))
            .collect()
    };
    let used = column(split.total.min(window), window, width);
    let weights: Vec<u64> = segments.iter().map(|(tokens, _)| *tokens).collect();
    let mut start = 0;
    for ((_, color), count) in segments.iter().zip(cell_counts(&weights, used)) {
        for cell in &mut cells[start..start + count] {
            *cell = (USED_CELL, *color);
        }
        start += count;
    }
    if let Some(column) = threshold.filter(|column| *column < width) {
        cells[column] = (MARKER_CELL, palette.error);
    }
    merge(cells)
}

/// The column `tokens` reaches in a `width`-cell bar of `window` tokens.
fn column(tokens: u64, window: u64, width: usize) -> usize {
    ((tokens as f64 / window as f64 * width as f64).round() as usize).min(width)
}

/// Split `cells` across `weights` by largest remainder, giving each non-zero weight
/// a cell while there are enough to go round, taken from the widest run.
fn cell_counts(weights: &[u64], cells: usize) -> Vec<usize> {
    // Summed wide so out-of-range backend counts cannot overflow.
    let total: u128 = weights.iter().map(|weight| u128::from(*weight)).sum();
    if total == 0 {
        return vec![0; weights.len()];
    }
    let exact: Vec<f64> = weights
        .iter()
        .map(|weight| *weight as f64 / total as f64 * cells as f64)
        .collect();
    let mut counts: Vec<usize> = exact.iter().map(|share| share.floor() as usize).collect();
    let mut order: Vec<usize> = (0..weights.len()).collect();
    order.sort_by(|a, b| {
        (exact[*b] - exact[*b].floor())
            .total_cmp(&(exact[*a] - exact[*a].floor()))
            .then(a.cmp(b))
    });
    let assigned: usize = counts.iter().sum();
    for index in order.into_iter().take(cells.saturating_sub(assigned)) {
        counts[index] += 1;
    }
    let visible = weights.iter().filter(|weight| **weight > 0).count();
    if visible <= cells {
        for index in 0..weights.len() {
            if weights[index] > 0 && counts[index] == 0 {
                let widest = (0..counts.len())
                    .max_by_key(|other| counts[*other])
                    .expect("weights are not empty");
                if counts[widest] > 1 {
                    counts[widest] -= 1;
                    counts[index] = 1;
                }
            }
        }
    }
    counts
}

fn merge(cells: Vec<(&'static str, Color)>) -> Line<'static> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut run = String::new();
    let mut color = None;
    for (symbol, cell_color) in cells {
        if color != Some(cell_color) && !run.is_empty() {
            spans.push(Span::styled(
                std::mem::take(&mut run),
                Style::default().fg(color.expect("run has a colour")),
            ));
        }
        color = Some(cell_color);
        run.push_str(symbol);
    }
    if let Some(color) = color.filter(|_| !run.is_empty()) {
        spans.push(Span::styled(run, Style::default().fg(color)));
    }
    Line::from(spans)
}

/// Each turn's growth as a block from `▁` to `█`, scaled to the largest shown.
pub(crate) fn spark(tokens: &[u64]) -> Vec<&'static str> {
    let largest = tokens.iter().copied().max().unwrap_or(0);
    tokens
        .iter()
        .map(|tokens| {
            if largest == 0 {
                return SPARK_LEVELS[0];
            }
            let level = (*tokens as f64 / largest as f64 * SPARK_LEVELS.len() as f64).ceil();
            SPARK_LEVELS[(level as usize).clamp(1, SPARK_LEVELS.len()) - 1]
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cells_keep_small_sources() {
        assert_eq!(cell_counts(&[90, 5, 5], 10), vec![8, 1, 1]);
        assert_eq!(cell_counts(&[100, 1], 10), vec![9, 1]);
        assert_eq!(cell_counts(&[1, 1, 1], 2), vec![1, 1, 0]);
        assert_eq!(cell_counts(&[0, 0], 4), vec![0, 0]);
    }

    #[test]
    fn first_run_is_turn_one() {
        let breakdown = ContextBreakdown {
            sources: Vec::new(),
            scaled: false,
            turns: Vec::new(),
            largest: Vec::new(),
        };
        let split = Split {
            total: 1_500,
            window: Some(10_000),
            reserve: 1_000,
            approximate: true,
            breakdown: Some(&breakdown),
            growth: 500,
        };
        assert_eq!(split.running_turn(), Some((1, 500)));
        // Finished, but its statistics have not arrived.
        assert_eq!(split.finished_turn(), Some((1, 500)));
        let split = Split { growth: 0, ..split };
        assert_eq!(split.finished_turn(), None);
    }

    #[test]
    fn free_without_reserve() {
        let split = Split {
            total: 6_000,
            window: Some(10_000),
            reserve: 0,
            approximate: false,
            breakdown: None,
            growth: 0,
        };
        assert_eq!(split.threshold(), None);
        assert_eq!(split.free(), Some(4_000));
    }

    #[test]
    fn spark_scales_to_largest() {
        assert_eq!(spark(&[1, 4, 8]), vec!["▁", "▄", "█"]);
        assert_eq!(spark(&[0, 0]), vec!["▁", "▁"]);
    }
}
