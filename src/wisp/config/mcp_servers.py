"""Read and change the user's MCP servers in ``~/.wisp/settings.json``.

MCP servers are user-only settings: each one is a command Wisp starts, or an
endpoint it connects to, on every launch. Changes are made on the raw JSON object
under :func:`wisp.config.settings.user_settings_lock`, so entries that are not being
changed stay byte-for-byte as the user wrote them and unknown keys survive. The
whole resulting document is validated before the file is replaced, so a change can
never leave the file in a state the loader would reject (which would drop every
user setting, not only the MCP ones).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from wisp.config.settings import (
    SettingsDocumentError,
    WispSettings,
    read_settings_document,
    replace_settings_document,
    user_settings_lock,
    user_settings_path,
)
from wisp.mcp.config import McpServerConfig

# Keys accepted from a pasted `{"mcpServers": {...}}` snippet. `type` is checked
# against the transport and dropped; anything else (headers, cwd, ...) is rejected
# by name because Wisp would not honour it.
_SNIPPET_KEYS = frozenset({"command", "args", "env", "url", "type"})
_STDIO_TYPES = frozenset({"stdio"})
_HTTP_TYPES = frozenset({"http", "streamable-http", "streamableHttp"})


class McpSettingsError(RuntimeError):
    """An MCP server change was refused; the settings file is unchanged."""


class McpServerExistsError(McpSettingsError):
    """A server with a requested name already exists and ``replace`` was false."""


def load_user_mcp_servers(*, home_dir: Path | None = None) -> tuple[McpServerConfig, ...]:
    """Return the configured MCP servers, sorted by name.

    Args:
        home_dir (Path | None): User home override, for isolated callers.

    Returns:
        tuple[McpServerConfig, ...]: Validated servers; empty when none are set.

    Raises:
        McpSettingsError: If the settings file cannot be read or its MCP servers
            are invalid. The message names the problem without echoing values.
    """

    path = user_settings_path(home_dir=home_dir)
    try:
        document = read_settings_document(path) or {}
    except SettingsDocumentError as exc:
        raise McpSettingsError(str(exc)) from None
    raw = document.get("mcp_servers")
    if raw is None:
        return ()
    return _validated_document({"mcp_servers": raw}, path).mcp_servers or ()


def add_user_mcp_servers(
    servers: Sequence[McpServerConfig],
    *,
    replace: bool = False,
    home_dir: Path | None = None,
) -> None:
    """Save ``servers`` in one update, refusing to overwrite existing ones unless ``replace``.

    Either every server is saved or none is.

    Args:
        servers (Sequence[McpServerConfig]): Validated servers to save.
        replace (bool): Whether existing servers with the same names may be replaced.
        home_dir (Path | None): User home override, for isolated callers.

    Raises:
        McpServerExistsError: If a name is taken and ``replace`` is false.
        McpSettingsError: If the resulting settings would be invalid.
    """

    entries = {server.name: _settings_entry(server) for server in servers}

    def change(current: dict[str, object]) -> None:
        taken = sorted(name for name in entries if name in current)
        if taken and not replace:
            names = ", ".join(repr(name) for name in taken)
            raise McpServerExistsError(f"MCP servers already exist: {names}")
        current.update(entries)

    _update_user_mcp_servers(change, home_dir=home_dir)


def remove_user_mcp_server(name: str, *, home_dir: Path | None = None) -> None:
    """Remove the server named ``name``.

    Raises:
        McpSettingsError: If no such server exists, or the settings are invalid.
    """

    def change(servers: dict[str, object]) -> None:
        if name not in servers:
            raise McpSettingsError(f"no MCP server named {name!r} is configured")
        del servers[name]

    _update_user_mcp_servers(change, home_dir=home_dir)


def set_user_mcp_server_enabled(
    name: str,
    enabled: bool,
    *,
    confirmed: Callable[[McpServerConfig], bool] | None = None,
    home_dir: Path | None = None,
) -> None:
    """Enable or disable the server named ``name`` without changing anything else.

    Args:
        name (str): Server to change.
        enabled (bool): New state.
        confirmed (Callable[[McpServerConfig], bool] | None): Whether the saved
            server is still the one the user confirmed, checked under the settings
            lock. When given and false, the change is refused, so a confirmation
            cannot enable a command that was edited in the meantime.
        home_dir (Path | None): User home override, for isolated callers.

    Raises:
        McpSettingsError: If no such server exists, it is no longer the one the user
            confirmed, or the settings are invalid.
    """

    def change(servers: dict[str, object]) -> None:
        entry = servers.get(name)
        if not isinstance(entry, dict):
            raise McpSettingsError(f"no MCP server named {name!r} is configured")
        if confirmed is not None and not confirmed(validated_mcp_server(name, entry)):
            raise McpSettingsError(f"MCP server {name!r} changed while you were confirming")
        updated = dict(entry)
        if enabled:
            updated.pop("enabled", None)
        else:
            updated["enabled"] = False
        servers[name] = updated

    _update_user_mcp_servers(change, home_dir=home_dir)


def parse_mcp_snippet(text: str) -> tuple[McpServerConfig, ...]:
    """Parse a published MCP config snippet into validated servers.

    Accepts the common ``{"mcpServers": {name: {...}}}`` shape (also
    ``mcp_servers``). Each entry may carry ``command``, ``args``, ``env``, ``url``,
    and a ``type`` that must match its transport; any other key is rejected by name.

    Args:
        text (str): Snippet text, as copied from a server's README.

    Returns:
        tuple[McpServerConfig, ...]: Validated servers in snippet order.

    Raises:
        McpSettingsError: If the snippet is not JSON, has no servers, or an entry
            is unsupported or invalid.
    """

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpSettingsError(f"the snippet is not valid JSON: {exc.msg}") from None
    servers = None
    if isinstance(parsed, Mapping):
        servers = parsed.get("mcpServers", parsed.get("mcp_servers"))
    if not isinstance(servers, Mapping) or not servers:
        raise McpSettingsError('the snippet must contain an "mcpServers" object with a server')
    configs: list[McpServerConfig] = []
    for name, raw in servers.items():
        if not isinstance(raw, Mapping):
            raise McpSettingsError(f"server {name!r} must be a JSON object")
        unsupported = sorted(str(key) for key in raw if key not in _SNIPPET_KEYS)
        if unsupported:
            raise McpSettingsError(
                f"server {name!r} uses unsupported settings: {', '.join(unsupported)}"
            )
        entry = {key: value for key, value in raw.items() if key != "type"}
        kind = raw.get("type")
        if kind is not None:
            expected = _STDIO_TYPES if "command" in raw else _HTTP_TYPES
            if kind not in expected:
                raise McpSettingsError(
                    f"server {name!r} has type {kind!r}, which does not match its transport"
                )
        configs.append(validated_mcp_server(str(name), entry))
    return tuple(configs)


def validated_mcp_server(name: str, entry: Mapping[str, object]) -> McpServerConfig:
    """Validate one server entry, naming each problem without echoing values.

    Raises:
        McpSettingsError: If the entry is not a valid server.
    """

    try:
        return McpServerConfig.model_validate({**entry, "name": name})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'entry'}: {error['msg']}"
            for error in exc.errors()
        )
        raise McpSettingsError(f"server {name!r} is invalid: {problems}") from None


def _settings_entry(server: McpServerConfig) -> dict[str, object]:
    """Return ``server`` as it is written under ``mcp_servers``, with real env values."""

    # Defaults are omitted so the entry reads like one written by hand; in
    # particular `enabled` appears only when false. Fields with a custom serializer
    # (`env`, `tool_safety`) survive `exclude_defaults` as `{}`, so drop empties too.
    dumped = server.model_dump(mode="json", exclude={"name"}, exclude_defaults=True)
    entry = {key: value for key, value in dumped.items() if value not in ({}, [])}
    # JSON dumps mask SecretStr values; the file must hold the real values.
    if server.env:
        entry["env"] = {name: value.get_secret_value() for name, value in server.env}
    return entry


def same_launch(saved: McpServerConfig, shown: McpServerConfig) -> bool:
    """Return whether two servers run or connect to the same thing, as a user sees it.

    Compares the transport, arguments, and environment variable names; environment
    values are never shown, so they are not part of what a user confirms.

    Args:
        saved (McpServerConfig): The server as currently saved.
        shown (McpServerConfig): The server as the user was shown it.

    Returns:
        bool: True when a confirmation of ``shown`` still covers ``saved``.
    """

    def launch(server: McpServerConfig) -> tuple[object, ...]:
        names = tuple(name for name, _value in server.env)
        return (server.command, server.args, server.url, names, server.env_from)

    return launch(saved) == launch(shown)


def _update_user_mcp_servers(
    change: Callable[[dict[str, object]], None], *, home_dir: Path | None
) -> None:
    path = user_settings_path(home_dir=home_dir)
    try:
        with user_settings_lock(path):
            document = dict(read_settings_document(path) or {})
            current = document.get("mcp_servers", {})
            if current is None:
                current = {}
            if not isinstance(current, Mapping):
                raise McpSettingsError(
                    f"mcp_servers in {path} is not a JSON object; fix it before changing servers"
                )
            servers = dict(current)
            change(servers)
            if servers:
                document["mcp_servers"] = servers
            else:
                document.pop("mcp_servers", None)
            _validated_document(document, path)
            replace_settings_document(path, document)
    except SettingsDocumentError as exc:
        raise McpSettingsError(str(exc)) from None
    except OSError as exc:
        raise McpSettingsError(f"could not write {path}: {exc}") from None


def _validated_document(document: Mapping[str, object], path: Path) -> WispSettings:
    # The loader validates `tui_keybindings` separately and drops it on its own, so
    # it must not block an MCP change here either.
    candidate = {key: value for key, value in document.items() if key != "tui_keybindings"}
    try:
        return WispSettings.model_validate(candidate)
    except ValidationError as exc:
        raw_servers = document.get("mcp_servers")
        server_names = list(raw_servers) if isinstance(raw_servers, Mapping) else []
        fields = sorted({_error_field(error["loc"], server_names) for error in exc.errors()})
        raise McpSettingsError(
            f"{path} would be invalid ({', '.join(fields)}); fix it before changing servers"
        ) from None


def _error_field(location: tuple[int | str, ...], server_names: list[str]) -> str:
    """Name the setting an error points at; servers by name, not list position."""

    if (
        len(location) >= 2
        and location[0] == "mcp_servers"
        and isinstance(location[1], int)
        and location[1] < len(server_names)
    ):
        return f"mcp_servers.{server_names[location[1]]}"
    return ".".join(str(part) for part in location[:2])


__all__ = [
    "McpServerExistsError",
    "McpSettingsError",
    "add_user_mcp_servers",
    "load_user_mcp_servers",
    "parse_mcp_snippet",
    "remove_user_mcp_server",
    "same_launch",
    "set_user_mcp_server_enabled",
    "validated_mcp_server",
]
