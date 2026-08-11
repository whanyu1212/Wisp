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
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from wisp.mcp.config import MAX_MCP_SERVERS, McpServerConfig
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.retry import RetrySettings
from wisp.validation import redact_validation_error_inputs

GLOBAL_SETTINGS_PATH = Path("~/.wisp/settings.json")
PROJECT_SETTINGS_DIRNAME = ".wisp"
PROJECT_SETTINGS_FILENAME = "settings.json"
_USER_ONLY_SETTINGS_FIELDS = frozenset(
    {
        "protected_paths",
        "retry",
        "effort",
        "context_reserve_tokens",
        "auto_compaction_enabled",
        "update_check_enabled",
        "mcp_servers",
        "openai_compatible",
    }
)

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
    ".wisp/settings.json",
)


class WispSettings(BaseModel):
    """Schema for a Wisp ``settings.json`` file.

    Every field is optional: a settings file only overrides the keys it names.
    Unknown keys are ignored so newer settings files stay loadable by older Wisp
    builds (``extra="ignore"``).
    """

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    provider: str | None = None
    model: str | None = None
    session_dir: str | None = None
    auth_path: str | None = None
    protected_paths: list[str] | None = None
    retry: RetrySettings | None = None
    effort: str | None = None
    context_reserve_tokens: int | None = Field(default=None, ge=0)
    auto_compaction_enabled: bool | None = None
    update_check_enabled: bool | None = None
    mcp_servers: tuple[McpServerConfig, ...] | None = Field(
        default=None, max_length=MAX_MCP_SERVERS, repr=False
    )
    openai_compatible: OpenAICompatibleSettings | None = None

    @model_validator(mode="wrap")
    @classmethod
    def _redact_mcp_validation_inputs(
        cls,
        value: Any,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(value)
        except ValidationError as exc:
            redacted = redact_validation_error_inputs(exc, field="mcp_servers")
            if redacted is exc:
                raise
            raise redacted from None

    @field_validator(
        "mcp_servers",
        mode="before",
        json_schema_input_type=dict[str, dict[str, Any]] | None,
    )
    @classmethod
    def _parse_mcp_servers(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("mcp_servers must be a JSON object")
        servers: list[dict[str, object]] = []
        for name, raw_server in value.items():
            if not isinstance(raw_server, Mapping):
                raise ValueError("each MCP server must be a JSON object")
            server = dict(raw_server)
            server["name"] = name
            servers.append(server)
        return servers

    @field_serializer("mcp_servers", when_used="json")
    def _serialize_mcp_servers(
        self,
        value: tuple[McpServerConfig, ...] | None,
    ) -> dict[str, dict[str, Any]] | None:
        if value is None:
            return None
        return {server.name: server.model_dump(mode="json", exclude={"name"}) for server in value}

    @field_validator("mcp_servers")
    @classmethod
    def _sort_mcp_servers(
        cls, value: tuple[McpServerConfig, ...] | None
    ) -> tuple[McpServerConfig, ...] | None:
        if value is None:
            return None
        return tuple(sorted(value, key=lambda server: server.name))


class ResolvedSettings(BaseModel):
    """Merged result of the file layers, before env/CLI overrides are applied.

    Values here come purely from settings files (project layered over user). They
    are the second-lowest precedence tier; :class:`WispSettings` fields left unset
    across every file stay ``None`` so higher tiers (env, CLI) or built-in defaults
    win.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    provider: str | None = None
    model: str | None = None
    session_dir: str | None = None
    auth_path: str | None = None
    protected_paths: tuple[str, ...] | None = None
    retry: RetrySettings | None = None
    effort: str | None = None
    context_reserve_tokens: int | None = Field(default=None, ge=0)
    auto_compaction_enabled: bool | None = None
    update_check_enabled: bool | None = None
    mcp_servers: tuple[McpServerConfig, ...] | None = Field(default=None, repr=False)
    openai_compatible: OpenAICompatibleSettings | None = None
    # Provenance used only while applying higher-precedence provider overrides.
    # Excluded from serialization because these are resolver details, not settings.
    user_provider: str | None = Field(default=None, exclude=True)
    model_from_user: bool = Field(default=False, exclude=True)


def resolve_settings(
    *,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
    trust_project: bool = False,
) -> ResolvedSettings:
    """Resolve the file-based settings layers into a single merged view.

    Reads the user (global) settings file first, then overlays the project
    settings file so project keys win. ``project_dir`` defaults to the current
    working directory and ``home_dir`` to the user's home — both are parameters so
    tests can point them at a ``tmp_path``.

    ``trust_project`` gates the project layer on the project-trust decision. A
    project ``.wisp/settings.json`` is project-controlled configuration: it can set
    ``provider``, ``model``, ``session_dir``, and ``auth_path``, redirecting Wisp's
    credential file or overriding user defaults. Applying it from an untrusted repo
    is the same class of bypass as loading an untrusted ``.env``, so when
    ``trust_project`` is ``False`` the project file is ignored entirely and only the
    user layer contributes. Trust is decided *before* this is called (from the
    global store / real-env ``WISP_TRUST``), never from a project-controlled source.

    It defaults to ``False`` (fail-closed): a caller that has not resolved trust must
    not accidentally ingest project settings. Production config construction passes an
    explicit decision via :meth:`wisp.config.WispConfig.from_env`.
    """

    home = home_dir if home_dir is not None else Path.home()
    project = project_dir if project_dir is not None else Path.cwd()

    user_file = (home / ".wisp" / PROJECT_SETTINGS_FILENAME).expanduser()
    user_settings = _load_settings_file(user_file, secure_permissions=True)

    # An untrusted project contributes nothing: skip its settings file entirely so a
    # cloned repo cannot inject provider/model/session_dir/auth_path. This is
    # fail-closed — an undecided project is treated as untrusted here.
    project_settings = (
        _load_settings_file(
            project / PROJECT_SETTINGS_DIRNAME / PROJECT_SETTINGS_FILENAME,
            ignored_fields=_USER_ONLY_SETTINGS_FIELDS,
        )
        if trust_project
        else WispSettings()
    )

    # Project layer wins over user layer key by key. Keep provenance for a model
    # inherited from the user layer so WispConfig can couple it to the user provider
    # only after explicit/environment provider overrides have also been applied.
    # Discarding it here would prevent a later WISP_PROVIDER override from restoring
    # the saved provider and its model.
    project_provider = _coalesce(project_settings.provider)
    user_provider = _coalesce(user_settings.provider)
    project_model = _coalesce(project_settings.model)
    user_model = _coalesce(user_settings.model)
    #
    # ``protected_paths`` is a SECURITY policy and is deliberately taken from the
    # USER layer only — even for a trusted project. A project ``.wisp/settings.json``
    # is project-controlled, so honoring its ``protected_paths`` would let a repo ship
    # ``{"protected_paths": []}`` to disable the secret-file guard and expose its own
    # ``.env`` to the model. The project may not weaken (or set) this policy.
    # Retry policy is also user-only: a project must not be able to increase API
    # spending or force a user to wait longer by changing its local settings.
    # Effort is user-only for the same reason as retry: it directly controls
    # per-request cost/latency (a higher reasoning tier means more tokens), so a
    # project must not be able to force an expensive tier on every prompt just by
    # being trusted for read/write access.
    # MCP server definitions are user-only because they name commands that Wisp may
    # later execute; project trust never grants authority to configure executables.
    return ResolvedSettings(
        provider=project_provider or user_provider,
        model=project_model or user_model,
        session_dir=_coalesce(project_settings.session_dir, user_settings.session_dir),
        auth_path=_coalesce(project_settings.auth_path, user_settings.auth_path),
        protected_paths=_coalesce_paths(user_settings.protected_paths),
        retry=user_settings.retry,
        effort=user_settings.effort,
        context_reserve_tokens=user_settings.context_reserve_tokens,
        auto_compaction_enabled=user_settings.auto_compaction_enabled,
        update_check_enabled=user_settings.update_check_enabled,
        mcp_servers=user_settings.mcp_servers,
        openai_compatible=user_settings.openai_compatible,
        user_provider=user_provider,
        model_from_user=project_model is None and user_model is not None,
    )


def user_settings_path(*, home_dir: Path | None = None) -> Path:
    """Return the user (global) settings file path (``~/.wisp/settings.json``)."""

    home = home_dir if home_dir is not None else Path.home()
    return (home / ".wisp" / PROJECT_SETTINGS_FILENAME).expanduser()


def persist_user_model_selection(
    provider: str,
    model: str | None,
    effort: str | None,
    *,
    home_dir: Path | None = None,
) -> None:
    """Persist the last successful TUI model selection as user defaults.

    ``None`` removes ``model`` or ``effort`` so provider defaults remain defaults
    rather than being copied into the settings file. The update is best-effort and
    preserves every unrelated user setting.
    """

    _persist_user_settings(
        {"provider": provider, "model": model, "effort": effort},
        home_dir=home_dir,
        preference="model selection",
    )


def persist_user_effort(effort: str | None, *, home_dir: Path | None = None) -> None:
    """Persist only ``effort`` while preserving the compatibility API."""

    _persist_user_settings(
        {"effort": effort},
        home_dir=home_dir,
        preference="effort",
    )


def _persist_user_settings(
    updates: Mapping[str, str | None],
    *,
    home_dir: Path | None,
    preference: str,
) -> None:
    """Safely read, update, and atomically replace the user settings file.

    A missing or malformed file is treated like an empty settings document, matching
    the read path. An existing file that cannot be read aborts the update because
    continuing would risk destroying unrelated settings. Write failures warn and do
    not affect the already-applied runtime configuration.
    """

    path = user_settings_path(home_dir=home_dir)
    data: dict[str, object] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = None
    except OSError as exc:
        _warn(
            f"could not read settings file {path} before writing, {preference} not persisted: {exc}"
        )
        return
    if raw is not None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            data = parsed

    # A valid JSON object can still contain values rejected by WispSettings. If
    # those values survive this write, the next startup rejects the entire file and
    # loses the newly persisted selection. Remove only invalid recognized top-level
    # fields; preserve valid and unknown keys for forward compatibility.
    try:
        WispSettings.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            location = error.get("loc", ())
            field = location[0] if location else None
            if isinstance(field, str) and field in WispSettings.model_fields:
                data.pop(field, None)

    for key, value in updates.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        tmp_path = path.with_name(f".{path.name}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp_path, flags, 0o600)
        try:
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                fd = -1
                tmp_file.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        finally:
            if fd >= 0:
                os.close(fd)
        tmp_path.replace(path)
    except OSError as exc:
        _warn(f"could not write settings file {path}: {exc}")


def _load_settings_file(
    path: Path,
    *,
    ignored_fields: frozenset[str] = frozenset(),
    secure_permissions: bool = False,
) -> WispSettings:
    """Load one settings file, returning empty settings on any problem.

    A missing file is normal (returns empty settings silently). A file that exists
    but cannot be parsed or validated is a user error worth surfacing, so we warn on
    stderr and continue with empty settings rather than aborting startup.
    """

    if secure_permissions and os.name == "posix":
        try:
            path.chmod(0o600)
            path.parent.chmod(0o700)
        except FileNotFoundError:
            return WispSettings()
        except OSError as exc:
            _warn(f"could not secure settings file {path}: {exc}")
            return WispSettings()

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

    # Project-owned policy fields are ignored before schema validation. An invalid
    # value for a field the project cannot control must not suppress otherwise-valid
    # provider, model, session, or auth settings from a trusted project.
    if ignored_fields:
        data = {key: value for key, value in data.items() if key not in ignored_fields}

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
