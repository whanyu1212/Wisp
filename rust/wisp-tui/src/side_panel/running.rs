//! The Running section: active and recently finished background processes and
//! subagents, each with its state, elapsed time, and latest output line.

use super::*;
use crate::script_card::NestedHost;
use crate::tool_cards::{ProcessDisplayState, ToolStatus};

/// Items the unfocused panel lists before the rest are counted.
pub(super) const MAX_RUNNING_ROWS: usize = 5;

/// The `Running` section's lines, its actionable rows, and the line span of an
/// open background-process peek.
pub(super) struct RunningRows {
    pub(super) lines: Vec<Line<'static>>,
    pub(super) actions: Vec<(usize, PanelAction)>,
    pub(super) peek: Option<(usize, usize)>,
}

struct SubagentRow<'a> {
    call_id: &'a str,
    subject: String,
    status: ToolStatus,
    elapsed: Option<String>,
    latest_line: Option<String>,
}

/// `Running`: active subagents and background processes with elapsed time and
/// latest output line, followed by finished items with their final state (`[done]`,
/// `failed`, etc.). Nothing when neither subagents nor background processes exist.
pub(super) fn running_lines(
    state: &UiState,
    panel: &SidePanel,
    shown: usize,
    width: usize,
    palette: Palette,
) -> Option<RunningRows> {
    let subagents = collect_subagents(state);
    let running_processes = state.background_processes.running();
    let finished_processes = state.background_processes.finished();

    let running_subagents: Vec<_> = subagents
        .iter()
        .filter(|item| !item.status.terminal())
        .collect();
    let finished_subagents: Vec<_> = subagents
        .iter()
        .filter(|item| item.status.terminal())
        .collect();

    let total_running = running_subagents.len() + running_processes.len();
    let total_finished = finished_subagents.len() + finished_processes.len();
    if total_running == 0 && total_finished == 0 {
        return None;
    }

    let muted = Style::default().fg(palette.muted);
    let count = match (total_running, total_finished) {
        (running, 0) => running.to_string(),
        (0, done) => format!("{done} ended"),
        (running, done) => format!("{running} running · {done} ended"),
    };
    let mut lines = vec![spread(
        vec![heading("Running", palette)],
        vec![Span::styled(count, muted)],
        width,
    )];
    let mut actions = Vec::new();
    let mut peek_span = None;
    let mut listed = 0usize;

    // Running background processes first, numbered as `/ps` and `/kill` number them,
    // so `focus_background()` lands on the first background process.
    let peek_open =
        |process_id: &str| panel.overlay_open() && panel.peek.as_deref() == Some(process_id);
    let peek = |process_id: &str| {
        state
            .background_processes
            .peek()
            .filter(|peek| peek.process_id == process_id)
    };
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |since| {
            i64::try_from(since.as_millis()).unwrap_or(i64::MAX)
        });
    for (index, process) in running_processes.iter().enumerate() {
        if listed >= shown {
            break;
        }
        listed += 1;
        let elapsed = process
            .started_at_ms
            .and_then(|started| u64::try_from(now_ms.checked_sub(started)?).ok())
            .map(crate::tool_cards::format_duration)
            .unwrap_or_else(|| "running".to_owned());
        let lead = format!("{} ● ", index + 1);
        let room = width.saturating_sub(elapsed.width() + lead.width() + 1);
        actions.push((
            lines.len(),
            PanelAction::Toggle(Disclosure::Process(process.process_id.clone())),
        ));
        lines.push(spread(
            vec![
                Span::styled(lead, Style::default().fg(palette.secondary)),
                Span::raw(ui::clip_columns(&first_line(&process.command), room)),
            ],
            vec![Span::styled(elapsed, muted)],
            width,
        ));
        if peek_open(&process.process_id) {
            let first = lines.len() - 1;
            lines.extend(peek::peek_lines(
                peek(&process.process_id),
                None,
                panel.confirm_stop,
                &process.command,
                width,
                palette,
            ));
            peek_span = Some((first, lines.len() - 1));
        } else if let Some(latest) = process_latest_line(state, &process.process_id) {
            lines.push(Line::styled(
                format!("  {}", ui::clip_columns(&latest, width.saturating_sub(2))),
                muted,
            ));
        }
    }

    // Running subagents.
    for subagent in running_subagents {
        if listed >= shown {
            break;
        }
        listed += 1;
        let state_label = subagent
            .elapsed
            .clone()
            .unwrap_or_else(|| "running".to_owned());
        let lead = "● ";
        let room = width.saturating_sub(state_label.width() + lead.width() + 1);
        actions.push((
            lines.len(),
            PanelAction::Open(Target::Subagent(subagent.call_id.to_owned())),
        ));
        lines.push(spread(
            vec![
                Span::styled(lead, Style::default().fg(palette.secondary)),
                Span::raw(ui::clip_columns(&subagent.subject, room)),
            ],
            vec![Span::styled(state_label, muted)],
            width,
        ));
        if let Some(latest) = &subagent.latest_line {
            lines.push(Line::styled(
                format!("  {}", ui::clip_columns(latest, width.saturating_sub(2))),
                muted,
            ));
        }
    }

    // Finished subagents from the current/latest turn.
    for subagent in finished_subagents {
        if listed >= shown {
            break;
        }
        listed += 1;
        let (mark, color, state_word) = match subagent.status {
            ToolStatus::Done => ("✓ ", palette.success, "[done]"),
            ToolStatus::Cancelled => ("■ ", palette.muted, "stopped"),
            _ => ("✗ ", palette.error, "failed"),
        };
        let label = match &subagent.elapsed {
            Some(elapsed) => format!("{elapsed} {state_word}"),
            None => state_word.to_owned(),
        };
        let room = width.saturating_sub(label.width() + mark.width() + 1);
        actions.push((
            lines.len(),
            PanelAction::Open(Target::Subagent(subagent.call_id.to_owned())),
        ));
        lines.push(spread(
            vec![
                Span::styled(mark, Style::default().fg(color)),
                Span::styled(ui::clip_columns(&subagent.subject, room), muted),
            ],
            vec![Span::styled(label, muted)],
            width,
        ));
    }

    // Finished background processes held by the backend.
    // The docked panel leaves clean exits to the `+N more · /ps` count so only
    // running or failed processes take a row; with focus every process is listed.
    let finished_rows = finished_processes
        .iter()
        .filter(|process| panel.overlay_open() || process.state != ProcessDisplayState::Completed)
        .take(shown.saturating_sub(listed));
    for process in finished_rows {
        listed += 1;
        let (mark, color, label) = match process.state {
            ProcessDisplayState::Completed => ("✓ ", palette.success, "[done]".to_owned()),
            ProcessDisplayState::Cancelled => ("■ ", palette.muted, peek::outcome(process)),
            ProcessDisplayState::TimedOut => ("✗ ", palette.warning, peek::outcome(process)),
            _ => ("✗ ", palette.error, peek::outcome(process)),
        };
        let room = width.saturating_sub(label.width() + mark.width() + 1);
        actions.push((
            lines.len(),
            PanelAction::Toggle(Disclosure::Process(process.process_id.clone())),
        ));
        lines.push(spread(
            vec![
                Span::styled(mark, Style::default().fg(color)),
                Span::styled(ui::clip_columns(&first_line(&process.command), room), muted),
            ],
            vec![Span::styled(label, muted)],
            width,
        ));
        if peek_open(&process.process_id) {
            let first = lines.len() - 1;
            lines.extend(peek::peek_lines(
                peek(&process.process_id),
                Some(process),
                false,
                &process.command,
                width,
                palette,
            ));
            peek_span = Some((first, lines.len() - 1));
        }
    }

    let unlisted = (total_running + total_finished).saturating_sub(listed);
    if unlisted > 0 {
        let suffix = if !running_processes.is_empty() || !finished_processes.is_empty() {
            format!("+{unlisted} more · /ps")
        } else {
            format!("+{unlisted} more")
        };
        lines.push(Line::styled(suffix, muted));
    }
    Some(RunningRows {
        lines,
        actions,
        peek: peek_span,
    })
}

/// Collect running subagents from anywhere in the transcript, plus finished
/// subagents from the latest turn.
fn collect_subagents(state: &UiState) -> Vec<SubagentRow<'_>> {
    let entries = state.transcript.entries();
    let latest_turn_start = entries
        .iter()
        .rposition(|entry| entry.role == TranscriptRole::User)
        .unwrap_or(0);
    let mut rows = Vec::new();
    for (index, entry) in entries.iter().enumerate() {
        let Some(card) = entry.tool_card() else {
            continue;
        };
        let Some(script) = card.script.as_deref() else {
            continue;
        };
        if script.host != NestedHost::Subagent {
            continue;
        }
        if card.status.terminal() && index < latest_turn_start {
            continue;
        }
        let subject = sanitize_for_terminal(&script.subject());
        let elapsed = if card.status.terminal() {
            card.duration_ms
                .map(|ms| crate::tool_cards::format_duration(u64::from(ms)))
        } else {
            entry.running_elapsed().map(duration)
        };
        let latest_line = if !card.status.terminal() {
            script
                .calls
                .last()
                .map(|call| sanitize_for_terminal(&call.tool))
                .or_else(|| card.live_line().map(sanitize_for_terminal))
                .filter(|line| !line.trim().is_empty())
        } else {
            None
        };
        rows.push(SubagentRow {
            call_id: &card.call_id,
            subject,
            status: card.status,
            elapsed,
            latest_line,
        });
    }
    rows
}

/// The latest non-empty output line known for a running background process, from
/// its live peek or its transcript process card.
fn process_latest_line(state: &UiState, process_id: &str) -> Option<String> {
    if let Some(peek) = state
        .background_processes
        .peek()
        .filter(|peek| peek.process_id == process_id)
    {
        if let Some(output) = &peek.output {
            if let Some((_, line)) = peek::output_lines(&output.chunks, 1).pop() {
                if !line.trim().is_empty() {
                    return Some(line);
                }
            }
        }
    }
    let entry_id = state.transcript.process_card(process_id)?;
    let index = state.transcript.entry_index(entry_id)?;
    let card = state.transcript.entries()[index].process_card()?;
    peek::latest_line(&card.retained_output.text)
}
