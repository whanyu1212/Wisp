"""Textual widget routing and cross-controller orchestration for Wisp's TUI.

State ownership is deliberately narrow: ``TextualInputController`` owns the
process-local input queue, prompt recall, and compact echoes;
``TextualOverlayController`` owns transient overlay/focus transitions;
``TextualHistoryController`` owns persisted-history retention; and
``TextualTranscriptController`` owns transient live transcript presentation.
``MarkdownStreamController`` remains the owner of native asynchronous Markdown
writes. ``TextualTui`` composes these owners, routes framework events, and owns
Textual-specific mounting and layout restoration; it does not interpret agent,
provider, session, approval, or RPC policy.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter
from typing import cast

from rich.console import RenderableType
from rich.segment import Segment
from textual import events, messages, on, work
from textual._compositor import ChopsUpdate, Compositor, CompositorMap, LayoutUpdate
from textual.app import App, ComposeResult
from textual.await_remove import AwaitRemove
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.geometry import Offset, Size
from textual.screen import Screen
from textual.strip import Strip
from textual.widget import AwaitMount, Widget
from textual.widgets import HelpPanel, KeyPanel, Static, TextArea

from wisp.config import WispConfig
from wisp.events import (
    RpcSessionSummary,
    RpcSkillCatalogSnapshot,
    SessionStats,
    SkillInvoked,
    ToolApprovalRequested,
    TrustRequested,
)
from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tools.context import ToolContext
from wisp.tui.commands import (
    DEFAULT_TUI_COMMAND_CATALOG,
    TEXTUAL_LOCAL_COMMAND_DESCRIPTORS,
    SlashCommandSpec,
    TuiCommandCatalog,
)
from wisp.tui.connect_widget import ConnectPanel, ConnectPanelMode
from wisp.tui.connections import ConnectionProviderStatus
from wisp.tui.context_widget import ContextStatusOverlay
from wisp.tui.diagnostics import (
    DisplayFrameCacheOutcome,
    DisplayUpdateDiagnostic,
    DisplayUpdateKind,
    InputEventCategory,
    InputLatencyDiagnostic,
    TuiDiagnosticsSink,
    record_display_update,
    record_input_latency,
)
from wisp.tui.diff_presentation import DiffPresentation
from wisp.tui.diff_viewer import DiffViewer
from wisp.tui.file_index import (
    FileIndexConfig,
    FileIndexRequest,
    ProjectSnapshot,
    collect_project_snapshot,
    format_file_reference,
)
from wisp.tui.file_result_presentation import FileResultPresentation
from wisp.tui.file_suggest import FileSuggest
from wisp.tui.input_priority import InputPriorityPolicy, InputPriorityToken
from wisp.tui.input_types import QueueSubmissionKind, TuiSubmission
from wisp.tui.overlay import (
    OverlayKind,
    OverlayOperation,
    TextualOverlayController,
    TranscriptViewportState,
)
from wisp.tui.presentation_clock import PresentationClock
from wisp.tui.process_lifecycle import ProcessLifecyclePresentation
from wisp.tui.prompt_history_widget import PromptHistoryPicker
from wisp.tui.rendering import TuiRenderer, TuiViewSnapshot, _unsent_submission_text
from wisp.tui.skills import skill_catalog_text, skill_invocation_text
from wisp.tui.state import (
    TuiCancelRequested,
    TuiExitReason,
    TuiQueueRestoreRequested,
    TuiQuitRequested,
)
from wisp.tui.stream_buffer import MarkdownStreamController
from wisp.tui.terminal_writes import TerminalWriteObserver
from wisp.tui.textual_input import TextualInputController
from wisp.tui.textual_renderer import TextualTuiRenderer
from wisp.tui.textual_transcript import TextualTranscriptController
from wisp.tui.theme import (
    DEFAULT_THEME_NAME,
    PAPER_THEME_NAME,
    WISP_DARK_THEME_NAMES,
    WISP_THEME_BY_NAME,
    WISP_THEME_BY_SLUG,
    WISP_THEME_NAMES,
    WISP_THEMES,
)
from wisp.tui.theme_picker import ThemePicker
from wisp.tui.theme_preference import ThemePreferenceState, load_theme_state, save_theme_state
from wisp.tui.tool_call import ToolActionStatus
from wisp.tui.update_prompt import UpdatePrompt
from wisp.tui.update_types import UpdatePromptAction
from wisp.tui.widgets import (
    ComposerRegion,
    DecisionPanel,
    HistoryNavigation,
    HistoryNavigationIntent,
    JumpToLatest,
    LineMessage,
    ModelPicker,
    OperationIndicator,
    ProcessCard,
    PromptEditor,
    SessionPicker,
    SlashSuggest,
    StatusBar,
    StreamMessage,
    ToolCard,
    Transcript,
    TranscriptEmptyState,
    WorkingIndicator,
)
from wisp.update_check import UpdateAvailable

_TERMINAL_CONTROL_INITIATORS = re.compile(r"[\x1b\x80-\x9f]")


def _sanitize_terminal_strip(strip: Strip) -> Strip:
    """Remove terminal-sequence initiators from ordinary rendered cells."""

    sanitized: list[Segment] = []
    changed = False
    for segment in strip:
        if segment.control or _TERMINAL_CONTROL_INITIATORS.search(segment.text) is None:
            sanitized.append(segment)
            continue
        changed = True
        sanitized.append(
            Segment(
                _TERMINAL_CONTROL_INITIATORS.sub("", segment.text),
                segment.style,
                segment.control,
            )
        )
    return Strip(sanitized, cell_length=strip.cell_length) if changed else strip


def _sanitize_terminal_update(renderable: RenderableType | None) -> RenderableType | None:
    """Neutralize untrusted terminal controls before cache comparison and output."""

    if isinstance(renderable, LayoutUpdate):
        changed = False
        layout_lines: list[Iterable[Strip]] = []
        for line in renderable.strips:
            sanitized_layout_line = tuple(_sanitize_terminal_strip(strip) for strip in line)
            changed = changed or any(
                sanitized is not original
                for sanitized, original in zip(sanitized_layout_line, line, strict=True)
            )
            layout_lines.append(sanitized_layout_line)
        return LayoutUpdate(layout_lines, renderable.region) if changed else renderable
    if isinstance(renderable, ChopsUpdate):
        changed = False
        chop_lines: list[dict[int, Strip | None]] = []
        for chop_line in renderable.chops:
            sanitized_chop_line: dict[int, Strip | None] = {}
            for start, strip in chop_line.items():
                sanitized = None if strip is None else _sanitize_terminal_strip(strip)
                changed = changed or sanitized is not strip
                sanitized_chop_line[start] = sanitized
            chop_lines.append(sanitized_chop_line)
        return (
            ChopsUpdate(chop_lines, renderable.spans, renderable.chop_ends)
            if changed
            else renderable
        )
    return renderable


# The Wisp wordmark, shown while the transcript is empty. Drawn from U+2588 FULL
# BLOCK rather than box-drawing or ASCII art: a single, near-universally
# available glyph whose cell is fully painted, so the letterforms hold their
# shape in any monospace font without depending on how a terminal renders
# partial blocks or line-drawing joins.
#
# Letters were laid out as per-glyph grids and joined column-wise; typing the
# rows by hand produces subtly misaligned stems that stop reading as letters.
# The rendered result is committed rather than generated at import time — there
# is no runtime need to re-derive a constant.
#
# Every row is padded to the full width. `text-align: center` centers each line
# independently, so ragged rows drift relative to one another and the letterforms
# shear apart — the block has to be a true rectangle to hold its shape.
_WORDMARK = """\
█   █  ███  ████  ████
█   █   █   █     █  █
█ █ █   █   ████  ████
██ ██   █      █  █   
█   █  ███  ████  █   """
# Cells occupied by the widest wordmark row, used to size the centered block.
_WORDMARK_WIDTH = 22
# The fallback for viewports too short or too narrow for the drawn mark. Plain
# lowercase rather than letterspaced caps: this substitution is triggered partly
# BY narrowness, and letterspacing would pad the very axis that ran out. It also
# matches how the name is written everywhere else in the UI — the window title
# and the `wisp>` prompt — so the small form reads as the product name rather
# than as a shrunken logo.
_WORDMARK_COMPACT = "wisp"
_EMPTY_TRANSCRIPT_TAGLINE = "A coding agent that stays in sync"
_EMPTY_TRANSCRIPT_HINT = "Type a prompt or / for commands."
_MARKDOWN_VISIBLE_MARKERS = frozenset("`*_[]<>#|~-+\\&@")
_DECISION_INPUT_KEYS = frozenset(
    {"1", "2", "3", "4", "escape", "enter", "up", "down", "pageup", "pagedown", "home", "end"}
)
_SESSION_OPERATION_LABELS: dict[OverlayOperation, str] = {
    OverlayOperation.history_hydration: "Loading session history…",
    OverlayOperation.session_catalog: "Loading sessions…",
    OverlayOperation.session_switch: "Switching session…",
}
_TRANSCRIPT_COVERING_OPERATIONS = frozenset(
    {
        OverlayOperation.history_hydration,
        OverlayOperation.session_switch,
    }
)

# The input's prompt glyph. The shell hands the Textual renderer a semantic hint
# (`wisp> `, `wisp(running)> `, `approve? [y/N] `) shared with the line/fullscreen
# renderers; Textual turns those states into concise editor guidance behind one
# terminal-native glyph instead of repeating the verbose `wisp>` chrome.
_PROMPT_GLYPH = "❯"

# Semantic-hint → terse Textual placeholder. Command activity remains in the
# persistent transcript heartbeat while the editor explains what submission does.
_INPUT_PLACEHOLDERS: dict[str, str] = {
    "wisp> ": f"{_PROMPT_GLYPH} Ask Wisp anything…",
    "wisp(running)> ": f"{_PROMPT_GLYPH} Steer the active run…",
    "wisp(exiting)> ": f"{_PROMPT_GLYPH} exiting…",
    "approve? [y/N] ": f"{_PROMPT_GLYPH} approve? [y/N]",
}


def _input_placeholder(hint: str) -> str:
    """Map a shared semantic prompt hint to the Textual input's glyph placeholder.

    Falls back to prefixing the glyph for any hint not in the table (e.g. a future
    mode), so the input always leads with `❯` regardless of the source string.
    """

    return _INPUT_PLACEHOLDERS.get(hint, f"{_PROMPT_GLYPH} {hint}")


def _slash_enter_prefills(typed: str, spec: SlashCommandSpec) -> bool:
    return spec.prefill_on_partial_enter and typed.lower() != spec.command.lower()


@dataclass
class _HistoryPrependAnchor:
    transcript: Transcript
    widget: Widget | None
    scroll_y: float
    widget_y: float
    following: bool
    reader_generation: int
    epoch: int
    navigation_generation: int
    navigation: HistoryNavigation


def _merge_history_navigation(
    current: HistoryNavigation,
    incoming: HistoryNavigation,
) -> HistoryNavigation:
    intent = (
        incoming.intent if current.intent is HistoryNavigationIntent.PRESERVE else current.intent
    )
    return HistoryNavigation(
        intent,
        current.remaining_rows + incoming.remaining_rows,
        incoming.reader_generation,
    )


@dataclass
class _DisplayedFrame:
    """Exact terminal cells last handed to Textual's display driver."""

    size: Size
    rows: list[Strip]

    @classmethod
    def from_layout(
        cls,
        update: LayoutUpdate,
        *,
        size: Size,
    ) -> tuple[LayoutUpdate, _DisplayedFrame | None]:
        """Materialize one full layout without consuming its row iterables twice."""

        if update.region != size.region:
            return update, None
        materialized: list[Iterable[Strip]] = [tuple(line) for line in update.strips]
        rows = [Strip.join(line).discard_meta().simplify() for line in materialized]
        if len(rows) != size.height or any(row.cell_count != size.width for row in rows):
            return LayoutUpdate(materialized, update.region), None
        return LayoutUpdate(materialized, update.region), cls(size=size, rows=rows)

    def filter_layout(
        self,
        update: LayoutUpdate,
        *,
        size: Size,
        allow_suppression: bool,
    ) -> tuple[RenderableType | None, _DisplayedFrame | None, bool]:
        """Reduce a full settled layout to changed rows when its shape is exact."""

        prepared, next_frame = self.from_layout(update, size=size)
        if next_frame is None or self.size != size or not allow_suppression:
            return prepared, next_frame, not allow_suppression

        chops: list[dict[int, Strip]] = []
        chop_ends: list[list[int]] = []
        spans: list[tuple[int, int, int]] = []
        fail_open = False
        for y, (line, previous, current) in enumerate(
            zip(prepared.strips, self.rows, next_frame.rows, strict=True)
        ):
            output = Strip.join(line)
            safe_to_compare = not _strip_has_control(previous) and not _strip_has_control(output)
            if not safe_to_compare:
                fail_open = True
            if not safe_to_compare or current != previous:
                chops.append({0: output})
                chop_ends.append([size.width])
                trailing_blanks = (
                    0 if not safe_to_compare else _shared_trailing_blank_cells(previous, current)
                )
                narrowed_end = size.width - min(trailing_blanks, size.width - 1)
                spans.append((y, 0, narrowed_end))
            else:
                chops.append({})
                chop_ends.append([])
        if not spans:
            return None, next_frame, fail_open
        return ChopsUpdate(chops, spans, chop_ends), next_frame, fail_open

    def filter_chops(
        self,
        update: ChopsUpdate,
        *,
        allow_suppression: bool,
    ) -> tuple[ChopsUpdate | None, bool, bool]:
        """Drop exact duplicate spans and advance the cached terminal frame.

        The second result reports whether every update span could be reconstructed.
        The third reports whether exact comparison was bypassed for any span. An
        unfamiliar Textual update shape invalidates the cache and passes through
        unchanged rather than risking stale terminal cells.
        """

        candidate_rows = self.rows.copy()
        retained_spans: list[tuple[int, int, int]] = []
        fail_open = not allow_suppression
        try:
            for y, x1, x2 in update.spans:
                if not (0 <= y < self.size.height and 0 <= x1 < x2 <= self.size.width):
                    return update, False, True
                patch = _chops_span_strip(update, y=y, x1=x1, x2=x2)
                if patch is None:
                    return update, False, True
                patch = patch.discard_meta().simplify()
                previous = candidate_rows[y].crop(x1, x2)
                safe_to_compare = not _strip_has_control(previous) and not _strip_has_control(patch)
                if not safe_to_compare:
                    fail_open = True
                if not allow_suppression or not safe_to_compare or patch != previous:
                    trailing_blanks = (
                        _shared_trailing_blank_cells(previous, patch)
                        if allow_suppression and safe_to_compare
                        else 0
                    )
                    narrowed_end = x2 - min(trailing_blanks, x2 - x1 - 1)
                    retained_spans.append((y, x1, narrowed_end))
                candidate_rows[y] = (
                    Strip.join(
                        (
                            candidate_rows[y].crop(0, x1),
                            patch,
                            candidate_rows[y].crop(x2, self.size.width),
                        )
                    )
                    .discard_meta()
                    .simplify()
                )
        except (IndexError, TypeError, ValueError):
            return update, False, True

        self.rows = candidate_rows
        if not retained_spans:
            return None, True, fail_open
        if retained_spans == update.spans:
            return update, True, fail_open
        return ChopsUpdate(update.chops, retained_spans, update.chop_ends), True, fail_open


def _chops_span_strip(
    update: ChopsUpdate,
    *,
    y: int,
    x1: int,
    x2: int,
) -> Strip | None:
    """Reconstruct one complete dirty span from Textual's sparse row chops."""

    line = update.chops[y]
    ends = update.chop_ends[y]
    cursor = x1
    pieces: list[Strip] = []
    for end, (start, strip) in zip(ends, line.items(), strict=True):
        if end <= x1:
            continue
        if start >= x2:
            break
        overlap_start = max(start, x1)
        overlap_end = min(end, x2)
        if overlap_start > cursor or strip is None:
            return None
        pieces.append(strip.crop(overlap_start - start, overlap_end - start))
        cursor = overlap_end
    if cursor != x2:
        return None
    patch = Strip.join(pieces)
    return patch if patch.cell_count == x2 - x1 else None


def _strip_has_control(strip: Strip) -> bool:
    """Return whether replaying an equal strip may still have a side effect."""

    return any(segment.control for segment in strip)


def _shared_trailing_blank_cells(previous: Strip, current: Strip) -> int:
    """Count an identical, style-stable suffix made only of ASCII spaces.

    Textual commonly dirties a widget's complete row even when its visible text is
    short. Trimming only matching single-cell blanks avoids replaying that padded
    tail without introducing grapheme-boundary decisions into the display filter.
    """

    previous_segments = [segment for segment in previous if segment.text]
    current_segments = [segment for segment in current if segment.text]
    previous_index = len(previous_segments) - 1
    current_index = len(current_segments) - 1
    previous_end = len(previous_segments[previous_index].text) if previous_segments else 0
    current_end = len(current_segments[current_index].text) if current_segments else 0
    shared = 0

    while previous_index >= 0 and current_index >= 0:
        previous_segment = previous_segments[previous_index]
        current_segment = current_segments[current_index]
        if (
            previous_segment.control
            or current_segment.control
            or previous_segment.style != current_segment.style
        ):
            break

        previous_text = previous_segment.text[:previous_end]
        current_text = current_segment.text[:current_end]
        previous_blanks = len(previous_text) - len(previous_text.rstrip(" "))
        current_blanks = len(current_text) - len(current_text.rstrip(" "))
        matched = min(previous_blanks, current_blanks)
        if matched == 0:
            break

        shared += matched
        previous_end -= matched
        current_end -= matched
        if previous_end == 0:
            previous_index -= 1
            if previous_index >= 0:
                previous_end = len(previous_segments[previous_index].text)
        if current_end == 0:
            current_index -= 1
            if current_index >= 0:
                current_end = len(current_segments[current_index].text)

    return shared


class _StableScrollCompositor(Compositor):
    """Retain the prior visible map while Textual rebuilds scroll hit-testing state.

    Textual's scroll fast path stores the newly visible geometry separately and
    invalidates its complete map. Rebuilding that complete map for mouse hit testing
    then clears the visible map. On the next scroll, ``reflow_visible`` consequently
    compares against an empty map and marks the complete screen dirty, repainting the
    unchanged composer and status bar along with the transcript.

    Keep the previous visible geometry through that complete-map read. It remains the
    correct baseline for the next visible reflow, which can then emit only the changed
    transcript rows.
    """

    @property
    def full_map(self) -> CompositorMap:
        visible_map = self._visible_map
        full_map = super().full_map
        if visible_map is not None and self._visible_map is None:
            self._visible_map = visible_map
        return full_map


class _WispScreen(Screen[object]):
    """Default screen with stable compositor state between transcript scrolls."""

    def __init__(self) -> None:
        super().__init__(id="_default")
        self._compositor = _StableScrollCompositor()


@dataclass(frozen=True, slots=True)
class _PendingInputLatency:
    category: InputEventCategory
    event_time: float
    received_at: float
    handled_at: float


class TextualTui(App[None]):
    """Minimal Textual shell that adapts Wisp's existing TUI loop."""

    _MAX_PENDING_INPUT_DIAGNOSTICS = 1_024
    _MAX_OBSERVED_INPUT_EVENTS = 1_024

    _displayed_frame: _DisplayedFrame | None
    _displayed_cursor_position: Offset | None
    _displayed_screen: Screen[object] | None

    # Wisp owns a typed, RPC-backed palette. Keep Textual's framework ctrl+p
    # palette disabled so terminal history remains untouched.
    ENABLE_COMMAND_PALETTE = False

    def _on_terminal_supports_synchronized_output(
        self,
        message: messages.TerminalSupportsSynchronizedOutput,
    ) -> None:
        """Let Textual own synchronized frames unless the user opted out."""

        if not self._synchronized_output:
            message.prevent_default()

    def get_default_screen(self) -> Screen[object]:
        """Use the compositor that keeps routine transcript scrolling local."""

        return _WispScreen()

    def _display(self, screen: Screen[object], renderable: RenderableType | None) -> None:
        """Hide unstable history frames and omit exact duplicate terminal cells."""

        diagnostics_enabled = self._diagnostics is not None
        if getattr(self, "_history_prepend_paint_suppressed", False):
            if diagnostics_enabled:
                self._record_display_diagnostic(
                    renderable,
                    emitted_spans=0,
                    frame_cache="retained",
                    history_prepend_suppressed=True,
                )
            return
        renderable = _sanitize_terminal_update(renderable)
        if renderable is None or self.is_inline or self._batch_count:
            display_started, displayed_at = self._emit_display(screen, renderable)
            if renderable is not None and not self._batch_count:
                self._input_frame_emitted(displayed_at)
            if diagnostics_enabled:
                self._record_display_diagnostic(renderable)
                if renderable is not None and not self._batch_count:
                    self._record_input_latency_diagnostics(
                        renderable,
                        display_started,
                        displayed_at,
                    )
            return

        displayed_frame = self._displayed_frame if self._displayed_screen is screen else None
        next_frame: _DisplayedFrame | None = None
        prepared: RenderableType | None = renderable
        diagnostic_renderable: RenderableType | None = renderable
        emitted_spans: int | None = None
        suppressed_spans = 0
        frame_cache: DisplayFrameCacheOutcome = "unavailable"
        fail_open = False
        cursor_position = screen.outer_size.clamp_offset(self.cursor_position)
        if isinstance(renderable, LayoutUpdate):
            if isinstance(displayed_frame, _DisplayedFrame) and (
                displayed_frame.size == screen.outer_size
            ):
                prepared, next_frame, fail_open = displayed_frame.filter_layout(
                    renderable,
                    size=screen.outer_size,
                    allow_suppression=cursor_position == self._displayed_cursor_position,
                )
            else:
                prepared, next_frame = _DisplayedFrame.from_layout(
                    renderable,
                    size=screen.outer_size,
                )
            if next_frame is not None:
                frame_cache = "updated"
            diagnostic_renderable = prepared
        elif isinstance(renderable, ChopsUpdate):
            if isinstance(displayed_frame, _DisplayedFrame) and (
                displayed_frame.size == screen.outer_size
            ):
                filtered, cache_valid, fail_open = displayed_frame.filter_chops(
                    renderable,
                    allow_suppression=cursor_position == self._displayed_cursor_position,
                )
                prepared = cast(RenderableType | None, filtered)
                if diagnostics_enabled:
                    emitted_spans = len(filtered.spans) if filtered is not None else 0
                if cache_valid:
                    if emitted_spans is not None:
                        suppressed_spans = len(renderable.spans) - emitted_spans
                    next_frame = displayed_frame
                    frame_cache = "retained"
                else:
                    frame_cache = "fail-open"
            else:
                fail_open = True

        display_started, displayed_at = self._emit_display(screen, prepared)
        self._displayed_frame = next_frame
        self._displayed_cursor_position = cursor_position
        self._displayed_screen = screen
        if prepared is not None:
            self._input_frame_emitted(displayed_at)
        if diagnostics_enabled:
            self._record_display_diagnostic(
                diagnostic_renderable,
                emitted_spans=emitted_spans,
                suppressed_spans=suppressed_spans,
                frame_cache=frame_cache,
                fail_open=fail_open,
            )
            if prepared is not None:
                self._record_input_latency_diagnostics(prepared, display_started, displayed_at)

    def _record_input_latency_diagnostics(
        self,
        renderable: RenderableType,
        display_started: float,
        displayed_at: float,
    ) -> None:
        """Settle observed input against the first subsequently emitted frame."""

        pending = tuple(self._pending_input_latency)
        if not pending:
            return
        self._pending_input_latency.clear()
        kind: DisplayUpdateKind = (
            "layout"
            if isinstance(renderable, LayoutUpdate)
            else "chops"
            if isinstance(renderable, ChopsUpdate)
            else "other"
        )
        display_seconds = max(0.0, displayed_at - display_started)
        for interaction in pending:
            record_input_latency(
                self._diagnostics,
                InputLatencyDiagnostic(
                    category=interaction.category,
                    handler_seconds=max(0.0, interaction.handled_at - interaction.received_at),
                    queued_seconds=max(0.0, display_started - interaction.handled_at),
                    display_seconds=display_seconds,
                    total_seconds=max(0.0, displayed_at - interaction.received_at),
                    display_kind=kind,
                ),
            )

    def _input_frame_emitted(self, displayed_at: float) -> None:
        """Wake non-critical stream work after an input-visible frame."""

        if self._input_priority.frame_emitted(now=displayed_at):
            self._stream.resume_after_input_frame()

    def input_priority_drain_delay(
        self,
        deferred_since: float | None,
    ) -> tuple[float, float | None]:
        """Return the remaining bounded delay for one Markdown drain."""

        return self._input_priority.drain_delay(deferred_since)

    def _emit_display(
        self,
        screen: Screen[object],
        renderable: RenderableType | None,
    ) -> tuple[float, float]:
        """Emit one Textual frame and observe writes after the latency clock stops."""

        observer = self._terminal_writes
        model_headless_frame = self.is_headless and not self._batch_count
        if observer is not None:
            observer.begin_frame(getattr(self, "_driver", None))
        display_started = perf_counter()
        try:
            super()._display(screen, renderable)
        finally:
            displayed_at = perf_counter()
            if observer is not None:
                observer.finish_frame(
                    renderable,
                    headless=model_headless_frame,
                    sync_available=bool(getattr(self, "_sync_available", False)),
                    console=self.console,
                )
        return display_started, displayed_at

    def discard_deferred_terminal_write_diagnostics(self) -> None:
        """Discard benchmark-only headless models captured during setup."""

        observer = self._terminal_writes
        if observer is not None:
            observer.discard_deferred_frames()

    def flush_deferred_terminal_write_diagnostics(self) -> None:
        """Publish benchmark-only headless write models outside measured work."""

        observer = self._terminal_writes
        if observer is not None:
            observer.flush_deferred_frames()

    def _record_display_diagnostic(
        self,
        renderable: RenderableType | None,
        *,
        emitted_spans: int | None = None,
        suppressed_spans: int = 0,
        frame_cache: DisplayFrameCacheOutcome = "unavailable",
        fail_open: bool = False,
        history_prepend_suppressed: bool = False,
    ) -> None:
        """Report update shape without retaining terminal content or affecting display."""

        kind: DisplayUpdateKind = (
            "none"
            if renderable is None
            else (
                "layout"
                if isinstance(renderable, LayoutUpdate)
                else "chops"
                if isinstance(renderable, ChopsUpdate)
                else "other"
            )
        )
        input_spans = len(renderable.spans) if isinstance(renderable, ChopsUpdate) else 0
        record_display_update(
            self._diagnostics,
            DisplayUpdateDiagnostic(
                kind=kind,
                input_spans=input_spans,
                emitted_spans=input_spans if emitted_spans is None else emitted_spans,
                suppressed_spans=suppressed_spans,
                frame_cache=frame_cache,
                fail_open=fail_open,
                history_prepend_suppressed=history_prepend_suppressed,
                history_prepend_unsettled=(
                    history_prepend_suppressed
                    or getattr(self, "_prepending_history", False)
                    or getattr(self, "_history_prepend_anchor", None) is not None
                ),
            ),
        )

    CSS = """
    Screen {
        layout: vertical;
    }

    #transcript {
        height: 1fr;
        border: none;
        padding: 0 1 3 1;
        overflow-x: hidden;
        /* Keep native scrolling and overflow state without painting scrollbar
           chrome or reserving transcript width for an invisible gutter. */
        scrollbar-visibility: hidden;
        scrollbar-size-vertical: 0;
    }

    /* No `min-height`: a floor here would keep `size.height` pinned above the
       real viewport on a short terminal, so the panel's own resize breakpoints
       could never observe the smaller sizes they exist to handle — and the
       oversized panel would overflow the transcript and clip the wordmark
       mid-glyph instead of switching to the compact badge. */
    #transcript-empty {
        width: 1fr;
        height: 1fr;
        align: center middle;
    }

    /* No border and no fixed size: the drawn letterforms are the mark, and a
       frame around them would read as chrome competing with the lettering.
       `height: auto` lets the tall wordmark and the one-row compact fallback
       share this rule. */
    #transcript-empty-wordmark {
        max-width: 100%;
        height: auto;
        background: transparent;
        color: $accent;
        text-align: center;
    }

    /* `height: auto` on the text rows, not a fixed `1`: on a viewport narrower
       than the block these lines have to wrap. Pinned to one row they truncated
       mid-word instead ("A coding agent", "Type a prompt or /"), which reads as
       a rendering fault rather than a deliberately compact layout. Wrapping
       costs rows, so the panel's height thresholds are what keep the total
       bounded — see TranscriptEmptyState. */
    #transcript-empty-tagline {
        max-width: 100%;
        height: auto;
        margin-top: 1;
        color: $text;
        text-align: center;
    }

    #transcript-empty-hint {
        max-width: 100%;
        height: auto;
        margin-top: 1;
        color: $text-muted;
        text-align: center;
    }

    #transcript-empty-actions {
        max-width: 100%;
        height: auto;
        margin-top: 1;
        color: $text-muted;
        text-style: dim;
        text-align: center;
    }

    #jump-latest-row {
        overlay: screen;
        constrain: inside;
        display: none;
        width: 1fr;
        height: 1;
        offset: 0 -1;
        padding: 0 2 0 0;
        align: right middle;
    }

    /* Left rails and lifecycle glyphs keep conversation roles legible without
       color. Authored input keeps a subtle surface; assistant prose and tool rows
       stay open on the transcript background. Colors come only from theme vars
       present in both themes. */
    .message {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 0 0 1;
        border-left: outer $secondary;
        border-title-color: $text-muted;
    }

    .message--user {
        border-left: heavy $primary;
        background: $panel;
        color: $foreground;
        text-style: bold;
        padding-right: 1;
    }

    .message--assistant {
        border-left: outer $success;
        background: transparent;
    }

    .message--tool {
        border-left: outer $accent;
        color: $accent;
        padding-right: 1;
    }

    .message--notice {
        border-left: outer $warning;
        color: $warning;
    }

    .message--approved {
        border-left: outer $success;
        background: $success-muted;
        color: $text-success;
        padding-right: 1;
    }

    .message--denied {
        border-left: outer $warning;
        background: $warning-muted;
        color: $text-warning;
        padding-right: 1;
    }

    .message--error {
        border-left: outer $error;
        background: $error-muted;
        color: $text-error;
        padding-right: 1;
    }

    .message--dim,
    .message--session {
        border-left: none;
        padding-left: 2;
        color: $transcript-muted;
    }

    /* Tool activity is a flat action/result tree rather than another bordered
       message card. Semantic classes still identify lifecycle states; focus adds the
       only rail so keyboard navigation remains visible. */
    ToolCard {
        color: $text-muted;
        margin-bottom: 1;
    }

    ToolCard.message--tool,
    ToolCard.message--approved,
    ToolCard.message--denied,
    ToolCard.message--error {
        border-left: none;
        background: transparent;
        color: $text-muted;
        padding-left: 0;
        padding-right: 0;
    }

    ToolCard:focus {
        outline: none;
        outline-left: heavy $accent;
    }

    /* OpenCode-style composer: a filled writing surface with a single left rail,
       then a detached status strip. The panel owns overlay visibility while the
       editor remains the focus and cursor owner. */
    #composer {
        height: auto;
        border-left: heavy $accent;
        background: $surface;
        padding: 1 2 0 2;
    }

    #input {
        height: auto;
        border: none;
        background: transparent;
        color: $foreground;
        padding: 0;
    }

    #pending-input {
        height: auto;
        max-height: 8;
        overflow: hidden hidden;
        color: $foreground 70%;
        margin-bottom: 1;
    }

    #composer-region {
        height: auto;
    }

    #startup-notice {
        height: 1;
        min-height: 1;
        margin-bottom: 1;
        padding: 0 2 0 3;
        color: $warning;
        text-style: bold;
    }

    #input .text-area--placeholder {
        color: $foreground 60%;
    }

    #composer-meta {
        height: 1;
        margin-top: 1;
        color: $foreground 60%;
    }

    #composer.-compact #composer-meta {
        margin-top: 0;
    }

    #composer.-compact {
        padding-top: 0;
    }

    #status {
        width: 1fr;
        height: 1;
        border-left: heavy $accent;
        padding: 0 2;
        color: $foreground 60%;
        background: $background;
    }

    HelpPanel {
        width: 36%;
        min-width: 30;
        max-width: 60;
    }

    Screen.-compact-help HelpPanel {
        split: bottom;
        width: 100%;
        min-width: 0;
        max-width: 100%;
        height: 50%;
        min-height: 8;
        max-height: 18;
        border-left: none;
        border-top: vkey $foreground 30%;
    }
    """

    # Ctrl+C and Ctrl+D are priority bindings so they fire while PromptEditor has
    # focus. Escape is deliberately non-priority: the nearest focused widget or
    # overlay gets the first chance to dismiss itself.
    #
    # Scrollback: the transcript is not in the focused editor's ancestor chain, so
    # its own scroll bindings never fire — forward the keys from the app instead.
    # These are priority bindings because TextArea consumes all four keys. Ctrl+A /
    # Ctrl+E remain available for moving within the prompt.
    #
    # Mouse reporting is enabled in run_shell so wheel and trackpad events reach
    # the transcript. Ctrl+C uses Wisp's double-press quit flow; terminal-native
    # selection remains available through the emulator's mouse-bypass modifier.
    BINDINGS = [
        Binding(
            "ctrl+g",
            "toggle_contextual_help",
            "Guide",
            priority=True,
            id="wisp.contextual_help",
        ),
        Binding("up", "menu_move(-1)", "Previous suggestion", priority=True, show=False),
        Binding("down", "menu_move(1)", "Next suggestion", priority=True, show=False),
        Binding("left", "file_tree_move(False)", "Collapse directory", priority=True, show=False),
        Binding("right", "file_tree_move(True)", "Expand directory", priority=True, show=False),
        Binding("tab", "menu_complete", "Complete / switch picker", priority=True, show=False),
        Binding("ctrl+r", "open_prompt_history", "History", priority=True),
        Binding("shift+tab", "toggle_agent_mode", "Plan/build", priority=True, show=False),
        Binding("ctrl+t", "toggle_theme", "Light/dark", priority=True, show=False),
        Binding("ctrl+c", "interrupt", "Quit", priority=True),
        Binding("ctrl+d", "eof", "EOF", priority=True),
        Binding("escape", "cancel", "Cancel", priority=False, show=False),
        Binding("pageup", "scroll_transcript_page_up", "Scroll up", priority=True, show=False),
        Binding(
            "pagedown", "scroll_transcript_page_down", "Scroll down", priority=True, show=False
        ),
        Binding("home", "scroll_transcript_home", "Scroll to top", priority=True, show=False),
        Binding("end", "scroll_transcript_end", "Scroll to bottom", priority=True, show=False),
    ]

    def __init__(
        self,
        *,
        protected_paths: tuple[str, ...] | None = None,
        diagnostics: TuiDiagnosticsSink | None = None,
        defer_headless_terminal_write_models: bool = False,
        synchronized_output: bool = True,
    ) -> None:
        super().__init__()
        self._synchronized_output = synchronized_output
        self._diagnostics = diagnostics
        terminal_write_recorder = (
            getattr(diagnostics, "record_terminal_write", None) if diagnostics is not None else None
        )
        self._terminal_writes = (
            TerminalWriteObserver(
                diagnostics,
                defer_headless_models=defer_headless_terminal_write_models,
            )
            if diagnostics is not None and callable(terminal_write_recorder)
            else None
        )
        self._pending_input_latency: list[_PendingInputLatency] = []
        self._input_priority = InputPriorityPolicy()
        self._observed_input_events: dict[tuple[int, float], None] = {}
        self.presentation_clock = PresentationClock(self.set_interval)
        self._displayed_frame = None
        self._displayed_cursor_position = None
        self._displayed_screen = None
        # Textual's native wheel handler reads sensitivity from the app, not the
        # scroll view. Two rows keeps terminal wheel input responsive without
        # making short transcript navigation jumpy.
        self.scroll_sensitivity_y = 2.0
        # Register and select a Wisp theme before application CSS is parsed so
        # the transcript's custom muted variable is available on first mount.
        for theme in WISP_THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME_NAME
        # The parent's already-resolved protected-path policy, threaded down from
        # `run_tui`. It is *not* re-derived here: the resolved policy reflects the
        # `--auth-file` override and the trust decision the parent made before
        # startup, neither of which a fresh `WispConfig.from_env` in this process
        # can reconstruct. `None` means an embedded caller supplied nothing, and
        # `_file_index_context` falls back to resolving on its own.
        self._protected_paths = protected_paths
        # Credential files adopted after startup, when a trusted project's config is
        # applied mid-session. Held apart from the snapshot above so `None` keeps
        # meaning "nothing supplied"; see `set_picker_auth_path`.
        self._adopted_auth_paths: tuple[str, ...] = ()
        self._file_index_generation = 0
        self._file_index_cwd: str | None = None
        self._file_index_request: FileIndexRequest | None = None
        self._input_controller = TextualInputController(self)
        self._buffered_submissions: dict[int, TuiSubmission] = {}
        self._shell_snapshot = TuiViewSnapshot(status="idle", input_hint="wisp> ")
        self._exit_unsent: list[str] = []
        self._transcript_controller = TextualTranscriptController(self)
        self._status: StatusBar | None = None
        self._composer: ComposerRegion | None = None
        self._transcript: Transcript | None = None
        self._jump_to_latest: JumpToLatest | None = None
        self._input: PromptEditor | None = None
        self._suggest: SlashSuggest | None = None
        self._file_suggest: FileSuggest | None = None
        self._prompt_history_picker: PromptHistoryPicker | None = None
        self._theme_picker: ThemePicker | None = None
        self._decision_panel: DecisionPanel | None = None
        self._connect_panel: ConnectPanel | None = None
        self._model_picker: ModelPicker | None = None
        self._session_picker: SessionPicker | None = None
        self._context_status: ContextStatusOverlay | None = None
        self._diff_viewer: DiffViewer | None = None
        self._operation_indicator: OperationIndicator | None = None
        self._update_prompt: UpdatePrompt | None = None
        self._overlay_controller: TextualOverlayController | None = None
        self._help_viewport_state: TranscriptViewportState | None = None
        self._help_viewport_baseline: TranscriptViewportState | None = None
        self._command_catalog = DEFAULT_TUI_COMMAND_CATALOG
        self._last_dark_theme = DEFAULT_THEME_NAME
        self._theme_picker_original: str | None = None
        self._pending_theme_preview: str | None = None
        self._theme_preview_scheduled = False
        self._theme_preview_epoch = 0
        self._skill_catalog = RpcSkillCatalogSnapshot()
        self._agent_mode = "build"
        self._current_prompt = "wisp> "
        self._runner: Callable[[], Awaitable[TuiExitReason]] | None = None
        self._runner_error: Exception | None = None
        self._runner_result = TuiExitReason.exited
        self._history_page_request_hook: Callable[[], Awaitable[None]] | None = None
        self._history_latest_request_hook: Callable[[], Awaitable[None]] | None = None
        self._history_newer_request_hook: Callable[[str], Awaitable[None]] | None = None
        self._history_detail_request_hook: Callable[[str], Awaitable[None]] | None = None
        self._history_detail_cards: dict[str, list[ProcessCard]] = {}
        self._connect_api_key_hook: Callable[[str, str], Awaitable[None]] | None = None
        self._connect_oauth_hook: Callable[[str], Awaitable[None]] | None = None
        self._update_action_hook: (
            Callable[[UpdatePromptAction, UpdateAvailable], Awaitable[None]] | None
        ) = None
        self._pending_update: UpdateAvailable | None = None
        self._visible_input_mode = "idle"
        self._history_window_older_hook: Callable[[], bool] | None = None
        self._history_window_newer_hook: Callable[[], bool] | None = None
        self._history_window_oldest_hook: Callable[[], bool] | None = None
        self._history_window_latest_hook: Callable[[], bool] | None = None
        self._live_widget_evicted_hook: Callable[[Widget], None] | None = None
        self._live_history_reload_pending = False
        self._history_newer_request_pending = False
        self._live_history_reload_needed = False
        self._live_history_eviction_generation = 0
        self._live_history_reload_generation: int | None = None
        self._live_history_recovery_navigation: HistoryNavigation | None = None
        self._live_history_recovery_blocked = False
        self._history_marker: Widget | None = None
        self._prepending_history = False
        self._history_prepend_paint_suppressed = False
        self._history_prepend_mounts: list[AwaitMount | AwaitRemove] = []
        self._history_prepend_anchor: _HistoryPrependAnchor | None = None
        self._transcript_navigation_generation = 0
        self._pending_history_navigation = HistoryNavigation()
        self._oldest_navigation_generation: int | None = None
        self._history_render_depth = 0
        self._history_render_batch: AbstractContextManager[None] | None = None
        self._history_render_mounts: list[AwaitMount | AwaitRemove] = []
        self._last_history_render_mounts: tuple[AwaitMount | AwaitRemove, ...] = ()
        self._history_removing_widgets: set[Widget] = set()
        self._history_layout_generation = 0
        self._transcript_epoch = 0
        self._session_operation_generation = 0
        self._stream = MarkdownStreamController(self, diagnostics=diagnostics)

    def clear_prompt_editor(self) -> None:
        """Clear the editor when an input-controller transition requests it."""

        if self._input is not None:
            self._input.value = ""

    def write_input_error(self, message: str) -> None:
        """Render a recoverable input-controller queue error."""

        self.write_error(message)

    def buffer_submission(self, submission: TuiSubmission) -> None:
        """Show a frontend-accepted prompt until the shell classifies it."""

        self._buffered_submissions[int(submission.id)] = submission
        self._render_pending_submissions()

    def resolve_submission(self, submission_id: int) -> None:
        """Remove one provisional or shell-owned pending prompt from the composer."""

        self._buffered_submissions.pop(submission_id, None)
        self._shell_snapshot = replace(
            self._shell_snapshot,
            pending_submissions=tuple(
                submission
                for submission in self._shell_snapshot.pending_submissions
                if int(submission.id) != submission_id
            ),
        )
        self._render_pending_submissions()

    def restore_submissions(self, submissions: tuple[TuiSubmission, ...]) -> bool:
        """Restore unstarted prompts ahead of any newer composer draft."""

        if self._input is None:
            return False
        parts = [submission.content for submission in submissions if submission.content]
        current = self._input.text_for_submission()
        if current:
            parts.append(current)
        restored_ids = {int(submission.id) for submission in submissions}
        for submission_id in restored_ids:
            self._buffered_submissions.pop(submission_id, None)
        self._shell_snapshot = replace(
            self._shell_snapshot,
            pending_submissions=tuple(
                submission
                for submission in self._shell_snapshot.pending_submissions
                if int(submission.id) not in restored_ids
            ),
        )
        self._input_controller.clear_compact_echoes()
        self._input.restore_prompt("\n".join(parts))
        self._render_pending_submissions()
        self._input.focus()
        return True

    def report_unsent_submissions(self, submissions: tuple[TuiSubmission, ...]) -> None:
        """Retain unsent text for display after the terminal screen is restored."""

        for submission in submissions:
            line = _unsent_submission_text(submission)
            self._exit_unsent.append(line)
            self.write_notice(line)

    def _render_pending_submissions(self) -> None:
        shell_ids = {int(item.id) for item in self._shell_snapshot.pending_submissions}
        provisional = tuple(
            submission.pending_view()
            for submission_id, submission in self._buffered_submissions.items()
            if submission_id not in shell_ids
        )
        snapshot = replace(
            self._shell_snapshot,
            pending_submissions=self._shell_snapshot.pending_submissions + provisional,
        )
        if self._composer is not None:
            self._composer.set_snapshot(snapshot)

    def compose(self) -> ComposeResult:
        with Vertical():
            # This full-screen overlay must be the first normal-layout child so
            # `overlay: screen` starts at the screen origin rather than below
            # transcript/composer siblings when it becomes visible.
            yield OperationIndicator(id="operation-indicator")
            yield UpdatePrompt(id="update-prompt")
            yield ContextStatusOverlay(id="context-status")
            yield DiffViewer(id="diff-viewer")
            # Transcript takes all remaining height (1fr). ComposerRegion is explicitly
            # auto-height so the notice, input, and detached footer hug the screen bottom.
            yield Transcript(
                empty_wordmark=_WORDMARK,
                empty_compact_wordmark=_WORDMARK_COMPACT,
                empty_tagline=_EMPTY_TRANSCRIPT_TAGLINE,
                empty_hint=_EMPTY_TRANSCRIPT_HINT,
                id="transcript",
            )
            # A full-width transparent overlay row provides right alignment while
            # preserving this natural anchor at the transcript's lower edge.
            with Horizontal(id="jump-latest-row"):
                yield JumpToLatest(id="jump-latest")
            # The slash-command menu floats on the overlay layer anchored near the
            # input; yielded here so it shares the Vertical's coordinate space.
            yield SlashSuggest(id="suggest")
            # Same anchored slot as the slash menu; only one is ever open at a time
            # (the `/` and `@` triggers are mutually exclusive by construction).
            yield FileSuggest(id="file-suggest")
            yield PromptHistoryPicker(id="prompt-history")
            yield ThemePicker(id="theme-picker")
            yield DecisionPanel(id="decision-panel")
            yield ConnectPanel(id="connect-panel")
            yield ModelPicker(id="model-picker")
            yield SessionPicker(id="session-picker")
            yield ComposerRegion(
                placeholder=_input_placeholder("wisp> "),
                input_ready=self._shell_snapshot.input_ready,
                id="composer-region",
            )
            # Textual uses a distinct one-row information hierarchy. The shared
            # Rich/prompt-toolkit renderers retain their existing two-line footer.
            yield StatusBar(id="status")

    async def on_mount(self) -> None:
        # Retain an application title for terminal metadata without spending a
        # permanent screen row on Textual's Header. The disposable welcome state
        # below is Wisp's only visible identity treatment.
        self.title = "wisp"
        # A persisted choice only selects among Wisp's own themes: Textual also
        # registers ~20 built-ins, and silently adopting one of those from a
        # stale or hand-edited file would leave the transcript's role colors
        # (resolved from the active theme) unrecognizable.
        preference = load_theme_state(
            valid_themes=WISP_THEME_NAMES,
            valid_dark_themes=WISP_DARK_THEME_NAMES,
        )
        self.theme = preference.active_theme or DEFAULT_THEME_NAME
        self._last_dark_theme = preference.last_dark_theme or (
            self.theme if self.theme in WISP_DARK_THEME_NAMES else DEFAULT_THEME_NAME
        )
        self._transcript = self.query_one("#transcript", Transcript)
        self._jump_to_latest = self.query_one("#jump-latest", JumpToLatest)
        self._status = self.query_one("#status", StatusBar)
        self._composer = self.query_one("#composer-region", ComposerRegion)
        self._input = self.query_one("#input", PromptEditor)
        self._suggest = self.query_one("#suggest", SlashSuggest)
        self._file_suggest = self.query_one("#file-suggest", FileSuggest)
        self._prompt_history_picker = self.query_one("#prompt-history", PromptHistoryPicker)
        self._theme_picker = self.query_one("#theme-picker", ThemePicker)
        self._decision_panel = self.query_one("#decision-panel", DecisionPanel)
        self._connect_panel = self.query_one("#connect-panel", ConnectPanel)
        self._model_picker = self.query_one("#model-picker", ModelPicker)
        self._session_picker = self.query_one("#session-picker", SessionPicker)
        self._context_status = self.query_one("#context-status", ContextStatusOverlay)
        self._diff_viewer = self.query_one("#diff-viewer", DiffViewer)
        self._operation_indicator = self.query_one("#operation-indicator", OperationIndicator)
        self._update_prompt = self.query_one("#update-prompt", UpdatePrompt)
        self._overlay_controller = TextualOverlayController(
            composer=self._composer,
            # Both composer-anchored menus, so an overlay opening tears down each
            # of them; the `@` picker would otherwise float over the overlay and
            # win the Escape/navigation keys that belong to the active workflow.
            suggestions=(self._suggest, self._file_suggest),
            transcript=self._transcript,
            overlays={
                OverlayKind.decision: self._decision_panel,
                OverlayKind.connect: self._connect_panel,
                OverlayKind.model_picker: self._model_picker,
                OverlayKind.session_picker: self._session_picker,
                OverlayKind.prompt_history: self._prompt_history_picker,
                OverlayKind.theme_picker: self._theme_picker,
                OverlayKind.context_status: self._context_status,
                OverlayKind.diff_viewer: self._diff_viewer,
                OverlayKind.update_prompt: self._update_prompt,
                OverlayKind.operation_indicator: self._operation_indicator,
            },
            defer_after_refresh=self._defer_overlay_restore,
            on_overlay_displaced=self._on_overlay_displaced,
            on_transition_finished=self._schedule_update_prompt,
        )
        self.set_command_catalog(self._command_catalog)
        self.set_skill_catalog(self._skill_catalog)
        self._input.focus()  # keep the editor as the resting focus
        if self._terminal_writes is not None:
            self._terminal_writes.attach(getattr(self, "_driver", None))
        if self._runner is not None:
            self.run_worker(self._run_and_exit(), exclusive=True)

    def on_unmount(self) -> None:
        """Drop any diagnostics-only driver wrap before Textual tears the app down."""

        if self._terminal_writes is not None:
            self._terminal_writes.detach()

    def on_resize(self, event: events.Resize) -> None:
        """Update responsive help and composer presentation after terminal resize."""

        self.screen.set_class(event.size.width < 80, "-compact-help")
        if self._composer is not None:
            self._composer.refresh_layout(height=event.size.height)

    def watch_theme(self, theme_name: str) -> None:
        # Theme variables recolor mounted CSS-owned content atomically. Widgets
        # with cached theme-derived Rich content refresh their own presentation.
        if self.is_running:
            if self._status is not None:
                self._status.refresh_theme()
            if self._composer is not None:
                self._composer.refresh_theme()

    async def on_prompt_editor_submitted(self, event: PromptEditor.Submitted) -> None:
        if self._block_submission_while_starting():
            return
        # Enter on a highlighted menu item accepts THAT command (Claude-Code/Codex/
        # Pi model), not the raw buffer — so `/`↓↓ Enter runs the highlighted
        # command even though only `/` was typed.
        if self._accept_menu_highlight_on_enter(event.value):
            return
        # No live menu: run the line as-is through the typed path.
        if self._suggest is not None:
            self._suggest.hide()
        if event.display != event.value:
            # Keep the legacy compact-echo API coherent for embedded renderers;
            # owned submissions also carry this display text directly by identity.
            self._input_controller.register_compact_echo(event.value, event.display)
        self.submit_command_line(
            event.value,
            display=event.display,
            queue_kind=event.queue_kind,
        )

    def on_prompt_editor_restore_queued(self, event: PromptEditor.RestoreQueued) -> None:
        """Forward queue restoration through the shell-owned RPC boundary."""

        event.stop()
        self._signal_input(
            TuiQueueRestoreRequested(),
            action="restore queued item",
            clear_editor=False,
        )

    def _accept_menu_highlight_on_enter(self, typed: str) -> bool:
        """Accept the highlighted slash command on Enter; return whether it handled.

        Returns False (Enter falls through to submitting the raw line) unless the
        menu is open on a highlighted command. Enter executes the highlighted
        command, including when the input is only a prefix (`/mo` -> `/model`).
        Tab remains the completion path for adding arguments and appends a space
        for commands that accept them. Destructive commands may require a fully
        typed name before Enter dispatches them.
        """

        # An active file picker claims Enter first: files are inserted and tree
        # directories expand/collapse without submitting the prompt.
        if self._file_suggest is not None and self._file_suggest.is_active:
            if self._activate_file_picker():
                return True

        suggest = self._suggest
        if suggest is None or not suggest.is_open:
            return False
        spec = suggest.highlighted_spec()
        if spec is None:
            return False
        suggest.hide()
        self.refresh_bindings()
        if _slash_enter_prefills(typed, spec):
            self.prefill_command(f"{spec.command} ")
        else:
            self.submit_command_line(spec.command)
        return True

    def clear_compact_echoes(self) -> None:
        """Drop echoes for queued prompts the shell actually abandoned."""

        self._input_controller.clear_compact_echoes()

    def compact_echo_for(self, prompt: str) -> str:
        """Return the compact transcript echo for one submitted prompt."""

        return self._input_controller.compact_echo_for(prompt)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Keep the inline menus in sync with the input WITHOUT ever touching the
        # input value — the line is the single source of truth (Claude-Code model),
        # so a leading `/` is always typable as text (`/etc/hosts`).
        #
        # The two menus are mutually exclusive: a slash command owns the whole line
        # (anchored at position 0), while an `@` mention is cursor-relative and can
        # appear anywhere. Consulting the slash menu first preserves its existing
        # behavior exactly; the file menu is offered only when no slash menu is live.
        if event.text_area is not self._input:
            return
        self._schedule_update_prompt()
        menu_was_open = self._suggestion_menu_is_open()
        slash_matches = 0
        if self._suggest is not None:
            slash_matches = self._suggest.show_for(event.text_area.text)
        if self._file_suggest is None:
            if menu_was_open != self._suggestion_menu_is_open():
                self.refresh_bindings()
            return
        if slash_matches:
            self._file_suggest.end_mention()
            if menu_was_open != self._suggestion_menu_is_open():
                self.refresh_bindings()
            return
        self._sync_file_suggest(event.text_area)
        if menu_was_open != self._suggestion_menu_is_open():
            self.refresh_bindings()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        """Keep a cursor-relative mention synchronized on caret-only movement."""

        if event.text_area is not self._input or self._file_suggest is None:
            return
        menu_was_open = self._suggestion_menu_is_open()
        if self._suggest is not None and self._suggest.is_open:
            self._file_suggest.end_mention()
        else:
            self._sync_file_suggest(event.text_area)
        if menu_was_open != self._suggestion_menu_is_open():
            self.refresh_bindings()

    def _sync_file_suggest(self, editor: TextArea) -> None:
        picker = self._file_suggest
        if picker is None:
            return
        cursor = self._file_offset_of_cursor(editor)
        mention_was_active = picker.mention_active
        picker.show_for(editor.text, cursor)
        if picker.mention_active and not mention_was_active:
            # A mention session gets exactly one refresh. Reopening on the same root
            # can continue displaying the immutable old snapshot while this runs.
            self._refresh_file_suggestions()

    @staticmethod
    def _file_offset_of_cursor(editor: TextArea) -> int:
        """Flat character offset of the caret, for the `@` trigger scan.

        TextArea reports the cursor as (row, column); the mention scan needs a flat
        index into the same string it is handed. PromptEditor exposes exactly this
        conversion as its Input-compatibility shim, so reuse it rather than
        recomputing the line offsets here.
        """

        cursor_position = getattr(editor, "cursor_position", None)
        if isinstance(cursor_position, int):
            return cursor_position
        return len(editor.text)

    def set_picker_auth_path(self, auth_path: Path) -> None:
        """Protect a credential file the session adopted after startup.

        A trusted project's config can move ``auth_path`` mid-session (deferred
        trust), after ``__init__`` captured the startup policy. These paths are kept
        *beside* that snapshot rather than merged into it, so the `None` case still
        means "no policy supplied" and embedded callers keep their fallback
        resolution instead of collapsing to just this one pattern.

        Accumulating rather than replacing is deliberate: a previously active
        credential file must stay hidden, since a config change should never widen
        what the picker is willing to offer.
        """

        # Capture the caller's raw immutable input only. Canonicalization belongs to
        # the index worker because even ``resolve(strict=False)`` may perform I/O or
        # fail. Invalidate first so no old credential name remains activatable.
        pattern = str(auth_path)
        if pattern not in self._adopted_auth_paths:
            self._invalidate_file_snapshot()
            self._adopted_auth_paths = (*self._adopted_auth_paths, pattern)

    def _publish_file_snapshot(self, snapshot: ProjectSnapshot | None) -> None:
        """Atomically project one accepted snapshot across composer surfaces."""

        if self._file_suggest is not None:
            self._file_suggest.set_snapshot(snapshot)
        if self._input is not None:
            self._input.set_project_snapshot(snapshot)

    def _invalidate_file_snapshot(self) -> None:
        """Synchronously hide indexed data and reject every older completion."""

        self._file_index_generation += 1
        self._file_index_request = None
        self._publish_file_snapshot(None)

    def load_file_suggestions(self, cwd: str) -> FileIndexRequest | None:
        """Capture and start one immutable off-thread snapshot request for ``cwd``."""

        picker = self._file_suggest
        if picker is None:
            return None
        raw_cwd = str(cwd)
        if self._file_index_cwd is not None and raw_cwd != self._file_index_cwd:
            # Compare only raw request identity here. A transition must hide the old
            # corpus before the worker performs any fallible canonicalization.
            self._invalidate_file_snapshot()
        self._file_index_cwd = raw_cwd
        return self._begin_file_index_request(
            cwd=raw_cwd,
            protected_paths=self._protected_paths,
            adopted_auth_paths=self._adopted_auth_paths,
            picker=picker,
        )

    def _refresh_file_suggestions(self) -> FileIndexRequest | None:
        """Refresh the active root once at the start of an ``@`` mention session."""

        picker = self._file_suggest
        request = self._file_index_request
        if picker is None or request is None:
            return None
        return self._begin_file_index_request(
            cwd=request.cwd,
            protected_paths=request.protected_paths,
            adopted_auth_paths=request.adopted_auth_paths,
            picker=picker,
            max_entries=request.max_entries,
            max_depth=request.max_depth,
        )

    def _begin_file_index_request(
        self,
        *,
        cwd: str,
        protected_paths: tuple[str, ...] | None,
        adopted_auth_paths: tuple[str, ...],
        picker: FileSuggest,
        max_entries: int = 10_000,
        max_depth: int = 12,
    ) -> FileIndexRequest:
        self._file_index_generation += 1
        request = FileIndexRequest(
            generation=self._file_index_generation,
            cwd=cwd,
            protected_paths=protected_paths,
            adopted_auth_paths=adopted_auth_paths,
            max_entries=max_entries,
            max_depth=max_depth,
        )
        self._file_index_request = request
        self._start_file_index_request(request, picker)
        return request

    def _start_file_index_request(self, request: FileIndexRequest, picker: FileSuggest) -> None:
        """Narrow injectable seam between UI lifecycle and the Textual worker."""

        self._collect_file_suggestions(request, picker)

    @work(thread=True, exclusive=True, group="file-suggest")
    def _collect_file_suggestions(self, request: FileIndexRequest, picker: FileSuggest) -> None:
        snapshot = _build_file_index_snapshot(request)
        # Exactly one event-loop hop installs the complete immutable snapshot.
        self.call_from_thread(self._install_file_suggestions, request, picker, snapshot)

    def _install_file_suggestions(
        self,
        request: FileIndexRequest,
        picker: FileSuggest,
        snapshot: ProjectSnapshot | None,
    ) -> None:
        """Install only the newest request and re-evaluate a currently typed mention."""

        if request != self._file_index_request or request.generation != self._file_index_generation:
            return
        if picker is not self._file_suggest:
            return
        self._publish_file_snapshot(snapshot)
        editor = self._input
        if editor is None or snapshot is None or not snapshot.entries:
            return
        # An overlay or pending operation has already torn the composer down. The
        # worker is a background arrival, not user intent, so it must never revive
        # a composer-anchored menu on top of an active approval or picker.
        controller = self._overlay_controller
        if controller is not None and (
            controller.active_overlay is not None or controller.active_operation is not None
        ):
            return
        # Same precedence as on_text_area_changed: a live slash menu owns the line.
        if self._suggest is not None and self._suggest.is_open:
            return
        picker.show_for(editor.text, self._file_offset_of_cursor(editor))

    def on_transcript_follow_changed(self, event: Transcript.FollowChanged) -> None:
        if event.following:
            # Transcript.watch_scroll_y only reports True from genuine reader- or
            # app-driven movement to the tail (content-driven clamps are excluded
            # at the source — see Transcript._size_updated), so an in-flight Home
            # traversal never observes a spurious follow here. End explicitly
            # cancels the oldest-navigation generation before restoring tail
            # intent, so this is always real reader intent to resume following.
            self._cancel_oldest_navigation()
            show_latest = self._history_window_latest_hook
            if show_latest is not None:
                show_latest()
            self._transcript_controller.clear_unseen_output()
            self._stream.resume_if_deferred()
            self._request_live_history_reload()

    def on_transcript_need_more_history(self, event: Transcript.NeedMoreHistory) -> None:
        event.stop()
        transcript = self._transcript
        if (
            transcript is not None
            and event.navigation.reader_generation != transcript.follow_generation
        ):
            transcript.history_page_request_failed()
            pending = self._pending_history_navigation
            if pending.reader_generation == transcript.follow_generation:
                transcript.request_history_at_top(pending)
            return
        if (
            event.navigation.intent is not HistoryNavigationIntent.PRESERVE
            and event.navigation != self._pending_history_navigation
        ):
            if transcript is not None:
                transcript.history_page_request_failed()
            return
        self._pending_history_navigation = event.navigation
        shift_older = self._history_window_older_hook
        if shift_older is not None and shift_older():
            if transcript is not None:
                transcript.history_page_request_failed()
            return
        hook = self._history_page_request_hook
        if hook is None or transcript is None or not transcript.has_more_history:
            self._pending_history_navigation = HistoryNavigation()
            if transcript is not None:
                transcript.history_page_request_failed()
            if event.navigation.intent is HistoryNavigationIntent.OLDEST:
                self._oldest_navigation_generation = None
            return
        self.run_worker(
            hook(),
            group="history-page-request",
            exit_on_error=False,
        )

    def on_transcript_need_newer_history(self, event: Transcript.NeedNewerHistory) -> None:
        """Continue a wheel gesture through the mounted transcript's newer edge."""

        event.stop()
        transcript = self._transcript
        if (
            transcript is None
            or transcript.is_following
            or event.navigation.reader_generation != transcript.follow_generation
        ):
            return
        self._cancel_card_expand_repin()
        generation = (
            self._transcript_navigation_generation
            if self._newer_navigation_in_progress()
            else self._begin_transcript_navigation()
        )
        navigation = HistoryNavigation(
            event.navigation.intent,
            event.navigation.remaining_rows,
            transcript.follow_generation,
        )
        self._queue_newer_navigation(transcript, navigation, generation)

    def on_jump_to_latest_selected(self, event: JumpToLatest.Selected) -> None:
        event.stop()
        self._scroll_transcript_to_latest()
        if self._input is not None and self._input.display:
            self._input.focus()
        elif self._decision_panel is not None:
            self._decision_panel.focus_options()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # Capture follow intent before Textual's deferred center-scroll of a tall
        # card can drop it; the controller clears this intent after user scrolling.
        if isinstance(event.widget, ToolCard):
            self._transcript_controller.tool_card_focused(event.widget)
            transcript = self._transcript
            if transcript is not None and not self.is_newest_transcript_widget(event.widget):
                # An active stream may have Textual's same-pass anchor armed.
                # Release it before an older focused card can expand and reflow.
                transcript.anchor(False)

    def on_tool_card_toggled(self, event: ToolCard.Toggled) -> None:
        # A card grew or shrank. Re-pin the tail only when the *newest* card (the
        # transcript's last child) is expanded while the reader was following: that
        # keeps its output in view even though focusing a tall card scrolled the tail
        # off first. Expanding an older card, or one the reader scrolled up to reach,
        # leaves the viewport alone so the freshly revealed content isn't yanked away.
        event.stop()
        transcript = self._transcript
        if transcript is not None and not self.is_newest_transcript_widget(event.card):
            # A provider delta may have re-armed the stream anchor after this
            # older card received focus. Disarm it again before the toggle's
            # invalidated layout is composed.
            transcript.anchor(False)
        self._transcript_controller.tool_card_toggled(event.card)

    def on_tool_card_leave_requested(self, event: ToolCard.LeaveRequested) -> None:
        # Escape on a focused card hands focus back to the resting target: the input,
        # or the decision panel's choice list when a panel has hidden the input (same
        # fallback as on_jump_to_latest_selected), so the reader is never stranded on
        # a card with no way back.
        event.stop()
        if self._input is not None and self._input.display:
            self._input.focus()
        elif self._decision_panel is not None:
            self._decision_panel.focus_options()

    def on_tool_card_view_diff_requested(self, event: ToolCard.ViewDiffRequested) -> None:
        """Open a card's retained structured diff without disturbing scrollback."""

        event.stop()
        self.show_diff_viewer(event.presentation)

    def on_diff_viewer_closed(self, event: DiffViewer.Closed) -> None:
        event.stop()
        self.hide_diff_viewer()

    def _cancel_card_expand_repin(self) -> None:
        # A user scroll after focusing a card is a deliberate move away from the tail.
        self._transcript_controller.user_scrolled()

    def _begin_transcript_navigation(self, *, preserve_live_history_recovery: bool = False) -> int:
        """Invalidate stale viewport work and cancel incompatible reader actions."""

        self._transcript_navigation_generation += 1
        self._cancel_oldest_navigation()
        if not preserve_live_history_recovery:
            self._live_history_recovery_navigation = None
            self._live_history_recovery_blocked = False
        return self._transcript_navigation_generation

    def _cancel_oldest_navigation(self) -> None:
        self._oldest_navigation_generation = None
        self._pending_history_navigation = HistoryNavigation()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._wheel_event_targets_transcript(event):
            self._cancel_card_expand_repin()
            assert self._transcript is not None
            transcript = self._transcript
            anchor = self._history_prepend_anchor
            continuing_prepend = (
                anchor is not None
                and anchor.transcript is transcript
                and anchor.navigation.intent is not HistoryNavigationIntent.OLDEST
            )
            continuing_load = transcript.history_page_loading
            if not continuing_prepend and not continuing_load:
                self._begin_transcript_navigation(preserve_live_history_recovery=True)
            navigation = transcript.prepare_wheel_up(request_history=not continuing_prepend)
            if navigation is not None:
                if continuing_prepend:
                    assert anchor is not None
                    anchor.navigation = _merge_history_navigation(anchor.navigation, navigation)
                elif continuing_load:
                    self._pending_history_navigation = _merge_history_navigation(
                        self._pending_history_navigation,
                        navigation,
                    )
                else:
                    self._pending_history_navigation = navigation
                self._recover_evicted_history_for_backward_navigation(navigation)
        self._forward_jump_overlay_scroll(event, direction=-1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._wheel_event_targets_transcript(event):
            self._cancel_card_expand_repin()
            transcript = self._transcript
            jump = self._jump_to_latest
            target = event.widget
            # A transcript whose content fits the viewport has no scroll range,
            # so Textual refuses the wheel entirely (``allow_vertical_scroll`` is
            # False) and ``Transcript._on_mouse_scroll_down`` never runs. Forward
            # window navigation is purely virtual there, so drive it from here.
            # When the transcript *can* scroll, the widget handler owns the step
            # and this must stay out of the way or the movement doubles.
            if (
                transcript is not None
                and not transcript.allow_vertical_scroll
                and not (event.ctrl or event.shift)
                and (jump is None or target not in {jump, jump.parent})
            ):
                transcript.wheel_down()
                event.stop()
        self._forward_jump_overlay_scroll(event, direction=1)

    def _wheel_event_targets_transcript(
        self,
        event: events.MouseScrollUp | events.MouseScrollDown,
    ) -> bool:
        """Return whether wheel input originated in the transcript or its overlay row."""

        transcript = self._transcript
        target = event.widget
        if transcript is None or target is None:
            return False
        if target is transcript or transcript in target.ancestors:
            return True
        jump = self._jump_to_latest
        return jump is not None and target in {jump, jump.parent}

    def _forward_jump_overlay_scroll(
        self,
        event: events.MouseScrollUp | events.MouseScrollDown,
        *,
        direction: int,
    ) -> None:
        """Route wheel input through the transparent badge-alignment row."""

        jump = self._jump_to_latest
        transcript = self._transcript
        target = event.widget
        if jump is None or transcript is None or target not in {jump, jump.parent}:
            return
        if direction > 0:
            transcript.wheel_down()
        else:
            transcript.scroll_to(
                y=transcript.scroll_target_y + direction * self.scroll_sensitivity_y,
                animate=False,
            )
        event.stop()

    def submit_command_line(
        self,
        text: str,
        *,
        display: str | None = None,
        queue_kind: QueueSubmissionKind = "auto",
    ) -> None:
        """Submit a typed line through the input controller."""

        if self._block_submission_while_starting():
            return
        if self._submit_local_theme_command(text):
            self.clear_prompt_editor()
            return
        self._input_controller.submit_line(
            text,
            clear_editor=True,
            display=display,
            queue_kind=queue_kind,
        )

    def _block_submission_while_starting(self) -> bool:
        """Keep an early draft editable until the shell can classify submissions."""

        if self._shell_snapshot.input_ready:
            return False
        if self._composer is not None:
            self._composer.show_startup_submission_blocked()
        return True

    def _submit_local_theme_command(self, text: str) -> bool:
        """Handle Textual-only ``/theme`` without crossing the RPC boundary."""

        if "\n" in text or "\r" in text:
            return False
        parts = text.strip().split()
        if not parts or parts[0].casefold() != "/theme":
            return False
        if len(parts) == 1:
            self.show_theme_picker()
            return True
        if len(parts) != 2:
            choices = "|".join(WISP_THEME_BY_SLUG)
            self.write_error(f"Usage: /theme [{choices}]")
            return True
        spec = WISP_THEME_BY_SLUG.get(parts[1].casefold())
        if spec is None:
            self.write_error(f"Unknown theme: {parts[1]}")
            return True
        self._commit_theme(spec.name, announce=True)
        return True

    def _submit_decision_line(self, text: str) -> bool:
        # The decision overlay temporarily hides the composer. Keep its draft
        # untouched so approval never discards a follow-up the user was typing.
        if self._block_submission_while_starting():
            return False
        return self._input_controller.submit_line(text, clear_editor=False)

    def on_decision_panel_selected(self, event: DecisionPanel.Selected) -> None:
        event.stop()
        self._submit_decision_line(event.answer)

    def on_connect_panel_method_selected(self, event: ConnectPanel.MethodSelected) -> None:
        event.stop()
        hook = self._connect_oauth_hook
        if hook is None:
            self.hide_connect_panel()
            return
        self.run_worker(
            hook(event.method.provider),
            group="connect-oauth",
            exclusive=True,
            exit_on_error=False,
        )

    def on_connect_panel_api_key_submitted(self, event: ConnectPanel.ApiKeySubmitted) -> None:
        event.stop()
        panel = self._connect_panel
        hook = self._connect_api_key_hook
        api_key = panel.take_api_key() if panel is not None else None
        if hook is None or api_key is None:
            self.hide_connect_panel()
            return
        self.run_worker(
            hook(event.provider, api_key),
            group="connect-api-key",
            exit_on_error=False,
        )

    def on_connect_panel_disconnect_selected(self, event: ConnectPanel.DisconnectSelected) -> None:
        event.stop()
        if not self._submit_decision_line(f"/disconnect {event.provider}"):
            self.hide_connect_panel()

    def on_connect_panel_cancelled(self, event: ConnectPanel.Cancelled) -> None:
        event.stop()
        self.workers.cancel_group(self, "connect-oauth")
        self.hide_connect_panel()

    def on_model_picker_selected(self, event: ModelPicker.Selected) -> None:
        event.stop()
        self.hide_model_picker()
        self._submit_decision_line(event.answer)

    def on_model_picker_cancelled(self, event: ModelPicker.Cancelled) -> None:
        event.stop()
        self.hide_model_picker()

    def on_session_picker_selected(self, event: SessionPicker.Selected) -> None:
        event.stop()
        # Enter the lifecycle before queueing the typed command: the shell awaits
        # the RPC transport before it repeats session_switch_started(), so this
        # closes the interval where Ctrl-C could otherwise clear the hidden draft.
        self.session_switch_started()
        if not self._submit_decision_line(f"/resume {event.session_id}"):
            self.session_switch_finished()

    def on_session_picker_cancelled(self, event: SessionPicker.Cancelled) -> None:
        event.stop()
        self.hide_session_picker()

    def on_prompt_history_picker_selected(self, event: PromptHistoryPicker.Selected) -> None:
        event.stop()
        if not self.hide_prompt_history():
            return
        if self._input is not None:
            self._input.restore_prompt(event.prompt)
            self._input.focus()

    def on_prompt_history_picker_cancelled(self, event: PromptHistoryPicker.Cancelled) -> None:
        event.stop()
        self.hide_prompt_history()

    def on_theme_picker_previewed(self, event: ThemePicker.Previewed) -> None:
        event.stop()
        if self._theme_picker_original is None:
            return
        self._pending_theme_preview = event.theme_name
        if self._theme_preview_scheduled:
            return
        self._theme_preview_scheduled = True
        epoch = self._theme_preview_epoch
        self.call_after_refresh(self._flush_theme_preview, epoch)

    def on_theme_picker_selected(self, event: ThemePicker.Selected) -> None:
        event.stop()
        overlays = self._overlay_controller
        if (
            overlays is None
            or overlays.active_overlay is not OverlayKind.theme_picker
            or self._theme_picker_original is None
        ):
            return
        self._invalidate_theme_preview()
        self._commit_theme(event.theme_name, announce=True)
        self._theme_picker_original = None
        self.hide_theme_picker()

    def on_theme_picker_cancelled(self, event: ThemePicker.Cancelled) -> None:
        event.stop()
        self._rollback_theme_picker_preview()
        self.hide_theme_picker()

    def _rollback_theme_picker_preview(self) -> None:
        original = self._theme_picker_original
        self._invalidate_theme_preview()
        self._theme_picker_original = None
        if original in WISP_THEME_NAMES:
            self.theme = original

    def _on_overlay_displaced(self, kind: OverlayKind) -> None:
        if kind is OverlayKind.theme_picker:
            self._rollback_theme_picker_preview()

    def on_update_prompt_selected(self, event: UpdatePrompt.Selected) -> None:
        event.stop()
        overlays = self._overlay_controller
        if overlays is None or overlays.active_overlay is not OverlayKind.update_prompt:
            return
        self._pending_update = None
        if event.action is UpdatePromptAction.update_and_restart:
            overlays.close(OverlayKind.update_prompt, restore_composer=False)
        else:
            overlays.close(OverlayKind.update_prompt)
        if event.action is UpdatePromptAction.later:
            return
        hook = self._update_action_hook
        if hook is None:
            self.write_error("The Wisp update action is unavailable.")
            if event.action is UpdatePromptAction.update_and_restart:
                self.update_operation_finished(installed=False, restarting=False)
            return
        self.run_worker(
            self._invoke_update_action(hook, event.action, event.update),
            exclusive=True,
            group="wisp-update-action",
        )

    async def _invoke_update_action(
        self,
        hook: Callable[[UpdatePromptAction, UpdateAvailable], Awaitable[None]],
        action: UpdatePromptAction,
        update: UpdateAvailable,
    ) -> None:
        try:
            await hook(action, update)
        except Exception as exc:  # noqa: BLE001 - restore a usable prompt on callback failure
            self.write_error(f"Update action failed: {exc}")
            if action is UpdatePromptAction.update_and_restart:
                self.update_operation_finished(installed=False, restarting=False)

    def _flush_theme_preview(self, epoch: int) -> None:
        if epoch != self._theme_preview_epoch:
            return
        self._theme_preview_scheduled = False
        theme_name, self._pending_theme_preview = self._pending_theme_preview, None
        if theme_name in WISP_THEME_NAMES and self._theme_picker_original is not None:
            self.theme = theme_name

    def _invalidate_theme_preview(self) -> None:
        self._theme_preview_epoch += 1
        self._pending_theme_preview = None
        self._theme_preview_scheduled = False

    def on_context_status_overlay_cancelled(self, event: ContextStatusOverlay.Cancelled) -> None:
        event.stop()
        self.hide_context_status()

    def set_command_catalog(self, catalog: TuiCommandCatalog) -> None:
        """Apply the executable catalog to inline slash suggestions."""

        self._command_catalog = catalog
        presentation_catalog = catalog.with_descriptors(*TEXTUAL_LOCAL_COMMAND_DESCRIPTORS)
        if self._input is not None:
            self._input.set_command_catalog(presentation_catalog)
        if self._suggest is not None:
            self._suggest.set_catalog(presentation_catalog)
            # Catalog discovery can complete just after the user starts typing at
            # startup. Reproject the current editor value so an already-open `/`
            # menu is refreshed instead of being dismissed by the catalog swap.
            if self._input is not None and self._input.display:
                self._suggest.show_for(self._input.text)

    def set_skill_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
        """Apply the immutable skill snapshot to inline completion."""

        self._skill_catalog = catalog
        if self._suggest is not None:
            self._suggest.set_skill_catalog(catalog)
            if self._input is not None and self._input.display:
                self._suggest.show_for(self._input.text)

    def show_skill_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
        """Mount a literal-text catalog inspection block."""

        self.write_message(skill_catalog_text(catalog), role="system")

    def show_skill_invocation(
        self,
        event: SkillInvoked,
        *,
        widget: LineMessage | None = None,
    ) -> None:
        """Replace a raw live invocation echo with its typed compact label."""

        text = skill_invocation_text(event)
        if widget is not None:
            widget.update(text)
            self.note_transcript_update(widget)
            return
        self.write_message(text, role="user")

    def prefill_command(self, prefix: str) -> None:
        """Put a command prefix in the editor, cursor at the end, without submitting.

        Tab-completion fills the highlighted command here (`/model `, `/help`); the
        user then adds any argument and presses Enter through the normal typed path.
        """
        if self._input is not None:
            self._input.value = prefix
            self._input.cursor_position = len(prefix)
            self._input.focus()

    def _input_event_category(self, event: events.Event) -> InputEventCategory | None:
        """Classify interactive input without retaining its value or coordinates."""

        if isinstance(event, (events.MouseScrollUp, events.MouseScrollDown)):
            return (
                "wheel"
                if not (event.ctrl or event.shift)
                and self._wheel_event_targets_transcript(event)
                and self._transcript_wheel_changes_view(event)
                else None
            )
        if isinstance(event, (events.MouseScrollLeft, events.MouseScrollRight)):
            return None
        if isinstance(event, events.Paste):
            editor = self._input
            focused = self.screen.focused
            return "paste" if editor is not None and editor.display and focused is editor else None
        if not isinstance(event, events.Key):
            return None
        key = event.key
        focused = self.screen.focused
        if isinstance(focused, ToolCard) and key not in {
            "ctrl+c",
            "ctrl+d",
            "home",
            "end",
            "pageup",
            "pagedown",
        }:
            return None
        if key in {"home", "end", "pageup", "pagedown"} and self._help_key_panel() is not None:
            return None
        decision_panel = self._decision_panel
        if decision_panel is not None and decision_panel.display:
            if key == "ctrl+c":
                return "cancellation"
            if key == "ctrl+d":
                return "typing" if self._prompt_deletion_mutates(backward=False) else None
            if key in _DECISION_INPUT_KEYS:
                if key in {"1", "2", "3", "4"} and not decision_panel.check_action(
                    "choose", (int(key),)
                ):
                    return None
                if key in {"up", "down", "home", "end", "pageup", "pagedown"} and not (
                    decision_panel.navigation_key_changes_highlight(key)
                ):
                    return None
                return "approval"
            return None
        if key == "ctrl+c" and self._editor_owns_selection():
            return None
        if key == "ctrl+c":
            return "cancellation"
        if key == "ctrl+d":
            return "typing" if self._prompt_deletion_mutates(backward=False) else None
        overlays = self._overlay_controller
        if overlays is not None:
            if overlays.active_overlay is not None:
                return None
            if overlays.active_operation is not None:
                return (
                    "cancellation"
                    if key == "escape" and overlays.active_operation is OverlayOperation.update
                    else None
                )
        if key == "escape":
            return None if self._suggestion_menu_is_open() else "cancellation"
        if key == "enter" and (
            self._file_picker_is_active() or self._slash_menu_prefills_on_enter()
        ):
            return None
        if key in {"up", "down"} and self._suggestion_menu_is_open():
            return None
        if key in {"left", "right"} and self._file_tree_is_active():
            return None
        if key == "enter" or (key == "alt+enter" and self._visible_input_mode == "running"):
            return "submission"
        if key == "alt+up":
            return (
                "typing"
                if self._visible_input_mode == "running" and isinstance(focused, PromptEditor)
                else None
            )
        if key == "alt+enter" or (
            key in {"shift+enter", "ctrl+j"} and isinstance(focused, PromptEditor)
        ):
            return "typing"
        if key in {
            "left",
            "right",
            "up",
            "down",
            "ctrl+left",
            "ctrl+right",
            "ctrl+home",
            "ctrl+end",
            "ctrl+a",
            "ctrl+e",
            "ctrl+shift+left",
            "ctrl+shift+right",
            "shift+home",
            "shift+end",
            "shift+up",
            "shift+down",
            "shift+left",
            "shift+right",
            "f6",
            "f7",
        }:
            return "cursor" if self._cursor_key_moves_prompt(key) else None
        if key in {"home", "end", "pageup", "pagedown"}:
            return "navigation" if self._transcript_navigation_changes_view(key) else None
        if key in {"backspace", "delete"} and isinstance(focused, PromptEditor):
            return "typing" if self._prompt_deletion_mutates(backward=key == "backspace") else None
        if key in {"ctrl+g", "ctrl+r", "ctrl+t", "shift+tab", "tab"}:
            return None
        if event.character is not None:
            return "typing"
        return None

    async def on_event(self, event: events.Event) -> None:
        # App.on_event is the earliest point a Key/Mouse/Paste event passes
        # through before Textual forwards it to whatever currently has focus
        # or is at its screen coordinates — earlier than any widget's own
        # on_key/BINDINGS, and earlier than this app's own on_key below. An
        # event timestamped before the overlay controller's barrier was read by the driver
        # before a decision panel opened (or is opening — the barrier is
        # raised before the composer is hidden or
        # focus moves), so it must never reach a focused/hit-tested widget:
        # dropping it here, rather than in DecisionPanel.on_key or
        # PromptEditor.on_key individually, closes the race for every widget
        # that could still be focused (or at those coordinates) when it
        # arrives, not just the one the caller expected by then.
        #
        # events.MouseEvent (not just Key) is gated as a whole base class,
        # not enumerated per subclass: every mouse variant Textual defines
        # (MouseDown, MouseUp, MouseMove, MouseScrollUp/Down/Left/Right) can
        # reach a widget the same way a stale key can, and Click is
        # synthesized from an already-forwarded MouseUp inside App.on_event's
        # own body (see textual.app.App.on_event) rather than delivered as an
        # independent top-level event, so gating the MouseEvent family here
        # transitively blocks a stale Click too. A stale MouseScrollDown in
        # particular could otherwise scroll the decision panel's highlighted
        # option out of view before a legitimate Enter lands, without ever
        # changing which option is logically selected — the same class of bug
        # this barrier exists to prevent, just via scroll instead of a key.
        # Paste is gated separately (it is not a MouseEvent or Key) because a
        # stale paste could otherwise still reach a focused-but-hidden
        # PromptEditor or an OptionList.
        input_event_types = (events.Key, events.MouseEvent, events.Paste)
        overlays = self._overlay_controller
        if (
            overlays is not None
            and isinstance(event, input_event_types)
            and overlays.event_is_stale(event.time)
        ):
            return
        if isinstance(event, input_event_types):
            observation_token = (id(event), event.time)
            if observation_token in self._observed_input_events:
                await super().on_event(event)
                return
            if len(self._observed_input_events) >= self._MAX_OBSERVED_INPUT_EVENTS:
                del self._observed_input_events[next(iter(self._observed_input_events))]
            self._observed_input_events[observation_token] = None
        category = self._input_event_category(event)
        if category is None:
            await super().on_event(event)
            return
        event_token: InputPriorityToken = (category, event.time)
        received_at = perf_counter()
        if not self._input_priority.observe_input(event_token, now=received_at):
            await super().on_event(event)
            return
        try:
            await super().on_event(event)
        except BaseException:
            self._input_priority.cancel_input(event_token)
            raise
        if self._diagnostics is None:
            return
        if len(self._pending_input_latency) >= self._MAX_PENDING_INPUT_DIAGNOSTICS:
            self._pending_input_latency.pop(0)
        self._pending_input_latency.append(
            _PendingInputLatency(
                category=category,
                event_time=event.time,
                received_at=received_at,
                handled_at=perf_counter(),
            )
        )

    def _suggestion_menu_is_open(self) -> bool:
        return bool(
            (self._file_suggest is not None and self._file_suggest.is_open)
            or (self._suggest is not None and self._suggest.is_open)
        )

    def _file_picker_is_active(self) -> bool:
        return self._file_suggest is not None and self._file_suggest.is_active

    def _slash_menu_prefills_on_enter(self) -> bool:
        suggest = self._suggest
        editor = self._input
        if suggest is None or editor is None or not suggest.is_open:
            return False
        spec = suggest.highlighted_spec()
        return spec is not None and _slash_enter_prefills(editor.text, spec)

    def _file_tree_is_active(self) -> bool:
        return bool(
            self._file_suggest is not None
            and self._file_suggest.is_active
            and self._file_suggest.is_tree_mode
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"menu_move", "menu_complete"}:
            return self._file_picker_is_active() or bool(
                self._suggest is not None and self._suggest.is_open
            )
        if action == "file_tree_move":
            return self._file_tree_is_active()
        return super().check_action(action, parameters)

    def action_menu_move(self, direction: int) -> None:
        picker = self._file_suggest
        if picker is not None and picker.is_active:
            picker.move_selection(direction)
            return
        suggest = self._suggest
        if suggest is None or not suggest.is_open:
            return
        if direction < 0:
            suggest.action_cursor_up()
        else:
            suggest.action_cursor_down()

    def action_file_tree_move(self, expand: bool) -> None:
        picker = self._file_suggest
        if picker is not None:
            picker.move_tree_horizontal(expand=expand)

    def action_menu_complete(self) -> None:
        picker = self._file_suggest
        if picker is not None and picker.is_active:
            # Tab changes project-file presentation; Enter performs activation.
            picker.toggle_mode()
        else:
            self._complete_from_menu()
        self.refresh_bindings()

    def _complete_from_menu(self) -> None:
        # Fill the highlighted command into the input. Arg-taking commands get a
        # trailing space so the user types the value next; arg-less ones don't, so
        # a following Enter runs them immediately. Hiding the menu happens via the
        # resulting on_input_changed (the value gains a space or fully matches).
        suggest = self._suggest
        if suggest is None:
            return
        spec = suggest.highlighted_spec()
        if spec is None:
            return
        self.prefill_command(f"{spec.command} " if spec.takes_args else spec.command)
        suggest.hide()
        self.refresh_bindings()

    def on_file_suggest_activation_requested(self, event: FileSuggest.ActivationRequested) -> None:
        """Route mouse rows through the same activation seam as Enter."""

        self._activate_file_picker(event.path)

    def _activate_file_picker(self, requested_path: str | None = None) -> bool:
        """Activate a picker row and splice an insertable path into the draft."""

        picker = self._file_suggest
        editor = self._input
        if picker is None or editor is None:
            return False
        value = editor.text
        cursor = self._file_offset_of_cursor(editor)
        query = picker.query_from_value(value, cursor)
        if query is None or query != picker.current_query or not picker.is_active:
            # A caret move can race a keyboard/mouse activation message. Reconcile
            # from the authoritative editor first and perform no picker side effect.
            picker.show_for(value, cursor)
            self.refresh_bindings()
            return False

        activation = picker.activate(requested_path)
        if not activation.handled:
            return False
        path = activation.insertion_path
        if path is None:
            # Tree directories only expand/collapse and still consume Enter/click.
            return True

        # Keyboard and mouse both use this pure formatter and this one splice.
        start = cursor - len(query) - 1
        replacement = f"{format_file_reference(path)} "
        editor.value = f"{value[:start]}{replacement}{value[cursor:]}"
        editor.cursor_position = start + len(replacement)
        picker.end_mention()
        editor.focus()
        self.refresh_bindings()
        return True

    def set_history_page_request_hook(
        self,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        """Register the shell callback used when the reader reaches transcript history."""

        self._history_page_request_hook = hook

    def set_history_latest_request_hook(
        self,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        """Register the shell callback used to reload an evicted transcript tail."""

        self._history_latest_request_hook = hook

    def set_history_newer_page_request_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        """Register the shell callback for an adjacent newer durable page."""

        self._history_newer_request_hook = hook

    def set_history_detail_request_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        """Register the shell callback for exact persisted process output."""

        self._history_detail_request_hook = hook

    def on_process_card_history_detail_requested(
        self,
        event: ProcessCard.HistoryDetailRequested,
    ) -> None:
        """Coalesce cards waiting on the same exact persisted row."""

        event.stop()
        cards = self._history_detail_cards.setdefault(event.entry_id, [])
        if event.card not in cards:
            cards.append(event.card)
        if len(cards) > 1:
            return
        hook = self._history_detail_request_hook
        if hook is None:
            self.history_detail_failed(event.entry_id, "Persisted output loading is unavailable.")
            return
        self.run_worker(
            hook(event.entry_id),
            group=f"history-detail-{event.entry_id}",
            exit_on_error=False,
        )

    def history_detail_loaded(self, entry_id: str, output: str) -> None:
        """Deliver one exact persisted payload to every waiting mounted card."""

        for card in self._history_detail_cards.pop(entry_id, []):
            if card.is_mounted:
                card.history_detail_loaded(entry_id, output)

    def history_detail_failed(self, entry_id: str, error: str) -> None:
        """Settle every card waiting on a failed exact-row lookup."""

        for card in self._history_detail_cards.pop(entry_id, []):
            if card.is_mounted:
                card.history_detail_failed(entry_id, error)

    def set_submit_hook(self, on_submit: Callable[[], str | None]) -> None:
        """Register the renderer's at-accept input-mode snapshot callback."""

        self._input_controller.set_submit_hook(on_submit)

    async def read_prompt(self, prompt: str) -> str:
        self.set_input_hint(prompt)
        return await self._input_controller.receive()

    async def run_shell(
        self,
        runner: Callable[[], Awaitable[TuiExitReason]],
    ) -> TuiExitReason:
        self._shell_snapshot = replace(self._shell_snapshot, input_ready=False)
        self._runner = runner
        # Textual must enable terminal mouse reporting for wheel/trackpad events to
        # reach the Transcript. Keep this explicit: the default is also True, but
        # silently reverting to mouse=False breaks real-terminal scrolling while
        # headless widget tests continue to pass.
        try:
            await self.run_async(mouse=True)
        finally:
            if self._terminal_writes is not None:
                self._terminal_writes.detach()
        for line in self._exit_unsent:
            self.console.print(line, markup=False, highlight=False)
        self._exit_unsent.clear()
        # Textual restores the terminal before returning; re-raise any error
        # from the shell worker here so it surfaces as a normal traceback
        # instead of being swallowed by the app teardown.
        if self._runner_error is not None:
            raise self._runner_error
        return self._runner_result

    async def _run_and_exit(self) -> None:
        if self._runner is None:
            return
        try:
            self._runner_result = await self._runner()
        except Exception as exc:
            self._runner_error = exc
        finally:
            try:
                await self._stream.shutdown()
            finally:
                self.exit()

    async def close(self) -> None:
        try:
            await self._stream.shutdown()
        finally:
            if self._terminal_writes is not None:
                self._terminal_writes.detach()
            self.exit()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy via pyperclip when available, otherwise Textual's OSC52 fallback."""

        try:
            import pyperclip  # type: ignore[import-untyped]
        except ImportError:
            pass
        else:
            with suppress(Exception):
                pyperclip.copy(text)
        super().copy_to_clipboard(text)

    def _editor_owns_selection(self) -> bool:
        """Whether a selection in the prompt editor owns the current copy gesture.

        The editor only owns copy (ctrl+c or mouse-drag) while it's actually
        visible and focused: when a decision panel is open the composer is hidden
        (display=False) and any stale draft selection must NOT claim ownership, or
        it swallows an interrupt/deny or blocks transcript auto-copy until cleared.
        """

        editor = self._input
        return bool(
            editor is not None and editor.display and editor.has_focus and editor.selected_text
        )

    def _prompt_deletion_mutates(self, *, backward: bool) -> bool:
        """Whether an editor deletion action will mutate the retained draft."""

        editor = self._input
        if editor is None:
            return False
        if editor.selected_text:
            return True
        cursor = editor.cursor_position
        return cursor > 0 if backward else cursor < len(editor.text)

    def _cursor_key_moves_prompt(self, key: str) -> bool:
        """Whether a focused prompt cursor action changes its selection."""

        editor = self._input
        if editor is None or self.screen.focused is not editor:
            return False
        selection = editor.selection
        cursor = selection.end
        selecting = key.startswith("shift+") or key.startswith("ctrl+shift+")
        if key in {"left", "shift+left"}:
            target = (
                editor.get_cursor_left_location()
                if selecting or selection.is_empty
                else min(selection)
            )
        elif key in {"right", "shift+right"}:
            target = (
                editor.get_cursor_right_location()
                if selecting or selection.is_empty
                else max(selection)
            )
        elif key in {"up", "shift+up"}:
            target = editor.get_cursor_up_location()
        elif key in {"down", "shift+down"}:
            target = editor.get_cursor_down_location()
        elif key in {"ctrl+left", "ctrl+shift+left"}:
            target = editor.get_cursor_word_left_location()
        elif key in {"ctrl+right", "ctrl+shift+right"}:
            target = editor.get_cursor_word_right_location()
        elif key == "ctrl+home":
            target = (0, 0)
        elif key == "ctrl+end":
            last_row = editor.document.line_count - 1
            target = (last_row, len(editor.document[last_row]))
        elif key in {"ctrl+a", "shift+home"}:
            target = editor.get_cursor_line_start_location(smart_home=True)
        elif key in {"ctrl+e", "shift+end"}:
            target = editor.get_cursor_line_end_location()
        elif key == "f6":
            row, _column = cursor
            return (selection.start, selection.end) != ((row, 0), (row, len(editor.document[row])))
        elif key == "f7":
            last_row = editor.document.line_count - 1
            return (selection.start, selection.end) != (
                (0, 0),
                (last_row, len(editor.document[last_row])),
            )
        else:
            return False
        return target != cursor if selecting else not selection.is_empty or target != cursor

    def _transcript_navigation_changes_view(self, key: str) -> bool:
        """Whether a transcript key can move or cross a retained-history edge."""

        transcript = self._transcript
        if transcript is None:
            return False
        can_move_up = transcript.scroll_y > 0 or transcript.can_page_to_older_history
        can_move_down = (
            transcript.scroll_y < transcript.max_scroll_y or transcript.can_page_to_newer_history
        )
        if key in {"home", "pageup"}:
            return can_move_up
        if key == "pagedown":
            return can_move_down
        if key == "end":
            return can_move_down or not transcript.is_following
        return False

    def _transcript_wheel_changes_view(
        self,
        event: events.MouseScrollUp | events.MouseScrollDown,
    ) -> bool:
        """Whether a routed vertical wheel gesture changes transcript state."""

        transcript = self._transcript
        if transcript is None:
            return False
        if isinstance(event, events.MouseScrollUp):
            return (
                transcript.scroll_y > 0
                or transcript.can_page_to_older_history
                or transcript.is_following
            )
        return transcript.scroll_y < transcript.max_scroll_y or transcript.can_page_to_newer_history

    def _is_streaming(self) -> bool:
        """Whether a streamed assistant turn is mid-flight and mutating the transcript.

        The active stream clears on flush, so selection bounds are stable before
        the final Markdown rendering settles.
        """

        return self._stream.is_streaming

    @on(events.TextSelected)
    def on_text_selected(self) -> None:
        """Auto-copy transcript selections so mouse drag + copy works."""

        # Don't auto-copy while the agent is streaming — the transcript
        # mutates and Textual's SELECTED bounds become stale, which would
        # copy unrelated output that scrolled into the selection region.
        # Explicit ctrl+c copy from the prompt editor still works regardless.
        with suppress(Exception):
            if self._transcript is None or self._is_streaming():
                return
            # If the (visible, focused) prompt editor has a selection, it owns the
            # copy — its own ctrl+c / ctrl+insert handling already covers it.
            if self._editor_owns_selection():
                return
            selection = self.screen.get_selected_text()
            if not selection:
                return
            self.copy_to_clipboard(selection)
            self.notify("Copied selection to clipboard.")

    def action_interrupt(self) -> None:
        # If the prompt editor owns the keystroke AND has selected text, ctrl+c
        # means "copy", not "interrupt". Because this binding is priority=True (so
        # it fires before TextArea's own handler and would otherwise swallow copy),
        # we explicitly copy here. Ownership requires the editor to be visible and
        # focused, so a stale selection behind a decision panel can't swallow the
        # interrupt/deny meant for the active approval.
        if self._editor_owns_selection():
            # Copy is best-effort: a broken terminal can make the OSC52 clipboard
            # write (or notify) raise, but the gesture was still a copy, not an
            # interrupt. Return unconditionally so a failed copy never falls
            # through to KeyboardInterrupt (which would also wipe the draft line).
            with suppress(Exception):
                selected = self._input.selected_text  # type: ignore[union-attr]
                self.copy_to_clipboard(selected)
                self.notify(f"Copied {len(selected)} chars to clipboard.")
            return
        self._signal_input(TuiQuitRequested(), action="quit")

    def action_toggle_contextual_help(self) -> None:
        """Toggle Textual's focus-aware help without moving keyboard focus."""

        if not self.is_running:
            return
        if self.screen.query(HelpPanel):
            transcript = self._transcript
            viewport_state = self._help_viewport_state
            baseline = self._help_viewport_baseline
            should_restore = bool(
                transcript is not None
                and viewport_state is not None
                and (baseline is None or transcript.viewport_state() == baseline)
            )
            self.action_hide_help_panel()
            self._help_viewport_state = None
            self._help_viewport_baseline = None
            if should_restore and transcript is not None and viewport_state is not None:
                self.call_after_refresh(transcript.restore_viewport_state, viewport_state)
            self._schedule_update_prompt()
            return

        if self._transcript is not None:
            self._help_viewport_state = self._transcript.viewport_state()
            self._help_viewport_baseline = None
        self.action_show_help_panel()
        if self._transcript is not None and self._help_viewport_state is not None:
            self.call_after_refresh(self._stabilize_help_viewport)

    def _stabilize_help_viewport(self) -> None:
        transcript = self._transcript
        viewport_state = self._help_viewport_state
        if transcript is None or viewport_state is None or not self.screen.query(HelpPanel):
            return
        transcript.restore_viewport_state(viewport_state)
        self._help_viewport_baseline = transcript.viewport_state()

    def _help_key_panel(self) -> KeyPanel | None:
        if not self.is_running:
            return None
        panels = self.screen.query(HelpPanel)
        if not panels:
            return None
        return panels.first().query_one(KeyPanel)

    def action_cancel(self) -> None:
        """Dismiss the nearest UI layer, then fall back to shell cancellation."""

        file_suggest = self._file_suggest
        if file_suggest is not None and file_suggest.is_active:
            file_suggest.dismiss()
            self.refresh_bindings()
            return
        suggest = self._suggest
        if suggest is not None and suggest.is_open:
            suggest.hide()
            self.refresh_bindings()
            return
        overlays = self._overlay_controller
        if overlays is not None and overlays.active_overlay is OverlayKind.connect:
            self.workers.cancel_group(self, "connect-oauth")
        if overlays is not None and overlays.active_operation is OverlayOperation.update:
            self._signal_input(TuiCancelRequested(), action="cancel", clear_editor=False)
            return
        if overlays is not None and overlays.consume_cancel():
            return
        self._signal_input(TuiCancelRequested(), action="cancel", clear_editor=False)

    def action_eof(self) -> None:
        editor = self._input
        if editor is not None and editor.text:
            # A draft remains authoritative even while an overlay temporarily
            # hides the composer; Ctrl+D must never turn that non-empty state into
            # EOF merely because focus moved.
            editor.action_delete_right()
            return
        self._signal_input(EOFError(), action="EOF")

    def action_toggle_agent_mode(self) -> None:
        """Route the plan/build hotkey through the normal slash-command path."""

        command = "/build" if self._agent_mode == "plan" else "/plan"
        if not self._block_submission_while_starting():
            self._input_controller.submit_line(command, clear_editor=False)

    def action_toggle_theme(self) -> None:
        """Toggle Paper against the most recently committed dark theme."""

        if self._theme_picker_original is not None:
            return
        current = WISP_THEME_BY_NAME.get(self.theme)
        next_name = (
            self._last_dark_theme if current is not None and not current.dark else PAPER_THEME_NAME
        )
        self._commit_theme(next_name, announce=True)

    def _commit_theme(self, theme_name: str, *, announce: bool) -> None:
        """Apply and persist one validated curated theme."""

        spec = WISP_THEME_BY_NAME.get(theme_name)
        if spec is None:
            return
        self.theme = spec.name
        if spec.dark:
            self._last_dark_theme = spec.name
        saved = save_theme_state(
            ThemePreferenceState(
                active_theme=spec.name,
                last_dark_theme=self._last_dark_theme,
            )
        )
        if announce:
            suffix = "" if saved else " (could not save)"
            self.write_notice(f"Theme: {spec.label}{suffix}")

    def show_theme_picker(self) -> None:
        picker = self._theme_picker
        overlays = self._overlay_controller
        if picker is None or overlays is None:
            return
        if picker.is_open:
            self.on_theme_picker_cancelled(ThemePicker.Cancelled())
            return
        self._theme_picker_original = self.theme
        self._invalidate_theme_preview()
        overlays.open(OverlayKind.theme_picker, preserve_viewport=True)
        picker.show(self.theme)

    def hide_theme_picker(self) -> bool:
        overlays = self._overlay_controller
        return overlays is not None and overlays.close(OverlayKind.theme_picker)

    def action_open_prompt_history(self) -> None:
        picker = self._prompt_history_picker
        if picker is None:
            return
        if picker.is_open:
            self.hide_prompt_history()
            return
        if self._input is None or not self._input.display:
            return
        overlays = self._overlay_controller
        if overlays is None:
            return
        editor_top = self._input.region.y
        overlays.open(OverlayKind.prompt_history, preserve_viewport=True)
        picker.styles.max_height = max(4, editor_top)
        picker.styles.offset = (0, "-100%")
        picker.show(self._input_controller.prompt_history_entries)

        def anchor_above_editor() -> None:
            overlap = picker.region.bottom - editor_top
            if overlap > 0:
                picker.styles.offset = (0, -picker.region.height - overlap)

        self.call_after_refresh(anchor_above_editor)

    # Scrollback: delegate to the Transcript's own scroll actions. Its scroll
    # watcher derives follow intent for normal movement; End uses return_to_latest
    # to restore that intent atomically before jumping. None-guarded like
    # _mount_line for calls before on_mount wires the widget.
    #
    # These are priority bindings (see BINDINGS), so they always win over the
    # focused widget's own PageUp/PageDown/Home/End handling. While a decision
    # panel is open, that would otherwise scroll the transcript out from under
    # the user instead of moving the approval highlight — stranding it on
    # whatever option was last selected (e.g. "Approve once") and turning the
    # next Enter into an unintended approval. Delegate to the panel's own
    # OptionList navigation in that case instead.
    def action_scroll_transcript_page_up(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_page_up()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_page_up()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_page_up()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_page_up()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_page_up()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            self._begin_transcript_navigation(preserve_live_history_recovery=True)
            navigation = self._transcript.page_up()
            if navigation is not None:
                self._pending_history_navigation = navigation
                self._recover_evicted_history_for_backward_navigation(navigation)

    def action_scroll_transcript_page_down(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_page_down()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_page_down()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_page_down()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_page_down()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_page_down()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            if not self._newer_navigation_in_progress():
                self._begin_transcript_navigation()
            self._transcript.page_down()

    def _newer_navigation_in_progress(self) -> bool:
        """Return whether a forward edge replacement is queued or in flight."""

        forward_intents = {
            HistoryNavigationIntent.PAGE_DOWN,
            HistoryNavigationIntent.WHEEL_DOWN,
        }
        anchor = self._history_prepend_anchor
        return (
            self._history_newer_request_pending
            or self._pending_history_navigation.intent in forward_intents
            or (anchor is not None and anchor.navigation.intent in forward_intents)
        )

    def _queue_newer_navigation(
        self,
        transcript: Transcript,
        navigation: HistoryNavigation,
        generation: int,
    ) -> None:
        """Merge repeated forward input without losing rows during async replacement."""

        anchor = self._history_prepend_anchor
        if anchor is not None and anchor.navigation.intent in {
            HistoryNavigationIntent.PAGE_DOWN,
            HistoryNavigationIntent.WHEEL_DOWN,
        }:
            anchor.navigation = _merge_history_navigation(anchor.navigation, navigation)
            return
        if self._history_newer_request_pending or self._pending_history_navigation.intent in {
            HistoryNavigationIntent.PAGE_DOWN,
            HistoryNavigationIntent.WHEEL_DOWN,
        }:
            self._pending_history_navigation = _merge_history_navigation(
                self._pending_history_navigation,
                navigation,
            )
            return
        self._pending_history_navigation = navigation
        self.call_after_refresh(
            self._continue_newer_navigation,
            transcript,
            generation,
            self._transcript_epoch,
        )

    def _continue_newer_navigation(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        """Carry a forward reader gesture across the mounted history edge."""

        if not (
            generation == self._transcript_navigation_generation
            and epoch == self._transcript_epoch
            and transcript is self._transcript
        ):
            return
        shift_newer = self._history_window_newer_hook
        if shift_newer is not None and shift_newer():
            return
        self._pending_history_navigation = HistoryNavigation()

    def action_scroll_transcript_home(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_home()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_scroll_home()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_first()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_first()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_first()
            return
        self._cancel_card_expand_repin()
        if self._transcript is not None:
            generation = self._begin_transcript_navigation()
            self._oldest_navigation_generation = generation
            self._transcript.scroll_to_oldest()
            self._continue_oldest_navigation(generation, self._transcript_epoch)

    def action_scroll_transcript_end(self) -> None:
        if self._diff_viewer is not None and self._diff_viewer.is_open:
            self._diff_viewer.action_end()
            return
        if help_keys := self._help_key_panel():
            help_keys.action_scroll_end()
            return
        if self._decision_panel is not None and self._decision_panel.is_open:
            self._decision_panel.move_highlight_last()
            return
        if self._prompt_history_picker is not None and self._prompt_history_picker.is_open:
            self._prompt_history_picker.move_highlight_last()
            return
        if self._session_picker is not None and self._session_picker.is_open:
            self._session_picker.move_highlight_last()
            return
        show_latest = self._history_window_latest_hook
        if show_latest is not None:
            show_latest()
        self._scroll_transcript_to_latest()

    def _scroll_transcript_to_latest(self) -> None:
        # The actual "scroll to bottom" behavior, split from
        # action_scroll_transcript_end so the jump-to-latest overlay's click
        # handler (on_jump_to_latest_selected) can call it directly. That
        # overlay is a mouse affordance meaning "get me to the bottom of the
        # transcript" — never a decision-panel-navigation gesture — so it must
        # not be redirected to move the panel highlight just because one
        # happens to be open, unlike a real End keypress.
        if self._transcript is not None:
            self._begin_transcript_navigation()
            self._transcript.return_to_latest()
        self._transcript_controller.clear_unseen_output()

    def _signal_input(
        self,
        signal: BaseException,
        *,
        action: str,
        clear_editor: bool = True,
    ) -> None:
        # Pending compact echoes remain intact here. Control signals can merely
        # affect presentation or arm quit; the shell reclaims echoes only when it
        # definitively drops queued follow-ups.
        self._input_controller.signal(
            signal,
            action=action,
            clear_editor=clear_editor,
        )

    def set_status(self, snapshot: TuiViewSnapshot) -> None:
        self._agent_mode = snapshot.mode
        self._visible_input_mode = snapshot.input_mode
        self._shell_snapshot = snapshot
        if self._input is not None:
            self._input.set_running(snapshot.input_mode == "running")
        self._render_pending_submissions()
        if self._status is not None:
            self._status.set_snapshot(snapshot)
        self._schedule_update_prompt()

    def status_width(self) -> int | None:
        # The width the footer text actually renders into: the #status widget's
        # content region (padding-excluded), NOT app.size.width. The status bar
        # has horizontal padding, so sizing from the app width over-pads each
        # footer line and makes it wrap/clip past the real region. Falls back to
        # None (no padding) before layout gives the widget a positive width.
        if self._status is None:
            return None
        width = self._status.content_size.width
        return width if width > 0 else None

    def set_input_hint(self, hint: str) -> None:
        self._current_prompt = hint
        if self._input is not None:
            self._input.placeholder = _input_placeholder(hint)

    def _defer_overlay_restore(self, callback: Callable[[], None]) -> None:
        self.call_after_refresh(callback)

    def record_prompt(self, prompt: str) -> None:
        """Retain one shell-accepted prompt for this TUI process only."""

        self._input_controller.record_prompt(prompt)

    def show_prompt_history(self) -> None:
        self.action_open_prompt_history()

    def hide_prompt_history(self) -> bool:
        overlays = self._overlay_controller
        if overlays is None:
            return False
        return overlays.close(OverlayKind.prompt_history)

    def show_approval(self, event: ToolApprovalRequested, *, cwd: str) -> None:
        panel = self._decision_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.decision)
        panel.show_approval(event, cwd=cwd)

    def show_trust(self, event: TrustRequested) -> None:
        panel = self._decision_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.decision)
        panel.show_trust(event)

    def hide_decision(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.decision)

    def show_connect_panel(
        self,
        providers: tuple[ConnectionProviderStatus, ...],
        *,
        mode: ConnectPanelMode = "connect",
        provider: str | None = None,
    ) -> None:
        panel = self._connect_panel
        overlays = self._overlay_controller
        if panel is None or overlays is None:
            return
        overlays.open(OverlayKind.connect)
        panel.show(
            providers,
            mode=mode,
            provider=provider,
        )

    def show_connect_device_code(self, verification_uri: str, user_code: str) -> None:
        panel = self._connect_panel
        if panel is None or not panel.is_open:
            return
        panel.query_one("#connect-title", Static).update("Authorize ChatGPT Plus/Pro")
        panel.query_one("#connect-hint", Static).update(
            f"{verification_uri} · code {user_code} · esc cancel"
        )

    def show_connect_error(self, message: str) -> None:
        panel = self._connect_panel
        if panel is not None and panel.is_open:
            panel.show_error(message)

    def hide_connect_panel(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.connect)

    def show_model_picker(
        self,
        entries: tuple[ModelCatalogProviderEntry, ...],
        *,
        current_provider: str,
        current_model: str | None,
        current_effort: str | None,
    ) -> None:
        picker = self._model_picker
        overlays = self._overlay_controller
        if picker is None or overlays is None:
            return
        overlays.open(OverlayKind.model_picker)
        picker.show(
            entries,
            current_provider=current_provider,
            current_model=current_model,
            current_effort=current_effort,
        )

    def hide_model_picker(self) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.model_picker)

    def show_session_picker(
        self,
        sessions: tuple[RpcSessionSummary, ...],
        *,
        selected_session_id: str | None,
    ) -> None:
        picker = self._session_picker
        overlays = self._overlay_controller
        if picker is None or overlays is None:
            return
        overlays.open(OverlayKind.session_picker)
        picker.show(sessions, selected_session_id=selected_session_id)

    def hide_session_picker(self, *, restore_input: bool = True) -> None:
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.close(OverlayKind.session_picker, restore_composer=restore_input)

    def show_context_status(self, stats: SessionStats) -> None:
        """Show an explicitly requested visual context report."""

        overlay = self._context_status
        overlays = self._overlay_controller
        if overlay is None or overlays is None:
            return
        # A context response may arrive after another interaction or session
        # operation took ownership. Do not let this informational report displace
        # approvals, pickers, or a guarded session transition.
        if overlays.active_overlay is not None or overlays.active_operation is not None:
            return
        overlays.open(OverlayKind.context_status, preserve_viewport=True)
        overlay.show_stats(stats)

    def hide_context_status(self) -> bool:
        """Dismiss the context report and restore its composer/viewport state."""

        overlays = self._overlay_controller
        if overlays is None:
            return False
        return overlays.close(OverlayKind.context_status)

    def show_diff_viewer(self, presentation: DiffPresentation) -> None:
        """Show one tool-card diff with an independently scrollable full view."""

        viewer = self._diff_viewer
        overlays = self._overlay_controller
        if viewer is None or overlays is None:
            return
        if overlays.active_overlay is not None or overlays.active_operation is not None:
            return
        overlays.open(OverlayKind.diff_viewer, preserve_viewport=True)
        viewer.show_diff(presentation)

    def hide_diff_viewer(self) -> bool:
        """Dismiss the diff reader and restore transcript position and composer."""

        overlays = self._overlay_controller
        if overlays is None:
            return False
        return overlays.close(OverlayKind.diff_viewer)

    def history_hydration_started(self) -> None:
        self._start_session_operation(OverlayOperation.history_hydration)

    def history_hydration_progress(self, label: str) -> None:
        """Update the active history operation without restarting its spinner."""

        overlays = self._overlay_controller
        if overlays is None or overlays.active_operation not in {
            OverlayOperation.history_hydration,
            OverlayOperation.session_switch,
        }:
            return
        indicator = self._operation_indicator
        if indicator is not None:
            indicator.update_operation(label)

    def history_hydration_finished(self) -> None:
        self._finish_session_operation(OverlayOperation.history_hydration)

    def session_catalog_started(self) -> None:
        self._start_session_operation(OverlayOperation.session_catalog)

    def session_catalog_finished(self) -> None:
        self._finish_session_operation(OverlayOperation.session_catalog)

    def session_switch_started(self) -> None:
        self._start_session_operation(OverlayOperation.session_switch)

    def session_switch_finished(self) -> None:
        self._finish_session_operation(OverlayOperation.session_switch)

    async def wait_for_session_operation_paint(self) -> None:
        """Wait until newly visible session-operation chrome reaches a frame."""

        indicator = self._operation_indicator
        if indicator is None or not indicator.is_running or not indicator.is_open:
            return
        await indicator.wait_for_refresh()

    def _start_session_operation(self, operation: OverlayOperation) -> None:
        """Show typed session work without giving the indicator lifecycle ownership."""

        self._session_operation_generation += 1
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.start_operation(operation)
        indicator = self._operation_indicator
        if indicator is not None:
            indicator.show_operation(
                _SESSION_OPERATION_LABELS[operation],
                cover_transcript=operation in _TRANSCRIPT_COVERING_OPERATIONS,
            )

    def _finish_session_operation(self, operation: OverlayOperation) -> None:
        """Hide a typed operation only after any covered transcript has settled."""

        overlays = self._overlay_controller
        if overlays is None:
            return
        if operation not in _TRANSCRIPT_COVERING_OPERATIONS:
            if overlays.finish_operation(operation):
                self._hide_operation_indicator()
            return
        if not overlays.prepare_operation_finish(operation):
            return
        transcript = self._transcript
        if (
            transcript is None
            or not transcript.is_running
            or all(isinstance(child, TranscriptEmptyState) for child in transcript.children)
        ):
            if overlays.finish_operation(operation):
                self._hide_operation_indicator()
            return
        generation = self._session_operation_generation
        self.run_worker(
            self._settle_covered_session_operation(
                operation,
                transcript,
                generation=generation,
                epoch=self._transcript_epoch,
            ),
            group="session-operation-finish",
            exclusive=True,
            exit_on_error=False,
        )

    async def _settle_covered_session_operation(
        self,
        operation: OverlayOperation,
        transcript: Transcript,
        *,
        generation: int,
        epoch: int,
    ) -> None:
        """Keep history covered until consecutive frames prove its layout is ready."""

        previous_ready_geometry: tuple[float, float, int, int] | None = None
        while self._covered_session_operation_is_current(
            operation,
            transcript,
            generation=generation,
            epoch=epoch,
        ):
            # A hydration operation can finish while Textual is still resolving the
            # final bounded mount batch. Await the currently published batch on every
            # pass; resolved awaitables are cheap, while a newly published batch must
            # settle before its child geometry can be trusted.
            await self.wait_for_history_render()
            await transcript.wait_for_refresh()
            if not self._covered_session_operation_is_current(
                operation,
                transcript,
                generation=generation,
                epoch=epoch,
            ):
                return

            viewport_height = transcript.scrollable_content_region.height
            layout_ready = self._history_render_depth == 0 and (
                viewport_height <= 0
                or not any(_transcript_child_layout_pending(child) for child in transcript.children)
            )
            at_required_offset = (
                viewport_height <= 0
                or not transcript.is_following
                or transcript.scroll_y == transcript.max_scroll_y
            )
            if not layout_ready or not at_required_offset:
                previous_ready_geometry = None
                if layout_ready and transcript.is_following:
                    # The composer is already restored by prepare_operation_finish().
                    # Follow only after its final viewport geometry is measurable.
                    # Re-arm on each frame because later Markdown measurement may
                    # move the tail after an earlier deferred callback ran.
                    transcript.follow_tail()
                continue

            ready_geometry = (
                float(transcript.scroll_y),
                float(transcript.max_scroll_y),
                transcript.scrollable_content_region.height,
                transcript.virtual_size.height,
            )
            if ready_geometry == previous_ready_geometry:
                overlays = self._overlay_controller
                if overlays is not None and overlays.finish_operation(operation):
                    self._hide_operation_indicator()
                return

            # Equality may first become true in the deferred tail callback, before
            # the corrected offset has reached the terminal. Require one more frame
            # with unchanged viewport and virtual geometry before removing the cover.
            previous_ready_geometry = ready_geometry

    def _covered_session_operation_is_current(
        self,
        operation: OverlayOperation,
        transcript: Transcript,
        *,
        generation: int,
        epoch: int,
    ) -> bool:
        overlays = self._overlay_controller
        return (
            overlays is not None
            and overlays.active_operation is operation
            and generation == self._session_operation_generation
            and epoch == self._transcript_epoch
            and transcript is self._transcript
        )

    def _hide_operation_indicator(self) -> None:
        indicator = self._operation_indicator
        if indicator is not None:
            indicator.hide()

    def replace_transcript(self) -> None:
        """Drop the previous session's UI-owned transcript bookkeeping."""

        self._transcript_epoch += 1
        self._history_detail_cards.clear()
        self._history_marker = None
        self._prepending_history = False
        self._history_prepend_paint_suppressed = False
        self._live_history_reload_pending = False
        self._history_newer_request_pending = False
        self._live_history_reload_needed = False
        self._live_history_eviction_generation = 0
        self._live_history_reload_generation = None
        self._live_history_recovery_navigation = None
        self._live_history_recovery_blocked = False
        self._history_prepend_mounts.clear()
        self._history_prepend_anchor = None
        self._cancel_oldest_navigation()
        self._history_render_depth = 0
        batch = self._history_render_batch
        self._history_render_batch = None
        if batch is not None:
            batch.__exit__(None, None, None)
        self._history_render_mounts.clear()
        self._last_history_render_mounts = ()
        self._history_removing_widgets.clear()
        self._history_layout_generation += 1
        self._stream.discard()
        self._transcript_controller.reset()
        self._input_controller.clear_compact_echoes()
        if self._transcript is not None:
            self._transcript.clear_messages()

    def set_history_window_hooks(
        self,
        *,
        shift_older: Callable[[], bool],
        shift_newer: Callable[[], bool],
        show_oldest: Callable[[], bool],
        show_latest: Callable[[], bool],
    ) -> None:
        """Install renderer-owned history-window navigation callbacks."""

        self._history_window_older_hook = shift_older
        self._history_window_newer_hook = shift_newer
        self._history_window_oldest_hook = show_oldest
        self._history_window_latest_hook = show_latest

    def set_connect_api_key_hook(
        self,
        hook: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Install the shell-owned secret-storage callback."""

        self._connect_api_key_hook = hook

    def set_connect_oauth_hook(
        self,
        hook: Callable[[str], Awaitable[None]],
    ) -> None:
        """Install the shell-owned device-authorization callback."""

        self._connect_oauth_hook = hook

    def set_update_action_hook(
        self,
        hook: Callable[[UpdatePromptAction, UpdateAvailable], Awaitable[None]],
    ) -> None:
        """Install the shell-owned update-choice callback."""

        self._update_action_hook = hook

    def offer_update(self, update: UpdateAvailable) -> None:
        """Queue one release offer until the Textual surface is safely idle."""

        self._pending_update = update
        self._schedule_update_prompt()

    def _schedule_update_prompt(self) -> None:
        if self._pending_update is not None and self.is_running:
            self.call_after_refresh(self._maybe_show_update_prompt)

    def _maybe_show_update_prompt(self) -> None:
        update = self._pending_update
        prompt = self._update_prompt
        overlays = self._overlay_controller
        editor = self._input
        if update is None or prompt is None or overlays is None or editor is None:
            return
        if (
            self._visible_input_mode != "idle"
            or editor.text != ""
            or self._suggestion_menu_is_open()
            or overlays.active_overlay is not None
            or overlays.active_operation is not None
            or bool(self.screen.query(HelpPanel))
        ):
            return
        overlays.open(OverlayKind.update_prompt)
        prompt.show_update(update)

    def update_operation_started(self, update: UpdateAvailable) -> None:
        overlays = self._overlay_controller
        indicator = self._operation_indicator
        if overlays is not None:
            overlays.start_operation(OverlayOperation.update)
        if indicator is not None:
            indicator.show_operation(f"Updating Wisp to {update.latest_version}…")

    def update_operation_finished(self, *, installed: bool, restarting: bool) -> None:
        indicator = self._operation_indicator
        if restarting:
            if indicator is not None:
                indicator.show_operation("Restarting Wisp…")
            return
        overlays = self._overlay_controller
        if overlays is not None:
            overlays.finish_operation(OverlayOperation.update)
        if indicator is not None:
            indicator.hide()

    def request_latest_history(self) -> bool:
        """Schedule a durable latest-page reload requested by history retention.

        Routes through ``_start_live_history_reload`` (an ungated primitive —
        it does not itself check ``_live_history_reload_needed``) rather than
        launching the worker directly, so ``_live_history_reload_pending`` is
        recorded here the same as it is for the other caller
        (``_request_live_history_reload``). Without that, a caller could
        follow this call with its own ``_request_live_history_reload()`` in
        the same tick and find ``_live_history_reload_pending`` still
        ``False``, dispatching a second, redundant ``history-latest-reload``
        worker for the same reload.
        """

        hook = self._history_latest_request_hook
        if hook is None:
            return False
        self._start_live_history_reload(hook)
        return True

    def request_newer_history(self, after_entry_id: str) -> bool:
        """Request the durable page immediately after a retained window edge."""

        hook = self._history_newer_request_hook
        if hook is None or self._history_newer_request_pending:
            return False
        self._history_newer_request_pending = True
        self.run_worker(
            hook(after_entry_id),
            group="history-newer-page-request",
            exit_on_error=False,
        )
        return True

    def history_newer_page_loaded(self) -> None:
        """Release the serialized newer-page request guard."""

        self._history_newer_request_pending = False

    def history_newer_page_request_failed(self) -> None:
        """Allow retrying a failed adjacent-newer page request."""

        self._history_newer_request_pending = False
        self._pending_history_navigation = HistoryNavigation()

    def history_is_at_top(self) -> bool:
        """Return whether persisted-history navigation is at the transcript top."""

        return self._transcript is not None and self._transcript.scroll_y == 0

    def history_is_following(self) -> bool:
        """Return the current tail-follow intent for retained-history appends."""

        return self._transcript is None or self._transcript.is_following

    # --- Transient live transcript presentation ---

    def transcript_available(self) -> bool:
        """Return whether Textual has mounted the live transcript widget."""

        return self._transcript is not None

    def mount_live_transcript_widget(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> None:
        """Mount one live transcript widget through the app's layout owner."""

        self._mount_transcript_message(widget, before=before)

    def remove_live_transcript_widget(self, widget: Widget) -> None:
        """Remove one live transcript widget without surfacing a stale unmount."""

        with suppress(Exception):
            widget.remove()

    def move_live_transcript_widget(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> None:
        """Move an existing lifecycle card without remounting or losing focus state."""

        transcript = self._transcript
        if transcript is None or widget.parent is not transcript:
            return
        if before is not None and before.parent is transcript and before is not widget:
            transcript.move_child(widget, before=before)
            return
        indicator = self._transcript_controller.working_indicator
        if indicator is not None and indicator.parent is transcript and indicator is not widget:
            transcript.move_child(widget, before=indicator)
        elif transcript.children and transcript.children[-1] is not widget:
            transcript.move_child(widget, after=transcript.children[-1])

    def live_transcript_widget_evicted(self, widget: Widget) -> None:
        """Let the renderer release live de-duplication after bounded eviction."""

        hook = self._live_widget_evicted_hook
        if hook is not None:
            hook(widget)
        self._live_history_eviction_generation += 1
        self._live_history_reload_needed = True
        self._request_live_history_reload()

    def _recover_evicted_history_for_backward_navigation(
        self,
        navigation: HistoryNavigation,
    ) -> None:
        """Load a durable prefix when PageUp or the wheel reaches live eviction.

        Ordinary history paging owns retained and server-known older pages. This
        path is only for a live transcript whose oldest mounted widgets were
        evicted before the reader started browsing, leaving no page cursor for
        the normal edge handler to follow.
        """

        transcript = self._transcript
        request_latest = self._history_latest_request_hook
        if (
            not self._live_history_reload_needed
            or request_latest is None
            or transcript is None
            or transcript.is_following
            or transcript.scroll_y != 0
            or transcript.can_page_to_older_history
            or self._live_history_recovery_blocked
        ):
            return
        pending = self._live_history_recovery_navigation
        if pending is not None:
            # Wheel events can arrive while the RPC page is in flight. Retain the
            # unconsumed distance so the restored viewport lands where the reader
            # intended, rather than making them repeat the burst after every page.
            self._live_history_recovery_navigation = HistoryNavigation(
                navigation.intent,
                pending.remaining_rows + navigation.remaining_rows,
                transcript.follow_generation,
            )
            return
        if self._live_history_reload_pending:
            return
        self._live_history_recovery_navigation = HistoryNavigation(
            navigation.intent,
            navigation.remaining_rows,
            transcript.follow_generation,
        )
        self._start_live_history_reload(request_latest)

    def consume_live_history_recovery(self) -> HistoryNavigation | None:
        """Return a still-valid backward recovery intent for the renderer."""

        navigation = self._live_history_recovery_navigation
        transcript = self._transcript
        self._live_history_recovery_navigation = None
        if (
            navigation is None
            or transcript is None
            or transcript.is_following
            or transcript.scroll_y != 0
        ):
            return None
        navigation = HistoryNavigation(
            navigation.intent,
            navigation.remaining_rows,
            transcript.follow_generation,
        )
        self._pending_history_navigation = navigation
        return navigation

    def _request_live_history_reload(self) -> None:
        transcript = self._transcript
        request_latest = self._history_latest_request_hook
        if (
            not self._live_history_reload_needed
            or self._live_history_reload_pending
            or request_latest is None
            or transcript is None
            or not transcript.is_following
        ):
            return
        self._start_live_history_reload(request_latest)

    def _start_live_history_reload(self, request_latest: Callable[[], Awaitable[None]]) -> None:
        """Start one serialized reload."""

        self._live_history_reload_pending = True
        self.run_worker(
            request_latest(),
            group="history-latest-reload",
            exit_on_error=False,
        )

    def capture_live_history_reload(self) -> None:
        """Record the evictions covered when the durable request actually starts."""

        self._live_history_reload_generation = self._live_history_eviction_generation

    def live_history_reloaded(self) -> None:
        """Finish one reload and repeat it if newer output was evicted in flight."""

        covered_generation = self._live_history_reload_generation
        self._live_history_reload_pending = False
        self._live_history_reload_generation = None
        self._live_history_reload_needed = (
            covered_generation is not None
            and covered_generation != self._live_history_eviction_generation
        )
        self._live_history_recovery_navigation = None
        self._live_history_recovery_blocked = False
        self._request_live_history_reload()

    def live_history_recovery_deferred(self) -> None:
        """Release an unsafe oldest-window recovery without losing tail reload work."""

        self._live_history_reload_pending = False
        self._live_history_reload_generation = None
        self._live_history_recovery_navigation = None
        self._live_history_recovery_blocked = True

    def live_history_reload_failed(self) -> None:
        """Release a failed request while retaining recovery work for a later retry."""

        self._live_history_reload_pending = False
        self._live_history_reload_generation = None
        self._live_history_recovery_navigation = None

    def set_live_widget_evicted_hook(self, hook: Callable[[Widget], None]) -> None:
        """Register the renderer-owned durable-history identity release hook."""

        self._live_widget_evicted_hook = hook

    def settle_stream_widget(self, widget: Widget) -> None:
        """Bound a completed native Markdown stream like other settled live output."""

        self._transcript_controller.settle_widget(widget, durable_entry_count=1)

    def transcript_is_following(self) -> bool:
        """Return whether a mounted transcript currently follows its tail."""

        return self._transcript is not None and self._transcript.is_following

    def return_transcript_to_latest(self) -> None:
        """Restore transcript tail-follow after a focused newest card expands."""

        if self._transcript is not None:
            self._transcript.return_to_latest()

    def is_newest_transcript_widget(self, widget: Widget) -> bool:
        """Return whether ``widget`` is the latest durable transcript message."""

        transcript = self._transcript
        if transcript is None:
            return False
        indicator = self._transcript_controller.working_indicator
        return (
            next(
                (child for child in reversed(transcript.children) if child is not indicator),
                None,
            )
            is widget
        )

    def show_unseen_output(self, count: int) -> None:
        """Show the jump-to-latest affordance for distinct unseen live output."""

        if self._jump_to_latest is not None:
            self._jump_to_latest.show_count(count)

    def hide_unseen_output(self) -> None:
        """Hide the jump-to-latest affordance when no live output is unseen."""

        if self._jump_to_latest is not None:
            self._jump_to_latest.hide()

    def show_working_indicator(self) -> None:
        self._transcript_controller.show_working_indicator()

    def renew_working_indicator(self) -> None:
        """Transfer the heartbeat to a newer model turn without remounting it."""

        self._transcript_controller.renew_working_indicator()

    def show_retry_indicator(self, label: str) -> None:
        self._transcript_controller.show_retry_indicator(label)

    def show_activity_indicator(self, label: str) -> None:
        self._transcript_controller.show_activity_indicator(label)

    def restart_working_indicator(self) -> None:
        """Start fresh transcript activity for a newly submitted prompt."""

        self._transcript_controller.restart_working_indicator()

    def hide_working_indicator(self) -> None:
        self._transcript_controller.hide_working_indicator()

    def working_indicator_for_stream(self) -> tuple[WorkingIndicator, int] | None:
        """Capture the heartbeat lease owned by a newly mounted assistant stream."""

        return self._transcript_controller.working_indicator_identity

    def hide_working_indicator_if_current(
        self,
        indicator: WorkingIndicator,
        *,
        generation: int,
    ) -> None:
        """Retire a stream heartbeat only while its captured turn still owns it."""

        self._transcript_controller.hide_working_indicator_if_current(
            indicator,
            generation=generation,
        )

    def hide_working_indicator_after_stream(self) -> None:
        """Remove the current heartbeat with the completed stream's final layout."""

        identity = self._transcript_controller.working_indicator_identity
        if identity is None:
            return
        indicator, generation = identity

        def hide_if_current() -> None:
            self._transcript_controller.hide_working_indicator_if_current(
                indicator,
                generation=generation,
            )

        if not self._stream.defer_until_latest_stream_settles(hide_if_current):
            hide_if_current()

    def mount_process_card(
        self,
        process_id: str,
        *,
        historical: bool = False,
        before: Widget | None = None,
        reposition: bool = False,
    ) -> ProcessCard | None:
        """Mount or recover one process-level presentation card."""

        return self._transcript_controller.mount_process_card(
            process_id,
            historical=historical,
            before=before,
            reposition=reposition,
        )

    def mount_process_call(self, call_id: str, process_id: str) -> ProcessCard | None:
        """Alias one live poll/cancel call to its process-level card."""

        return self._transcript_controller.mount_process_call(call_id, process_id)

    def update_historical_process_card(
        self,
        card: Widget,
        presentation: ProcessLifecyclePresentation,
    ) -> ProcessCard | None:
        """Update one retained process replay card independently of live output."""

        return self._transcript_controller.update_historical_process_card(
            card,
            presentation,
        )

    def update_process_card(
        self,
        presentation: ProcessLifecyclePresentation,
        *,
        elapsed: float | None = None,
        settle_terminal: bool = False,
    ) -> ProcessCard | None:
        """Apply one bounded lifecycle snapshot to a process card."""

        return self._transcript_controller.update_process_card(
            presentation,
            elapsed=elapsed,
            settle_terminal=settle_terminal,
        )

    def resolve_process_call(
        self,
        call_id: str,
        presentation: ProcessLifecyclePresentation,
        *,
        elapsed: float | None = None,
    ) -> ProcessCard | None:
        """Finish one call alias and update its stable process card."""

        return self._transcript_controller.resolve_process_call(
            call_id,
            presentation,
            elapsed=elapsed,
        )

    def mount_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: object,
        *,
        historical_card_id: str | None = None,
        historical: bool = False,
        arguments_available: bool = True,
        before: Widget | None = None,
    ) -> ToolCard | None:
        """Mount and register one evolving live or retained-history tool card."""

        return self._transcript_controller.mount_tool_call(
            call_id,
            name,
            arguments,
            historical_card_id=historical_card_id,
            historical=historical,
            arguments_available=arguments_available,
            before=before,
        )

    def enrich_historical_tool_call(
        self,
        card_id: str,
        name: str,
        arguments: object,
        *,
        status: ToolActionStatus,
        detail: str | Content | DiffPresentation | FileResultPresentation,
        full_output: str,
        truncated: bool,
    ) -> bool:
        """Apply a paged-in call to its already-mounted historical result card."""

        return self._transcript_controller.enrich_historical_tool_call(
            card_id,
            name,
            arguments,
            status=status,
            detail=detail,
            full_output=full_output,
            truncated=truncated,
        )

    def resolve_tool_call(
        self,
        call_id: str,
        status: ToolActionStatus,
        *,
        detail: str | Content | DiffPresentation | FileResultPresentation = "",
        elapsed: float | None = None,
        full_output: str = "",
        truncated: bool = False,
    ) -> ToolCard | None:
        """Resolve a registered tool card in place."""

        return self._transcript_controller.resolve_tool_call(
            call_id,
            status,
            detail=detail,
            elapsed=elapsed,
            full_output=full_output,
            truncated=truncated,
        )

    def fail_pending_tool_calls(self, detail: str = "cancelled") -> None:
        """Drain unresolved live tool cards after a cancelled or failed turn."""

        self._transcript_controller.fail_pending_tool_calls(detail)

    def _write_styled(self, role: str, message: str) -> None:
        self._mount_line(role, message)

    def write_notice(self, message: str) -> None:
        self._write_styled("notice", message)

    def write_error(self, message: str) -> None:
        self._write_styled("error", message)

    def write_dim(self, message: str) -> None:
        self._write_styled("dim", message)

    def mount_history_marker(self, message: str, *, before: Widget | None) -> None:
        """Mount the durable boundary between the session label and history."""

        widget = LineMessage(message, role="dim")
        transcript = self._transcript
        if before is not None and transcript is not None and before.parent is not transcript:
            # Textual may not have attached a widget mounted in the completed
            # history batch yet. Wait for that batch before using its widget as
            # a relative mount point, rather than appending this marker below
            # the restored transcript.
            self._history_marker = widget
            self.run_worker(
                self._mount_history_marker_after_history_batch(
                    widget,
                    before=before,
                    transcript=transcript,
                    epoch=self._transcript_epoch,
                ),
                group="history-marker",
                exit_on_error=False,
            )
            return
        self._mount_history_marker_widget(widget, before=before)

    async def _mount_history_marker_after_history_batch(
        self,
        widget: LineMessage,
        *,
        before: Widget,
        transcript: Transcript,
        epoch: int,
    ) -> None:
        """Wait for restored history before mounting a deferred session marker."""

        await self.wait_for_history_render()
        self.call_after_refresh(
            self._mount_deferred_history_marker,
            widget,
            before,
            transcript,
            epoch,
        )

    def _mount_deferred_history_marker(
        self,
        widget: LineMessage,
        before: Widget,
        transcript: Transcript,
        epoch: int,
    ) -> None:
        """Mount a session marker once its history boundary is attached."""

        if epoch != self._transcript_epoch or transcript is not self._transcript:
            return
        # A failed or superseded history mount may leave the original boundary
        # detached. The transcript head is still a stable boundary that keeps
        # the marker above all restored entries.
        mount_before = (
            before if before.parent is transcript else next(iter(transcript.children), None)
        )
        self._mount_history_marker_widget(widget, before=mount_before)

    def _mount_history_marker_widget(self, widget: LineMessage, *, before: Widget | None) -> None:
        """Mount and retain one session-history boundary widget."""

        self._mount_transcript_message(widget, before=before)
        self._history_marker = widget

    def write_user(self, message: str) -> LineMessage | None:
        return self._mount_line("user", message)

    def write_assistant(self, message: str) -> StreamMessage | None:
        """Mount a settled assistant turn through the same Markdown path as streaming."""

        if self._transcript is None:
            return None
        widget = StreamMessage(message)
        self._mount_transcript_message(widget)
        self._transcript_controller.settle_widget(widget, durable_entry_count=1)
        self.note_transcript_update(widget)
        self._follow_tail_after_refresh()
        return widget

    def write_message(self, message: str, *, role: str) -> Widget | None:
        # Assistant content is Markdown whether it streamed live or arrived as one
        # completed event. Other roles remain literal at the widget boundary.
        if role == "assistant":
            return self.write_assistant(message)
        return self._mount_line(role, message)

    def _mount_line(self, role: str, text: str) -> LineMessage | None:
        # Mount one role-styled LineMessage. Transcript owns the transcript and
        # its own follow-the-tail intent; we just re-assert the follow after the
        # mount lays out.
        if self._transcript is None:
            return None
        widget = LineMessage(text, role=role)
        self._mount_transcript_message(widget)
        self._transcript_controller.settle_widget(
            widget,
            durable_entry_count=1 if role in {"user", "assistant"} else 0,
        )
        self.note_transcript_update(widget)
        self._follow_tail_after_refresh()
        return widget

    def mount_historical_line(
        self,
        role: str,
        message: str,
        *,
        before: Widget | None = None,
    ) -> Widget | None:
        """Mount one retained history message without treating it as new output."""

        if self._transcript is None:
            return None
        widget: Widget = (
            StreamMessage(message)
            if role == "assistant" and _historical_message_needs_markdown(message)
            else LineMessage(message, role=role)
        )
        self._mount_transcript_message(widget, before=before)
        return widget

    def _mount_transcript_message(
        self,
        widget: Widget,
        *,
        before: Widget | None = None,
    ) -> AwaitMount | None:
        transcript = self._transcript
        if transcript is None:
            return None
        anchor = self._history_prepend_anchor
        anchor_boundary = (
            anchor.widget
            if (
                self._prepending_history
                and anchor is not None
                and anchor.transcript is transcript
                and anchor.widget is not None
                and anchor.widget.parent is transcript
                and anchor.navigation.intent
                not in {HistoryNavigationIntent.PAGE_DOWN, HistoryNavigationIntent.WHEEL_DOWN}
            )
            else None
        )
        # Textual queues mounts until the next message-pump cycle. Reconciliation
        # can therefore offer a widget mounted earlier in this batch as the next
        # insertion boundary even though it has no parent yet. Textual rejects
        # such a relative mount, so fall back to the stable prepend anchor.
        indicator = self._transcript_controller.working_indicator
        live_boundary = (
            indicator
            if before is None and widget is not indicator and indicator is not None
            else None
        )
        mount_before = next(
            (
                candidate
                for candidate in (before, live_boundary, anchor_boundary)
                if candidate is not None and candidate.parent is transcript
            ),
            None,
        )
        mounted = transcript.mount_message(widget, before=mount_before)
        if self._prepending_history:
            self._history_prepend_mounts.append(mounted)
        if self._history_render_depth:
            self._history_render_mounts.append(mounted)
        if live_boundary is not None and live_boundary.parent is not transcript:
            # Both mounts may be queued in one message-pump turn. Once they settle,
            # restore the heartbeat as the tail without unmounting or resetting it.
            self.call_after_refresh(self._move_working_indicator_to_tail)
        return mounted

    def mount_stream_widget(self, widget: StreamMessage) -> AwaitMount:
        """Mount a streaming assistant turn before the persistent heartbeat."""

        mounted = self._mount_transcript_message(widget)
        if mounted is None:
            raise RuntimeError("cannot mount a stream without a transcript")
        return mounted

    def _move_working_indicator_to_tail(self) -> None:
        transcript = self._transcript
        indicator = self._transcript_controller.working_indicator
        if (
            transcript is None
            or indicator is None
            or indicator.parent is not transcript
            or not transcript.children
            or transcript.children[-1] is indicator
        ):
            return
        transcript.move_child(indicator, after=transcript.children[-1])

    def history_insertion_boundary(self, history_widgets: set[Widget]) -> Widget | None:
        """Return the first live widget after the managed history window."""

        transcript = self._transcript
        if transcript is None:
            return None
        return next(
            (
                child
                for child in transcript.children
                if child is not self._history_marker
                and child not in history_widgets
                and child not in self._history_removing_widgets
            ),
            None,
        )

    def remove_historical_widget(self, widget: Widget) -> None:
        """Evict retained history without removing a card transferred to live output."""

        if self._transcript_controller.release_historical_widget(widget):
            self._history_removing_widgets.add(widget)
            removed = widget.remove()
            if self._prepending_history:
                self._history_prepend_mounts.append(removed)
            if self._history_render_depth:
                self._history_render_mounts.append(removed)
            else:
                self.call_after_refresh(self._history_removing_widgets.discard, widget)

    def historical_tool_card(self, card_id: str) -> ToolCard | None:
        """Return a mounted historical card for a page-boundary tool exchange."""

        return self._transcript_controller.historical_tool_card(card_id)

    def set_history_window_available(self, *, has_older: bool, has_newer: bool) -> None:
        """Expose retained entries beyond both mounted transcript edges."""

        if self._transcript is not None:
            self._transcript.history_window_available(
                has_older=has_older,
                has_newer=has_newer,
            )

    def begin_history_render(self) -> None:
        """Track one renderer history batch until all of its widgets mount."""

        if self._history_render_depth == 0:
            self._history_render_mounts.clear()
            batch = self.batch_update()
            batch.__enter__()
            self._history_render_batch = batch
        self._history_render_depth += 1

    def finish_history_render(self) -> None:
        """Publish the latest completed history batch's mount awaitables."""

        self._history_render_depth -= 1
        if self._history_render_depth == 0:
            self._last_history_render_mounts = tuple(self._history_render_mounts)
            self._history_render_mounts.clear()
            batch = self._history_render_batch
            self._history_render_batch = None
            if batch is not None:
                batch.__exit__(None, None, None)
            self._history_removing_widgets.clear()

    async def wait_for_history_render(self) -> None:
        """Wait until the latest renderer history batch has mounted its widgets."""

        for mounted in self._last_history_render_mounts:
            await mounted

    async def wait_for_complete_history_batch(self) -> None:
        """Wait once for an ordered fresh-mount batch instead of relayout per widget."""

        if self._last_history_render_mounts:
            await self._last_history_render_mounts[-1]

    async def wait_for_history_refresh(self) -> None:
        """Yield through the next Textual refresh so progress remains animated."""

        transcript = self._transcript
        if transcript is None or not transcript.is_running:
            return
        await transcript.wait_for_refresh()

    def history_page_loaded(self, *, has_more: bool) -> None:
        """Record pagination state after the current history batch has laid out."""

        transcript = self._transcript
        if transcript is None:
            return
        transcript.history_page_loaded(has_more=has_more)
        self._history_layout_generation += 1
        if not has_more and self._oldest_navigation_generation is None:
            return
        self.run_worker(
            self._settle_history_page_layout(
                transcript,
                self._last_history_render_mounts,
                self._history_layout_generation,
                self._transcript_epoch,
            ),
            group="history-page-layout",
            exit_on_error=False,
        )

    async def _settle_history_page_layout(
        self,
        transcript: Transcript,
        mounts: tuple[AwaitMount | AwaitRemove, ...],
        generation: int,
        epoch: int,
    ) -> None:
        for mounted in mounts:
            await mounted
        self.call_after_refresh(
            self._settle_history_page_after_refresh,
            transcript,
            generation,
            epoch,
        )

    def _settle_history_page_after_refresh(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        if (
            generation != self._history_layout_generation
            or epoch != self._transcript_epoch
            or transcript is not self._transcript
        ):
            return
        oldest_generation = self._oldest_navigation_generation
        if (
            oldest_generation is not None
            and oldest_generation == self._transcript_navigation_generation
        ):
            transcript.history_page_layout_settled()
            self._continue_oldest_navigation(oldest_generation, epoch)
            return
        # Pin the initial history batch before arming edge pagination. Under a
        # delayed Textual layout, arming first can let a pending top-edge watcher
        # request an unnecessary page before follow_tail() reaches the real end.
        transcript.follow_tail()
        transcript.history_page_layout_settled()
        self.call_after_refresh(
            self._request_history_if_still_at_top,
            transcript,
            generation,
            epoch,
        )

    def _request_history_if_still_at_top(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        if not (
            generation == self._history_layout_generation
            and epoch == self._transcript_epoch
            and transcript is self._transcript
        ):
            return
        viewport_height = transcript.scrollable_content_region.height
        if viewport_height <= 0 or any(
            _transcript_child_layout_pending(child) for child in transcript.children
        ):
            self.call_after_refresh(
                self._request_history_if_still_at_top,
                transcript,
                generation,
                epoch,
            )
            return
        # Child count remains a conservative lower bound while Textual updates
        # virtual geometry. A measured-empty Markdown child still contributes to
        # that bound even though it occupies no visible rows.
        # Never request another page merely because scroll_y has not caught up.
        if (
            len(transcript.children) > viewport_height
            or transcript.virtual_size.height > viewport_height
            or transcript.max_scroll_y > 0
        ):
            return
        transcript.request_history_at_top()

    def _continue_oldest_navigation(self, generation: int, epoch: int) -> None:
        """Advance one retained or durable step toward the session beginning."""

        transcript = self._transcript
        if (
            generation != self._oldest_navigation_generation
            or generation != self._transcript_navigation_generation
            or epoch != self._transcript_epoch
            or transcript is None
            or transcript.is_following
        ):
            return
        self._pending_history_navigation = HistoryNavigation(
            HistoryNavigationIntent.OLDEST,
            reader_generation=transcript.follow_generation,
        )
        show_oldest = self._history_window_oldest_hook
        if show_oldest is not None and show_oldest():
            self.run_worker(
                self._continue_oldest_after_mounts(
                    transcript,
                    self._last_history_render_mounts,
                    generation,
                    epoch,
                ),
                group="history-oldest-navigation",
                exit_on_error=False,
            )
            return
        transcript.scroll_home(animate=False)
        if transcript.has_more_history:
            transcript.request_history_at_top(
                HistoryNavigation(
                    HistoryNavigationIntent.OLDEST,
                    reader_generation=transcript.follow_generation,
                )
            )
            return
        self._cancel_oldest_navigation()

    async def _continue_oldest_after_mounts(
        self,
        transcript: Transcript,
        mounts: tuple[AwaitMount | AwaitRemove, ...],
        generation: int,
        epoch: int,
    ) -> None:
        for mounted in mounts:
            await mounted
        self.call_after_refresh(
            self._continue_oldest_after_refresh,
            transcript,
            generation,
            epoch,
        )

    def _continue_oldest_after_refresh(
        self,
        transcript: Transcript,
        generation: int,
        epoch: int,
    ) -> None:
        if transcript is self._transcript:
            self._continue_oldest_navigation(generation, epoch)

    def history_page_request_failed(self) -> None:
        """Stop automatic Home traversal while leaving manual history retry armed."""

        self._cancel_oldest_navigation()
        transcript = self._transcript
        if transcript is not None:
            transcript.history_page_request_failed()

    def mark_history_marker(self) -> None:
        """Keep a resumed-session label above every subsequently prepended page."""

        transcript = self._transcript
        if transcript is not None and transcript.children:
            self._history_marker = transcript.children[-1]

    def begin_history_prepend(self, *, anchor: Widget | None = None) -> None:
        """Capture the viewport before replacing a mounted history edge."""

        transcript = self._transcript
        if transcript is None or not transcript.children:
            return
        history_anchor = anchor or next(
            (child for child in transcript.children if child is not self._history_marker), None
        )
        self._prepending_history = True
        self._history_prepend_paint_suppressed = True
        self._history_prepend_mounts.clear()
        navigation = self._pending_history_navigation
        self._pending_history_navigation = HistoryNavigation()
        self._history_prepend_anchor = _HistoryPrependAnchor(
            transcript=transcript,
            widget=history_anchor,
            scroll_y=transcript.scroll_y,
            widget_y=history_anchor.virtual_region.y if history_anchor is not None else 0.0,
            following=transcript.is_following,
            reader_generation=transcript.follow_generation,
            epoch=self._transcript_epoch,
            navigation_generation=self._transcript_navigation_generation,
            navigation=navigation,
        )

    def finish_history_prepend(self) -> None:
        """Restore the reader's anchor after one older page has mounted."""

        self._prepending_history = False
        anchor = self._history_prepend_anchor
        mounts = tuple(self._history_prepend_mounts)
        self._history_prepend_mounts.clear()
        if anchor is not None:
            self.run_worker(
                self._restore_prepend_viewport_after_mounts(anchor, mounts),
                group="history-prepend",
                exit_on_error=False,
            )

    async def _restore_prepend_viewport_after_mounts(
        self,
        anchor: _HistoryPrependAnchor,
        mounts: tuple[AwaitMount | AwaitRemove, ...],
    ) -> None:
        try:
            for mounted in mounts:
                await mounted
        finally:
            self.call_after_refresh(self._restore_prepend_viewport_after_refresh, anchor)

    def _restore_prepend_viewport_after_refresh(
        self,
        anchor: _HistoryPrependAnchor,
    ) -> None:
        """Wait through the DOM-prune frame before measuring the surviving anchor."""

        self.call_after_refresh(self._restore_prepend_viewport, anchor)

    def _restore_prepend_viewport(
        self,
        anchor: _HistoryPrependAnchor,
    ) -> None:
        owns_paint_suppression = self._history_prepend_anchor is anchor
        if owns_paint_suppression:
            self._history_prepend_anchor = None
        transcript = anchor.transcript
        expected_reader_generation = (
            anchor.navigation.reader_generation
            if anchor.navigation.reader_generation >= 0
            else anchor.reader_generation
        )
        if not (
            anchor.epoch != self._transcript_epoch
            or anchor.navigation_generation != self._transcript_navigation_generation
            or transcript is not self._transcript
            or transcript.follow_generation != expected_reader_generation
        ):
            transcript.restore_prepend_viewport(
                scroll_y=anchor.scroll_y,
                anchor=anchor.widget,
                anchor_y_before=anchor.widget_y,
                following=anchor.following,
                navigation=anchor.navigation,
            )
        if owns_paint_suppression:
            # ``restore_prepend_viewport`` schedules a second layout at the corrected
            # scroll offset. Keep suppressing paints through the following compositor
            # pass too; DOM pruning may settle one refresh later under load.
            self.call_after_refresh(self._finish_history_prepend_paint_after_refresh, anchor)

    def _finish_history_prepend_paint_after_refresh(
        self,
        anchor: _HistoryPrependAnchor,
    ) -> None:
        """Wait one final compositor pass after the corrected scroll layout."""

        self.call_after_refresh(self._finish_history_prepend_paint, anchor)

    def _finish_history_prepend_paint(self, anchor: _HistoryPrependAnchor) -> None:
        """Emit the settled history replacement after its corrected layout pass."""

        if self._history_prepend_anchor is not None:
            return
        if anchor.epoch != self._transcript_epoch:
            return
        self._history_prepend_paint_suppressed = False
        # Both suppressed compositor passes consumed their dirty regions, so request
        # one complete repaint now that the viewport anchor is stable.
        self.refresh(repaint=True)

    def append_stream(self, delta: str) -> None:
        self._stream.append(delta)

    def flush_stream(self, completed_content: str | None = None) -> None:
        self._stream.flush(completed_content)

    def stream_widget_for_completed_message(self) -> Widget | None:
        """Return the completed stream widget represented by a suppressed message event."""

        return self._stream.last_completed_widget

    @property
    def last_stream_write_count(self) -> int:
        """Return Markdown writes used by the latest completed stream turn."""

        return self._stream.last_completed_write_count

    async def wait_for_stream_idle(self) -> None:
        """Wait for scheduled Markdown rendering work to finish."""

        await self._stream.wait_until_idle()

    def _follow_tail_after_refresh(self) -> None:
        # Non-streamed lines (LineMessage) mount synchronously enough that one
        # post-refresh pass reaches the settled scroll range; used by _mount_line.
        if self._transcript is not None:
            self.call_after_refresh(self._transcript.follow_tail)

    def follow_transcript_tail_after_refresh(self) -> None:
        """Settle a historical render at the tail when it began in follow mode."""

        self._follow_tail_after_refresh()

    @property
    def transcript(self) -> Transcript | None:
        """The transcript scroll view, or None before on_mount wires it.

        Public so collaborators (e.g. the StreamCoalescer) reach it through the
        app's surface rather than a private field, matching TextualTuiRenderer.
        """

        return self._transcript

    def note_transcript_update(self, widget: Widget) -> None:
        """Record a live widget update while preserving history-prepend semantics."""

        if not self._prepending_history:
            self._transcript_controller.note_update(widget)

    def record_live_transcript_update(self, widget: Widget) -> None:
        """Surface live-controller updates through the history-prepend guard."""

        self.note_transcript_update(widget)


def _historical_message_needs_markdown(message: str) -> bool:
    """Return whether Markdown can visibly differ from one literal transcript line.

    Plain one-line history stays lightweight: a retained window can contain hundreds
    of messages, and mounting a nested Markdown document for every literal sentence
    makes history paging prohibitively slow. Any multiline content or Markdown
    construct still uses the same Markdown widget as a live assistant turn.
    """

    if "\n" in message or "\r" in message or "\t" in message:
        return True
    if message.startswith("    ") or any(marker in message for marker in _MARKDOWN_VISIBLE_MARKERS):
        return True
    lowered = message.lower()
    if "http://" in lowered or "https://" in lowered or "www." in lowered:
        return True
    first, separator, _rest = message.lstrip().partition(" ")
    return bool(separator and first.rstrip(".)").isdigit())


def _transcript_child_layout_pending(child: Widget) -> bool:
    """Return whether a zero-height transcript child has not completed measurement."""

    if child.region.height > 0:
        return False
    return not isinstance(child, StreamMessage) or not child.has_measured_empty_render


def _build_file_index_snapshot(request: FileIndexRequest) -> ProjectSnapshot | None:
    """Resolve raw request inputs and collect a snapshot on the worker thread.

    Canonicalization failures fail closed: the event-loop installer replaces any
    surviving projection with ``None`` rather than exposing a stale corpus.
    """

    try:
        root = Path(request.cwd).expanduser().resolve(strict=False)
        adopted_auth_paths = tuple(
            Path(path).expanduser().resolve(strict=False).as_posix()
            for path in request.adopted_auth_paths
        )
        context = _file_index_context(root, request.protected_paths, adopted_auth_paths)
        return collect_project_snapshot(
            FileIndexConfig(
                root=root,
                context=context,
                max_entries=request.max_entries,
                max_depth=request.max_depth,
            )
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _file_index_context(
    root: Path,
    protected_paths: tuple[str, ...] | None = None,
    adopted_auth_paths: tuple[str, ...] = (),
) -> ToolContext:
    """Resolve the protected-path policy governing what the `@` picker may list.

    A bare ``ToolContext(cwd=root)`` would hardcode ``DEFAULT_PROTECTED_PATHS`` and
    silently ignore the user's real policy: a configured ``protected_paths`` entry
    (say ``secrets.yaml``) would be denied by the agent's tools but still offered
    for ``@``-mention here, and a nonstandard ``auth_path`` would lose the
    credential-file backstop that ``ToolContext.from_config`` guarantees.

    ``protected_paths`` is the parent's **already-resolved** policy and is preferred
    whenever the caller has one. Re-resolving instead would silently drop two things
    the parent alone knows: an ``--auth-file`` override (a CLI flag this process
    never sees again) and a trusted project's in-project ``auth_path`` (which
    ``WispConfig.from_env`` gates on ``trusted``). Either omission leaves the
    credential file indexable and offerable for mention while the agent's real tool
    context protects it.

    The fallback path exists for embedded callers that construct the app directly and
    have no resolved policy to hand over. There, resolving with ``trusted=False`` is
    deliberate: ``protected_paths`` is a user-scoped security setting that
    ``wisp.settings`` never reads from project-controlled config, so omitting the
    project layer cannot weaken the policy — at worst the picker hides a file the
    agent would have shown, which fails closed.

    ``adopted_auth_paths`` are credential files the session started protecting after
    startup, when a trusted project's config was applied mid-session. They union into
    whichever base policy the two branches above produced, since a deferred-trust
    approval moves the real auth file regardless of how the base was obtained.

    Config resolution touches the filesystem, so this runs on the picker's worker
    thread, never the event loop. A failure here must not take the TUI down: the
    picker is advisory, and falling back to the secure defaults keeps secrets hidden.
    """

    if protected_paths is not None:
        return ToolContext(
            cwd=root, protected_paths=_with_adopted(protected_paths, adopted_auth_paths)
        )
    try:
        config = WispConfig.from_env(project_dir=root)
        base = ToolContext.from_config(config, cwd=root)
    except Exception:
        base = ToolContext(cwd=root)
    # The adopted credential paths apply to the fallback branch too: a mid-session
    # trust approval moves the real auth file regardless of how the base policy
    # was obtained.
    if not adopted_auth_paths:
        return base
    return ToolContext(
        cwd=root, protected_paths=_with_adopted(base.protected_paths, adopted_auth_paths)
    )


def _with_adopted(base: tuple[str, ...], adopted: tuple[str, ...]) -> tuple[str, ...]:
    """Union the base policy with credential paths adopted after startup."""

    return (*base, *(pattern for pattern in adopted if pattern not in base))


def create_textual_tui(
    *,
    protected_paths: tuple[str, ...] | None = None,
    diagnostics: TuiDiagnosticsSink | None = None,
    defer_headless_terminal_write_models: bool = False,
    synchronized_output: bool = True,
) -> tuple[TextualTui, TuiRenderer]:
    """Create a Textual app and renderer pair for `TuiShell`.

    ``protected_paths`` is the caller's already-resolved policy, forwarded to the
    `@`-picker so it hides exactly what the agent's tools deny. See
    ``_file_index_context`` for why re-deriving it here would be wrong. ``diagnostics``
    is an internal, privacy-safe benchmark hook; normal product startup leaves it disabled.
    """

    app = TextualTui(
        protected_paths=protected_paths,
        diagnostics=diagnostics,
        defer_headless_terminal_write_models=defer_headless_terminal_write_models,
        synchronized_output=synchronized_output,
    )
    return app, TextualTuiRenderer(app)


__all__ = ["TextualTui", "TextualTuiRenderer", "create_textual_tui"]
