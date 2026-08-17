"""Runtime registries populated by built-in and future user extensions."""

from __future__ import annotations

from collections.abc import Iterable

from wisp.providers.base import Provider, ToolSpec
from wisp.tools.base import Tool, ToolExecutionMetadata, ToolPromptMetadata


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
        self._execution_metadata: dict[str, ToolExecutionMetadata] = {}
        self._prompt_metadata: dict[str, ToolPromptMetadata] = {}

    def register(
        self,
        tool: Tool,
        *,
        execution: ToolExecutionMetadata | None = None,
        prompt: ToolPromptMetadata | None = None,
        replace: bool = True,
    ) -> None:
        """Register a tool by its declared name."""

        if not replace and tool.name in self._tools:
            msg = f"Tool already registered: {tool.name}"
            raise ValueError(msg)
        self._tools[tool.name] = tool
        if execution is None:
            self._execution_metadata.pop(tool.name, None)
        else:
            self._execution_metadata[tool.name] = execution
        if prompt is None:
            self._prompt_metadata.pop(tool.name, None)
        else:
            self._prompt_metadata[tool.name] = prompt

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

    def execution_metadata_for(self, name: str) -> ToolExecutionMetadata:
        """Return conservative execution metadata for one registered tool."""

        return self._execution_metadata.get(name, ToolExecutionMetadata())

    def prompt_metadata(self, names: Iterable[str]) -> tuple[ToolPromptMetadata, ...]:
        """Return prompt metadata for selected tools in registration order."""

        selected = frozenset(names)
        return tuple(
            metadata
            for name in self._tools
            if name in selected and (metadata := self._prompt_metadata.get(name)) is not None
        )

    def prompt_metadata_for(self, name: str) -> ToolPromptMetadata | None:
        """Return prompt metadata for one registered tool, if present."""

        return self._prompt_metadata.get(name)
