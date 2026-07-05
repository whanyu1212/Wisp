"""Live prompt-toolkit fullscreen TUI adapter."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from wisp.tui.rendering import FullscreenTuiRenderer, TuiTranscriptEntry, TuiViewSnapshot


class LiveFullscreenInputInterrupted(Exception):
    """Raised by the live input adapter for Ctrl-C interrupts."""


class LiveFullscreenTui(FullscreenTuiRenderer):
    """Prompt-toolkit fullscreen renderer/input adapter.

    This is intentionally an MVP: it owns the terminal input line and keeps the
    existing RPC/TUI controller flow intact. The renderer state remains the same
    `FullscreenTuiRenderer` state used by non-live fallback rendering.
    """

    def __init__(self, *, run_application: bool = True) -> None:
        super().__init__(clear_screen=False)
        self.run_application = run_application
        self._buffer = Buffer(multiline=False)
        self._input_future: asyncio.Future[str] | None = None
        self._application: Application[None] | None = None
        self._application_task: asyncio.Task[None] | None = None
        self._visible_input_mode = "idle"
        self._buffer_input_mode = "idle"
        self._submitted_input_mode: str | None = None
        self._last_buffer_text = ""
        self._buffer.on_text_changed += self._handle_buffer_text_changed
        self._key_bindings = self._build_key_bindings()

    async def read_prompt(self, prompt: str) -> str:
        """Read one line from the live fullscreen input area."""

        if self._input_future is not None and not self._input_future.done():
            raise RuntimeError("live fullscreen input read already in progress")
        loop = asyncio.get_running_loop()
        self._input_future = loop.create_future()
        self.state.input_hint = prompt
        self._visible_input_mode = self.state.input_mode
        self._buffer_input_mode = self._visible_input_mode
        self._submitted_input_mode = None
        self._last_buffer_text = ""
        self._buffer.reset()
        self._refresh()
        if self.run_application:
            self._ensure_application_started()
        return await self._input_future

    async def close(self) -> None:
        """Close the live fullscreen app if it is running."""

        if self._input_future is not None and not self._input_future.done():
            self._input_future.set_exception(EOFError())
        if self._application is not None and not self._application.is_done:
            with suppress(Exception):
                self._application.exit()
        if self._application_task is not None:
            try:
                await asyncio.wait_for(self._application_task, timeout=1)
            except TimeoutError:
                self._application_task.cancel()
                with suppress(asyncio.CancelledError, EOFError):
                    await self._application_task
            except (asyncio.CancelledError, EOFError):
                pass

    def token_delta(self, delta: str) -> None:
        """Append streamed text and refresh the live screen immediately."""

        super().token_delta(delta)
        self._refresh()

    def view_updated(self, snapshot: TuiViewSnapshot) -> None:
        """Apply a shell view snapshot and keep live input tags in sync."""

        super().view_updated(snapshot)
        self._visible_input_mode = snapshot.input_mode
        if not self._buffer.text:
            self._buffer_input_mode = self._visible_input_mode

    def consume_submitted_input_mode(self, fallback: str) -> str:
        """Return and clear the mode captured when the current line was accepted."""

        mode = self._submitted_input_mode or fallback
        self._submitted_input_mode = None
        return mode

    def _refresh(self) -> None:
        if self._application is not None and not self._application.is_done:
            self._application.invalidate()

    def _ensure_application_started(self) -> None:
        if self._application is None:
            self._application = self._build_application()
        if self._application_task is None or self._application_task.done():
            self._application_task = asyncio.create_task(self._application.run_async())
            self._application_task.add_done_callback(self._handle_application_done)

    def _handle_application_done(self, task: asyncio.Task[None]) -> None:
        if self._input_future is not None and not self._input_future.done():
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                exc = EOFError()
            if exc is not None:
                self._input_future.set_exception(exc)
            else:
                self._input_future.set_exception(EOFError())

    def _build_application(self) -> Application[None]:
        input_control = BufferControl(buffer=self._buffer)
        root = HSplit(
            [
                Frame(
                    Window(
                        FormattedTextControl(self._header_fragments),
                        height=1,
                    ),
                    title="Wisp",
                ),
                Frame(
                    Window(
                        FormattedTextControl(self._transcript_fragments),
                        wrap_lines=True,
                    ),
                    title="Transcript",
                ),
                VSplit(
                    [
                        Frame(
                            Window(
                                FormattedTextControl(self._status_fragments),
                                width=32,
                            ),
                            title="Status",
                        ),
                        Frame(
                            VSplit(
                                [
                                    Window(
                                        FormattedTextControl(self._input_prompt_fragments),
                                        dont_extend_width=True,
                                    ),
                                    Window(input_control),
                                ]
                            ),
                            title="Input",
                        ),
                    ],
                    height=5,
                ),
            ]
        )
        return Application(
            layout=Layout(root, focused_element=input_control),
            key_bindings=self._key_bindings,
            full_screen=True,
            mouse_support=False,
            style=Style.from_dict(
                {
                    "header": "bold cyan",
                    "status": "bold",
                    "dim": "ansibrightblack",
                    "error": "ansired",
                    "approval": "ansiyellow",
                    "assistant": "ansigreen",
                    "tool": "ansiblue",
                    "user": "bold",
                }
            ),
        )

    def _build_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def _accept(event: KeyPressEvent) -> None:
            self._accept_input()
            event.app.invalidate()

        @bindings.add("c-c")
        def _interrupt(event: KeyPressEvent) -> None:
            self._interrupt_input()
            event.app.invalidate()

        @bindings.add("c-d")
        def _eof(event: KeyPressEvent) -> None:
            if self._buffer.text:
                self._buffer.delete()
                event.app.invalidate()
                return
            self._close_input()
            event.app.invalidate()

        return bindings

    def _accept_input(self) -> None:
        if self._input_future is None or self._input_future.done():
            return
        text = self._buffer.text
        self._submitted_input_mode = self._buffer_input_mode
        self._buffer.reset()
        self._input_future.set_result(text)

    def _interrupt_input(self) -> None:
        if self._input_future is None or self._input_future.done():
            return
        self._submitted_input_mode = self._buffer_input_mode
        self._buffer.reset()
        self._input_future.set_exception(LiveFullscreenInputInterrupted())

    def _close_input(self) -> None:
        if self._input_future is None or self._input_future.done():
            return
        self._submitted_input_mode = self._buffer_input_mode
        self._buffer.reset()
        self._input_future.set_exception(EOFError())

    def _handle_buffer_text_changed(self, _buffer: Buffer) -> None:
        text = self._buffer.text
        if not text or not self._last_buffer_text:
            self._buffer_input_mode = self._visible_input_mode
        self._last_buffer_text = text

    def _header_fragments(self) -> StyleAndTextTuples:
        return [("class:header", "Wisp · RPC-backed TUI")]

    def _transcript_fragments(self) -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        if not self.state.transcript and not self.state.streaming_text:
            return [("class:dim", "No messages yet.")]
        for entry in self.state.transcript:
            self._append_entry_fragments(fragments, entry)
        if self.state.streaming_text:
            self._append_entry_fragments(
                fragments,
                TuiTranscriptEntry("assistant", self.state.streaming_text, "assistant"),
            )
        return fragments

    def _append_entry_fragments(
        self,
        fragments: StyleAndTextTuples,
        entry: TuiTranscriptEntry,
    ) -> None:
        if fragments:
            fragments.append(("", "\n"))
        style = _prompt_toolkit_style(entry.style)
        label_style = f"class:{style} bold" if style else "bold"
        content_style = f"class:{style}" if style else ""
        fragments.append((label_style, f"{entry.role}: "))
        fragments.append((content_style, entry.content))

    def _status_fragments(self) -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = [("class:status", self.state.status)]
        if self.state.queued_follow_ups:
            fragments.append(("class:dim", f"\nqueued follow-ups: {self.state.queued_follow_ups}"))
        if self.state.last_session:
            fragments.append(("class:dim", f"\nsession: {self.state.last_session}"))
        return fragments

    def _input_prompt_fragments(self) -> StyleAndTextTuples:
        return [("class:status", self.state.input_hint)]


def _prompt_toolkit_style(style: str) -> str:
    if style in {"red", "error"}:
        return "error"
    if style in {"yellow", "approval"}:
        return "approval"
    if style in {"green", "assistant"}:
        return "assistant"
    if style in {"blue", "tool"}:
        return "tool"
    if style in {"bold", "user"}:
        return "user"
    if style == "dim":
        return "dim"
    return ""
