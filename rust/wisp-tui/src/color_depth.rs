//! Terminal colour depth and the downconversion of a rendered frame to it.
//!
//! Palettes stay RGB because blending and contrast checks need exact channels. A frame is
//! downconverted only after it is rendered, so every colour, including derived and fixed
//! ones, reaches a 256- or 16-colour terminal as the nearest colour it can show.
//! [`BasicColorWriter`] then encodes 16-colour output with basic ANSI codes.

use std::collections::HashMap;
use std::io::{self, Write};

use ratatui::buffer::Buffer;
use ratatui::style::Color;

/// How many colours the terminal can show.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum ColorDepth {
    /// 24-bit RGB. Frames are sent unchanged.
    #[default]
    TrueColor,
    /// The xterm 256-colour palette.
    Ansi256,
    /// The 16 named ANSI colours.
    Ansi16,
}

/// The variable that overrides detection: `auto`, `truecolor`, `256` or `16`.
pub(crate) const OVERRIDE_VARIABLE: &str = "WISP_TUI_COLORS";

impl ColorDepth {
    /// Detects the colour depth, reading environment variables through `variable`.
    ///
    /// `WISP_TUI_COLORS` wins when it names a depth. Otherwise the result is truecolor when
    /// `COLORTERM` or `TERM` advertises it, 256 for macOS Terminal.app, 16 when `TERM` names
    /// a 16- or 8-colour terminal, and truecolor for anything else. Truecolor stays the
    /// default without a strong signal against it, because a false downgrade is visible on
    /// every capable terminal. Inside tmux, RGB is sent as is: tmux downconverts it for an
    /// outer terminal that lacks it, without contrast repair, and the override is the way
    /// to get the repair there.
    pub(crate) fn from_environment(variable: impl Fn(&str) -> Option<String>) -> Self {
        let requested = variable(OVERRIDE_VARIABLE).unwrap_or_default();
        match requested.trim().to_ascii_lowercase().as_str() {
            "truecolor" => return Self::TrueColor,
            "256" => return Self::Ansi256,
            "16" => return Self::Ansi16,
            _ => {}
        }
        let colorterm = variable("COLORTERM")
            .unwrap_or_default()
            .to_ascii_lowercase();
        let term = variable("TERM").unwrap_or_default().to_ascii_lowercase();
        if ["truecolor", "24bit"]
            .iter()
            .any(|name| colorterm.contains(name) || term.contains(name))
            || term.ends_with("-direct")
        {
            return Self::TrueColor;
        }
        if variable("TERM_PROGRAM").as_deref() == Some("Apple_Terminal") {
            return Self::Ansi256;
        }
        if matches!(
            term.as_str(),
            "linux" | "vt100" | "vt220" | "ansi" | "xterm-color"
        ) || term.ends_with("-16color")
            || term.ends_with("-8color")
        {
            return Self::Ansi16;
        }
        Self::TrueColor
    }

    /// Replaces every colour in a rendered buffer with the nearest one this depth can show.
    pub(crate) fn downconvert_buffer(self, buffer: &mut Buffer) {
        if self == Self::TrueColor {
            return;
        }
        // A frame has a few dozen distinct pairs across thousands of cells, and a contrast
        // repair can scan the whole palette, so each pair is converted once per frame.
        let mut converted = HashMap::new();
        for cell in &mut buffer.content {
            (cell.fg, cell.bg) = *converted
                .entry((cell.fg, cell.bg))
                .or_insert_with(|| self.downconvert_pair(cell.fg, cell.bg));
            cell.underline_color = self.downconvert(cell.underline_color);
        }
    }

    /// Downconverts a cell's foreground and background together.
    ///
    /// Quantizing each colour on its own can push text below WCAG AA (4.5:1) against its
    /// background. When that happens, the foreground moves to the nearest colour of this
    /// depth that keeps the original ratio, capped at 4.5:1. A pair that was already below
    /// that keeps its own ratio as the floor. Near-gray text, including every NO_COLOR
    /// colour, is repaired to a gray when one is readable, so the repair adds no hue.
    fn downconvert_pair(self, foreground: Color, background: Color) -> (Color, Color) {
        let (shown_foreground, shown_background) =
            (self.downconvert(foreground), self.downconvert(background));
        let (Some(original_text), Some(original_fill), Some(fill)) = (
            rgb(foreground),
            rgb(background),
            shown_rgb(shown_background),
        ) else {
            return (shown_foreground, shown_background);
        };
        let target = contrast(original_text, original_fill).min(MINIMUM_CONTRAST);
        let readable = |candidate: Color| {
            shown_rgb(candidate).is_some_and(|text| contrast(text, fill) >= target)
        };
        if readable(shown_foreground) {
            return (shown_foreground, shown_background);
        }
        let candidates: Vec<(Color, (u8, u8, u8))> = match self {
            Self::TrueColor => return (foreground, background),
            Self::Ansi256 => (16..=255)
                .map(|index| (Color::Indexed(index), indexed_rgb(index)))
                .collect(),
            Self::Ansi16 => ANSI_16.to_vec(),
        };
        let nearest_readable = |grays_only: bool| {
            candidates
                .iter()
                .filter(|(_, shown)| !grays_only || is_gray(*shown))
                .filter(|(candidate, _)| readable(*candidate))
                .min_by_key(|(_, shown)| distance(original_text, *shown))
                .map(|(candidate, _)| *candidate)
        };
        let near_gray = {
            let (r, g, b) = original_text;
            r.max(g).max(b) - r.min(g).min(b) <= NEAR_GRAY_SPREAD
        };
        let repaired = near_gray
            .then(|| nearest_readable(true))
            .flatten()
            .or_else(|| nearest_readable(false))
            .unwrap_or(shown_foreground);
        (repaired, shown_background)
    }

    /// Keeps background tiers apart after downconversion.
    ///
    /// Tiers a few levels apart in RGB, such as a background and the surface on it, can
    /// land on one 256-colour gray. At 256 colours, a tier that would show as the same
    /// colour as an earlier, different tier moves to the nearest free step of the gray
    /// ramp on the side it sits on in RGB, or on the other side when that side is full,
    /// so it stays visible after the frame is downconverted. Tiers equal in RGB stay
    /// equal. Other depths return the tiers unchanged: truecolor needs no help, and 16
    /// colours have no steps to spare.
    pub(crate) fn separate_tiers<const N: usize>(self, tiers: [Color; N]) -> [Color; N] {
        if self != Self::Ansi256 {
            return tiers;
        }
        let shown = |color: Color| shown_rgb(self.downconvert(color));
        let mut separated = tiers;
        for index in 1..N {
            if let Some(twin) = tiers[..index].iter().position(|tier| *tier == tiers[index]) {
                separated[index] = separated[twin];
                continue;
            }
            let taken = |color: Color| {
                separated[..index]
                    .iter()
                    .any(|earlier| shown(*earlier) == shown(color))
            };
            let Some(earlier) =
                (0..index).find(|earlier| shown(separated[*earlier]) == shown(tiers[index]))
            else {
                continue;
            };
            let (Some(level), Some(from), Some(other)) = (
                shown(separated[earlier]).map(gray_level),
                rgb(tiers[index]).map(gray_level),
                rgb(tiers[earlier]).map(gray_level),
            ) else {
                continue;
            };
            let lighter = from >= other;
            let free = gray_ramp(level, lighter)
                .into_iter()
                .chain(gray_ramp(level, !lighter))
                .map(|step| Color::Rgb(step, step, step))
                .find(|candidate| !taken(*candidate));
            if let Some(free) = free {
                separated[index] = free;
            }
        }
        separated
    }

    /// Returns the nearest colour this depth can show. Named colours and `Reset` pass through.
    pub(crate) fn downconvert(self, color: Color) -> Color {
        match (self, color) {
            (Self::TrueColor, _) => color,
            (Self::Ansi256, Color::Rgb(r, g, b)) => Color::Indexed(nearest_256(r, g, b)),
            (Self::Ansi16, Color::Rgb(r, g, b)) => nearest_16(r, g, b),
            (Self::Ansi16, Color::Indexed(index)) => {
                let (r, g, b) = indexed_rgb(index);
                nearest_16(r, g, b)
            }
            _ => color,
        }
    }
}

/// The six channel levels of the xterm 6×6×6 colour cube (indices 16–231).
const CUBE_LEVELS: [u8; 6] = [0, 95, 135, 175, 215, 255];

/// The nearest xterm 256-colour index, choosing between the colour cube and the 24-step
/// gray ramp (232–255). The 16 system colours are skipped because terminals theme them.
/// A tie keeps the cube colour.
fn nearest_256(r: u8, g: u8, b: u8) -> u8 {
    let cube_index = |value: u8| -> u8 {
        CUBE_LEVELS
            .iter()
            .enumerate()
            .min_by_key(|(_, level)| level.abs_diff(value))
            .map_or(0, |(index, _)| index as u8)
    };
    let (cr, cg, cb) = (cube_index(r), cube_index(g), cube_index(b));
    let cube = (
        CUBE_LEVELS[usize::from(cr)],
        CUBE_LEVELS[usize::from(cg)],
        CUBE_LEVELS[usize::from(cb)],
    );
    let average = (u16::from(r) + u16::from(g) + u16::from(b)) / 3;
    // The ramp runs 8, 18, …, 238.
    let gray_step = (average.saturating_sub(3) / 10).min(23) as u8;
    let gray = 8 + 10 * gray_step;
    if distance((r, g, b), (gray, gray, gray)) < distance((r, g, b), cube) {
        232 + gray_step
    } else {
        16 + 36 * cr + 6 * cg + cb
    }
}

/// The mean of a colour's channels, the level its gray would sit at.
fn gray_level((r, g, b): (u8, u8, u8)) -> u8 {
    ((u16::from(r) + u16::from(g) + u16::from(b)) / 3) as u8
}

/// The steps of the 256-colour gray ramp (8, 18, …, 238) beyond `level`, nearest first:
/// those above it when `lighter`, otherwise those below it.
fn gray_ramp(level: u8, lighter: bool) -> Vec<u8> {
    let steps = (8..=238_u8).step_by(10);
    if lighter {
        steps.filter(|step| *step > level).collect()
    } else {
        steps.rev().filter(|step| *step < level).collect()
    }
}

/// The 16 named ANSI colours with xterm's default RGB values, in index order.
const ANSI_16: [(Color, (u8, u8, u8)); 16] = [
    (Color::Black, (0, 0, 0)),
    (Color::Red, (205, 0, 0)),
    (Color::Green, (0, 205, 0)),
    (Color::Yellow, (205, 205, 0)),
    (Color::Blue, (0, 0, 238)),
    (Color::Magenta, (205, 0, 205)),
    (Color::Cyan, (0, 205, 205)),
    (Color::Gray, (229, 229, 229)),
    (Color::DarkGray, (127, 127, 127)),
    (Color::LightRed, (255, 0, 0)),
    (Color::LightGreen, (0, 255, 0)),
    (Color::LightYellow, (255, 255, 0)),
    (Color::LightBlue, (92, 92, 255)),
    (Color::LightMagenta, (255, 0, 255)),
    (Color::LightCyan, (0, 255, 255)),
    (Color::White, (255, 255, 255)),
];

/// The nearest named ANSI colour. A tie keeps the lower index. crossterm still writes
/// named colours as `38;5;n`, so [`BasicColorWriter`] turns them into basic codes.
fn nearest_16(r: u8, g: u8, b: u8) -> Color {
    ANSI_16
        .iter()
        .min_by_key(|(_, rgb)| distance((r, g, b), *rgb))
        .map_or(Color::Reset, |(color, _)| *color)
}

/// The RGB value xterm uses for a 256-colour index.
fn indexed_rgb(index: u8) -> (u8, u8, u8) {
    match index {
        0..16 => ANSI_16[usize::from(index)].1,
        16..232 => {
            let offset = index - 16;
            (
                CUBE_LEVELS[usize::from(offset / 36)],
                CUBE_LEVELS[usize::from(offset / 6 % 6)],
                CUBE_LEVELS[usize::from(offset % 6)],
            )
        }
        232.. => {
            let gray = 8 + 10 * (index - 232);
            (gray, gray, gray)
        }
    }
}

/// WCAG AA contrast for normal text, the floor the palettes already hold.
const MINIMUM_CONTRAST: f64 = 4.5;

/// The largest channel spread, max minus min, at which text counts as near-gray and its
/// contrast repair prefers grays. Theme body text such as Dawn's #575279 (spread 39) is
/// in; status hues such as #a95570 (spread 84) are not.
const NEAR_GRAY_SPREAD: u8 = 48;

fn is_gray((r, g, b): (u8, u8, u8)) -> bool {
    r == g && g == b
}

fn rgb(color: Color) -> Option<(u8, u8, u8)> {
    match color {
        Color::Rgb(r, g, b) => Some((r, g, b)),
        _ => None,
    }
}

/// The RGB a downconverted colour shows, assuming xterm's default palette. Terminals
/// theme their 16 system colours, so this is an estimate for named colours. `Reset`
/// has no known RGB.
fn shown_rgb(color: Color) -> Option<(u8, u8, u8)> {
    match color {
        Color::Rgb(r, g, b) => Some((r, g, b)),
        Color::Indexed(index) => Some(indexed_rgb(index)),
        Color::Reset => None,
        named => ANSI_16
            .iter()
            .find(|(candidate, _)| *candidate == named)
            .map(|(_, rgb)| *rgb),
    }
}

fn contrast(text: (u8, u8, u8), fill: (u8, u8, u8)) -> f64 {
    crate::theme::contrast_ratio(
        Color::Rgb(text.0, text.1, text.2),
        Color::Rgb(fill.0, fill.1, fill.2),
    )
}

/// Squared Euclidean distance between two RGB colours.
fn distance(a: (u8, u8, u8), b: (u8, u8, u8)) -> u32 {
    let channel = |x: u8, y: u8| u32::from(x.abs_diff(y)).pow(2);
    channel(a.0, b.0) + channel(a.1, b.1) + channel(a.2, b.2)
}

const ESCAPE: u8 = 0x1b;

/// A CSI sequence longer than this is not one crossterm writes; it passes through as is.
const MAX_SEQUENCE: usize = 64;

/// Writes terminal output, encoding the 16 system colours with basic ANSI codes in
/// 16-colour mode.
///
/// crossterm writes even named colours as 256-colour parameters (`38;5;n` and `48;5;n`),
/// which a 16-colour terminal cannot parse. In 16-colour mode this writer rewrites those
/// parameters in SGR sequences to `30`–`37`/`90`–`97` and `40`–`47`/`100`–`107`. Other
/// bytes, and all output at other depths, pass through unchanged. A sequence split across
/// writes is held until it completes.
pub(crate) struct BasicColorWriter<W> {
    inner: W,
    rewrite: bool,
    pending: Vec<u8>,
}

impl<W> BasicColorWriter<W> {
    pub(crate) fn new(inner: W, depth: ColorDepth) -> Self {
        Self {
            inner,
            rewrite: depth == ColorDepth::Ansi16,
            pending: Vec::new(),
        }
    }

    fn push(&mut self, byte: u8, out: &mut Vec<u8>) {
        match self.pending.as_slice() {
            [] if byte == ESCAPE => self.pending.push(byte),
            [] => out.push(byte),
            [ESCAPE] if byte == b'[' => self.pending.push(byte),
            [ESCAPE] => {
                out.append(&mut self.pending);
                self.push(byte, out);
            }
            _ => {
                self.pending.push(byte);
                if (0x40..=0x7e).contains(&byte) {
                    if byte == b'm' {
                        let parameters = &self.pending[2..self.pending.len() - 1];
                        out.extend_from_slice(b"\x1b[");
                        out.extend(basic_sgr(parameters));
                        out.push(b'm');
                        self.pending.clear();
                    } else {
                        out.append(&mut self.pending);
                    }
                } else if self.pending.len() > MAX_SEQUENCE {
                    out.append(&mut self.pending);
                }
            }
        }
    }
}

impl<W: Write> Write for BasicColorWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if !self.rewrite {
            return self.inner.write(buf);
        }
        let mut out = Vec::with_capacity(buf.len());
        for &byte in buf {
            self.push(byte, &mut out);
        }
        self.inner.write_all(&out)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Rewrites SGR parameters, turning each `38;5;n` or `48;5;n` with `n < 16` into the
/// matching basic foreground or background code.
fn basic_sgr(parameters: &[u8]) -> Vec<u8> {
    let tokens: Vec<&[u8]> = parameters.split(|byte| *byte == b';').collect();
    let mut codes: Vec<Vec<u8>> = Vec::with_capacity(tokens.len());
    let mut index = 0;
    while index < tokens.len() {
        let system = match tokens[index..] {
            [layer @ (b"38" | b"48"), b"5", value, ..] => std::str::from_utf8(value)
                .ok()
                .and_then(|value| value.parse::<u8>().ok())
                .filter(|value| *value < 16)
                .map(|value| (layer == b"48", value)),
            _ => None,
        };
        if let Some((background, value)) = system {
            let base = match (background, value < 8) {
                (false, true) => 30,
                (false, false) => 90 - 8,
                (true, true) => 40,
                (true, false) => 100 - 8,
            };
            codes.push((base + value).to_string().into_bytes());
            index += 3;
        } else {
            codes.push(tokens[index].to_vec());
            index += 1;
        }
    }
    codes.join(&b';')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn written(depth: ColorDepth, chunks: &[&[u8]]) -> Vec<u8> {
        let mut out = Vec::new();
        let mut writer = BasicColorWriter::new(&mut out, depth);
        for chunk in chunks {
            writer.write_all(chunk).unwrap();
        }
        writer.flush().unwrap();
        out
    }

    #[test]
    fn writer_passes_links_through() {
        // An OSC 8 link, even split across writes, reaches the terminal unchanged while
        // the colour beside it is still rewritten.
        let link = b"\x1b]8;id=1;https://example.test/a;b\x1b\\x\x1b]8;;\x1b\\";
        let (head, tail) = link.split_at(9);
        let out = written(ColorDepth::Ansi16, &[b"\x1b[38;5;1m", head, tail]);
        assert_eq!(out, [&b"\x1b[31m"[..], link].concat());
    }

    #[test]
    fn writer_uses_basic_codes() {
        for (input, expected) in [
            (&b"\x1b[38;5;1m"[..], &b"\x1b[31m"[..]),
            (b"\x1b[38;5;9m", b"\x1b[91m"),
            (b"\x1b[48;5;0m", b"\x1b[40m"),
            (b"\x1b[48;5;15m", b"\x1b[107m"),
            (b"\x1b[1;38;5;7;48;5;8m", b"\x1b[1;37;100m"),
            (b"\x1b[38;5;196m", b"\x1b[38;5;196m"),
            (b"\x1b[38;2;1;5;9m", b"\x1b[38;2;1;5;9m"),
            (b"\x1b[39;49m", b"\x1b[39;49m"),
            (b"\x1b[3;5H\x1b]0;title\x07", b"\x1b[3;5H\x1b]0;title\x07"),
            (b"a\x1b\x1b[38;5;2mb", b"a\x1b\x1b[32mb"),
        ] {
            assert_eq!(
                written(ColorDepth::Ansi16, &[input]),
                expected,
                "{}",
                String::from_utf8_lossy(input)
            );
        }
    }

    #[test]
    fn writer_joins_split_sequences() {
        let input = b"x\x1b[38;5;12;48;5;3my";
        let chunks: Vec<&[u8]> = input.chunks(1).collect();
        assert_eq!(written(ColorDepth::Ansi16, &chunks), b"x\x1b[94;43my");
    }

    #[test]
    fn writer_passes_other_depths() {
        let input = b"\x1b[38;5;9m\x1b[48;5;0m";
        for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
            assert_eq!(written(depth, &[input]), input);
        }
    }

    #[test]
    fn backend_emits_basic_codes() {
        use ratatui::backend::{Backend, CrosstermBackend};
        use ratatui::buffer::Cell;

        // Wisp forces colour output on in production; NO_COLOR in the test environment
        // would otherwise suppress every colour sequence.
        crossterm::style::force_color_output(true);
        let mut cell = Cell::default();
        cell.set_symbol("x")
            .set_fg(Color::LightRed)
            .set_bg(Color::Black);
        let mut out = Vec::new();
        {
            let mut backend =
                CrosstermBackend::new(BasicColorWriter::new(&mut out, ColorDepth::Ansi16));
            backend.draw([(0, 0, &cell)].into_iter()).unwrap();
            Backend::flush(&mut backend).unwrap();
        }
        // ratatui sends both colours in one `SetColors`, `38;5;9;48;5;0` from crossterm.
        let text = String::from_utf8_lossy(&out);
        assert!(text.contains("\x1b[91;40m"), "{text:?}");
        assert!(!text.contains("5;"), "{text:?}");
    }

    fn detect(variables: &[(&str, &str)]) -> ColorDepth {
        ColorDepth::from_environment(|name| {
            variables
                .iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| (*value).to_owned())
        })
    }

    #[test]
    fn detection_table() {
        let cases: &[(&[(&str, &str)], ColorDepth)] = &[
            (&[], ColorDepth::TrueColor),
            (&[("COLORTERM", "truecolor")], ColorDepth::TrueColor),
            (
                &[("COLORTERM", "24bit"), ("TERM", "linux")],
                ColorDepth::TrueColor,
            ),
            (&[("TERM", "xterm-direct")], ColorDepth::TrueColor),
            (&[("TERM", "xterm-256color")], ColorDepth::TrueColor),
            (&[("TERM", "tmux-256color")], ColorDepth::TrueColor),
            (
                &[
                    ("TERM_PROGRAM", "Apple_Terminal"),
                    ("TERM", "xterm-256color"),
                ],
                ColorDepth::Ansi256,
            ),
            (
                &[
                    ("TERM_PROGRAM", "Apple_Terminal"),
                    ("COLORTERM", "truecolor"),
                ],
                ColorDepth::TrueColor,
            ),
            (&[("TERM", "linux")], ColorDepth::Ansi16),
            (&[("TERM", "xterm-color")], ColorDepth::Ansi16),
            (&[("TERM", "rxvt-16color")], ColorDepth::Ansi16),
            (&[("TERM", "vt100")], ColorDepth::Ansi16),
        ];
        for (variables, expected) in cases {
            assert_eq!(detect(variables), *expected, "{variables:?}");
        }
    }

    #[test]
    fn override_wins() {
        let capable = [("COLORTERM", "truecolor")];
        for (value, expected) in [
            ("256", ColorDepth::Ansi256),
            (" 16 ", ColorDepth::Ansi16),
            ("TrueColor", ColorDepth::TrueColor),
        ] {
            let variables = [capable[0], (OVERRIDE_VARIABLE, value)];
            assert_eq!(detect(&variables), expected, "{value}");
        }
        let linux = [("TERM", "linux"), (OVERRIDE_VARIABLE, "truecolor")];
        assert_eq!(detect(&linux), ColorDepth::TrueColor);
        for value in ["auto", "bogus", ""] {
            let variables = [("TERM", "linux"), (OVERRIDE_VARIABLE, value)];
            assert_eq!(detect(&variables), ColorDepth::Ansi16, "{value}");
        }
    }

    #[test]
    fn nearest_256_colours() {
        for ((r, g, b), expected) in [
            ((0, 0, 0), 16),
            ((255, 255, 255), 231),
            ((255, 0, 0), 196),
            ((95, 135, 175), 67),
            ((8, 8, 8), 232),
            ((238, 238, 238), 255),
            ((128, 128, 128), 244),
            ((30, 30, 46), 235),
        ] {
            assert_eq!(nearest_256(r, g, b), expected, "({r}, {g}, {b})");
        }
    }

    #[test]
    fn indexed_round_trip() {
        for index in 16..=255 {
            let (r, g, b) = indexed_rgb(index);
            assert_eq!(nearest_256(r, g, b), index, "{index}");
        }
    }

    #[test]
    fn nearest_16_colours() {
        for ((r, g, b), expected) in [
            ((10, 10, 20), Color::Black),
            ((250, 250, 250), Color::White),
            ((200, 30, 30), Color::Red),
            ((120, 120, 130), Color::DarkGray),
            ((80, 80, 250), Color::LightBlue),
        ] {
            assert_eq!(nearest_16(r, g, b), expected, "({r}, {g}, {b})");
        }
    }

    /// The text and fill pairs a theme actually paints. A theme drawn on the terminal's own
    /// background leaves base cells with a `Reset` fill, so only the composer pair counts.
    fn painted_pairs(palette: crate::theme::Palette) -> Vec<(String, Color, Color)> {
        let fills = [
            ("background", palette.base().bg),
            ("composer", palette.composer().bg),
        ];
        let texts = [
            ("foreground", palette.foreground),
            ("muted", palette.muted),
            ("error", palette.error),
        ];
        let mut pairs = Vec::new();
        for (fill_name, fill) in fills {
            let Some(fill) = fill else { continue };
            for (text_name, text) in texts {
                pairs.push((format!("{text_name} on {fill_name}"), text, fill));
            }
        }
        pairs
    }

    fn ratio(text: Color, fill: Color) -> f64 {
        contrast(
            shown_rgb(text).expect("a converted colour"),
            shown_rgb(fill).expect("a converted colour"),
        )
    }

    #[test]
    fn themes_stay_readable() {
        let mut failures = Vec::new();
        for theme in crate::theme::themes() {
            for no_color in [false, true] {
                let palette = theme.palette(no_color);
                for (pair, text, fill) in painted_pairs(palette) {
                    let target = ratio(text, fill).min(MINIMUM_CONTRAST);
                    for depth in [ColorDepth::Ansi256, ColorDepth::Ansi16] {
                        let (shown_text, shown_fill) = depth.downconvert_pair(text, fill);
                        let shown = ratio(shown_text, shown_fill);
                        let label = format!("{} no_color={no_color} {depth:?} {pair}", theme.name);
                        if shown < target {
                            failures.push(format!("{label}: {shown:.2} < {target:.2}"));
                        }
                        if no_color && !shown_rgb(shown_text).is_some_and(is_gray) {
                            failures.push(format!("{label}: {shown_text:?} is not gray"));
                        }
                    }
                }
            }
        }
        assert!(failures.is_empty(), "{failures:#?}");
    }

    #[test]
    fn tiers_stay_apart() {
        for theme in crate::theme::themes() {
            for no_color in [false, true] {
                let palette = theme.palette(no_color);
                let fills = |palette: crate::theme::Palette| {
                    [
                        palette.background,
                        palette.surface,
                        palette.panel,
                        palette.selected,
                        palette.addition_background,
                        palette.deletion_background,
                    ]
                };
                let rgb = fills(palette);
                let shown = fills(palette.for_depth(ColorDepth::Ansi256))
                    .map(|fill| ColorDepth::Ansi256.downconvert(fill));
                for later in 1..rgb.len() {
                    for earlier in 0..later {
                        // Tiers equal in RGB, such as Grove's surface and panel, stay equal.
                        assert_eq!(
                            shown[later] == shown[earlier],
                            rgb[later] == rgb[earlier],
                            "{} no_color={no_color} tiers {earlier} and {later}: {shown:?}",
                            theme.name
                        );
                    }
                }
                let truecolor = palette.for_depth(ColorDepth::TrueColor);
                assert_eq!(truecolor.surface, palette.surface);
                assert_eq!(truecolor.selected, palette.selected);
            }
        }
    }

    #[test]
    fn tier_moves_past_collision() {
        let (background, surface) = (Color::Rgb(24, 24, 30), Color::Rgb(30, 30, 36));
        assert_eq!(
            ColorDepth::Ansi256.separate_tiers([background, surface]),
            [background, Color::Rgb(38, 38, 38)]
        );
        // A light theme's darker tier steps down the ramp.
        let (paper, darker) = (Color::Rgb(242, 240, 229), Color::Rgb(240, 238, 226));
        assert_eq!(
            ColorDepth::Ansi256.separate_tiers([paper, darker]),
            [paper, Color::Rgb(228, 228, 228)]
        );
        // A tier between two taken steps goes to the nearest free one instead of
        // bouncing between them.
        let gray = |level| Color::Rgb(level, level, level);
        assert_eq!(
            ColorDepth::Ansi256.separate_tiers([gray(24), gray(40), gray(30)]),
            [gray(24), gray(40), gray(48)]
        );
        for depth in [ColorDepth::TrueColor, ColorDepth::Ansi16] {
            assert_eq!(
                depth.separate_tiers([background, surface]),
                [background, surface]
            );
        }
    }

    #[test]
    fn diff_bands_keep_hue() {
        for theme in crate::theme::themes() {
            let palette = theme.palette(false);
            for (band, red) in [
                (palette.addition_background, false),
                (palette.deletion_background, true),
            ] {
                let (r, g, b) = shown_rgb(ColorDepth::Ansi256.downconvert(band)).unwrap();
                let (lead, other) = if red { (r, g) } else { (g, r) };
                assert!(
                    lead > other && lead > b,
                    "{} {band:?} shows as ({r}, {g}, {b})",
                    theme.name
                );
            }
        }
    }

    #[test]
    fn pair_keeps_contrast() {
        // Dawn's body text on its background. Quantized alone, the text lands on DarkGray
        // at 4.0:1 against White.
        let (text, fill) = (Color::Rgb(87, 82, 121), Color::Rgb(250, 244, 237));
        assert_eq!(ColorDepth::Ansi16.downconvert(text), Color::DarkGray);
        // Near-gray text is repaired to a gray, not to the nearer LightBlue.
        assert_eq!(
            ColorDepth::Ansi16.downconvert_pair(text, fill),
            (Color::Black, Color::White)
        );

        // A status hue keeps a hue when one is readable.
        let error = Color::Rgb(169, 85, 112);
        let (repaired, _) = ColorDepth::Ansi16.downconvert_pair(error, fill);
        assert!(!shown_rgb(repaired).is_some_and(is_gray), "{repaired:?}");
        assert!(ratio(repaired, Color::White) >= MINIMUM_CONTRAST);

        // NO_COLOR gray text stays gray.
        let (gray, light) = (Color::Rgb(105, 105, 105), Color::Rgb(245, 245, 245));
        assert_eq!(
            ColorDepth::Ansi16.downconvert_pair(gray, light),
            (Color::Black, Color::White)
        );

        // A pair already below 4.5:1 is held to its own ratio, not raised to 4.5:1.
        let (muted, panel) = (Color::Rgb(120, 120, 120), Color::Rgb(70, 70, 70));
        let (kept, _) = ColorDepth::Ansi256.downconvert_pair(muted, panel);
        assert_eq!(kept, ColorDepth::Ansi256.downconvert(muted));

        // Without a known background, the foreground is quantized on its own.
        let (alone, reset) = ColorDepth::Ansi16.downconvert_pair(text, Color::Reset);
        assert_eq!((alone, reset), (Color::DarkGray, Color::Reset));
    }

    #[test]
    fn downconvert_by_depth() {
        let rgb = Color::Rgb(255, 0, 0);
        assert_eq!(ColorDepth::TrueColor.downconvert(rgb), rgb);
        assert_eq!(ColorDepth::Ansi256.downconvert(rgb), Color::Indexed(196));
        assert_eq!(ColorDepth::Ansi16.downconvert(rgb), Color::LightRed);
        assert_eq!(
            ColorDepth::Ansi16.downconvert(Color::Indexed(196)),
            Color::LightRed
        );
        assert_eq!(
            ColorDepth::Ansi16.downconvert(Color::Indexed(1)),
            Color::Red
        );
        for depth in [ColorDepth::Ansi256, ColorDepth::Ansi16] {
            assert_eq!(depth.downconvert(Color::Reset), Color::Reset);
            assert_eq!(depth.downconvert(Color::Blue), Color::Blue);
        }
        assert_eq!(
            ColorDepth::Ansi256.downconvert(Color::Indexed(42)),
            Color::Indexed(42)
        );
    }
}
