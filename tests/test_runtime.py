from __future__ import annotations

import anyio
import pytest

from wisp.events import AgentStarted
from wisp.providers.fake import FakeProvider
from wisp.runtime import EventBus, ExtensionAPI, ProviderRegistry, UnknownProviderError
from wisp.runtime.extensions import activate_extensions, build_runtime


def test_provider_registry_registers_and_resolves_provider() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider()

    registry.register(provider)

    assert registry.get("fake") is provider
    assert registry.names() == ("fake",)


def test_provider_registry_raises_for_unknown_provider() -> None:
    registry = ProviderRegistry()

    with pytest.raises(UnknownProviderError, match="Unknown provider: missing"):
        registry.get("missing")


def test_extension_api_registers_provider() -> None:
    providers = ProviderRegistry()
    event_bus = EventBus()
    api = ExtensionAPI(providers=providers, events=event_bus)

    api.register_provider(FakeProvider())

    assert providers.names() == ("fake",)


def test_activate_extensions_runs_extension_factories() -> None:
    async def run() -> tuple[str, ...]:
        providers = ProviderRegistry()
        event_bus = EventBus()
        api = ExtensionAPI(providers=providers, events=event_bus)

        def extension(api: ExtensionAPI) -> None:
            api.register_provider(FakeProvider())

        await activate_extensions(api, [extension])
        return providers.names()

    assert anyio.run(run) == ("fake",)


def test_build_runtime_activates_builtin_providers() -> None:
    async def run() -> tuple[str, ...]:
        runtime = await build_runtime()
        return runtime.providers.names()

    assert anyio.run(run) == ("fake", "openai")


def test_event_bus_emits_to_named_and_wildcard_handlers() -> None:
    seen: list[str] = []

    async def run() -> None:
        event_bus = EventBus()
        event_bus.on("agent.started", lambda event: seen.append(f"named:{event.type}"))
        event_bus.on("*", lambda event: seen.append(f"wildcard:{event.type}"))

        await event_bus.emit(AgentStarted(session_id="test-session"))

    anyio.run(run)

    assert seen == ["named:agent.started", "wildcard:agent.started"]
