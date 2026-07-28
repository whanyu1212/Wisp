"""Minimal terminal UI shell for Wisp."""

from wisp.tui.app import run_tui
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    HistoricalTranscriptMessage,
    history_from_rpc_messages,
)
from wisp.tui.launch import TuiOptions
from wisp.tui.live import LiveFullscreenInputInterrupted, LiveFullscreenTui
from wisp.tui.rendering import (
    FullscreenTuiRenderer,
    FullscreenTuiState,
    LineTuiRenderer,
    TuiRenderer,
    TuiRendererKind,
    TuiTranscriptEntry,
    TuiViewSnapshot,
    create_tui_renderer,
    format_tui_footer_lines,
    format_tui_footer_text,
)
from wisp.tui.shell import TuiShell
from wisp.tui.state import TuiInteractionState, TuiStatus, TuiViewState

__all__ = [
    "FullscreenTuiRenderer",
    "FullscreenTuiState",
    "HistoricalTranscriptMessage",
    "LineTuiRenderer",
    "LiveFullscreenInputInterrupted",
    "LiveFullscreenTui",
    "TUI_HISTORY_MESSAGE_LIMIT",
    "TuiInteractionState",
    "TuiOptions",
    "TuiRenderer",
    "TuiRendererKind",
    "TuiShell",
    "TuiTranscriptEntry",
    "TuiStatus",
    "TuiViewSnapshot",
    "TuiViewState",
    "create_tui_renderer",
    "format_tui_footer_lines",
    "format_tui_footer_text",
    "history_from_rpc_messages",
    "run_tui",
]
