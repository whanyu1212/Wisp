"""TUI launch and subprocess helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from wisp.config import WispConfig
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.rendering import TuiRendererKind


@dataclass(frozen=True)
class TuiOptions:
    """Options used to start the Wisp TUI shell."""

    config: WispConfig
    allow_read_tools: bool = False
    allowed_tools: tuple[str, ...] = ()
    resume: str | None = None
    continue_latest: bool = False
    approve_unsafe_tools: bool = False
    max_tool_iterations: int | None = None
    renderer: TuiRendererKind = TuiRendererKind.line


async def _preflight_tui_options(options: TuiOptions) -> None:
    runtime = await build_runtime()
    runtime.providers.get(options.config.provider)
    for tool_name in set(options.allowed_tools):
        runtime.tools.get(tool_name)
    sessions = JsonlSessionStore(options.config.session_dir)
    if options.resume is not None:
        sessions.load(options.resume)
    elif options.continue_latest:
        sessions.latest()


def _rpc_command(options: TuiOptions) -> tuple[str, ...]:
    command: list[str] = [
        sys.executable,
        "-m",
        "wisp",
        "--mode",
        "rpc",
        "--provider",
        options.config.provider,
        "--session-dir",
        str(options.config.session_dir),
    ]
    if options.config.model is not None:
        command.extend(("--model", options.config.model))
    if options.resume is not None:
        command.extend(("--resume", options.resume))
    if options.continue_latest:
        command.append("--continue")
    if options.allow_read_tools:
        command.append("--allow-read-tools")
    for tool_name in options.allowed_tools:
        command.extend(("--allow-tool", tool_name))
    if options.approve_unsafe_tools:
        command.append("--yes")
    if options.max_tool_iterations is not None:
        command.extend(("--max-tool-iterations", str(options.max_tool_iterations)))
    return tuple(command)


def _rpc_env() -> dict[str, str]:
    return dict(os.environ)


def _stdin_is_interactive() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty and isatty())


def _stdout_is_interactive() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty and isatty())


def _stdio_is_interactive() -> bool:
    return _stdin_is_interactive() and _stdout_is_interactive()
