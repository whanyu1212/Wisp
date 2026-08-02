"""Coalescing buffer for a streamed assistant turn.

A streamed turn arrives token-by-token. Reconciling the Markdown widget on every
token would reparse O(n^2) and hit a mount race (``update()`` on a not-yet-mounted
widget silently drops content). This coalescer keeps the authoritative text buffer
and the live widget, and reconciles at most once per refresh via
``call_after_refresh`` — so the Markdown reparses once per frame, not per token,
and the reconcile can await the mount before following the tail.

It is Textual-coupled by nature (it needs the app's ``call_after_refresh``
scheduler, the transcript, and the working-indicator lifecycle), so it holds a
back-reference to the owning :class:`~wisp.tui.textual_app.TextualTui` rather than
being a standalone data structure.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING

from wisp.tui.widgets import StreamMessage

if TYPE_CHECKING:
    from wisp.tui.textual_app import TextualTui


class StreamCoalescer:
    """Buffers streamed tokens and reconciles the live widget once per refresh."""

    def __init__(self, app: TextualTui) -> None:
        self._app = app
        # Authoritative buffer + the live assistant widget + a coalescing flag so
        # the widget reconciles once per refresh, not once per token (avoids the
        # O(n^2) Markdown reparse and the mount race).
        self._text = ""
        self._widget: StreamMessage | None = None
        self._refresh_pending = False

    @property
    def is_streaming(self) -> bool:
        """Whether a streamed turn is mid-flight and mutating the transcript.

        The stream widget is mounted on the first token delta and cleared on
        flush, so its presence (or a non-empty buffer, which the widget lags by a
        frame) marks the window where Textual's selection bounds can go stale.
        """

        return self._widget is not None or bool(self._text)

    @property
    def live_widget(self) -> StreamMessage | None:
        """The live streaming widget, or None when no turn is mid-flight."""

        return self._widget

    @property
    def buffered_text(self) -> str:
        """The authoritative streamed-text buffer (empty once flushed)."""

        return self._text

    def append(self, delta: str) -> None:
        # Accumulate into the authoritative buffer; lazily mount the streaming
        # assistant widget on the first delta; reconcile via one coalesced
        # refresh so the Markdown reparses at most once per frame, not per token.
        self._app.hide_working_indicator()
        self._text += delta
        transcript = self._app.transcript
        if self._widget is None and transcript is not None:
            self._widget = StreamMessage()
            transcript.mount_message(self._widget)
        self._schedule_refresh()

    def flush(self) -> None:
        # Finalize the streamed turn. This is the ONLY place a streamed assistant
        # bubble is completed: the shell suppresses the trailing MessageCompleted
        # when tokens were rendered (shell.py de-dup), so it never reaches event().
        # Capture the widget + final text and reconcile AFTER refresh, because the
        # widget may have been mounted this same tick — reconciling inline would
        # hit the mount race and drop the content.
        self._app.hide_working_indicator()
        widget = self._widget
        final_text = self._text
        self._text = ""
        self._widget = None
        self._refresh_pending = False
        if widget is not None:
            self._app.call_after_refresh(self._finalize, widget, final_text)

    def discard(self) -> None:
        """Forget an in-flight stream while replacing the owning transcript."""

        self._text = ""
        self._widget = None
        self._refresh_pending = False

    async def _finalize(self, widget: StreamMessage, text: str) -> None:
        await self._follow_tail_after_content(widget, widget.set_content(text))

    def _schedule_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        # call_after_refresh runs the reconcile once the pending mount/refresh
        # settles, sidestepping the mount race (update() on a not-yet-mounted
        # widget silently drops content). Textual awaits coroutine callbacks, so
        # the reconcile can await the Markdown mount before following the tail.
        self._app.call_after_refresh(self._reconcile)

    def resume_if_deferred(self) -> None:
        """Reconcile buffered output after the reader returns to the tail."""

        if self._widget is not None and self._text:
            self._schedule_refresh()

    async def _reconcile(self) -> None:
        self._refresh_pending = False
        widget = self._widget
        transcript = self._app.transcript
        if widget is None:
            return
        if transcript is not None and not transcript.is_following:
            self._app.note_transcript_update(widget)
            return
        await self._follow_tail_after_content(widget, widget.set_content(self._text))

    async def _follow_tail_after_content(
        self,
        widget: StreamMessage,
        await_content: Awaitable[None],
    ) -> None:
        # Await the Markdown update's AwaitComplete so this update's block children
        # have mounted, THEN follow the tail — the scroll lands on the grown extent
        # instead of a partially-mounted one. This replaces guessing a fixed number
        # of refresh cycles with the update's own completion signal. The Transcript
        # still decides whether to scroll (it stays put if the user scrolled away).
        await await_content
        self._app.note_transcript_update(widget)
        transcript = self._app.transcript
        if transcript is not None:
            transcript.follow_tail()
