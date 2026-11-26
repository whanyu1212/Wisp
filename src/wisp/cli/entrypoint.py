"""Minimal CLI bootstrap before the Typer application imports.

The installed console script and ``python -m wisp`` both enter here.
"""

from __future__ import annotations

import sys


def application_export(name: str) -> object:
    # Keep the public imports while letting main secure the fd before CLI imports.
    if name in {"ToolApprovalDecision", "app"}:
        from wisp.cli import application

        return getattr(application, name)
    raise AttributeError(name)


def main() -> None:
    """Secure the launcher's private pipe before loading the CLI or running callbacks."""

    from wisp.cli.startup_pipe import PREVIEW_FD_PREFIX, StartupPreviewPipe

    args = sys.argv[1:]
    preview = None
    if args[:2] == ["--mode", "rpc"] and len(args) >= 3 and args[2].startswith(PREVIEW_FD_PREFIX):
        preview = StartupPreviewPipe.claim(args.pop(2))
    try:
        tui = args[:1] == ["tui"] or args[:2] == ["--mode", "tui"]
        if tui and any(arg in ("--continue", "--resume") for arg in args):
            from wisp.cli.native_tui.preview import warm_cli_imports

            with warm_cli_imports():
                _invoke(args, preview)
        else:
            _invoke(args, preview)
    finally:
        if preview is not None:
            preview.stream.close()


def _invoke(args: list[str], context: object) -> None:
    from wisp.cli.application import app

    app(args=args, obj=context)
