//! Turn and subagent swimlane waterfall timeline for the `Session` side-panel tab.
//!
//! Renders wall-clock horizontal Gantt spans in the expanded or dedicated timeline
//! view (`f` / `t`), grouping overlapping tool batches and expanding nested subagent
//! and codemode tool calls. Falls back to compact duration bars on narrow widths.

use super::*;
use crate::script_card::{NestedHost, ScriptCall, ScriptCallState};
use crate::tool_cards::{ToolCardSnapshot, ToolStatus, format_duration};
use crate::transcript::{RunTotals, TranscriptEntry, TranscriptRole};
use crate::ui::sanitize_for_terminal;
use ratatui::style::Color;
use std::collections::BTreeSet;

const WIDE_WATERFALL_MIN_WIDTH: usize = 72;
const LABEL_COL_MIN: usize = 18;
const LABEL_COL_MAX: usize = 24;
const NARROW_BAR_WIDTH: usize = 6;
const PROMPT_PREVIEW_MAX_COLS: usize = 48;

fn sanitize_single_line(text: &str) -> String {
    sanitize_for_terminal(text).replace('\n', " ")
}

fn contains_point(rect: Rect, column: u16, row: u16) -> bool {
    !rect.is_empty()
        && column >= rect.x
        && column < rect.right()
        && row >= rect.y
        && row < rect.bottom()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SpanStatus {
    Running,
    Done,
    Error,
    Cancelled,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum SpanKind {
    Thinking {
        preview: String,
    },
    Tool {
        call_id: String,
        tool_name: String,
        detail: String,
    },
    ParallelBatch {
        batch_number: usize,
        count: usize,
    },
    Subagent {
        call_id: String,
        subagent_number: usize,
        subject: String,
        task: String,
        omitted: u64,
    },
    Script {
        call_id: String,
        title: String,
        omitted: u64,
    },
    NestedCall {
        parent_key: String,
        tool: String,
        error: Option<String>,
    },
    FinalReply {
        outcome: String,
        output_tokens: Option<u64>,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct TimelineSpan {
    key: String,
    label: String,
    kind: SpanKind,
    start_ms: u64,
    duration_ms: u64,
    started_at_ms: Option<i64>,
    status: SpanStatus,
    children: Vec<TimelineSpan>,
}

impl TimelineSpan {
    fn end_ms(&self) -> u64 {
        self.start_ms.saturating_add(self.duration_ms)
    }

    fn collapsible(&self) -> bool {
        !self.children.is_empty()
    }

    fn target(&self) -> Option<Target> {
        match &self.kind {
            SpanKind::Subagent { call_id, .. } => Some(Target::Subagent(call_id.clone())),
            SpanKind::Tool { call_id, .. } | SpanKind::Script { call_id, .. } => {
                Some(Target::ToolCall(call_id.clone()))
            }
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct TurnTimeline {
    turn_number: usize,
    prompt: String,
    total_ms: u64,
    parallel_batches: usize,
    tool_calls: usize,
    subagents: usize,
    spans: Vec<TimelineSpan>,
}

#[derive(Clone, Debug)]
struct VisibleRow {
    key: String,
    span: TimelineSpan,
    is_child: bool,
    expanded: bool,
    line: usize,
}

#[derive(Debug, Default)]
pub(super) struct TimelineState {
    selected_turn: Option<usize>,
    selected_index: usize,
    selected_key: Option<String>,
    collapsed_keys: BTreeSet<String>,
    scroll: Cell<usize>,
    reveal_selection: Cell<bool>,
    painted_rows: RefCell<Vec<VisibleRow>>,
    painted_list_rect: Cell<Rect>,
    painted_list_total: Cell<usize>,
}

impl TimelineState {
    pub(super) fn reset_for_session(&mut self) {
        self.selected_turn = None;
        self.selected_index = 0;
        self.selected_key = None;
        self.collapsed_keys.clear();
        self.scroll.set(0);
        self.reveal_selection.set(true);
        self.painted_rows.borrow_mut().clear();
    }

    pub(super) fn scroll_by(&self, delta: i32) {
        let height = usize::from(self.painted_list_rect.get().height).max(1);
        let max = self.painted_list_total.get().saturating_sub(height);
        let next = self
            .scroll
            .get()
            .saturating_add_signed(delta as isize)
            .min(max);
        self.scroll.set(next);
    }

    fn is_expanded(&self, key: &str) -> bool {
        !self.collapsed_keys.contains(key)
    }

    fn set_expanded(&mut self, key: &str, expanded: bool) {
        self.reveal_selection.set(true);
        if expanded {
            self.collapsed_keys.remove(key);
        } else {
            self.collapsed_keys.insert(key.to_owned());
        }
    }

    fn selected_resolved_index(&self) -> Option<usize> {
        let rows = self.painted_rows.borrow();
        if rows.is_empty() {
            return None;
        }
        if let Some(key) = &self.selected_key
            && let Some(pos) = rows.iter().position(|row| row.key == *key)
        {
            return Some(pos);
        }
        Some(self.selected_index.min(rows.len() - 1))
    }

    fn selected_row(&self) -> Option<VisibleRow> {
        let idx = self.selected_resolved_index()?;
        self.painted_rows.borrow().get(idx).cloned()
    }

    fn select_index(&mut self, index: usize) {
        let rows = self.painted_rows.borrow();
        let Some(row) = rows.get(index) else {
            self.selected_index = 0;
            self.selected_key = None;
            return;
        };
        let key = row.key.clone();
        let line = row.line;
        drop(rows);
        self.selected_index = index;
        self.selected_key = Some(key);
        self.reveal_selection.set(true);
        self.ensure_selected_visible(line);
    }

    fn ensure_selected_visible(&self, line: usize) {
        let height = usize::from(self.painted_list_rect.get().height).max(1);
        let mut offset = self.scroll.get();
        if line < offset {
            offset = line;
        } else if line >= offset + height {
            offset = line + 1 - height;
        }
        let max_scroll = self.painted_list_total.get().saturating_sub(height);
        self.scroll.set(offset.min(max_scroll));
    }

    fn move_selection(&mut self, delta: isize) -> OverlayKey {
        let len = self.painted_rows.borrow().len();
        if len == 0 {
            return OverlayKey::Moved;
        }
        let cur = self.selected_resolved_index().unwrap_or(0) as isize;
        let next = (cur + delta).clamp(0, (len - 1) as isize) as usize;
        self.select_index(next);
        OverlayKey::Moved
    }

    fn select_key(&mut self, key: &str) {
        let pos = self
            .painted_rows
            .borrow()
            .iter()
            .position(|row| row.key == key);
        if let Some(idx) = pos {
            self.select_index(idx);
        }
    }

    fn collapse_or_parent(&mut self) -> OverlayKey {
        let Some(row) = self.selected_row() else {
            return OverlayKey::Moved;
        };
        if row.span.collapsible() && row.expanded {
            self.set_expanded(&row.key, false);
        } else if let SpanKind::NestedCall { parent_key, .. } = &row.span.kind {
            self.select_key(parent_key);
        } else if row.is_child {
            // Child of a ParallelBatch: walk backwards to the preceding batch row.
            let cur = self.selected_resolved_index().unwrap_or(0);
            let parent_key = self.painted_rows.borrow()[..cur]
                .iter()
                .rev()
                .find(|candidate| !candidate.is_child)
                .map(|candidate| candidate.key.clone());
            if let Some(key) = parent_key {
                self.select_key(&key);
            }
        }
        OverlayKey::Moved
    }

    fn expand_selected(&mut self) -> OverlayKey {
        if let Some(row) = self.selected_row()
            && row.span.collapsible()
        {
            self.set_expanded(&row.key, true);
        }
        OverlayKey::Moved
    }

    fn step_turn(&mut self, delta: isize, turn_count: usize) -> OverlayKey {
        if turn_count == 0 {
            return OverlayKey::Ignored;
        }
        let current = self
            .selected_turn
            .unwrap_or(turn_count)
            .clamp(1, turn_count);
        let next = (current as isize + delta).clamp(1, turn_count as isize) as usize;
        if next != current || self.selected_turn.is_none() {
            self.selected_turn = Some(next);
            self.selected_index = 0;
            self.selected_key = None;
            self.scroll.set(0);
            self.reveal_selection.set(true);
        }
        OverlayKey::Moved
    }

    pub(super) fn handle_key(&mut self, key: KeyCode, state: &UiState) -> OverlayKey {
        match key {
            KeyCode::Up => self.move_selection(-1),
            KeyCode::Down => self.move_selection(1),
            KeyCode::Home => {
                self.select_index(0);
                OverlayKey::Moved
            }
            KeyCode::End => {
                let len = self.painted_rows.borrow().len();
                if len > 0 {
                    self.select_index(len - 1);
                }
                OverlayKey::Moved
            }
            KeyCode::Left => self.collapse_or_parent(),
            KeyCode::Right => self.expand_selected(),
            KeyCode::Char(' ') => {
                if let Some(row) = self.selected_row()
                    && row.span.collapsible()
                {
                    self.set_expanded(&row.key, !row.expanded);
                }
                OverlayKey::Moved
            }
            KeyCode::Enter => {
                let Some(row) = self.selected_row() else {
                    return OverlayKey::Ignored;
                };
                if let Some(target) = row.span.target() {
                    return OverlayKey::Open(target);
                }
                if let SpanKind::NestedCall { parent_key, .. } = &row.span.kind {
                    // Jump up to the parent's card when inspecting a nested subagent/script call.
                    let target = self
                        .painted_rows
                        .borrow()
                        .iter()
                        .find(|candidate| candidate.key == *parent_key)
                        .and_then(|parent| parent.span.target());
                    if let Some(target) = target {
                        return OverlayKey::Open(target);
                    }
                }
                if row.span.collapsible() {
                    self.set_expanded(&row.key, !row.expanded);
                    return OverlayKey::Moved;
                }
                OverlayKey::Ignored
            }
            KeyCode::PageUp => {
                let page = i32::from(self.painted_list_rect.get().height.max(1));
                self.scroll_by(-page);
                OverlayKey::Moved
            }
            KeyCode::PageDown => {
                let page = i32::from(self.painted_list_rect.get().height.max(1));
                self.scroll_by(page);
                OverlayKey::Moved
            }
            KeyCode::Char('<' | 'p') => {
                let turns = collect_turns(state.transcript.entries());
                self.step_turn(-1, turns.len())
            }
            KeyCode::Char('>' | 'n') => {
                let turns = collect_turns(state.transcript.entries());
                self.step_turn(1, turns.len())
            }
            _ => OverlayKey::Ignored,
        }
    }

    pub(super) fn click(&mut self, column: u16, row: u16) -> OverlayKey {
        let rect = self.painted_list_rect.get();
        if !contains_point(rect, column, row) {
            return OverlayKey::Ignored;
        }
        let line = usize::from(row - rect.y) + self.scroll.get();
        let found = self
            .painted_rows
            .borrow()
            .iter()
            .position(|entry| entry.line == line);
        let Some(idx) = found else {
            return OverlayKey::Ignored;
        };
        let was_selected = self.selected_resolved_index() == Some(idx);
        self.select_index(idx);
        let clicked = self.painted_rows.borrow().get(idx).cloned();
        if let Some(clicked) = clicked
            && clicked.span.collapsible()
            && was_selected
        {
            self.set_expanded(&clicked.key, !clicked.expanded);
        }
        OverlayKey::Moved
    }
}

/// One-line summary of the latest turn for the docked `Session` panel, when
/// at least one turn has tool, subagent, script, or thinking spans.
pub(super) fn latest_turn_summary(state: &UiState) -> Option<String> {
    let turns = collect_turns(state.transcript.entries());
    let turn = turns.last()?;
    let interesting = turn
        .spans
        .iter()
        .any(|span| !matches!(span.kind, SpanKind::FinalReply { .. }));
    if !interesting {
        return None;
    }
    let mut parts = vec![format!("Turn #{}", turn.turn_number)];
    if turn.total_ms > 0 {
        parts.push(format_duration(turn.total_ms));
    }
    if turn.subagents > 0 {
        let word = if turn.subagents == 1 {
            "subagent"
        } else {
            "subagents"
        };
        parts.push(format!("{} {word}", turn.subagents));
    } else if turn.parallel_batches > 0 {
        parts.push(format!("{} // batch", turn.parallel_batches));
    } else if turn.tool_calls > 0 {
        let word = if turn.tool_calls == 1 {
            "tool"
        } else {
            "tools"
        };
        parts.push(format!("{} {word}", turn.tool_calls));
    }
    Some(parts.join(" · "))
}

pub(super) fn render(
    frame: &mut Frame<'_>,
    inner: Rect,
    surface: Rect,
    state: &UiState,
    panel: &SidePanel,
    palette: Palette,
) {
    let width = usize::from(inner.width);
    let height = usize::from(inner.height);
    if width == 0 || height == 0 {
        return;
    }

    let mut top_rows = PanelRows::default();
    if panel.overlay_open() || panel.expanded() {
        browser::push_controls(&mut top_rows, panel, width, palette);
    }
    let controls_lines_len = top_rows.lines.len();
    let controls_rect = Rect {
        height: (controls_lines_len as u16).min(inner.height),
        ..inner
    };
    if !top_rows.lines.is_empty() {
        frame.render_widget(Paragraph::new(top_rows.lines), controls_rect);
    }

    let remaining = Rect {
        y: inner.y + controls_rect.height,
        height: inner.height.saturating_sub(controls_rect.height),
        ..inner
    };
    if remaining.height == 0 {
        *panel.painted.borrow_mut() = Painted {
            surface,
            area: controls_rect,
            total: controls_lines_len,
            actions: top_rows.actions,
            ..Painted::default()
        };
        return;
    }

    let turns = collect_turns(state.transcript.entries());
    let turn_count = turns.len();
    let wide = width >= WIDE_WATERFALL_MIN_WIDTH;

    let footer_height =
        usize::from(panel.overlay_open() || panel.expanded() || remaining.height >= 4);
    let body_area = Rect {
        height: remaining.height.saturating_sub(footer_height as u16),
        ..remaining
    };

    if turns.is_empty() {
        panel
            .timeline_view
            .borrow()
            .painted_rows
            .borrow_mut()
            .clear();
        panel
            .timeline_view
            .borrow()
            .painted_list_rect
            .set(body_area);
        panel.timeline_view.borrow().painted_list_total.set(1);
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(heading("TURN TIMELINE", palette)),
                Line::styled(
                    "No turns recorded yet. Run a prompt to inspect timing and subagents.",
                    Style::default().fg(palette.muted),
                ),
            ]),
            body_area,
        );
    } else {
        let selected_turn_num = panel
            .timeline_view
            .borrow()
            .selected_turn
            .unwrap_or(turn_count)
            .clamp(1, turn_count);
        let turn = &turns[selected_turn_num - 1];

        let header_lines = build_header_lines(turn, turn_count, wide, width, palette);
        let header_height = (header_lines.len() as u16).min(body_area.height);
        let header_rect = Rect {
            height: header_height,
            ..body_area
        };
        frame.render_widget(Paragraph::new(header_lines), header_rect);

        let after_header = Rect {
            y: body_area.y + header_height,
            height: body_area.height.saturating_sub(header_height),
            ..body_area
        };

        let inspect_height: u16 = if after_header.height >= 8 {
            if wide { 4 } else { 2 }
        } else {
            0
        };
        let list_rect = Rect {
            height: after_header.height.saturating_sub(inspect_height),
            ..after_header
        };
        let inspect_rect = Rect {
            y: list_rect.bottom(),
            height: inspect_height,
            ..after_header
        };

        let (mut rows, mut lines) = {
            let view = panel.timeline_view.borrow();
            build_swimlane_rows(turn, &view, wide, width, palette)
        };
        let total = lines.len();
        let list_height = usize::from(list_rect.height);

        let selected_row = {
            let mut view = panel.timeline_view.borrow_mut();
            view.painted_list_rect.set(list_rect);
            view.painted_list_total.set(total);
            *view.painted_rows.borrow_mut() = rows.clone();
            let prev_idx = view.selected_index;
            let reveal = view.reveal_selection.replace(false);
            let idx = view.selected_resolved_index();
            if let Some(idx) = idx {
                let row = &rows[idx];
                view.selected_index = idx;
                view.selected_key = Some(row.key.clone());
                if reveal || prev_idx != idx {
                    view.ensure_selected_visible(row.line);
                }
                Some((idx, row.clone()))
            } else {
                None
            }
        };

        if let Some((idx, _)) = &selected_row
            && let Some(line) = lines.get_mut(rows[*idx].line)
        {
            let padding = width.saturating_sub(line.width());
            line.spans.push(Span::raw(" ".repeat(padding)));
            *line = palette.selected_line(std::mem::take(line));
        }

        let max_scroll = total.saturating_sub(list_height.max(1));
        let offset = panel.timeline_view.borrow().scroll.get().min(max_scroll);
        panel.timeline_view.borrow().scroll.set(offset);
        let visible: Vec<_> = lines.into_iter().skip(offset).take(list_height).collect();
        frame.render_widget(Paragraph::new(visible), list_rect);

        if inspect_height > 0 {
            render_inspect_pane(
                frame,
                inspect_rect,
                turn,
                selected_row.as_ref().map(|(_, row)| row),
                wide,
                width,
                palette,
            );
        }
        rows.clear();
    }

    if footer_height > 0 {
        let footer = Rect {
            y: body_area.bottom(),
            height: footer_height as u16,
            ..remaining
        };
        let hint = if wide {
            if panel.expanded() {
                "↑↓ span · ←→ fold · Enter inspect · < > turn · f restore · Esc"
            } else {
                "↑↓ span · ←→ fold · Enter inspect · < > turn · t session · Esc"
            }
        } else if panel.expanded() {
            "↑↓ span · ←→ fold · < > turn · f restore · Esc"
        } else {
            "↑↓ span · Enter inspect · f expand · t session"
        };
        frame.render_widget(Paragraph::new(ui::keycap_hints(hint, palette)), footer);
    }

    // Top controls sit in an unscrolled header; keep panel.scroll at 0 so clicks hit them cleanly.
    panel.scroll.set(0);
    *panel.painted.borrow_mut() = Painted {
        surface,
        area: controls_rect,
        total: controls_lines_len,
        actions: top_rows.actions,
        ..Painted::default()
    };
}

fn build_header_lines(
    turn: &TurnTimeline,
    turn_count: usize,
    wide: bool,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let dim = Style::default().fg(palette.dim());

    let mut summary_parts = vec![
        format!("TURN #{} TIMELINE", turn.turn_number),
        format_duration(turn.total_ms),
    ];
    if turn.parallel_batches > 0 {
        let word = if turn.parallel_batches == 1 {
            "parallel batch"
        } else {
            "parallel batches"
        };
        summary_parts.push(format!("{} {word}", turn.parallel_batches));
    }
    if turn.subagents > 0 {
        let word = if turn.subagents == 1 {
            "subagent"
        } else {
            "subagents"
        };
        summary_parts.push(format!("{} {word}", turn.subagents));
    } else if turn.tool_calls > 0 {
        let word = if turn.tool_calls == 1 {
            "tool"
        } else {
            "tools"
        };
        summary_parts.push(format!("{} {word}", turn.tool_calls));
    }

    let right = if turn_count > 1 {
        vec![Span::styled(
            format!("< Turn {}/{} >", turn.turn_number, turn_count),
            muted,
        )]
    } else {
        Vec::new()
    };
    let title_line = spread(
        vec![heading(&summary_parts.join(" · "), palette)],
        right,
        width,
    );

    if !wide {
        return vec![title_line];
    }

    let prompt_snippet = sanitize_single_line(&turn.prompt);
    let prompt_line = Line::styled(
        ui::clip_columns(&prompt_snippet, width.min(PROMPT_PREVIEW_MAX_COLS + 20)),
        muted,
    );

    let (label_w, bar_w, dur_w) = wide_columns(width);
    // Align ruler with `render_span_line`: `"│ "` (2) + `label` (`label_w`) + `" "` (1).
    let prefix_pad = " ".repeat(label_w + 3);
    let ruler_labels = axis_label_line(turn.total_ms, bar_w);
    let ruler_ticks = axis_tick_line(bar_w);

    vec![
        title_line,
        prompt_line,
        Line::from(vec![
            Span::raw(prefix_pad.clone()),
            Span::styled(ruler_labels, dim),
            Span::raw(" ".repeat(dur_w + 2)),
        ]),
        Line::from(vec![Span::raw(prefix_pad), Span::styled(ruler_ticks, dim)]),
    ]
}

fn wide_columns(width: usize) -> (usize, usize, usize) {
    let label_w = (width / 4).clamp(LABEL_COL_MIN, LABEL_COL_MAX);
    let dur_w = 8;
    // Layout: `│ ` (2) + label + ` ` (1) + bar + ` ●` (2) + dur
    let bar_w = width.saturating_sub(label_w + dur_w + 5).max(16);
    (label_w, bar_w, dur_w)
}

fn axis_label_line(total_ms: u64, bar_w: usize) -> String {
    if bar_w < 12 {
        return "0s".to_owned();
    }
    let mut row = vec![' '; bar_w];
    let ticks = [0usize, bar_w / 4, bar_w / 2, (bar_w * 3) / 4, bar_w - 1];
    for (idx, &col) in ticks.iter().enumerate() {
        let ms = if idx == 4 {
            total_ms
        } else {
            (total_ms.saturating_mul(col as u64)) / (bar_w.saturating_sub(1).max(1) as u64)
        };
        let text = if ms == 0 {
            "0.0s".to_owned()
        } else {
            format_axis_ms(ms)
        };
        let start = if idx == 4 {
            bar_w.saturating_sub(text.chars().count())
        } else {
            col.min(bar_w.saturating_sub(text.chars().count()))
        };
        // Avoid colliding adjacent labels.
        let end = start + text.chars().count();
        if row[start..end].iter().all(|c| *c == ' ') {
            for (offset, ch) in text.chars().enumerate() {
                row[start + offset] = ch;
            }
        }
    }
    row.into_iter().collect()
}

fn axis_tick_line(bar_w: usize) -> String {
    if bar_w < 4 {
        return "─".repeat(bar_w);
    }
    let mut ticks: Vec<char> = vec!['─'; bar_w];
    for pos in [0, bar_w / 4, bar_w / 2, (bar_w * 3) / 4, bar_w - 1] {
        ticks[pos] = if pos == 0 {
            '├'
        } else if pos == bar_w - 1 {
            '┤'
        } else {
            '┼'
        };
    }
    ticks.into_iter().collect()
}

fn format_axis_ms(ms: u64) -> String {
    if ms < 1_000 {
        format!("{ms}ms")
    } else if ms < 60_000 {
        format!("{:.1}s", ms as f64 / 1_000.0)
    } else {
        format_duration(ms)
    }
}

fn build_swimlane_rows(
    turn: &TurnTimeline,
    view: &TimelineState,
    wide: bool,
    width: usize,
    palette: Palette,
) -> (Vec<VisibleRow>, Vec<Line<'static>>) {
    let mut rows = Vec::new();
    let mut lines = Vec::new();

    for span in &turn.spans {
        let expanded = view.is_expanded(&span.key);
        let line_idx = lines.len();
        lines.push(render_span_line(
            span,
            false,
            false,
            expanded,
            turn.total_ms,
            wide,
            width,
            palette,
        ));
        rows.push(VisibleRow {
            key: span.key.clone(),
            span: span.clone(),
            is_child: false,
            expanded,
            line: line_idx,
        });

        if span.collapsible() && expanded {
            let count = span.children.len();
            for (idx, child) in span.children.iter().enumerate() {
                let is_last = idx + 1 == count;
                let child_line_idx = lines.len();
                lines.push(render_span_line(
                    child,
                    true,
                    is_last,
                    false,
                    turn.total_ms,
                    wide,
                    width,
                    palette,
                ));
                rows.push(VisibleRow {
                    key: child.key.clone(),
                    span: child.clone(),
                    is_child: true,
                    expanded: false,
                    line: child_line_idx,
                });
            }
        }
    }

    (rows, lines)
}

#[allow(clippy::too_many_arguments)]
fn render_span_line(
    span: &TimelineSpan,
    is_child: bool,
    is_last_child: bool,
    expanded: bool,
    total_ms: u64,
    wide: bool,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let dim = Style::default().fg(palette.dim());
    let muted = Style::default().fg(palette.muted);
    let dur_text = if span.status == SpanStatus::Running && span.duration_ms == 0 {
        "live".to_owned()
    } else {
        format_duration(span.duration_ms)
    };
    let bar_color = span_color(span, palette);
    let bar_style = Style::default().fg(bar_color);
    let label_style = if span.status == SpanStatus::Error {
        Style::default().fg(palette.error)
    } else if is_child {
        muted
    } else {
        Style::default().fg(palette.foreground)
    };

    if !wide {
        let prefix = if span.collapsible() {
            if expanded { "▾ " } else { "▸ " }
        } else if is_child {
            if is_last_child { " └" } else { " ├" }
        } else {
            "  "
        };
        let fill_cols = ((span.duration_ms.saturating_mul(NARROW_BAR_WIDTH as u64))
            .div_ceil(total_ms.max(1)))
        .clamp(1, NARROW_BAR_WIDTH as u64) as usize;
        let glyph = match span.kind {
            SpanKind::Thinking { .. } => "▓",
            _ => "█",
        };
        let bar = format!(
            "{}{}",
            glyph.repeat(fill_cols),
            "░".repeat(NARROW_BAR_WIDTH.saturating_sub(fill_cols))
        );
        let right = vec![
            Span::styled(bar, bar_style),
            Span::raw(" "),
            Span::styled(format!("{dur_text:>6}"), muted),
        ];
        let right_w: usize = right.iter().map(|s| s.content.width()).sum();
        let label_room = width.saturating_sub(right_w + prefix.width() + 1);
        let clipped_label = ui::clip_columns(&sanitize_single_line(&span.label), label_room);
        return spread(
            vec![
                Span::styled(prefix, dim),
                Span::styled(clipped_label, label_style),
            ],
            right,
            width,
        );
    }

    let (label_w, bar_w, dur_w) = wide_columns(width);
    let prefix = if span.collapsible() {
        if expanded { "▾ " } else { "▸ " }
    } else if is_child {
        if is_last_child { " └─" } else { " ├─" }
    } else {
        "  "
    };
    let text_room = label_w.saturating_sub(prefix.width());
    let clipped = ui::clip_columns(&sanitize_single_line(&span.label), text_room);
    let pad = " ".repeat(label_w.saturating_sub(prefix.width() + clipped.width()));

    let (lead, body, tail) = gantt_bar(span, total_ms, bar_w);
    let status_dot = match span.status {
        SpanStatus::Running => Span::styled(" ●", Style::default().fg(palette.secondary)),
        SpanStatus::Error => Span::styled(" ✗", Style::default().fg(palette.error)),
        SpanStatus::Cancelled => Span::styled(" ■", muted),
        SpanStatus::Done => Span::raw("  "),
    };

    Line::from(vec![
        Span::styled("│ ", dim),
        Span::styled(prefix, dim),
        Span::styled(clipped, label_style),
        Span::raw(pad),
        Span::styled(" ", dim),
        Span::styled(lead, dim),
        Span::styled(body, bar_style),
        Span::styled(tail, dim),
        status_dot,
        Span::styled(format!("{dur_text:>dur_w$}"), muted),
    ])
}

fn gantt_bar(span: &TimelineSpan, total_ms: u64, bar_w: usize) -> (String, String, String) {
    let total = total_ms.max(1);
    let start_col = ((span.start_ms.saturating_mul(bar_w as u64)) / total)
        .min(bar_w.saturating_sub(1) as u64) as usize;
    let end_ms = span.end_ms().min(total);
    let mut end_col = ((end_ms.saturating_mul(bar_w as u64)).div_ceil(total)) as usize;
    if end_col <= start_col {
        end_col = (start_col + 1).min(bar_w);
    }
    let fill_len = end_col.saturating_sub(start_col).max(1);
    let trail_len = bar_w.saturating_sub(start_col + fill_len);

    let lead = "·".repeat(start_col);
    let body = match span.kind {
        SpanKind::Thinking { .. } => "▓".repeat(fill_len),
        _ if span.status == SpanStatus::Running && fill_len >= 2 => {
            format!("{}▓", "█".repeat(fill_len - 1))
        }
        _ => "█".repeat(fill_len),
    };
    let tail = "░".repeat(trail_len);
    (lead, body, tail)
}

fn span_color(span: &TimelineSpan, palette: Palette) -> Color {
    match span.status {
        SpanStatus::Error => palette.error,
        SpanStatus::Cancelled => palette.muted,
        SpanStatus::Running | SpanStatus::Done => match &span.kind {
            SpanKind::Thinking { .. } => palette.primary,
            SpanKind::ParallelBatch { .. } => palette.secondary,
            SpanKind::Subagent { .. } => palette.heading_2,
            SpanKind::Script { .. } => palette.heading_3,
            SpanKind::Tool { .. } | SpanKind::NestedCall { .. } => palette.secondary,
            SpanKind::FinalReply { .. } => palette.success,
        },
    }
}

fn render_inspect_pane(
    frame: &mut Frame<'_>,
    area: Rect,
    turn: &TurnTimeline,
    selected: Option<&VisibleRow>,
    wide: bool,
    width: usize,
    palette: Palette,
) {
    if area.is_empty() {
        return;
    }
    let dim = Style::default().fg(palette.dim());
    let muted = Style::default().fg(palette.muted);
    let mut lines = Vec::new();
    lines.push(Line::styled("─".repeat(width), dim));

    let Some(row) = selected else {
        lines.push(Line::styled("No span selected.", muted));
        frame.render_widget(Paragraph::new(lines), area);
        return;
    };

    let span = &row.span;
    let status_word = match span.status {
        SpanStatus::Running => "running",
        SpanStatus::Done => "ok",
        SpanStatus::Error => "error",
        SpanStatus::Cancelled => "cancelled",
    };
    let window = format!(
        "{} ({} → {})",
        format_duration(span.duration_ms),
        format_axis_ms(span.start_ms),
        format_axis_ms(span.end_ms().min(turn.total_ms))
    );
    let header_text = format!(
        "INSPECT · {} · {status_word} · {window}",
        sanitize_single_line(&span.label)
    );
    lines.push(Line::styled(
        ui::clip_columns(&header_text, width),
        Style::default()
            .fg(palette.foreground)
            .add_modifier(Modifier::BOLD),
    ));

    if wide {
        let detail = match &span.kind {
            SpanKind::Thinking { preview } => {
                if preview.is_empty() {
                    "Model reasoning before tool/reply output".to_owned()
                } else {
                    format!("Reasoning: {preview}")
                }
            }
            SpanKind::Tool { detail, .. } => {
                if detail.is_empty() {
                    "Tool execution".to_owned()
                } else {
                    detail.clone()
                }
            }
            SpanKind::ParallelBatch { count, .. } => {
                format!("{count} concurrent tool calls in one batch · Space/←/→ fold children")
            }
            SpanKind::Subagent {
                subject,
                task,
                omitted,
                ..
            } => {
                let body = if task.is_empty() { subject } else { task };
                let extra = if *omitted > 0 {
                    format!(" (+{omitted} omitted calls)")
                } else {
                    String::new()
                };
                format!("Task: {body}{extra} · Enter open detail card")
            }
            SpanKind::Script { title, omitted, .. } => {
                let extra = if *omitted > 0 {
                    format!(" (+{omitted} omitted calls)")
                } else {
                    String::new()
                };
                format!("Codemode script: {title}{extra} · Enter open detail card")
            }
            SpanKind::NestedCall { error, .. } => match error {
                Some(err) => format!("Error: {err} · Enter open parent card"),
                None => "Nested call inside parent · ← jump to parent · Enter open card".to_owned(),
            },
            SpanKind::FinalReply {
                outcome,
                output_tokens,
            } => match output_tokens {
                Some(tokens) => format!("Turn finished ({outcome}) · {tokens} output tokens"),
                None => format!("Turn finished ({outcome})"),
            },
        };
        lines.push(Line::styled(ui::clip_columns(&detail, width), muted));
        lines.push(Line::styled("─".repeat(width), dim));
    }

    frame.render_widget(Paragraph::new(lines), area);
}

fn collect_turns(entries: &[TranscriptEntry]) -> Vec<TurnTimeline> {
    if entries.is_empty() {
        return Vec::new();
    }
    let mut slices: Vec<(String, &[TranscriptEntry])> = Vec::new();
    let mut start = 0usize;

    for (idx, entry) in entries.iter().enumerate() {
        if entry.role == TranscriptRole::User {
            if idx > start {
                let prompt = if entries[start].role == TranscriptRole::User {
                    sanitize_single_line(&entries[start].content)
                } else {
                    "Session start".to_owned()
                };
                slices.push((prompt, &entries[start..idx]));
            }
            start = idx;
        }
    }
    if start < entries.len() {
        let prompt = if entries[start].role == TranscriptRole::User {
            sanitize_single_line(&entries[start].content)
        } else {
            "Session start".to_owned()
        };
        slices.push((prompt, &entries[start..]));
    }

    let mut turns = Vec::new();
    for (prompt, segment) in slices {
        if let Some(turn) = build_turn(turns.len() + 1, prompt, segment) {
            turns.push(turn);
        }
    }
    turns
}

fn build_turn(
    turn_number: usize,
    prompt: String,
    segment: &[TranscriptEntry],
) -> Option<TurnTimeline> {
    let anchor_ms = segment
        .iter()
        .filter_map(|entry| entry.tool_card().and_then(|card| card.started_at_ms))
        .min();

    let mut raw_spans: Vec<TimelineSpan> = Vec::new();
    let mut cursor_ms = 0u64;
    let mut anchored_offset_ms: Option<u64> = None;
    let mut subagent_counter = 0usize;
    let mut tool_calls = 0usize;
    let mut run_totals: Option<&RunTotals> = None;

    for (idx, entry) in segment.iter().enumerate() {
        if let Some(totals) = entry.run_totals() {
            run_totals = Some(totals);
        }

        if entry.role == TranscriptRole::Assistant
            && (!entry.thinking().is_empty() || entry.thinking_ms().is_some())
        {
            let dur = entry
                .thinking_ms()
                .map(u64::from)
                .unwrap_or_else(|| if entry.is_thinking() { 1 } else { 0 });
            let status = if entry.is_thinking() {
                SpanStatus::Running
            } else {
                SpanStatus::Done
            };
            let preview = sanitize_single_line(
                entry
                    .thinking()
                    .lines()
                    .find(|line| !line.trim().is_empty())
                    .unwrap_or(""),
            );
            raw_spans.push(TimelineSpan {
                key: format!("t{turn_number}-think-{idx}"),
                label: "Thinking".to_owned(),
                kind: SpanKind::Thinking { preview },
                start_ms: cursor_ms,
                duration_ms: dur,
                started_at_ms: None,
                status,
                children: Vec::new(),
            });
            cursor_ms = cursor_ms.saturating_add(dur);
        }

        if let Some(card) = entry.tool_card() {
            tool_calls += 1;
            let dur = card
                .duration_ms
                .map(u64::from)
                .or_else(|| {
                    entry
                        .running_elapsed()
                        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
                })
                .unwrap_or(0);
            let start_ms = match (anchor_ms, card.started_at_ms) {
                (Some(anchor), Some(started)) => {
                    // Preserve any pre-tool Thinking time accumulated before the first anchored call.
                    let offset = *anchored_offset_ms.get_or_insert(cursor_ms);
                    offset
                        .saturating_add(u64::try_from(started.saturating_sub(anchor)).unwrap_or(0))
                }
                _ => cursor_ms,
            };
            cursor_ms = cursor_ms.max(start_ms.saturating_add(dur));
            let span =
                project_tool_span(turn_number, idx, card, start_ms, dur, &mut subagent_counter);
            raw_spans.push(span);
        }
    }

    if raw_spans.is_empty() && run_totals.is_none() {
        return None;
    }

    let (spans, parallel_batches) = group_parallel_batches(turn_number, raw_spans);
    let mut final_spans = spans;
    let max_span_end = final_spans
        .iter()
        .map(TimelineSpan::end_ms)
        .max()
        .unwrap_or(0);

    if let Some(totals) = run_totals {
        let total_dur = totals.duration_ms.unwrap_or(max_span_end);
        let reply_dur = total_dur.saturating_sub(max_span_end);
        if reply_dur > 0 || final_spans.is_empty() {
            final_spans.push(TimelineSpan {
                key: format!("t{turn_number}-reply"),
                label: "Final reply".to_owned(),
                kind: SpanKind::FinalReply {
                    outcome: sanitize_single_line(&totals.outcome),
                    output_tokens: totals.output_tokens,
                },
                start_ms: max_span_end,
                duration_ms: reply_dur,
                started_at_ms: None,
                status: match totals.outcome.as_str() {
                    "completed" => SpanStatus::Done,
                    "cancelled" => SpanStatus::Cancelled,
                    _ => SpanStatus::Error,
                },
                children: Vec::new(),
            });
        }
    }

    let total_ms = run_totals
        .and_then(|t| t.duration_ms)
        .into_iter()
        .chain(final_spans.iter().map(TimelineSpan::end_ms))
        .max()
        .unwrap_or(0)
        .max(1);

    Some(TurnTimeline {
        turn_number,
        prompt,
        total_ms,
        parallel_batches,
        tool_calls,
        subagents: subagent_counter,
        spans: final_spans,
    })
}

fn project_tool_span(
    turn_number: usize,
    entry_idx: usize,
    card: &ToolCardSnapshot,
    start_ms: u64,
    duration_ms: u64,
    subagent_counter: &mut usize,
) -> TimelineSpan {
    let status = match card.status {
        ToolStatus::Requested | ToolStatus::AwaitingApproval | ToolStatus::Running => {
            SpanStatus::Running
        }
        ToolStatus::Done => SpanStatus::Done,
        ToolStatus::Error | ToolStatus::Denied => SpanStatus::Error,
        ToolStatus::Cancelled => SpanStatus::Cancelled,
    };

    if let Some(script) = card.script.as_deref() {
        if script.host == NestedHost::Subagent {
            *subagent_counter += 1;
            let num = *subagent_counter;
            let subject = sanitize_single_line(&script.subject());
            let task = sanitize_single_line(&script.code);
            let parent_key = format!("t{turn_number}-sub-{entry_idx}-{}", card.call_id);
            let children = project_nested_calls(&parent_key, &script.calls, start_ms, duration_ms);
            return TimelineSpan {
                key: parent_key,
                label: format!("Subagent #{num}"),
                kind: SpanKind::Subagent {
                    call_id: card.call_id.clone(),
                    subagent_number: num,
                    subject,
                    task,
                    omitted: script.omitted,
                },
                start_ms,
                duration_ms,
                started_at_ms: card.started_at_ms,
                status,
                children,
            };
        }

        let title = sanitize_single_line(
            script
                .title
                .as_deref()
                .filter(|t| !t.trim().is_empty())
                .unwrap_or("codemode"),
        );
        let parent_key = format!("t{turn_number}-script-{entry_idx}-{}", card.call_id);
        let children = project_nested_calls(&parent_key, &script.calls, start_ms, duration_ms);
        return TimelineSpan {
            key: parent_key,
            label: format!("codemode: {title}"),
            kind: SpanKind::Script {
                call_id: card.call_id.clone(),
                title,
                omitted: script.omitted,
            },
            start_ms,
            duration_ms,
            started_at_ms: card.started_at_ms,
            status,
            children,
        };
    }

    let tool_name = sanitize_single_line(&card.name);
    let detail = sanitize_single_line(&card.detail);
    TimelineSpan {
        key: format!("t{turn_number}-tool-{entry_idx}-{}", card.call_id),
        label: tool_name.clone(),
        kind: SpanKind::Tool {
            call_id: card.call_id.clone(),
            tool_name,
            detail,
        },
        start_ms,
        duration_ms,
        started_at_ms: card.started_at_ms,
        status,
        children: Vec::new(),
    }
}

fn project_nested_calls(
    parent_key: &str,
    calls: &[ScriptCall],
    parent_start_ms: u64,
    parent_duration_ms: u64,
) -> Vec<TimelineSpan> {
    if calls.is_empty() {
        return Vec::new();
    }
    let known_sum: u64 = calls
        .iter()
        .filter_map(|call| call.duration_ms.map(u64::from))
        .sum();
    let unknown_count = calls.iter().filter(|c| c.duration_ms.is_none()).count() as u64;
    let remaining = parent_duration_ms.saturating_sub(known_sum);
    let fallback_each = if unknown_count > 0 {
        remaining / unknown_count
    } else {
        0
    };
    let scale_down = parent_duration_ms > 0 && known_sum > parent_duration_ms;

    let mut cur = parent_start_ms;
    let parent_end = parent_start_ms.saturating_add(parent_duration_ms);
    let mut children = Vec::with_capacity(calls.len());

    for (idx, call) in calls.iter().enumerate() {
        let raw = call.duration_ms.map(u64::from).unwrap_or(fallback_each);
        let dur = if scale_down {
            (raw.saturating_mul(parent_duration_ms)) / known_sum.max(1)
        } else {
            raw
        };
        let status = match call.state {
            ScriptCallState::Running => SpanStatus::Running,
            ScriptCallState::Done => SpanStatus::Done,
            ScriptCallState::Failed | ScriptCallState::Denied => SpanStatus::Error,
            ScriptCallState::Unfinished => SpanStatus::Cancelled,
        };
        let label = sanitize_single_line(&call.tool);
        let span_start = cur.min(parent_end);
        let span_dur = if parent_duration_ms > 0 {
            dur.min(parent_end.saturating_sub(span_start))
        } else {
            dur
        };
        cur = span_start.saturating_add(span_dur);
        children.push(TimelineSpan {
            key: format!("{parent_key}-c{idx}-{}", call.call_id),
            label: label.clone(),
            kind: SpanKind::NestedCall {
                parent_key: parent_key.to_owned(),
                tool: label,
                error: call.error.as_deref().map(sanitize_single_line),
            },
            start_ms: span_start,
            duration_ms: span_dur,
            started_at_ms: None,
            status,
            children: Vec::new(),
        });
    }
    children
}

fn group_parallel_batches(
    turn_number: usize,
    raw_spans: Vec<TimelineSpan>,
) -> (Vec<TimelineSpan>, usize) {
    let mut out = Vec::new();
    let mut tool_run: Vec<TimelineSpan> = Vec::new();
    let mut batch_number = 0usize;

    let flush_run =
        |run: &mut Vec<TimelineSpan>, out: &mut Vec<TimelineSpan>, batch_number: &mut usize| {
            if run.len() >= 2 {
                *batch_number += 1;
                let num = *batch_number;
                let min_start = run.iter().map(|s| s.start_ms).min().unwrap_or(0);
                let max_end = run
                    .iter()
                    .map(TimelineSpan::end_ms)
                    .max()
                    .unwrap_or(min_start);
                let status = if run.iter().any(|s| s.status == SpanStatus::Running) {
                    SpanStatus::Running
                } else if run.iter().any(|s| s.status == SpanStatus::Error) {
                    SpanStatus::Error
                } else if run.iter().all(|s| s.status == SpanStatus::Cancelled) {
                    SpanStatus::Cancelled
                } else {
                    SpanStatus::Done
                };
                let count = run.len();
                let children = std::mem::take(run);
                out.push(TimelineSpan {
                    key: format!("t{turn_number}-batch-{num}"),
                    label: format!("Batch {num} (×{count})"),
                    kind: SpanKind::ParallelBatch {
                        batch_number: num,
                        count,
                    },
                    start_ms: min_start,
                    duration_ms: max_end.saturating_sub(min_start),
                    started_at_ms: None,
                    status,
                    children,
                });
            } else {
                out.append(run);
            }
        };

    for span in raw_spans {
        let is_plain_tool = matches!(span.kind, SpanKind::Tool { .. });
        if !is_plain_tool {
            flush_run(&mut tool_run, &mut out, &mut batch_number);
            out.push(span);
            continue;
        }
        if let Some(first) = tool_run.first() {
            let group_end = tool_run.iter().map(TimelineSpan::end_ms).max().unwrap_or(0);
            let same_start =
                first.started_at_ms.is_some() && span.started_at_ms == first.started_at_ms;
            let overlaps = span.start_ms < group_end || same_start;
            if overlaps {
                tool_run.push(span);
            } else {
                flush_run(&mut tool_run, &mut out, &mut batch_number);
                tool_run.push(span);
            }
        } else {
            tool_run.push(span);
        }
    }
    flush_run(&mut tool_run, &mut out, &mut batch_number);
    (out, batch_number)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::script_card::ScriptCallState;
    use crate::tool_cards::{ToolCallInput, ToolResultInput};
    use crate::transcript::RunTotals;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use serde_json::json;

    fn add_tool(
        state: &mut UiState,
        call_id: &str,
        name: &str,
        args: serde_json::Value,
        started_at_ms: Option<i64>,
        duration_ms: Option<u64>,
        nested: Vec<ScriptCall>,
    ) {
        state.transcript.observe_tool_call(ToolCallInput {
            call_id: call_id.into(),
            name: name.into(),
            arguments: args,
            detail_source: crate::tool_detail::ToolDetailSource::None,
        });
        state.transcript.observe_tool_result(ToolResultInput {
            nested: crate::script_card::ScriptResult {
                calls: nested,
                omitted: 0,
            },
            call_id: call_id.into(),
            name: name.into(),
            output_source_bytes: 2,
            output_source_lines: 1,
            output: "ok".into(),
            output_tail: None,
            output_projection_cut_mid_line: false,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: Some(0),
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
            duration_ms,
            started_at_ms,
            process_started_at_ms: None,
            process_ended_at_ms: None,
            edit_locations: Vec::new(),
            images: Vec::new(),
            image_parts: Vec::new(),
        });
    }

    fn buffer_text(buf: &ratatui::buffer::Buffer) -> String {
        let mut out = String::new();
        for y in 0..buf.area.height {
            for x in 0..buf.area.width {
                out.push_str(buf[(x, y)].symbol());
            }
            out.push('\n');
        }
        out
    }

    #[test]
    fn groups_overlapping_tools_and_keeps_sequential_tools_flat() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_prompt("inspect workspace".into());
        // Parallel batch: three overlapping tools starting at t=1000..1200 before batch ends at 3400.
        add_tool(
            &mut state,
            "c1",
            "read",
            json!({"path": "a.rs"}),
            Some(1_000),
            Some(800),
            Vec::new(),
        );
        add_tool(
            &mut state,
            "c2",
            "rg",
            json!({"pattern": "foo"}),
            Some(1_050),
            Some(1_500),
            Vec::new(),
        );
        add_tool(
            &mut state,
            "c3",
            "git_diff",
            json!({}),
            Some(1_200),
            Some(2_200),
            Vec::new(),
        );
        // Sequential tool starting after batch ends (t=4000 > 3400).
        add_tool(
            &mut state,
            "c4",
            "edit",
            json!({"path": "lib.rs"}),
            Some(4_000),
            Some(900),
            Vec::new(),
        );

        let turns = collect_turns(state.transcript.entries());
        assert_eq!(turns.len(), 1);
        let turn = &turns[0];
        assert_eq!(turn.parallel_batches, 1);
        assert_eq!(turn.tool_calls, 4);
        assert_eq!(turn.spans.len(), 2);
        assert!(matches!(
            turn.spans[0].kind,
            SpanKind::ParallelBatch { count: 3, .. }
        ));
        assert_eq!(turn.spans[0].duration_ms, 2_400);
        assert!(matches!(turn.spans[1].kind, SpanKind::Tool { .. }));
    }

    #[test]
    fn subagent_children_fold_and_navigate_wide_and_narrow() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_prompt("run review\x1b[31m!".into());
        add_tool(
            &mut state,
            "sub-1",
            "spawn_agent",
            json!({"task": "Check\x1b[2J files\nline2"}),
            Some(2_000),
            Some(5_000),
            vec![
                ScriptCall {
                    call_id: "sc-1".into(),
                    tool: "read\x1b[31m".into(),
                    arguments: json!({}),
                    state: ScriptCallState::Done,
                    failure_code: None,
                    auto_approved: true,
                    approval_note: None,
                    error: None,
                    duration_ms: Some(1_200),
                },
                ScriptCall {
                    call_id: "sc-2".into(),
                    tool: "cargo_test".into(),
                    arguments: json!({}),
                    state: ScriptCallState::Done,
                    failure_code: None,
                    auto_approved: true,
                    approval_note: None,
                    error: None,
                    duration_ms: Some(3_800),
                },
            ],
        );
        let _ = state.transcript.start_message(1);
        let _ = state.transcript.complete_message(1, "done".into());
        state
            .transcript
            .attach_run_summary(RunTotals::finished(7_200, 1));

        let mut panel = SidePanel::default();
        panel.show(PanelMode::LiveWork);
        panel.toggle_expanded();

        // Wide render shows waterfall, axis, subagent, children, and sanitized task.
        let mut wide_term = Terminal::new(TestBackend::new(100, 22)).unwrap();
        wide_term
            .draw(|frame| {
                render(
                    frame,
                    frame.area(),
                    frame.area(),
                    &state,
                    &panel,
                    Palette::default(),
                );
            })
            .unwrap();
        let wide_text = buffer_text(wide_term.backend().buffer());
        assert!(wide_text.contains("TURN #1 TIMELINE"), "{wide_text}");
        assert!(wide_text.contains("Subagent #1"), "{wide_text}");
        assert!(wide_text.contains("cargo_test"), "{wide_text}");
        assert!(!wide_text.contains('\x1b'), "must strip terminal escapes");

        // Down twice lands on nested call `sc-1`; Left jumps to `Subagent #1`; Left collapses it.
        assert_eq!(
            panel
                .timeline_view
                .borrow_mut()
                .handle_key(KeyCode::Down, &state),
            OverlayKey::Moved
        );
        assert_eq!(
            panel
                .timeline_view
                .borrow_mut()
                .handle_key(KeyCode::Left, &state),
            OverlayKey::Moved
        );
        assert_eq!(
            panel
                .timeline_view
                .borrow()
                .selected_row()
                .and_then(|r| r.span.target()),
            Some(Target::Subagent("sub-1".into()))
        );
        assert_eq!(
            panel
                .timeline_view
                .borrow_mut()
                .handle_key(KeyCode::Left, &state),
            OverlayKey::Moved
        );
        assert_eq!(
            panel
                .timeline_view
                .borrow_mut()
                .handle_key(KeyCode::Enter, &state),
            OverlayKey::Open(Target::Subagent("sub-1".into()))
        );

        // Narrow fallback renders compact duration bars without axis overflow.
        let mut narrow_term = Terminal::new(TestBackend::new(48, 16)).unwrap();
        narrow_term
            .draw(|frame| {
                render(
                    frame,
                    frame.area(),
                    frame.area(),
                    &state,
                    &panel,
                    Palette::default(),
                );
            })
            .unwrap();
        let narrow_text = buffer_text(narrow_term.backend().buffer());
        assert!(narrow_text.contains("TURN #1 TIMELINE"), "{narrow_text}");
        assert!(narrow_text.contains("Subagent #1"), "{narrow_text}");
    }

    #[test]
    fn turn_switching_and_scroll_persist_across_repaints() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_prompt("first turn".into());
        add_tool(
            &mut state,
            "t1-a",
            "read",
            json!({}),
            Some(100),
            Some(300),
            Vec::new(),
        );
        state.transcript.append_prompt("second turn".into());
        for i in 0..12 {
            let start = i * 500;
            add_tool(
                &mut state,
                &format!("t2-{i}"),
                &format!("step_{i}"),
                json!({}),
                Some(start),
                Some(200),
                Vec::new(),
            );
        }

        let mut panel = SidePanel::default();
        panel.show(PanelMode::LiveWork);
        panel.toggle_expanded();

        let mut term = Terminal::new(TestBackend::new(88, 14)).unwrap();
        let draw_once = |term: &mut Terminal<TestBackend>| {
            term.draw(|frame| {
                render(
                    frame,
                    frame.area(),
                    frame.area(),
                    &state,
                    &panel,
                    Palette::default(),
                );
            })
            .unwrap();
        };

        draw_once(&mut term);
        assert!(buffer_text(term.backend().buffer()).contains("TURN #2 TIMELINE"));

        // Scroll stays put across repaints.
        panel.timeline_view.borrow().scroll_by(4);
        assert_eq!(panel.timeline_view.borrow().scroll.get(), 4);
        draw_once(&mut term);
        assert_eq!(panel.timeline_view.borrow().scroll.get(), 4);

        // `<` steps back to Turn #1 and `>` steps forward to Turn #2.
        assert_eq!(
            panel
                .timeline_view
                .borrow_mut()
                .handle_key(KeyCode::Char('<'), &state),
            OverlayKey::Moved
        );
        draw_once(&mut term);
        assert!(buffer_text(term.backend().buffer()).contains("TURN #1 TIMELINE"));
        assert_eq!(
            panel
                .timeline_view
                .borrow_mut()
                .handle_key(KeyCode::Char('>'), &state),
            OverlayKey::Moved
        );
        draw_once(&mut term);
        assert!(buffer_text(term.backend().buffer()).contains("TURN #2 TIMELINE"));
    }

    #[test]
    fn skipped_empty_prompt_keeps_sequential_turn_numbers() {
        let mut state = UiState::new("fake".into(), None, None);
        // An interrupted/empty prompt before any tools or RunTotals should be skipped cleanly.
        state.transcript.append_prompt("aborted prompt".into());
        state.transcript.append_prompt("first real turn".into());
        add_tool(
            &mut state,
            "real-1",
            "read",
            json!({}),
            Some(500),
            Some(400),
            Vec::new(),
        );
        let _ = state.transcript.start_message(1);
        let _ = state.transcript.complete_message(1, "done".into());
        state
            .transcript
            .attach_run_summary(RunTotals::finished(900, 1));

        state.transcript.append_prompt("second real turn".into());
        add_tool(
            &mut state,
            "real-2",
            "edit",
            json!({}),
            Some(1_000),
            Some(300),
            Vec::new(),
        );

        let turns = collect_turns(state.transcript.entries());
        assert_eq!(turns.len(), 2);
        assert_eq!(turns[0].turn_number, 1);
        assert_eq!(turns[1].turn_number, 2);

        let mut panel = SidePanel::default();
        panel.show(PanelMode::LiveWork);
        panel.toggle_expanded();
        let mut term = Terminal::new(TestBackend::new(88, 16)).unwrap();
        term.draw(|frame| {
            render(
                frame,
                frame.area(),
                frame.area(),
                &state,
                &panel,
                Palette::default(),
            );
        })
        .unwrap();
        let text = buffer_text(term.backend().buffer());
        assert!(text.contains("< Turn 2/2 >"), "{text}");
    }

    #[test]
    fn scrolled_session_clears_scroll_for_timeline_controls_and_focus_background_resets_timeline() {
        let mut state = UiState::new("fake".into(), None, None);
        state.transcript.append_prompt("work".into());
        add_tool(
            &mut state,
            "c1",
            "bash",
            json!({"command": "echo hi"}),
            Some(100),
            Some(200),
            Vec::new(),
        );

        let mut panel = SidePanel::default();
        panel.observe_width(150);
        panel.show(PanelMode::LiveWork);
        panel.scroll.set(5);

        assert_eq!(panel.activate(PanelAction::OpenTimeline), OverlayKey::Moved);
        assert!(panel.timeline_active());
        assert_eq!(panel.scroll.get(), 0);

        let mut term = Terminal::new(TestBackend::new(50, 18)).unwrap();
        term.draw(|frame| {
            render(
                frame,
                frame.area(),
                frame.area(),
                &state,
                &panel,
                Palette::default(),
            );
        })
        .unwrap();
        let text = buffer_text(term.backend().buffer());
        let expand_col = text
            .lines()
            .next()
            .unwrap()
            .find("[Expand]")
            .expect("[Expand] control") as u16;
        assert_eq!(panel.click(expand_col, 0), OverlayKey::Moved);
        assert!(panel.expanded());

        // Switching to background processes leaves inline timeline mode.
        panel.focus_background();
        assert!(!panel.timeline_active());
        assert!(!panel.expanded());

        // Switching tabs via `show` clears `timeline_open`.
        panel.key(KeyCode::Char('t'), &state);
        assert!(panel.timeline_active());
        panel.show(PanelMode::Files);
        panel.show(PanelMode::LiveWork);
        assert!(!panel.timeline_active());
    }
}
