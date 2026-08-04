"""Slash-command parsing for the Wisp TUI."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum

from wisp.events import RpcCommandDescriptor
from wisp.runtime.builtin_commands import builtin_command_descriptors
from wisp.runtime.commands import (
    CommandArgument,
    CommandDescriptor,
    CommandRegistry,
    UnknownCommandError,
)


class TuiSlashCommandName(StrEnum):
    """Built-in TUI slash commands."""

    help = "help"
    history = "history"
    quit = "quit"
    auth = "auth"
    login = "login"
    logout = "logout"
    provider = "provider"
    model = "model"
    resume = "resume"
    compact = "compact"
    context = "context"


# Sentinel effort token for "/model <id> <token>" meaning "explicitly clear
# back to provider default" -- distinct from omitting the effort argument
# entirely, which means "leave whatever effort is already configured
# untouched." No provider's real effort tier is ever this bare dash
# (confirmed against catalog.toml), so it can't collide with a genuine tier
# string. Produced by widgets.ModelPicker, consumed by
# TuiShell._handle_model_command -- lives here (imported by both, no
# renderer-specific dependency) rather than in the Textual-only widgets
# module, since TuiShell must stay renderer-agnostic.
MODEL_COMMAND_CLEAR_EFFORT_TOKEN = "-"


@dataclass(frozen=True)
class TuiSlashCommand:
    """Parsed TUI slash command."""

    name: TuiSlashCommandName
    args: tuple[str, ...] = ()


class TuiSlashCommandError(ValueError):
    """Raised when a slash command is syntactically invalid."""


@dataclass(frozen=True)
class SlashCommandSpec:
    """A user-facing slash command for the inline completion menu.

    One row per command the menu offers: the canonical ``/``-prefixed spelling,
    a one-line description, and whether it accepts an argument. This is the single
    source of truth the menu and completion read, kept alongside the parser so all
    three stay in sync.

    ``takes_args`` means the command *accepts* an argument (required or optional),
    not that one is mandatory: ``/model``/``/auth`` run bare (show current /
    default) yet still take a value. Tab-completion uses it to leave a trailing
    space for the value. ``prefill_on_partial_enter`` keeps destructive commands
    in argument-completion mode unless the user explicitly types the full name.
    """

    command: str  # canonical spelling, e.g. "/model"
    description: str
    takes_args: bool = False
    prefill_on_partial_enter: bool = False


def _slash_spec_from_descriptor(descriptor: CommandDescriptor) -> SlashCommandSpec:
    return SlashCommandSpec(
        descriptor.slash_command,
        descriptor.description,
        takes_args=descriptor.accepts_arguments,
        prefill_on_partial_enter=descriptor.prefill_on_partial_enter,
    )


@dataclass(frozen=True)
class TuiCommandCatalog:
    """Executable command metadata shared by TUI command surfaces."""

    descriptors: tuple[CommandDescriptor, ...]
    _registry: CommandRegistry = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        registry = CommandRegistry(self.descriptors)
        object.__setattr__(self, "descriptors", registry.all())
        object.__setattr__(self, "_registry", registry)

    @property
    def specs(self) -> tuple[SlashCommandSpec, ...]:
        return tuple(_slash_spec_from_descriptor(descriptor) for descriptor in self.descriptors)

    def get(self, identifier: str) -> CommandDescriptor:
        return self._registry.get(identifier)

    @classmethod
    def from_rpc(cls, descriptors: tuple[RpcCommandDescriptor, ...]) -> TuiCommandCatalog:
        """Build a catalog from RPC discovery, excluding commands without TUI handlers."""

        executable_names = {command.value for command in TuiSlashCommandName}
        return cls(
            tuple(
                _command_descriptor_from_rpc(descriptor)
                for descriptor in descriptors
                if descriptor.name in executable_names
            )
        )


def _command_descriptor_from_rpc(descriptor: RpcCommandDescriptor) -> CommandDescriptor:
    return CommandDescriptor(
        name=descriptor.name,
        title=descriptor.title,
        description=descriptor.description,
        category=descriptor.category,
        aliases=descriptor.aliases,
        arguments=tuple(
            CommandArgument(
                name=argument.name,
                description=argument.description,
                required=argument.required,
            )
            for argument in descriptor.arguments
        ),
        accepts_arguments=descriptor.accepts_arguments,
        prefill_on_partial_enter=descriptor.prefill_on_partial_enter,
        order=descriptor.order,
    )


DEFAULT_TUI_COMMAND_CATALOG = TuiCommandCatalog(builtin_command_descriptors())


# Ordered for display: the everyday commands first, session/auth after. Only
# user-facing spellings appear (aliases like /exit, :q are still parsed, but the
# menu shows one canonical entry per command).
SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = DEFAULT_TUI_COMMAND_CATALOG.specs


# The *whole* input is a command attempt only if it's a lone slash-word: a slash
# followed by a command-like word, nothing else (`/help`, `/mdoel`). The moment
# more follows — another word (`/todo remember this`), an inner slash
# (`/etc/hosts`), or a space (`/ note`) — it's prose that merely starts with a
# slash, a valid literal message rather than a mistyped command, so it reaches the
# model instead of raising "Unknown command". Matching the whole input (not just
# the first token) is what lets multi-word slash prose through.
_COMMAND_ATTEMPT = re.compile(r"^/[A-Za-z][A-Za-z-]*$")


def parse_tui_slash_command(
    text: str,
    *,
    catalog: TuiCommandCatalog = DEFAULT_TUI_COMMAND_CATALOG,
) -> TuiSlashCommand | None:
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
    try:
        descriptor = catalog.get(first_word)
    except UnknownCommandError:
        # Not a known command. It's a mistyped-command error only when the WHOLE
        # input is a lone `/word`; any slash word followed by more (words, a path,
        # a quote) is a literal prompt and passes through as None to the model —
        # never touching shlex.
        if _COMMAND_ATTEMPT.match(stripped):
            raise TuiSlashCommandError(f"Unknown command: {first_word}") from None
        return None

    # Known command: now tokenize for args. A quote error here IS a command error
    # (the user is invoking a real command with malformed quoting).
    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        raise TuiSlashCommandError(str(exc)) from exc
    return TuiSlashCommand(name=TuiSlashCommandName(descriptor.name), args=tuple(parts[1:]))


__all__ = [
    "DEFAULT_TUI_COMMAND_CATALOG",
    "SLASH_COMMAND_SPECS",
    "SlashCommandSpec",
    "TuiCommandCatalog",
    "TuiSlashCommand",
    "TuiSlashCommandError",
    "TuiSlashCommandName",
    "parse_tui_slash_command",
]
