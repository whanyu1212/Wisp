//! Presentation-only palettes, loaded from `theme_catalog.json`.

use crate::color_depth::ColorDepth;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use serde::{Deserialize, Deserializer};
use std::sync::OnceLock;

fn color<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Color, D::Error> {
    let value = String::deserialize(deserializer)?;
    if value.len() != 7
        || !value.starts_with('#')
        || !value[1..].bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err(serde::de::Error::custom("expected #rrggbb"));
    }
    let rgb = u32::from_str_radix(&value[1..], 16).map_err(serde::de::Error::custom)?;
    Ok(Color::Rgb((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8))
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
pub(crate) struct Palette {
    #[serde(deserialize_with = "color")]
    pub background: Color,
    #[serde(deserialize_with = "color")]
    pub foreground: Color,
    #[serde(deserialize_with = "color")]
    pub primary: Color,
    #[serde(deserialize_with = "color")]
    pub secondary: Color,
    #[serde(deserialize_with = "color")]
    pub accent: Color,
    #[serde(deserialize_with = "color")]
    pub success: Color,
    #[serde(deserialize_with = "color")]
    pub warning: Color,
    #[serde(deserialize_with = "color")]
    pub error: Color,
    #[serde(deserialize_with = "color")]
    pub surface: Color,
    #[serde(deserialize_with = "color")]
    pub panel: Color,
    /// The band under a selected row, one tier past `panel`.
    #[serde(deserialize_with = "color")]
    pub selected: Color,
    /// Reply headings, by level: each its own hue so levels read apart from each other
    /// and from the speaker label.
    #[serde(deserialize_with = "color")]
    pub heading_1: Color,
    #[serde(deserialize_with = "color")]
    pub heading_2: Color,
    #[serde(deserialize_with = "color")]
    pub heading_3: Color,
    /// Inline code in replies and the composer.
    #[serde(deserialize_with = "color")]
    pub code: Color,
    #[serde(deserialize_with = "color")]
    pub muted: Color,
    #[serde(deserialize_with = "color")]
    pub addition: Color,
    #[serde(deserialize_with = "color")]
    pub addition_background: Color,
    #[serde(deserialize_with = "color")]
    pub deletion: Color,
    #[serde(deserialize_with = "color")]
    pub deletion_background: Color,
    #[serde(skip)]
    monochrome: bool,
    #[serde(skip)]
    terminal_background: bool,
    #[serde(skip)]
    depth: ColorDepth,
}

impl Palette {
    pub fn is_monochrome(self) -> bool {
        self.monochrome
    }

    /// This palette for a terminal of `depth`. At 256 colours, background tiers and diff
    /// bands that would show as one colour are moved apart (see
    /// [`ColorDepth::separate_tiers`]); at 16 colours, where every dark tier shows as
    /// black, selections reverse instead of drawing a band.
    pub fn for_depth(mut self, depth: ColorDepth) -> Self {
        self.depth = depth;
        let [
            background,
            surface,
            panel,
            selected,
            addition_background,
            deletion_background,
        ] = depth.separate_tiers([
            self.background,
            self.surface,
            self.panel,
            self.selected,
            self.addition_background,
            self.deletion_background,
        ]);
        self.background = background;
        self.surface = surface;
        self.panel = panel;
        self.selected = selected;
        self.addition_background = addition_background;
        self.deletion_background = deletion_background;
        self
    }

    /// Whether a row band can show: not in monochrome, on the terminal's own
    /// background, or at 16 colours.
    pub fn shows_bands(self) -> bool {
        !(self.monochrome || self.terminal_background || self.depth == ColorDepth::Ansi16)
    }

    /// The user's prompt: a band on the panel tier, or bold text where a theme draws on
    /// the terminal's own background.
    pub fn user_text(self) -> Style {
        if self.terminal_background {
            self.base().add_modifier(Modifier::BOLD)
        } else {
            self.base().bg(self.panel)
        }
    }

    pub fn speaker_label(self) -> Style {
        Style::default()
            .fg(self.primary)
            .add_modifier(Modifier::BOLD)
    }

    pub fn tool_name(self) -> Style {
        Style::default()
            .fg(self.accent)
            .add_modifier(Modifier::BOLD)
    }

    /// A tool-specific label: the tool's hue on the plain background, in bold, so a column
    /// of stacked rows reads as labels rather than blocks. Where the hue is too faint on the
    /// background, the text moves toward the foreground until it reads; monochrome palettes
    /// use bold alone.
    pub fn chip(self, label: &str) -> Style {
        if self.monochrome {
            return Style::default().add_modifier(Modifier::BOLD);
        }
        // Stable hue families keep tools distinguishable even in a theme whose accents
        // are close together. Blend them into the theme, then enforce text contrast.
        // Status remains on the marker, so a failed Read does not change identity.
        let accent = match label.trim() {
            "Read" => Color::Rgb(132, 178, 224),
            "Search" => Color::Rgb(103, 194, 168),
            "List" => Color::Rgb(166, 191, 122),
            "Explore" => Color::Rgb(115, 196, 213),
            "Bash" => Color::Rgb(225, 163, 112),
            "Edit" => Color::Rgb(190, 151, 223),
            "Write" => Color::Rgb(218, 146, 172),
            "MCP" => Color::Rgb(148, 157, 225),
            "Process" => Color::Rgb(215, 191, 109),
            _ => self.muted,
        };
        let accent = mix(accent, self.secondary, 0.85);
        let text = (0..=20)
            .map(|step| mix(self.foreground, accent, f64::from(step) / 20.0))
            .find(|text| contrast_ratio(*text, self.background) >= 4.5)
            .unwrap_or(self.foreground);
        Style::default().fg(text).add_modifier(Modifier::BOLD)
    }

    /// The mode opening a turn's summary row, bold in a colour of its own: the warning
    /// hue for `Plan` and the accent for any other mode.
    pub fn run_mode(self, label: &str) -> Style {
        let color = if label == "Plan" {
            self.warning
        } else {
            self.accent
        };
        Style::default().fg(color).add_modifier(Modifier::BOLD)
    }

    /// The verb that opens a tool call's line: bold body text, or the error colour when
    /// the call failed. Colour stays free to mean an outcome.
    pub fn action_verb(self, failed: bool) -> Style {
        let color = if failed { self.error } else { self.foreground };
        Style::default().fg(color).add_modifier(Modifier::BOLD)
    }

    pub fn tool_argument(self) -> Style {
        Style::default().fg(self.secondary)
    }

    pub fn tool_command(self) -> Style {
        Style::default().fg(self.secondary)
    }

    pub fn tool_path(self) -> Style {
        Style::default().fg(self.foreground)
    }

    /// Emphasize only changed words, preserving a non-color cue in monochrome.
    pub fn diff_change(self, base: Style) -> Style {
        if self.monochrome {
            return base.add_modifier(Modifier::BOLD | Modifier::UNDERLINED);
        }
        let tint = if base.bg == Some(self.deletion_background) {
            self.error
        } else {
            self.success
        };
        let background = base.bg.unwrap_or(self.background);
        // Keep normal text legible even for the light palettes.
        let fill = (0..=8)
            .map(|step| mix(tint, background, 0.24 - f64::from(step) * 0.03))
            .find(|fill| contrast_ratio(self.foreground, *fill) >= 4.5)
            .unwrap_or(background);
        base.fg(self.foreground)
            .bg(fill)
            .add_modifier(Modifier::BOLD | Modifier::UNDERLINED)
    }

    pub fn syntax(self, base: Style, class: crate::syntax::SyntaxClass) -> Style {
        use crate::syntax::SyntaxClass;
        let color = match class {
            SyntaxClass::Plain => return base,
            SyntaxClass::Comment => self.muted,
            SyntaxClass::Keyword => self.accent,
            SyntaxClass::String => self.success,
            SyntaxClass::Number | SyntaxClass::Constant | SyntaxClass::Operator => self.warning,
            SyntaxClass::Type => self.secondary,
            SyntaxClass::Function => self.primary,
            SyntaxClass::Variable | SyntaxClass::Punctuation => self.foreground,
        };
        let color = if base.bg.is_some_and(|bg| contrast_ratio(color, bg) < 4.5) {
            self.foreground
        } else {
            color
        };
        base.fg(color)
    }

    pub fn semantic_command(self) -> Style {
        Style::default()
            .fg(self.primary)
            .add_modifier(Modifier::BOLD)
    }

    pub fn semantic_path(self, resolved: bool) -> Style {
        if resolved {
            Style::default()
                .fg(self.success)
                .add_modifier(Modifier::UNDERLINED)
        } else {
            let modifier = if self.monochrome {
                Modifier::UNDERLINED | Modifier::DIM
            } else {
                Modifier::UNDERLINED
            };
            Style::default().fg(self.warning).add_modifier(modifier)
        }
    }

    /// A heading at `level`, bold: the theme's three heading hues for the first three
    /// levels, then greys that step down toward muted text.
    pub fn semantic_heading(self, level: u8) -> Style {
        let color = match level {
            1 => self.heading_1,
            2 => self.heading_2,
            3 => self.heading_3,
            4 => mix(self.muted, self.foreground, 0.5),
            _ => self.muted,
        };
        Style::default().fg(color).add_modifier(Modifier::BOLD)
    }

    pub fn semantic_list_marker(self) -> Style {
        // Muted, since the accent colour is reserved for speaker labels and chips.
        Style::default().fg(self.muted)
    }

    /// Inline code: bold in the code colour. Monochrome keeps the raised surface, since
    /// its grays alone would not set code apart from bold text.
    pub fn semantic_inline_code(self) -> Style {
        if self.monochrome {
            return Style::default().fg(self.foreground).bg(self.panel);
        }
        Style::default().fg(self.code).add_modifier(Modifier::BOLD)
    }

    pub fn semantic_code_delimiter(self) -> Style {
        Style::default().fg(self.muted).add_modifier(Modifier::DIM)
    }

    pub fn semantic_fence_delimiter(self) -> Style {
        Style::default()
            .fg(self.secondary)
            .add_modifier(Modifier::BOLD)
    }

    pub fn semantic_fence_info(self) -> Style {
        Style::default()
            .fg(self.accent)
            .add_modifier(Modifier::ITALIC)
    }

    pub fn semantic_fence_body(self) -> Style {
        Style::default().fg(self.secondary)
    }

    /// Links read in the secondary colour, apart from the accent that marks
    /// speakers and status, unless a theme's secondary is too close to its body
    /// text to tell apart.
    pub fn link(self) -> Color {
        if contrast_ratio(self.secondary, self.foreground) < 1.5 {
            self.primary
        } else {
            self.secondary
        }
    }

    /// The rail of a finished turn: a border tone, quieter than muted text, so it
    /// frames the turn without reading as content.
    pub fn rail(self) -> Color {
        mix(self.muted, self.background, 0.4)
    }

    /// Quiet text a step below muted, such as section labels, fold markers and
    /// separators.
    pub fn dim(self) -> Color {
        mix(self.muted, self.background, 0.4)
    }

    /// The fill of a diff side with no line opposite the other side's change.
    pub fn diff_filler(self) -> Color {
        mix(self.surface, self.background, 0.5)
    }

    /// The rail of a turn still running: the running colour, quieted toward the
    /// background so it frames the turn without competing with its text.
    pub fn running_rail(self) -> Color {
        mix(self.secondary, self.background, 0.55)
    }

    /// `color` breathing on the composer surface: full strength at frame 0, easing to
    /// 40% strength halfway through a 16-frame cycle (2 s at the activity tick) and back.
    /// Starting at full strength keeps a still frame identical to the solid colour, and a
    /// cycle that divides 256 keeps the `u8` frame counter's wrap seamless.
    pub fn breathing(self, color: Color, frame: u8) -> Color {
        mix(color, self.surface, 0.4 + 0.6 * breath_level(frame))
    }

    /// `style` breathing at `frame` as `motion` allows, over the row's own backdrop.
    ///
    /// Smooth motion blends the foreground toward the backdrop as [`Palette::breathing`]
    /// does. Where colours cannot blend, the foreground stays put and the text steps
    /// between bold at the peak and dim at the trough instead: a blend downconverted to
    /// a smaller palette would flicker between neighbouring entries. A theme that draws
    /// on the terminal's own background has no known backdrop to blend toward, so it
    /// steps too.
    pub fn breathe(self, style: Style, motion: MotionMode, frame: u8) -> Style {
        let level = breath_level(frame);
        let smooth = motion == MotionMode::Smooth && !self.terminal_background;
        match (motion, style.fg) {
            (MotionMode::Still, _) => style,
            (_, Some(color)) if smooth => style.fg(mix(color, self.background, 0.4 + 0.6 * level)),
            _ if level > 2.0 / 3.0 => style.add_modifier(Modifier::BOLD),
            _ if level < 1.0 / 3.0 => style.add_modifier(Modifier::DIM),
            _ => style,
        }
    }

    /// The background of a card row `age` frames after its call finished: the success
    /// colour faded into the transcript background over [`FINISH_GLOW_FRAMES`], or
    /// `None` once it has faded or where colours cannot blend.
    pub fn finish_glow(self, motion: MotionMode, age: u8) -> Option<Color> {
        if motion != MotionMode::Smooth || self.terminal_background || age >= FINISH_GLOW_FRAMES {
            return None;
        }
        let remaining = f64::from(FINISH_GLOW_FRAMES - age) / f64::from(FINISH_GLOW_FRAMES);
        Some(mix(self.success, self.background, 0.22 * remaining))
    }

    pub fn composer(self) -> Style {
        self.base().bg(self.surface)
    }

    pub fn base(self) -> Style {
        let style = Style::default().fg(self.foreground);
        if self.terminal_background {
            style
        } else {
            style.bg(self.background)
        }
    }

    pub fn overlay(self) -> Style {
        self.base().bg(if self.terminal_background {
            self.surface
        } else {
            self.background
        })
    }

    /// The key of a shortcut hint, on a raised surface so it reads apart from its action.
    pub fn keycap(self) -> Style {
        if self.monochrome {
            return Style::default().add_modifier(Modifier::BOLD);
        }
        Style::default().fg(self.foreground).bg(self.panel)
    }

    /// The composer's border in plan mode: the warning colour blended 70% into the
    /// background, so the mode reads from the frame while the word `plan` keeps the
    /// full colour. A lighter blend would sit darker than the normal muted border and
    /// read as dimmed rather than amber. At 16 colours a blend shows as the same grey as
    /// the normal border, so the border is the warning colour itself. Like every token
    /// it stays RGB, and grey in monochrome, until `color_depth.rs` converts the frame.
    pub fn plan_border(self) -> Color {
        if self.depth == ColorDepth::Ansi16 {
            return self.warning;
        }
        mix(self.warning, self.background, 0.7)
    }

    pub fn border(self) -> Style {
        Style::default().fg(self.muted)
    }

    /// A selected picker or list row: bold text on the selected band, or where a band
    /// cannot show, reversed foreground text, so the row reverses as one bar rather than a
    /// block per span colour.
    pub fn selection(self) -> Style {
        let style = Style::default()
            .fg(self.foreground)
            .add_modifier(Modifier::BOLD);
        if self.shows_bands() {
            style.bg(self.selected)
        } else {
            style.add_modifier(Modifier::REVERSED)
        }
    }

    /// `style` on a selected row's band. Text keeps its own colour where that reads at
    /// 4.5:1 on the band and otherwise moves toward the foreground until it does. Where a
    /// band cannot show, the row reverses as one bar in the foreground colour, rather than
    /// each coloured span turning into a block of its own.
    pub fn selected_row(self, style: Style) -> Style {
        if !self.shows_bands() {
            return style.fg(self.foreground).add_modifier(Modifier::REVERSED);
        }
        let style = style.bg(self.selected);
        match style.fg {
            Some(color) => style.fg(self.readable_on(color, self.selected)),
            None => style,
        }
    }

    /// A picker or panel row drawn as selected: [`Palette::selection`] across the line,
    /// with each span's own colour kept as [`Palette::selected_row`] keeps it.
    pub fn selected_line(self, mut line: Line<'static>) -> Line<'static> {
        for span in &mut line.spans {
            span.style = self.selected_row(span.style);
        }
        line.patch_style(self.selection())
    }

    /// `color`, or the nearest blend toward the foreground that reads at 4.5:1 on `fill`.
    fn readable_on(self, color: Color, fill: Color) -> Color {
        (0..=20)
            .map(|step| mix(self.foreground, color, f64::from(step) / 20.0))
            .find(|text| contrast_ratio(*text, fill) >= 4.5)
            .unwrap_or(self.foreground)
    }

    /// Selected text in the composer, in the primary colour so it stands out from the
    /// composer's own surface.
    pub fn text_selection(self) -> Style {
        if self.monochrome {
            Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
        } else {
            Style::default()
                .fg(self.background)
                .bg(self.primary)
                .add_modifier(Modifier::BOLD)
        }
    }

    /// Start with Textual's rounded Rec.709 conversion, then minimally correct
    /// native foreground grays where a rendered pair would fall below WCAG AA.
    /// Labels, diff signs, underlining, and reversed selection remain available.
    fn without_color(self) -> Self {
        fn gray(color: Color) -> Color {
            let Color::Rgb(r, g, b) = color else {
                return color;
            };
            let value = (0.2126 * f64::from(r) + 0.7152 * f64::from(g) + 0.0722 * f64::from(b))
                .round() as u8;
            Color::Rgb(value, value, value)
        }
        let mut palette = Self {
            background: gray(self.background),
            foreground: gray(self.foreground),
            primary: gray(self.primary),
            secondary: gray(self.secondary),
            accent: gray(self.accent),
            success: gray(self.success),
            warning: gray(self.warning),
            error: gray(self.error),
            surface: gray(self.surface),
            panel: gray(self.panel),
            selected: gray(self.selected),
            heading_1: gray(self.heading_1),
            heading_2: gray(self.heading_2),
            heading_3: gray(self.heading_3),
            code: gray(self.code),
            muted: gray(self.muted),
            addition: gray(self.addition),
            addition_background: gray(self.addition_background),
            deletion: gray(self.deletion),
            deletion_background: gray(self.deletion_background),
            monochrome: true,
            terminal_background: self.terminal_background,
            depth: self.depth,
        };
        let backgrounds = [palette.background, palette.surface];
        for foreground in [
            &mut palette.foreground,
            &mut palette.primary,
            &mut palette.secondary,
            &mut palette.accent,
            &mut palette.success,
            &mut palette.error,
            &mut palette.muted,
            &mut palette.heading_1,
            &mut palette.heading_2,
            &mut palette.heading_3,
        ] {
            *foreground = readable_gray(*foreground, &backgrounds);
        }
        palette.warning = readable_gray(palette.warning, &[palette.background, palette.panel]);
        palette.addition = readable_gray(palette.addition, &[palette.addition_background]);
        palette.deletion = readable_gray(palette.deletion, &[palette.deletion_background]);
        palette
    }
}

pub(crate) fn contrast_ratio(foreground: Color, background: Color) -> f64 {
    fn luminance(color: Color) -> f64 {
        let Color::Rgb(r, g, b) = color else {
            return 0.0;
        };
        let channel = |value| {
            let value = f64::from(value) / 255.0;
            if value <= 0.04045 {
                value / 12.92
            } else {
                ((value + 0.055) / 1.055).powf(2.4)
            }
        };
        0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
    }
    let a = luminance(foreground);
    let b = luminance(background);
    (a.max(b) + 0.05) / (a.min(b) + 0.05)
}

/// How live indicators move in a frame. Smooth motion blends colours, which needs
/// truecolor; stepped motion changes only weight, for 256- and 16-colour terminals.
/// Still is for `NO_COLOR`, `WISP_TUI_MOTION=off`, and a terminal in the background.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum MotionMode {
    #[default]
    Still,
    Smooth,
    Stepped,
}

/// How many activity frames a finished card glows for: 0.75 s at the 125 ms tick.
pub const FINISH_GLOW_FRAMES: u8 = 6;

/// Where a breath is at `frame`: 1 at frame 0, easing to 0 halfway through a 16-frame
/// cycle (2 s at the activity tick) and back. A cycle that divides 256 keeps the `u8`
/// frame counter's wrap seamless.
fn breath_level(frame: u8) -> f64 {
    const CYCLE: u8 = 16;
    let phase = f64::from(frame % CYCLE) / f64::from(CYCLE);
    (1.0 + (std::f64::consts::TAU * phase).cos()) / 2.0
}

/// Blend `share` of `color` into `base`. Colours other than RGB come back unchanged.
fn mix(color: Color, base: Color, share: f64) -> Color {
    let (Color::Rgb(r, g, b), Color::Rgb(base_r, base_g, base_b)) = (color, base) else {
        return color;
    };
    let channel = |value: u8, base: u8| {
        (f64::from(base) + (f64::from(value) - f64::from(base)) * share).round() as u8
    };
    Color::Rgb(channel(r, base_r), channel(g, base_g), channel(b, base_b))
}

/// Find the nearest readable gray rather than encoding a palette-specific fix.
fn readable_gray(foreground: Color, backgrounds: &[Color]) -> Color {
    let Color::Rgb(level, _, _) = foreground else {
        return foreground;
    };
    for distance in 0..=255 {
        for value in [level.checked_sub(distance), level.checked_add(distance)]
            .into_iter()
            .flatten()
        {
            let candidate = Color::Rgb(value, value, value);
            if backgrounds
                .iter()
                .all(|background| contrast_ratio(candidate, *background) >= 4.5)
            {
                return candidate;
            }
        }
    }
    foreground
}

impl Default for Palette {
    fn default() -> Self {
        default_theme().colors
    }
}

#[derive(Debug, Deserialize)]
pub(crate) struct Theme {
    pub name: String,
    pub slug: String,
    pub label: String,
    pub description: String,
    pub dark: bool,
    #[serde(default)]
    terminal_background: bool,
    colors: Palette,
}

impl Theme {
    pub fn palette(&self, no_color: bool) -> Palette {
        let mut palette = if no_color {
            self.colors.without_color()
        } else {
            self.colors
        };
        palette.terminal_background = self.terminal_background;
        palette
    }
}

#[derive(Deserialize)]
struct Catalog {
    default: String,
    paper: String,
    command: wisp_protocol::events::CommandDescriptor,
    themes: Vec<Theme>,
}

fn catalog() -> &'static Catalog {
    static CATALOG: OnceLock<Catalog> = OnceLock::new();
    CATALOG.get_or_init(|| {
        serde_json::from_str(include_str!("theme_catalog.json"))
            .expect("checked-in Wisp theme catalog")
    })
}

pub(crate) fn themes() -> &'static [Theme] {
    &catalog().themes
}

pub(crate) fn command() -> &'static wisp_protocol::events::CommandDescriptor {
    &catalog().command
}

pub(crate) fn named(name: &str) -> Option<&'static Theme> {
    themes().iter().find(|theme| theme.name == name)
}

pub(crate) fn resolve(query: &str) -> Option<&'static Theme> {
    themes().iter().find(|theme| {
        theme.slug.eq_ignore_ascii_case(query)
            || theme.name.eq_ignore_ascii_case(query)
            || theme.label.eq_ignore_ascii_case(query)
    })
}

pub(crate) fn default_theme() -> &'static Theme {
    named(&catalog().default).expect("catalog default exists")
}

pub(crate) fn paper_theme() -> &'static Theme {
    named(&catalog().paper).expect("catalog paper theme exists")
}
