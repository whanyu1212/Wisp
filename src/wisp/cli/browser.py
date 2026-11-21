"""Commands for Wisp's dedicated live browser profile."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from wisp.coding.browser_control import BrowserControlError
from wisp.coding.browser_profile import clear_browser_profile
from wisp.config.settings import browser_profile_path

browser_app = typer.Typer(help="Manage the saved live browser profile.", no_args_is_help=True)


@browser_app.command("clear")
def clear_command(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Clear without asking.")] = False,
) -> None:
    """Clear saved browser data, including cookies and sign-ins."""

    if not yes:
        if not sys.stdin.isatty():
            typer.echo("Confirmation needs a terminal. Pass --yes to clear the profile.", err=True)
            raise typer.Exit(1)
        if not typer.confirm("Clear Wisp's saved browser data, including site sign-ins?"):
            raise typer.Exit(1)
    try:
        clear_browser_profile(browser_profile_path())
    except BrowserControlError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Cleared Wisp's saved browser data.")
