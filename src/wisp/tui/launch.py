"""TUI launch and subprocess helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from wisp.config import WispConfig
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownToolError
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.rendering import TuiRendererKind


@dataclass(frozen=True)
class TuiOptions:
    """Options used to start the Wisp TUI shell.

    ``config`` is the parent's startup view of configuration, used for preflight
    validation and the header display. It is deliberately **not** serialized into the
    RPC subprocess's arguments: the subprocess owns config resolution and receives
    the trust decision resolved by the parent before TUI startup, so a trusted
    project's ``.wisp/settings.json`` can set provider / model / session dir / auth
    file consistently in both processes. Only values the *user* set
    explicitly on the command (``user_provider`` / ``user_model`` / ``user_session_dir``
    / ``user_auth_file``) are forwarded, since those are legitimate highest-precedence
    overrides the subprocess cannot re-derive.

    ``config.effort``, unlike ``config.provider``/``config.model``/``config.session_dir``/
    ``config.auth_path`` above, IS forwarded to the subprocess (as a ``WISP_EFFORT``
    env var, in ``_rpc_env``) -- effort is never trust-gated (see
    ``resolve_settings``), so it already resolves identically in both processes
    regardless of trust, and carries none of the precedence-inversion risk
    forwarding a trust-gated field would. Without this, a caller that sets
    ``config.effort`` directly (bypassing ``WISP_EFFORT``/the settings file
    entirely -- e.g. an embedder constructing
    ``TuiOptions(config=WispConfig(effort=...))``) would seed the parent
    shell/model picker with that tier while the subprocess never applied it to
    any prompt.

    ``project_trusted`` carries the parent CLI's already-resolved decision into the
    child process. It remains optional so direct/embedded ``run_tui`` callers can
    retain the RPC trust-request fallback.
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
    project_trusted: bool | None = None
    user_provider: str | None = None
    user_model: str | None = None
    user_session_dir: Path | None = None
    user_auth_file: Path | None = None


async def _preflight_tui_options(options: TuiOptions) -> None:
    runtime = await build_runtime(
        auth_path=options.config.auth_path,
        retry_policy=options.config.retry_policy,
    )
    try:
        runtime.providers.get(options.config.provider)
        for tool_name in set(options.allowed_tools):
            try:
                runtime.tools.get(tool_name)
            except UnknownToolError:
                prefixes = tuple(f"mcp__{server.name}__" for server in options.config.mcp_servers)
                if not tool_name.startswith(prefixes):
                    raise
        sessions = JsonlSessionStore(options.config.session_dir)
        if options.resume is not None:
            sessions.load(options.resume)
        elif options.continue_latest:
            sessions.latest()
    finally:
        await runtime.aclose()


def _rpc_command(options: TuiOptions) -> tuple[str, ...]:
    # Do NOT pass --provider / --model / --session-dir / --auth-file from the resolved
    # config: those are trust-gated (a trusted project's settings.json may set them), and
    # the subprocess resolves them itself using the parent's process-scoped trust decision.
    # Forwarding the parent's resolved values as CLI flags would outrank project settings (CLI is
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


def _rpc_env(options: TuiOptions | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if options is not None and options.project_trusted is not None:
        env["WISP_TRUST"] = "1" if options.project_trusted else "0"
    if options is not None and options.config.effort is not None:
        # Safe to forward the resolved value directly, unlike provider/model/
        # session_dir/auth_file (never sent as CLI args/env here) -- effort is
        # never trust-gated, so it already resolves identically in both
        # processes regardless of trust (see TuiOptions's docstring).
        env["WISP_EFFORT"] = options.config.effort
    if options is not None:
        env["WISP_CONTEXT_RESERVE_TOKENS"] = str(options.config.context_reserve_tokens)
        env["WISP_AUTO_COMPACTION"] = "1" if options.config.auto_compaction_enabled else "0"
    return env


def _stdin_is_interactive() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(isatty and isatty())


def _stdout_is_interactive() -> bool:
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(isatty and isatty())


def _stdio_is_interactive() -> bool:
    return _stdin_is_interactive() and _stdout_is_interactive()
