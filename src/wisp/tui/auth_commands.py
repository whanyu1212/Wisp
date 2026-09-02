"""The `/auth`, `/connect`, `/disconnect` slash-command handlers for the TUI shell.

Credential mutation is backend-owned. This collaborator only presents the current
connection catalog and asks the shell to send typed RPC commands.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from wisp.auth.connections import (
    ConnectionProviderStatus,
    auth_status_line,
    connection_method,
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
        openai_compatible_provider: str | None = None,
    ) -> None:
        del openai_compatible_provider  # Compatibility only; the backend catalog is authoritative.
        self._renderer = renderer
        self._get_catalog = get_catalog
        self._get_default_provider = get_default_provider
        self._store_api_key = store_api_key
        self._on_disconnect = disconnect_provider
        self._begin_device_code = begin_device_code

    def status(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /auth [provider]")
            return
        provider = args[0] if args else self._get_default_provider()
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

        try:
            selected = connection_method(self._get_catalog(), provider)
        except Exception as exc:  # noqa: BLE001 - show catalog failure in the TUI
            self._connect_error(f"Auth storage error: {exc}")
            return
        if selected is None or selected.kind != "api_key":
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
        try:
            method = connection_method(self._get_catalog(), provider)
        except Exception:  # noqa: BLE001 - mutation succeeded; status refresh is secondary
            self._renderer.notice(
                f"Stored API key for {provider}; connection status refresh unavailable."
            )
            return
        environment_variable = (
            method.environment_variable
            if method is not None and method.source == "environment"
            else None
        )
        if environment_variable is None:
            self._renderer.notice(f"Connected: {provider}")
            return
        self._renderer.notice(
            f"Stored API key for {provider}; {environment_variable} currently takes precedence. "
            f"Unset all API-key environment variables for {provider} to use the stored key."
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
        await self._disconnect_provider(args[0])

    async def _disconnect_provider(self, provider: str) -> None:
        try:
            await self._on_disconnect(provider)
        except Exception as extra:  # noqa: BLE001 - show storage failure in the TUI
            self._renderer.command_error(f"Auth storage error: {extra}")
            return
        self._call_renderer_optional("connect_completed", provider)
        try:
            method = connection_method(self._get_catalog(), provider)
        except Exception:  # noqa: BLE001 - mutation succeeded; status refresh is secondary
            self._renderer.notice(
                f"Stored credentials cleared for {provider}; connection status refresh unavailable."
            )
            return
        environment_variable = (
            method.environment_variable
            if method is not None and method.source == "environment"
            else None
        )
        if environment_variable is not None:
            self._renderer.notice(
                f"Stored credentials cleared for {provider}; still connected through "
                f"{environment_variable}. Unset all API-key environment variables for "
                f"{provider} to disconnect."
            )
            return
        self._renderer.notice(f"Disconnected: {provider}")

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
