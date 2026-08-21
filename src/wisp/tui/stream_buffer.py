"""Paced single-widget Markdown streaming for one assistant turn at a time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from textual.widget import AwaitMount

from wisp.tui.diagnostics import (
    MarkdownDrainDiagnostic,
    TuiDiagnosticsSink,
    record_markdown_drain,
)
from wisp.tui.widgets import StreamMessage

if TYPE_CHECKING:
    from wisp.tui.textual_app import TextualTui
    from wisp.tui.widgets import Transcript, WorkingIndicator


_MIN_DRAIN_INTERVAL_SECONDS = 1 / 15
_MAX_DRAIN_INTERVAL_SECONDS = 0.25
_RENDER_COOLDOWN_MULTIPLIER = 2.0


def _next_drain_delay(render_seconds: float | None) -> float:
    """Bound stream cadence from the previous successful Markdown rebuild cost."""

    if render_seconds is None:
        return _MIN_DRAIN_INTERVAL_SECONDS
    return min(
        _MAX_DRAIN_INTERVAL_SECONDS,
        max(_MIN_DRAIN_INTERVAL_SECONDS, render_seconds * _RENDER_COOLDOWN_MULTIPLIER),
    )


@dataclass
class _StreamTurn:
    widget: StreamMessage
    mounted: AwaitMount
    working_indicator: WorkingIndicator | None = None
    working_indicator_generation: int | None = None
    working_indicator_retired: bool = False
    source_fragments: list[str] = field(default_factory=list)
    completed_content: str | None = None
    pending: list[str] = field(default_factory=list)
    pending_bytes: int = 0
    deferred: list[str] = field(default_factory=list)
    drain_scheduled: bool = False
    drain_running: bool = False
    finalize_requested: bool = False
    finalize_scheduled: bool = False
    drain_timer: asyncio.TimerHandle | None = None
    has_written: bool = False
    write_count: int = 0
    last_render_seconds: float | None = None
    discarded: bool = False
    incremental_write_failed: bool = False
    settled_callbacks: list[Callable[[], None]] = field(default_factory=list)


class MarkdownStreamController:
    """Bridge synchronous renderer calls to the async assistant message API.

    Provider fragments are retained in an amortized buffer until the turn settles.
    Each paced write replaces the renderable inside one mounted ``StreamMessage``
    and retires that stream's captured working indicator after the first visible
    frame. Finalization replaces the widget from the completed message, so an
    interrupted incremental render cannot leave a permanently partial response.
    """

    # Paced to leave headroom for the cost of the write it schedules, rather than
    # to a nominal frame rate. Appending one fragment to a mounted StreamMessage
    # and following the tail measured ~28 ms into an empty transcript and ~50 ms
    # once a few hundred messages were mounted (headless, so treat the absolute
    # numbers as indicative and the ratio as the point). A 1/30 s interval sits
    # below even the best of those, so a drain was always ready the instant the
    # previous one finished and frames landed whenever rendering happened to
    # complete — an irregular beat, which reads as jitter even though throughput
    # is fine. At 1/15 s the budget clears the measured cost in a long session,
    # so writes settle into a steady cadence.
    #
    # This bounds *repaints*, never throughput: fragments accumulate in
    # `turn.pending` between drains. A first-write burst still cuts the initial
    # wait short, while later bursts respect the cooldown established by the
    # previous Markdown update.
    _DRAIN_IMMEDIATE_BYTES = 4 * 1024

    def __init__(
        self,
        app: TextualTui,
        *,
        diagnostics: TuiDiagnosticsSink | None = None,
    ) -> None:
        self._app = app
        self._diagnostics = diagnostics
        self._turn: _StreamTurn | None = None
        # Flushed turns finalize asynchronously after `_turn` is cleared so a new
        # stream may begin. Retain their identities until completion so transcript
        # replacement can invalidate every stale callback, not just the active turn.
        self._finalizing_turns: list[_StreamTurn] = []
        self._last_completed_widget: StreamMessage | None = None
        self._last_completed_write_count = 0
        self._anchored_turn: _StreamTurn | None = None
        self._anchored_transcript: Transcript | None = None
        self._pending_callbacks = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def is_streaming(self) -> bool:
        """Whether a live turn can still mutate the transcript."""

        return self._turn is not None

    def append(self, delta: str) -> None:
        if not delta:
            return
        turn = self._turn
        transcript = self._app.transcript
        if turn is None:
            if transcript is None:
                return
            widget = StreamMessage()
            mounted = self._app.mount_stream_widget(widget)
            working_indicator = self._app.working_indicator_for_stream()
            turn = _StreamTurn(
                widget=widget,
                mounted=mounted,
                working_indicator=(working_indicator[0] if working_indicator is not None else None),
                working_indicator_generation=(
                    working_indicator[1] if working_indicator is not None else None
                ),
            )
            self._turn = turn
            self._last_completed_widget = None

        turn.source_fragments.append(delta)
        if transcript is None or not transcript.is_following:
            turn.deferred.append(delta)
            self._app.note_transcript_update(turn.widget)
            return
        turn.pending.append(delta)
        turn.pending_bytes += len(delta.encode("utf-8"))
        self._queue_drain(turn, immediate=not turn.has_written)

    def flush(self, completed_content: str | None = None) -> None:
        """Finish the active turn and reconcile it with completed provider content."""

        turn = self._turn
        self._turn = None
        if turn is not None:
            if completed_content is not None:
                turn.completed_content = completed_content
            # History reconciliation runs synchronously after flush(), before the
            # async finalizer, so publish the stable widget identity immediately.
            self._last_completed_widget = turn.widget
            turn.finalize_requested = True
            self._finalizing_turns.append(turn)
            if not self._cancel_drain(turn):
                self._queue_finalize(turn)

    def defer_until_latest_stream_settles(self, callback: Callable[[], None]) -> bool:
        """Run ``callback`` after the newest flushed stream finishes final layout."""

        if not self._finalizing_turns:
            return False
        self._finalizing_turns[-1].settled_callbacks.append(callback)
        return True

    def discard(self) -> None:
        """Stop a replaced transcript without rendering stale output."""

        turn = self._turn
        self._turn = None
        if turn is not None:
            turn.discarded = True
            turn.finalize_requested = False
            self._cancel_drain(turn)
        for finalizing in self._finalizing_turns:
            finalizing.discarded = True
            self._run_settled_callbacks(finalizing)
        anchored_turn = self._anchored_turn
        if anchored_turn is not None:
            self._release_stream_anchor(anchored_turn)
        self._last_completed_widget = None

    def resume_if_deferred(self) -> None:
        """Render buffered output when the reader returns to the transcript tail."""

        turn = self._turn
        if turn is None or not turn.deferred:
            return
        turn.pending.extend(turn.deferred)
        turn.pending_bytes += sum(len(delta.encode("utf-8")) for delta in turn.deferred)
        turn.deferred.clear()
        self._queue_drain(turn)

    @property
    def last_completed_widget(self) -> StreamMessage | None:
        """Return the latest completed stream widget for durable-history reconciliation."""

        return self._last_completed_widget

    @property
    def last_completed_write_count(self) -> int:
        """Return Markdown writes used by the most recently completed turn."""

        return self._last_completed_write_count

    async def shutdown(self) -> None:
        """Drain a live stream before Textual begins tearing down its widgets."""

        self.flush()
        await self.wait_until_idle()

    async def wait_until_idle(self) -> None:
        """Wait for scheduled writes and final reconciliation to complete."""

        await self._idle.wait()

    def _queue_drain(self, turn: _StreamTurn, *, immediate: bool = False) -> None:
        if turn.discarded or turn.drain_scheduled:
            return
        turn.drain_scheduled = True
        self._begin_callback()
        burst_before_first_write = (
            turn.last_render_seconds is None and turn.pending_bytes >= self._DRAIN_IMMEDIATE_BYTES
        )
        if immediate or burst_before_first_write:
            if not self._app.call_after_refresh(self._drain, turn):
                turn.drain_scheduled = False
                self._finish_callback()
            return
        turn.drain_timer = asyncio.get_running_loop().call_later(
            _next_drain_delay(turn.last_render_seconds),
            self._schedule_drain_after_frame,
            turn,
        )

    def _schedule_drain_after_frame(self, turn: _StreamTurn) -> None:
        turn.drain_timer = None
        if not turn.discarded and turn.drain_scheduled:
            if not self._app.call_after_refresh(self._drain, turn):
                turn.drain_scheduled = False
                self._finish_callback()

    def _cancel_drain(self, turn: _StreamTurn) -> bool:
        if turn.drain_running:
            return True
        timer = turn.drain_timer
        if timer is not None:
            timer.cancel()
            turn.drain_timer = None
        if turn.drain_scheduled:
            turn.drain_scheduled = False
            self._finish_callback()
        return False

    def _queue_finalize(self, turn: _StreamTurn) -> None:
        # Preserve flush order even when an earlier turn was still draining while a
        # later one completed. Settled-widget retention relies on chronological order.
        if self._finalizing_turns and self._finalizing_turns[0] is not turn:
            return
        if turn.discarded:
            self._forget_finalizing_turn(turn)
            self._queue_next_finalizer()
            return
        if turn.finalize_scheduled:
            return
        turn.finalize_scheduled = True
        self._begin_callback()
        if not self._app.call_after_refresh(self._finalize, turn):
            turn.finalize_scheduled = False
            self._forget_finalizing_turn(turn)
            self._queue_next_finalizer()
            self._finish_callback()

    def _forget_finalizing_turn(self, turn: _StreamTurn) -> None:
        self._finalizing_turns = [
            candidate for candidate in self._finalizing_turns if candidate is not turn
        ]

    @staticmethod
    def _run_settled_callbacks(turn: _StreamTurn) -> None:
        callbacks = tuple(turn.settled_callbacks)
        turn.settled_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception as error:
                turn.widget.log.error(f"Stream settlement callback failed: {error}")

    def _queue_next_finalizer(self) -> None:
        while self._finalizing_turns and self._finalizing_turns[0].discarded:
            self._forget_finalizing_turn(self._finalizing_turns[0])
        if self._finalizing_turns:
            self._queue_finalize(self._finalizing_turns[0])

    def _begin_callback(self) -> None:
        self._pending_callbacks += 1
        self._idle.clear()

    def _finish_callback(self) -> None:
        self._pending_callbacks -= 1
        if self._pending_callbacks == 0:
            self._idle.set()

    @staticmethod
    async def _ensure_mounted(turn: _StreamTurn) -> None:
        """Wait for first attachment without re-triggering the parent's layout."""

        if turn.widget.is_mounted:
            return
        # Textual's AwaitMount refreshes its parent with layout=True after every
        # await, even once its mounted event is already set. Guarding on the widget
        # state avoids a redundant full transcript repaint during finalization.
        await turn.mounted

    async def _drain(self, turn: _StreamTurn) -> None:
        if not turn.drain_scheduled:
            return
        turn.drain_running = True
        try:
            if turn.discarded:
                return
            text = "".join(turn.pending)
            turn.pending.clear()
            turn.pending_bytes = 0
            if not text:
                return
            transcript = self._app.transcript
            if transcript is None or not transcript.is_following:
                turn.deferred.append(text)
                self._app.note_transcript_update(turn.widget)
                return
            self._anchor_stream_tail(turn, transcript)
            render_started: float | None = None
            render_seconds: float | None = None
            succeeded = False
            try:
                # The first provider fragment can arrive in the same event-loop
                # tick as the StreamMessage mount. Wait until its app/theme context
                # exists before building the Rich Markdown renderable.
                await self._ensure_mounted(turn)
                render_started = time.perf_counter()
                await turn.widget.append_markdown(text)
                render_seconds = time.perf_counter() - render_started
                succeeded = True
            except Exception as error:
                if render_started is not None:
                    render_seconds = time.perf_counter() - render_started
                # Keep the authoritative full source and repair the widget during
                # finalization instead of allowing one incremental parser/layout
                # failure to terminate the app or strand all later fragments.
                if not turn.incremental_write_failed:
                    turn.widget.log.error(f"Incremental Markdown update failed: {error}")
                turn.incremental_write_failed = True
            else:
                if turn.discarded:
                    return
                turn.has_written = True
                turn.write_count += 1
                turn.last_render_seconds = render_seconds
                self._retire_working_indicator(turn)
                self._app.note_transcript_update(turn.widget)
            if render_seconds is not None and self._diagnostics is not None:
                record_markdown_drain(
                    self._diagnostics,
                    MarkdownDrainDiagnostic(
                        render_seconds=render_seconds,
                        appended_chars=len(text),
                        appended_bytes=len(text.encode("utf-8")),
                        resulting_source_chars=len(turn.widget.source),
                        processed_source_chars=(
                            turn.widget.last_markdown_processed_chars if succeeded else 0
                        ),
                        reused_source_chars=(
                            turn.widget.last_markdown_reused_chars if succeeded else 0
                        ),
                        incremental=(turn.widget.last_markdown_incremental if succeeded else False),
                        succeeded=succeeded,
                    ),
                )
        finally:
            turn.drain_running = False
            turn.drain_scheduled = False
            # Schedule successor work before releasing this callback's count so
            # wait_until_idle() cannot observe a transient idle state between stages.
            if turn.finalize_requested:
                self._queue_finalize(turn)
            elif turn.pending and not turn.discarded:
                self._queue_drain(turn)
            self._finish_callback()

    async def _finalize(self, turn: _StreamTurn) -> None:
        try:
            if turn.discarded:
                return
            turn.pending.clear()
            turn.pending_bytes = 0
            turn.deferred.clear()
            # Always reconcile from authoritative source. Besides repairing failed
            # incremental writes, this replaces provider deltas with the exact
            # MessageCompleted content when providers normalize their final text.
            streamed_source = "".join(turn.source_fragments)
            # Some providers stream the full response but leave the terminal
            # completion payload empty. Do not let that erase visible output.
            source = turn.completed_content or streamed_source
            await self._ensure_mounted(turn)
            if turn.discarded:
                return
            transcript = self._app.transcript
            if transcript is not None and transcript.is_following:
                self._anchor_stream_tail(turn, transcript)
            if turn.incremental_write_failed or turn.widget.needs_reconciliation(source):
                await turn.widget.replace_markdown(source)
                if turn.discarded:
                    return
                turn.write_count += 1
            if source:
                # A failed or cancelled incremental write may reach completion before
                # any visible drain. Retire the heartbeat once final reconciliation
                # has made the authoritative response visible.
                self._retire_working_indicator(turn)
            self._last_completed_write_count = turn.write_count
            turn.widget.release_streaming_markdown_caches()
            self._app.settle_stream_widget(turn.widget)
            self._app.note_transcript_update(turn.widget)
        finally:
            self._run_settled_callbacks(turn)
            if self._anchored_turn is turn and not self._app.call_after_refresh(
                self._release_stream_anchor,
                turn,
            ):
                self._release_stream_anchor(turn)
            self._forget_finalizing_turn(turn)
            turn.finalize_scheduled = False
            # Keep the idle barrier closed while handing off to the next flushed turn.
            self._queue_next_finalizer()
            self._finish_callback()

    def _retire_working_indicator(self, turn: _StreamTurn) -> None:
        """Remove this stream's heartbeat after its first visible response frame."""

        if turn.working_indicator_retired:
            return
        turn.working_indicator_retired = True
        indicator = turn.working_indicator
        generation = turn.working_indicator_generation
        turn.working_indicator = None
        turn.working_indicator_generation = None
        if indicator is not None and generation is not None:
            self._app.hide_working_indicator_if_current(
                indicator,
                generation=generation,
            )

    def _anchor_stream_tail(self, turn: _StreamTurn, transcript: Transcript) -> None:
        """Pin one followed stream inside the compositor pass that grows it."""

        if (
            self._anchored_turn is turn
            and self._anchored_transcript is transcript
            and transcript.is_anchored
        ):
            # Already anchored to this turn from an earlier drain of the same
            # paced stream -- re-arming would re-run Textual's anchor(True),
            # which stops any in-flight scroll animation and re-derives
            # scroll_target_y, on every one of the ~4-15 drains/sec a single
            # streaming turn produces. The compositor's own anchored-arrange
            # pass is what actually keeps the view pinned as content grows.
            #
            # transcript.is_anchored must be checked too, not just our own
            # bookkeeping: something outside this controller (e.g. card-focus
            # handling in textual_app.py) can call transcript.anchor(False)
            # directly, disarming the transcript without going through
            # _release_stream_anchor. Our _anchored_turn/_anchored_transcript
            # would still (correctly) name this turn as the one we intend to
            # keep following, but the transcript's live anchor is off, so it
            # must be re-armed on the next drain rather than skipped.
            return
        transcript.anchor()
        self._anchored_turn = turn
        self._anchored_transcript = transcript

    def _release_stream_anchor(self, turn: _StreamTurn) -> None:
        """Disable native anchoring after the owning stream's final layout."""

        if self._anchored_turn is not turn:
            return
        transcript = self._anchored_transcript
        self._anchored_turn = None
        self._anchored_transcript = None
        if transcript is not None:
            transcript.anchor(False)
