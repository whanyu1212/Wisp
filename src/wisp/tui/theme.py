"""Wisp's Textual theme and the transcript role→style bridge.

Textual themes style *widgets* through CSS `$variables`. The transcript, however,
is a `RichLog(markup=True)` rendered via Rich markup, which does not understand
Textual `$variables` — so transcript line colors cannot use `$primary` and must
be resolved to concrete styles at write time. `role_styles()` builds that map
from the active `Theme`, so transcript lines track the theme (for lines written
after a theme switch; `RichLog` cannot restyle already-written lines).
"""

from __future__ import annotations

from textual.theme import Theme

# A cool, vaporous identity: a muted teal-cyan accent over cool-biased neutrals,
# with semantic colors kept clearly distinct from the accent hue.
WISP_THEME_DARK = Theme(
    name="wisp",
    primary="#4aa3c7",  # cool blue — structural accent (borders, user)
    secondary="#7c8b99",
    accent="#3fb8b8",  # vapor teal — the one bold hue
    warning="#d3a25a",
    error="#d16a7c",
    success="#5cc9a7",
    foreground="#dfe6ec",
    background="#0e1216",
    surface="#151b21",
    panel="#1b232b",
    dark=True,
)

WISP_THEME_LIGHT = Theme(
    name="wisp-light",
    primary="#2f8fb3",
    secondary="#55636d",
    accent="#2f8f8f",
    warning="#a9701c",
    error="#b64a5e",
    success="#2f9d78",
    foreground="#12171c",
    background="#fbfcfd",
    surface="#ffffff",
    panel="#eef3f5",
    dark=False,
)

WISP_THEMES = (WISP_THEME_DARK, WISP_THEME_LIGHT)


# Each transcript role maps to a Theme attribute (its base color) plus whether
# the label reads bold. Kept as attribute names, not literal colors, so a theme
# switch re-derives the whole palette from role_styles().
_ROLE_COLOR_ATTR: dict[str, str] = {
    "notice": "accent",
    "error": "error",
    "dim": "secondary",
    "user": "primary",
    "assistant": "success",
    "session": "secondary",
    "tool": "accent",
    "approved": "success",
    "denied": "error",
    "banner": "accent",
}
_BOLD_ROLES = frozenset({"user", "assistant"})
_DIM_ROLES = frozenset({"dim", "session"})


def role_styles(theme: Theme) -> dict[str, str]:
    """Resolve a role→Rich-style map from the active theme.

    Returns Rich markup style strings (e.g. ``"bold #5cc9a7"``) suitable for
    `RichLog` markup. Re-call after a theme change to pick up the new palette.
    """

    styles: dict[str, str] = {}
    for role, attr in _ROLE_COLOR_ATTR.items():
        color = getattr(theme, attr)
        parts = [color]
        if role in _BOLD_ROLES:
            parts.insert(0, "bold")
        if role in _DIM_ROLES:
            parts.append("dim")
        styles[role] = " ".join(parts)
    return styles
