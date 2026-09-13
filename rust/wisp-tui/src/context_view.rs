//! Read-only presentation of backend context budgets and session totals.

use crate::reducer::UiState;
use crate::theme::Palette;
use crate::ui::sanitize_for_terminal;
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::Rect,
    widgets::{Block, Borders, Paragraph},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{ContextAccountingMethod, ContextBudget};

fn tokens(budget: &ContextBudget) -> u64 {
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

fn approximate(budget: &ContextBudget) -> bool {
    budget.accounting_method == ContextAccountingMethod::ProviderObservedPlusEstimate
        || !(budget.observed_is_current && budget.observed_tokens.is_some())
}

fn abbreviated(value: u64) -> String {
    if value >= 1_000_000 {
        format!("{:.1}m", value as f64 / 1_000_000.0)
    } else if value >= 1_000 {
        format!("{:.1}k", value as f64 / 1_000.0)
    } else {
        value.to_string()
    }
}

pub(crate) fn indicator(state: &UiState, width: usize) -> String {
    let Some(budget) = &state.context.budget else {
        return if state.context.loading() {
            "ctx ~…"
        } else {
            "ctx ~?"
        }
        .into();
    };
    let current = tokens(budget);
    let prefix = if approximate(budget) { "~" } else { "" };
    let percent = budget
        .context_window
        .map(|window| format!("{:.0}%", current as f64 / window as f64 * 100.0));
    let full = format!(
        "ctx {prefix}{}/{} {}",
        abbreviated(current),
        budget
            .context_window
            .map(abbreviated)
            .unwrap_or_else(|| "?".into()),
        percent.as_deref().unwrap_or("")
    );
    if full.width() <= width {
        full.trim_end().into()
    } else {
        format!("ctx {prefix}{}", percent.unwrap_or_else(|| "?".into()))
    }
}

pub(crate) fn rows(state: &UiState) -> Vec<String> {
    let mut rows = Vec::new();
    if state.context.loading() {
        rows.push("Refreshing context…".into());
    }
    if let Some(error) = &state.context.error {
        rows.push(error.clone());
    }
    if let Some(budget) = &state.context.budget {
        let current = tokens(budget);
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

#[derive(Default)]
pub(crate) struct ContextView {
    offset: usize,
    row_count: usize,
    page_height: usize,
}

impl ContextView {
    pub fn scroll(&mut self, key: KeyCode) {
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
        let block = Block::default()
            .borders(Borders::ALL)
            .border_style(palette.border())
            .title(" Context ")
            .title_bottom(" ↑↓ PgUp/PgDn · r refresh · Esc close ");
        let inner = block.inner(area);
        frame.render_widget(block, area);
        // Wrap before scrolling so long policy explanations stay reachable on narrow screens.
        let mut wrapped = Vec::new();
        for row in rows(state) {
            let sanitized = sanitize_for_terminal(&row);
            for physical_line in sanitized.split('\n') {
                let mut line = String::new();
                let mut width = 0;
                for grapheme in physical_line.graphemes(true) {
                    let next = grapheme.width();
                    if width + next > usize::from(inner.width) && !line.is_empty() {
                        wrapped.push(std::mem::take(&mut line));
                        width = 0;
                    }
                    line.push_str(grapheme);
                    width += next;
                }
                wrapped.push(line);
            }
        }
        self.row_count = wrapped.len();
        self.page_height = usize::from(inner.height);
        self.offset = self
            .offset
            .min(self.row_count.saturating_sub(self.page_height));
        let text = wrapped
            .into_iter()
            .skip(self.offset)
            .take(self.page_height)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(Paragraph::new(text), inner);
    }
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
        assert_eq!(indicator(&state, 80), "ctx ~3.0k/10.0k 30%");
        assert_eq!(indicator(&state, 10), "ctx ~30%");
        assert!(rows(&state).join("\n").contains("≥$0.0123 USD"));
        let budget = state.context.budget.as_mut().unwrap();
        budget.observed_is_current = true;
        budget.observed_tokens = Some(4000);
        budget.accounting_method = ContextAccountingMethod::ProviderObserved;
        assert!(indicator(&state, 80).contains("ctx 4.0k"));
        let budget = state.context.budget.as_mut().unwrap();
        budget.effective_tokens = Some(11000);
        budget.accounting_method = ContextAccountingMethod::ProviderObservedPlusEstimate;
        let text = rows(&state).join("\n");
        assert!(text.contains("110.0% · remaining: -2000 tokens · over budget"));
        assert!(text.contains("provider observation + trailing estimate"));
        state.context.budget.as_mut().unwrap().context_window = None;
        assert!(rows(&state).join("\n").contains("11000 / unknown"));
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
        view.scroll(KeyCode::End);
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
}
