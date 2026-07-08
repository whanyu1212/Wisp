"""Slash-command parsing for the Wisp TUI."""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class SlashCommandSpec:
    """A user-facing slash command for the inline completion menu.

    One row per command the menu offers: the canonical ``/``-prefixed spelling,
    a one-line description, and whether it takes an argument (so Tab-completion
    knows to leave a trailing space for the value). This is the single source of
    truth the menu and completion read, kept alongside the parser so all three
    stay in sync.
    """

    command: str  # canonical spelling, e.g. "/model"
    description: str
    takes_args: bool = False


# Ordered for display: the everyday commands first, session/auth after. Only
# user-facing spellings appear (aliases like /exit, :q are still parsed, but the
# menu shows one canonical entry per command).
SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("/help", "Show the TUI commands"),
    SlashCommandSpec("/model", "Show or switch the active model", takes_args=True),
    SlashCommandSpec("/provider", "Show or switch the active provider", takes_args=True),
    SlashCommandSpec("/auth", "Show credential status"),
    SlashCommandSpec("/login", "Log in to a provider", takes_args=True),
    SlashCommandSpec("/logout", "Remove stored credentials"),
    SlashCommandSpec("/quit", "Quit the TUI"),
)


# A leading token is a *command attempt* only if it's a single slash-word: a
# slash followed by a command-like word with no inner slashes (`/help`, `/mdoel`).
# Anything path-like (`/etc/hosts`), spaced (`/ note`), or multi-segment is prose
# that merely starts with a slash — a valid literal message, not a mistyped
# command — so it must reach the model instead of raising "Unknown command".
_COMMAND_ATTEMPT = re.compile(r"^/[A-Za-z][A-Za-z-]*$")


def parse_tui_slash_command(text: str) -> TuiSlashCommand | None:
    """Parse a TUI slash command, returning ``None`` for normal prompts.

    Returns a command for a known slash word, ``None`` for a normal prompt
    (including slash-prefixed prose like ``/etc/hosts is broken``), and raises
    ``TuiSlashCommandError`` only for a genuine command attempt that is unknown
    or malformed.
    """

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
        # An unknown token is only a "command attempt" (worth erroring on) when it
        # looks like a mistyped command — a lone `/word`. Path-like or spaced
        # leading slashes are literal prompts and pass through as None.
        if _COMMAND_ATTEMPT.match(parts[0]):
            raise TuiSlashCommandError(f"Unknown command: {parts[0]}")
        return None
    return TuiSlashCommand(name=name, args=tuple(parts[1:]))


__all__ = [
    "SLASH_COMMAND_SPECS",
    "SlashCommandSpec",
    "TuiSlashCommand",
    "TuiSlashCommandError",
    "TuiSlashCommandName",
    "parse_tui_slash_command",
]
