"""Wisp's Textual theme and the transcript role→style bridge.

Textual themes style *widgets* through CSS `$variables`. The transcript, however,
is a `VerticalScroll` of per-message `LineMessage`/`StreamMessage` widgets, each
escaped and styled via Rich markup at mount time — those widgets don't understand
Textual `$variables`, so transcript line colors cannot use `$primary` and must
be resolved to concrete styles at write time. `role_styles()` builds that map
from the active `Theme`, so transcript lines track the theme (for lines written
after a theme switch; already-mounted lines keep whatever style they were given).
"""

from __future__ import annotations

from textual.theme import Theme

# Diff rows are painted as full-width tinted bands (the row background) with a
# stronger tint on the specific changed tokens, matching how conventional diff
# viewers separate "this line changed" from "this is what changed in it".
#
# These are deliberately hand-picked rather than derived from `success`/`error`
# via Textual's automatic `$success-muted`/`$text-success` variables. Those are
# tuned for isolated UI chrome and label text; applied to a dense block of diff
# rows they read far too saturated, which is why an earlier auto-derived attempt
# was reverted. Fixing the values here keeps the tint under direct control.
#
# Every foreground clears WCAG AA (>= 4.5:1) against BOTH its row band and its
# stronger token band — the token band is the tighter pairing, so it is the one
# that governs. Row bands sit at ~1.1-1.2:1 against the app background: visible
# as a band without reading as a separate raised surface. Re-verify with
# `contrast_ratio` before changing any value; see
# `test_diff_theme_colors_clear_contrast_thresholds`.
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


_DARK_WARNING = "#d3a25a"
_LIGHT_WARNING = "#9c671a"

# A cool, vaporous identity: a muted teal-cyan accent over cool-biased neutrals,
# with semantic colors kept clearly distinct from the accent hue.
WISP_THEME_DARK = Theme(
    name="wisp",
    primary="#4aa3c7",  # cool blue — structural accent (borders, user)
    secondary="#7c8b99",
    accent="#3fb8b8",  # vapor teal — the one bold hue
    warning=_DARK_WARNING,
    error="#d16a7c",
    success="#5cc9a7",
    foreground="#dfe6ec",
    background="#0e1216",
    surface="#151b21",
    panel="#1b232b",
    dark=True,
    variables=_DARK_DIFF_VARIABLES,
)

WISP_THEME_LIGHT = Theme(
    name="wisp-light",
    primary="#277795",
    secondary="#55636d",
    accent="#2e7676",
    warning=_LIGHT_WARNING,
    error="#b64a5e",
    success="#2b8164",
    foreground="#12171c",
    background="#fbfcfd",
    surface="#ffffff",
    panel="#eef3f5",
    dark=False,
    variables=_LIGHT_DIFF_VARIABLES,
)

WISP_THEMES = (WISP_THEME_DARK, WISP_THEME_LIGHT)
# Textual also registers ~20 built-in themes on every app. Wisp's own names are
# tracked separately so a persisted or user-supplied theme can be validated
# against the palettes whose role colors and diff variables actually exist.
WISP_THEME_NAMES = frozenset(theme.name for theme in WISP_THEMES)


# Muted-text color for the `dim`/`session` roles (issue #76). These roles used
# to take their base `secondary` color and append Rich's literal `"dim"`
# attribute — but Wisp never enables Textual's `TEXTUAL_FILTERS=dim`, so that
# attribute is never converted to a deterministic blended color by Textual's
# `DimFilter`. It survives as a raw ANSI SGR-2 ("faint") escape, which most
# terminals render inconsistently — some barely dim it at all, defeating the
# point. These baked hex values replace the `dim` attribute outright: each is
# a `secondary`-hue-tinted neutral gray landing near the same contrast tier
# Textual's own `text-muted` CSS variable (`auto 60%`) already achieves for
# other muted UI chrome (~5.5-7:1 against Wisp's real backgrounds), so the
# transcript's muted text and the rest of the app's muted chrome read as one
# consistent, WCAG-4.5:1-clearing "muted" tier instead of two disagreeing
# ones. Do not reintroduce the `dim` attribute here without re-verifying
# contrast — see `test_role_styles_no_longer_uses_bare_dim_attribute_for_muted_roles`.
MUTED_DARK = "#92989e"
MUTED_LIGHT = "#5e6367"

# Each transcript role maps to a Theme attribute (its base color) plus whether
# the label reads bold. Kept as attribute names, not literal colors, so a theme
# switch re-derives the whole palette from role_styles(). `dim`/`session` are
# the exception — they resolve to the baked MUTED_DARK/MUTED_LIGHT constants
# above instead of a Theme attribute, since neither theme defines a dedicated
# "muted" color.
_ROLE_COLOR_ATTR: dict[str, str] = {
    "notice": "warning",
    "error": "error",
    "user": "primary",
    "assistant": "success",
    "tool": "accent",
    "approved": "success",
    "denied": "warning",
}
_BOLD_ROLES = frozenset({"user", "assistant"})
_MUTED_ROLES = frozenset({"dim", "session"})


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance of an sRGB color (0-255 per channel)."""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colors, always >= 1.0."""

    def to_rgb(hex_color: str) -> tuple[int, int, int]:
        h = hex_color.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    l_a = relative_luminance(to_rgb(hex_a))
    l_b = relative_luminance(to_rgb(hex_b))
    lighter, darker = max(l_a, l_b), min(l_a, l_b)
    return (lighter + 0.05) / (darker + 0.05)


def role_styles(theme: Theme) -> dict[str, str]:
    """Resolve a role→Rich-style map from the active theme.

    Returns Rich markup style strings (e.g. ``"bold #5cc9a7"``) suitable for
    the transcript's Rich-markup-styled widgets. Re-call after a theme change
    to pick up the new palette.
    """

    muted = MUTED_DARK if theme.dark else MUTED_LIGHT
    styles: dict[str, str] = {}
    for role, attr in _ROLE_COLOR_ATTR.items():
        color = getattr(theme, attr)
        parts = [color]
        if role in _BOLD_ROLES:
            parts.insert(0, "bold")
        styles[role] = " ".join(parts)
    for role in _MUTED_ROLES:
        styles[role] = muted
    return styles
