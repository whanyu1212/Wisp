"""Frontend-neutral command descriptors and registry."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SPECIAL_ALIAS_RE = re.compile(r"^:[a-z][a-z0-9-]*$")


class CommandCategory(StrEnum):
    """High-level command groups shared by frontends."""

    general = "general"
    session = "session"
    configuration = "configuration"
    auth = "auth"


class CommandRegistryError(ValueError):
    """Base class for command-registry validation errors."""


class DuplicateCommandError(CommandRegistryError):
    """Raised when a command name or alias conflicts with another command."""


class UnknownCommandError(KeyError):
    """Raised when a command token is not registered."""

    def __init__(self, identifier: str) -> None:
        super().__init__(identifier)
        self.identifier = identifier

    def __str__(self) -> str:
        return f"Unknown command: {self.identifier}"


@dataclass(frozen=True)
class CommandArgument:
    """Display/help metadata for one command argument."""

    name: str
    description: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        if not _COMMAND_NAME_RE.fullmatch(self.name):
            msg = f"Command argument name is invalid: {self.name!r}"
            raise ValueError(msg)


@dataclass(frozen=True)
class CommandDescriptor:
    """Typed, frontend-neutral metadata for one command."""

    name: str
    title: str
    description: str
    category: CommandCategory | str = CommandCategory.general
    aliases: tuple[str, ...] = ()
    arguments: tuple[CommandArgument, ...] = ()
    accepts_arguments: bool = False
    prefill_on_partial_enter: bool = False
    order: int = 1000

    def __post_init__(self) -> None:
        _validate_command_name(self.name)
        object.__setattr__(self, "category", CommandCategory(self.category))
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "arguments", tuple(self.arguments))
        if not self.title.strip():
            raise ValueError("Command title must be non-empty")
        if not self.description.strip():
            raise ValueError("Command description must be non-empty")
        if self.arguments and not self.accepts_arguments:
            raise ValueError("Command arguments require accepts_arguments=True")
        seen_aliases: set[str] = set()
        for alias in self.aliases:
            _validate_command_alias(alias)
            if alias == self.name:
                raise ValueError(f"Command alias duplicates command name: {alias}")
            if alias in seen_aliases:
                raise ValueError(f"Duplicate command alias: {alias}")
            seen_aliases.add(alias)

    @property
    def slash_command(self) -> str:
        """Return the canonical slash spelling."""

        return f"/{self.name}"

    @property
    def slash_aliases(self) -> tuple[str, ...]:
        """Return aliases as user-typed command tokens."""

        return tuple(alias if alias.startswith(":") else f"/{alias}" for alias in self.aliases)


class CommandRegistry:
    """Deterministic registry of frontend-neutral command descriptors."""

    def __init__(self, descriptors: Iterable[CommandDescriptor] = ()) -> None:
        self._descriptors: dict[str, CommandDescriptor] = {}
        self._aliases: dict[str, str] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: CommandDescriptor, *, replace: bool = False) -> None:
        """Register a command descriptor by stable name and aliases."""

        if not replace and descriptor.name in self._descriptors:
            raise DuplicateCommandError(f"Command already registered: {descriptor.name}")
        owner = self._aliases.get(descriptor.name)
        if owner is not None and owner != descriptor.name:
            raise DuplicateCommandError(
                f"Command name conflicts with alias: {descriptor.name} -> {owner}"
            )

        for alias in descriptor.aliases:
            alias_owner = self._aliases.get(alias)
            if alias_owner is not None and alias_owner != descriptor.name:
                raise DuplicateCommandError(f"Command alias already registered: {alias}")
            if alias in self._descriptors and alias != descriptor.name:
                raise DuplicateCommandError(f"Command alias conflicts with name: {alias}")

        if replace and descriptor.name in self._descriptors:
            self._remove_aliases(descriptor.name)

        self._descriptors[descriptor.name] = descriptor
        for alias in descriptor.aliases:
            self._aliases[alias] = descriptor.name

    def get(self, identifier: str) -> CommandDescriptor:
        """Return a descriptor by name, slash spelling, or alias."""

        key = _normalize_identifier(identifier)
        name = self._aliases.get(key, key)
        try:
            return self._descriptors[name]
        except KeyError as exc:
            raise UnknownCommandError(identifier) from exc

    def all(self) -> tuple[CommandDescriptor, ...]:
        """Return descriptors in deterministic display/discovery order."""

        return tuple(sorted(self._descriptors.values(), key=lambda item: (item.order, item.name)))

    def names(self) -> tuple[str, ...]:
        """Return command names in deterministic display/discovery order."""

        return tuple(descriptor.name for descriptor in self.all())

    def _remove_aliases(self, name: str) -> None:
        for alias, owner in tuple(self._aliases.items()):
            if owner == name:
                del self._aliases[alias]


def _normalize_identifier(identifier: str) -> str:
    if identifier.startswith("/"):
        return identifier[1:]
    return identifier


def _validate_command_name(name: str) -> None:
    if not _COMMAND_NAME_RE.fullmatch(name):
        msg = f"Command name is invalid: {name!r}"
        raise ValueError(msg)


def _validate_command_alias(alias: str) -> None:
    if alias.startswith("/"):
        msg = f"Command alias must omit leading slash: {alias!r}"
        raise ValueError(msg)
    if not (_COMMAND_NAME_RE.fullmatch(alias) or _SPECIAL_ALIAS_RE.fullmatch(alias)):
        msg = f"Command alias is invalid: {alias!r}"
        raise ValueError(msg)
