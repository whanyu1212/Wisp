"""Wisp's curated Textual themes and shared presentation variables."""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme

# Diff rows are full-width semantic bands with a stronger token-level tint. The
# foreground must clear WCAG AA against both surfaces; the token band is the
# tighter pairing and therefore governs these hand-tuned values.
_DARK_DIFF_VARIABLES = {
    "diff-add-fg": "#8fbfa8",
    "diff-add-bg": "#16241e",
    "diff-add-token-bg": "#22432f",
    "diff-del-fg": "#cf95a1",
    "diff-del-bg": "#241a1d",
    "diff-del-token-bg": "#4a2630",
}
_LIGHT_DIFF_VARIABLES = {
    "diff-add-fg": "#265c48",
    "diff-add-bg": "#eaf5ee",
    "diff-add-token-bg": "#c3e4d0",
    "diff-del-fg": "#8a3548",
    "diff-del-bg": "#fbecef",
    "diff-del-token-bg": "#f4ccd4",
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
    primary="#4aa3c7",
    secondary="#7c8b99",
    accent="#3fb8b8",
    warning="#d3a25a",
    error="#d16a7c",
    success="#5cc9a7",
    foreground="#dfe6ec",
    background="#0e1216",
    surface="#151b21",
    panel="#1b232b",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#92989e"),
)

WISP_THEME_ORCHID = Theme(
    name="wisp-orchid",
    primary="#9b8af2",
    secondary="#9a93a8",
    accent="#c184f4",
    warning="#d3a25a",
    error="#d16a7c",
    success="#5cc9a7",
    foreground="#e8e5ef",
    background="#100f16",
    surface="#181721",
    panel="#232031",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#9c98a8"),
)

WISP_THEME_EMBER = Theme(
    name="wisp-ember",
    primary="#d69a62",
    secondary="#a29389",
    accent="#e4775d",
    warning="#e0ad5e",
    error="#dc7482",
    success="#68c7a5",
    foreground="#eee7e2",
    background="#15110f",
    surface="#1e1815",
    panel="#2a211c",
    dark=True,
    variables=_theme_variables(_DARK_DIFF_VARIABLES, transcript_muted="#a29a95"),
)

WISP_THEME_LIGHT = Theme(
    name="wisp-light",
    primary="#277795",
    secondary="#55636d",
    accent="#2e7676",
    warning="#9c671a",
    error="#b64a5e",
    success="#2b8164",
    foreground="#12171c",
    background="#fbfcfd",
    surface="#ffffff",
    panel="#eef3f5",
    dark=False,
    variables=_theme_variables(_LIGHT_DIFF_VARIABLES, transcript_muted="#5e6367"),
)

WISP_THEME_SPECS = (
    WispThemeSpec("vapor", "Vapor", "Cool cyan and vapor teal", WISP_THEME_DARK),
    WispThemeSpec("orchid", "Orchid", "Indigo and muted violet", WISP_THEME_ORCHID),
    WispThemeSpec("ember", "Ember", "Warm amber and restrained coral", WISP_THEME_EMBER),
    WispThemeSpec("paper", "Paper", "Clean light neutrals", WISP_THEME_LIGHT),
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
    "WISP_THEME_DARK",
    "WISP_THEME_EMBER",
    "WISP_THEME_LIGHT",
    "WISP_THEME_NAMES",
    "WISP_THEME_ORCHID",
    "WISP_THEME_SPECS",
    "WISP_THEMES",
    "WispThemeSpec",
    "contrast_ratio",
    "relative_luminance",
]
