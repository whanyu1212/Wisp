//! The full-screen details of a codemode script card: the whole script as the card
//! keeps it, every call the script made with its arguments, and its complete output.
//!
//! The view rebuilds its lines whenever the card changes, so it follows a script while
//! it runs, and wraps only the rows it paints. Everything shown is untrusted backend
//! text, already bounded by the card; each grapheme is sanitized as it is wrapped.

use ratatui::{
    Frame,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, Paragraph},
};
use serde_json::Value;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::reducer::UiState;
use crate::script_card::{SCRIPT_MAX_BYTES, ScriptCallState, ScriptCard};
use crate::syntax::{SyntaxClass, SyntaxToken};
use crate::theme::Palette;
use crate::tool_cards::{BoundedText, ToolCardSnapshot, ToolStatus};
use crate::transcript::TranscriptEntryId;
use crate::transcript_view::sanitize_grapheme;

/// Script lines highlighted together when the whole script is past the highlighter's
/// limits; a block it still declines is shown plain.
const HIGHLIGHT_BLOCK_LINES: usize = 48;

/// The script card `entry_id` names, if it is one.
pub(crate) fn script_card(
    state: &UiState,
    entry_id: TranscriptEntryId,
) -> Option<&ToolCardSnapshot> {
    let card = state.transcript.entry(entry_id)?.tool_card()?;
    card.script.is_some().then_some(card)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Tone {
    Plain,
    Muted,
    Heading,
    Warning,
    Error,
    Syntax(SyntaxClass),
}

/// One logical line of the details, before it is wrapped.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct DetailLine {
    segments: Vec<(Tone, String)>,
    /// Columns a wrapped continuation row is indented by, so it hangs under the text.
    hang: usize,
}

impl DetailLine {
    fn new(tone: Tone, text: impl Into<String>) -> Self {
        Self {
            segments: vec![(tone, text.into())],
            hang: 0,
        }
    }

    fn hanging(mut self, hang: usize) -> Self {
        self.hang = hang;
        self
    }
}

/// What the lines were built from, kept to rebuild them when the card changes.
#[derive(Clone, Debug, Eq, PartialEq)]
struct Source {
    status: ToolStatus,
    script: ScriptCard,
    output: BoundedText,
    detail: String,
    display_base: Option<std::sync::Arc<str>>,
}

impl Source {
    /// Whether `card` is still what these lines show, compared without copying it.
    fn matches(&self, card: &ToolCardSnapshot, script: &ScriptCard) -> bool {
        self.status == card.status
            && self.script == *script
            && self.output == card.retained_output
            && self.detail == card.detail
            && self.display_base == card.display_base
    }

    fn of(card: &ToolCardSnapshot, script: &ScriptCard) -> Self {
        Self {
            status: card.status,
            script: script.clone(),
            output: card.retained_output.clone(),
            detail: card.detail.clone(),
            display_base: card.display_base.clone(),
        }
    }
}

/// The top painted row: a logical line and which of its wrapped rows.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct Anchor {
    line: usize,
    row: usize,
}

/// Scroll state and built lines for one open script's details.
#[derive(Debug)]
pub struct ScriptDetailView {
    source: Option<Source>,
    /// The script's own lines, kept so a new call does not highlight it again.
    script_lines: Vec<DetailLine>,
    lines: Vec<DetailLine>,
    /// How many rows each line wraps to at `width`, filled in as lines are reached.
    row_counts: Vec<Option<usize>>,
    top: Anchor,
    width: usize,
    height: usize,
}

impl Default for ScriptDetailView {
    fn default() -> Self {
        Self {
            source: None,
            script_lines: Vec::new(),
            lines: Vec::new(),
            row_counts: Vec::new(),
            top: Anchor::default(),
            width: 1,
            height: 1,
        }
    }
}

impl ScriptDetailView {
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    /// Rebuild the lines if the card changed, then fit the view to `width` × `height`.
    /// The top line stays put; a card whose calls shrank, as when its result replaces
    /// the live rows, pulls it back into range.
    fn sync(&mut self, card: &ToolCardSnapshot, script: &ScriptCard, width: usize, height: usize) {
        if !self
            .source
            .as_ref()
            .is_some_and(|source| source.matches(card, script))
        {
            if self
                .source
                .as_ref()
                .is_none_or(|source| source.script.code != script.code)
            {
                self.script_lines = script_lines(&script.code);
            }
            self.lines = build_lines(&self.script_lines, card, script);
            self.row_counts = vec![None; self.lines.len()];
            self.source = Some(Source::of(card, script));
        }
        let width = width.max(1);
        if width != self.width {
            self.row_counts.fill(None);
        }
        self.width = width;
        self.height = height.max(1);
        self.clamp_top();
    }

    /// Pull the top row back into range after the lines or the width changed.
    fn clamp_top(&mut self) {
        let Some(last) = self.lines.len().checked_sub(1) else {
            self.top = Anchor::default();
            return;
        };
        self.top.line = self.top.line.min(last);
        self.top.row = self.top.row.min(self.rows_of(self.top.line) - 1);
    }

    pub fn scroll_lines(&mut self, lines: i32) {
        for _ in 0..lines.unsigned_abs() {
            let moved = if lines > 0 {
                self.step_down()
            } else {
                self.step_up()
            };
            if !moved {
                return;
            }
        }
    }

    pub fn page_up(&mut self) {
        self.scroll_lines(-self.page());
    }

    pub fn page_down(&mut self) {
        self.scroll_lines(self.page());
    }

    pub fn home(&mut self) {
        self.top = Anchor::default();
    }

    /// Bottom-align the last row.
    pub fn end(&mut self) {
        let Some(last) = self.lines.len().checked_sub(1) else {
            return;
        };
        self.top = Anchor {
            line: last,
            row: self.rows_of(last) - 1,
        };
        for _ in 1..self.height {
            if !self.step_up() {
                return;
            }
        }
    }

    fn page(&self) -> i32 {
        i32::try_from(self.height.saturating_sub(1).max(1)).unwrap_or(i32::MAX)
    }

    fn step_down(&mut self) -> bool {
        if self.lines.is_empty() {
            return false;
        }
        if self.top.row + 1 < self.rows_of(self.top.line) {
            self.top.row += 1;
        } else if self.top.line + 1 < self.lines.len() {
            self.top = Anchor {
                line: self.top.line + 1,
                row: 0,
            };
        } else {
            return false;
        }
        true
    }

    fn step_up(&mut self) -> bool {
        if self.top.row > 0 {
            self.top.row -= 1;
        } else if self.top.line > 0 {
            self.top.line -= 1;
            self.top.row = self.rows_of(self.top.line) - 1;
        } else {
            return false;
        }
        true
    }

    fn rows_of(&mut self, line: usize) -> usize {
        if let Some(rows) = self.row_counts[line] {
            return rows;
        }
        let rows = wrap(&self.lines[line], self.width).len();
        self.row_counts[line] = Some(rows);
        rows
    }

    /// The rows that fill the view from its top.
    fn visible_rows(&self) -> Vec<Vec<(Tone, String)>> {
        let mut rows = Vec::with_capacity(self.height);
        for (index, line) in self.lines.iter().enumerate().skip(self.top.line) {
            let skip = if index == self.top.line {
                self.top.row
            } else {
                0
            };
            rows.extend(wrap(line, self.width).into_iter().skip(skip));
            if rows.len() >= self.height {
                rows.truncate(self.height);
                break;
            }
        }
        rows
    }
}

/// Paint `card`'s details over `area`, as other cards' details are painted.
pub(crate) fn render(
    frame: &mut Frame<'_>,
    area: Rect,
    view: &mut ScriptDetailView,
    card: &ToolCardSnapshot,
    palette: Palette,
) {
    let Some(script) = card.script.as_deref() else {
        return;
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(2),
            Constraint::Length(1),
        ])
        .split(area);
    frame.render_widget(
        Paragraph::new(crate::ui::sanitize_for_terminal(&heading(card, script))).block(
            Block::default()
                .title(" script details ")
                .border_style(palette.border())
                .borders(Borders::ALL),
        ),
        chunks[0],
    );
    let width = usize::from(chunks[1].width.saturating_sub(2)).max(1);
    let height = usize::from(chunks[1].height.saturating_sub(2)).max(1);
    view.sync(card, script, width, height);
    let lines: Vec<Line<'static>> = view
        .visible_rows()
        .into_iter()
        .map(|row| {
            Line::from(
                row.into_iter()
                    .map(|(tone, text)| Span::styled(text, tone_style(tone, palette)))
                    .collect::<Vec<_>>(),
            )
        })
        .collect();
    frame.render_widget(
        Paragraph::new(Text::from(lines)).block(
            Block::default()
                .title(" detail ")
                .borders(Borders::ALL)
                .border_style(palette.border()),
        ),
        chunks[1],
    );
    frame.render_widget(
        Paragraph::new("↑/↓ scroll · PgUp/PgDn · Home/End · Esc close")
            .style(Style::default().fg(palette.muted)),
        chunks[2],
    );
}

/// The card's own line: `• Scripted count TODOs  4 calls (1 failed) · read ×3 · 1.2s`.
fn heading(card: &ToolCardSnapshot, script: &ScriptCard) -> String {
    let line = format!(
        "{}{} {}",
        crate::transcript_view::status_marker(card.status),
        card.action_verb(),
        script.subject()
    );
    let notes: Vec<String> = [
        Some(script.tally()).filter(|tally| !tally.is_empty()),
        card.status_note().map(str::to_owned),
        card.duration_label(),
    ]
    .into_iter()
    .flatten()
    .collect();
    if notes.is_empty() {
        line
    } else {
        format!("{line}  {}", notes.join(" · "))
    }
}

fn tone_style(tone: Tone, palette: Palette) -> Style {
    let base = Style::default();
    match tone {
        Tone::Plain => base,
        Tone::Muted => base.fg(palette.muted),
        Tone::Heading => base.fg(palette.primary).add_modifier(Modifier::BOLD),
        Tone::Warning => base.fg(palette.warning),
        Tone::Error => base.fg(palette.error),
        Tone::Syntax(class) => {
            let style = palette.syntax(base, class);
            match class {
                SyntaxClass::Comment => style.add_modifier(Modifier::ITALIC),
                SyntaxClass::Keyword => style.add_modifier(Modifier::BOLD),
                _ => style,
            }
        }
    }
}

fn build_lines(
    script_lines: &[DetailLine],
    card: &ToolCardSnapshot,
    script: &ScriptCard,
) -> Vec<DetailLine> {
    let mut lines = script_lines.to_vec();
    lines.push(DetailLine::default());
    push_calls(&mut lines, card, script);
    lines.push(DetailLine::default());
    push_output(&mut lines, card);
    lines
}

/// The script, numbered and highlighted as Python.
fn script_lines(code: &str) -> Vec<DetailLine> {
    let mut lines = Vec::new();
    let code_lines: Vec<&str> = code.lines().collect();
    lines.push(DetailLine::new(
        Tone::Heading,
        match code_lines.len() {
            0 => "Script".to_owned(),
            1 => "Script · 1 line".to_owned(),
            count => format!("Script · {count} lines"),
        },
    ));
    if code_lines.is_empty() {
        lines.push(DetailLine::new(
            Tone::Muted,
            "This record does not include the script.",
        ));
        return lines;
    }
    let width = code_lines.len().to_string().len();
    for (index, (line, tokens)) in code_lines
        .iter()
        .zip(highlight_lines(&code_lines))
        .enumerate()
    {
        let mut segments = vec![(Tone::Muted, format!("{:>width$} ", index + 1))];
        let mut offset = 0;
        for token in tokens {
            if token.range.start > offset {
                segments.push((Tone::Plain, line[offset..token.range.start].to_owned()));
            }
            segments.push((
                Tone::Syntax(token.class),
                line[token.range.clone()].to_owned(),
            ));
            offset = token.range.end;
        }
        if offset < line.len() {
            segments.push((Tone::Plain, line[offset..].to_owned()));
        }
        lines.push(DetailLine {
            segments,
            hang: width + 1,
        });
    }
    // The card cuts a longer script on a character boundary at most 3 bytes short of
    // the limit, but does not record that it did, so a script this long may be whole.
    if code.len() + 3 >= SCRIPT_MAX_BYTES {
        lines.push(DetailLine::new(
            Tone::Warning,
            format!(
                "A card keeps up to {} KiB of a script; any more is not shown.",
                SCRIPT_MAX_BYTES / 1024
            ),
        ));
    }
    lines
}

/// Each line's syntax tokens, with ranges relative to that line. The whole script is
/// highlighted at once when the highlighter accepts it, otherwise in blocks of lines;
/// a string that spans two blocks is then coloured as if each block stood alone.
fn highlight_lines(lines: &[&str]) -> Vec<Vec<SyntaxToken>> {
    if let Some(tokens) = highlight_block(lines) {
        return tokens;
    }
    lines
        .chunks(HIGHLIGHT_BLOCK_LINES)
        .flat_map(|block| highlight_block(block).unwrap_or_else(|| vec![Vec::new(); block.len()]))
        .collect()
}

fn highlight_block(lines: &[&str]) -> Option<Vec<Vec<SyntaxToken>>> {
    let tokens = crate::syntax::highlight_fence("python", &lines.join("\n"))
        .ok()?
        .tokens;
    // The highlighter saw the lines joined by `\n`; move each token onto its line.
    let mut per_line = Vec::with_capacity(lines.len());
    let mut start = 0;
    for line in lines {
        let end = start + line.len();
        per_line.push(
            tokens
                .iter()
                .filter_map(|token| {
                    let from = token.range.start.max(start);
                    let to = token.range.end.min(end);
                    (from < to).then(|| SyntaxToken {
                        range: from - start..to - start,
                        class: token.class,
                    })
                })
                .collect(),
        );
        start = end + 1;
    }
    Some(per_line)
}

/// Every recorded call, numbered: its action line, arguments, and why it failed.
fn push_calls(lines: &mut Vec<DetailLine>, card: &ToolCardSnapshot, script: &ScriptCard) {
    let failed = script
        .calls
        .iter()
        .filter(|call| call.state == ScriptCallState::Failed)
        .count();
    let total = u64::try_from(script.calls.len())
        .unwrap_or(u64::MAX)
        .saturating_add(script.omitted);
    lines.push(DetailLine::new(
        Tone::Heading,
        match (total, failed) {
            (0, _) => "Calls".to_owned(),
            (total, 0) => format!("Calls · {total}"),
            (total, failed) => format!("Calls · {total}, {failed} failed"),
        },
    ));
    if total == 0 {
        let none = if card.status.terminal() {
            "No calls."
        } else {
            "No calls yet."
        };
        lines.push(DetailLine::new(Tone::Muted, none));
        return;
    }
    let width = script.calls.len().to_string().len();
    let indent = " ".repeat(width + 3);
    for (index, call) in script.calls.iter().enumerate() {
        let mut action = crate::transcript_view::script_call_action(card, call);
        if call.state == ScriptCallState::Unfinished {
            action.push_str(" · script ended first");
        }
        if let Some(duration) = call.duration_ms {
            action.push(' ');
            action.push_str(&crate::tool_cards::format_duration(u64::from(duration)));
        }
        let tone = match call.state {
            ScriptCallState::Failed => Tone::Error,
            ScriptCallState::Unfinished => Tone::Muted,
            ScriptCallState::Running | ScriptCallState::Done => Tone::Plain,
        };
        lines.push(DetailLine {
            segments: vec![
                (Tone::Muted, format!("{:>width$} ", index + 1)),
                (tone, action),
            ],
            hang: width + 3,
        });
        for (tone, text) in argument_lines(&call.arguments) {
            lines.push(DetailLine::new(tone, format!("{indent}{text}")).hanging(indent.len() + 2));
        }
        if let Some(error) = call.error.as_deref() {
            lines.push(
                DetailLine::new(Tone::Error, format!("{indent}error: {error}"))
                    .hanging(indent.len() + 2),
            );
        }
    }
    if script.omitted > 0 {
        let noun = if script.omitted == 1 { "call" } else { "calls" };
        lines.push(DetailLine::new(
            Tone::Muted,
            format!("… {} more {noun} the record leaves out", script.omitted),
        ));
    }
}

/// A call's bounded arguments, one `key: value` line each, values as compact JSON.
fn argument_lines(arguments: &Value) -> Vec<(Tone, String)> {
    let Value::Object(object) = arguments else {
        return vec![if arguments.is_null() {
            (Tone::Muted, "Arguments not kept.".to_owned())
        } else {
            (Tone::Plain, arguments.to_string())
        }];
    };
    let (items, omitted) = crate::tool_cards::generic_argument_items(object).unwrap_or((object, 0));
    if items.is_empty() && omitted == 0 {
        return vec![(Tone::Muted, "No arguments.".to_owned())];
    }
    let mut lines: Vec<(Tone, String)> = items
        .iter()
        .map(|(key, value)| (Tone::Plain, format!("{key}: {value}")))
        .collect();
    if omitted > 0 {
        let noun = if omitted == 1 {
            "argument"
        } else {
            "arguments"
        };
        lines.push((Tone::Muted, format!("… {omitted} more {noun} not kept")));
    }
    lines
}

/// The script's output as the card keeps it: all of it, or a failed script's end and a
/// finished one's start, with a note where the rest was cut.
fn push_output(lines: &mut Vec<DetailLine>, card: &ToolCardSnapshot) {
    let output = &card.retained_output;
    let output_lines: Vec<&str> = output.text.lines().collect();
    lines.push(DetailLine::new(
        Tone::Heading,
        match output_lines.len() {
            0 => "Output".to_owned(),
            1 => "Output · 1 line".to_owned(),
            count => format!("Output · {count} lines"),
        },
    ));
    let tone = if card.status == ToolStatus::Error {
        Tone::Error
    } else {
        Tone::Plain
    };
    if output.text.trim().is_empty() {
        let reason = if !card.detail.trim().is_empty() {
            (tone, card.detail.clone())
        } else if card.status.terminal() {
            (Tone::Muted, "No output.".to_owned())
        } else {
            (Tone::Muted, "No output yet.".to_owned())
        };
        lines.push(DetailLine::new(reason.0, reason.1));
        return;
    }
    // A kept tail starts past the bytes it dropped; a kept head starts at zero.
    let kept_tail = output.base_offset > 0;
    let dropped = dropped_note(output);
    if let Some(dropped) = dropped.as_deref().filter(|_| kept_tail) {
        lines.push(DetailLine::new(
            Tone::Muted,
            format!("… {dropped} earlier not kept"),
        ));
    }
    lines.extend(output_lines.iter().map(|line| DetailLine::new(tone, *line)));
    if let Some(dropped) = dropped.as_deref().filter(|_| !kept_tail) {
        lines.push(DetailLine::new(
            Tone::Muted,
            format!("… {dropped} more not kept"),
        ));
    }
}

fn dropped_note(output: &BoundedText) -> Option<String> {
    let (count, singular, plural) = if output.dropped_lines > 0 {
        (output.dropped_lines, "line", "lines")
    } else if output.dropped_bytes > 0 {
        (output.dropped_bytes, "byte", "bytes")
    } else {
        return None;
    };
    Some(format!(
        "{count} {}",
        if count == 1 { singular } else { plural }
    ))
}

/// Split `line` into rows at most `width` columns wide, sanitizing every grapheme.
/// Continuation rows hang under the line's text when there is room for it.
fn wrap(line: &DetailLine, width: usize) -> Vec<Vec<(Tone, String)>> {
    let hang = if line.hang < width / 2 { line.hang } else { 0 };
    let mut rows = Vec::new();
    let mut row: Vec<(Tone, String)> = Vec::new();
    let mut column = 0usize;
    let mut row_has_text = false;
    for (tone, text) in &line.segments {
        for grapheme in text.graphemes(true) {
            let mut safe = sanitize_grapheme(grapheme, column);
            let mut grapheme_width = UnicodeWidthStr::width(safe.as_str());
            if row_has_text && column.saturating_add(grapheme_width) > width {
                rows.push(std::mem::take(&mut row));
                column = hang;
                if hang > 0 {
                    row.push((Tone::Plain, " ".repeat(hang)));
                }
                safe = sanitize_grapheme(grapheme, column);
                grapheme_width = UnicodeWidthStr::width(safe.as_str());
            }
            match row.last_mut() {
                Some((last, text)) if last == tone => text.push_str(&safe),
                _ => row.push((*tone, safe)),
            }
            column = column.saturating_add(grapheme_width);
            row_has_text = true;
        }
    }
    rows.push(row);
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn text(row: &[(Tone, String)]) -> String {
        row.iter().map(|(_, text)| text.as_str()).collect()
    }

    #[test]
    fn wrap_hangs_continuations_and_sanitizes() {
        let line = DetailLine {
            segments: vec![
                (Tone::Muted, "1 ".into()),
                (Tone::Plain, "abc\u{1b}]52;x\u{7}\u{202e}defgh".into()),
            ],
            hang: 2,
        };

        let rows: Vec<String> = wrap(&line, 6).iter().map(|row| text(row)).collect();

        assert_eq!(rows.len(), 4, "{rows:?}");
        assert!(
            rows[1..].iter().all(|row| row.starts_with("  ")),
            "{rows:?}"
        );
        assert!(
            rows.iter()
                .all(|row| !row.contains(['\u{1b}', '\u{7}', '\u{202e}'])),
            "{rows:?}"
        );
    }

    #[test]
    fn long_script_still_highlights() {
        // Past the highlighter's fragment limit as one fence, so it goes in blocks.
        let code: Vec<String> = (0..300)
            .map(|index| format!("value_{index} = len(\"{index}\") + {index}"))
            .collect();
        let lines: Vec<&str> = code.iter().map(String::as_str).collect();
        assert!(highlight_block(&lines).is_none());

        let tokens = highlight_lines(&lines);

        assert_eq!(tokens.len(), lines.len());
        assert!(
            tokens[299]
                .iter()
                .any(|token| token.class == SyntaxClass::String)
        );
    }

    #[test]
    fn generic_arguments_are_unwrapped() {
        let bounded = crate::tool_cards::bounded_tool_arguments(
            "mcp__db__query",
            &json!({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7, "h": 8, "i": 9}),
        );

        let lines = argument_lines(&bounded);

        assert_eq!(lines[0], (Tone::Plain, "a: 1".to_owned()));
        assert_eq!(lines.last().unwrap().1, "… 1 more argument not kept");
        assert!(lines.iter().all(|(_, line)| !line.contains('\0')));
        assert_eq!(argument_lines(&Value::Null)[0].1, "Arguments not kept.");
    }

    /// A view of `lines` that is `width` × `height`, as `sync` would leave it.
    fn view(lines: Vec<DetailLine>, width: usize, height: usize) -> ScriptDetailView {
        ScriptDetailView {
            row_counts: vec![None; lines.len()],
            lines,
            width,
            height,
            ..ScriptDetailView::default()
        }
    }

    #[test]
    fn view_scrolls_within_bounds() {
        let lines = (0..10)
            .map(|index| DetailLine::new(Tone::Plain, format!("line {index}")))
            .collect();
        let mut view = view(lines, 20, 4);

        view.end();
        assert_eq!(text(&view.visible_rows()[3]), "line 9");
        view.page_down();
        assert_eq!(view.top, Anchor { line: 9, row: 0 });
        view.scroll_lines(-100);
        assert_eq!(view.top, Anchor::default());
    }

    #[test]
    fn view_steps_wrapped_rows() {
        // "aaaabbbbcccc" at width 4 wraps to three rows; "end" is one.
        let lines = vec![
            DetailLine::new(Tone::Plain, "aaaabbbbcccc"),
            DetailLine::new(Tone::Plain, "end"),
        ];
        let mut view = view(lines, 4, 2);

        view.scroll_lines(2);
        assert_eq!(view.top, Anchor { line: 0, row: 2 });
        assert_eq!(
            view.visible_rows()
                .iter()
                .map(|row| text(row))
                .collect::<Vec<_>>(),
            ["cccc", "end"]
        );
        view.scroll_lines(1);
        assert_eq!(view.top, Anchor { line: 1, row: 0 });
        // Stepping back up lands on the last row of the wrapped line before.
        view.scroll_lines(-1);
        assert_eq!(view.top, Anchor { line: 0, row: 2 });
        view.end();
        assert_eq!(view.top, Anchor { line: 0, row: 2 });
    }

    #[test]
    fn shrunk_lines_clamp_top() {
        let mut view = view(
            (0..20)
                .map(|index| DetailLine::new(Tone::Plain, format!("line {index}")))
                .collect(),
            20,
            4,
        );
        view.scroll_lines(15);
        assert_eq!(view.top.line, 15);

        // As when a result's record replaces more live rows than it keeps.
        view.lines.truncate(5);
        view.row_counts.truncate(5);
        view.clamp_top();

        assert_eq!(view.top, Anchor { line: 4, row: 0 });
        assert_eq!(text(&view.visible_rows()[0]), "line 4");
    }
}
