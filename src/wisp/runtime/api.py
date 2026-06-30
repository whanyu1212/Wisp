"""Public API exposed to built-in and future user extensions."""

from __future__ import annotations

from dataclasses import dataclass

from wisp.providers.base import Provider
from wisp.runtime.event_bus import EventBus, EventHandler
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.tools.base import Tool


class ExtensionAPI:
    """Small extension-facing API for registering runtime capabilities."""

    def __init__(
        self, *, providers: ProviderRegistry, tools: ToolRegistry, events: EventBus
    ) -> None:
        self._providers = providers
        self._tools = tools
        self._events = events

    def register_provider(self, provider: Provider, *, replace: bool = True) -> None:
        """Register a model provider with the runtime."""

        self._providers.register(provider, replace=replace)

    def register_tool(self, tool: Tool, *, replace: bool = True) -> None:
        """Register a local tool with the runtime."""

        self._tools.register(tool, replace=replace)

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Subscribe to runtime events emitted by the agent core."""

        self._events.on(event_type, handler)


@dataclass(frozen=True)
class WispRuntime:
    """Runtime state shared by CLI renderers, agent loops, and extensions."""

    providers: ProviderRegistry
    tools: ToolRegistry
    events: EventBus
    api: ExtensionAPI
