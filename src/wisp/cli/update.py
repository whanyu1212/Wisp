"""Explicit Wisp update checks and installations."""

from __future__ import annotations

from typing import Annotated

import anyio
import typer

from wisp.update_check import (
    UpdateCheckError,
    UpdateInstallError,
    get_update_status,
    install_update,
)


def update_command(
    check: Annotated[
        bool,
        typer.Option("--check", help="Check for an update without installing it."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Install without asking for confirmation."),
    ] = False,
) -> None:
    """Check PyPI now and optionally install the latest compatible Wisp release."""

    try:
        status = anyio.run(get_update_status)
    except UpdateCheckError as exc:
        typer.echo(f"Update check failed: {exc}", err=True)
        raise typer.Exit(1) from None

    update = status.available
    if update is None:
        typer.echo(f"Wisp {status.current_version} is up to date.")
        return

    typer.echo(f"Wisp {update.latest_version} is available (current {update.current_version}).")
    if check:
        typer.echo(f"Update with: {update.update_command}")
        return
    if not yes and not typer.confirm(f"Install Wisp {update.latest_version} now?"):
        typer.echo("Update cancelled.")
        return

    try:
        anyio.run(install_update, update)
    except UpdateInstallError as exc:
        typer.echo(f"Update failed: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Updated Wisp to {update.latest_version}. Restart Wisp to use the new version.")


__all__ = ["update_command"]
