"""Bounded lifecycle management for shell processes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast, overload
from uuid import uuid4

import anyio

from wisp.tools.output_store import OutputStore, SavedStream, saved_output_notice
from wisp.tools.progress import OutputProgress, ToolProgress
from wisp.tools.result import ToolError
from wisp.tools.shell.process import (
    ProcessResult,
    _CapturedOutput,
    _close_process_pipes,
    _collect_limited_output,
    _create_shell_process,
    _kill_process_tree_and_wait,
    _process_output_parts,
    _start_tail_readers,
    _stop_readers,
    _StreamTail,
    _terminate_process_tree,
    _wait_for_exit,
    decode_stream_head,
    stopped_detached_processes,
)
from wisp.tools.truncation import truncate_text_tail
from wisp.tools.utf8 import DecodedTextUnit, decode_utf8_units, decode_utf8_with_source_byte_lengths

ProcessState = Literal["running", "completed", "failed", "timed_out", "cancelled"]
# What a poll with a wait returns for: the next output, or the process stopping.
PollUntil = Literal["output", "exit"]
_PendingTextBackend = Literal["auto", "python", "native"]
_T = TypeVar("_T")

_logger = logging.getLogger(__name__)

DEFAULT_MAX_MANAGED_PROCESSES = 8
DEFAULT_MAX_RETAINED_BYTES = 50_000
DEFAULT_MAX_RETAINED_LINES = 2_000
# How long a foreground command moved to the background may run from then on.
DEFAULT_BACKGROUND_LIFETIME_SECONDS = 3600.0
POST_TERMINATION_DRAIN_TIMEOUT = 0.25
# The last output a timeout error shows. The full output is saved, and an error
# message has a small budget of its own.
_TIMEOUT_TAIL_BYTES = 1_000
_TIMEOUT_TAIL_LINES = 20
# How often a running one-shot command's output is sampled for progress, and how
# much of each stream's end a sample carries. The executor throttles delivery.
_PROGRESS_SAMPLE_SECONDS = 0.25
_PROGRESS_TAIL_BYTES = 4_096
PROCESS_TREE_CLEANUP_ERROR = "Failed to terminate process tree"
_LINE_BREAK_CHARACTERS = frozenset(
    {"\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"}
)


@dataclass(frozen=True, slots=True)
class ProcessUpdate:
    """One incremental snapshot of a managed process."""

    process_id: str
    state: ProcessState
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    error: str | None = None
    stdout_retained_bytes: int | None = None
    stderr_retained_bytes: int | None = None
    stdout_source_byte_lengths: tuple[int, ...] | None = None
    stderr_source_byte_lengths: tuple[int, ...] | None = None
    # When the process started, and when it reached a terminal state (None while it
    # runs). Both are UTC wall-clock times.
    started_at: datetime | None = None
    ended_at: datetime | None = None
    # Whether cleanup stopped processes the command had detached, such as a daemon.
    background_stopped: bool = False
    # The full streams, which the caller saves if its result shows only part of them.
    stdout_saved: SavedStream | None = None
    stderr_saved: SavedStream | None = None


@dataclass(frozen=True, slots=True)
class ProcessExit:
    """A started process that stopped without being cancelled.

    Attributes:
        process_id (str): The handle ``start`` returned.
        command (str): The command as started.
        state (ProcessState): ``completed``, ``failed``, or ``timed_out``.
        exit_code (int | None): The exit status, for a completed process.
        started_at (datetime): When the process started, in UTC.
        ended_at (datetime): When it stopped, in UTC.
        notify (bool): Whether the caller asked to be told of the exit.
    """

    process_id: str
    command: str
    state: ProcessState
    exit_code: int | None
    started_at: datetime
    ended_at: datetime
    notify: bool


ProcessExitListener = Callable[[ProcessExit], None]


@dataclass(frozen=True, slots=True)
class RunningProcess:
    """A started process that has not stopped yet.

    Attributes:
        process_id (str): The handle ``start`` returned.
        command (str): The command as started.
        started_at (datetime): When the process started, in UTC.
        notify (bool): Whether the caller asked to be told of its exit.
    """

    process_id: str
    command: str
    started_at: datetime
    notify: bool


@dataclass
class _PendingFragment:
    text: str
    source_byte_lengths: tuple[int, ...]
    encoded_byte_lengths: tuple[int, ...]
    source_bytes: int
    encoded_bytes: int


@dataclass
class _PendingLine:
    fragments: deque[_PendingFragment] = field(default_factory=deque)
    complete: bool = False


@dataclass
class _PythonPendingText:
    max_bytes: int
    max_lines: int
    dropped_bytes: int = 0
    _lines: deque[_PendingLine] = field(default_factory=deque)
    _retained_source_bytes: int = 0
    _retained_encoded_bytes: int = 0
    _pending_utf8: bytes = b""
    _pending_carriage_return: bool = False

    @property
    def retained_source_bytes(self) -> int:
        return self._retained_source_bytes

    @property
    def has_text(self) -> bool:
        return bool(self._lines)

    @property
    def text(self) -> str:
        return "".join(fragment.text for line in self._lines for fragment in line.fragments)

    def append(self, value: str) -> None:
        if not value:
            return
        self._append_text_units((character, len(character.encode("utf-8"))) for character in value)

    def append_bytes(self, value: bytes, *, final: bool = False) -> None:
        data = self._pending_utf8 + value
        if data.isascii():
            self._pending_utf8 = b""
            self._append_text_units((character, 1) for character in data.decode("ascii"))
            return
        units, pending = decode_utf8_units(data, final=final)
        self._pending_utf8 = pending
        self._append_units(units)

    def _append_units(self, units: Iterable[DecodedTextUnit]) -> None:
        self._append_text_units((unit.text, unit.source_bytes) for unit in units)

    def _append_text_units(self, units: Iterable[tuple[str, int]]) -> None:
        text_parts: list[str] = []
        source_byte_lengths: list[int] = []
        appended = False

        def append_unit(text: str, source_bytes: int) -> None:
            text_parts.append(text)
            source_byte_lengths.append(source_bytes)

        def flush_fragment() -> None:
            if not text_parts:
                return
            lengths = tuple(source_byte_lengths)
            text = "".join(text_parts)
            encoded_lengths = (
                lengths
                if text.isascii()
                else tuple(len(part.encode("utf-8")) for part in text_parts)
            )
            source_bytes = sum(lengths)
            encoded_bytes = sum(encoded_lengths)
            self._current_line().fragments.append(
                _PendingFragment(
                    text=text,
                    source_byte_lengths=lengths,
                    encoded_byte_lengths=encoded_lengths,
                    source_bytes=source_bytes,
                    encoded_bytes=encoded_bytes,
                )
            )
            self._retained_source_bytes += source_bytes
            self._retained_encoded_bytes += encoded_bytes
            text_parts.clear()
            source_byte_lengths.clear()

        for text, source_bytes in units:
            appended = True
            if self._pending_carriage_return:
                if text == "\n":
                    append_unit(text, source_bytes)
                    flush_fragment()
                    self._finish_current_line()
                    continue
                flush_fragment()
                self._finish_current_line()

            append_unit(text, source_bytes)
            if text == "\r":
                self._pending_carriage_return = True
            elif text in _LINE_BREAK_CHARACTERS:
                flush_fragment()
                self._finish_current_line()
        if appended:
            flush_fragment()
            self._trim()

    def _current_line(self) -> _PendingLine:
        if not self._lines or self._lines[-1].complete:
            self._lines.append(_PendingLine())
        return self._lines[-1]

    def _finish_current_line(self) -> None:
        if self._lines:
            self._lines[-1].complete = True
        self._pending_carriage_return = False

    def _trim(self) -> None:
        if self.max_bytes <= 0 or self.max_lines <= 0:
            self._drop_all()
            return
        while len(self._lines) > self.max_lines:
            self._drop_line()
        while self._retained_encoded_bytes > self.max_bytes and self._lines:
            line = self._lines[0]
            fragment = line.fragments[0]
            excess_bytes = self._retained_encoded_bytes - self.max_bytes
            if fragment.encoded_bytes <= excess_bytes:
                self._drop_fragment(line.fragments.popleft())
            else:
                self._trim_fragment(fragment, excess_bytes)
            if not line.fragments:
                self._lines.popleft()
        if not self._lines or self._lines[-1].complete:
            self._pending_carriage_return = False

    def _drop_all(self) -> None:
        while self._lines:
            self._drop_line()
        self._pending_carriage_return = False

    def _drop_line(self) -> None:
        line = self._lines.popleft()
        while line.fragments:
            self._drop_fragment(line.fragments.popleft())

    def _drop_fragment(self, fragment: _PendingFragment) -> None:
        self._retained_source_bytes -= fragment.source_bytes
        self._retained_encoded_bytes -= fragment.encoded_bytes
        self.dropped_bytes += fragment.source_bytes

    def _trim_fragment(self, fragment: _PendingFragment, excess_encoded_bytes: int) -> None:
        removed_encoded_bytes = 0
        removed_source_bytes = 0
        removed_characters = 0
        for source_bytes, encoded_bytes in zip(
            fragment.source_byte_lengths, fragment.encoded_byte_lengths, strict=True
        ):
            if removed_encoded_bytes >= excess_encoded_bytes:
                break
            removed_source_bytes += source_bytes
            removed_encoded_bytes += encoded_bytes
            removed_characters += 1
        fragment.text = fragment.text[removed_characters:]
        fragment.source_byte_lengths = fragment.source_byte_lengths[removed_characters:]
        fragment.encoded_byte_lengths = fragment.encoded_byte_lengths[removed_characters:]
        fragment.source_bytes -= removed_source_bytes
        fragment.encoded_bytes -= removed_encoded_bytes
        self._retained_source_bytes -= removed_source_bytes
        self._retained_encoded_bytes -= removed_encoded_bytes
        self.dropped_bytes += removed_source_bytes

    def drain(self) -> tuple[str, int, int, tuple[int, ...]]:
        text = self.text
        dropped_bytes = self.dropped_bytes
        retained_source_bytes = self.retained_source_bytes
        source_byte_lengths = tuple(
            source_bytes
            for line in self._lines
            for fragment in line.fragments
            for source_bytes in fragment.source_byte_lengths
        )
        self.dropped_bytes = 0
        self._lines.clear()
        self._retained_source_bytes = 0
        self._retained_encoded_bytes = 0
        self._pending_carriage_return = False
        return text, dropped_bytes, retained_source_bytes, source_byte_lengths


class _PendingTextLike(Protocol):
    """State and operations consumed by the managed-process supervisor."""

    @property
    def dropped_bytes(self) -> int: ...

    @property
    def retained_source_bytes(self) -> int: ...

    @property
    def has_text(self) -> bool: ...

    @property
    def text(self) -> str: ...

    def append(self, value: str) -> None: ...

    def append_bytes(self, value: bytes, *, final: bool = False) -> None: ...

    def drain(self) -> tuple[str, int, int, tuple[int, ...]]: ...


_PendingTextFactory = Callable[[int, int], _PendingTextLike]


def _load_native_pending_text() -> _PendingTextFactory | None:
    try:
        native_module = import_module("wisp._native")
    except ModuleNotFoundError as exc:
        if exc.name == "wisp._native":
            return None
        raise
    return cast(_PendingTextFactory, native_module.PendingText)


_NativePendingText = _load_native_pending_text()


def _pending_text_class(backend: _PendingTextBackend = "auto") -> _PendingTextFactory:
    """Resolve a pending-text implementation for production or controlled tests."""

    if backend == "python":
        return _PythonPendingText
    if backend == "native":
        if _NativePendingText is None:
            raise RuntimeError("the native pending-text backend is not installed")
        return _NativePendingText
    if backend == "auto":
        return _NativePendingText or _PythonPendingText
    raise ValueError(f"unknown pending-text backend: {backend}")


def _pending_text_backends() -> tuple[tuple[str, _PendingTextFactory], ...]:
    """Return concrete backends available to conformance tests."""

    backends: list[tuple[str, _PendingTextFactory]] = [("python", _PythonPendingText)]
    if _NativePendingText is not None:
        backends.append(("native", _NativePendingText))
    return tuple(backends)


_PendingText = _pending_text_class()


@dataclass
class _ManagedProcess:
    process_id: str
    process: asyncio.subprocess.Process
    command: str
    stdout: _PendingTextLike
    stderr: _PendingTextLike
    stdout_saved: SavedStream
    stderr_saved: SavedStream
    state: ProcessState = "running"
    exit_code: int | None = None
    terminal_override: ProcessState | None = None
    error: str | None = None
    error_after_cleanup: str | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    completion_task: asyncio.Task[None] | None = None
    terminal_reported: bool = False
    notify_on_exit: bool = False
    exit_announced: bool = False
    # A user stopped it, so its exit is announced like one that ended on its own.
    announce_cancel: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.monotonic)
    ended_at: datetime | None = None

    def mark_ended(self) -> None:
        """Record when the process first reached a terminal state.

        The end is measured from the start on the monotonic clock, so a wall-clock
        change while the process runs cannot skew its elapsed time. A later cleanup
        retry that revises the state keeps the original end.
        """
        if self.ended_at is None:
            elapsed = time.monotonic() - self.started_monotonic
            self.ended_at = self.started_at + timedelta(seconds=elapsed)

    def has_pending_output(self) -> bool:
        return bool(
            self.stdout.has_text
            or self.stderr.has_text
            or self.stdout.dropped_bytes
            or self.stderr.dropped_bytes
        )


def _completion_ended(managed: _ManagedProcess) -> bool:
    # A completion task that failed before recording a terminal state leaves the
    # process "running"; an exit wait would otherwise sit out its whole deadline.
    task = managed.completion_task
    return task is not None and task.done()


class ProcessSupervisor:
    """Own bounded shell-process handles and terminate them on shutdown."""

    def __init__(
        self,
        *,
        max_processes: int = DEFAULT_MAX_MANAGED_PROCESSES,
        output_store: OutputStore | None = None,
        _pending_text_backend: _PendingTextBackend = "auto",
    ) -> None:
        if max_processes < 1:
            raise ValueError("max_processes must be greater than or equal to 1")
        self._max_processes = max_processes
        self._output_store = output_store if output_store is not None else OutputStore()
        self._pending_text_class = _pending_text_class(_pending_text_backend)
        self._managed: dict[str, _ManagedProcess] = {}
        self._one_shot: dict[
            asyncio.subprocess.Process,
            asyncio.Task[Any],
        ] = {}
        self._one_shot_locks: dict[asyncio.subprocess.Process, asyncio.Lock] = {}
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._exit_listener: ProcessExitListener | None = None

    def set_exit_listener(self, listener: ProcessExitListener | None) -> None:
        """Call ``listener`` once for each started process that stops on its own.

        The call happens on the event loop, after the process's terminal state is
        final and any waiting poll has been woken. It is not made for a cancelled
        process: whoever cancelled it already has the result. The listener must not
        block; an exception it raises is logged and does not affect the process.

        Args:
            listener (ProcessExitListener | None): The callback, or None to stop.
        """

        self._exit_listener = listener

    @property
    def has_exit_listener(self) -> bool:
        """Whether a listener will hear about processes that stop on their own."""

        return self._exit_listener is not None

    def running_processes(self) -> tuple[RunningProcess, ...]:
        """Return the started processes still running, in the order they started.

        One-shot ``run`` commands are not included: they end within their call.

        Returns:
            tuple[RunningProcess, ...]: One entry per running process.
        """

        return tuple(
            RunningProcess(
                process_id=managed.process_id,
                command=managed.command,
                started_at=managed.started_at,
                notify=managed.notify_on_exit,
            )
            for managed in self._managed.values()
            if managed.state == "running" and not _completion_ended(managed)
        )

    def is_terminal_reported(self, process_id: str) -> bool:
        """Whether a poll or cancel has already returned this process's final state.

        A handle the supervisor no longer holds counts as reported: only reported
        processes are released to make room.

        Args:
            process_id (str): The handle ``start`` returned.

        Returns:
            bool: True once the caller has seen the terminal state.
        """

        managed = self._managed.get(process_id)
        return managed is None or managed.terminal_reported

    @property
    def output_store(self) -> OutputStore:
        """Return the store that keeps full output of commands whose results truncate it."""

        return self._output_store

    @overload
    async def run_to_completion(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
        progress: ToolProgress | None = None,
        background: None = None,
        background_lifetime: float = ...,
    ) -> ProcessResult: ...

    @overload
    async def run_to_completion(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
        progress: ToolProgress | None = None,
        background: asyncio.Event,
        background_lifetime: float = ...,
    ) -> ProcessResult | ProcessUpdate: ...

    async def run_to_completion(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
        progress: ToolProgress | None = None,
        background: asyncio.Event | None = None,
        background_lifetime: float = DEFAULT_BACKGROUND_LIFETIME_SECONDS,
    ) -> ProcessResult | ProcessUpdate:
        """Run one command to completion while tracking it for shutdown.

        If ``background`` is set while the command runs, the command moves to the
        background instead: it becomes a managed process, as ``start`` makes, that
        tells the exit listener when it stops, and this returns at once with its
        output so far. Its readers carry on into the managed process, so no output
        is lost or repeated, and its process tree stays owned throughout.

        Args:
            command (str): Shell command to run.
            cwd (Path): Working directory.
            timeout (float): Seconds before the process tree is stopped, while the
                command runs in the foreground.
            max_output_bytes (int): Bytes of each stream's tail to keep.
            env (Mapping[str, str] | None, optional): Environment for the command.
                Defaults to None.
            progress (ToolProgress | None, optional): Receives the output so far
                while the command runs. Defaults to None.
            background (asyncio.Event | None, optional): Set to move the command to
                the background. Defaults to None.
            background_lifetime (float, optional): Seconds a command moved to the
                background may run from then on. Defaults to an hour.

        Returns:
            ProcessResult | ProcessUpdate: The exit status and captured output, or,
            once moved to the background, the running process's id and its output
            so far. A command that finishes as the move is asked for returns its
            result.

        Raises:
            ToolError: The command timed out, or its process tree could not be stopped.
        """

        async with self._lock:
            self._ensure_open()
            self._evict_terminals_for_capacity()
            if self._owned_process_count() >= self._max_processes:
                raise self._capacity_error()
            process = await self._spawn(command, cwd=cwd, env=env)
            started_at = datetime.now(UTC)
            started_monotonic = time.monotonic()
            self._one_shot_locks[process] = asyncio.Lock()
            stdout_tail = _StreamTail(
                max_output_bytes,
                self._open_saved_stream("stdout", command, spill_threshold=max_output_bytes),
            )
            stderr_tail = _StreamTail(
                max_output_bytes,
                self._open_saved_stream("stderr", command, spill_threshold=max_output_bytes),
            )
            # The readers belong to this call, not to capture, so a move to the
            # background can hand them on while they keep reading.
            readers = _start_tail_readers(process, stdout_tail, stderr_tail)
            capture_task = asyncio.create_task(
                _collect_limited_output(process, stdout_tail, stderr_tail, readers=readers)
            )
            self._one_shot[process] = capture_task

        release_ownership = False
        output_reported = False
        moved: ProcessUpdate | None = None
        sampler = (
            asyncio.create_task(_sample_progress(progress, stdout_tail, stderr_tail))
            if progress is not None
            else None
        )
        try:
            try:
                captured = await _capture_or_background(capture_task, readers, background, timeout)
                if captured is None:
                    moved = await self._move_to_background(
                        _OneShotHandover(
                            process=process,
                            command=command,
                            capture=capture_task,
                            readers=readers,
                            stdout_tail=stdout_tail,
                            stderr_tail=stderr_tail,
                            started_at=started_at,
                            started_monotonic=started_monotonic,
                        ),
                        lifetime=background_lifetime,
                    )
                    if moved is None:
                        # It finished as the move was asked for.
                        captured = capture_task.result()
            except TimeoutError as exc:
                cleanup_task = asyncio.create_task(self._terminate_one_shot(process, wait=True))
                try:
                    cleanup_succeeded = await self._await_task_before_propagating_cancellation(
                        cleanup_task
                    )
                except asyncio.CancelledError:
                    release_ownership = bool(
                        cleanup_task.done()
                        and not cleanup_task.cancelled()
                        and cleanup_task.result()
                    )
                    raise
                release_ownership = cleanup_succeeded
                if not cleanup_succeeded:
                    raise ToolError(PROCESS_TREE_CLEANUP_ERROR) from exc
                message = await _timeout_message(timeout, stdout_tail, stderr_tail)
                output_reported = True
                raise ToolError(
                    message,
                    failure_code="timeout",
                    retryable=True,
                    recovery_hint=(
                        "The result is inconclusive; retry with a suitable timeout if appropriate."
                    ),
                ) from exc
            except BaseException:
                cleanup_task = asyncio.create_task(self._terminate_one_shot(process, wait=True))
                try:
                    cleanup_succeeded = await self._await_task_before_propagating_cancellation(
                        cleanup_task
                    )
                except asyncio.CancelledError:
                    release_ownership = bool(
                        cleanup_task.done()
                        and not cleanup_task.cancelled()
                        and cleanup_task.result()
                    )
                    raise
                release_ownership = cleanup_succeeded
                raise

            if moved is not None:
                # The managed process owns the tree, readers, and saved output now.
                output_reported = True
                return moved
            assert captured is not None
            # One task freezes the output, as capture's own readers stopped then, and
            # reaps the group, so a cancel landing between them cannot skip the reap.
            cleanup_task = asyncio.create_task(self._stop_and_reap_one_shot(readers, process))
            try:
                cleanup_succeeded = await self._await_task_before_propagating_cancellation(
                    cleanup_task
                )
            except asyncio.CancelledError:
                release_ownership = bool(
                    cleanup_task.done() and not cleanup_task.cancelled() and cleanup_task.result()
                )
                raise
            if not cleanup_succeeded:
                raise ToolError("Failed to terminate process tree")
            stdout_result, stderr_result = captured.stdout, captured.stderr
            stdout, stdout_source_byte_lengths = decode_utf8_with_source_byte_lengths(
                stdout_result.output
            )
            stderr, stderr_source_byte_lengths = decode_utf8_with_source_byte_lengths(
                stderr_result.output
            )
            result = ProcessResult(
                exit_code=process.returncode if process.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_result.truncated,
                stderr_truncated=stderr_result.truncated,
                stdout_dropped_bytes=stdout_result.dropped_bytes,
                stderr_dropped_bytes=stderr_result.dropped_bytes,
                stdout_retained_bytes=len(stdout_result.output),
                stderr_retained_bytes=len(stderr_result.output),
                stdout_source_byte_lengths=stdout_source_byte_lengths,
                stderr_source_byte_lengths=stderr_source_byte_lengths,
                stdout_head=decode_stream_head(stdout_result.head),
                stderr_head=decode_stream_head(stderr_result.head),
                background_stopped=(
                    captured.pipes_held_after_exit or stopped_detached_processes(process)
                ),
                stdout_saved=stdout_tail.saved,
                stderr_saved=stderr_tail.saved,
            )
            release_ownership = True
            output_reported = True
            return result
        finally:
            if sampler is not None:
                # Not awaited: during cancellation an await here would raise again and
                # skip the cleanup below. The sampler is parked in its sleep, so it
                # cannot report again, and the executor ignores reports after the call.
                sampler.cancel()
            if moved is None:
                # Normally stopped already; not awaited, as for the sampler.
                for reader in readers:
                    reader.cancel()
            if not output_reported:
                # No result will name this output, so nothing could ever read it.
                for tail in (stdout_tail, stderr_tail):
                    if tail.saved is not None:
                        tail.saved.discard()
            if release_ownership:
                release_task = asyncio.create_task(self._release_one_shot(process))
                await self._await_task_before_propagating_cancellation(release_task)

    async def _stop_and_reap_one_shot(
        self,
        readers: tuple[asyncio.Task[None], asyncio.Task[None]],
        process: asyncio.subprocess.Process,
    ) -> bool:
        """Stop a finished command's readers, then reap its process group."""

        await _stop_readers(readers)
        return await self._terminate_one_shot(process)

    async def _move_to_background(
        self, handover: _OneShotHandover, *, lifetime: float
    ) -> ProcessUpdate | None:
        """Turn a running foreground command into a managed process that notifies.

        Everything that changes hands does so under the supervisor lock with no await
        in between, so a reader sees each chunk on exactly one side of the move, and
        the process tree is owned by the foreground call or the managed process,
        never neither.

        Args:
            handover (_OneShotHandover): The foreground command and what it owns.
            lifetime (float): Seconds the managed process may run from now.

        Returns:
            ProcessUpdate | None: The new process's id and the output the foreground
            call had, or None if the command had already finished.
        """

        process = handover.process
        async with self._lock:
            self._ensure_open()
            if handover.capture.done():
                return None
            stdout_so_far = handover.stdout_tail.result()
            stderr_so_far = handover.stderr_tail.result()
            # A character the cut split goes to the managed buffer, where the rest of it
            # arrives, rather than being decoded half here.
            stdout_text, stdout_partial = _split_incomplete_utf8(stdout_so_far.output)
            stderr_text, stderr_partial = _split_incomplete_utf8(stderr_so_far.output)
            assert handover.stdout_tail.saved is not None
            assert handover.stderr_tail.saved is not None
            managed = _ManagedProcess(
                process_id=uuid4().hex,
                process=process,
                command=handover.command,
                stdout=self._pending_text_class(
                    DEFAULT_MAX_RETAINED_BYTES, DEFAULT_MAX_RETAINED_LINES
                ),
                stderr=self._pending_text_class(
                    DEFAULT_MAX_RETAINED_BYTES, DEFAULT_MAX_RETAINED_LINES
                ),
                stdout_saved=handover.stdout_tail.saved,
                stderr_saved=handover.stderr_tail.saved,
                notify_on_exit=True,
                started_at=handover.started_at,
                started_monotonic=handover.started_monotonic,
            )
            managed.stdout_task, managed.stderr_task = handover.readers
            managed.stdout.append_bytes(stdout_partial)
            managed.stderr.append_bytes(stderr_partial)
            handover.stdout_tail.sink = partial(_forward_output, managed, managed.stdout)
            handover.stderr_tail.sink = partial(_forward_output, managed, managed.stderr)
            self._managed[managed.process_id] = managed
            del self._one_shot[process]
            self._one_shot_locks.pop(process, None)
            # Capture only waited on the readers, which carry on; nothing to await.
            handover.capture.cancel()
            managed.completion_task = asyncio.create_task(
                self._wait_for_completion(managed, lifetime)
            )
        stdout, stdout_lengths = decode_utf8_with_source_byte_lengths(stdout_text)
        stderr, stderr_lengths = decode_utf8_with_source_byte_lengths(stderr_text)
        return ProcessUpdate(
            process_id=managed.process_id,
            state="running",
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_so_far.truncated,
            stderr_truncated=stderr_so_far.truncated,
            stdout_dropped_bytes=stdout_so_far.dropped_bytes,
            stderr_dropped_bytes=stderr_so_far.dropped_bytes,
            stdout_retained_bytes=len(stdout_text),
            stderr_retained_bytes=len(stderr_text),
            stdout_source_byte_lengths=stdout_lengths,
            stderr_source_byte_lengths=stderr_lengths,
            started_at=handover.started_at,
            stdout_saved=managed.stdout_saved,
            stderr_saved=managed.stderr_saved,
        )

    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES,
        max_retained_lines: int = DEFAULT_MAX_RETAINED_LINES,
        env: Mapping[str, str] | None = None,
        notify_on_exit: bool = False,
    ) -> str:
        """Start one resumable process and return an opaque runtime-local handle.

        ``notify_on_exit`` reaches the exit listener as ``ProcessExit.notify``.
        """

        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retained_bytes < 0 or max_retained_lines < 0:
            raise ValueError("retained output limits must be non-negative")

        async with self._lock:
            self._ensure_open()
            self._evict_terminals_for_capacity()
            if self._owned_process_count() >= self._max_processes:
                raise self._capacity_error()
            stdout = self._pending_text_class(max_retained_bytes, max_retained_lines)
            stderr = self._pending_text_class(max_retained_bytes, max_retained_lines)
            process = await self._spawn(command, cwd=cwd, env=env)
            process_id = uuid4().hex
            managed = _ManagedProcess(
                process_id=process_id,
                process=process,
                command=command,
                stdout=stdout,
                stderr=stderr,
                stdout_saved=self._open_saved_stream(
                    "stdout", command, spill_threshold=max_retained_bytes
                ),
                stderr_saved=self._open_saved_stream(
                    "stderr", command, spill_threshold=max_retained_bytes
                ),
                notify_on_exit=notify_on_exit,
            )
            managed.stdout_task = asyncio.create_task(
                self._read_stream(managed, process.stdout, managed.stdout, managed.stdout_saved)
            )
            managed.stderr_task = asyncio.create_task(
                self._read_stream(managed, process.stderr, managed.stderr, managed.stderr_saved)
            )
            managed.completion_task = asyncio.create_task(
                self._wait_for_completion(managed, timeout)
            )
            self._managed[process_id] = managed
            return process_id

    async def poll(
        self, process_id: str, *, wait_seconds: float = 0, until: PollUntil = "output"
    ) -> ProcessUpdate:
        """Return output not delivered by earlier polls and the current state.

        Args:
            process_id (str): The handle ``start`` returned.
            wait_seconds (float): The longest to wait while the process runs; 0
                returns at once.
            until (PollUntil): ``"output"`` returns as soon as there is output not
                yet delivered. ``"exit"`` keeps waiting through output until the
                process stops running, so a command that prints regularly cannot
                cut the wait short; the output gathered meanwhile is returned
                together, bounded like any other.

        Returns:
            ProcessUpdate: Output since the last poll and the process's state.

        Raises:
            ValueError: ``wait_seconds`` is negative.
        """

        if wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative")
        managed = await self._get(process_id)
        if wait_seconds > 0:
            await self._wait_for_poll(managed, wait_seconds, until)
        async with managed.operation_lock:
            return self._snapshot(managed)

    async def _wait_for_poll(
        self, managed: _ManagedProcess, wait_seconds: float, until: PollUntil
    ) -> None:
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            async with managed.operation_lock:
                # Nothing awaits between these checks and the clear, so a chunk or a
                # state change lands either before the checks, which see it, or after
                # the clear, which wakes the wait below. Readers and the completion
                # task do not take this lock; that ordering is what makes it safe.
                if managed.state != "running" or _completion_ended(managed):
                    return
                if until == "output" and managed.has_pending_output():
                    return
                managed.changed.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(managed.changed.wait(), timeout=remaining)
            except TimeoutError:
                return
            # An output wait returns on any wake, as it always has, even one that
            # brought no text (a closed stream, part of a UTF-8 character).
            if until == "output":
                return

    async def stop(self, process_id: str) -> ProcessState:
        """Terminate a running process for the user, leaving it for the caller to poll.

        Unlike ``cancel``, this reads nothing: the process's output and final state
        stay for the caller's next poll, as for a process that stopped on its own,
        and the exit is announced to the exit listener as ``cancelled``.

        Args:
            process_id (str): The handle ``start`` returned.

        Returns:
            ProcessState: The final state: ``cancelled``, or ``failed`` when the
            process tree could not be stopped. A process that had already stopped
            keeps the state it stopped with.

        Raises:
            ToolError: No managed process has this handle.
        """

        managed = await self._get(process_id)
        async with managed.operation_lock:
            if managed.state == "running":
                managed.terminal_override = "cancelled"
                managed.announce_cancel = True
                cleanup_task = asyncio.create_task(self._terminate_managed(managed))
                await self._await_task_before_propagating_cancellation(cleanup_task)
            return managed.state

    async def cancel(self, process_id: str) -> ProcessUpdate:
        """Terminate one managed process tree and return its final snapshot."""

        managed = await self._get(process_id)
        async with managed.operation_lock:
            if managed.state == "running" or managed.error == PROCESS_TREE_CLEANUP_ERROR:
                if managed.state == "running":
                    managed.terminal_override = "cancelled"
                cleanup_task = asyncio.create_task(self._terminate_managed(managed))
                await self._await_task_before_propagating_cancellation(cleanup_task)
            return self._snapshot(managed)

    async def cancel_all(self) -> tuple[ProcessUpdate, ...]:
        """Terminate every running managed process; the supervisor stays open.

        A process whose earlier cleanup failed is retried, as ``aclose`` does. The
        processes keep their final state, so a later poll reports each as cancelled
        rather than unknown.

        Returns:
            tuple[ProcessUpdate, ...]: The processes whose tree still could not be
            terminated. They stay registered, so a later call retries them.
        """

        async with self._lock:
            pending = [
                process_id
                for process_id, managed in self._managed.items()
                if managed.state == "running" or managed.error == PROCESS_TREE_CLEANUP_ERROR
            ]
        updates = await asyncio.gather(*(self.cancel(process_id) for process_id in pending))
        return tuple(update for update in updates if update.error == PROCESS_TREE_CLEANUP_ERROR)

    async def aclose(self) -> None:
        """Terminate all owned process trees. Safe to call repeatedly or concurrently."""

        # Keep shutdown attached to this call even when its caller is cancelled.
        # Frontends call aclose() from their finalizers and may tear down the event
        # loop as soon as cancellation propagates, so merely leaving close_task
        # running in the background is not sufficient.
        init_task = asyncio.create_task(self._initialize_close_task())
        try:
            close_task = await asyncio.shield(init_task)
        except asyncio.CancelledError:
            close_task = await self._await_task_after_cancellation(init_task)
            await self._await_task_after_cancellation(close_task)
            raise

        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await self._await_task_after_cancellation(close_task)
            raise

    async def _initialize_close_task(self) -> asyncio.Task[None]:
        async with self._lock:
            close_failed = (
                self._close_task is not None
                and self._close_task.done()
                and not self._close_task.cancelled()
                and self._close_task.exception() is not None
            )
            closed_with_owned_processes = bool(
                self._close_task is not None
                and self._close_task.done()
                and (self._managed or self._one_shot)
            )
            if self._close_task is None or close_failed or closed_with_owned_processes:
                self._closed = True
                self._close_task = asyncio.create_task(self._close_owned_processes())
            assert self._close_task is not None
            return self._close_task

    async def _release_one_shot(self, process: asyncio.subprocess.Process) -> None:
        async with self._lock:
            if self._closed:
                return
            self._remove_one_shot(process)

    def _remove_one_shot(self, process: asyncio.subprocess.Process) -> None:
        self._one_shot.pop(process, None)
        self._one_shot_locks.pop(process, None)

    def _owned_process_count(self) -> int:
        return len(self._managed) + len(self._one_shot)

    def _capacity_error(self) -> ToolError:
        """Name the processes holding every slot, so the caller can free one.

        After compaction the model may no longer have the process ids it started.
        """

        held = [
            f"{managed.process_id} ({managed.state}): {_command_summary(managed.command)}"
            for managed in self._managed.values()
        ]
        if self._one_shot:
            held.append(f"{len(self._one_shot)} one-shot command(s) still running")
        return ToolError(
            f"Cannot start command: managed process limit ({self._max_processes}) reached. "
            f"Held by: {'; '.join(held)}",
            retryable=True,
            recovery_hint=(
                "Poll a finished process or cancel one you no longer need by its "
                "process_id, then retry."
            ),
        )

    async def _terminate_one_shot(
        self,
        process: asyncio.subprocess.Process,
        *,
        wait: bool = False,
        force: bool = False,
    ) -> bool:
        cleanup_lock = self._one_shot_locks.get(process)
        if cleanup_lock is None:
            # Closing the supervisor stopped this tree and then released it, which it
            # does only once the tree is gone.
            return True
        async with cleanup_lock:
            if wait:
                return await _kill_process_tree_and_wait(process)
            if force:
                return await _terminate_process_tree(process, force=True)
            return await _terminate_process_tree(process)

    async def _await_task_before_propagating_cancellation(self, task: asyncio.Task[_T]) -> _T:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._await_task_after_cancellation(task)
            raise

    async def _await_task_after_cancellation(self, task: asyncio.Task[_T]) -> _T:
        while True:
            if task.done():
                return task.result()
            try:
                with anyio.CancelScope(shield=True):
                    result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A caller may issue more than one direct asyncio cancellation
                # while cleanup is in progress.
                continue
            return result

    async def _close_owned_processes(self) -> None:
        async with self._lock:
            managed = tuple(self._managed.values())
            one_shot = tuple(self._one_shot.items())
        managed_to_cleanup = tuple(
            item
            for item in managed
            if item.state == "running" or item.error == PROCESS_TREE_CLEANUP_ERROR
        )
        managed_results = await asyncio.gather(
            *(self._close_managed_process(item) for item in managed_to_cleanup),
            return_exceptions=True,
        )
        one_shot_results = await asyncio.gather(
            *(self._terminate_one_shot(process) for process, _capture_task in one_shot),
            return_exceptions=True,
        )
        failed_managed_ids = {
            item.process_id
            for item, result in zip(managed_to_cleanup, managed_results, strict=True)
            if result is not True
        }
        failed_one_shot_processes = {
            process
            for (process, _capture_task), result in zip(one_shot, one_shot_results, strict=True)
            if result is not True
        }
        await asyncio.gather(
            *(
                self._finish_one_shot_capture(process, capture_task)
                for process, capture_task in one_shot
                if process not in failed_one_shot_processes
            )
        )

        await asyncio.gather(
            *(
                item.completion_task
                for item in managed
                if item.completion_task is not None and item.process_id not in failed_managed_ids
            ),
            return_exceptions=True,
        )

        async with self._lock:
            if failed_managed_ids or failed_one_shot_processes:
                self._managed = {
                    process_id: item
                    for process_id, item in self._managed.items()
                    if process_id in failed_managed_ids
                }
                self._one_shot = {
                    process: capture_task
                    for process, capture_task in self._one_shot.items()
                    if process in failed_one_shot_processes
                }
                self._one_shot_locks = {
                    process: cleanup_lock
                    for process, cleanup_lock in self._one_shot_locks.items()
                    if process in failed_one_shot_processes
                }
            else:
                self._managed.clear()
                self._one_shot.clear()
                self._one_shot_locks.clear()

        # Saved output is owned by this runtime and goes away with it.
        await self._output_store.aclose()
        if failed_managed_ids or failed_one_shot_processes:
            for item in managed_to_cleanup:
                if item.process_id in failed_managed_ids:
                    item.error = PROCESS_TREE_CLEANUP_ERROR
                    item.state = "failed"
                    item.changed.set()
            raise ToolError(PROCESS_TREE_CLEANUP_ERROR)

    async def _close_managed_process(self, managed: _ManagedProcess) -> bool:
        async with managed.operation_lock:
            if managed.state != "running" and managed.error != PROCESS_TREE_CLEANUP_ERROR:
                return True
            if managed.state == "running":
                managed.terminal_override = "cancelled"
            await self._terminate_managed(managed)
            return managed.error != PROCESS_TREE_CLEANUP_ERROR

    async def _spawn(
        self, command: str, *, cwd: Path, env: Mapping[str, str] | None
    ) -> asyncio.subprocess.Process:
        """Spawn while the caller holds the supervisor lock."""

        return await _create_shell_process(command, cwd=cwd, env=env)

    async def _get(self, process_id: str) -> _ManagedProcess:
        async with self._lock:
            self._ensure_open()
            try:
                return self._managed[process_id]
            except KeyError as exc:
                raise ToolError(f"Unknown managed process: {process_id}") from exc

    def _open_saved_stream(self, name: str, command: str, *, spill_threshold: int) -> SavedStream:
        return self._output_store.open_stream(
            f"{name} of `{_command_summary(command)}`",
            spill_threshold=spill_threshold,
        )

    async def _read_stream(
        self,
        managed: _ManagedProcess,
        stream: asyncio.StreamReader | None,
        output: _PendingTextLike,
        saved: SavedStream,
    ) -> None:
        try:
            if stream is None:
                return
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                output.append_bytes(chunk)
                managed.changed.set()
                await saved.append(chunk)
            output.append_bytes(b"", final=True)
            managed.changed.set()
        finally:
            # Reading ends here however it stops: end of file or cancellation.
            saved.close()

    async def _wait_for_completion(self, managed: _ManagedProcess, timeout: float) -> None:
        assert managed.stdout_task is not None
        assert managed.stderr_task is not None
        stream_tasks = (managed.stdout_task, managed.stderr_task)

        def first_reader_failure(results: Iterable[object]) -> BaseException | None:
            return next(
                (
                    result
                    for result in results
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ),
                None,
            )

        def reader_task_failure(task: asyncio.Task[None]) -> BaseException | None:
            if task.cancelled():
                return None
            failure = task.exception()
            if failure is not None and not isinstance(failure, asyncio.CancelledError):
                return failure
            return None

        async def wait_for_reader_failure() -> BaseException | None:
            pending = set(stream_tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                failure = first_reader_failure(reader_task_failure(task) for task in done)
                if failure is not None:
                    return failure
            return None

        async def wait_for_process_and_streams() -> BaseException | None:
            # The stream tasks still wait for end of file. Once termination cancels
            # them, a pipe an escaped process holds must not keep this waiting.
            await _wait_for_exit(managed.process)
            results = await asyncio.gather(*stream_tasks, return_exceptions=True)
            return first_reader_failure(results)

        async def wait_for_process_completion_or_reader_failure() -> BaseException | None:
            process_and_streams = asyncio.create_task(wait_for_process_and_streams())
            reader_failure = asyncio.create_task(wait_for_reader_failure())
            try:
                pending_tasks = {process_and_streams, reader_failure}
                while True:
                    done, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if reader_failure in done:
                        failure = reader_failure.result()
                        if failure is not None:
                            return failure
                    if process_and_streams in done:
                        return process_and_streams.result()
            finally:
                for task in (process_and_streams, reader_failure):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(process_and_streams, reader_failure, return_exceptions=True)

        completion = asyncio.create_task(wait_for_process_completion_or_reader_failure())
        reader_failure: BaseException | None = None
        cleanup_succeeded = True
        try:
            try:
                reader_failure = await asyncio.wait_for(
                    asyncio.shield(completion),
                    timeout=timeout,
                )
            except TimeoutError:
                managed.terminal_override = managed.terminal_override or "timed_out"
                cleanup_succeeded = await self._terminate_managed_tree(managed)
                if cleanup_succeeded:
                    await self._finish_terminated_io(managed)
                    reader_failure = await completion
            if cleanup_succeeded and reader_failure is not None:
                cleanup_succeeded = await self._terminate_managed_tree(managed)
                if cleanup_succeeded:
                    await self._finish_terminated_io(managed)
            # A shell can exit after launching descendants whose output is
            # redirected away from its pipes. The leader and both readers are
            # then finished even though the owned process group is not. Reap
            # that remaining group before publishing a terminal handle.
            elif cleanup_succeeded:
                cleanup_succeeded = await self._terminate_managed_tree(managed)
            managed.exit_code = managed.process.returncode
            if not cleanup_succeeded:
                if reader_failure is not None:
                    managed.error_after_cleanup = "Failed to read process output"
                managed.error = PROCESS_TREE_CLEANUP_ERROR
                managed.state = "failed"
            elif managed.terminal_override is not None:
                managed.state = managed.terminal_override
            elif reader_failure is not None:
                managed.error = "Failed to read process output"
                managed.state = "failed"
            else:
                managed.state = "completed"
            managed.mark_ended()
        finally:
            if not completion.done():
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
            managed.changed.set()
            self._announce_exit(managed)

    def _announce_exit(self, managed: _ManagedProcess) -> None:
        """Tell the exit listener, once, about a process that stopped on its own.

        A cancelled process is announced only when the user stopped it; a caller's
        own cancel already reports the final state to that caller.
        """

        listener = self._exit_listener
        if (
            listener is None
            or managed.exit_announced
            or managed.state == "running"
            or (managed.state == "cancelled" and not managed.announce_cancel)
            or managed.ended_at is None
        ):
            return
        managed.exit_announced = True
        try:
            listener(
                ProcessExit(
                    process_id=managed.process_id,
                    command=managed.command,
                    state=managed.state,
                    exit_code=managed.exit_code if managed.state == "completed" else None,
                    started_at=managed.started_at,
                    ended_at=managed.ended_at,
                    notify=managed.notify_on_exit,
                )
            )
        except Exception:
            # The listener reports to frontends; a failure there must not leave
            # the process's own completion unfinished.
            _logger.exception("Process exit listener failed for %s", managed.process_id)

    async def _finish_terminated_io(self, managed: _ManagedProcess) -> None:
        """Bound pipe draining after the owned process tree has been terminated."""

        await _wait_for_exit(managed.process)
        stream_tasks = tuple(
            task for task in (managed.stdout_task, managed.stderr_task) if task is not None
        )
        if not stream_tasks:
            return
        _, pending = await asyncio.wait(
            stream_tasks,
            timeout=POST_TERMINATION_DRAIN_TIMEOUT,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*stream_tasks, return_exceptions=True)
        if pending:
            _close_process_pipes(managed.process)

    async def _finish_one_shot_capture(
        self,
        process: asyncio.subprocess.Process,
        capture_task: asyncio.Task[Any],
    ) -> None:
        """Bound one-shot pipe draining after the owned process tree has been terminated."""

        await _wait_for_exit(process)
        if not capture_task.done():
            _, pending = await asyncio.wait(
                (capture_task,),
                timeout=POST_TERMINATION_DRAIN_TIMEOUT,
            )
            for task in pending:
                task.cancel()
            if pending:
                _close_process_pipes(process)
        await asyncio.gather(capture_task, return_exceptions=True)

    async def _terminate_managed(self, managed: _ManagedProcess) -> None:
        was_cleanup_failed = managed.error == PROCESS_TREE_CLEANUP_ERROR
        cleanup_succeeded = await self._terminate_managed_tree(managed)
        if cleanup_succeeded:
            await self._finish_terminated_io(managed)
        assert managed.completion_task is not None
        if cleanup_succeeded and not managed.completion_task.cancelled():
            await asyncio.gather(managed.completion_task, return_exceptions=True)
        elif not managed.completion_task.done():
            managed.completion_task.cancel()
            await asyncio.gather(managed.completion_task, return_exceptions=True)

        if not cleanup_succeeded:
            managed.error = PROCESS_TREE_CLEANUP_ERROR
            managed.state = "failed"
            managed.mark_ended()
            managed.changed.set()
        elif was_cleanup_failed:
            recovered_error = managed.error_after_cleanup
            managed.error_after_cleanup = None
            managed.error = recovered_error
            if recovered_error is not None:
                managed.state = "failed"
            elif managed.terminal_override is not None:
                managed.state = managed.terminal_override
            elif managed.process.returncode is not None:
                managed.state = "completed"
            else:
                managed.state = "cancelled"
            managed.changed.set()

    async def _terminate_managed_tree(self, managed: _ManagedProcess) -> bool:
        async with managed.cleanup_lock:
            return await _terminate_process_tree(managed.process)

    def _snapshot(self, managed: _ManagedProcess) -> ProcessUpdate:
        stdout, stdout_dropped, stdout_retained, stdout_source_byte_lengths = managed.stdout.drain()
        stderr, stderr_dropped, stderr_retained, stderr_source_byte_lengths = managed.stderr.drain()
        if managed.state != "running":
            managed.terminal_reported = True
        return ProcessUpdate(
            process_id=managed.process_id,
            state=managed.state,
            stdout=stdout,
            stderr=stderr,
            exit_code=managed.exit_code if managed.state == "completed" else None,
            stdout_truncated=stdout_dropped > 0,
            stderr_truncated=stderr_dropped > 0,
            stdout_dropped_bytes=stdout_dropped,
            stderr_dropped_bytes=stderr_dropped,
            error=managed.error,
            stdout_retained_bytes=stdout_retained,
            stderr_retained_bytes=stderr_retained,
            stdout_source_byte_lengths=stdout_source_byte_lengths,
            stderr_source_byte_lengths=stderr_source_byte_lengths,
            started_at=managed.started_at,
            ended_at=managed.ended_at if managed.state != "running" else None,
            background_stopped=(
                managed.state != "running" and stopped_detached_processes(managed.process)
            ),
            stdout_saved=managed.stdout_saved,
            stderr_saved=managed.stderr_saved,
        )

    def _evict_terminals_for_capacity(self) -> None:
        for process_id, managed in tuple(self._managed.items()):
            if managed.terminal_reported and managed.error != PROCESS_TREE_CLEANUP_ERROR:
                del self._managed[process_id]

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ProcessSupervisor is closed")


async def _sample_progress(
    progress: ToolProgress, stdout: _StreamTail, stderr: _StreamTail
) -> None:
    """Report a running command's output whenever it has grown since the last sample."""

    reported: tuple[int, int] = (0, 0)
    while True:
        await asyncio.sleep(_PROGRESS_SAMPLE_SECONDS)
        totals = (stdout.total_bytes, stderr.total_bytes)
        if totals == reported:
            continue
        reported = totals
        progress.report(
            OutputProgress(
                stdout_tail=stdout.text_tail(_PROGRESS_TAIL_BYTES),
                stderr_tail=stderr.text_tail(_PROGRESS_TAIL_BYTES),
                stdout_bytes=totals[0],
                stderr_bytes=totals[1],
            )
        )


@dataclass(frozen=True, slots=True)
class _OneShotHandover:
    """A running foreground command and what it owns, as a move to the background takes it."""

    process: asyncio.subprocess.Process
    command: str
    capture: asyncio.Task[_CapturedOutput]
    readers: tuple[asyncio.Task[None], asyncio.Task[None]]
    stdout_tail: _StreamTail
    stderr_tail: _StreamTail
    started_at: datetime
    started_monotonic: float


def _split_incomplete_utf8(data: bytes) -> tuple[bytes, bytes]:
    """Split off a UTF-8 character cut short at the end, if any."""

    start = len(data)
    # A character is at most four bytes: look back past up to three continuation bytes.
    while start > 0 and len(data) - start < 3 and data[start - 1] & 0xC0 == 0x80:
        start -= 1
    if start == 0:
        return data, b""
    lead = data[start - 1]
    if lead < 0xC0:
        return data, b""
    needed = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
    if len(data) - (start - 1) >= needed:
        return data, b""
    return data[: start - 1], data[start - 1 :]


def _forward_output(
    managed: _ManagedProcess, output: _PendingTextLike, chunk: bytes, final: bool
) -> None:
    """Hand a moved command's next output to its managed process, as `_read_stream` does."""

    output.append_bytes(chunk, final=final)
    managed.changed.set()


async def _capture_or_background(
    capture: asyncio.Task[_CapturedOutput],
    readers: tuple[asyncio.Task[None], asyncio.Task[None]],
    background: asyncio.Event | None,
    timeout: float,
) -> _CapturedOutput | None:
    """Wait for capture, its deadline, or a request to move the command to the background.

    Like ``asyncio.wait_for``, capture is cancelled, and waited for, on the deadline
    or when this is cancelled; the readers stop then too, and when capture fails,
    so nothing the cleanup that follows prints is read. Capture finishing wins over
    a move asked for at the same moment, and the readers keep going after a move.

    Args:
        capture (asyncio.Task[_CapturedOutput]): The command's capture.
        readers (tuple[asyncio.Task[None], asyncio.Task[None]]): Its stream readers.
        background (asyncio.Event | None): Set to move the command to the background.
        timeout (float): Seconds to wait.

    Returns:
        _CapturedOutput | None: The captured output, or None when the move was
        asked for first.

    Raises:
        TimeoutError: The deadline passed first.
    """

    requested = asyncio.create_task(background.wait()) if background is not None else None
    waiting: set[asyncio.Task[Any]] = {capture} if requested is None else {capture, requested}
    try:
        done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        capture.cancel()
        await asyncio.gather(capture, return_exceptions=True)
        await _stop_readers(readers)
        raise
    finally:
        if requested is not None:
            requested.cancel()
    if capture in done:
        if capture.cancelled() or capture.exception() is not None:
            await _stop_readers(readers)
        return capture.result()
    if not done:
        capture.cancel()
        await asyncio.gather(capture, return_exceptions=True)
        await _stop_readers(readers)
        raise TimeoutError
    return None


async def _timeout_message(timeout: float, stdout: _StreamTail, stderr: _StreamTail) -> str:
    """Describe a one-shot timeout with where its output was saved and how it ended.

    The saved-output notice comes before the tail because error text is shortened
    from the end.
    """

    lines = [f"Command timed out after {timeout:g} seconds."]
    statuses = [
        (name, await tail.saved.save())
        for name, tail in (("stdout", stdout), ("stderr", stderr))
        if tail.saved is not None
    ]
    if notice := saved_output_notice(statuses):
        lines.append(notice)
    parts = _process_output_parts(
        stdout.result().output.decode("utf-8", errors="replace"),
        stderr.result().output.decode("utf-8", errors="replace"),
    )
    if parts:
        tail = truncate_text_tail(
            "\n".join(parts),
            max_bytes=_TIMEOUT_TAIL_BYTES,
            max_lines=_TIMEOUT_TAIL_LINES,
        )
        lines.append(f"Last output:\n{tail.text}")
    return "\n".join(lines)


def _bounded_text_tail(text: str, *, max_bytes: int, max_lines: int) -> tuple[str, int]:
    encoded = text.encode("utf-8")
    if max_bytes <= 0 or max_lines <= 0:
        return "", len(encoded)

    lines = text.splitlines(keepends=True)
    bounded = "".join(lines[-max_lines:]) if len(lines) > max_lines else text
    bounded_bytes = bounded.encode("utf-8")
    if len(bounded_bytes) > max_bytes:
        bounded_bytes = bounded_bytes[-max_bytes:]
        while bounded_bytes:
            try:
                bounded = bounded_bytes.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                bounded_bytes = bounded_bytes[exc.start + 1 :]
        else:
            bounded = ""

    kept_bytes = len(bounded.encode("utf-8"))
    return bounded, len(encoded) - kept_bytes


__all__ = [
    "DEFAULT_MAX_MANAGED_PROCESSES",
    "DEFAULT_MAX_RETAINED_BYTES",
    "DEFAULT_MAX_RETAINED_LINES",
    "ProcessState",
    "ProcessSupervisor",
    "ProcessUpdate",
]


_COMMAND_SUMMARY_CHARS = 80


def _command_summary(command: str) -> str:
    """Return a command on one line, shortened to fit a list of processes."""

    one_line = " ".join(command.split())
    if len(one_line) <= _COMMAND_SUMMARY_CHARS:
        return one_line
    return f"{one_line[: _COMMAND_SUMMARY_CHARS - 3]}..."
