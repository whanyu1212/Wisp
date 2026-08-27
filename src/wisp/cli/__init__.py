"""Command-line interface for Wisp."""

from __future__ import annotations

import os
import sys
from functools import partial
from pathlib import Path
from typing import Annotated, NoReturn

import anyio
import typer
from rich.console import Console

from wisp import __version__
from wisp.agent.prompt import resolve_project_context_root
from wisp.cli.auth import auth_app
from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.config import WispConfig
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
)
from wisp.providers.base import ProviderError
from wisp.rpc.configuration import _ConfigOverrides
from wisp.rpc.host import build_runtime_for_config
from wisp.runtime.api import WispRuntime
from wisp.runtime.registry import UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSessionStore, SessionError
from wisp.skills.lifecycle import discover_skill_catalog
from wisp.tools.approval import ToolApprovalDecision as ToolApprovalDecision
from wisp.tools.result import ToolError
from wisp.tui.rendering import TuiRendererKind

from . import options as _cli_options
from . import output as _cli_output
from . import rpc as _cli_rpc
from . import skills as _cli_skills
from . import tools as _cli_tools
from . import trust as _cli_trust
from . import update as _cli_update
from .types import OutputMode, _JsonOutputModeError, _RenderedPrintError

# Compatibility aliases for callers/tests that import private helpers from wisp.cli.
_resolve_cli_trust = _cli_trust.resolve_cli_trust
_env_value = _cli_options._env_value
_has_callback_cli_args = _cli_options._has_callback_cli_args
_option_was_provided = _cli_options._option_was_provided
_output_mode_from_env = _cli_options._output_mode_from_env
_resolve_cli_mode = _cli_options._resolve_cli_mode
_resolve_tui_renderer = _cli_options._resolve_tui_renderer
_terminal_is_interactive = _cli_options._terminal_is_interactive
_tui_renderer_from_env = _cli_options._tui_renderer_from_env

_format_event_arguments = _cli_output._format_event_arguments
_format_event_output = _cli_output._format_event_output
_format_usage_cost = _cli_output._format_usage_cost
_print_event_line = _cli_output._print_event_line
_render_json_events = _cli_output._render_json_events
_render_print_event = _cli_output._render_print_event
_truncate_inline = _cli_output._truncate_inline
_write_json_event = _cli_output._write_json_event
_writes_json_events = _cli_output._writes_json_events

_print_mode_tool_approval_policy = _cli_tools._print_mode_tool_approval_policy
_print_mode_tool_registry = _cli_tools._print_mode_tool_registry
_session_for_print_run = _cli_tools._session_for_print_run


def _exit_with_error(message: str, *, mode: OutputMode, console: Console) -> NoReturn:
    if mode is OutputMode.rpc:
        _cli_rpc._write_startup_error(message)
        raise typer.Exit(1)
    _cli_output._exit_with_error(message, mode=mode, console=console)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"wisp {__version__}")
        raise typer.Exit()


app = typer.Typer(
    add_completion=False,
    help="Wisp: a terminal-first coding agent. Run without arguments to launch the TUI.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(auth_app, name="auth")
app.add_typer(_cli_trust.trust_app, name="trust")
app.command("skills")(_cli_skills.skills_command)
app.command("update")(_cli_update.update_command)


@app.callback()
def cli_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed Wisp version and exit.",
        ),
    ] = False,
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", "-p", help="Run one prompt and exit."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option(help="Provider to use, e.g. fake or openai."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Model name for the selected provider."),
    ] = None,
    session_dir: Annotated[
        Path | None,
        typer.Option(help="Directory for JSONL session files."),
    ] = None,
    auth_file: Annotated[
        Path | None,
        typer.Option(help="Path to Wisp's private provider auth JSON file."),
    ] = None,
    mode: Annotated[
        OutputMode,
        typer.Option(
            "--mode",
            help=(
                "Output mode: text, JSONL events, RPC, or TUI. A bare interactive invocation "
                "defaults to TUI; other invocations default to text."
            ),
            show_default=False,
        ),
    ] = OutputMode.text,
    tui_renderer: Annotated[
        TuiRendererKind,
        typer.Option(
            "--tui-renderer",
            help="TUI renderer to use with --mode tui.",
        ),
    ] = TuiRendererKind.line,
    no_synchronized_output: Annotated[
        bool,
        typer.Option(
            "--no-synchronized-output",
            help="Disable capability-gated synchronized frames in the Textual TUI.",
        ),
    ] = False,
    all_tools: Annotated[
        bool,
        typer.Option(
            "--all-tools/--no-all-tools",
            help=(
                "Expose the full tool registry in agent modes (unsafe calls still prompt). "
                "Defaults on for TUI modes, off otherwise."
            ),
        ),
    ] = False,
    allow_read_tools: Annotated[
        bool,
        typer.Option(help="Expose sandboxed read-only tools in agent modes."),
    ] = False,
    allow_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-tool",
            help="Expose a specific tool in agent modes. Can be repeated.",
        ),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume", help="Continue a session by JSONL path, filename, id, or id prefix."
        ),
    ] = None,
    continue_latest: Annotated[
        bool,
        typer.Option("--continue", help="Continue the latest session in the session directory."),
    ] = False,
    approve_unsafe_tools: Annotated[
        bool,
        typer.Option(
            "--yes",
            "--allow-unsafe-tool-execution",
            help="Approve non-interactive execution of mutating and command tools.",
        ),
    ] = False,
    max_tool_iterations: Annotated[
        int | None,
        typer.Option(
            "--max-tool-iterations",
            help="Optional cap on model/tool rounds. Defaults to uncapped.",
        ),
    ] = None,
) -> None:
    """Run Wisp through its selected or interactive-default interface."""

    if ctx.invoked_subcommand is not None:
        return

    console = Console(stderr=True)
    # Explicit CLI/env modes still win; only a bare interactive terminal defaults
    # to the fullscreen TUI before trust and project config are resolved.
    mode_was_provided = _option_was_provided(ctx, "mode")
    bare_interactive_invocation = (
        prompt is None and not _has_callback_cli_args(ctx) and _terminal_is_interactive()
    )
    resolved_mode = _resolve_cli_mode(
        OutputMode.tui if bare_interactive_invocation else mode,
        prompt=prompt,
        mode_was_provided=mode_was_provided,
        console=console,
    )
    resolved_tui_renderer = TuiRendererKind.textual if bare_interactive_invocation else tui_renderer
    resolved_all_tools = all_tools
    if resolved_mode is OutputMode.tui:
        resolved_tui_renderer = _resolve_tui_renderer(
            resolved_tui_renderer,
            renderer_was_provided=_option_was_provided(ctx, "tui_renderer"),
            console=console,
        )
        # Every interactive TUI path defaults to the full toolset, matching the
        # dedicated `tui` command. An explicit --all-tools/--no-all-tools still wins.
        if not _option_was_provided(ctx, "all_tools"):
            resolved_all_tools = True

    rpc_handshake_complete = False
    if resolved_mode is OutputMode.rpc:
        rpc_handshake_complete = anyio.run(_cli_rpc._negotiate_rpc_connection)
        if not rpc_handshake_complete:
            return

    if prompt is None and resolved_mode is not OutputMode.tui and not _has_callback_cli_args(ctx):
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    if resolved_mode is OutputMode.rpc:
        if prompt is not None:
            _exit_with_error(
                "--prompt is not used with --mode rpc; send prompt commands on stdin",
                mode=resolved_mode,
                console=console,
            )
    elif resolved_mode is OutputMode.tui:
        if prompt is not None:
            _exit_with_error(
                "--prompt is not used with --mode tui; enter prompts in the TUI",
                mode=resolved_mode,
                console=console,
            )
    elif prompt is None:
        # Keep direct calls to the callback friendly in tests or embedded usage.
        raise typer.Exit(0)

    _validate_session_and_iteration_options(
        resume=resume,
        continue_latest=continue_latest,
        max_tool_iterations=max_tool_iterations,
        mode=resolved_mode,
        console=console,
    )

    # Resolve project trust and gate project-local config (the .wisp/settings.json
    # layer) on it: an untrusted repo must not be able to redirect the credential
    # file or override user defaults. Trust is read from safe sources only — the
    # global store and the real-env WISP_TRUST — never from project-controlled files.
    # Print/JSON and TUI resolve interactively here (the prompt goes to stderr,
    # keeping JSON stdout clean and ensuring TUI trust is decided before the UI or
    # RPC subprocess starts). Standalone RPC prompts out-of-band.
    cwd = Path.cwd()
    project_context_root = resolve_project_context_root(cwd)
    if resolved_mode is OutputMode.rpc:
        trusted = _cli_trust.trusted_noninteractive(project_context_root)
    else:
        trusted = _resolve_cli_trust(project_context_root).trusted

    config_overrides = _ConfigOverrides(
        provider=provider,
        model=model,
        session_dir=session_dir,
        auth_path=auth_file,
    )
    try:
        config = config_overrides.build(
            trusted=trusted,
            project_dir=project_context_root,
        )
    except ValueError as exc:
        _exit_with_error(str(exc), mode=resolved_mode, console=console)
    try:
        if resolved_mode is OutputMode.rpc:
            anyio.run(
                partial(
                    _cli_rpc._run_rpc,
                    config,
                    resolved_all_tools,
                    allow_read_tools,
                    tuple(allow_tool or ()),
                    resume,
                    continue_latest,
                    approve_unsafe_tools,
                    max_tool_iterations,
                    trusted,
                    config_overrides,
                    project_context_root,
                    handshake_complete=rpc_handshake_complete,
                )
            )
        elif resolved_mode is OutputMode.tui:
            _run_tui_from_cli_options(
                config=config,
                all_tools=resolved_all_tools,
                allow_read_tools=allow_read_tools,
                allowed_tools=tuple(allow_tool or ()),
                resume=resume,
                continue_latest=continue_latest,
                approve_unsafe_tools=approve_unsafe_tools,
                max_tool_iterations=max_tool_iterations,
                renderer=resolved_tui_renderer,
                synchronized_output=not no_synchronized_output,
                project_trusted=trusted,
                # Forward the user's explicit --provider/--model/--session-dir/--auth-file
                # (each None unless set) so the legacy `--mode tui` path keeps honoring
                # them; the launcher no longer launders the resolved config into flags.
                user_provider=provider,
                user_model=model,
                user_session_dir=session_dir,
                user_auth_file=auth_file,
            )
        else:
            assert prompt is not None
            anyio.run(
                _run_print,
                prompt,
                config,
                resolved_all_tools,
                allow_read_tools,
                tuple(allow_tool or ()),
                resume,
                continue_latest,
                approve_unsafe_tools,
                max_tool_iterations,
                resolved_mode,
                trusted,
                project_context_root,
            )
    except _JsonOutputModeError as exc:
        raise typer.Exit(1) from exc
    except (ProviderError, SessionError, ToolError, UnknownProviderError, UnknownToolError) as exc:
        if isinstance(exc, _RenderedPrintError):
            pass
        elif resolved_mode is OutputMode.rpc:
            _cli_rpc._write_startup_error(str(exc))
        elif _writes_json_events(resolved_mode):
            _write_json_event(ErrorEvent(message=str(exc)))
        else:
            console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command("tui")
def tui_command(
    line: Annotated[
        bool,
        typer.Option("--line", help="Use the simple line renderer instead of the Textual TUI."),
    ] = False,
    no_synchronized_output: Annotated[
        bool,
        typer.Option(
            "--no-synchronized-output",
            help="Disable capability-gated synchronized frames in the Textual TUI.",
        ),
    ] = False,
    session_dir: Annotated[
        Path | None,
        typer.Option(help="Directory for JSONL session files."),
    ] = None,
    auth_file: Annotated[
        Path | None,
        typer.Option(help="Path to Wisp's private provider auth JSON file."),
    ] = None,
    all_tools: Annotated[
        bool,
        typer.Option(
            "--all-tools/--no-all-tools",
            help="Expose the full tool registry (default on; unsafe calls still prompt).",
        ),
    ] = True,
    allow_read_tools: Annotated[
        bool,
        typer.Option(help="Expose sandboxed read-only tools in agent modes."),
    ] = False,
    allow_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-tool",
            help="Expose a specific tool in agent modes. Can be repeated.",
        ),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume", help="Continue a session by JSONL path, filename, id, or id prefix."
        ),
    ] = None,
    continue_latest: Annotated[
        bool,
        typer.Option("--continue", help="Continue the latest session in the session directory."),
    ] = False,
    approve_unsafe_tools: Annotated[
        bool,
        typer.Option(
            "--yes",
            "--allow-unsafe-tool-execution",
            help="Approve non-interactive execution of mutating and command tools.",
        ),
    ] = False,
    max_tool_iterations: Annotated[
        int | None,
        typer.Option(
            "--max-tool-iterations",
            help="Optional cap on model/tool rounds. Defaults to uncapped.",
        ),
    ] = None,
) -> None:
    """Start Wisp's fullscreen TUI."""

    console = Console(stderr=True)
    # Resolve trust before config, preflight, or terminal UI startup. The persisted
    # decision is then observed by the RPC subprocess, so normal TUI launches never
    # enter the fullscreen interface with project trust still undecided.
    project_context_root = resolve_project_context_root(Path.cwd())
    trusted = _resolve_cli_trust(project_context_root).trusted
    _validate_session_and_iteration_options(
        resume=resume,
        continue_latest=continue_latest,
        max_tool_iterations=max_tool_iterations,
        mode=OutputMode.text,
        console=console,
    )
    renderer = TuiRendererKind.line if line else TuiRendererKind.textual
    try:
        config = WispConfig.from_env(
            session_dir=session_dir,
            auth_path=auth_file,
            project_dir=project_context_root,
            trusted=trusted,
        )
        _run_tui_from_cli_options(
            config=config,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=tuple(allow_tool or ()),
            resume=resume,
            continue_latest=continue_latest,
            approve_unsafe_tools=approve_unsafe_tools,
            max_tool_iterations=max_tool_iterations,
            renderer=renderer,
            synchronized_output=not no_synchronized_output,
            project_trusted=trusted,
            # These default to None on the `tui` command, so they are non-None only when
            # the user explicitly set them — exactly the values that should override a
            # trusted project's settings in the RPC subprocess.
            user_session_dir=session_dir,
            user_auth_file=auth_file,
        )
    except (
        ProviderError,
        SessionError,
        ToolError,
        UnknownProviderError,
        UnknownToolError,
        ValueError,
    ) as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console-script entry point."""

    app()


def _validate_session_and_iteration_options(
    *,
    resume: str | None,
    continue_latest: bool,
    max_tool_iterations: int | None,
    mode: OutputMode,
    console: Console,
) -> None:
    """Validate options shared by the callback and the `tui` subcommand."""

    if resume is not None and continue_latest:
        _exit_with_error(
            "use either --resume or --continue, not both",
            mode=mode,
            console=console,
        )
    if max_tool_iterations is not None and max_tool_iterations < 0:
        _exit_with_error(
            "--max-tool-iterations must be non-negative",
            mode=mode,
            console=console,
        )


def _run_tui_from_cli_options(
    *,
    config: WispConfig,
    all_tools: bool,
    allow_read_tools: bool,
    allowed_tools: tuple[str, ...],
    resume: str | None,
    continue_latest: bool,
    approve_unsafe_tools: bool,
    max_tool_iterations: int | None,
    renderer: TuiRendererKind,
    synchronized_output: bool,
    project_trusted: bool,
    user_provider: str | None = None,
    user_model: str | None = None,
    user_session_dir: Path | None = None,
    user_auth_file: Path | None = None,
) -> None:
    from wisp.tui import TuiExitReason, TuiOptions, run_tui

    restart_argv = tuple(sys.orig_argv)
    restart_cwd = Path.cwd()
    restart_environment = dict(os.environ)
    result = anyio.run(
        run_tui,
        TuiOptions(
            config=config,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
            resume=resume,
            continue_latest=continue_latest,
            approve_unsafe_tools=approve_unsafe_tools,
            max_tool_iterations=max_tool_iterations,
            renderer=renderer,
            synchronized_output=synchronized_output,
            project_trusted=project_trusted,
            user_provider=user_provider,
            user_model=user_model,
            user_session_dir=user_session_dir,
            user_auth_file=user_auth_file,
        ),
    )
    if result is TuiExitReason.restart_requested:
        try:
            _restart_current_process(
                restart_argv,
                cwd=restart_cwd,
                environment=restart_environment,
            )
        except OSError as exc:
            typer.echo(f"Wisp was updated, but restart failed: {exc}", err=True)
            raise typer.Exit(1) from exc


def _restart_current_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Replace this process with the exact invocation captured before TUI startup."""

    if not argv:
        raise OSError("the original process invocation is unavailable")
    os.chdir(cwd)
    os.execvpe(argv[0], list(argv), environment)


async def _run_print(
    prompt: str,
    config: WispConfig,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    mode: OutputMode = OutputMode.text,
    trusted: bool = False,
    project_context_root: Path | None = None,
) -> None:
    runtime = await build_runtime_for_config(config)
    try:
        await _run_print_with_runtime(
            prompt,
            config,
            runtime,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
            resume=resume,
            continue_latest=continue_latest,
            approve_unsafe_tools=approve_unsafe_tools,
            max_tool_iterations=max_tool_iterations,
            mode=mode,
            trusted=trusted,
            project_context_root=project_context_root,
        )
    finally:
        await runtime.aclose()


async def _run_print_with_runtime(
    prompt: str,
    config: WispConfig,
    runtime: WispRuntime,
    *,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    mode: OutputMode = OutputMode.text,
    trusted: bool = False,
    project_context_root: Path | None = None,
) -> None:
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    history = session.read_context_messages() if session is not None else ()
    skill_catalog = await discover_skill_catalog(
        project_root=project_context_root,
        trusted=trusted,
        protected_paths=config.protected_paths,
    )
    initial_configuration = resolve_coding_session_configuration(
        config,
        providers=runtime.providers,
        models=runtime.models,
        trusted=trusted,
        skill_catalog=skill_catalog,
    )
    agent = CodingSession.from_configuration(
        initial_configuration,
        sessions=sessions,
        events=runtime.events,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
            ignored_unknown_prefixes=runtime.unavailable_tool_prefixes,
        ),
        tool_approval_policy=_print_mode_tool_approval_policy(approve_unsafe_tools),
        max_tool_iterations=max_tool_iterations,
        project_context_root=project_context_root,
    )

    events = agent.run(prompt, session=session, history=history)
    if mode is OutputMode.json:
        for event in runtime.startup_events:
            _write_json_event(event)
        await _render_json_events(events)
        return

    event_console = Console(stderr=True, soft_wrap=True)
    for event in runtime.startup_events:
        if isinstance(event, ErrorEvent):
            event_console.print(f"error: {event.message}", markup=False)
        else:
            _render_print_event(event, event_console)
    wrote_tokens = False
    stdout_line_terminated = False
    streamed_text_for_message = False
    stderr_needs_separator = False
    failure_message: str | None = None
    failure_was_rendered = False
    rendered_overflow_failures: set[str] = set()
    try:
        async for event in events:
            if isinstance(event, MessageDelta) and event.content_kind == "text":
                if event.delta:
                    sys.stdout.write(event.delta)
                    sys.stdout.flush()
                    wrote_tokens = True
                    stdout_line_terminated = False
                    streamed_text_for_message = True
                    stderr_needs_separator = True
            elif isinstance(event, MessageCompleted):
                if not streamed_text_for_message and event.content:
                    sys.stdout.write(event.content)
                    sys.stdout.flush()
                    wrote_tokens = True
                    stdout_line_terminated = False
                    stderr_needs_separator = True
                streamed_text_for_message = False
                if event.cost is not None:
                    if stderr_needs_separator:
                        event_console.print()
                        stderr_needs_separator = False
                    event_console.print(_format_usage_cost(event.cost), markup=False)
            elif isinstance(event, ErrorEvent):
                failure_message = event.message
                failure_was_rendered = event.message in rendered_overflow_failures
            else:
                line = _print_event_line(event)
                if stderr_needs_separator and line is not None:
                    if isinstance(event, CompactionStarted) and event.reason in {
                        "threshold",
                        "overflow",
                    }:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        stdout_line_terminated = True
                    else:
                        event_console.print()
                    stderr_needs_separator = False
                _render_print_event(event, event_console)
                if (
                    isinstance(event, CompactionCompleted)
                    and event.reason == "overflow"
                    and not event.will_retry
                    and line is not None
                ):
                    rendered_overflow_failures.add(line)
    except Exception:
        if failure_message is None:
            raise

    if wrote_tokens and not stdout_line_terminated:
        sys.stdout.write("\n")
    if failure_message is not None:
        if failure_was_rendered:
            raise _RenderedPrintError(failure_message)
        raise ProviderError(failure_message)
