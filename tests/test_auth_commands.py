"""Unit tests for the AuthCommands collaborator, in isolation from the shell.

The shell-integration tests (test_tui_shell_core) drive these through a full
TuiShell; these pin the collaborator's contract directly with fakes, including
the callable-dependency behavior that keeps it correct across a mid-session
auth_store rebind and a changing default provider.
"""

from __future__ import annotations

import anyio
from pytest import MonkeyPatch

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


def _commands(store: _FakeStore, default: str = "openai") -> tuple[AuthCommands, _FakeRenderer]:
    renderer = _FakeRenderer()
    commands = AuthCommands(renderer, lambda: store, lambda: default)
    return commands, renderer


def test_status_reports_not_logged_in_for_missing_credential() -> None:
    commands, renderer = _commands(_FakeStore())
    commands.status(())
    assert renderer.notices == ["openai: not logged in"]  # used the default provider
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


def test_disconnect_deletes_and_reports_presence() -> None:
    store = _FakeStore({"openai": ApiKeyCredential(key="sk-1")})
    commands, renderer = _commands(store)
    commands.disconnect(("openai",))
    assert renderer.notices == ["Disconnected: openai"]
    commands.disconnect(("openai",))  # already gone
    assert renderer.notices[-1] == "Not connected: openai"


def test_connect_rejects_unknown_providers() -> None:
    commands, renderer = _commands(_FakeStore(), default="openai")

    async def run() -> None:
        await commands.connect(("missing",))

    anyio.run(run)
    assert renderer.errors == [
        "Unknown provider. Choose one of: openai-codex, openai, openai-compatible, "
        "anthropic, google."
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
        "Stored API key for anthropic; ANTHROPIC_API_KEY still takes precedence. "
        "Unset it in your shell to use the stored key."
    ]


def test_disconnect_removes_stored_credentials_hidden_by_environment(
    monkeypatch: MonkeyPatch,
) -> None:
    store = _FakeStore({"openai": ApiKeyCredential(key="stored")})
    commands, renderer = _commands(store)
    monkeypatch.setenv("OPENAI_API_KEY", "environment")

    commands.disconnect(("openai",))

    assert store.credentials == {}
    assert renderer.notices == [
        "Removed stored credentials for openai; still connected through OPENAI_API_KEY. "
        "Unset it in your shell to disconnect."
    ]


def test_disconnect_cannot_remove_environment_only_credentials(monkeypatch: MonkeyPatch) -> None:
    store = _FakeStore()
    commands, renderer = _commands(store)
    monkeypatch.setenv("OPENAI_API_KEY", "environment")

    commands.disconnect(("openai",))

    assert renderer.errors == [
        "openai is connected through OPENAI_API_KEY; unset it in your shell."
    ]


def test_google_auth_status_recognizes_gemini_api_key(monkeypatch: MonkeyPatch) -> None:
    commands, renderer = _commands(_FakeStore(), default="google")
    monkeypatch.setenv("GEMINI_API_KEY", "environment")

    commands.status(())

    assert renderer.notices == ["google: api key configured via GEMINI_API_KEY"]
    google = next(
        family
        for family in commands._connection_catalog()
        if family.id == "google"  # noqa: SLF001
    )
    assert google.methods[0].source == "environment"
    assert google.methods[0].environment_variable == "GEMINI_API_KEY"


def test_default_provider_is_read_lazily_each_call() -> None:
    # The default provider tracks live shell state; AuthCommands must read it per
    # call, not capture it once.
    store = _FakeStore()
    renderer = _FakeRenderer()
    current = {"provider": "openai"}
    commands = AuthCommands(renderer, lambda: store, lambda: current["provider"])
    commands.status(())
    current["provider"] = "anthropic"
    commands.status(())
    assert renderer.notices == ["openai: not logged in", "anthropic: not logged in"]


def test_store_is_read_lazily_so_a_rebind_is_honored() -> None:
    # The shell rebinds auth_store on a trusted-project rebuild; passing a callable
    # (not a captured instance) means AuthCommands sees the new store.
    first = _FakeStore({"openai": ApiKeyCredential(key="old")})
    second = _FakeStore(
        {"openai": OAuthCredential(access="a", refresh="r", expires=4_102_444_800_000)}
    )
    active = {"store": first}
    renderer = _FakeRenderer()
    commands = AuthCommands(renderer, lambda: active["store"], lambda: "openai")
    commands.status(())
    active["store"] = second  # simulate the mid-session rebind
    commands.status(())
    assert renderer.notices[0] == "openai: api key configured"
    assert renderer.notices[1].startswith("openai: oauth configured")
