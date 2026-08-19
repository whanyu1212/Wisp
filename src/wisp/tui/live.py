"""Live prompt-toolkit fullscreen TUI adapter."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.clipboard.base import Clipboard, ClipboardData
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from wisp.tui.input_types import PendingSubmissionView, TuiSubmission, new_submission_id
from wisp.tui.rendering import (
    FullscreenTuiRenderer,
    TuiViewSnapshot,
    _RenderedTranscriptLine,
    format_tui_footer_lines,
)
from wisp.tui.state import TuiQuitRequested

_HEADER_FRAME_HEIGHT = 3
_FOOTER_HEIGHT = 5
_TRANSCRIPT_FRAME_BORDER_HEIGHT = 2
_TRANSCRIPT_FRAME_BORDER_WIDTH = 2


class LiveFullscreenInputInterrupted(Exception):
    """Raised by the live input adapter for Escape cancellation."""


class _Osc52Clipboard(InMemoryClipboard):
    """Keep copied text locally and publish it through terminal OSC 52."""

    def __init__(
        self,
        *,
        write_raw: Callable[[str], None],
        flush: Callable[[], None],
    ) -> None:
        super().__init__()
        self._write_raw = write_raw
        self._flush = flush

    def set_data(self, data: ClipboardData) -> None:
        super().set_data(data)
        encoded = base64.b64encode(data.text.encode("utf-8")).decode("ascii")
        self._write_raw(f"\x1b]52;c;{encoded}\x07")
        self._flush()


class LiveFullscreenTui(FullscreenTuiRenderer):
    """Prompt-toolkit fullscreen renderer/input adapter.

    This is intentionally an MVP: it owns the terminal input line and keeps the
    existing RPC/TUI controller flow intact. The renderer state remains the same
    `FullscreenTuiRenderer` state used by non-live fallback rendering.
    """

    def __init__(self, *, run_application: bool = True) -> None:
        super().__init__(clear_screen=False)
        self.run_application = run_application
        self._buffer = Buffer(multiline=True)
        self._input_future: asyncio.Future[str | TuiSubmission] | None = None
        self._application: Application[None] | None = None
        self._application_task: asyncio.Task[None] | None = None
        self._visible_input_mode = "idle"
        self._buffer_input_mode = "idle"
        self._submitted_input_mode: str | None = None
        self._queued_inputs: deque[tuple[str | TuiSubmission | BaseException, str]] = deque()
        self._buffered_submissions: dict[int, TuiSubmission] = {}
        self._shell_pending_submissions: tuple[PendingSubmissionView, ...] = ()
        self._last_buffer_text = ""
        self._buffer.on_text_changed += self._handle_buffer_text_changed
        self._key_bindings = self._build_key_bindings()

    async def read_prompt(self, prompt: str) -> str | TuiSubmission:
        """Read one line from the live fullscreen input area."""

        if self._input_future is not None and not self._input_future.done():
            raise RuntimeError("live fullscreen input read already in progress")
        self.state.input_hint = prompt
        self._visible_input_mode = self.state.input_mode
        self._submitted_input_mode = None
        if not self._buffer.text:
            self._clear_buffer()
        self._refresh()
        if self.run_application:
            self._ensure_application_started()
        if self._queued_inputs:
            value, mode = self._queued_inputs.popleft()
            self._submitted_input_mode = mode
            if isinstance(value, BaseException):
                raise value
            return value
        loop = asyncio.get_running_loop()
        self._input_future = loop.create_future()
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

        self._shell_pending_submissions = snapshot.pending_submissions
        shell_ids = {int(submission.id) for submission in snapshot.pending_submissions}
        provisional = tuple(
            submission.pending_view()
            for submission_id, submission in self._buffered_submissions.items()
            if submission_id not in shell_ids
        )
        super().view_updated(
            replace(
                snapshot,
                pending_submissions=snapshot.pending_submissions + provisional,
            )
        )
        self._visible_input_mode = snapshot.input_mode
        if not self._buffer.text:
            self._buffer_input_mode = self._visible_input_mode

    def consume_submitted_input_mode(self, fallback: str) -> str:
        """Return and clear the mode captured when the current line was accepted."""

        mode = self._submitted_input_mode or fallback
        self._submitted_input_mode = None
        return mode

    def resolve_submission(self, submission_id: int) -> None:
        self._buffered_submissions.pop(submission_id, None)
        self.state.pending_submissions = tuple(
            submission
            for submission in self.state.pending_submissions
            if int(submission.id) != submission_id
        )
        self._refresh()

    def restore_submissions(self, submissions: tuple[TuiSubmission, ...]) -> bool:
        """Restore unstarted prompts before any newer live-editor draft."""

        restored = [submission.content for submission in submissions if submission.content]
        if self._buffer.text:
            restored.append(self._buffer.text)
        self._clear_buffer()
        if restored:
            self._buffer.insert_text("\n".join(restored))
        restored_ids = {int(submission.id) for submission in submissions}
        for submission_id in restored_ids:
            self._buffered_submissions.pop(submission_id, None)
        self.state.pending_submissions = tuple(
            submission
            for submission in self.state.pending_submissions
            if int(submission.id) not in restored_ids
        )
        self._refresh()
        return True

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
        output = create_output()
        clipboard = _Osc52Clipboard(write_raw=output.write_raw, flush=output.flush)
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
                    title="Editor",
                    height=3,
                ),
                Window(
                    FormattedTextControl(self._footer_fragments),
                    height=2,
                ),
            ]
        )
        return Application(
            layout=Layout(root, focused_element=input_control),
            key_bindings=self._key_bindings,
            clipboard=clipboard,
            output=output,
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

        @bindings.add("c-j")
        def _newline(event: KeyPressEvent) -> None:
            self._insert_newline()
            event.app.invalidate()

        @bindings.add("escape")
        def _cancel(event: KeyPressEvent) -> None:
            self._interrupt_input()
            event.app.invalidate()

        @bindings.add("c-c")
        def _quit(event: KeyPressEvent) -> None:
            if not self._copy_selection(event.app.clipboard):
                self._quit_input()
            event.app.invalidate()

        @bindings.add("c-d")
        def _eof(event: KeyPressEvent) -> None:
            self._delete_right_or_close()
            event.app.invalidate()

        @bindings.add(Keys.BackTab)
        def _toggle_agent_mode(event: KeyPressEvent) -> None:
            command = "/build" if self.state.mode == "plan" else "/plan"
            self._submit_synthetic_input(command)
            event.app.invalidate()

        @bindings.add(Keys.PageUp)
        def _page_up(event: KeyPressEvent) -> None:
            self.scroll_transcript_up()
            event.app.invalidate()

        @bindings.add(Keys.PageDown)
        def _page_down(event: KeyPressEvent) -> None:
            self.scroll_transcript_down()
            event.app.invalidate()

        @bindings.add(Keys.BracketedPaste)
        def _paste(event: KeyPressEvent) -> None:
            self._paste_input(event.data)
            event.app.invalidate()

        return bindings

    def _submit_synthetic_input(self, text: str) -> None:
        """Submit a control line without discarding the user's editor draft."""

        mode = self._buffer_input_mode
        if self._input_future is None or self._input_future.done():
            self._queued_inputs.append((text, mode))
            return
        self._submitted_input_mode = mode
        self._input_future.set_result(text)

    def _accept_input(self) -> None:
        text = self._buffer.text
        mode = self._buffer_input_mode
        submission = TuiSubmission(
            id=new_submission_id(),
            content=text,
            display=text,
        )
        if mode not in {"approval", "trust"}:
            self._buffered_submissions[int(submission.id)] = submission
            self.state.pending_submissions = (
                *self.state.pending_submissions,
                submission.pending_view(),
            )
        self._clear_buffer()
        self._refresh()
        if self._input_future is None or self._input_future.done():
            self._queued_inputs.append((submission, mode))
            return
        self._submitted_input_mode = mode
        self._input_future.set_result(submission)

    def _insert_newline(self) -> None:
        self._buffer.insert_text("\n")

    def _paste_input(self, text: str) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._buffer.insert_text(normalized)

    def _interrupt_input(self) -> None:
        mode = self._buffer_input_mode
        signal = LiveFullscreenInputInterrupted()
        if self._input_future is None or self._input_future.done():
            self._queued_inputs.append((signal, mode))
            return
        self._submitted_input_mode = mode
        self._input_future.set_exception(signal)

    def _copy_selection(self, clipboard: Clipboard) -> bool:
        """Copy an active editor selection instead of treating Ctrl+C as quit."""

        if self._buffer.selection_state is None:
            return False
        clipboard.set_data(self._buffer.copy_selection())
        return True

    def _quit_input(self) -> None:
        mode = self._buffer_input_mode
        signal = TuiQuitRequested()
        self._clear_buffer()
        if self._input_future is None or self._input_future.done():
            self._queued_inputs.append((signal, mode))
            return
        self._submitted_input_mode = mode
        self._input_future.set_exception(signal)

    def _delete_right_or_close(self) -> None:
        if self._buffer.text:
            self._buffer.delete()
            return
        self._close_input()

    def _close_input(self) -> None:
        if self._input_future is None or self._input_future.done():
            return
        self._submitted_input_mode = self._buffer_input_mode
        self._clear_buffer()
        self._input_future.set_exception(EOFError())

    def _clear_buffer(self) -> None:
        self._buffer.reset()
        self._last_buffer_text = ""
        self._buffer_input_mode = self._visible_input_mode

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
        for entry in self._visible_transcript_entries():
            self._append_entry_fragments(fragments, entry)
        return fragments

    def _transcript_view_entries(self) -> int:
        rows, _columns = self._terminal_size()
        if rows is None:
            return super()._transcript_view_entries()
        transcript_rows = (
            rows - _HEADER_FRAME_HEIGHT - _FOOTER_HEIGHT - _TRANSCRIPT_FRAME_BORDER_HEIGHT
        )
        return max(1, min(super()._transcript_view_entries(), transcript_rows))

    def _transcript_wrap_width(self) -> int | None:
        _rows, columns = self._terminal_size()
        if columns is None:
            return None
        return max(1, columns - _TRANSCRIPT_FRAME_BORDER_WIDTH)

    def _terminal_size(self) -> tuple[int | None, int | None]:
        output = getattr(self._application, "output", None)
        get_size = getattr(output, "get_size", None)
        if not callable(get_size):
            return None, None
        size = get_size()
        rows = getattr(size, "rows", None)
        columns = getattr(size, "columns", None)
        return (
            rows if isinstance(rows, int) and rows > 0 else None,
            columns if isinstance(columns, int) and columns > 0 else None,
        )

    def _append_entry_fragments(
        self,
        fragments: StyleAndTextTuples,
        entry: _RenderedTranscriptLine,
    ) -> None:
        if fragments:
            fragments.append(("", "\n"))
        style = _prompt_toolkit_style(entry.style)
        label_style = f"class:{style} bold" if style else "bold"
        content_style = f"class:{style}" if style else ""
        if entry.role:
            fragments.append((label_style, f"{entry.role}: "))
        fragments.append((content_style, entry.content))

    def _footer_fragments(self) -> StyleAndTextTuples:
        _rows, columns = self._terminal_size()
        fragments: StyleAndTextTuples = []
        for index, line in enumerate(format_tui_footer_lines(self._view_snapshot(), width=columns)):
            if index:
                fragments.append(("", "\n"))
            fragments.append(("class:dim", line))
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
