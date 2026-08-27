"""CLI option and environment default resolution."""

from __future__ import annotations

import os
import sys

import typer
from rich.console import Console

from .output import _exit_with_error
from .types import OutputMode, TuiFrontendKind


def _option_was_provided(ctx: typer.Context, name: str) -> bool:
    source = ctx.get_parameter_source(name)
    return getattr(source, "name", None) == "COMMANDLINE"


def _has_callback_cli_args(ctx: typer.Context) -> bool:
    return any(_option_was_provided(ctx, name) for name in ctx.params)


def _terminal_is_interactive() -> bool:
    for stream in (sys.stdin, sys.stdout):
        isatty = getattr(stream, "isatty", None)
        if not callable(isatty) or not isatty():
            return False
    return True


def _resolve_cli_mode(
    mode: OutputMode,
    *,
    prompt: str | None,
    mode_was_provided: bool,
    console: Console,
) -> OutputMode:
    if mode_was_provided or prompt is not None:
        return mode
    env_mode = _output_mode_from_env(console)
    return env_mode or mode


def _resolve_tui_renderer(
    renderer: TuiFrontendKind,
    *,
    renderer_was_provided: bool,
    console: Console,
) -> TuiFrontendKind:
    if renderer_was_provided:
        return renderer
    env_renderer = _tui_renderer_from_env(console)
    return env_renderer or renderer


def _output_mode_from_env(console: Console) -> OutputMode | None:
    value = _env_value("WISP_MODE")
    if value is None:
        return None
    try:
        return OutputMode(value)
    except ValueError:
        allowed = ", ".join(mode.value for mode in OutputMode)
        _exit_with_error(
            f"WISP_MODE must be one of: {allowed}", mode=OutputMode.text, console=console
        )


def _tui_renderer_from_env(console: Console) -> TuiFrontendKind | None:
    value = _env_value("WISP_TUI_RENDERER")
    if value is None:
        return None
    try:
        return TuiFrontendKind(value)
    except ValueError:
        allowed = ", ".join(renderer.value for renderer in TuiFrontendKind)
        _exit_with_error(
            f"WISP_TUI_RENDERER must be one of: {allowed}",
            mode=OutputMode.text,
            console=console,
        )


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip().lower()
    return stripped or None
