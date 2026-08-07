"""Auth-related CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from wisp.agent.prompt import resolve_project_context_root
from wisp.auth.storage import ApiKeyCredential, AuthCredential, JsonAuthStore, OAuthCredential
from wisp.config import WispConfig

from . import trust as _cli_trust

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


def _store_from_options(auth_file: Path | None) -> JsonAuthStore:
    # Auth commands are non-interactive: honor only safe existing trust signals
    # (WISP_TRUST or the global trust store), never prompt from a credential command.
    # Untrusted remains fail-closed, while an already trusted project can direct
    # auth status/logout to its configured auth_path.
    project_root = resolve_project_context_root(Path.cwd())
    trusted = _cli_trust.trusted_noninteractive(project_root)
    config = WispConfig.from_env(
        auth_path=auth_file,
        project_dir=project_root,
        trusted=trusted,
    )
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
