"""Persisted TUI theme choice.

Theme is a *presentation* concern owned entirely by the Textual client: the RPC
subprocess renders nothing, so this deliberately does not live in ``WispSettings``
and never crosses the subprocess boundary (see ``tui/launch.py``). It is stored
beside the other user-local client state in ``~/.wisp/`` rather than in the
agent's settings file, which project directories can influence.

A malformed or unreadable file is ignored rather than fatal, matching how
settings files are treated: an unusable preference falls back to the default
theme instead of preventing the TUI from starting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

_PREFERENCE_FILENAME = "tui.json"
_THEME_KEY = "theme"


def theme_preference_path(*, home_dir: Path | None = None) -> Path:
    """Return the user-local file holding TUI presentation preferences."""

    home = Path.home() if home_dir is None else home_dir
    return home.expanduser() / ".wisp" / _PREFERENCE_FILENAME


def load_theme_preference(
    *,
    home_dir: Path | None = None,
    valid_themes: frozenset[str] | None = None,
) -> str | None:
    """Return the persisted theme name, or ``None`` when unset or unusable.

    ``valid_themes`` rejects a name that no longer exists — a theme removed or
    renamed between releases must not leave the TUI trying to select a theme
    Textual cannot resolve.
    """

    try:
        raw = theme_preference_path(home_dir=home_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None

    theme = payload.get(_THEME_KEY)
    if not isinstance(theme, str) or not theme:
        return None
    if valid_themes is not None and theme not in valid_themes:
        return None
    return theme


def save_theme_preference(theme: str, *, home_dir: Path | None = None) -> bool:
    """Persist ``theme``, returning whether it was written.

    Preserves any unrelated keys already in the file so this stays usable as the
    home for further client-side preferences. A write failure is reported rather
    than raised: failing to remember a theme must not interrupt the session that
    just switched it.
    """

    path = theme_preference_path(home_dir=home_dir)
    payload: dict[str, object] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        existing = None
    except json.JSONDecodeError:
        existing = None
    if isinstance(existing, Mapping):
        payload.update(existing)
    payload[_THEME_KEY] = theme

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


__all__ = [
    "load_theme_preference",
    "save_theme_preference",
    "theme_preference_path",
]
