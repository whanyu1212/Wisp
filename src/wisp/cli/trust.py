"""Entrypoint helpers for resolving project trust and gating the project ``.env``.

Each output mode resolves trust the same way — consult the store, prompt on a
first run — but surfaces the prompt differently (a ``typer.confirm`` in the text
CLI, an RPC command over stdin, a ``[y/N]`` line in the TUI). This module holds the
shared, mode-agnostic pieces: the environment override, the text-mode prompter, and
the trust-gated loading of a project's ``.env`` (project-local configuration that an
untrusted repo must not be allowed to apply).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from wisp.config import load_project_env
from wisp.trust import is_trusted
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


def load_env_if_trusted_noninteractive(
    project_path: Path, *, trust_path: Path | None = None
) -> None:
    """Load the project ``.env`` only if trust is already decided as trusted.

    For entrypoints (RPC, TUI) that resolve trust asynchronously or out-of-band, we
    still must decide ``.env`` at startup. This consults only the non-interactive,
    project-safe signals — a ``WISP_TRUST`` override or a stored decision — and loads
    ``.env`` when they say trusted. An undecided project does **not** get its ``.env``
    applied for this run (the safe default); the interactive/out-of-band prompt still
    runs and governs project-local resource loading (context files, extensions).
    """

    override = trust_override_from_env()
    if override is True or (override is None and is_trusted(project_path, trust_path=trust_path)):
        load_project_env()


def resolve_trust_and_load_env(
    project_path: Path, *, trust_path: Path | None = None
) -> TrustDecision:
    """Resolve project trust, then load the project ``.env`` only if trusted.

    A project's ``.env`` is project-local configuration (it can set the provider,
    session directory, credential paths, and API keys). Applying it from an
    untrusted repo would let a cloned project inject configuration, so ``.env`` is
    gated on trust exactly like context files and project extensions. Trust itself
    is resolved from safe sources only (the global store and the real-process
    ``WISP_TRUST``), never from ``.env`` — so the gate cannot be bootstrapped by the
    very file it guards.
    """

    decision = resolve_cli_trust(project_path, trust_path=trust_path)
    if decision.trusted:
        load_project_env()
    return decision


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
            "(context files, project extensions, .env). You can still use Wisp either way.",
            default=False,
            err=True,
        )
    except (EOFError, typer.Abort):
        return None
