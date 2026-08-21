"""Runtime registries populated by built-in and future user extensions."""

from __future__ import annotations

from collections.abc import Callable, Iterable

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
    """Registry of model providers available to the agent runtime.

    A provider may be registered as a name plus a factory instead of an instance.
    Each provider module imports its vendor SDK at module scope, and those imports
    dominate cold start — roughly 1.4 s for the full set — even though a run uses
    at most one provider. Deferring construction until :meth:`get` keeps that cost
    off startup without changing what callers observe.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._factories: dict[str, Callable[[], Provider]] = {}

    def register(self, provider: Provider, *, replace: bool = True) -> None:
        """Register a provider by its declared name."""

        self._reserve(provider.name, replace=replace)
        self._factories.pop(provider.name, None)
        self._providers[provider.name] = provider

    def register_factory(
        self,
        name: str,
        factory: Callable[[], Provider],
        *,
        replace: bool = True,
    ) -> None:
        """Register a provider to be constructed the first time it is requested.

        ``name`` must match the ``name`` the constructed provider declares; it is the
        identity every caller sees until something actually needs the provider.
        """

        self._reserve(name, replace=replace)
        self._providers.pop(name, None)
        self._factories[name] = factory

    def _reserve(self, name: str, *, replace: bool) -> None:
        if not replace and name in self._names():
            msg = f"Provider already registered: {name}"
            raise ValueError(msg)

    def _names(self) -> tuple[str, ...]:
        # Registration order across both maps, without constructing anything.
        ordered = dict.fromkeys((*self._providers, *self._factories))
        return tuple(ordered)

    def get(self, name: str) -> Provider:
        """Return a registered provider, constructing it on first use."""

        provider = self._providers.get(name)
        if provider is not None:
            return provider
        factory = self._factories.get(name)
        if factory is None:
            raise UnknownProviderError(name)
        provider = factory()
        if provider.name != name:
            msg = f"Provider factory for {name!r} produced provider {provider.name!r}"
            raise ValueError(msg)
        self._providers[name] = provider
        self._factories.pop(name, None)
        return provider

    def names(self) -> tuple[str, ...]:
        """Return registered provider names in registration order."""

        return self._names()

    def constructed(self) -> dict[str, Provider]:
        """Return only the providers that already exist, constructing nothing."""

        return dict(self._providers)

    def is_deferred(self, name: str) -> bool:
        """Return whether ``name`` is registered but not yet constructed."""

        return name in self._factories

    def all(self) -> tuple[Provider, ...]:
        """Return registered providers in registration order.

        Constructs every deferred provider, so prefer :meth:`names` when only the
        registered identities are needed.
        """

        return tuple(self.get(name) for name in self._names())

    def replace_all(self, providers: Iterable[Provider]) -> None:
        """Atomically replace provider instances while preserving this registry.

        Runtime extension APIs retain a reference to this registry. Replacing its
        contents, rather than replacing the registry object, keeps that API and
        the runtime's event bus connected during credential/config refreshes.
        """

        replacements = {provider.name: provider for provider in providers}
        self._providers = replacements
        self._factories = {}


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
