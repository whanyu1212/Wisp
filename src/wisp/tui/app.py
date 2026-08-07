"""Minimal Rich-based TUI shell for Wisp.

This module keeps the public launch surface and private compatibility aliases while
state, shell, and launch helpers live in focused sibling modules.
"""

from __future__ import annotations

from functools import partial

from rich.console import Console

from wisp.rpc import JsonlSubprocessRpcTransport, RpcController
from wisp.tools.context import ToolContext
from wisp.update_check import check_for_update

from . import launch as _launch
from . import live as _live
from . import shell as _shell
from . import state as _state
from .launch import TuiOptions, _preflight_tui_options, _rpc_command, _rpc_env
from .live import LiveFullscreenTui
from .rendering import TuiRendererKind, create_tui_renderer
from .shell import PromptReader, TuiController, TuiShell, _default_prompt_reader
from .textual_app import create_textual_tui

# Compatibility aliases for callers/tests that import private helpers from wisp.tui.app.
_stdin_is_interactive = _launch._stdin_is_interactive
_stdio_is_interactive = _launch._stdio_is_interactive
_stdout_is_interactive = _launch._stdout_is_interactive
LiveFullscreenInputInterrupted = _live.LiveFullscreenInputInterrupted
_compact_session_path = _shell._compact_session_path
_is_rpc_cancelled_message = _shell._is_rpc_cancelled_message
TuiInteractionState = _state.TuiInteractionState
TuiStatus = _state.TuiStatus
TuiViewState = _state.TuiViewState
_coerce_input_mode = _state._coerce_input_mode
_InputCancelled = _state._InputCancelled
_InputClosed = _state._InputClosed
_InputInterrupted = _state._InputInterrupted
_InputLine = _state._InputLine
_InputMode = _state._InputMode
_QuitPressed = _state._QuitPressed
_input_mode_for_status = _state._input_mode_for_status
_prompt_for_mode = _state._prompt_for_mode
_RpcEvent = _state._RpcEvent
_RpcEventsClosed = _state._RpcEventsClosed
_TuiSignal = _state._TuiSignal
_view_status_for_status = _state._view_status_for_status


async def run_tui(
    options: TuiOptions,
    *,
    console: Console | None = None,
    prompt_reader: PromptReader | None = None,
    controller: TuiController | None = None,
) -> None:
    """Run the minimal Wisp TUI shell."""

    selected_console = console or Console()
    selected_controller = controller
    owns_controller = selected_controller is None
    if selected_controller is None:
        await _preflight_tui_options(options)
        transport = await JsonlSubprocessRpcTransport.start(
            _rpc_command(options),
            env=_rpc_env(options),
        )
        selected_controller = RpcController(transport)

    textual_tui = None
    live_tui: LiveFullscreenTui | None = None
    selected_prompt_reader = prompt_reader or _default_prompt_reader
    # An injected prompt_reader means the caller is driving input themselves
    # (scripted/headless embeds and tests). The Textual app seizes the terminal
    # on launch, so only stand it up when no reader was supplied; otherwise fall
    # back to a line renderer and consume the injected reader, mirroring how the
    # fullscreen path declines to start the live UI when a reader is provided.
    if options.renderer is TuiRendererKind.textual and prompt_reader is None:
        # Hand the picker the policy this process already resolved rather than
        # letting it re-derive one. `options.config` reflects the `--auth-file`
        # override and the parent's trust decision; a fresh resolution inside the
        # app would see neither and could leave the real credential file listable.
        # `from_config` also guarantees `auth_path` is in the returned globs.
        textual_tui, selected_renderer = create_textual_tui(
            protected_paths=ToolContext.from_config(options.config).protected_paths,
        )
        selected_prompt_reader = textual_tui.read_prompt
    else:
        line_console_renderer = (
            TuiRendererKind.line
            if options.renderer is TuiRendererKind.textual
            else options.renderer
        )
        selected_renderer = create_tui_renderer(line_console_renderer, selected_console)
    if (
        options.renderer is TuiRendererKind.fullscreen
        and prompt_reader is None
        and console is None
        and _stdio_is_interactive()
    ):
        live_tui = LiveFullscreenTui()
        selected_renderer = live_tui
        selected_prompt_reader = live_tui.read_prompt

    shell = TuiShell(
        selected_controller,
        renderer=selected_renderer,
        prompt_reader=selected_prompt_reader,
        provider=options.config.provider,
        model=options.config.model,
        effort=options.config.effort,
        auth_path=options.config.auth_path,
        update_checker=partial(
            check_for_update,
            enabled=options.config.update_check_enabled,
        ),
    )
    try:
        if textual_tui is not None:
            await textual_tui.run_shell(shell.run)
        else:
            await shell.run()
    finally:
        try:
            if textual_tui is not None:
                await textual_tui.close()
            if live_tui is not None:
                await live_tui.close()
        finally:
            if owns_controller:
                await selected_controller.close()
