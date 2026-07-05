"""Auth-related CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console

from wisp.auth.openai_codex import OpenAICodexLoginMethod, login_openai_codex
from wisp.auth.storage import ApiKeyCredential, AuthCredential, JsonAuthStore, OAuthCredential
from wisp.config import WispConfig, load_project_env

SUPPORTED_LOGIN_PROVIDERS = ("openai-codex",)


auth_app = typer.Typer(help="Manage Wisp provider credentials.")


@auth_app.command("status")
def auth_status(
    provider: Annotated[str | None, typer.Argument(help="Provider to inspect.")] = None,
    auth_file: Annotated[
        Path | None,
        typer.Option("--auth-file", help="Path to Wisp's private auth JSON file."),
    ] = None,
) -> None:
    """Show configured provider auth without revealing secrets."""

    store = _store_from_options(auth_file)
    providers = [provider] if provider is not None else list(store.providers())
    if not providers:
        typer.echo("no credentials configured")
        return
    for provider_name in providers:
        credential = store.get(provider_name)
        typer.echo(_status_line(provider_name, credential))


@auth_app.command("login")
def auth_login(
    provider: Annotated[str, typer.Argument(help="Provider to authenticate.")],
    method: Annotated[
        OpenAICodexLoginMethod,
        typer.Option("--method", help="Login method for providers that support OAuth."),
    ] = OpenAICodexLoginMethod.browser,
    auth_file: Annotated[
        Path | None,
        typer.Option("--auth-file", help="Path to Wisp's private auth JSON file."),
    ] = None,
    open_browser: Annotated[
        bool,
        typer.Option("--open-browser/--no-open-browser", help="Open the browser login URL."),
    ] = True,
) -> None:
    """Authenticate a provider and store credentials privately."""

    if provider not in SUPPORTED_LOGIN_PROVIDERS:
        supported = ", ".join(SUPPORTED_LOGIN_PROVIDERS)
        raise typer.BadParameter(f"unsupported login provider: {provider} (supported: {supported})")
    store = _store_from_options(auth_file)
    console = Console(stderr=True)
    credential = anyio.run(
        _login_openai_codex,
        method,
        console,
        open_browser,
    )
    store.set(provider, credential)
    typer.echo(f"logged in: {provider}")
    typer.echo(f"credentials saved: {store.path}")


@auth_app.command("logout")
def auth_logout(
    provider: Annotated[str, typer.Argument(help="Provider to forget.")],
    auth_file: Annotated[
        Path | None,
        typer.Option("--auth-file", help="Path to Wisp's private auth JSON file."),
    ] = None,
) -> None:
    """Remove stored credentials for a provider."""

    store = _store_from_options(auth_file)
    if store.delete(provider):
        typer.echo(f"logged out: {provider}")
    else:
        typer.echo(f"not logged in: {provider}")


async def _login_openai_codex(
    method: OpenAICodexLoginMethod,
    console: Console,
    open_browser: bool,
) -> OAuthCredential:
    return await login_openai_codex(
        method=method,
        on_auth_url=lambda url: console.print(f"Open this URL to authenticate:\n{url}"),
        on_device_code=lambda info: console.print(
            f"Open {info.verification_uri} and enter code {info.user_code}"
        ),
        prompt=lambda message: typer.prompt(message),
        open_browser=open_browser,
    )


def _store_from_options(auth_file: Path | None) -> JsonAuthStore:
    load_project_env()
    config = WispConfig.from_env(auth_path=auth_file, load_env_file=False)
    return JsonAuthStore(config.auth_path)


def _status_line(provider: str, credential: AuthCredential | None) -> str:
    if credential is None:
        return f"{provider}: not logged in"
    if isinstance(credential, ApiKeyCredential):
        return f"{provider}: api key configured"
    return f"{provider}: oauth configured ({_expiry_text(credential)})"


def _expiry_text(credential: OAuthCredential) -> str:
    expires = datetime.fromtimestamp(credential.expires / 1000, tz=UTC)
    return f"expires {expires.isoformat()}"


__all__ = ["auth_app"]
