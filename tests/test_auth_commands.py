"""Unit tests for the AuthCommands collaborator, in isolation from the shell."""

from __future__ import annotations

import anyio
from pytest import MonkeyPatch

from wisp.auth.connections import (
    ConnectionMethodStatus,
    ConnectionProviderStatus,
    ConnectionSource,
    connection_catalog,
)
from wisp.auth.storage import ApiKeyCredential, AuthStorageError, OAuthCredential
from wisp.tui.auth_commands import AuthCommands


class _FakeRenderer:
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.errors: list[str] = []

    def notice(self, message: str) -> None:
        self.notices.append(message)

    def command_error(self, message: str) -> None:
        self.errors.append(message)


class _FakeStore:
    def __init__(self, credentials: dict[str, object] | None = None) -> None:
        self.credentials = dict(credentials or {})
        self.raise_on: str | None = None

    def get(self, provider: str) -> object | None:
        if self.raise_on == "get":
            raise AuthStorageError("boom")
        return self.credentials.get(provider)

    def set(self, provider: str, credential: object) -> None:
        if self.raise_on == "set":
            raise AuthStorageError("boom")
        self.credentials[provider] = credential

    def delete(self, provider: str) -> bool:
        if self.raise_on == "delete":
            raise AuthStorageError("boom")
        return self.credentials.pop(provider, None) is not None


def _commands(
    store: _FakeStore,
    default: str = "openai",
    *,
    openai_compatible_provider: str = "openai-compatible",
) -> tuple[AuthCommands, _FakeRenderer]:
    renderer = _FakeRenderer()

    async def store_api_key(provider: str, api_key: str) -> None:
        store.set(provider, ApiKeyCredential(key=api_key))

    async def disconnect_provider(provider: str) -> None:
        store.delete(provider)

    async def begin_device_code(provider: str) -> None:
        store.set(
            provider,
            OAuthCredential(access="a", refresh="r", expires=4_102_444_800_000),
        )

    commands = AuthCommands(
        renderer,
        lambda: connection_catalog(
            store,
            openai_compatible_provider=openai_compatible_provider,
        ),
        lambda: default,
        store_api_key=store_api_key,
        disconnect_provider=disconnect_provider,
        begin_device_code=begin_device_code,
        openai_compatible_provider=openai_compatible_provider,
    )
    return commands, renderer


def test_status_reports_not_logged_in_for_missing_credential() -> None:
    commands, renderer = _commands(_FakeStore())
    commands.status(())
    assert renderer.notices == ["openai: not logged in"]
    assert renderer.errors == []


def test_connection_catalog_omits_unrepresentable_oauth_expiry() -> None:
    store = _FakeStore(
        {
            "openai-codex": OAuthCredential(
                access="access",
                refresh="refresh",
                expires=10**1000,
            )
        }
    )

    catalog = connection_catalog(store, environ=lambda _name: None)
    method = catalog[0].methods[0]

    assert method.source == "stored"
    assert method.has_stored_credential is True
    assert method.oauth_expires_at is None


def test_auth_notices_use_backend_catalog_instead_of_frontend_environment(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "frontend-only")
    source: dict[str, ConnectionSource] = {"value": "missing"}
    has_stored_credential = {"value": False}
    renderer = _FakeRenderer()

    def catalog() -> tuple[ConnectionProviderStatus, ...]:
        return (
            ConnectionProviderStatus(
                id="openai",
                label="OpenAI",
                methods=(
                    ConnectionMethodStatus(
                        provider="openai",
                        label="OpenAI API key",
                        kind="api_key",
                        source=source["value"],
                        environment_variable="OPENAI_API_KEY",
                        has_stored_credential=has_stored_credential["value"],
                    ),
                ),
            ),
        )

    async def store_api_key(_provider: str, _api_key: str) -> None:
        source["value"] = "environment"
        has_stored_credential["value"] = True

    async def disconnect_provider(_provider: str) -> None:
        source["value"] = "missing"
        has_stored_credential["value"] = False

    commands = AuthCommands(
        renderer,
        catalog,
        lambda: "openai",
        store_api_key=store_api_key,
        disconnect_provider=disconnect_provider,
        begin_device_code=lambda _provider: anyio.sleep(0),
    )

    async def run() -> None:
        commands.status(())
        await commands.connect_api_key("openai", "stored-key")
        await commands.disconnect(("openai",))

    anyio.run(run)

    assert renderer.notices == [
        "openai: not logged in",
        "Stored API key for openai; OPENAI_API_KEY currently takes precedence. "
        "Unset all API-key environment variables for openai to use the stored key.",
        "Disconnected: openai",
    ]
    assert renderer.errors == []


def test_status_reports_api_key_and_oauth_shapes() -> None:
    store = _FakeStore(
        {
            "openai": ApiKeyCredential(key="sk-123"),
            "openai-codex": OAuthCredential(access="a", refresh="r", expires=4_102_444_800_000),
        }
    )
    commands, renderer = _commands(store)
    commands.status(("openai",))
    commands.status(("openai-codex",))
    assert renderer.notices[0] == "openai: api key configured"
    assert renderer.notices[1].startswith("openai-codex: oauth configured (expires ")


def test_status_rejects_extra_args_and_surfaces_storage_errors() -> None:
    store = _FakeStore()
    commands, renderer = _commands(store)
    commands.status(("a", "b"))
    assert renderer.errors == ["Usage: /auth [provider]"]
    store.raise_on = "get"
    commands.status(("openai",))
    assert renderer.errors[-1].startswith("Auth storage error:")


def test_connect_acknowledges_store_when_status_refresh_fails() -> None:
    renderer = _FakeRenderer()
    stored: list[tuple[str, str]] = []

    async def store_api_key(provider: str, api_key: str) -> None:
        stored.append((provider, api_key))

    commands = AuthCommands(
        renderer,
        lambda: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
        lambda: "openai",
        store_api_key=store_api_key,
        disconnect_provider=lambda _provider: anyio.sleep(0),
        begin_device_code=lambda _provider: anyio.sleep(0),
    )

    anyio.run(commands.connect_api_key, "openai", "stored-key")

    assert stored == [("openai", "stored-key")]
    assert renderer.notices == ["Stored API key for openai; connection status refresh unavailable."]
    assert renderer.errors == []


def test_disconnect_deletes_and_reports_presence() -> None:
    store = _FakeStore({"openai": ApiKeyCredential(key="sk-1")})
    commands, renderer = _commands(store)

    async def run() -> None:
        await commands.disconnect(("openai",))
        await commands.disconnect(("openai",))

    anyio.run(run)
    assert renderer.notices == ["Disconnected: openai", "Disconnected: openai"]


def test_disconnect_acknowledges_mutation_when_status_refresh_fails() -> None:
    renderer = _FakeRenderer()
    catalog_calls = 0
    disconnected: list[str] = []

    def catalog() -> tuple[ConnectionProviderStatus, ...]:
        nonlocal catalog_calls
        catalog_calls += 1
        if catalog_calls > 1:
            raise RuntimeError("catalog unavailable")
        return connection_catalog(_FakeStore(), environ=lambda _name: None)

    async def disconnect_provider(provider: str) -> None:
        disconnected.append(provider)

    commands = AuthCommands(
        renderer,
        catalog,
        lambda: "openai",
        store_api_key=lambda _provider, _api_key: anyio.sleep(0),
        disconnect_provider=disconnect_provider,
        begin_device_code=lambda _provider: anyio.sleep(0),
    )

    anyio.run(commands.disconnect, ("openai",))

    assert disconnected == ["openai"]
    assert renderer.notices == [
        "Stored credentials cleared for openai; connection status refresh unavailable."
    ]
    assert renderer.errors == []


def test_connect_rejects_unknown_providers() -> None:
    commands, renderer = _commands(_FakeStore(), default="openai")

    async def run() -> None:
        await commands.connect(("missing",))

    anyio.run(run)
    assert renderer.errors == [
        "Unknown provider. Choose one of: openai-codex, openai, openai-compatible, "
        "xai, deepseek, anthropic, google."
    ]


def test_connect_rejects_extra_arguments() -> None:
    commands, renderer = _commands(_FakeStore(), default="openai-codex")

    async def run() -> None:
        await commands.connect(("openai-codex", "device-code"))

    anyio.run(run)
    assert renderer.errors == ["Usage: /connect [provider]"]


def test_connect_api_key_stores_secret_without_rendering_it() -> None:
    store = _FakeStore()
    commands, renderer = _commands(store)

    async def run() -> None:
        await commands.connect_api_key("anthropic", "  sentinel-secret  ")

    anyio.run(run)
    assert store.credentials == {"anthropic": ApiKeyCredential(key="sentinel-secret")}
    assert renderer.notices == ["Connected: anthropic"]
    assert "sentinel-secret" not in repr(renderer.notices)


def test_connect_api_key_reports_environment_precedence(monkeypatch: MonkeyPatch) -> None:
    store = _FakeStore()
    commands, renderer = _commands(store)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment")

    async def run() -> None:
        await commands.connect_api_key("anthropic", "stored-key")

    anyio.run(run)

    assert store.credentials == {"anthropic": ApiKeyCredential(key="stored-key")}
    assert renderer.notices == [
        "Stored API key for anthropic; ANTHROPIC_API_KEY currently takes precedence. "
        "Unset all API-key environment variables for anthropic to use the stored key."
    ]


def test_disconnect_removes_stored_credentials_hidden_by_environment(
    monkeypatch: MonkeyPatch,
) -> None:
    store = _FakeStore({"openai": ApiKeyCredential(key="stored")})
    commands, renderer = _commands(store)
    monkeypatch.setenv("OPENAI_API_KEY", "environment")

    async def run() -> None:
        await commands.disconnect(("openai",))

    anyio.run(run)

    assert store.credentials == {}
    assert renderer.notices == [
        "Stored credentials cleared for openai; still connected through OPENAI_API_KEY. "
        "Unset all API-key environment variables for openai to disconnect."
    ]


def test_disconnect_reports_environment_only_credentials(monkeypatch: MonkeyPatch) -> None:
    store = _FakeStore()
    commands, renderer = _commands(store)
    monkeypatch.setenv("OPENAI_API_KEY", "environment")

    async def run() -> None:
        await commands.disconnect(("openai",))

    anyio.run(run)

    assert renderer.notices == [
        "Stored credentials cleared for openai; still connected through OPENAI_API_KEY. "
        "Unset all API-key environment variables for openai to disconnect."
    ]
    assert renderer.errors == []


def test_xai_auth_status_recognizes_xai_api_key(monkeypatch: MonkeyPatch) -> None:
    commands, renderer = _commands(_FakeStore(), default="xai")
    monkeypatch.setenv("XAI_API_KEY", "environment")

    commands.status(())

    assert renderer.notices == ["xai: api key configured via XAI_API_KEY"]
    xai = next(family for family in commands._connection_catalog() if family.id == "xai")
    assert xai.label == "xAI"
    assert xai.methods[0].label == "xAI API key"
    assert xai.methods[0].source == "environment"


def test_deepseek_auth_status_recognizes_api_key(monkeypatch: MonkeyPatch) -> None:
    commands, renderer = _commands(_FakeStore(), default="deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment")

    commands.status(())

    assert renderer.notices == ["deepseek: api key configured via DEEPSEEK_API_KEY"]
    deepseek = next(family for family in commands._connection_catalog() if family.id == "deepseek")
    assert deepseek.label == "DeepSeek"
    assert deepseek.methods[0].label == "DeepSeek API key"
    assert deepseek.methods[0].source == "environment"


def test_google_auth_status_recognizes_gemini_api_key(monkeypatch: MonkeyPatch) -> None:
    commands, renderer = _commands(_FakeStore(), default="google")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "environment")

    commands.status(())

    assert renderer.notices == ["google: api key configured via GEMINI_API_KEY"]
    google = next(family for family in commands._connection_catalog() if family.id == "google")
    assert google.methods[0].source == "environment"
    assert google.methods[0].environment_variable == "GEMINI_API_KEY"


def test_custom_provider_auth_uses_named_environment_with_generic_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    store = _FakeStore()
    commands, renderer = _commands(
        store, default="openrouter", openai_compatible_provider="openrouter"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-environment")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fallback-environment")

    commands.status(())
    catalog = commands._connection_catalog()
    custom = next(family for family in catalog if family.id == "openrouter")

    assert renderer.notices == ["openrouter: api key configured via OPENROUTER_API_KEY"]
    assert custom.label == "openrouter"
    assert custom.methods[0].provider == "openrouter"
    assert custom.methods[0].source == "environment"
    assert custom.methods[0].environment_variable == "OPENROUTER_API_KEY"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    renderer.notices.clear()
    commands.status(())

    assert renderer.notices == ["openrouter: api key configured via OPENAI_COMPATIBLE_API_KEY"]


def test_custom_provider_connect_and_disconnect_report_backend_environment_variable(
    monkeypatch: MonkeyPatch,
) -> None:
    store = _FakeStore()
    commands, renderer = _commands(
        store, default="openrouter", openai_compatible_provider="openrouter"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-environment")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "fallback-environment")

    async def run() -> None:
        await commands.connect_api_key("openrouter", "stored-key")
        renderer.notices.clear()
        await commands.disconnect(("openrouter",))

    anyio.run(run)

    assert renderer.notices == [
        "Stored credentials cleared for openrouter; still connected through "
        "OPENROUTER_API_KEY. Unset all API-key environment variables for openrouter "
        "to disconnect."
    ]


def test_custom_provider_connect_stores_under_custom_name() -> None:
    store = _FakeStore()
    commands, renderer = _commands(
        store, default="openrouter", openai_compatible_provider="openrouter"
    )

    async def run() -> None:
        await commands.connect_api_key("openrouter", " custom-secret ")

    anyio.run(run)

    assert store.credentials == {"openrouter": ApiKeyCredential(key="custom-secret")}
    assert renderer.notices == ["Connected: openrouter"]


def test_default_provider_is_read_lazily_each_call() -> None:
    store = _FakeStore()
    renderer = _FakeRenderer()
    current = {"provider": "openai"}

    async def store_api_key(provider: str, api_key: str) -> None:
        store.set(provider, ApiKeyCredential(key=api_key))

    async def disconnect_provider(provider: str) -> None:
        store.delete(provider)

    async def begin_device_code(provider: str) -> None:
        return None

    commands = AuthCommands(
        renderer,
        lambda: connection_catalog(store),
        lambda: current["provider"],
        store_api_key=store_api_key,
        disconnect_provider=disconnect_provider,
        begin_device_code=begin_device_code,
    )
    commands.status(())
    current["provider"] = "anthropic"
    commands.status(())
    assert renderer.notices == ["openai: not logged in", "anthropic: not logged in"]


def test_store_is_read_lazily_so_a_rebind_is_honored() -> None:
    first = _FakeStore({"openai": ApiKeyCredential(key="old")})
    second = _FakeStore()
    active = {"store": first}
    renderer = _FakeRenderer()

    async def store_api_key(provider: str, api_key: str) -> None:
        active["store"].set(provider, ApiKeyCredential(key=api_key))

    async def disconnect_provider(provider: str) -> None:
        active["store"].delete(provider)

    async def begin_device_code(provider: str) -> None:
        return None

    commands = AuthCommands(
        renderer,
        lambda: connection_catalog(active["store"]),
        lambda: "openai",
        store_api_key=store_api_key,
        disconnect_provider=disconnect_provider,
        begin_device_code=begin_device_code,
    )
    commands.status(())
    active["store"] = second
    commands.status(())
    assert renderer.notices[0] == "openai: api key configured"
    assert renderer.notices[1] == "openai: not logged in"
