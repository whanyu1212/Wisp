"""Runtime extension substrate for Wisp."""

from .api import ExtensionAPI, WispRuntime
from .commands import (
    CommandArgument,
    CommandCategory,
    CommandDescriptor,
    CommandRegistry,
    CommandRegistryError,
    DuplicateCommandError,
    UnknownCommandError,
)
from .event_bus import EventBus
from .registry import ProviderRegistry, ToolRegistry, UnknownProviderError, UnknownToolError

__all__ = [
    "CommandArgument",
    "CommandCategory",
    "CommandDescriptor",
    "CommandRegistry",
    "CommandRegistryError",
    "DuplicateCommandError",
    "EventBus",
    "ExtensionAPI",
    "ProviderRegistry",
    "ToolRegistry",
    "UnknownCommandError",
    "UnknownProviderError",
    "UnknownToolError",
    "WispRuntime",
]
