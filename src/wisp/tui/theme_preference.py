"""Persisted TUI theme choice.

Theme is a *presentation* concern owned entirely by the Textual client: the RPC
subprocess renders nothing, so this deliberately does not live in ``WispSettings``
and never crosses the subprocess boundary (see ``tui/launch.py``). It is stored
beside the other user-local client state in ``~/.wisp/`` rather than in the
agent's settings file, which project directories can influence.

Reading is forgiving and writing is conservative. Any unusable file — missing,
permission-denied, not valid UTF-8, not valid JSON, or valid JSON of the wrong
shape — falls back to the default theme on load rather than raising, matching how
settings files are treated. This matters more than it looks: loading happens
during ``on_mount``, so an escaping exception here does not degrade the theme, it
stops the TUI from starting.

Saving, by contrast, would rather not persist at all than damage what is already
on disk. It refuses to overwrite a document it could not read back, and stages
writes through a temporary file so a failure cannot leave a truncated one behind.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

_PREFERENCE_FILENAME = "tui.json"
_THEME_KEY = "theme"


class _Unreadable(Exception):
    """The file exists but its contents could not be recovered."""


def theme_preference_path(*, home_dir: Path | None = None) -> Path:
    """Return the user-local file holding TUI presentation preferences."""

    home = Path.home() if home_dir is None else home_dir
    return home.expanduser() / ".wisp" / _PREFERENCE_FILENAME


def _read_document(path: Path) -> str | None:
    """Return the file's text, ``None`` when absent, or raise :class:`_Unreadable`.

    Reading can fail at two layers, and only one of them is an ``OSError``:
    the filesystem may refuse the read, or the bytes may not be valid UTF-8
    (``UnicodeDecodeError`` subclasses ``ValueError``). A partially written or
    corrupted file produces the latter, so handling only ``OSError`` left a
    decode error escaping to the caller — and since loading happens during
    ``on_mount``, that stopped the TUI from starting at all.

    The three outcomes stay distinct because callers need them to be: absent
    means "no preference yet", unreadable means "do not overwrite what you
    cannot see", and text means "parse it".
    """

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise _Unreadable(str(error)) from error


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

    # Absent and unreadable are the same answer here — no usable preference —
    # unlike `save`, where the two call for opposite behavior.
    try:
        raw = _read_document(theme_preference_path(home_dir=home_dir))
    except _Unreadable:
        return None
    if raw is None:
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
    home for further client-side preferences. A failure is reported rather than
    raised — failing to remember a theme must not interrupt the session that just
    switched it — but never at the cost of the existing document: this returns
    ``False`` without touching the file rather than replacing content it could
    not first read back.
    """

    path = theme_preference_path(home_dir=home_dir)
    payload: dict[str, object] = {}
    try:
        # An absent file genuinely means "no document yet". Anything unreadable
        # leaves the real contents unknown, and overwriting then would silently
        # drop unrelated preferences.
        raw = _read_document(path)
    except _Unreadable:
        return False

    if raw is not None:
        try:
            existing = json.loads(raw)
        except json.JSONDecodeError:
            # Unparseable content carries nothing worth preserving, so replacing
            # it is a repair rather than a loss.
            existing = None
        if isinstance(existing, Mapping):
            payload.update(existing)
    payload[_THEME_KEY] = theme

    # Write to a sibling temp file and rename over the destination. `write_text`
    # truncates before writing, so a failure partway through would leave the file
    # empty or half-written — losing the previous theme and every unrelated key,
    # while still reporting failure. `os.replace` is atomic within a directory,
    # so the destination either keeps its old contents or gains the complete new
    # document.
    document = json.dumps(payload, indent=2) + "\n"
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(document)
        os.replace(temporary, path)
    except OSError:
        # Best-effort cleanup; a stray temp file is preferable to raising from a
        # path whose whole contract is that it does not interrupt the session.
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
        return False
    return True


__all__ = [
    "load_theme_preference",
    "save_theme_preference",
    "theme_preference_path",
]
