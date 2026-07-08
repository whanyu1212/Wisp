"""Entrypoint helpers for resolving project trust.

Each output mode resolves trust the same way — consult the store, prompt on a
first run — but surfaces the prompt differently (a ``typer.confirm`` in the text
CLI, an RPC command over stdin, a ``[y/N]`` line in the TUI). This module holds the
shared, mode-agnostic pieces: the environment override and the text-mode prompter.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from wisp.trust_flow import TrustDecision, resolve_trust

_TRUTHY = {"1", "true", "yes", "on", "trust"}
_FALSY = {"0", "false", "no", "off", "untrust"}


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
    """Prompt for trust on the text CLI; None if the answer can't be read."""

    try:
        return typer.confirm(
            f"Do you trust the files in {project_path}?\n"
            "Trusting lets Wisp load this project's local configuration "
            "(context files, project extensions). You can still use Wisp either way.",
            default=False,
        )
    except (EOFError, typer.Abort):
        return None
