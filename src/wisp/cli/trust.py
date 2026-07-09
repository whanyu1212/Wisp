"""Entrypoint helpers for resolving project trust.

Each output mode resolves trust the same way — consult the store, prompt on a
first run — but surfaces the prompt differently (a ``typer.confirm`` in the text
CLI, an RPC command over stdin, a ``[y/N]`` line in the TUI). This module holds the
shared, mode-agnostic pieces: the environment override and the text-mode prompter.
The resolved decision is threaded into :meth:`wisp.config.WispConfig.from_env`, which
gates the project-local settings file on it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from wisp.agent.prompt import resolve_project_context_root
from wisp.trust import _canonical_key, forget_trust, is_trusted, record_trust
from wisp.trust_flow import TrustDecision, resolve_trust

_TRUTHY = {"1", "true", "yes", "on", "trust"}
_FALSY = {"0", "false", "no", "off", "untrust"}

trust_app = typer.Typer(help="Manage Wisp project trust decisions.")


def _canonical_project(project_path: Path) -> str:
    return _canonical_key(project_path)


def _selected_project_root(project: Path | None) -> Path:
    selected = project or Path(".")
    return resolve_project_context_root(selected.expanduser())


@trust_app.command("status")
def trust_status(
    project: Annotated[
        Path | None,
        typer.Argument(help="Project path to inspect. Defaults to the current directory."),
    ] = None,
) -> None:
    """Show the persisted trust decision for a project."""

    selected = _selected_project_root(project)
    canonical = _canonical_project(selected)
    trusted = is_trusted(selected)
    if trusted is True:
        typer.echo(f"trusted: {canonical}")
    elif trusted is False:
        typer.echo(f"untrusted: {canonical}")
    else:
        typer.echo(f"undecided: {canonical}")


@trust_app.command("allow")
def trust_allow(
    project: Annotated[
        Path | None,
        typer.Argument(help="Project path to trust. Defaults to the current directory."),
    ] = None,
) -> None:
    """Persistently trust a project."""

    selected = _selected_project_root(project)
    record_trust(selected, True)
    typer.echo(f"trusted: {_canonical_project(selected)}")


@trust_app.command("revoke")
def trust_revoke(
    project: Annotated[
        Path | None,
        typer.Argument(help="Project path to mark untrusted. Defaults to the current directory."),
    ] = None,
) -> None:
    """Persistently mark a project as untrusted."""

    selected = _selected_project_root(project)
    record_trust(selected, False)
    typer.echo(f"untrusted: {_canonical_project(selected)}")


@trust_app.command("forget")
def trust_forget(
    project: Annotated[
        Path | None,
        typer.Argument(help="Project path whose trust decision should be forgotten."),
    ] = None,
) -> None:
    """Forget a persisted trust decision so Wisp can prompt again later."""

    selected = _selected_project_root(project)
    canonical = _canonical_project(selected)
    if forget_trust(selected):
        typer.echo(f"forgot trust decision: {canonical}")
    else:
        typer.echo(f"no trust decision: {canonical}")


def trust_override_from_env() -> bool | None:
    """Return a forced trust decision from ``WISP_TRUST``, or None if unset.

    Lets non-interactive/CI runs opt in (``WISP_TRUST=1``) or explicitly out
    (``WISP_TRUST=0``) without a prompt. Unrecognized values are ignored.
    """

    raw = os.environ.get("WISP_TRUST")
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def trusted_noninteractive(project_path: Path, *, trust_path: Path | None = None) -> bool:
    """Return the trust decision from non-interactive, project-safe signals only.

    For entrypoints (RPC, TUI) that surface the trust prompt asynchronously or
    out-of-band, config must still be built at startup with *some* trust value. This
    consults only signals that a project cannot forge — a ``WISP_TRUST`` override or a
    stored decision — and returns ``True`` only when they say trusted. An undecided
    project is treated as untrusted here (the safe default), so its local settings are
    not applied at startup; the out-of-band prompt still runs and, once answered,
    governs project-local resource loading for the rest of the session.
    """

    override = trust_override_from_env()
    if override is not None:
        return override
    return is_trusted(project_path, trust_path=trust_path) is True


def resolve_cli_trust(project_path: Path, *, trust_path: Path | None = None) -> TrustDecision:
    """Resolve trust for the text/JSON CLI.

    Honors ``WISP_TRUST`` first, then a stored decision, then an interactive
    ``typer.confirm`` when stdin is a TTY. A non-interactive first run with no
    override resolves to untrusted (safe) without persisting.
    """

    override = trust_override_from_env()
    if override is not None:
        return TrustDecision(project_path=project_path, trusted=override)

    prompter = _text_trust_prompter if sys.stdin.isatty() else None
    return resolve_trust(project_path, prompter=prompter, trust_path=trust_path)


def _text_trust_prompter(project_path: Path) -> bool | None:
    """Prompt for trust on the text CLI; None if the answer can't be read.

    The prompt is written to stderr (``err=True``) so it never contaminates
    stdout — which in ``--mode json`` carries the machine-readable JSONL event
    stream that clients parse.
    """

    try:
        return typer.confirm(
            f"Do you trust the files in {project_path}?\n"
            "Trusting lets Wisp load this project's local configuration "
            "(its .wisp/settings.json, context files, project extensions). "
            "You can still use Wisp either way.",
            default=False,
            err=True,
        )
    except EOFError:
        return None
