"""Full copies of tool output whose model-facing result was truncated.

A tool result shows the model a bounded part of what a command printed. When that
bound cuts output away, the full stream is saved here so the model can page through
it with ``read_output`` instead of rerunning the command.

Each stream buffers in memory until it outgrows its spill threshold or its owner
asks for it to be saved; only then does it create a file. Saved output belongs to
one Wisp runtime: it lives in a private directory under ``~/.wisp/tool-output/``
that ``aclose`` removes. A live runtime holds a lock in its directory, and the first
runtime to save output removes any directory whose lock it can take, so a crashed
runtime's output does not accumulate.

File work runs in a worker thread so it does not block the event loop. The stream
and store locks are threading locks for that reason. Lock order is stream, then
store: the store never takes a stream lock, so eviction only changes state the
store lock guards.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import secrets
import shutil
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio

from wisp.tools.result import ToolError

_logger = logging.getLogger(__name__)

MAX_SAVED_STREAM_BYTES: Final = 8 * 1024 * 1024
"""Bytes saved from one stream. Later output is counted but not saved."""
MAX_SAVED_TOTAL_BYTES: Final = 64 * 1024 * 1024
"""Bytes saved across a runtime. The oldest finished output is evicted first."""
MAX_SAVED_OUTPUTS: Final = 32
"""Saved streams kept at once. The oldest finished output is evicted first."""

_WRITE_BATCH_BYTES: Final = 64 * 1024
_PRIVATE_DIR_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_LOCK_FILENAME: Final = ".lock"
_RUNTIME_DIR_PREFIX: Final = "runtime-"
# A runtime creates its directory and then its lock, so a directory without a lock
# is only stale once it is older than that gap could plausibly be.
_UNLOCKED_DIRECTORY_GRACE_SECONDS: Final = 60.0
_OUTPUT_ID_BYTES: Final = 8


def default_output_root() -> Path:
    """Return the directory that holds every runtime's saved output.

    Returns:
        Path: ``~/.wisp/tool-output``.
    """

    return Path.home() / ".wisp" / "tool-output"


@dataclass(frozen=True, slots=True)
class SavedOutput:
    """A stream saved in the store and readable with ``read_output``.

    Attributes:
        output_id (str): Opaque id the model passes to ``read_output``.
        saved_bytes (int): Bytes on disk.
        line_count (int): Lines on disk, counting a final unterminated line.
        omitted_bytes (int): Bytes the stream produced past the saving limits.
        complete (bool): Whether the stream has ended.
    """

    output_id: str
    saved_bytes: int
    line_count: int
    omitted_bytes: int
    complete: bool


@dataclass(frozen=True, slots=True)
class UnsavedOutput:
    """A stream that needed saving but could not be saved.

    Attributes:
        reason (str): Why saving failed, safe to show the model.
    """

    reason: str


SaveStatus = SavedOutput | UnsavedOutput


@dataclass(frozen=True, slots=True)
class OutputPage:
    """Lines read from one saved stream.

    Attributes:
        lines (tuple[str, ...]): The selected lines, without line endings.
        offset (int): 1-based number of the first selected line.
        line_count (int): Lines saved when the page was read.
        label (str): What the stream is, such as ``stdout of `make test```.
        omitted_bytes (int): Bytes the stream produced past the saving limits.
        complete (bool): Whether the stream had ended when the page was read.
        line_truncated (bool): Whether a selected line was shortened to fit.
    """

    lines: tuple[str, ...]
    offset: int
    line_count: int
    label: str
    omitted_bytes: int
    complete: bool
    line_truncated: bool

    @property
    def next_offset(self) -> int | None:
        """Return the offset that continues this page, or None at the end."""

        following = self.offset + len(self.lines)
        return following if following <= self.line_count else None


def saved_output_notice(streams: Sequence[tuple[str, SaveStatus | None]]) -> str | None:
    """Tell the model, in one line, where truncated output was saved.

    Args:
        streams (Sequence[tuple[str, SaveStatus | None]]): Each stream's name, such
            as ``stdout``, with its status. Streams with no status are left out.

    Returns:
        str | None: The notice, or None when no stream needed saving.
    """

    saved: list[str] = []
    unsaved: list[str] = []
    for name, status in streams:
        if isinstance(status, SavedOutput):
            size = f"{status.saved_bytes} bytes, {status.line_count} lines"
            if status.omitted_bytes:
                size = f"{size}; {status.omitted_bytes} later bytes not saved"
            if not status.complete:
                size = f"{size} so far"
            saved.append(f"{name} output_id={status.output_id} ({size})")
        elif isinstance(status, UnsavedOutput):
            unsaved.append(f"The full {name} could not be saved: {status.reason}.")
    sentences: list[str] = []
    if saved:
        sentences.append(f"Full output saved: {'; '.join(saved)}.")
    sentences.extend(unsaved)
    if saved:
        sentences.append("Read it with read_output.")
    return f"[{' '.join(sentences)}]" if sentences else None


class _SaveFailed(Exception):
    """Saving a stream failed for a reason that is safe to show the model."""


class OutputStore:
    """Own the saved output of one Wisp runtime."""

    def __init__(self, root: Path | None = None) -> None:
        """Create a store; nothing touches disk until output is saved.

        Args:
            root (Path | None, optional): Directory shared by every runtime's saved
                output. Defaults to ``default_output_root()``.
        """

        self._root = root if root is not None else default_output_root()
        self._lock = threading.Lock()
        self._runtime_dir: Path | None = None
        self._lock_fd: int | None = None
        # Insertion order is age order, which eviction relies on.
        self._saved: OrderedDict[str, SavedStream] = OrderedDict()
        # Bytes each saved stream has claimed, including a write still in progress.
        self._claimed: dict[str, int] = {}
        self._total_bytes = 0
        self._closed = False

    def open_stream(self, label: str, *, spill_threshold: int) -> SavedStream:
        """Start a stream that is saved once it outgrows ``spill_threshold`` bytes.

        Args:
            label (str): What the stream is, shown when it is read.
            spill_threshold (int): Bytes buffered in memory before a file is created.

        Returns:
            SavedStream: The stream. Its owner feeds, saves, closes, or discards it.
        """

        return SavedStream(self, label=label, spill_threshold=spill_threshold)

    async def read_page(
        self,
        output_id: str,
        *,
        offset: int,
        limit: int | None,
        max_bytes: int,
        max_lines: int,
    ) -> OutputPage:
        """Read lines from a saved stream.

        Output still buffered by a running stream is written first, so a page shows
        everything the stream has produced so far.

        Args:
            output_id (str): Id reported when the stream was saved.
            offset (int): 1-based number of the first line to return.
            limit (int | None): Most lines to return, or None for as many as fit.
            max_bytes (int): UTF-8 byte budget for the returned lines.
            max_lines (int): Line budget for the returned lines.

        Returns:
            OutputPage: The selected lines and the stream's current totals.

        Raises:
            ToolError: The id is unknown, the output was evicted or lost, or
                ``offset`` is past the end.
        """

        with self._lock:
            stream = self._saved.get(output_id)
        if stream is None:
            raise ToolError(
                f"Unknown output_id: {output_id}",
                failure_code="not_found",
                recovery_hint=(
                    "Saved output lasts only while this Wisp process runs, and the oldest "
                    "is removed once the store is full. Rerun the command if you need it."
                ),
            )
        return await _run_in_thread(
            lambda: stream._read_page(
                offset=offset,
                limit=limit,
                max_bytes=max_bytes,
                max_lines=max_lines,
            )
        )

    async def aclose(self) -> None:
        """Delete this runtime's saved output and release its directory lock.

        Idempotent. Streams still open stop saving.
        """

        await _run_in_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            self._closed = True
            for stream in self._saved.values():
                stream._evicted = True
            self._saved.clear()
            self._claimed.clear()
            self._total_bytes = 0
            runtime_dir = self._runtime_dir
            lock_fd = self._lock_fd
            self._runtime_dir = None
            self._lock_fd = None
        if lock_fd is not None:
            os.close(lock_fd)
        if runtime_dir is not None:
            shutil.rmtree(runtime_dir, ignore_errors=True)

    def _create_file(self, stream: SavedStream) -> tuple[int, Path]:
        """Create a stream's file and register it. Called with the stream lock held."""

        with self._lock:
            if self._closed:
                raise _SaveFailed("the output store is closed")
            runtime_dir = self._ensure_runtime_dir()
            if len(self._saved) >= MAX_SAVED_OUTPUTS:
                self._evict(lambda: len(self._saved) < MAX_SAVED_OUTPUTS)
            if len(self._saved) >= MAX_SAVED_OUTPUTS:
                raise _SaveFailed(
                    f"{MAX_SAVED_OUTPUTS} outputs are already saved and still being written"
                )
            path = runtime_dir / f"{stream.output_id}.log"
            try:
                fd = os.open(path, _new_file_flags(os.O_WRONLY), _PRIVATE_FILE_MODE)
            except OSError as exc:
                raise _SaveFailed(_os_error_reason(exc)) from exc
            self._saved[stream.output_id] = stream
            self._claimed[stream.output_id] = 0
            return fd, path

    def _reserve(self, stream: SavedStream, size: int) -> int:
        """Return how many of ``size`` bytes fit in the store, and claim them."""

        with self._lock:
            # Closing the store and evicting the stream both drop its ledger entry.
            if stream.output_id not in self._claimed:
                return 0
            self._evict(
                lambda: self._total_bytes + size <= MAX_SAVED_TOTAL_BYTES,
                keep=stream,
            )
            granted = max(0, min(size, MAX_SAVED_TOTAL_BYTES - self._total_bytes))
            self._claimed[stream.output_id] += granted
            self._total_bytes += granted
            return granted

    def _release(self, stream: SavedStream) -> None:
        """Forget a stream that is no longer saved and return the bytes it claimed."""

        with self._lock:
            if self._saved.get(stream.output_id) is stream:
                self._forget(stream.output_id)

    def _mark_reported(self, stream: SavedStream) -> None:
        """Protect a stream whose id a result has given out from eviction while it runs."""

        with self._lock:
            stream._reported = True

    def _evict(self, satisfied: Callable[[], bool], *, keep: SavedStream | None = None) -> None:
        """Evict the oldest evictable streams until ``satisfied`` holds or none remain.

        A finished stream can go. So can a running one whose id no result has given
        out yet: nothing can refer to it, and its owner stops saving when it notices.
        A running stream the model was told about is kept until it finishes.
        """

        for output_id, stream in list(self._saved.items()):
            if satisfied():
                return
            if stream is keep or (stream._reported and not stream._finished):
                continue
            self._forget(output_id)
            stream._evicted = True
            if stream._path is not None:
                with suppress(FileNotFoundError):
                    stream._path.unlink()

    def _forget(self, output_id: str) -> None:
        del self._saved[output_id]
        self._total_bytes -= self._claimed.pop(output_id)

    def _ensure_runtime_dir(self) -> Path:
        if self._runtime_dir is not None:
            return self._runtime_dir
        try:
            _ensure_private_directory(self._root)
            runtime_dir = Path(tempfile.mkdtemp(prefix=_RUNTIME_DIR_PREFIX, dir=self._root))
            lock_fd = os.open(
                runtime_dir / _LOCK_FILENAME,
                _new_file_flags(os.O_RDWR),
                _PRIVATE_FILE_MODE,
            )
        except OSError as exc:
            raise _SaveFailed(_os_error_reason(exc)) from exc
        if not _try_lock(lock_fd):
            os.close(lock_fd)
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise _SaveFailed("could not lock the saved-output directory")
        self._runtime_dir = runtime_dir
        self._lock_fd = lock_fd
        _remove_abandoned_runtime_dirs(self._root, keep=runtime_dir)
        return runtime_dir


class SavedStream:
    """One stream of output, buffered in memory and saved to a file when needed.

    The stream's owner calls ``append`` for each chunk in order, then ``close`` at end
    of input. ``save`` creates the file early, for output that was truncated without
    outgrowing the spill threshold; ``discard`` drops the stream when no result will
    ever report it.
    """

    def __init__(self, store: OutputStore, *, label: str, spill_threshold: int) -> None:
        self.output_id = secrets.token_hex(_OUTPUT_ID_BYTES)
        self.label = label
        self._store = store
        self._spill_threshold = max(0, spill_threshold)
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._fd: int | None = None
        self._path: Path | None = None
        self._file_id: tuple[int, int] | None = None
        self._saved_bytes = 0
        self._omitted_bytes = 0
        self._newlines = 0
        self._ends_with_newline = True
        self._failure: str | None = None
        self._ended = False
        self._discarded = False
        # Written under the store lock, which eviction holds instead of this stream's.
        self._finished = False
        self._evicted = False
        self._reported = False

    async def append(self, chunk: bytes) -> None:
        """Add the next chunk, writing a batch to disk once one is due.

        Args:
            chunk (bytes): Output in the order the command produced it.
        """

        with self._lock:
            if self._ended or self._discarded or self._failure is not None or self._evicted:
                return
            room = max(0, MAX_SAVED_STREAM_BYTES - self._saved_bytes - len(self._buffer))
            self._buffer += chunk[:room]
            self._omitted_bytes += max(0, len(chunk) - room)
            if self._fd is not None:
                flush_due = len(self._buffer) >= _WRITE_BATCH_BYTES
            else:
                flush_due = len(self._buffer) > self._spill_threshold
        if flush_due:
            await _run_in_thread(self._flush)

    async def save(self) -> SaveStatus | None:
        """Save the stream now, even if it has not outgrown its spill threshold.

        Returns:
            SaveStatus | None: Where the output is saved, why it could not be, or
                None when the stream has produced nothing.
        """

        def save_now() -> None:
            self._flush()
            with self._lock:
                if self._ended:
                    self._finish_file()

        await _run_in_thread(save_now)
        status = self.status()
        if isinstance(status, SavedOutput):
            self._store._mark_reported(self)
        return status

    def close(self) -> None:
        """Mark the end of input and write what is buffered to an open file.

        Synchronous so cancelled readers can call it from ``finally``; it writes at
        most one batch. Output that never needed a file stays buffered for ``save``.
        """

        with self._lock:
            if self._ended:
                return
            self._ended = True
            if self._fd is not None:
                self._write_buffer()
                self._finish_file()

    def discard(self) -> None:
        """Drop the stream and delete its file; no result will report it."""

        with self._lock:
            self._discarded = True
            self._ended = True
            self._buffer.clear()
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if self._path is not None:
                with suppress(FileNotFoundError):
                    self._path.unlink()
            self._store._release(self)

    def status(self) -> SaveStatus | None:
        """Return where the stream is saved, why it is not, or None if not needed.

        Returns:
            SaveStatus | None: The current status; None until a file is needed.
        """

        with self._lock:
            if self._failure is not None:
                return UnsavedOutput(self._failure)
            if self._evicted:
                return UnsavedOutput("it was removed to make room for newer output")
            if self._path is None or self._discarded:
                return None
            return SavedOutput(
                output_id=self.output_id,
                saved_bytes=self._saved_bytes,
                line_count=self._line_count(),
                omitted_bytes=self._omitted_bytes,
                complete=self._ended,
            )

    def _flush(self) -> None:
        """Write buffered bytes, creating the file if this is the first write."""

        with self._lock:
            if self._discarded or self._failure is not None or not self._buffer:
                return
            if self._fd is None:
                if self._path is not None:
                    return
                try:
                    self._fd, self._path = self._store._create_file(self)
                    info = os.fstat(self._fd)
                    self._file_id = (info.st_dev, info.st_ino)
                except (_SaveFailed, OSError) as exc:
                    self._fail(str(exc) if isinstance(exc, _SaveFailed) else _os_error_reason(exc))
                    return
            self._write_buffer()

    def _write_buffer(self) -> None:
        """Write the buffer to the open file. Called with the stream lock held."""

        assert self._fd is not None
        granted = self._store._reserve(self, len(self._buffer))
        pending = bytes(self._buffer[:granted])
        self._omitted_bytes += len(self._buffer) - granted
        self._buffer.clear()
        if not pending:
            return
        try:
            _write_all(self._fd, pending)
        except OSError as exc:
            self._fail(_os_error_reason(exc))
            return
        self._saved_bytes += len(pending)
        self._newlines += pending.count(b"\n")
        self._ends_with_newline = pending.endswith(b"\n")

    def _finish_file(self) -> None:
        """Close the file of a stream whose input has ended."""

        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None
        with self._store._lock:
            self._finished = True

    def _fail(self, reason: str) -> None:
        """Stop saving after an error, deleting the partial file."""

        self._buffer.clear()
        self._failure = reason
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._path is not None:
            with suppress(FileNotFoundError):
                self._path.unlink()
        self._store._release(self)

    def _line_count(self) -> int:
        if self._saved_bytes == 0:
            return 0
        return self._newlines + (0 if self._ends_with_newline else 1)

    def _read_page(
        self,
        *,
        offset: int,
        limit: int | None,
        max_bytes: int,
        max_lines: int,
    ) -> OutputPage:
        with self._lock:
            if self._fd is not None:
                self._write_buffer()
            if self._evicted or self._path is None or self._failure is not None:
                raise ToolError(
                    f"Saved output {self.output_id} is no longer available",
                    failure_code="not_found",
                )
            path, file_id = self._path, self._file_id
            size = self._saved_bytes
            line_count = self._line_count()
            omitted_bytes = self._omitted_bytes
            complete = self._ended
        if offset > max(line_count, 1):
            raise ToolError(
                f"read_output.offset {offset} is beyond the end of the output ({line_count} lines)",
                failure_code="invalid_arguments",
                retryable=True,
                recovery_hint=f"Use an offset from 1 to {max(line_count, 1)}.",
            )
        content = _read_saved_file(path, file_id, size)
        lines, line_truncated = _select_lines(
            content,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            max_lines=max_lines,
        )
        return OutputPage(
            lines=lines,
            offset=offset,
            line_count=line_count,
            label=self.label,
            omitted_bytes=omitted_bytes,
            complete=complete,
            line_truncated=line_truncated,
        )


def _read_saved_file(path: Path, file_id: tuple[int, int] | None, size: int) -> bytes:
    """Read a saved file, refusing one that was replaced since it was created."""

    try:
        fd = os.open(path, _read_flags())
    except OSError as exc:
        raise ToolError("Saved output is no longer available", failure_code="not_found") from exc
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != file_id or not stat.S_ISREG(info.st_mode):
            raise ToolError("Saved output was replaced and cannot be read")
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


_LINE_TRUNCATED_MARKER: Final = " [line truncated]"


def _select_lines(
    content: bytes,
    *,
    offset: int,
    limit: int | None,
    max_bytes: int,
    max_lines: int,
) -> tuple[tuple[str, ...], bool]:
    """Pick lines from ``offset`` within the budgets, always making progress."""

    raw_lines = content.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    wanted = raw_lines[offset - 1 :]
    line_budget = max(1, max_lines if limit is None else min(limit, max_lines))
    selected: list[str] = []
    used_bytes = 0
    line_truncated = False
    for raw_line in wanted[:line_budget]:
        line = raw_line.removesuffix(b"\r").decode("utf-8", errors="replace")
        line_bytes = len(line.encode("utf-8")) + 1
        if used_bytes + line_bytes <= max_bytes:
            selected.append(line)
            used_bytes += line_bytes
            continue
        if not selected:
            # A single line larger than the budget is shortened rather than skipped,
            # so the next offset always moves forward.
            room = max(0, max_bytes - len(_LINE_TRUNCATED_MARKER.encode("utf-8")) - 1)
            shortened = line.encode("utf-8")[:room].decode("utf-8", errors="ignore")
            selected.append(f"{shortened}{_LINE_TRUNCATED_MARKER}")
            line_truncated = True
        break
    return tuple(selected), line_truncated


def _remove_abandoned_runtime_dirs(root: Path, *, keep: Path) -> None:
    """Delete runtime directories whose owning process is gone.

    Best effort: a directory that cannot be inspected or removed is left for a
    later runtime, and saving output proceeds either way.
    """

    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        _logger.debug("Could not list saved-output directory %s: %s", root, exc)
        return
    for entry in entries:
        if not entry.name.startswith(_RUNTIME_DIR_PREFIX) or entry.path == str(keep):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if _runtime_dir_is_abandoned(Path(entry.path)):
                shutil.rmtree(entry.path)
        except OSError as exc:
            _logger.debug("Could not remove saved output in %s: %s", entry.path, exc)


def _runtime_dir_is_abandoned(runtime_dir: Path) -> bool:
    try:
        fd = os.open(runtime_dir / _LOCK_FILENAME, _read_flags(os.O_RDWR))
    except FileNotFoundError:
        age = time.time() - runtime_dir.lstat().st_mtime
        return age > _UNLOCKED_DIRECTORY_GRACE_SECONDS
    try:
        return _try_lock(fd)
    finally:
        # Closing the descriptor releases the lock before the directory is removed.
        os.close(fd)


def _try_lock(fd: int) -> bool:
    """Take an exclusive lock on ``fd`` without waiting; return whether it was taken."""

    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK}:
            return False
        raise
    return True


def _ensure_private_directory(path: Path) -> None:
    """Create ``path`` as a directory only the current user can open."""

    path.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ENOTDIR, "the saved-output location is not a directory")
    if os.name != "posix":
        return
    if info.st_uid != os.getuid():
        raise OSError(errno.EPERM, "the saved-output directory belongs to another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        path.chmod(_PRIVATE_DIR_MODE)


def _new_file_flags(access: int) -> int:
    flags = access | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _read_flags(access: int = os.O_RDONLY) -> int:
    flags = access | getattr(os, "O_BINARY", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError(errno.EIO, "a write made no progress")
        view = view[written:]


def _os_error_reason(exc: OSError) -> str:
    # strerror names the failure without exposing the file path.
    return exc.strerror or type(exc).__name__


async def _run_in_thread[T](function: Callable[[], T]) -> T:
    """Run file work in a worker thread and wait for it even if the caller is cancelled.

    The work changes a stream's file state. Returning before it finished would let a
    cancelled reader close the file while the thread still writes to it.
    """

    future = asyncio.get_running_loop().run_in_executor(None, function)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
        raise
