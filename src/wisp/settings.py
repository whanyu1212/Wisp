"""Layered settings resolution for Wisp.

Wisp historically read configuration from environment variables / ``.env`` only.
This module adds a persistent settings layer on top of that: a JSON settings file
in the user's home directory (global) and one in the project directory (project),
resolved with a clear precedence chain.

Precedence, highest to lowest::

    explicit CLI argument
      > environment variable
      > project ./.wisp/settings.json
      > user ~/.wisp/settings.json
      > built-in default

The resolver only fills in the *file* layers. Explicit arguments and environment
variables are applied by :meth:`wisp.config.WispConfig.from_env`, which calls this
module. A settings file is always optional: a missing file contributes nothing, and
a malformed file is skipped with a warning rather than crashing startup — a broken
project config should never make Wisp unusable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

GLOBAL_SETTINGS_PATH = Path("~/.wisp/settings.json")
PROJECT_SETTINGS_DIRNAME = ".wisp"
PROJECT_SETTINGS_FILENAME = "settings.json"

# Default glob patterns whose contents tools refuse to read. These guard secrets
# from being pulled into model context by an over-eager read/grep. Bare patterns
# match on basename at any depth; slash-bearing patterns match as a path suffix.
# Tune via the ``protected_paths`` setting (an empty list disables the guard).
#
# TODO(tuning): This default list is a security-vs-friction judgment call worth a
# maintainer's eye. It deliberately does NOT use a broad ``.env.*`` glob, because
# that would also block committed placeholder files (``.env.example``,
# ``.env.sample``, ``.env.template``) that legitimately belong in model context.
# Instead it enumerates the ``.env`` variants that typically hold real secrets.
# Add/remove entries as real-world usage warrants.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".env",
    ".env.local",
    ".env.*.local",
    ".env.dev",
    ".env.development",
    ".env.prod",
    ".env.production",
    ".env.staging",
    ".env.qa",
    ".env.test",
    ".env.secret",
    ".env.secrets",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "id_dsa",
    "id_ecdsa",
    "credentials.json",
    ".netrc",
    ".pgpass",
    ".wisp/auth.json",
)


class WispSettings(BaseModel):
    """Schema for a Wisp ``settings.json`` file.

    Every field is optional: a settings file only overrides the keys it names.
    Unknown keys are ignored so newer settings files stay loadable by older Wisp
    builds (``extra="ignore"``).
    """

    model_config = ConfigDict(extra="ignore")

    provider: str | None = None
    model: str | None = None
    session_dir: str | None = None
    auth_path: str | None = None
    protected_paths: list[str] | None = None


class ResolvedSettings(BaseModel):
    """Merged result of the file layers, before env/CLI overrides are applied.

    Values here come purely from settings files (project layered over user). They
    are the second-lowest precedence tier; :class:`WispSettings` fields left unset
    across every file stay ``None`` so higher tiers (env, CLI) or built-in defaults
    win.
    """

    model_config = ConfigDict(frozen=True)

    provider: str | None = None
    model: str | None = None
    session_dir: str | None = None
    auth_path: str | None = None
    protected_paths: tuple[str, ...] | None = None


def resolve_settings(
    *,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
) -> ResolvedSettings:
    """Resolve the file-based settings layers into a single merged view.

    Reads the user (global) settings file first, then overlays the project
    settings file so project keys win. ``project_dir`` defaults to the current
    working directory and ``home_dir`` to the user's home — both are parameters so
    tests can point them at a ``tmp_path``.
    """

    home = home_dir if home_dir is not None else Path.home()
    project = project_dir if project_dir is not None else Path.cwd()

    user_file = (home / ".wisp" / PROJECT_SETTINGS_FILENAME).expanduser()
    project_file = project / PROJECT_SETTINGS_DIRNAME / PROJECT_SETTINGS_FILENAME

    user_settings = _load_settings_file(user_file)
    project_settings = _load_settings_file(project_file)

    # Project layer wins over user layer, key by key. ``_coalesce`` keeps the first
    # non-None value, so a key absent from the project file falls through to the
    # user file, and a key absent from both stays None.
    #
    # ``protected_paths`` is a SECURITY policy and is deliberately taken from the
    # USER layer only. A project ``.wisp/settings.json`` is project-controlled, so
    # honoring its ``protected_paths`` would let an untrusted repo ship
    # ``{"protected_paths": []}`` to disable the secret-file guard and expose its own
    # ``.env`` to the model. The project may not weaken (or set) this policy.
    return ResolvedSettings(
        provider=_coalesce(project_settings.provider, user_settings.provider),
        model=_coalesce(project_settings.model, user_settings.model),
        session_dir=_coalesce(project_settings.session_dir, user_settings.session_dir),
        auth_path=_coalesce(project_settings.auth_path, user_settings.auth_path),
        protected_paths=_coalesce_paths(user_settings.protected_paths),
    )


def _load_settings_file(path: Path) -> WispSettings:
    """Load one settings file, returning empty settings on any problem.

    A missing file is normal (returns empty settings silently). A file that exists
    but cannot be parsed or validated is a user error worth surfacing, so we warn on
    stderr and continue with empty settings rather than aborting startup.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return WispSettings()
    except OSError as exc:
        _warn(f"could not read settings file {path}: {exc}")
        return WispSettings()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(f"ignoring malformed settings file {path}: {exc}")
        return WispSettings()

    if not isinstance(data, dict):
        _warn(f"ignoring settings file {path}: expected a JSON object")
        return WispSettings()

    try:
        return WispSettings.model_validate(data)
    except ValidationError as exc:
        _warn(f"ignoring invalid settings in {path}: {exc}")
        return WispSettings()


def _coalesce(*values: str | None) -> str | None:
    """Return the first value that is set and non-empty after stripping."""

    for value in values:
        if value and value.strip():
            return value.strip()
    return None


def _coalesce_paths(*values: list[str] | None) -> tuple[str, ...] | None:
    """Return the first list-valued setting that is present (empty list counts).

    Unlike scalar settings, an explicitly empty ``protected_paths: []`` is a
    meaningful choice — "protect nothing" — so it is *not* treated as unset. Only
    ``None`` (key absent) falls through to the next layer.
    """

    for value in values:
        if value is not None:
            return tuple(value)
    return None


def _warn(message: str) -> None:
    print(f"wisp: warning: {message}", file=sys.stderr)
