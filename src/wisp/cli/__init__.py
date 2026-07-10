"""Command-line interface for Wisp."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console

from wisp.agent.loop import Agent
from wisp.agent.prompt import resolve_project_context_root
from wisp.cli.auth import auth_app
from wisp.config import WispConfig
from wisp.events import ErrorEvent, MessageCompleted, MessageDelta
from wisp.providers.base import ProviderError
from wisp.runtime.registry import UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSessionStore, SessionError
from wisp.tools.approval import ToolApprovalDecision as ToolApprovalDecision
from wisp.tools.context import ToolContext
from wisp.tui.rendering import TuiRendererKind

from . import options as _cli_options
from . import output as _cli_output
from . import rpc as _cli_rpc
from . import tools as _cli_tools
from . import trust as _cli_trust
from .types import OutputMode, _JsonOutputModeError

# Compatibility aliases for callers/tests that import private helpers from wisp.cli.
_resolve_cli_trust = _cli_trust.resolve_cli_trust
_env_value = _cli_options._env_value
_has_callback_cli_args = _cli_options._has_callback_cli_args
_option_was_provided = _cli_options._option_was_provided
_output_mode_from_env = _cli_options._output_mode_from_env
_resolve_cli_mode = _cli_options._resolve_cli_mode
_resolve_tui_renderer = _cli_options._resolve_tui_renderer
_tui_renderer_from_env = _cli_options._tui_renderer_from_env

_exit_with_error = _cli_output._exit_with_error
_format_event_arguments = _cli_output._format_event_arguments
_format_event_output = _cli_output._format_event_output
_print_event_line = _cli_output._print_event_line
_render_json_events = _cli_output._render_json_events
_render_print_event = _cli_output._render_print_event
_truncate_inline = _cli_output._truncate_inline
_write_json_event = _cli_output._write_json_event
_writes_json_events = _cli_output._writes_json_events

_print_mode_tool_approval_policy = _cli_tools._print_mode_tool_approval_policy
_print_mode_tool_registry = _cli_tools._print_mode_tool_registry
_session_for_print_run = _cli_tools._session_for_print_run

# RPC output mode lives in wisp.cli.rpc; re-export its private helpers so
# existing callers and tests keep importing them from wisp.cli.
_RpcInputCommand = _cli_rpc._RpcInputCommand
_RpcInputClosed = _cli_rpc._RpcInputClosed
_RpcPromptCompleted = _cli_rpc._RpcPromptCompleted
_RpcSessionState = _cli_rpc._RpcSessionState
_RpcRunningPrompt = _cli_rpc._RpcRunningPrompt
_RpcPendingApproval = _cli_rpc._RpcPendingApproval
_RpcToolApprovalPolicy = _cli_rpc._RpcToolApprovalPolicy

_STDIN_READ_CHUNK_SIZE = _cli_rpc._STDIN_READ_CHUNK_SIZE
_STDIN_THREAD_POLL_INTERVAL = _cli_rpc._STDIN_THREAD_POLL_INTERVAL
_STDIN_THREAD_QUEUE_SIZE = _cli_rpc._STDIN_THREAD_QUEUE_SIZE
_MAX_QUEUED_RPC_COMMANDS = _cli_rpc._MAX_QUEUED_RPC_COMMANDS

_build_runtime_for_config = _cli_rpc._build_runtime_for_config
_run_rpc = _cli_rpc._run_rpc
_dispatch_rpc_command = _cli_rpc._dispatch_rpc_command
_rpc_session_state = _cli_rpc._rpc_session_state
_read_rpc_stdin = _cli_rpc._read_rpc_stdin
_rpc_stdin_needs_thread_reader = _cli_rpc._rpc_stdin_needs_thread_reader
_read_rpc_text_stdin = _cli_rpc._read_rpc_text_stdin
_read_rpc_thread_stdin = _cli_rpc._read_rpc_thread_stdin
_read_rpc_fd_stdin = _cli_rpc._read_rpc_fd_stdin
_send_rpc_input_line = _cli_rpc._send_rpc_input_line
_decode_rpc_stdin_line = _cli_rpc._decode_rpc_stdin_line
_start_rpc_prompt_command = _cli_rpc._start_rpc_prompt_command
_run_rpc_prompt_command = _cli_rpc._run_rpc_prompt_command
_updated_rpc_history = _cli_rpc._updated_rpc_history
_reject_rpc_command = _cli_rpc._reject_rpc_command
_handle_rpc_control_command = _cli_rpc._handle_rpc_control_command
_handle_rpc_configure_command = _cli_rpc._handle_rpc_configure_command
_handle_rpc_approval_command = _cli_rpc._handle_rpc_approval_command
_handle_rpc_cancel_command = _cli_rpc._handle_rpc_cancel_command
_write_rpc_command_error = _cli_rpc._write_rpc_command_error
_rpc_command_identity = _cli_rpc._rpc_command_identity
_rpc_command_type = _cli_rpc._rpc_command_type
_rpc_command_id = _cli_rpc._rpc_command_id
_parse_rpc_command = _cli_rpc._parse_rpc_command


app = typer.Typer(
    add_completion=False,
    help="Wisp: a Python, Pi-inspired coding agent.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(auth_app, name="auth")
app.add_typer(_cli_trust.trust_app, name="trust")


@app.callback()
def cli_callback(
    ctx: typer.Context,
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
        typer.Option("--mode", help="Output mode: text, JSONL events, RPC, or TUI."),
    ] = OutputMode.text,
    tui_renderer: Annotated[
        TuiRendererKind,
        typer.Option(
            "--tui-renderer",
            help="TUI renderer to use with --mode tui.",
        ),
    ] = TuiRendererKind.line,
    all_tools: Annotated[
        bool,
        typer.Option(
            "--all-tools/--no-all-tools",
            help=(
                "Expose the full tool registry in agent modes (unsafe calls still prompt). "
                "Defaults on for --mode tui, off otherwise."
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
    """Run Wisp in the initial print-mode CLI."""

    if ctx.invoked_subcommand is not None:
        return

    console = Console(stderr=True)
    # Mode is resolved from CLI flags / real-env WISP_MODE only, before trust is
    # resolved and project-local config is applied.
    mode_was_provided = _option_was_provided(ctx, "mode")
    resolved_mode = _resolve_cli_mode(
        mode,
        prompt=prompt,
        mode_was_provided=mode_was_provided,
        console=console,
    )
    resolved_tui_renderer = tui_renderer
    resolved_all_tools = all_tools
    if resolved_mode is OutputMode.tui:
        resolved_tui_renderer = _resolve_tui_renderer(
            tui_renderer,
            renderer_was_provided=_option_was_provided(ctx, "tui_renderer"),
            console=console,
        )
        # The interactive TUI defaults to the full toolset — matching the dedicated
        # `tui` command — so the legacy `--mode tui` / WISP_MODE=tui path isn't a
        # toolless agent. An explicit --all-tools/--no-all-tools still wins.
        if not _option_was_provided(ctx, "all_tools"):
            resolved_all_tools = True

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
    # print/JSON resolve interactively here (the prompt goes to stderr, keeping JSON
    # stdout clean); rpc/tui prompt out-of-band, so at startup they use only the
    # non-interactive signals (an undecided project is untrusted until answered).
    cwd = Path.cwd()
    project_context_root = resolve_project_context_root(cwd)
    if resolved_mode in (OutputMode.rpc, OutputMode.tui):
        trusted = _cli_trust.trusted_noninteractive(project_context_root)
    else:
        trusted = _resolve_cli_trust(project_context_root).trusted

    config_overrides = _cli_rpc._ConfigOverrides(
        provider=provider,
        model=model,
        session_dir=session_dir,
        auth_path=auth_file,
    )
    config = config_overrides.build(
        trusted=trusted,
        project_dir=project_context_root,
    )
    try:
        if resolved_mode is OutputMode.rpc:
            anyio.run(
                _run_rpc,
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
    except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
        if _writes_json_events(resolved_mode):
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
    # The TUI resolves trust out-of-band (its RPC subprocess prompts via TrustCommand),
    # so gate the project settings layer on the non-interactive trust signals here; an
    # undecided project's local settings are not applied until the prompt is answered.
    project_context_root = resolve_project_context_root(Path.cwd())
    trusted = _cli_trust.trusted_noninteractive(project_context_root)
    _validate_session_and_iteration_options(
        resume=resume,
        continue_latest=continue_latest,
        max_tool_iterations=max_tool_iterations,
        mode=OutputMode.text,
        console=console,
    )
    config = WispConfig.from_env(
        session_dir=session_dir,
        auth_path=auth_file,
        project_dir=project_context_root,
        trusted=trusted,
    )
    renderer = TuiRendererKind.line if line else TuiRendererKind.textual
    try:
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
            # These default to None on the `tui` command, so they are non-None only when
            # the user explicitly set them — exactly the values that should override a
            # trusted project's settings in the RPC subprocess.
            user_session_dir=session_dir,
            user_auth_file=auth_file,
        )
    except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
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
    user_provider: str | None = None,
    user_model: str | None = None,
    user_session_dir: Path | None = None,
    user_auth_file: Path | None = None,
) -> None:
    from wisp.tui import TuiOptions, run_tui

    anyio.run(
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
            user_provider=user_provider,
            user_model=user_model,
            user_session_dir=user_session_dir,
            user_auth_file=user_auth_file,
        ),
    )


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
    runtime = await _build_runtime_for_config(config)
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    history = session.read_messages() if session is not None else ()
    agent = Agent(
        provider=provider,
        sessions=sessions,
        events=runtime.events,
        model=config.model,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
        ),
        tool_context=ToolContext.from_config(config),
        tool_approval_policy=_print_mode_tool_approval_policy(approve_unsafe_tools),
        max_tool_iterations=max_tool_iterations,
        trusted=trusted,
        project_context_root=project_context_root,
    )

    events = agent.run(prompt, session=session, history=history)
    if mode is OutputMode.json:
        await _render_json_events(events)
        return

    event_console = Console(stderr=True, soft_wrap=True)
    wrote_tokens = False
    streamed_text_for_message = False
    stderr_needs_separator = False
    failure_message: str | None = None
    try:
        async for event in events:
            if isinstance(event, MessageDelta) and event.content_kind == "text":
                if event.delta:
                    sys.stdout.write(event.delta)
                    sys.stdout.flush()
                    wrote_tokens = True
                    streamed_text_for_message = True
                    stderr_needs_separator = True
            elif isinstance(event, MessageCompleted):
                if not streamed_text_for_message and event.content:
                    sys.stdout.write(event.content)
                    sys.stdout.flush()
                    wrote_tokens = True
                    stderr_needs_separator = True
                streamed_text_for_message = False
            elif isinstance(event, ErrorEvent):
                failure_message = event.message
            else:
                if stderr_needs_separator and _print_event_line(event) is not None:
                    event_console.print()
                    stderr_needs_separator = False
                _render_print_event(event, event_console)
    except Exception:
        if failure_message is None:
            raise

    if wrote_tokens:
        sys.stdout.write("\n")
    if failure_message is not None:
        raise ProviderError(failure_message)
