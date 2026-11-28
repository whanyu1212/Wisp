//! The peek under a selected background process: its latest output, read again
//! while it runs, its outcome once it ends, and the keys that act on it.

use super::terminal_output::{RenderedOutput, terminal_lines};
use super::*;
use crate::reducer::background_processes::{Peek, ProcessOutput, ProcessReport};
use crate::tool_cards::ProcessDisplayState;

/// Output lines a peek shows, the latest last.
const PEEK_LINES: u16 = 6;
/// What leads each peek row, so the output reads as belonging to the row above.
const GUTTER: &str = "  │ ";
const DOCK_EXPAND: &str = "Expand output";
const BACK: &str = "Esc back";

/// Only the painted expand label responds to a click.
pub(super) fn expand_button(body: Rect, row: u16, focused: bool) -> Rect {
    let gutter = GUTTER.width() as u16;
    let width = if focused { "f expand" } else { DOCK_EXPAND }.width() as u16;
    Rect::new(
        body.x.saturating_add(gutter),
        row,
        width.min(body.width.saturating_sub(gutter)),
        1,
    )
}

/// The peek's rows under its process row; focus lands on none of them.
///
/// `ended` is the outcome the panel lists for a process that is no longer running,
/// and a pending stop confirmation replaces the usual key hints.
pub(super) fn peek_lines(
    peek: Option<&Peek>,
    ended: Option<&ProcessReport>,
    panel: &SidePanel,
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
            let screen = panel
                .output_view
                .screen(output, room as u16, PEEK_LINES, palette);
            let printed = &screen.lines;
            if printed.is_empty() {
                lines.push(row("No output yet".into(), muted));
            }
            for line in &printed[printed.len().saturating_sub(usize::from(PEEK_LINES))..] {
                let mut line = line.clone();
                line.spans.insert(0, Span::styled(GUTTER, muted));
                lines.push(line);
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
    let hint = if panel.confirm_stop && running {
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
    } else if !panel.overlay_open() {
        row(DOCK_EXPAND.into(), Style::default().fg(palette.accent))
    } else {
        let keys = if running {
            "f expand · x stop · Enter card"
        } else {
            "f expand · Enter card"
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

/// Parsed output is shared across repaints until a new snapshot, palette, or
/// viewport arrives. Each backend reply is a replacement tail, not a delta.
#[derive(Debug)]
struct ParsedOutput {
    output: Arc<ProcessOutput>,
    size: (u16, u16),
    palette: Palette,
    screen: Arc<RenderedOutput>,
}

#[derive(Clone, Copy, Debug, Default)]
enum Position {
    #[default]
    Compact,
    Follow,
    Scrolled(usize),
}

#[derive(Debug, Default)]
pub(super) struct OutputView {
    cache: RefCell<Option<ParsedOutput>>,
    position: Cell<Position>,
    max_offset: Cell<usize>,
    page: Cell<u16>,
}

impl OutputView {
    fn screen(
        &self,
        output: &Arc<ProcessOutput>,
        width: u16,
        height: u16,
        palette: Palette,
    ) -> Arc<RenderedOutput> {
        let mut cache = self.cache.borrow_mut();
        // A polling reply replaces the tail, and can evict its oldest lines.
        // Keep the snapshot under the reader until they resume following.
        let output = cache
            .as_ref()
            .filter(|parsed| {
                self.paused() && parsed.output.process.process_id == output.process.process_id
            })
            .map_or_else(|| output.clone(), |parsed| parsed.output.clone());
        if let Some(parsed) = cache.as_ref()
            && Arc::ptr_eq(&parsed.output, &output)
            && parsed.size == (width, height)
            && parsed.palette == palette
        {
            return parsed.screen.clone();
        }
        let screen = Arc::new(terminal_lines(&output.chunks, width, height, &palette));
        *cache = Some(ParsedOutput {
            output,
            size: (width, height),
            palette,
            screen: screen.clone(),
        });
        screen
    }

    fn paused(&self) -> bool {
        matches!(self.position.get(), Position::Scrolled(_))
    }

    pub(super) fn expanded(&self) -> bool {
        !matches!(self.position.get(), Position::Compact)
    }
    pub(super) fn expand(&self) {
        self.position.set(Position::Follow);
    }
    pub(super) fn restore(&self) {
        self.position.set(Position::Compact);
    }

    pub(super) fn scroll(&self, delta: i32) {
        let max = self.max_offset.get();
        let top = self.top(max).saturating_add_signed(delta as isize);
        self.position.set(if top >= max {
            Position::Follow
        } else {
            Position::Scrolled(top)
        });
    }

    fn top(&self, max: usize) -> usize {
        match self.position.get() {
            Position::Scrolled(top) => top.min(max),
            _ => max,
        }
    }

    pub(super) fn key(&self, key: KeyCode) -> OverlayKey {
        match key {
            KeyCode::Up => self.scroll(-1),
            KeyCode::Down => self.scroll(1),
            KeyCode::PageUp => self.scroll(-i32::from(self.page.get())),
            KeyCode::PageDown => self.scroll(i32::from(self.page.get())),
            KeyCode::Home => self.position.set(Position::Scrolled(0)),
            KeyCode::End => self.expand(),
            _ => return OverlayKey::Ignored,
        }
        OverlayKey::Moved
    }
}

pub(super) fn render_expanded(
    frame: &mut Frame<'_>,
    inner: Rect,
    area: Rect,
    state: &UiState,
    panel: &SidePanel,
    palette: Palette,
) {
    let peek = state
        .background_processes
        .peek()
        .filter(|peek| Some(&peek.process_id) == panel.peek.as_ref());
    let output = peek.and_then(|peek| peek.output.as_ref());
    let command = output
        .map(|output| output.process.command.as_str())
        .or_else(|| {
            state
                .background_processes
                .running()
                .iter()
                .find(|p| Some(&p.process_id) == panel.peek.as_ref())
                .map(|p| p.command.as_str())
        })
        .unwrap_or("Background process");
    let header = Line::from(vec![
        heading("Running  ", palette),
        Span::raw(first_line(command)),
    ]);
    frame.render_widget(Paragraph::new(header), Rect { height: 1, ..inner });
    let footer = Rect {
        y: inner.bottom() - 1,
        height: 1,
        ..inner
    };
    let body = Rect {
        y: inner.y.saturating_add(2),
        height: inner.height.saturating_sub(3),
        ..inner
    };
    let view = &panel.output_view;
    let screen = output.map(|output| view.screen(output, body.width, body.height, palette));
    let printed = screen
        .as_ref()
        .map(|screen| screen.lines.as_slice())
        .unwrap_or_default();
    let status = match output {
        Some(output) if output.process.state != ProcessDisplayState::Running => {
            outcome(&output.process)
        }
        _ if peek.is_some_and(|p| p.gone) => "unavailable".to_string(),
        _ => "live".to_string(),
    };
    let omitted = view
        .cache
        .borrow()
        .as_ref()
        .filter(|_| screen.is_some())
        .map_or(0, |snapshot| snapshot.output.omitted_bytes);
    let omitted = if omitted > 0 {
        format!(" · {omitted} earlier bytes omitted")
    } else {
        String::new()
    };
    let recent = if screen.as_ref().is_some_and(|screen| screen.history_limited) {
        " · showing recent output"
    } else {
        ""
    };
    let reading = if view.paused() {
        "scroll paused"
    } else {
        "read-only"
    };
    let status_line = format!("{status} · {reading}{recent}{omitted}");
    if inner.height > 2 {
        frame.render_widget(
            Paragraph::new(Line::styled(
                status_line,
                Style::default().fg(palette.secondary),
            )),
            Rect {
                y: inner.y + 1,
                height: 1,
                ..inner
            },
        );
    }
    let max = printed.len().saturating_sub(usize::from(body.height));
    view.page.set(body.height);
    view.max_offset.set(max);
    let top = view.top(max);
    if printed.is_empty() {
        let message = if peek.is_some_and(|p| p.gone) {
            "No longer held by the backend"
        } else if output.is_some() {
            "No output yet"
        } else {
            "reading…"
        };
        frame.render_widget(Paragraph::new(message), body);
    } else {
        for (i, line) in printed[top..]
            .iter()
            .take(usize::from(body.height))
            .enumerate()
        {
            frame.render_widget(
                line,
                Rect {
                    y: body.y + i as u16,
                    height: 1,
                    ..body
                },
            );
        }
    }
    frame.render_widget(
        Paragraph::new(ui::keycap_hints(
            "Esc back · ↑↓ scroll · End follow",
            palette,
        )),
        footer,
    );
    *panel.painted.borrow_mut() = Painted {
        surface: area,
        area: body,
        output_button: Some(Rect {
            width: (BACK.width() as u16).min(footer.width),
            ..footer
        }),
        ..Painted::default()
    };
}
