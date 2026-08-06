"""Native Textual Markdown streaming for one assistant turn at a time."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from textual.widget import AwaitMount

from wisp.tui.widgets import StreamMessage

if TYPE_CHECKING:
    from wisp.tui.textual_app import TextualTui


@dataclass
class _StreamTurn:
    widget: StreamMessage
    mounted: AwaitMount
    source: str = ""
    pending: list[str] = field(default_factory=list)
    pending_bytes: int = 0
    deferred: list[str] = field(default_factory=list)
    drain_scheduled: bool = False
    drain_running: bool = False
    finalize_requested: bool = False
    finalize_scheduled: bool = False
    follow_scheduled: bool = False
    follow_generation: int | None = None
    drain_timer: asyncio.TimerHandle | None = None
    has_written: bool = False
    write_count: int = 0
    discarded: bool = False
    incremental_write_failed: bool = False


class MarkdownStreamController:
    """Bridge synchronous renderer calls to Textual's async Markdown API.

    Provider fragments are retained in ``source`` until the turn settles. Textual's
    public ``Markdown.append`` API is awaited directly, avoiding the private
    ``MarkdownStream`` background queue. Finalization replaces the document from
    the completed message, so an interrupted incremental render cannot leave a
    permanently partial response.
    """

    _DRAIN_INTERVAL_SECONDS = 1 / 30
    _DRAIN_IMMEDIATE_BYTES = 4 * 1024

    def __init__(self, app: TextualTui) -> None:
        self._app = app
        self._turn: _StreamTurn | None = None
        self._last_completed_widget: StreamMessage | None = None
        self._last_completed_write_count = 0
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
        self._app.hide_working_indicator()
        turn = self._turn
        transcript = self._app.transcript
        if turn is None:
            if transcript is None:
                return
            widget = StreamMessage()
            mounted = transcript.mount_message(widget)
            turn = _StreamTurn(widget=widget, mounted=mounted)
            if transcript.is_following:
                turn.follow_generation = transcript.follow_generation
            self._turn = turn
            self._last_completed_widget = None

        turn.source += delta
        if transcript is None or not transcript.is_following:
            turn.deferred.append(delta)
            self._app.note_transcript_update(turn.widget)
            return
        turn.pending.append(delta)
        turn.pending_bytes += len(delta.encode("utf-8"))
        self._queue_drain(turn, immediate=not turn.has_written)

    def flush(self, completed_content: str | None = None) -> None:
        """Finish the active turn and reconcile it with completed provider content."""

        self._app.hide_working_indicator()
        turn = self._turn
        self._turn = None
        if turn is not None:
            if completed_content is not None:
                turn.source = completed_content
            # History reconciliation runs synchronously after flush(), before the
            # async finalizer, so publish the stable widget identity immediately.
            self._last_completed_widget = turn.widget
            turn.finalize_requested = True
            if not self._cancel_drain(turn):
                self._queue_finalize(turn)

    def discard(self) -> None:
        """Stop a replaced transcript without rendering stale output."""

        turn = self._turn
        self._turn = None
        if turn is not None:
            turn.discarded = True
            turn.finalize_requested = True
            if not self._cancel_drain(turn):
                self._queue_finalize(turn)

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
        if immediate or turn.pending_bytes >= self._DRAIN_IMMEDIATE_BYTES:
            self._app.call_after_refresh(self._drain, turn)
            return
        turn.drain_timer = asyncio.get_running_loop().call_later(
            self._DRAIN_INTERVAL_SECONDS,
            self._schedule_drain_after_frame,
            turn,
        )

    def _schedule_drain_after_frame(self, turn: _StreamTurn) -> None:
        turn.drain_timer = None
        if not turn.discarded and turn.drain_scheduled:
            self._app.call_after_refresh(self._drain, turn)

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
        if turn.finalize_scheduled:
            return
        turn.finalize_scheduled = True
        self._begin_callback()
        self._app.call_after_refresh(self._finalize, turn)

    def _begin_callback(self) -> None:
        self._pending_callbacks += 1
        self._idle.clear()

    def _finish_callback(self) -> None:
        self._pending_callbacks -= 1
        if self._pending_callbacks == 0:
            self._idle.set()

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
            turn.follow_generation = transcript.follow_generation
            try:
                # The first provider fragment can arrive in the same event-loop
                # tick as the StreamMessage mount. Wait for compose() to mount its
                # Markdown child before calling the public append API.
                await turn.mounted
                await turn.widget.append_markdown(text)
            except Exception:
                # Keep the authoritative full source and repair the widget during
                # finalization instead of allowing one incremental parser/layout
                # failure to terminate the app or strand all later fragments.
                turn.incremental_write_failed = True
            else:
                turn.has_written = True
                turn.write_count += 1
                self._app.note_transcript_update(turn.widget)
                self._queue_follow_tail(turn)
        finally:
            turn.drain_running = False
            turn.drain_scheduled = False
            self._finish_callback()
            if turn.finalize_requested:
                self._queue_finalize(turn)
            elif turn.pending and not turn.discarded:
                self._queue_drain(turn)

    def _queue_follow_tail(self, turn: _StreamTurn) -> None:
        if turn.follow_scheduled:
            return
        turn.follow_scheduled = True
        self._app.call_after_refresh(self._follow_tail, turn)

    def _follow_tail(self, turn: _StreamTurn) -> None:
        turn.follow_scheduled = False
        if turn.discarded:
            return
        transcript = self._app.transcript
        if transcript is not None and turn.follow_generation == transcript.follow_generation:
            transcript.return_to_latest()

    async def _finalize(self, turn: _StreamTurn) -> None:
        try:
            if turn.discarded:
                return
            turn.pending.clear()
            turn.pending_bytes = 0
            turn.deferred.clear()
            transcript = self._app.transcript
            if transcript is not None and transcript.is_following:
                turn.follow_generation = transcript.follow_generation

            # Always reconcile from authoritative source. Besides repairing failed
            # incremental writes, this replaces provider deltas with the exact
            # MessageCompleted content when providers normalize their final text.
            await turn.mounted
            await turn.widget.replace_markdown(turn.source)
            turn.write_count += 1
            self._last_completed_write_count = turn.write_count
            self._app.settle_stream_widget(turn.widget)
            self._app.note_transcript_update(turn.widget)
            self._queue_follow_tail(turn)
        finally:
            turn.finalize_scheduled = False
            self._finish_callback()
