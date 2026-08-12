"""Public API exposed to built-in and future user extensions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from wisp.events import WispEvent
from wisp.providers.base import Provider
from wisp.providers.catalog import ModelRegistry
from wisp.runtime.commands import CommandDescriptor, CommandRegistry
from wisp.runtime.event_bus import EventBus, EventHandler
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.tools.base import Tool, ToolPromptMetadata
from wisp.tools.process_manager import ProcessSupervisor

if TYPE_CHECKING:
    from wisp.mcp.runtime import McpRuntime


class ExtensionAPI:
    """Small extension-facing API for registering runtime capabilities."""

    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        tools: ToolRegistry,
        commands: CommandRegistry | None = None,
        events: EventBus,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._providers = providers
        self._tools = tools
        self._commands = commands or CommandRegistry()
        self._events = events
        self._process_supervisor = process_supervisor

    def register_provider(self, provider: Provider, *, replace: bool = True) -> None:
        """Register a model provider with the runtime."""

        self._providers.register(provider, replace=replace)

    def register_tool(
        self,
        tool: Tool,
        *,
        prompt: ToolPromptMetadata | None = None,
        replace: bool = True,
    ) -> None:
        """Register a local tool with the runtime."""

        self._tools.register(tool, prompt=prompt, replace=replace)

    def register_command(self, descriptor: CommandDescriptor, *, replace: bool = False) -> None:
        """Register a frontend-neutral command descriptor with the runtime."""

        self._commands.register(descriptor, replace=replace)

    @property
    def commands(self) -> CommandRegistry:
        """Return the command registry connected to this extension API."""

        return self._commands

    @property
    def process_supervisor(self) -> ProcessSupervisor | None:
        """Return the runtime process supervisor, if the API is runtime-bound."""

        return self._process_supervisor

    def bind_process_supervisor(self, process_supervisor: ProcessSupervisor) -> None:
        """Bind this API to the runtime-owned process supervisor."""

        if (
            self._process_supervisor is not None
            and self._process_supervisor is not process_supervisor
        ):
            raise ValueError(
                "ExtensionAPI.process_supervisor must match WispRuntime.process_supervisor"
            )
        self._process_supervisor = process_supervisor

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
    commands: CommandRegistry = field(default_factory=CommandRegistry)
    process_supervisor: ProcessSupervisor = field(
        default_factory=ProcessSupervisor,
        repr=False,
    )
    mcp_runtime: McpRuntime | None = field(default=None, repr=False)
    startup_events: tuple[WispEvent, ...] = ()
    unavailable_tool_prefixes: tuple[str, ...] = ()
    _configured_providers: dict[str, Provider] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Normalize shared registries and direct-construction provider state."""

        self.api.bind_process_supervisor(self.process_supervisor)
        if self.commands is not self.api.commands:
            if self.commands.names():
                raise ValueError("WispRuntime.commands must match ExtensionAPI.commands")
            object.__setattr__(self, "commands", self.api.commands)
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

    async def adopt_provider_configuration(self, candidate: WispRuntime) -> None:
        """Adopt configured providers and release the displaced provider adapters.

        The adopted providers transfer from ``candidate`` to this runtime so closing
        the temporary candidate cannot close clients now owned by the live runtime.
        Provider registrations that exist only in the live runtime remain available.
        """

        previous_configured = dict(self._configured_providers)
        providers = self.providers_for_configuration(candidate)
        retained_ids = {id(provider) for provider in providers}
        retained = {
            name: provider
            for name, provider in previous_configured.items()
            if id(provider) in retained_ids
        }
        transferred = {
            name: provider
            for name, provider in candidate._configured_providers.items()
            if id(provider) in retained_ids
        }
        adopted = {**retained, **transferred}
        self.providers.replace_all(providers)
        self._configured_providers.clear()
        self._configured_providers.update(adopted)
        for name in transferred:
            candidate._configured_providers.pop(name, None)
        displaced = tuple(
            provider
            for provider in previous_configured.values()
            if id(provider) not in retained_ids
        )
        await _close_providers(displaced)

    async def aclose(self) -> None:
        """Release runtime-owned providers, MCP connections, and managed processes."""

        try:
            await _close_providers(tuple(self._configured_providers.values()))
            self._configured_providers.clear()
        finally:
            try:
                if self.mcp_runtime is not None:
                    await self.mcp_runtime.aclose()
            finally:
                await self.process_supervisor.aclose()


async def _close_providers(providers: tuple[Provider, ...]) -> None:
    """Close provider adapters that expose an asynchronous cleanup hook."""

    seen: set[int] = set()
    for provider in providers:
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        close = getattr(provider, "aclose", None)
        if callable(close):
            await cast(Callable[[], Awaitable[None]], close)()
