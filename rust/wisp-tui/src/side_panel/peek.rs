//! The peek under a selected background process: its latest output, read again
//! while it runs, its outcome once it ends, and the keys that act on it.

use std::collections::VecDeque;

use super::*;
use crate::reducer::background_processes::{OutputStream, Peek, ProcessReport};
use crate::tool_cards::ProcessDisplayState;

/// Output lines a peek shows, the latest last.
const PEEK_LINES: usize = 6;
/// What leads each peek row, so the output reads as belonging to the row above.
const GUTTER: &str = "  │ ";

/// The peek's rows under its process row; focus lands on none of them.
///
/// `ended` is the outcome the panel lists for a process that is no longer running,
/// and `confirming` asks whether to stop it instead of naming the keys.
pub(super) fn peek_lines(
    peek: Option<&Peek>,
    ended: Option<&ProcessReport>,
    confirming: bool,
    command: &str,
    width: usize,
    palette: Palette,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(palette.muted);
    let room = width.saturating_sub(GUTTER.width());
    let row = |text: String, style: Style| {
        Line::from(vec![
            Span::styled(GUTTER, muted),
            Span::styled(ui::clip_columns(&text, room), style),
        ])
    };
    let mut lines = Vec::new();
    match peek {
        Some(peek) if peek.gone => {
            lines.push(row("No longer held by the backend".into(), muted));
        }
        Some(Peek {
            output: Some(output),
            ..
        }) => {
            let printed = output_lines(&output.chunks, PEEK_LINES);
            if printed.is_empty() {
                lines.push(row("No output yet".into(), muted));
            }
            for (stream, text) in printed {
                let style = match stream {
                    OutputStream::Stdout => Style::default(),
                    OutputStream::Stderr => Style::default().fg(palette.warning),
                };
                lines.push(row(text, style));
            }
        }
        _ => lines.push(row("reading…".into(), muted)),
    }
    let ended = peek
        .and_then(|peek| peek.output.as_ref())
        .map(|output| &output.process)
        .filter(|process| process.state != ProcessDisplayState::Running)
        .or(ended);
    if let Some(ended) = ended {
        lines.push(row(outcome(ended), muted));
    }
    let running = ended.is_none() && !peek.is_some_and(|peek| peek.gone);
    let hint = if confirming && running {
        let mut spans = vec![
            Span::styled(GUTTER, muted),
            Span::styled(
                ui::clip_columns(
                    &format!("Stop {}? ", first_line(command)),
                    room.saturating_sub(16),
                ),
                Style::default().fg(palette.warning),
            ),
        ];
        spans.extend(ui::keycap_hints("y stop · n keep", palette).spans);
        Line::from(spans)
    } else {
        let keys = if running {
            "Enter card · x stop · Esc close"
        } else {
            "Enter card · Esc close"
        };
        let mut spans = vec![Span::styled(GUTTER, muted)];
        spans.extend(ui::keycap_hints(keys, palette).spans);
        Line::from(clip_spans(spans, width))
    };
    lines.push(hint);
    lines
}

/// How a process that is no longer running ended, in words.
pub(super) fn outcome(process: &ProcessReport) -> String {
    match (process.state, process.exit_code) {
        (ProcessDisplayState::Completed, _) => "finished".into(),
        (ProcessDisplayState::TimedOut, _) => "timed out".into(),
        (ProcessDisplayState::Cancelled, _) if process.stopped_by_user => "stopped by you".into(),
        (ProcessDisplayState::Cancelled, _) => "stopped".into(),
        (_, Some(code)) => format!("failed (exit {code})"),
        _ => "failed".into(),
    }
}

/// The last `max` lines of output, made safe to draw.
///
/// Escape sequences such as colours are dropped. A carriage return starts its line
/// again, as a terminal shows a progress bar, so a line keeps what was printed after
/// the last one. A line printed in parts across chunks reads as one, with the stream
/// that finished it. Other control characters are replaced.
pub(super) fn output_lines(
    chunks: &[(OutputStream, String)],
    max: usize,
) -> Vec<(OutputStream, String)> {
    let mut lines: VecDeque<(OutputStream, String)> = VecDeque::new();
    let mut open: Option<(OutputStream, String)> = None;
    for (stream, text) in chunks {
        for piece in without_escapes(text).split_inclusive('\n') {
            let line = open.get_or_insert_with(|| (*stream, String::new()));
            line.0 = *stream;
            line.1.push_str(piece.trim_end_matches('\n'));
            if piece.ends_with('\n') {
                if lines.len() == max {
                    lines.pop_front();
                }
                lines.extend(open.take());
            }
        }
    }
    if let Some(line) = open.filter(|(_, text)| !text.is_empty()) {
        if lines.len() == max {
            lines.pop_front();
        }
        lines.push_back(line);
    }
    lines
        .into_iter()
        .map(|(stream, text)| (stream, sanitize_for_terminal(last_overwrite(&text))))
        .collect()
}

/// The last non-empty line of `text`, stripped of terminal escape sequences and
/// carriage-return overwrites.
pub(super) fn latest_line(text: &str) -> Option<String> {
    without_escapes(text)
        .lines()
        .rev()
        .map(|line| sanitize_for_terminal(last_overwrite(line)))
        .find(|line| !line.trim().is_empty())
}

/// What a terminal leaves of a line rewritten with carriage returns: the last part
/// printed, or the one before it when the line ends in a return.
fn last_overwrite(line: &str) -> &str {
    let line = line.strip_suffix('\r').unwrap_or(line);
    line.rsplit('\r').next().unwrap_or(line)
}

/// `text` without terminal escape sequences: CSI (colours, cursor moves), OSC
/// (titles, links), DCS-style strings, and the shorter escapes.
fn without_escapes(text: &str) -> String {
    let mut kept = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(character) = chars.next() {
        if character != '\u{1b}' {
            kept.push(character);
            continue;
        }
        match chars.next() {
            // CSI: parameters and intermediates up to a final byte.
            Some('[') => {
                for next in chars.by_ref() {
                    if ('\u{40}'..='\u{7e}').contains(&next) {
                        break;
                    }
                }
            }
            // A string up to BEL or ST (ESC \).
            Some(']' | 'P' | 'X' | '^' | '_') => {
                while let Some(next) = chars.next() {
                    if next == '\u{7}' {
                        break;
                    }
                    if next == '\u{1b}' && chars.peek() == Some(&'\\') {
                        chars.next();
                        break;
                    }
                }
            }
            // Intermediates up to a final byte, as `ESC ( B` picks a character set.
            Some(' '..='/') => {
                for next in chars.by_ref() {
                    if ('0'..='~').contains(&next) {
                        break;
                    }
                }
            }
            _ => {}
        }
    }
    kept
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stdout(text: &str) -> (OutputStream, String) {
        (OutputStream::Stdout, text.to_owned())
    }

    fn texts(lines: &[(OutputStream, String)]) -> Vec<&str> {
        lines.iter().map(|(_, text)| text.as_str()).collect()
    }

    #[test]
    fn keeps_latest_lines() {
        let lines = output_lines(&[stdout("one\ntwo\nthree\nfour")], 3);
        assert_eq!(texts(&lines), ["two", "three", "four"]);
    }

    #[test]
    fn joins_lines_across_chunks() {
        let lines = output_lines(
            &[
                stdout("build "),
                (OutputStream::Stderr, "warning\n".into()),
                stdout("done\n"),
            ],
            6,
        );
        assert_eq!(
            lines,
            [
                (OutputStream::Stderr, "build warning".into()),
                stdout("done")
            ]
        );
    }

    #[test]
    fn progress_keeps_last_overwrite() {
        let lines = output_lines(&[stdout("10%\r50%\r100%\r\nnext\r\n")], 6);
        assert_eq!(texts(&lines), ["100%", "next"]);
    }

    #[test]
    fn escapes_are_dropped() {
        let printed = "\u{1b}[1;31mred\u{1b}[0m \u{1b}]0;title\u{7}\u{1b}]8;;https://x\u{1b}\\link\u{1b}]8;;\u{1b}\\\u{1b}(B\n";
        let lines = output_lines(&[stdout(printed)], 6);
        assert_eq!(texts(&lines), ["red link"]);
    }

    #[test]
    fn controls_are_replaced() {
        let lines = output_lines(&[stdout("a\u{7}b\u{202e}c\tz\n")], 6);
        assert_eq!(texts(&lines), ["a�b�c   z"]);
    }
}
