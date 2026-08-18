"""Wisp's curated Textual themes and shared presentation variables."""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme

# Diff rows are full-width semantic bands with a stronger token-level tint. The
# foreground must clear WCAG AA against both surfaces; the token band is the
# tighter pairing and therefore governs these hand-tuned values.
_DARK_DIFF_VARIABLES = {
    # Pi's diff hues are retained exactly; supporting bands stay dark enough
    # that source, token, sign, and gutter roles all clear WCAG AA.
    "diff-add-fg": "#b5bd68",
    "diff-add-count-fg": "#b5bd68",
    "diff-add-bg": "#16241e",
    "diff-add-token-bg": "#22432f",
    "diff-add-gutter-bg": "#102018",
    "diff-add-sign-fg": "#b5bd68",
    "diff-del-fg": "#cc6666",
    # Header counts sit on theme panels, including Storm's blue panel; this
    # Pi-red tonal step is light enough to remain readable on every dark panel.
    "diff-del-count-fg": "#e89595",
    "diff-del-bg": "#241a1d",
    "diff-del-token-bg": "#2b181e",
    "diff-del-gutter-bg": "#25151a",
    "diff-del-sign-fg": "#cc6666",
    "diff-line-number-fg": "#a1a9b0",
    "diff-context-fg": "#b2b9c0",
    "diff-hunk-fg": "#9dc3d3",
}
_LIGHT_DIFF_VARIABLES = {
    # Pi green needs a minimal darker tonal step on light surfaces to meet 4.5:1.
    # Pi red already clears the target and remains exact.
    "diff-add-fg": "#4d754d",
    # Header counts sit on each light theme's panel. These darker tonal steps
    # preserve the Pi hues while clearing warm Paper and Dawn surfaces too.
    "diff-add-count-fg": "#426742",
    "diff-add-bg": "#f5faf5",
    "diff-add-token-bg": "#e5f0e5",
    "diff-add-gutter-bg": "#edf5ed",
    "diff-add-sign-fg": "#4d754d",
    "diff-del-fg": "#aa5555",
    "diff-del-count-fg": "#964747",
    "diff-del-bg": "#fff8f8",
    "diff-del-token-bg": "#fff0f0",
    "diff-del-gutter-bg": "#fff4f4",
    "diff-del-sign-fg": "#aa5555",
    "diff-line-number-fg": "#4e5a63",
    "diff-context-fg": "#45515a",
    "diff-hunk-fg": "#315e73",
}


def _theme_variables(diff_variables: dict[str, str], *, transcript_muted: str) -> dict[str, str]:
    """Return Wisp-specific variables shared by theme-reactive transcript CSS."""

    return {**diff_variables, "transcript-muted": transcript_muted}


@dataclass(frozen=True)
class WispThemeSpec:
    """User-facing metadata paired with one registered Textual theme."""

    slug: str
    label: str
    description: str
    theme: Theme

    @property
    def name(self) -> str:
        return self.theme.name

    @property
    def dark(self) -> bool:
        return self.theme.dark


WISP_THEME_DARK = Theme(
    name="wisp",
    # Pi's restrained blue/teal roles on a more deliberate neutral ladder.
    primary="#81a2be",
    secondary="#a7adb3",
    accent="#8abeb7",
    warning="#f0c674",
    error="#d97979",
    success="#c5cd78",
    foreground="#d4d4d4",
    background="#18181e",
    surface="#1e1e24",
    panel="#2d2d38",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#a0a0a8"),
)

WISP_THEME_ORCHID = Theme(
    name="wisp-orchid",
    # Catppuccin Macchiato's mauve family, rearranged onto Wisp's elevation order.
    primary="#c6a0f6",
    secondary="#8aadf4",
    accent="#f5bde6",
    warning="#eed49f",
    error="#ed8796",
    success="#86bd79",
    foreground="#cad3f5",
    background="#181926",
    surface="#1e2030",
    panel="#363a4f",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#a5adcb"),
)

WISP_THEME_EMBER = Theme(
    name="wisp-ember",
    # OpenCode peach and Flexoki warmth, with brighter semantic text roles.
    primary="#fab283",
    secondary="#8bb7d8",
    accent="#f58b6b",
    warning="#e5c07b",
    error="#e06c75",
    success="#8fce9b",
    foreground="#eeeeee",
    background="#100f0f",
    surface="#1c1b1a",
    panel="#282726",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#a7a29c"),
)

WISP_THEME_STORM = Theme(
    name="wisp-storm",
    # Tokyo Night's normal-intensity blue (#7aa2f7) clears 4.5:1 against the
    # background but only 4.28:1 against $panel (e.g. JumpToLatest's badge
    # text), so this uses the scheme's own bright-blue variant instead.
    primary="#8db0ff",
    secondary="#bb9af7",
    accent="#7dcfff",
    warning="#e0af68",
    error="#f7768e",
    success="#9ece6a",
    foreground="#c0caf5",
    background="#1a1b26",
    surface="#24283b",
    panel="#2e3c64",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#a9b1d6"),
)

WISP_THEME_GROVE = Theme(
    name="wisp-grove",
    # Everforest's green-gray atmosphere with a darker panel for diff readability.
    primary="#8fc9bd",
    secondary="#a7c080",
    accent="#e3a8c3",
    warning="#e0bc7f",
    error="#ef8e90",
    success="#b6d18f",
    foreground="#d3c6aa",
    background="#2d353b",
    surface="#333c43",
    panel="#374149",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#aab3aa"),
)

WISP_THEME_WAVE = Theme(
    name="wisp-wave",
    # Kanagawa-inspired ink, crystal blue, violet, and sakura.
    primary="#98b4e6",
    secondary="#b5a0d2",
    accent="#e79ab1",
    warning="#e0b56b",
    error="#f07575",
    success="#a9c982",
    foreground="#dcd7ba",
    background="#1f1f28",
    surface="#2a2a37",
    panel="#363646",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#aaa89c"),
)

WISP_THEME_LIGHT = Theme(
    name="wisp-light",
    # Flexoki's warm paper and ink replace the previous cool near-white palette.
    primary="#205ea6",
    secondary="#5e409d",
    accent="#9f4510",
    warning="#815f00",
    error="#af3029",
    success="#536b09",
    foreground="#100f0f",
    background="#fffcf0",
    surface="#f2f0e5",
    panel="#e6e4d9",
    dark=False,
    variables=_theme_variables(_LIGHT_DIFF_VARIABLES, transcript_muted="#575653"),
)

WISP_THEME_DAWN = Theme(
    name="wisp-dawn",
    # Rosé Pine Dawn's blush neutrals with strengthened semantic contrast.
    primary="#286983",
    secondary="#6f5b80",
    accent="#9d5353",
    warning="#815b00",
    error="#a95570",
    success="#3f6645",
    foreground="#575279",
    background="#faf4ed",
    surface="#fffaf3",
    panel="#f2e9e1",
    dark=False,
    variables=_theme_variables(_LIGHT_DIFF_VARIABLES, transcript_muted="#646072"),
)

WISP_THEME_SPECS = (
    WispThemeSpec("vapor", "Vapor", "Quiet blue and spectral teal", WISP_THEME_DARK),
    WispThemeSpec("orchid", "Orchid", "Mauve, periwinkle, and soft pink", WISP_THEME_ORCHID),
    WispThemeSpec("ember", "Ember", "Peach and coral on warm charcoal", WISP_THEME_EMBER),
    WispThemeSpec("storm", "Storm", "Tokyo Night blues and violet", WISP_THEME_STORM),
    WispThemeSpec("grove", "Grove", "Forest green and warm parchment", WISP_THEME_GROVE),
    WispThemeSpec("wave", "Wave", "Ink blue, violet, and sakura", WISP_THEME_WAVE),
    WispThemeSpec("paper", "Paper", "Warm paper and crisp blue ink", WISP_THEME_LIGHT),
    WispThemeSpec("dawn", "Dawn", "Blush canvas and muted berry", WISP_THEME_DAWN),
)
WISP_THEMES = tuple(spec.theme for spec in WISP_THEME_SPECS)
WISP_THEME_NAMES = frozenset(spec.name for spec in WISP_THEME_SPECS)
WISP_DARK_THEME_NAMES = frozenset(spec.name for spec in WISP_THEME_SPECS if spec.dark)
WISP_THEME_BY_NAME = {spec.name: spec for spec in WISP_THEME_SPECS}
WISP_THEME_BY_SLUG = {spec.slug: spec for spec in WISP_THEME_SPECS}
DEFAULT_THEME_NAME = WISP_THEME_DARK.name
PAPER_THEME_NAME = WISP_THEME_LIGHT.name


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance of an sRGB color (0-255 per channel)."""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """Return the WCAG contrast ratio between two ``#rrggbb`` colors."""

    def to_rgb(hex_color: str) -> tuple[int, int, int]:
        h = hex_color.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    l_a = relative_luminance(to_rgb(hex_a))
    l_b = relative_luminance(to_rgb(hex_b))
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


__all__ = [
    "DEFAULT_THEME_NAME",
    "PAPER_THEME_NAME",
    "WISP_DARK_THEME_NAMES",
    "WISP_THEME_BY_NAME",
    "WISP_THEME_BY_SLUG",
    "WISP_THEME_DAWN",
    "WISP_THEME_DARK",
    "WISP_THEME_EMBER",
    "WISP_THEME_GROVE",
    "WISP_THEME_LIGHT",
    "WISP_THEME_NAMES",
    "WISP_THEME_ORCHID",
    "WISP_THEME_SPECS",
    "WISP_THEME_STORM",
    "WISP_THEME_WAVE",
    "WISP_THEMES",
    "WispThemeSpec",
    "contrast_ratio",
    "relative_luminance",
]
