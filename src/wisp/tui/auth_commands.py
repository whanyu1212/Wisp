"""The `/auth`, `/connect`, `/disconnect` slash-command handlers for the TUI shell.

Credential mutation is backend-owned. This collaborator only presents the current
connection catalog and asks the shell to send typed RPC commands.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from wisp.auth.connections import (
    ConnectionProviderStatus,
    auth_status_line,
    configured_environment_variables,
    connection_method,
    supports_api_key,
)
from wisp.tui.rendering import TuiRenderer

type CatalogLoader = Callable[[], tuple[ConnectionProviderStatus, ...]]
type ApiKeyStore = Callable[[str, str], Awaitable[None]]
type ProviderDisconnect = Callable[[str], Awaitable[None]]
type DeviceCodeLogin = Callable[[str], Awaitable[None]]


class AuthCommands:
    """Handlers for the credential slash commands, delegated to by the shell."""

    def __init__(
        self,
        renderer: TuiRenderer,
        get_catalog: CatalogLoader,
        get_default_provider: Callable[[], str],
        *,
        store_api_key: ApiKeyStore,
        disconnect_provider: ProviderDisconnect,
        begin_device_code: DeviceCodeLogin,
        openai_compatible_provider: str = "openai-compatible",
    ) -> None:
        self._renderer = renderer
        self._get_catalog = get_catalog
        self._get_default_provider = get_default_provider
        self._store_api_key = store_api_key
        self._on_disconnect = disconnect_provider
        self._begin_device_code = begin_device_code
        self._openai_compatible_provider = openai_compatible_provider

    def status(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /auth [provider]")
            return
        provider = args[0] if args else self._get_default_provider()
        environment_variables = configured_environment_variables(
            provider,
            openai_compatible_provider=self._openai_compatible_provider,
        )
        if environment_variables:
            configured_via = ", ".join(environment_variables)
            self._renderer.notice(f"{provider}: api key configured via {configured_via}")
            return
        try:
            method = connection_method(self._get_catalog(), provider)
        except Exception as extra:  # noqa: BLE001 - show storage failure in the TUI
            self._renderer.command_error(f"Auth storage error: {extra}")
            return
        self._renderer.notice(auth_status_line(provider, method))

    async def connect(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /connect [provider]")
            return
        try:
            catalog = self._get_catalog()
        except Exception as extra:  # noqa: BLE001 - show storage failure in the TUI
            self._renderer.command_error(f"Auth storage error: {extra}")
            return
        if not args:
            self._connect_picker_request(catalog)
            return
        provider = args[0]
        method = connection_method(catalog, provider)
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
        self._renderer.notice("Starting openai-codex device-code login...")
        try:
            await self._begin_device_code(provider)
        except Exception as exc:  # noqa: BLE001 - show login failure in the TUI
            self._connect_error(f"Connection failed: {exc}")

    async def connect_api_key(self, provider: str, api_key: str) -> None:
        """Persist a key received through the renderer's redacted callback."""

        if not supports_api_key(
            provider,
            openai_compatible_provider=self._openai_compatible_provider,
        ):
            self._connect_error(f"API-key connection is not supported for {provider}.")
            return
        normalized = api_key.strip()
        if not normalized:
            self._connect_error("API key cannot be empty.")
            return
        try:
            await self._store_api_key(provider, normalized)
        except Exception as exc:  # noqa: BLE001 - show storage failure in the TUI
            self._connect_error(f"Auth storage error: {exc}")
            return
        self._call_renderer_optional("connect_completed", provider)
        environment_variables = configured_environment_variables(
            provider,
            openai_compatible_provider=self._openai_compatible_provider,
        )
        if not environment_variables:
            self._renderer.notice(f"Connected: {provider}")
            return
        names = ", ".join(environment_variables)
        verb = "takes" if len(environment_variables) == 1 else "take"
        pronoun = "it" if len(environment_variables) == 1 else "them"
        self._renderer.notice(
            f"Stored API key for {provider}; {names} still {verb} precedence. "
            f"Unset {pronoun} in your shell to use the stored key."
        )

    async def disconnect(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /disconnect [provider]")
            return
        try:
            catalog = self._get_catalog()
        except Exception as extra:  # noqa: BLE001 - show storage failure in the TUI
            self._renderer.command_error(f"Auth storage error: {extra}")
            return
        if not args:
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
        await self._disconnect_provider(args[0], catalog)

    async def _disconnect_provider(
        self,
        provider: str,
        catalog: tuple[ConnectionProviderStatus, ...],
    ) -> None:
        environment_variables = configured_environment_variables(
            provider,
            openai_compatible_provider=self._openai_compatible_provider,
        )
        method = connection_method(catalog, provider)
        deleted = bool(method is not None and method.has_stored_credential)
        try:
            await self._on_disconnect(provider)
        except Exception as extra:  # noqa: BLE001 - show storage failure in the TUI
            self._renderer.command_error(f"Auth storage error: {extra}")
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
        return self._get_catalog()

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
            selected = connection_method(catalog, provider)
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


__all__ = ["AuthCommands"]
