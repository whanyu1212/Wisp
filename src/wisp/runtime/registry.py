"""Runtime registries populated by built-in and future user extensions."""

from __future__ import annotations

from collections.abc import Iterable

from wisp.providers.base import Provider, ToolSpec
from wisp.tools.base import Tool


class UnknownProviderError(KeyError):
    """Raised when a provider name is not registered in the runtime."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"Unknown provider: {self.name}"


class UnknownToolError(KeyError):
    """Raised when a tool name is not registered in the runtime."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"Unknown tool: {self.name}"


class ProviderRegistry:
    """Registry of model providers available to the agent runtime."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider, *, replace: bool = True) -> None:
        """Register a provider by its declared name."""

        if not replace and provider.name in self._providers:
            msg = f"Provider already registered: {provider.name}"
            raise ValueError(msg)
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider:
        """Return a registered provider by name."""

        try:
            return self._providers[name]
        except KeyError as exc:
            raise UnknownProviderError(name) from exc

    def names(self) -> tuple[str, ...]:
        """Return registered provider names in registration order."""

        return tuple(self._providers.keys())

    def all(self) -> tuple[Provider, ...]:
        """Return registered providers in registration order."""

        return tuple(self._providers.values())

    def replace_all(self, providers: Iterable[Provider]) -> None:
        """Atomically replace provider instances while preserving this registry.

        Runtime extension APIs retain a reference to this registry. Replacing its
        contents, rather than replacing the registry object, keeps that API and
        the runtime's event bus connected during credential/config refreshes.
        """

        replacements = {provider.name: provider for provider in providers}
        self._providers = replacements


class ToolRegistry:
    """Registry of tools available to the agent runtime."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = True) -> None:
        """Register a tool by its declared name."""

        if not replace and tool.name in self._tools:
            msg = f"Tool already registered: {tool.name}"
            raise ValueError(msg)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Return a registered tool by name."""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(name) from exc

    def names(self) -> tuple[str, ...]:
        """Return registered tool names in registration order."""

        return tuple(self._tools.keys())

    def all(self) -> tuple[Tool, ...]:
        """Return registered tools in registration order."""

        return tuple(self._tools.values())

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return provider-facing specs for registered tools."""

        return tuple(ToolSpec.from_tool(tool) for tool in self._tools.values())
