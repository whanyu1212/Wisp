"""Public API exposed to built-in and future user extensions."""

from __future__ import annotations

from dataclasses import dataclass, field

from wisp.providers.base import Provider
from wisp.providers.catalog import ModelRegistry
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
    models: ModelRegistry
    _configured_providers: dict[str, Provider] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Treat providers present at direct construction as runtime-configured."""

        if not self._configured_providers:
            self.capture_provider_configuration()

    def capture_provider_configuration(self) -> None:
        """Record the provider instances owned by runtime configuration.

        Extensions may subsequently add providers or replace configured names.
        Identity against this snapshot distinguishes those extension-owned entries
        from adapters that should be refreshed when auth or retry settings change.
        """

        self._configured_providers.clear()
        self._configured_providers.update(
            (provider.name, provider) for provider in self.providers.all()
        )

    def providers_for_configuration(self, candidate: WispRuntime) -> tuple[Provider, ...]:
        """Return the provider set produced by adopting ``candidate`` safely."""

        candidate_configured = dict(candidate._configured_providers)
        if not candidate_configured:
            candidate_configured = {
                provider.name: provider for provider in candidate.providers.all()
            }
        remaining = dict(candidate_configured)
        providers: list[Provider] = []
        for provider in self.providers.all():
            candidate_provider = remaining.pop(provider.name, None)
            if (
                self._configured_providers.get(provider.name) is provider
                and candidate_provider is not None
            ):
                providers.append(candidate_provider)
            else:
                providers.append(provider)
        return (*providers, *remaining.values())

    def adopt_provider_configuration(self, candidate: WispRuntime) -> None:
        """Adopt configured provider instances without replacing shared runtime state.

        A runtime rebuilt for another auth path owns fresh provider adapters, but
        its event bus, extension API, and tool registry must not displace those
        already connected to an active coding session. Provider registrations that
        exist only in the live runtime are extension-owned and remain available.
        """

        providers = self.providers_for_configuration(candidate)
        self.providers.replace_all(providers)
        self._configured_providers.clear()
        self._configured_providers.update(candidate._configured_providers)
