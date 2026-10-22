//! Presentation-only palettes, generated from the existing Python theme catalog.

use ratatui::style::{Color, Modifier, Style};
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
}

impl Palette {
    pub fn is_monochrome(self) -> bool {
        self.monochrome
    }

    pub fn user_text(self) -> Style {
        self.base().bg(self.panel)
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

    /// A tool-specific label on a tinted surface. The wash stays visible over a glass
    /// theme's terminal background. Where the accent is too faint on its wash, the text
    /// moves toward the foreground until it reads; monochrome palettes reverse the
    /// row's colours instead.
    pub fn chip(self, label: &str) -> Style {
        const TINT: f64 = 0.18;
        if self.monochrome {
            return Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD);
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
        let fill = mix(accent, self.background, TINT);
        let text = (0..=20)
            .map(|step| mix(self.foreground, accent, f64::from(step) / 20.0))
            .find(|text| contrast_ratio(*text, fill) >= 4.5)
            .unwrap_or(self.foreground);
        Style::default()
            .fg(text)
            .bg(fill)
            .add_modifier(Modifier::BOLD)
    }

    /// Shade the existing padding cells to give a compact chip a shallow edge.
    pub fn chip_edge(self, chip: Style, leading: bool) -> Style {
        let (Some(text), Some(fill)) = (chip.fg, chip.bg) else {
            return chip;
        };
        if self.monochrome {
            return chip;
        }
        chip.bg(if leading {
            mix(text, fill, 0.16)
        } else {
            mix(fill, self.background, 0.35)
        })
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

    pub fn semantic_heading(self, level: u8) -> Style {
        Style::default()
            .fg(if level <= 2 {
                self.primary
            } else {
                self.secondary
            })
            .add_modifier(Modifier::BOLD)
    }

    pub fn semantic_list_marker(self) -> Style {
        // Muted, since the accent colour is reserved for speaker labels and chips.
        Style::default().fg(self.muted)
    }

    pub fn semantic_inline_code(self) -> Style {
        // A neutral surface keeps the accent colours for labels and status.
        Style::default().fg(self.foreground).bg(self.panel)
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

    pub fn border(self) -> Style {
        Style::default().fg(self.muted)
    }

    pub fn selection(self) -> Style {
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
            muted: gray(self.muted),
            addition: gray(self.addition),
            addition_background: gray(self.addition_background),
            deletion: gray(self.deletion),
            deletion_background: gray(self.deletion_background),
            monochrome: true,
            terminal_background: self.terminal_background,
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
