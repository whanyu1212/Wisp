"""Input queue, prompt recall, and compact-echo state for the Textual frontend.

Import direction is intentionally one-way::

    textual_app -> textual_input -> prompt_history / compact_echo

The controller owns process-local input state only. It does not import Textual
widgets, the shell, RPC, providers, sessions, or approval policy. The app remains
the Textual event router and supplies the two UI effects needed when an input is
accepted or rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream

from wisp.tui.compact_echo import CompactEchoLog
from wisp.tui.prompt_history import PromptHistory, PromptHistoryEntry

_INPUT_BUFFER_CAPACITY = 100


class TextualInputSurface(Protocol):
    """Minimal Textual effects needed by :class:`TextualInputController`."""

    def clear_prompt_editor(self) -> None:
        """Clear the editable prompt when an input transition requires it."""

    def write_input_error(self, message: str) -> None:
        """Surface a recoverable input-queue failure to the user."""


class TextualInputController:
    """Own process-local prompt queue, recall history, and compact echoes.

    The controller has no knowledge of shell input modes or overlay policy. A
    caller chooses whether a submission clears the editor: ordinary typed and
    command submissions clear it, while decision responses preserve the hidden
    draft behind their transient overlay.
    """

    def __init__(
        self,
        surface: TextualInputSurface,
        *,
        queue_capacity: int = _INPUT_BUFFER_CAPACITY,
        prompt_history: PromptHistory | None = None,
        compact_echoes: CompactEchoLog | None = None,
    ) -> None:
        self._surface = surface
        self._send, self._receive = anyio.create_memory_object_stream[str | BaseException](
            queue_capacity
        )
        self._on_submit: Callable[[], None] | None = None
        self._prompt_history = prompt_history or PromptHistory()
        self._compact_echoes = compact_echoes or CompactEchoLog()

    @property
    def receive_stream(self) -> MemoryObjectReceiveStream[str | BaseException]:
        """Expose the queue for deterministic frontend integration tests."""

        return self._receive

    @property
    def prompt_history_entries(self) -> tuple[PromptHistoryEntry, ...]:
        """Return process-local accepted prompts, newest first."""

        return self._prompt_history.entries

    @property
    def compact_echo_key_count(self) -> int:
        """Return the number of prompts with pending compact echoes."""

        return self._compact_echoes.key_count

    @property
    def pending_compact_echo_count(self) -> int:
        """Return the bounded total number of pending compact echoes."""

        return self._compact_echoes.pending_count

    @property
    def compact_echo_order_length(self) -> int:
        """Return compact-echo insertion markers retained for bounded eviction."""

        return self._compact_echoes.order_length

    def set_submit_hook(self, on_submit: Callable[[], None]) -> None:
        """Set the callback run immediately before each queued input line."""

        self._on_submit = on_submit

    async def receive(self) -> str:
        """Wait for one queued prompt or re-raise its terminal input signal."""

        value = await self._receive.receive()
        if isinstance(value, BaseException):
            raise value
        return value

    def submit_line(self, text: str, *, clear_editor: bool) -> bool:
        """Queue a line, optionally clearing the editor before the attempt.

        The submit hook intentionally fires before the queue attempt, preserving
        the renderer's existing at-accept input-mode snapshot even if the bounded
        queue subsequently rejects the line.
        """

        if clear_editor:
            self._surface.clear_prompt_editor()
        if self._on_submit is not None:
            self._on_submit()
        try:
            self._send.send_nowait(text)
        except anyio.WouldBlock:
            self._surface.write_input_error("input buffer full; command dropped")
            return False
        return True

    def signal(self, signal: BaseException, *, action: str) -> bool:
        """Queue an interrupt/EOF signal and clear the editor only on success."""

        try:
            self._send.send_nowait(signal)
        except anyio.WouldBlock:
            self._surface.write_input_error(f"input buffer full; {action} ignored")
            return False
        self._surface.clear_prompt_editor()
        return True

    def register_compact_echo(self, prompt: str, display: str) -> None:
        """Associate one submitted full prompt with its compact transcript echo."""

        self._compact_echoes.register(prompt, display)

    def compact_echo_for(self, prompt: str) -> str:
        """Return and consume the compact echo for ``prompt`` when one exists."""

        return self._compact_echoes.take(prompt)

    def clear_compact_echoes(self) -> None:
        """Drop echoes for queued prompts the shell definitively abandoned."""

        self._compact_echoes.clear()

    def record_prompt(self, prompt: str) -> None:
        """Record one shell-accepted prompt for process-local recall."""

        self._prompt_history.record(prompt)


__all__ = ["TextualInputController", "TextualInputSurface"]
