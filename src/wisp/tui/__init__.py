"""Minimal terminal UI shell for Wisp."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    HistoricalSkillInvocation,
    HistoricalToolCard,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    history_entries_from_rpc_messages,
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
from wisp.tui.state import TuiExitReason, TuiInteractionState, TuiStatus, TuiViewState

if TYPE_CHECKING:
    from wisp.tui.app import run_tui


def __getattr__(name: str) -> object:
    if name != "run_tui":
        raise AttributeError(name)
    from wisp.tui.app import run_tui

    globals()[name] = run_tui
    return run_tui


__all__ = [
    "FullscreenTuiRenderer",
    "FullscreenTuiState",
    "HistoricalSkillInvocation",
    "HistoricalToolCard",
    "HistoricalTranscriptEntry",
    "HistoricalTranscriptMessage",
    "LineTuiRenderer",
    "LiveFullscreenInputInterrupted",
    "LiveFullscreenTui",
    "TUI_HISTORY_MESSAGE_LIMIT",
    "TuiInteractionState",
    "TuiExitReason",
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
    "history_entries_from_rpc_messages",
    "history_from_rpc_messages",
    "run_tui",
]
