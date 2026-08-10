from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident

import anyio
import pytest

import wisp.runtime.extensions as runtime_extensions
from wisp.auth.storage import JsonAuthStore
from wisp.events import AgentStarted
from wisp.providers.anthropic import AnthropicProvider
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.catalog import ModelRegistry, effective_catalog
from wisp.providers.fake import FakeProvider
from wisp.providers.google import GoogleProvider
from wisp.providers.openai import OpenAIProvider
from wisp.providers.openai_codex import OpenAICodexProvider
from wisp.retry import RetryPolicy
from wisp.runtime import (
    CommandDescriptor,
    CommandRegistry,
    EventBus,
    ExtensionAPI,
    ProviderRegistry,
    ToolPromptMetadata,
    ToolRegistry,
    UnknownProviderError,
    UnknownToolError,
    WispRuntime,
)
from wisp.runtime.extensions import activate_builtin_extensions, activate_extensions, build_runtime
from wisp.tools.builtin import ReadTool
from wisp.tools.search import FindTool, GrepTool
from wisp.tools.selection import select_tools
from wisp.tools.shell import BashTool


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


def test_tool_prompt_metadata_rejects_non_string_guidance() -> None:
    with pytest.raises(TypeError, match="snippet must be a string"):
        ToolPromptMetadata(prompt_snippet=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="guidelines must be strings"):
        ToolPromptMetadata(guidelines=(object(),))  # type: ignore[arg-type]


def test_tool_registry_returns_provider_tool_specs() -> None:
    registry = ToolRegistry()
    tool = ReadTool()

    registry.register(tool)

    assert registry.specs()[0].name == "read"
    assert registry.specs()[0].description == tool.description
    assert registry.specs()[0].input_schema == tool.input_schema


def test_tool_registry_keeps_prompt_metadata_separate_from_provider_specs() -> None:
    registry = ToolRegistry()
    tool = ReadTool()
    prompt = ToolPromptMetadata(
        prompt_snippet="Read only the relevant section.",
        guidelines=("Prefer dedicated tools.",),
    )

    registry.register(tool, prompt=prompt)

    assert registry.prompt_metadata(("read",)) == (prompt,)
    assert registry.prompt_metadata(("missing",)) == ()
    assert registry.specs()[0].description == tool.description
    assert not hasattr(registry.specs()[0], "prompt_snippet")


def test_tool_registry_replacement_clears_stale_prompt_metadata() -> None:
    registry = ToolRegistry()
    registry.register(
        ReadTool(),
        prompt=ToolPromptMetadata(prompt_snippet="Old guidance."),
    )

    registry.register(ReadTool())

    assert registry.prompt_metadata(("read",)) == ()


def test_tool_selection_preserves_metadata_only_for_selected_tools() -> None:
    registry = ToolRegistry()
    read_prompt = ToolPromptMetadata(prompt_snippet="Read narrowly.")
    bash_prompt = ToolPromptMetadata(prompt_snippet="Check command status.")
    registry.register(ReadTool(), prompt=read_prompt)
    registry.register(BashTool(), prompt=bash_prompt)

    filtered = select_tools(registry, allow_read_tools=True)

    assert filtered.names() == ("read",)
    assert filtered.prompt_metadata(("read", "bash")) == (read_prompt,)


def test_tool_selection_ignores_unknown_names_from_failed_startup_extension() -> None:
    registry = ToolRegistry()
    registry.register(ReadTool())

    filtered = select_tools(
        registry,
        allowed_tools=("mcp__broken__search",),
        ignored_unknown_prefixes=("mcp__broken__",),
    )

    assert filtered.names() == ()


def test_tool_registry_raises_for_unknown_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(UnknownToolError, match="Unknown tool: missing"):
        registry.get("missing")


def test_extension_api_registers_provider_and_tool() -> None:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    commands = CommandRegistry()
    event_bus = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, commands=commands, events=event_bus)

    api.register_provider(FakeProvider())
    prompt = ToolPromptMetadata(prompt_snippet="Read narrowly.")
    api.register_tool(ReadTool(), prompt=prompt)
    api.register_command(
        CommandDescriptor(
            name="help",
            title="Help",
            description="Show help",
        )
    )

    assert providers.names() == ("fake",)
    assert tools.names() == ("read",)
    assert tools.prompt_metadata(("read",)) == (prompt,)
    assert commands.names() == ("help",)


def test_activate_extensions_runs_extension_factories() -> None:
    async def run() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        providers = ProviderRegistry()
        tools = ToolRegistry()
        commands = CommandRegistry()
        event_bus = EventBus()
        api = ExtensionAPI(providers=providers, tools=tools, commands=commands, events=event_bus)

        def extension(api: ExtensionAPI) -> None:
            api.register_provider(FakeProvider())
            api.register_tool(ReadTool())
            api.register_command(
                CommandDescriptor(
                    name="help",
                    title="Help",
                    description="Show help",
                )
            )

        await activate_extensions(api, [extension])
        return providers.names(), tools.names(), commands.names()

    assert anyio.run(run) == (("fake",), ("read",), ("help",))


def test_build_runtime_cancel_abandons_catalog_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    started = Event()
    release = Event()
    original_effective_catalog = runtime_extensions.effective_catalog

    def blocked_effective_catalog() -> object:
        started.set()
        release.wait(timeout=5)
        return original_effective_catalog(home_dir=Path("/nonexistent-test-home"))

    monkeypatch.setattr(runtime_extensions, "effective_catalog", blocked_effective_catalog)

    async def scenario() -> None:
        cancel_scope = anyio.CancelScope()
        cancelled = anyio.Event()

        async def build() -> None:
            with cancel_scope:
                try:
                    await build_runtime()
                except anyio.get_cancelled_exc_class():
                    cancelled.set()
                    raise
            if cancel_scope.cancel_called:
                cancelled.set()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(build)
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                cancel_scope.cancel()
                with anyio.fail_after(1):
                    await cancelled.wait()
        finally:
            release.set()

    anyio.run(scenario)


def test_build_runtime_activates_builtin_providers_tools_and_commands() -> None:
    async def run() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        runtime = await build_runtime()
        return runtime.providers.names(), runtime.tools.names(), runtime.commands.names()

    assert anyio.run(run) == (
        ("fake", "openai", "openai-codex", "anthropic", "google"),
        ("read", "write", "edit", "bash", "grep", "find", "ls", "skill"),
        (
            "help",
            "compact",
            "context",
            "history",
            "update",
            "skills",
            "mcp",
            "plan",
            "build",
            "model",
            "new",
            "resume",
            "provider",
            "auth",
            "connect",
            "disconnect",
            "quit",
        ),
    )


def test_build_runtime_offloads_catalog_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    main_thread = get_ident()
    catalog_threads: list[int] = []
    original_effective_catalog = runtime_extensions.effective_catalog

    def effective_catalog_in_worker() -> object:
        catalog_threads.append(get_ident())
        return original_effective_catalog(home_dir=Path("/nonexistent-test-home"))

    monkeypatch.setattr(runtime_extensions, "effective_catalog", effective_catalog_in_worker)

    async def scenario() -> None:
        runtime = await build_runtime()
        await runtime.aclose()

    anyio.run(scenario)

    assert catalog_threads and all(thread_id != main_thread for thread_id in catalog_threads)


def test_build_runtime_wires_process_tools_to_runtime_supervisor_and_closes_it() -> None:
    async def run() -> None:
        runtime = await build_runtime()
        bash = runtime.tools.get("bash")
        grep = runtime.tools.get("grep")
        find = runtime.tools.get("find")

        assert isinstance(bash, BashTool)
        assert isinstance(grep, GrepTool)
        assert isinstance(find, FindTool)
        assert bash._process_supervisor is runtime.process_supervisor  # noqa: SLF001
        assert grep._process_supervisor is runtime.process_supervisor  # noqa: SLF001
        assert find._process_supervisor is runtime.process_supervisor  # noqa: SLF001

        await runtime.aclose()
        await runtime.aclose()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await runtime.process_supervisor.start("true", cwd=Path.cwd(), timeout=1)

    anyio.run(run)


def test_direct_runtime_construction_uses_extension_api_command_registry() -> None:
    async def run() -> tuple[str, ...]:
        template = await build_runtime()
        providers = ProviderRegistry()
        tools = ToolRegistry()
        events = EventBus()
        api = ExtensionAPI(providers=providers, tools=tools, events=events)
        runtime = WispRuntime(
            providers=providers,
            tools=tools,
            events=events,
            api=api,
            models=template.models,
        )

        api.register_command(
            CommandDescriptor(
                name="help",
                title="Help",
                description="Show help",
            )
        )
        return runtime.commands.names()

    assert anyio.run(run) == ("help",)


def test_direct_runtime_activation_wires_process_tools_to_runtime_supervisor() -> None:
    async def run() -> None:
        providers = ProviderRegistry()
        tools = ToolRegistry()
        events = EventBus()
        api = ExtensionAPI(providers=providers, tools=tools, events=events)
        runtime = WispRuntime(
            providers=providers,
            tools=tools,
            events=events,
            api=api,
            models=ModelRegistry(effective_catalog()),
        )

        await activate_builtin_extensions(runtime.api)
        bash = runtime.tools.get("bash")
        grep = runtime.tools.get("grep")
        find = runtime.tools.get("find")

        assert isinstance(bash, BashTool)
        assert isinstance(grep, GrepTool)
        assert isinstance(find, FindTool)
        assert bash._process_supervisor is runtime.process_supervisor  # noqa: SLF001
        assert grep._process_supervisor is runtime.process_supervisor  # noqa: SLF001
        assert find._process_supervisor is runtime.process_supervisor  # noqa: SLF001

        await runtime.aclose()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await runtime.process_supervisor.start("true", cwd=Path.cwd(), timeout=1)

    anyio.run(run)


def test_build_runtime_passes_retry_policy_to_builtin_providers() -> None:
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=2)

    async def run() -> RetryPolicy:
        runtime = await build_runtime(retry_policy=policy)
        provider = runtime.providers.get("openai")
        assert isinstance(provider, OpenAIProvider)
        return provider._retry_policy  # noqa: SLF001

    assert anyio.run(run) == policy


def test_build_runtime_passes_stored_auth_resolver_to_builtin_providers(
    tmp_path: Path,
) -> None:
    auth_path = tmp_path / "auth.json"

    async def run() -> None:
        runtime = await build_runtime(auth_path=auth_path)
        providers = (
            (runtime.providers.get("openai"), OpenAIProvider),
            (runtime.providers.get("openai-codex"), OpenAICodexProvider),
            (runtime.providers.get("anthropic"), AnthropicProvider),
            (runtime.providers.get("google"), GoogleProvider),
        )
        resolvers = []
        for provider, provider_type in providers:
            assert isinstance(provider, provider_type)
            resolver = provider._auth_resolver  # noqa: SLF001
            assert isinstance(resolver, StoredProviderAuthResolver)
            assert isinstance(resolver.store, JsonAuthStore)
            assert resolver.store.path == auth_path
            resolvers.append(resolver)
        assert all(resolver is resolvers[0] for resolver in resolvers[1:])
        await runtime.aclose()

    anyio.run(run)


def test_direct_runtime_construction_captures_configured_providers() -> None:
    async def run() -> None:
        template = await build_runtime()
        current_providers = ProviderRegistry()
        current_provider = FakeProvider()
        current_providers.register(current_provider)
        current_tools = ToolRegistry()
        current_events = EventBus()
        current = WispRuntime(
            providers=current_providers,
            tools=current_tools,
            events=current_events,
            api=ExtensionAPI(
                providers=current_providers,
                tools=current_tools,
                events=current_events,
            ),
            models=template.models,
        )
        candidate_providers = ProviderRegistry()
        candidate_provider = FakeProvider()
        candidate_providers.register(candidate_provider)
        candidate_tools = ToolRegistry()
        candidate_events = EventBus()
        candidate = WispRuntime(
            providers=candidate_providers,
            tools=candidate_tools,
            events=candidate_events,
            api=ExtensionAPI(
                providers=candidate_providers,
                tools=candidate_tools,
                events=candidate_events,
            ),
            models=template.models,
        )

        current.adopt_provider_configuration(candidate)

        assert current.providers.get("fake") is candidate_provider

    anyio.run(run)


def test_event_bus_emits_to_named_and_wildcard_handlers() -> None:
    seen: list[str] = []

    async def run() -> None:
        event_bus = EventBus()
        event_bus.on("agent.started", lambda event: seen.append(f"named:{event.type}"))
        event_bus.on("*", lambda event: seen.append(f"wildcard:{event.type}"))

        await event_bus.emit(AgentStarted(session_id="test-session"))

    anyio.run(run)

    assert seen == ["named:agent.started", "wildcard:agent.started"]
