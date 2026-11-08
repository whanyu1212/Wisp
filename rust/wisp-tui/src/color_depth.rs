//! Terminal colour depth and the downconversion of a rendered frame to it.
//!
//! Palettes stay RGB because blending and contrast checks need exact channels. A frame is
//! downconverted only after it is rendered, so every colour, including derived and fixed
//! ones, reaches a 256- or 16-colour terminal as the nearest colour it can show.

use std::collections::HashMap;

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

/// The nearest named ANSI colour. Named variants are used rather than `Indexed(0..16)`,
/// which crossterm writes as a 256-colour sequence a 16-colour terminal cannot parse.
/// A tie keeps the lower index.
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

#[cfg(test)]
mod tests {
    use super::*;

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
