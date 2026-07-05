"""Minimal Rich-based TUI shell for Wisp.

This module keeps the public launch surface and private compatibility aliases while
state, shell, and launch helpers live in focused sibling modules.
"""

from __future__ import annotations

from rich.console import Console

from wisp.rpc import JsonlSubprocessRpcTransport, RpcController

from . import launch as _launch
from . import live as _live
from . import shell as _shell
from . import state as _state
from .launch import TuiOptions, _preflight_tui_options, _rpc_command, _rpc_env
from .live import LiveFullscreenTui
from .rendering import TuiRendererKind, create_tui_renderer
from .shell import PromptReader, TuiController, TuiShell, _default_prompt_reader

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
_InputClosed = _state._InputClosed
_InputInterrupted = _state._InputInterrupted
_InputLine = _state._InputLine
_InputMode = _state._InputMode
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
        transport = await JsonlSubprocessRpcTransport.start(_rpc_command(options), env=_rpc_env())
        selected_controller = RpcController(transport)

    live_tui: LiveFullscreenTui | None = None
    selected_renderer = create_tui_renderer(options.renderer, selected_console)
    selected_prompt_reader = prompt_reader or _default_prompt_reader
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
        auth_path=options.config.auth_path,
    )
    try:
        await shell.run()
    finally:
        try:
            if live_tui is not None:
                await live_tui.close()
        finally:
            if owns_controller:
                await selected_controller.close()
