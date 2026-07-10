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


# The *whole* input is a command attempt only if it's a lone slash-word: a slash
# followed by a command-like word, nothing else (`/help`, `/mdoel`). The moment
# more follows — another word (`/todo remember this`), an inner slash
# (`/etc/hosts`), or a space (`/ note`) — it's prose that merely starts with a
# slash, a valid literal message rather than a mistyped command, so it reaches the
# model instead of raising "Unknown command". Matching the whole input (not just
# the first token) is what lets multi-word slash prose through.
_COMMAND_ATTEMPT = re.compile(r"^/[A-Za-z][A-Za-z-]*$")


def parse_tui_slash_command(text: str) -> TuiSlashCommand | None:
    """Parse a TUI slash command, returning ``None`` for normal prompts.

    Returns a command for a known slash word, ``None`` for a normal prompt
    (including multiline input and slash-prefixed prose like ``/etc/hosts is
    broken``), and raises ``TuiSlashCommandError`` only for a genuine command
    attempt that is unknown or malformed. Commands are deliberately single-line
    so pasted prompt content can never be interpreted as TUI control syntax.
    """

    if "\n" in text or "\r" in text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if not stripped.startswith("/") and stripped != ":q":
        return None

    # Classify BEFORE tokenizing. shlex.split is command-argument machinery — it
    # can raise on an unterminated quote, which is a real error for a known
    # command but would wrongly reject prose that merely contains a lone quote
    # (`/todo remember "this`). So decide what the line *is* from the bare first
    # word first, and only shlex the paths that are actually commands.
    first_word = stripped.split(maxsplit=1)[0]
    name = _ALIASES.get(first_word)
    if name is None:
        # Not a known command. It's a mistyped-command error only when the WHOLE
        # input is a lone `/word`; any slash word followed by more (words, a path,
        # a quote) is a literal prompt and passes through as None to the model —
        # never touching shlex.
        if _COMMAND_ATTEMPT.match(stripped):
            raise TuiSlashCommandError(f"Unknown command: {first_word}")
        return None

    # Known command: now tokenize for args. A quote error here IS a command error
    # (the user is invoking a real command with malformed quoting).
    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        raise TuiSlashCommandError(str(exc)) from exc
    return TuiSlashCommand(name=name, args=tuple(parts[1:]))


__all__ = [
    "SLASH_COMMAND_SPECS",
    "SlashCommandSpec",
    "TuiSlashCommand",
    "TuiSlashCommandError",
    "TuiSlashCommandName",
    "parse_tui_slash_command",
]
