"""The `/auth`, `/connect`, `/disconnect` slash-command handlers for the TUI shell.

Credential management is a self-contained concern: it talks to the auth store and
reports through the renderer, and touches none of the shell's conversation or
config-lifecycle state. `AuthCommands` collects those three handlers so the shell
delegates to it rather than carrying them inline.

Dependencies are passed as callables, not captured values, on purpose: the shell
rebinds ``auth_store`` mid-session when a trusted project supplies its own auth
path, and the default provider is derived from live shell state (pending configs
+ current provider). Reading them through ``get_store`` / ``get_default_provider``
keeps this collaborator correct across those changes instead of holding a stale
reference.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

from wisp.auth.openai_codex import DeviceCodeInfo, login_openai_codex_device_code
from wisp.auth.storage import (
    ApiKeyCredential,
    AuthCredential,
    AuthStorageError,
    JsonAuthStore,
    OAuthCredential,
)
from wisp.openai_compatible import openai_compatible_api_key_environment
from wisp.tui.connections import (
    API_KEY_ENVIRONMENT_VARIABLES,
    OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE,
    ConnectionMethodStatus,
    ConnectionProviderStatus,
    ConnectionSource,
)
from wisp.tui.rendering import TuiRenderer


class AuthCommands:
    """Handlers for the credential slash commands, delegated to by the shell."""

    def __init__(
        self,
        renderer: TuiRenderer,
        get_store: Callable[[], JsonAuthStore],
        get_default_provider: Callable[[], str],
        openai_compatible_provider: str = "openai-compatible",
    ) -> None:
        self._renderer = renderer
        self._openai_compatible_provider = openai_compatible_provider
        # Read the store lazily: the shell swaps it on a trusted-project rebuild.
        self._get_store = get_store
        # The default provider tracks live shell state (pending configs + current).
        self._get_default_provider = get_default_provider

    def status(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /auth [provider]")
            return
        provider = args[0] if args else self._get_default_provider()
        environment_variables = self._configured_environment_variables(provider)
        if environment_variables:
            configured_via = ", ".join(environment_variables)
            self._renderer.notice(f"{provider}: api key configured via {configured_via}")
            return
        try:
            credential = self._get_store().get(provider)
        except AuthStorageError as exc:
            self._renderer.command_error(f"Auth storage error: {exc}")
            return
        self._renderer.notice(_auth_status_line(provider, credential))

    async def connect(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /connect [provider]")
            return
        try:
            catalog = self._connection_catalog()
        except AuthStorageError as exc:
            self._renderer.command_error(f"Auth storage error: {exc}")
            return
        if not args:
            self._connect_picker_request(catalog)
            return
        provider = args[0]
        method = _find_method(catalog, provider)
        if method is None:
            choices = ", ".join(item.provider for family in catalog for item in family.methods)
            self._renderer.command_error(f"Unknown provider. Choose one of: {choices}.")
            return
        picker = getattr(self._renderer, "connect_picker_request", None)
        if callable(picker):
            picker(catalog, provider=provider)
            return
        if method.kind == "api_key":
            self._connect_picker_request(catalog, provider=provider)
            return
        await self.connect_oauth("openai-codex")

    async def connect_oauth(self, provider: str) -> None:
        if provider != "openai-codex":
            self._renderer.command_error(f"OAuth connection is not supported for {provider}.")
            return
        await self._connect_openai_codex()

    async def connect_api_key(self, provider: str, api_key: str) -> None:
        """Persist a key received through the renderer's redacted callback."""

        if not self._supports_api_key(provider):
            self._connect_error(f"API-key connection is not supported for {provider}.")
            return
        normalized = api_key.strip()
        if not normalized:
            self._connect_error("API key cannot be empty.")
            return
        try:
            self._get_store().set(provider, ApiKeyCredential(key=normalized))
        except AuthStorageError as exc:
            self._connect_error(f"Auth storage error: {exc}")
            return
        self._call_renderer_optional("connect_completed", provider)
        environment_variables = self._configured_environment_variables(provider)
        if not environment_variables:
            self._renderer.notice(f"Connected: {provider}")
        else:
            names = ", ".join(environment_variables)
            verb = "takes" if len(environment_variables) == 1 else "take"
            pronoun = "it" if len(environment_variables) == 1 else "them"
            self._renderer.notice(
                f"Stored API key for {provider}; {names} still {verb} precedence. "
                f"Unset {pronoun} in your shell to use the stored key."
            )

    async def _connect_openai_codex(self) -> None:
        self._renderer.notice("Starting openai-codex device-code login...")
        try:
            credential = await login_openai_codex_device_code(
                on_device_code=self._show_device_code,
            )
        except Exception as exc:  # noqa: BLE001 - show login failure in the TUI
            self._connect_error(f"Connection failed: {exc}")
            return
        try:
            self._get_store().set("openai-codex", credential)
        except AuthStorageError as exc:
            self._connect_error(f"Auth storage error: {exc}")
            return
        self._call_renderer_optional("connect_completed", "openai-codex")
        self._renderer.notice("Connected: openai-codex")

    def disconnect(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /disconnect [provider]")
            return
        if not args:
            try:
                catalog = self._connection_catalog()
            except AuthStorageError as exc:
                self._renderer.command_error(f"Auth storage error: {exc}")
                return
            method = getattr(self._renderer, "disconnect_picker_request", None)
            if callable(method):
                method(catalog)
            else:
                connected = ", ".join(
                    item.provider for family in catalog for item in family.methods if item.connected
                )
                suffix = connected or "none"
                self._renderer.notice(f"Usage: /disconnect <provider> (connected: {suffix})")
            return
        self._disconnect_provider(args[0])

    def _disconnect_provider(self, provider: str) -> None:
        environment_variables = self._configured_environment_variables(provider)
        try:
            deleted = self._get_store().delete(provider)
        except AuthStorageError as exc:
            self._renderer.command_error(f"Auth storage error: {exc}")
            return
        if environment_variables:
            names = ", ".join(environment_variables)
            pronoun = "it" if len(environment_variables) == 1 else "them"
            if deleted:
                self._call_renderer_optional("connect_completed", provider)
                self._renderer.notice(
                    f"Removed stored credentials for {provider}; still connected through "
                    f"{names}. Unset {pronoun} in your shell to disconnect."
                )
            else:
                self._connect_error(
                    f"{provider} is connected through {names}; unset {pronoun} in your shell."
                )
            return
        if deleted:
            self._call_renderer_optional("connect_completed", provider)
            self._renderer.notice(f"Disconnected: {provider}")
        else:
            self._renderer.notice(f"Not connected: {provider}")

    def _connection_catalog(self) -> tuple[ConnectionProviderStatus, ...]:
        store = self._get_store()
        openai_codex = store.get("openai-codex")
        return (
            ConnectionProviderStatus(
                id="openai",
                label="OpenAI",
                methods=(
                    ConnectionMethodStatus(
                        provider="openai-codex",
                        label="ChatGPT Plus/Pro",
                        kind="device_code",
                        source="stored" if isinstance(openai_codex, OAuthCredential) else "missing",
                    ),
                    self._api_key_method("openai", "OpenAI API key", store.get("openai")),
                ),
            ),
            ConnectionProviderStatus(
                id=self._openai_compatible_provider,
                label=self._openai_compatible_provider,
                methods=(
                    self._api_key_method(
                        self._openai_compatible_provider,
                        f"{self._openai_compatible_provider} API key",
                        store.get(self._openai_compatible_provider),
                    ),
                ),
            ),
            ConnectionProviderStatus(
                id="anthropic",
                label="Anthropic",
                methods=(
                    self._api_key_method("anthropic", "Anthropic API key", store.get("anthropic")),
                ),
            ),
            ConnectionProviderStatus(
                id="google",
                label="Google",
                methods=(self._api_key_method("google", "Google API key", store.get("google")),),
            ),
        )

    def _api_key_method(
        self,
        provider: str,
        label: str,
        credential: AuthCredential | None,
    ) -> ConnectionMethodStatus:
        environment_variable = self._configured_environment_variable(provider)
        if environment_variable is not None:
            source: ConnectionSource = "environment"
        elif isinstance(credential, ApiKeyCredential):
            source = "stored"
        else:
            source = "missing"
        return ConnectionMethodStatus(
            provider=provider,
            label=label,
            kind="api_key",
            source=source,
            environment_variable=(environment_variable or self._api_key_environment(provider)[0]),
        )

    def _supports_api_key(self, provider: str) -> bool:
        return provider in API_KEY_ENVIRONMENT_VARIABLES or (
            provider == self._openai_compatible_provider
        )

    def _configured_environment_variable(self, provider: str) -> str | None:
        return next(iter(self._configured_environment_variables(provider)), None)

    def _configured_environment_variables(self, provider: str) -> tuple[str, ...]:
        return tuple(
            name
            for name in self._api_key_environment(provider)
            if _environment_value(name) is not None
        )

    def _api_key_environment(self, provider: str) -> tuple[str, ...]:
        if provider == self._openai_compatible_provider:
            provider_environment = openai_compatible_api_key_environment(provider)
            if provider_environment == OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE:
                return (provider_environment,)
            return (
                provider_environment,
                OPENAI_COMPATIBLE_API_KEY_ENVIRONMENT_VARIABLE,
            )
        return API_KEY_ENVIRONMENT_VARIABLES.get(provider, ())

    def _show_device_code(self, info: DeviceCodeInfo) -> None:
        self._call_renderer_optional("connect_device_code", info.verification_uri, info.user_code)
        self._renderer.notice(f"Open {info.verification_uri} and enter code {info.user_code}")

    def _connect_picker_request(
        self,
        catalog: tuple[ConnectionProviderStatus, ...],
        *,
        provider: str | None = None,
    ) -> None:
        method = getattr(self._renderer, "connect_picker_request", None)
        if callable(method):
            method(catalog, provider=provider)
            return
        if provider is not None:
            selected = _find_method(catalog, provider)
            environment_variable = selected.environment_variable if selected is not None else None
            if environment_variable is not None:
                self._renderer.notice(
                    f"Set {environment_variable} before starting Wisp to connect {provider}."
                )
            return
        choices = ", ".join(method.provider for family in catalog for method in family.methods)
        self._renderer.notice(f"Usage: /connect <provider> ({choices})")

    def _call_renderer_optional(self, method_name: str, *args: object, **kwargs: object) -> None:
        method = getattr(self._renderer, method_name, None)
        if callable(method):
            method(*args, **kwargs)

    def _connect_error(self, message: str) -> None:
        self._call_renderer_optional("connect_failed", message)
        self._renderer.command_error(message)


def _auth_status_line(provider: str, credential: AuthCredential | None) -> str:
    if credential is None:
        return f"{provider}: not logged in"
    if isinstance(credential, ApiKeyCredential):
        return f"{provider}: api key configured"
    return f"{provider}: oauth configured ({_oauth_expiry_text(credential)})"


def _find_method(
    catalog: tuple[ConnectionProviderStatus, ...],
    provider: str,
) -> ConnectionMethodStatus | None:
    return next(
        (method for family in catalog for method in family.methods if method.provider == provider),
        None,
    )


def _environment_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _oauth_expiry_text(credential: OAuthCredential) -> str:
    expires = datetime.fromtimestamp(credential.expires / 1000, tz=UTC)
    return f"expires {expires.isoformat()}"
