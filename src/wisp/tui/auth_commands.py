"""The `/auth`, `/login`, `/logout` slash-command handlers for the TUI shell.

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

from collections.abc import Callable
from datetime import UTC, datetime

from wisp.auth.openai_codex import OpenAICodexLoginMethod, login_openai_codex
from wisp.auth.storage import (
    ApiKeyCredential,
    AuthCredential,
    AuthStorageError,
    JsonAuthStore,
    OAuthCredential,
)
from wisp.tui.rendering import TuiRenderer


class AuthCommands:
    """Handlers for the credential slash commands, delegated to by the shell."""

    def __init__(
        self,
        renderer: TuiRenderer,
        get_store: Callable[[], JsonAuthStore],
        get_default_provider: Callable[[], str],
    ) -> None:
        self._renderer = renderer
        # Read the store lazily: the shell swaps it on a trusted-project rebuild.
        self._get_store = get_store
        # The default provider tracks live shell state (pending configs + current).
        self._get_default_provider = get_default_provider

    def status(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /auth [provider]")
            return
        provider = args[0] if args else self._get_default_provider()
        try:
            credential = self._get_store().get(provider)
        except AuthStorageError as exc:
            self._renderer.command_error(f"Auth storage error: {exc}")
            return
        self._renderer.notice(_auth_status_line(provider, credential))

    async def login(self, args: tuple[str, ...]) -> None:
        if len(args) > 2:
            self._renderer.command_error("Usage: /login [provider] [device-code]")
            return
        provider = args[0] if args else self._get_default_provider()
        if provider != "openai-codex":
            self._renderer.command_error("TUI login currently supports only openai-codex.")
            return
        method_text = args[1] if len(args) == 2 else OpenAICodexLoginMethod.device_code.value
        try:
            method = OpenAICodexLoginMethod(method_text)
        except ValueError:
            self._renderer.command_error("Usage: /login [openai-codex] [device-code]")
            return
        if method is OpenAICodexLoginMethod.browser:
            self._renderer.command_error(
                "Browser login is not available inside the TUI; use `wisp auth login openai-codex`."
            )
            return
        self._renderer.notice("Starting openai-codex device-code login...")
        try:
            credential = await login_openai_codex(
                method=method,
                on_device_code=lambda info: self._renderer.notice(
                    f"Open {info.verification_uri} and enter code {info.user_code}"
                ),
                open_browser=False,
            )
        except Exception as exc:  # noqa: BLE001 - show login failure in the TUI
            self._renderer.command_error(f"Login failed: {exc}")
            return
        try:
            self._get_store().set(provider, credential)
        except AuthStorageError as exc:
            self._renderer.command_error(f"Auth storage error: {exc}")
            return
        self._renderer.notice(f"Logged in: {provider}")

    def logout(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._renderer.command_error("Usage: /logout [provider]")
            return
        provider = args[0] if args else self._get_default_provider()
        try:
            deleted = self._get_store().delete(provider)
        except AuthStorageError as exc:
            self._renderer.command_error(f"Auth storage error: {exc}")
            return
        if deleted:
            self._renderer.notice(f"Logged out: {provider}")
        else:
            self._renderer.notice(f"Not logged in: {provider}")


def _auth_status_line(provider: str, credential: AuthCredential | None) -> str:
    if credential is None:
        return f"{provider}: not logged in"
    if isinstance(credential, ApiKeyCredential):
        return f"{provider}: api key configured"
    return f"{provider}: oauth configured ({_oauth_expiry_text(credential)})"


def _oauth_expiry_text(credential: OAuthCredential) -> str:
    expires = datetime.fromtimestamp(credential.expires / 1000, tz=UTC)
    return f"expires {expires.isoformat()}"
