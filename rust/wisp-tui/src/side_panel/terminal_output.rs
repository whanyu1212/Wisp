//! Renders captured process output as terminal cells within the side panel.

use std::fmt::Write as _;

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthChar as _;

use crate::color_depth::indexed_rgb;
use crate::reducer::background_processes::OutputStream;
use crate::theme::Palette;

/// Covers the backend's default retained stdout and stderr combined.
const MAX_INPUT_BYTES: usize = 128 * 1024;
const MAX_COLUMNS: u16 = 240;
const MAX_SCREEN_ROWS: u16 = 100;
const MAX_HISTORY_ROWS: usize = 2_000;
const MAX_GRID_CELLS: usize = 200_000;

#[derive(Debug, Default)]
pub(super) struct RenderedOutput {
    pub(super) lines: Vec<Line<'static>>,
    /// The renderer clipped the input or filled its bounded scrollback.
    pub(super) history_limited: bool,
}

/// Replays the latest captured output at this width and returns scrollback followed by screen rows.
pub(super) fn terminal_lines(
    chunks: &[(OutputStream, String)],
    width: u16,
    rows: u16,
    palette: &Palette,
) -> RenderedOutput {
    if width == 0 || rows == 0 {
        return RenderedOutput::default();
    }
    let width = width.min(MAX_COLUMNS);
    // vt100 0.15's wrap implementation requires two rows when a line fills the screen.
    let rows = rows.clamp(2, MAX_SCREEN_ROWS);
    // Reading history can temporarily expand the main and alternate screens.
    let history_capacity = (MAX_GRID_CELLS / (3 * usize::from(width))).min(MAX_HISTORY_ROWS);
    let mut input = TerminalInput {
        terminal: vt100::Parser::new(rows, width, history_capacity),
        width,
        stream: OutputStream::Stdout,
        warning: warning_sequence(palette.warning),
        warning_active: false,
        escape: String::new(),
    };
    let mut parser = vte::Parser::new();
    let (chunks, input_limited) = retained_chunks(chunks);
    for (stream, chunk) in chunks {
        input.stream = stream;
        for byte in chunk.bytes() {
            parser.advance(&mut input, byte);
        }
    }

    let terminal = &mut input.terminal;
    terminal.set_scrollback(history_capacity);
    let history = terminal.screen().scrollback() as u16;
    // vt100 0.15 requires the viewport to be at least as tall as its history offset.
    if history > rows {
        terminal.set_size(history, width);
    }
    let mut lines = Vec::with_capacity(usize::from(history) + usize::from(rows));
    lines.extend((0..history).map(|row| screen_line(terminal.screen(), row, width, palette)));
    terminal.set_scrollback(0);
    let screen = terminal.screen();
    let (cursor_row, _) = screen.cursor_position();
    let first_screen_row = lines.len();
    lines.extend((0..rows).map(|row| screen_line(screen, row, width, palette)));
    // Preserve blank lines already passed by the cursor. A trailing newline leaves an empty
    // cursor row, and the unused rows below it are viewport padding.
    let completed = first_screen_row + usize::from(cursor_row);
    while lines.len() > completed && lines.last().is_some_and(|line| line.spans.is_empty()) {
        lines.pop();
    }
    RenderedOutput {
        lines,
        history_limited: input_limited || usize::from(history) == history_capacity,
    }
}

fn retained_chunks(
    chunks: &[(OutputStream, String)],
) -> (impl Iterator<Item = (OutputStream, &str)>, bool) {
    let mut bytes_left = MAX_INPUT_BYTES;
    let mut start = 0;
    let mut skip = 0;
    for (index, (_, chunk)) in chunks.iter().enumerate().rev() {
        if chunk.len() > bytes_left {
            start = index;
            skip = chunk.len() - bytes_left;
            while !chunk.is_char_boundary(skip) {
                skip += 1;
            }
            break;
        }
        bytes_left -= chunk.len();
    }
    let input_limited = skip > 0;
    let retained = chunks[start..].iter().map(move |(stream, chunk)| {
        let offset = std::mem::take(&mut skip);
        (*stream, &chunk[offset..])
    });
    (retained, input_limited)
}

/// Only these parser callbacks can change the virtual screen. OSC, DCS, terminal mode
/// changes and other commands have no handlers, and their source bytes never reach ratatui.
struct TerminalInput {
    terminal: vt100::Parser,
    width: u16,
    stream: OutputStream,
    warning: Option<String>,
    warning_active: bool,
    escape: String,
}

impl vte::Perform for TerminalInput {
    fn print(&mut self, character: char) {
        if self.warning_active && self.stream == OutputStream::Stdout {
            self.terminal.process(b"\x1b[39m");
            self.warning_active = false;
        }
        if self.stream == OutputStream::Stderr
            && self.terminal.screen().fgcolor() == vt100::Color::Default
            && let Some(warning) = &self.warning
        {
            self.terminal.process(warning.as_bytes());
            self.warning_active = true;
        }

        let character = if character.is_control()
            || crate::is_bidi_control(character)
            || character
                .width()
                .is_some_and(|width| width > 2 || width > usize::from(self.width))
        {
            '�'
        } else {
            character
        };
        self.terminal
            .process(character.encode_utf8(&mut [0; 4]).as_bytes());
    }

    fn execute(&mut self, byte: u8) {
        match byte {
            // The backend captures pipes. Normalize LF as a terminal line break.
            b'\n' => self.terminal.process(b"\r\n"),
            b'\r' | b'\t' | 8 => self.terminal.process(&[byte]),
            _ => {}
        }
    }

    fn csi_dispatch(
        &mut self,
        params: &vte::Params,
        intermediates: &[u8],
        ignore: bool,
        action: char,
    ) {
        if ignore || !intermediates.is_empty() {
            return;
        }
        match action {
            // Colours, cursor moves, erase-in-line. Restrict movement to small screen
            // coordinates and avoid commands that can multiply or clear the full grid.
            'm' => {
                if self.warning_active {
                    self.terminal.process(b"\x1b[39m");
                    self.warning_active = false;
                }
            }
            'A' | 'B' | 'C' | 'D' | 'G' | 'H' | 'K'
                if params
                    .iter()
                    .all(|param| matches!(param, [0..=MAX_COLUMNS])) => {}
            _ => return,
        }

        self.escape.clear();
        self.escape.push_str("\x1b[");
        for (index, param) in params.iter().enumerate() {
            if index > 0 {
                self.escape.push(';');
            }
            for (subindex, value) in param.iter().enumerate() {
                if subindex > 0 {
                    self.escape.push(':');
                }
                write!(self.escape, "{value}").expect("writing to a String is infallible");
            }
        }
        self.escape.push(action);
        self.terminal.process(self.escape.as_bytes());
    }
}

fn warning_sequence(color: Color) -> Option<String> {
    if let Color::Rgb(red, green, blue) = color {
        Some(format!("\x1b[38;2;{red};{green};{blue}m"))
    } else {
        None
    }
}

fn screen_line(screen: &vt100::Screen, row: u16, width: u16, palette: &Palette) -> Line<'static> {
    let last = (0..width).rev().find(|&col| {
        screen.cell(row, col).is_some_and(|cell| {
            cell.has_contents() || cell.bgcolor() != vt100::Color::Default || cell.inverse()
        })
    });
    let Some(last) = last else {
        return Line::default();
    };
    let mut spans = Vec::new();
    let mut contents = String::new();
    let mut style = Style::default();
    for cell in (0..=last)
        .filter_map(|col| screen.cell(row, col))
        .filter(|cell| !cell.is_wide_continuation())
    {
        let next_style = cell_style(cell, palette);
        if next_style != style && !contents.is_empty() {
            spans.push(Span::styled(std::mem::take(&mut contents), style));
        }
        style = next_style;
        if cell.has_contents() {
            contents.push_str(&cell.contents());
        } else {
            contents.push(' ');
        }
    }
    if !contents.is_empty() {
        spans.push(Span::styled(contents, style));
    }
    Line::from(spans)
}

fn cell_style(cell: &vt100::Cell, palette: &Palette) -> Style {
    let mut style = Style {
        fg: terminal_color(cell.fgcolor(), palette),
        bg: terminal_color(cell.bgcolor(), palette),
        ..Style::default()
    };
    for (enabled, modifier) in [
        (cell.bold(), Modifier::BOLD),
        (cell.italic(), Modifier::ITALIC),
        (cell.underline(), Modifier::UNDERLINED),
        (cell.inverse(), Modifier::REVERSED),
    ] {
        if enabled {
            style = style.add_modifier(modifier);
        }
    }
    style
}

fn terminal_color(color: vt100::Color, palette: &Palette) -> Option<Color> {
    if palette.is_monochrome() {
        return None;
    }
    Some(match color {
        vt100::Color::Default => return None,
        vt100::Color::Rgb(r, g, b) => Color::Rgb(r, g, b),
        vt100::Color::Idx(index) => match index {
            0 => palette.background,
            1 | 9 => palette.error,
            2 | 10 => palette.success,
            3 | 11 => palette.warning,
            4 | 12 => palette.primary,
            5 | 13 => palette.secondary,
            6 | 14 => palette.accent,
            7 | 15 => palette.foreground,
            8 => palette.muted,
            16..=255 => {
                let (red, green, blue) = indexed_rgb(index);
                Color::Rgb(red, green, blue)
            }
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::theme;

    fn stdout(text: impl Into<String>) -> (OutputStream, String) {
        (OutputStream::Stdout, text.into())
    }

    fn plain(lines: &[Line<'_>]) -> Vec<String> {
        lines.iter().map(ToString::to_string).collect()
    }

    #[test]
    fn applies_split_escape_styles() {
        let palette = theme::default_theme().palette(false);
        let output = terminal_lines(
            &[stdout("a\x1b[3"), stdout("1;1mb\x1b[0m"), stdout("c")],
            12,
            5,
            &palette,
        );

        assert!(!output.history_limited);
        let lines = output.lines;
        assert_eq!(plain(&lines), ["abc"]);
        assert_eq!(lines[0].spans[1].style.fg, Some(palette.error));
        assert!(
            lines[0].spans[1]
                .style
                .add_modifier
                .contains(Modifier::BOLD)
        );
        assert_eq!(lines[0].spans[2].style, Style::default());
    }

    #[test]
    fn preserves_stream_default_colors() {
        let palette = theme::default_theme().palette(false);
        let lines = terminal_lines(
            &[
                (OutputStream::Stderr, "a\x1b[32mb\x1b[0mc".into()),
                stdout("d"),
            ],
            12,
            5,
            &palette,
        )
        .lines;

        assert_eq!(plain(&lines), ["abcd"]);
        let colors: Vec<_> = lines[0].spans.iter().map(|span| span.style.fg).collect();
        assert_eq!(
            colors,
            [
                Some(palette.warning),
                Some(palette.success),
                Some(palette.warning),
                None
            ]
        );
    }

    #[test]
    fn shows_in_place_updates() {
        let palette = theme::default_theme().palette(false);
        let lines = terminal_lines(
            &[stdout("progress 123\r\x1b[Kdone\r\n\nnext\n")],
            16,
            2,
            &palette,
        )
        .lines;

        assert_eq!(plain(&lines), ["done", "", "next"]);
        assert_eq!(
            plain(&terminal_lines(&[stdout("abc\rX")], 16, 4, &palette).lines),
            ["Xbc"]
        );
    }

    #[test]
    fn wraps_on_width_changes() {
        let palette = theme::default_theme().palette(false);
        let output = [stdout("hello界!")];

        assert_eq!(
            plain(&terminal_lines(&output, 5, 1, &palette).lines),
            ["hello", "界!"]
        );
        assert_eq!(
            plain(&terminal_lines(&output, 9, 2, &palette).lines),
            ["hello界!"]
        );
        assert_eq!(
            plain(&terminal_lines(&[stdout("界\u{17d8}!")], 1, 1, &palette).lines),
            ["�", "�", "!"]
        );
    }

    #[test]
    fn drops_terminal_side_effects() {
        let palette = theme::default_theme().palette(false);
        let lines = terminal_lines(
            &[
                stdout("a\x1b]52;c;"),
                stdout("YWJj\x07\x1b]0;title\x1b\\\x1b]8;;https://example.test\x1b\\"),
                stdout("link\x1b]8;;\x1b\\\x1bPqhidden\x1b\\"),
                stdout("\x1b[8;65535;65535t\x1b[65535b\x1b[?1049h\x1b[65535B"),
                stdout("\u{202e}\u{009b}z\x1b]52;c;unterminated"),
            ],
            20,
            3,
            &palette,
        )
        .lines;

        assert_eq!(plain(&lines), ["alink��z"]);
    }

    #[test]
    fn caps_input_and_history() {
        let palette = theme::default_theme().palette(false);
        let output = [stdout(format!(
            "old\n{}latest",
            "界\n".repeat(MAX_INPUT_BYTES)
        ))];
        let rendered = terminal_lines(&output, 12, 2, &palette);
        assert!(rendered.history_limited);
        let lines = rendered.lines;

        assert_eq!(lines.len(), MAX_HISTORY_ROWS + 2);
        assert_eq!(lines[0].to_string(), "界");
        assert_eq!(lines.last().unwrap().to_string(), "latest");
    }

    #[test]
    fn reports_limited_scrollback() {
        let palette = theme::default_theme().palette(false);
        let chunks = [stdout(format!("first\n{}last", "line\n".repeat(998)))];

        let output = terminal_lines(&chunks, 160, 24, &palette);

        assert!(output.history_limited);
        assert!(output.lines.len() < 1000);
        assert_eq!(output.lines.first().unwrap().to_string(), "line");
        assert_eq!(output.lines.last().unwrap().to_string(), "last");
    }

    #[test]
    fn reports_limited_input() {
        let palette = theme::default_theme().palette(false);
        let chunks = [stdout(format!("{}last", "x\r".repeat(MAX_INPUT_BYTES)))];

        let output = terminal_lines(&chunks, 160, 24, &palette);

        assert!(output.history_limited);
        assert_eq!(plain(&output.lines), ["last"]);
    }

    #[test]
    fn renders_extended_colors() {
        let palette = theme::default_theme().palette(false);
        let lines =
            terminal_lines(&[stdout("\x1b[38;5;170;48;2;2;3;4mtext")], 12, 3, &palette).lines;

        assert_eq!(plain(&lines), ["text"]);
        assert_eq!(
            lines[0].spans[0].style,
            Style::default()
                .fg(Color::Rgb(215, 95, 215))
                .bg(Color::Rgb(2, 3, 4))
        );
    }

    #[test]
    fn suppresses_monochrome_colors() {
        let palette = theme::default_theme().palette(true);
        let lines =
            terminal_lines(&[stdout("\x1b[38;5;170;48;2;2;3;4mtext")], 12, 3, &palette).lines;

        assert_eq!(plain(&lines), ["text"]);
        assert_eq!(lines[0].spans[0].style, Style::default());
    }
}
