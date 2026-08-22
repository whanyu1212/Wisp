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
    _configured_registrations: dict[str, object] = field(default_factory=dict, repr=False)

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
        self._configured_registrations.clear()
        self._configured_registrations.update(
            (name, token)
            for name in self._configured_names
            if (token := self.providers.registration_token(name)) is not None
        )

    def _configured_provider_items(self) -> tuple[tuple[str, Provider], ...]:
        """Return configured providers that exist, in registration order.

        Deferred entries are omitted rather than constructed: every caller either
        compares identity (which a fresh instance would fail anyway) or closes a
        client (which a never-built provider does not hold).
        """

        constructed = self.providers.constructed()
        items: list[tuple[str, Provider]] = []
        for name in self._configured_names:
            provider = self._configured_providers.get(name)
            registration_unchanged = self.providers.registration_token(
                name
            ) is self._configured_registrations.get(name)
            if provider is None and registration_unchanged:
                # A provider built from its configured factory after the initial
                # snapshot remains configuration-owned. A direct registry or API
                # replacement changes the token and therefore remains extension-owned.
                provider = constructed.get(name)
            if provider is not None:
                items.append((name, provider))
        return tuple(items)

    def _configured_names_or_all(self) -> tuple[str, ...]:
        """Return the configured provider names, falling back to every registration."""

        return tuple(self._configured_names) or self.providers.names()

    def providers_for_configuration(self, candidate: WispRuntime) -> tuple[Provider, ...]:
        """Return the provider set produced by adopting ``candidate`` safely.

        Deferred names participate as names. A candidate that has not yet constructed
        a provider still *owns* that configuration -- its auth path, retry policy, and
        endpoint -- so skipping it would silently retain the live runtime's stale
        adapter across a refresh.
        """

        candidate_names = candidate._configured_names_or_all()
        candidate_constructed = candidate.providers.constructed()
        remaining = list(candidate_names)
        configured = dict(self._configured_provider_items())
        providers: list[Provider] = []
        for name in self.providers.names():
            replaces = name in remaining
            if replaces:
                remaining.remove(name)
            existing = self.providers.constructed().get(name)
            if existing is None:
                registration_unchanged = self.providers.registration_token(
                    name
                ) is self._configured_registrations.get(name)
                # A still-deferred configured registration can be refreshed from the
                # candidate. If the token changed, a live extension replaced it with
                # its own factory; resolve and preserve that override instead.
                providers.append(
                    candidate.providers.get(name)
                    if replaces and registration_unchanged
                    else self.providers.get(name)
                )
                continue
            # Configuration owns this name unless an extension replaced the instance
            # it recorded. A provider the runtime built *after* capture is still
            # configuration-owned: it came from the configured factory, and only an
            # extension registering a different instance transfers ownership away.
            recorded = configured.get(name)
            configuration_owned = recorded is existing or (
                name in self._configured_names
                and self.providers.registration_token(name)
                is self._configured_registrations.get(name)
            )
            if configuration_owned and replaces:
                providers.append(candidate_constructed.get(name) or candidate.providers.get(name))
            else:
                providers.append(existing)
        # Names the candidate contributes that the live runtime never registered.
        for name in remaining:
            providers.append(candidate.providers.get(name))
        return tuple(providers)

    async def adopt_provider_configuration(self, candidate: WispRuntime) -> None:
        """Adopt configured providers and release the displaced provider adapters.

        The adopted providers transfer from ``candidate`` to this runtime so closing
        the temporary candidate cannot close clients now owned by the live runtime.
        Provider registrations that exist only in the live runtime remain available.
        """

        providers = self.providers_for_configuration(candidate)
        # Planning may construct a configured provider that exists only in the live
        # registry. Capture ownership afterwards so that preserved instance remains
        # refreshable and closable.
        previous_configured = dict(self._configured_provider_items())
        retained_ids = {id(provider) for provider in providers}
        retained = {
            name: provider
            for name, provider in previous_configured.items()
            if id(provider) in retained_ids
        }
        # Keyed by the candidate's configured *names*, not just the instances it had
        # recorded: a deferred provider resolved during this adoption was constructed
        # after the candidate's snapshot, so it is absent from that mapping. Omitting
        # it would drop the name from configured ownership here, leaving the next
        # refresh unable to replace the adapter and nobody responsible for closing it.
        adopted_by_name = {provider.name: provider for provider in providers}
        candidate_constructed = candidate.providers.constructed()
        transferred = {}
        for name in candidate._configured_names_or_all():
            adopted_provider = adopted_by_name.get(name)
            if adopted_provider is None or id(adopted_provider) not in retained_ids:
                continue
            candidate_registration_unchanged = candidate.providers.registration_token(
                name
            ) is candidate._configured_registrations.get(name)
            if (
                candidate_constructed.get(name) is not adopted_provider
                or not candidate_registration_unchanged
            ):
                # The candidate stayed deferred because a live extension override
                # won, its provider was otherwise masked, or the selected instance is
                # itself an extension override. None is configured ownership to
                # transfer; extension-owned instances remain outside runtime cleanup.
                continue
            transferred[name] = adopted_provider
        adopted = {**retained, **transferred}
        # Registration order of the adopted runtime: live names first, then any the
        # candidate contributes, so `names()` keeps promising registration order.
        order = [
            *self.providers.names(),
            *(
                name
                for name in candidate._configured_names_or_all()
                if name not in self.providers.names()
            ),
        ]
        self.providers.replace_all(providers, order=order)
        self._configured_providers.clear()
        self._configured_providers.update(adopted)
        self._configured_names.clear()
        self._configured_names.extend(name for name in self.providers.names() if name in adopted)
        self._configured_registrations.clear()
        self._configured_registrations.update(
            (name, token)
            for name in self._configured_names
            if (token := self.providers.registration_token(name)) is not None
        )
        for name in transferred:
            candidate._configured_providers.pop(name, None)
            candidate._configured_registrations.pop(name, None)
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
            self._configured_registrations.clear()
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
