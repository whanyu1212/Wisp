"""TUI launch and subprocess helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from wisp.config import WispConfig
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.rendering import TuiRendererKind


@dataclass(frozen=True)
class TuiOptions:
    """Options used to start the Wisp TUI shell.

    ``config`` is the parent's startup view of configuration, used for preflight
    validation and the header display. It is deliberately **not** serialized into the
    RPC subprocess's arguments: the subprocess owns config resolution so that a
    trusted project's ``.wisp/settings.json`` can set the provider / model / session
    dir / auth file after the trust prompt is answered. Only values the *user* set
    explicitly on the command (``user_provider`` / ``user_model`` / ``user_session_dir``
    / ``user_auth_file``) are forwarded, since those are legitimate highest-precedence
    overrides the subprocess cannot re-derive.
    """

    config: WispConfig
    all_tools: bool = False
    allow_read_tools: bool = False
    allowed_tools: tuple[str, ...] = ()
    resume: str | None = None
    continue_latest: bool = False
    approve_unsafe_tools: bool = False
    max_tool_iterations: int | None = None
    renderer: TuiRendererKind = TuiRendererKind.line
    user_provider: str | None = None
    user_model: str | None = None
    user_session_dir: Path | None = None
    user_auth_file: Path | None = None


async def _preflight_tui_options(options: TuiOptions) -> None:
    runtime = await build_runtime(
        auth_path=options.config.auth_path,
        retry_policy=options.config.retry_policy,
    )
    runtime.providers.get(options.config.provider)
    for tool_name in set(options.allowed_tools):
        runtime.tools.get(tool_name)
    sessions = JsonlSessionStore(options.config.session_dir)
    if options.resume is not None:
        sessions.load(options.resume)
    elif options.continue_latest:
        sessions.latest()


def _rpc_command(options: TuiOptions) -> tuple[str, ...]:
    # Do NOT pass --provider / --model / --session-dir / --auth-file from the resolved
    # config: those are trust-gated (a trusted project's settings.json may set them), and
    # the subprocess resolves them itself after the trust prompt. Forwarding the parent's
    # untrusted-startup values as CLI flags would outrank the project settings (CLI is
    # highest precedence) and defeat the whole gate. Only forward the user's explicit
    # overrides, which the subprocess cannot otherwise know about.
    command: list[str] = [
        sys.executable,
        "-m",
        "wisp",
        "--mode",
        "rpc",
    ]
    if options.user_provider is not None:
        command.extend(("--provider", options.user_provider))
    if options.user_model is not None:
        command.extend(("--model", options.user_model))
    if options.user_session_dir is not None:
        command.extend(("--session-dir", str(options.user_session_dir)))
    if options.user_auth_file is not None:
        command.extend(("--auth-file", str(options.user_auth_file)))
    if options.resume is not None:
        command.extend(("--resume", options.resume))
    if options.continue_latest:
        command.append("--continue")
    if options.all_tools:
        command.append("--all-tools")
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
