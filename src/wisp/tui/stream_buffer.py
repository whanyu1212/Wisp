"""Native Textual Markdown streaming for one assistant turn at a time."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from textual.widgets._markdown import MarkdownStream

from wisp.tui.widgets import StreamMessage

if TYPE_CHECKING:
    from wisp.tui.textual_app import TextualTui


@dataclass
class _StreamTurn:
    widget: StreamMessage
    stream: MarkdownStream
    deferred: list[str] = field(default_factory=list)
    discarded: bool = False


class MarkdownStreamController:
    """Bridge synchronous renderer calls to Textual's async MarkdownStream API."""

    def __init__(self, app: TextualTui) -> None:
        self._app = app
        self._turn: _StreamTurn | None = None
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
            transcript.mount_message(widget)
            turn = _StreamTurn(widget=widget, stream=widget.get_stream())
            self._turn = turn

        if transcript is None or not transcript.is_following:
            turn.deferred.append(delta)
            self._app.note_transcript_update(turn.widget)
            return
        self._schedule(self._write, turn, delta)

    def flush(self) -> None:
        """Finish the active turn without blocking the synchronous renderer."""

        self._app.hide_working_indicator()
        turn = self._turn
        self._turn = None
        if turn is not None:
            self._schedule(self._finalize, turn)

    def discard(self) -> None:
        """Stop a replaced transcript's native stream without rendering stale output."""

        turn = self._turn
        self._turn = None
        if turn is not None:
            turn.discarded = True
            self._schedule(self._stop, turn)

    def resume_if_deferred(self) -> None:
        """Render buffered output when the reader returns to the transcript tail."""

        turn = self._turn
        if turn is None or not turn.deferred:
            return
        deferred = "".join(turn.deferred)
        turn.deferred.clear()
        self._schedule(self._write, turn, deferred)

    async def shutdown(self) -> None:
        """Drain a live stream before Textual begins tearing down its widgets."""

        self.flush()
        await self.wait_until_idle()

    async def wait_until_idle(self) -> None:
        """Wait for scheduled writes and native-stream finalization to complete."""

        await self._idle.wait()

    def _schedule(self, callback: object, *args: object) -> None:
        self._pending_callbacks += 1
        self._idle.clear()
        # Markdown's mount initialization can overwrite an early append. Scheduling
        # after refresh keeps the native stream incremental while closing that race.
        self._app.call_after_refresh(self._run_scheduled, callback, *args)

    async def _run_scheduled(self, callback: object, *args: object) -> None:
        try:
            assert callable(callback)
            result = callback(*args)
            assert hasattr(result, "__await__")
            await result
        finally:
            self._pending_callbacks -= 1
            if self._pending_callbacks == 0:
                self._idle.set()

    async def _write(self, turn: _StreamTurn, text: str) -> None:
        if turn.discarded:
            return
        await turn.stream.write(text)
        self._app.note_transcript_update(turn.widget)
        transcript = self._app.transcript
        if transcript is not None and transcript.is_following:
            self._app.call_after_refresh(transcript.follow_tail)

    async def _finalize(self, turn: _StreamTurn) -> None:
        if not turn.discarded and turn.deferred:
            await turn.stream.write("".join(turn.deferred))
            turn.deferred.clear()
        await self._stop(turn)
        if not turn.discarded:
            self._app.note_transcript_update(turn.widget)
            transcript = self._app.transcript
            if transcript is not None and transcript.is_following:
                self._app.call_after_refresh(transcript.follow_tail)

    async def _stop(self, turn: _StreamTurn) -> None:
        await turn.stream.stop()
