"""Slash-command parsing for the Wisp TUI."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum


class TuiSlashCommandName(StrEnum):
    """Built-in TUI slash commands."""

    help = "help"
    quit = "quit"
    auth = "auth"
    login = "login"
    logout = "logout"
    provider = "provider"
    model = "model"


@dataclass(frozen=True)
class TuiSlashCommand:
    """Parsed TUI slash command."""

    name: TuiSlashCommandName
    args: tuple[str, ...] = ()


class TuiSlashCommandError(ValueError):
    """Raised when a slash command is syntactically invalid."""


_ALIASES: dict[str, TuiSlashCommandName] = {
    "/help": TuiSlashCommandName.help,
    "/quit": TuiSlashCommandName.quit,
    "/exit": TuiSlashCommandName.quit,
    ":q": TuiSlashCommandName.quit,
    "/auth": TuiSlashCommandName.auth,
    "/login": TuiSlashCommandName.login,
    "/logout": TuiSlashCommandName.logout,
    "/provider": TuiSlashCommandName.provider,
    "/model": TuiSlashCommandName.model,
}


def parse_tui_slash_command(text: str) -> TuiSlashCommand | None:
    """Parse a TUI slash command, returning ``None`` for normal prompts."""

    stripped = text.strip()
    if not stripped:
        return None
    if not stripped.startswith("/") and stripped != ":q":
        return None
    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        raise TuiSlashCommandError(str(exc)) from exc
    if not parts:
        return None
    name = _ALIASES.get(parts[0])
    if name is None:
        raise TuiSlashCommandError(f"Unknown command: {parts[0]}")
    return TuiSlashCommand(name=name, args=tuple(parts[1:]))


__all__ = [
    "TuiSlashCommand",
    "TuiSlashCommandError",
    "TuiSlashCommandName",
    "parse_tui_slash_command",
]
