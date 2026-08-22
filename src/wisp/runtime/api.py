"""Public API exposed to built-in and future user extensions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from wisp.events import WispEvent
from wisp.providers.base import Provider
from wisp.providers.catalog import ModelRegistry
from wisp.runtime.commands import CommandDescriptor, CommandRegistry
from wisp.runtime.event_bus import EventBus, EventHandler
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.tools.base import Tool, ToolExecutionMetadata, ToolPromptMetadata
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

    def register_provider_factory(
        self,
        name: str,
        factory: Callable[[], Provider],
        *,
        replace: bool = True,
    ) -> None:
        """Register a provider constructed on first use.

        Prefer this for providers whose module imports a vendor SDK: the import is
        then paid only by a run that selects the provider, instead of by every
        process at startup.
        """

        self._providers.register_factory(name, factory, replace=replace)

    def register_tool(
        self,
        tool: Tool,
        *,
        execution: ToolExecutionMetadata | None = None,
        prompt: ToolPromptMetadata | None = None,
        replace: bool = True,
    ) -> None:
        """Register a local tool with the runtime."""

        self._tools.register(
            tool,
            execution=execution,
            prompt=prompt,
            replace=replace,
        )

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
    _configured_names: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        """Normalize shared registries and direct-construction provider state."""

        self.api.bind_process_supervisor(self.process_supervisor)
        if self.commands is not self.api.commands:
            if self.commands.names():
                raise ValueError("WispRuntime.commands must match ExtensionAPI.commands")
            object.__setattr__(self, "commands", self.api.commands)
        if not self._configured_names:
            self.capture_provider_configuration()

    def capture_provider_configuration(self) -> None:
        """Record the providers owned by runtime configuration.

        Extensions may subsequently add providers or replace configured names.
        Identity against this snapshot distinguishes those extension-owned entries
        from adapters that should be refreshed when auth or retry settings change.

        Providers register lazily so their vendor SDKs stay off the startup path, so
        record the configured *names* and keep only the instances that already exist.
        A provider that has never been constructed holds no client: it cannot be
        shadowed by an extension yet, there is nothing to transfer on adoption, and
        nothing to close. Whenever an instance is genuinely needed, the registry
        constructs it then.
        """

        self._configured_providers.clear()
        self._configured_providers.update(self.providers.constructed())
        self._configured_names.clear()
        self._configured_names.extend(self.providers.names())

    def _configured_provider_items(self) -> tuple[tuple[str, Provider], ...]:
        """Return configured providers that exist, in registration order.

        Deferred entries are omitted rather than constructed: every caller either
        compares identity (which a fresh instance would fail anyway) or closes a
        client (which a never-built provider does not hold).
        """

        constructed = self.providers.constructed()
        items: list[tuple[str, Provider]] = []
        for name in self._configured_names:
            provider = self._configured_providers.get(name) or constructed.get(name)
            if provider is not None:
                items.append((name, provider))
        return tuple(items)

    def providers_for_configuration(self, candidate: WispRuntime) -> tuple[Provider, ...]:
        """Return the provider set produced by adopting ``candidate`` safely."""

        candidate_configured = dict(candidate._configured_provider_items())
        if not candidate_configured:
            candidate_configured = {
                provider.name: provider for provider in candidate.providers.all()
            }
        remaining = dict(candidate_configured)
        configured = dict(self._configured_provider_items())
        providers: list[Provider] = []
        for name in self.providers.names():
            candidate_provider = remaining.pop(name, None)
            existing = self.providers.constructed().get(name)
            if existing is None:
                if candidate_provider is not None:
                    # A constructed candidate supersedes a deferral with nothing to
                    # displace.
                    providers.append(candidate_provider)
                elif self._deferred_after_adoption(candidate, name) is None:
                    providers.append(self.providers.get(name))
                # Otherwise both sides are deferred: carried across as a factory by
                # `adopt_provider_configuration`, so it is omitted from the instance
                # set rather than constructed here.
                continue
            if configured.get(name) is existing and candidate_provider is not None:
                providers.append(candidate_provider)
            else:
                providers.append(existing)
        return (*providers, *remaining.values())

    def _deferred_after_adoption(
        self, candidate: WispRuntime, name: str
    ) -> Callable[[], Provider] | None:
        """Return the factory to carry across when neither runtime built ``name``.

        Constructing a provider purely to hand it to a refresh would reintroduce the
        vendor-SDK import that lazy registration exists to avoid, and would break the
        identity both runtimes are expected to share afterwards.
        """

        if candidate.providers.constructed().get(name) is not None:
            return None
        factory = candidate.providers.deferred_factory(name) or self.providers.deferred_factory(
            name
        )
        if factory is None:
            return None

        def shared() -> Provider:
            # Both runtimes must observe the *same* provider once either resolves it.
            # Ownership transfer is what lets the candidate be closed without killing
            # a client the live runtime now uses, and two independently constructed
            # instances would silently break that.
            provider = candidate.providers.get(name)
            return provider

        return shared

    async def adopt_provider_configuration(self, candidate: WispRuntime) -> None:
        """Adopt configured providers and release the displaced provider adapters.

        The adopted providers transfer from ``candidate`` to this runtime so closing
        the temporary candidate cannot close clients now owned by the live runtime.
        Provider registrations that exist only in the live runtime remain available.
        """

        previous_configured = dict(self._configured_provider_items())
        providers = self.providers_for_configuration(candidate)
        retained_ids = {id(provider) for provider in providers}
        retained = {
            name: provider
            for name, provider in previous_configured.items()
            if id(provider) in retained_ids
        }
        transferred = {
            name: provider
            for name, provider in candidate._configured_provider_items()
            if id(provider) in retained_ids
        }
        adopted = {**retained, **transferred}
        # Only names that survive as deferrals: anything the adoption resolved to a
        # concrete provider (an extension override, or a transferred instance) must
        # keep that provider rather than fall back to a factory.
        resolved = {provider.name for provider in providers}
        carried = {
            name: factory
            for name in self.providers.names()
            if name not in resolved
            and (factory := self._deferred_after_adoption(candidate, name)) is not None
        }
        self.providers.replace_all(providers, deferred=carried)
        self._configured_providers.clear()
        self._configured_providers.update(adopted)
        self._configured_names.clear()
        self._configured_names.extend(name for name in self.providers.names() if name in adopted)
        for name in transferred:
            candidate._configured_providers.pop(name, None)
            with suppress(ValueError):
                candidate._configured_names.remove(name)
        displaced = tuple(
            provider
            for provider in previous_configured.values()
            if id(provider) not in retained_ids
        )
        await _close_providers(displaced)

    async def aclose(self) -> None:
        """Release runtime-owned providers, MCP connections, and managed processes."""

        try:
            await _close_providers(
                tuple(provider for _name, provider in self._configured_provider_items())
            )
            self._configured_providers.clear()
            self._configured_names.clear()
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
