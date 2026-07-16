from __future__ import annotations

import anyio
import pytest

from wisp.events import AgentStarted
from wisp.providers.fake import FakeProvider
from wisp.providers.openai import OpenAIProvider
from wisp.retry import RetryPolicy
from wisp.runtime import (
    EventBus,
    ExtensionAPI,
    ProviderRegistry,
    ToolRegistry,
    UnknownProviderError,
    UnknownToolError,
)
from wisp.runtime.extensions import activate_extensions, build_runtime
from wisp.tools.builtin import ReadTool


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


def test_tool_registry_registers_and_resolves_tool() -> None:
    registry = ToolRegistry()
    tool = ReadTool()

    registry.register(tool)

    assert registry.get("read") is tool
    assert registry.names() == ("read",)
    assert registry.all() == (tool,)


def test_tool_registry_returns_provider_tool_specs() -> None:
    registry = ToolRegistry()
    tool = ReadTool()

    registry.register(tool)

    assert registry.specs()[0].name == "read"
    assert registry.specs()[0].description == tool.description
    assert registry.specs()[0].input_schema == tool.input_schema


def test_tool_registry_raises_for_unknown_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(UnknownToolError, match="Unknown tool: missing"):
        registry.get("missing")


def test_extension_api_registers_provider_and_tool() -> None:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    event_bus = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=event_bus)

    api.register_provider(FakeProvider())
    api.register_tool(ReadTool())

    assert providers.names() == ("fake",)
    assert tools.names() == ("read",)


def test_activate_extensions_runs_extension_factories() -> None:
    async def run() -> tuple[str, ...]:
        providers = ProviderRegistry()
        tools = ToolRegistry()
        event_bus = EventBus()
        api = ExtensionAPI(providers=providers, tools=tools, events=event_bus)

        def extension(api: ExtensionAPI) -> None:
            api.register_provider(FakeProvider())
            api.register_tool(ReadTool())

        await activate_extensions(api, [extension])
        return providers.names(), tools.names()

    assert anyio.run(run) == (("fake",), ("read",))


def test_build_runtime_activates_builtin_providers_and_tools() -> None:
    async def run() -> tuple[tuple[str, ...], tuple[str, ...]]:
        runtime = await build_runtime()
        return runtime.providers.names(), runtime.tools.names()

    assert anyio.run(run) == (
        ("fake", "openai", "openai-codex", "anthropic"),
        ("read", "write", "edit", "bash", "grep", "find", "ls"),
    )


def test_build_runtime_passes_retry_policy_to_builtin_providers() -> None:
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=2)

    async def run() -> RetryPolicy:
        runtime = await build_runtime(retry_policy=policy)
        provider = runtime.providers.get("openai")
        assert isinstance(provider, OpenAIProvider)
        return provider._retry_policy  # noqa: SLF001

    assert anyio.run(run) == policy


def test_event_bus_emits_to_named_and_wildcard_handlers() -> None:
    seen: list[str] = []

    async def run() -> None:
        event_bus = EventBus()
        event_bus.on("agent.started", lambda event: seen.append(f"named:{event.type}"))
        event_bus.on("*", lambda event: seen.append(f"wildcard:{event.type}"))

        await event_bus.emit(AgentStarted(session_id="test-session"))

    anyio.run(run)

    assert seen == ["named:agent.started", "wildcard:agent.started"]
