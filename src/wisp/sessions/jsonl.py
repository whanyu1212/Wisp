"""Append-only JSONL session persistence."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

import anyio

from wisp.agent.messages import Message
from wisp.events import JsonObject, KnownWispEvent, WispEvent
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    session_entry_from_json,
    typed_event_from_envelope,
)
from wisp.sessions.errors import MalformedSessionEntryError, SessionError
from wisp.sessions.replay import (
    SessionReplay,
    StaleCompactionError,
    replay_session_entries,
)

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_FileSignature = tuple[int, int, int, int]

__all__ = [
    "AmbiguousSessionError",
    "JsonlSession",
    "JsonlSessionStore",
    "SessionError",
    "SessionNotFoundError",
    "StaleCompactionError",
]


class _SessionFileState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0


_SESSION_FILE_STATES_GUARD = threading.Lock()
_SESSION_FILE_STATES: WeakValueDictionary[Path, _SessionFileState] = WeakValueDictionary()


def _session_file_state(path: Path) -> _SessionFileState:
    key = Path(os.path.abspath(path))
    with _SESSION_FILE_STATES_GUARD:
        state = _SESSION_FILE_STATES.get(key)
        if state is None:
            state = _SessionFileState()
            _SESSION_FILE_STATES[key] = state
        return state


class SessionNotFoundError(SessionError):
    """Raised when a requested session cannot be found."""


class AmbiguousSessionError(SessionError):
    """Raised when a session reference matches more than one session."""


class JsonlSessionStore:
    """Creates and opens JSONL-backed Wisp sessions."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self) -> JsonlSession:
        session_id = uuid4().hex
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = self.root / f"{timestamp}-{session_id[:8]}.jsonl"
        return JsonlSession(session_id=session_id, path=path)

    def load(self, reference: str | Path) -> JsonlSession:
        """Open a session by JSONL path, filename, full id, or id prefix."""

        path = self._resolve_path(reference)
        return JsonlSession(session_id=_read_session_id(path), path=path)

    def latest(self) -> JsonlSession:
        """Open the newest session file in the store."""

        files = list(self._session_files())
        if not files:
            raise SessionNotFoundError(f"No sessions found in {self.root}")
        path = max(files, key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name))
        return JsonlSession(session_id=_read_session_id(path), path=path)

    def _resolve_path(self, reference: str | Path) -> Path:
        selected = Path(reference).expanduser()
        direct_candidates = [selected]
        if not selected.is_absolute():
            direct_candidates.append(self.root / selected)
        for candidate in direct_candidates:
            if candidate.is_file():
                return candidate.resolve(strict=False)

        ref_text = str(reference)
        matches = [path for path in self._session_files() if _matches_reference(path, ref_text)]
        if not matches:
            raise SessionNotFoundError(f"Session not found: {reference}")
        if len(matches) > 1:
            matched = ", ".join(path.name for path in matches[:5])
            suffix = "..." if len(matches) > 5 else ""
            raise AmbiguousSessionError(
                f"Session reference is ambiguous: {reference} ({matched}{suffix})"
            )
        return matches[0]

    def _session_files(self) -> tuple[Path, ...]:
        if not self.root.exists():
            return ()
        return tuple(sorted(self.root.glob("*.jsonl"), key=lambda path: path.name))


class JsonlSession:
    """A single append-only JSONL session file."""

    def __init__(self, *, session_id: str, path: Path) -> None:
        self.session_id = session_id
        self.path = path
        self._append_lock = anyio.Lock()
        self._file_state = _session_file_state(path)
        self._entry_index: dict[str, SessionEntry] | None = None
        self._entry_index_generation: int | None = None
        self._entry_index_signature: _FileSignature | None = None

    async def append_message(
        self,
        message: Message,
        *,
        operation_id: str | None = None,
    ) -> SessionEntry:
        entry = MessageSessionEntry(
            session_id=self.session_id,
            message=message,
            operation_id=operation_id,
        )
        return await self.append_entry(entry)

    async def append_event(
        self,
        event: WispEvent,
        *,
        operation_id: str | None = None,
    ) -> SessionEntry:
        """Persist a structured runtime event for audit/debugging."""

        entry = EventSessionEntry(
            session_id=self.session_id,
            event=PersistedEventEnvelope(payload=event.model_dump(mode="json")),
            operation_id=operation_id,
        )
        return await self.append_entry(entry)

    async def append_entry(self, entry: SessionEntry) -> SessionEntry:
        """Persist a prebuilt entry once, keyed by its stable entry id."""

        if entry.session_id != self.session_id:
            raise SessionError(
                f"Session entry belongs to {entry.session_id}, not {self.session_id}"
            )
        async with self._append_lock:
            await anyio.to_thread.run_sync(self._append_entry_once, entry)
        return entry

    async def append_compaction_entry(
        self,
        entry: SessionEntry,
        *,
        expected_context_entry_ids: Sequence[str],
    ) -> SessionEntry:
        """Atomically append a compaction if its planned context is still active."""

        if entry.session_id != self.session_id:
            raise SessionError(
                f"Session entry belongs to {entry.session_id}, not {self.session_id}"
            )
        if not isinstance(entry, CompactionSessionEntry):
            raise SessionError("Atomic compaction append requires a compaction entry")
        expected = tuple(expected_context_entry_ids)
        async with self._append_lock:
            await anyio.to_thread.run_sync(
                self._append_compaction_entry_once,
                entry,
                expected,
            )
        return entry

    async def truncate_entries(self, count: int) -> None:
        """Remove entries after count, preserving the first count entries."""

        if count < 0:
            raise ValueError("Session entry count cannot be negative")
        async with self._append_lock:
            await anyio.to_thread.run_sync(self._truncate_entries_once, count)

    async def truncate_operation_entries(self, count: int, *, operation_id: str) -> bool:
        """Truncate an owned suffix only if no other writer appended within it."""

        if count < 0:
            raise ValueError("Session entry count cannot be negative")
        async with self._append_lock:
            return await anyio.to_thread.run_sync(
                self._truncate_operation_entries_once,
                count,
                operation_id,
            )

    def read_entries(self) -> tuple[SessionEntry, ...]:
        """Read all persisted entries from the session file."""

        return tuple(_read_entries(self.path))

    def read_messages(self) -> tuple[Message, ...]:
        """Read all persisted messages from the session file."""

        return tuple(
            entry.message for entry in self.read_entries() if isinstance(entry, MessageSessionEntry)
        )

    def read_context(self) -> SessionReplay:
        """Replay the active provider context while preserving durable entry ids."""

        return replay_session_entries(self.read_entries())

    def read_context_messages(self) -> tuple[Message, ...]:
        """Read only the messages in the active replay context."""

        return self.read_context().messages

    def read_events(self) -> tuple[JsonObject, ...]:
        """Read all persisted structured events from the session file."""

        return tuple(
            entry.event.payload
            for entry in self.read_entries()
            if isinstance(entry, EventSessionEntry)
        )

    def read_typed_events(self) -> tuple[KnownWispEvent, ...]:
        """Validate retained raw events through the supported event schemas."""

        return tuple(
            typed_event_from_envelope(
                entry.event,
                source=f"{self.path} entry {entry.id}",
            )
            for entry in self.read_entries()
            if isinstance(entry, EventSessionEntry)
        )

    def _append_line(self, line: str) -> None:
        _ensure_private_directory(self.path.parent)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, PRIVATE_FILE_MODE)
        try:
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8") as session_file:
                fd = -1
                session_file.write(line)
                session_file.write("\n")
        finally:
            if fd != -1:
                os.close(fd)

    def _append_entry_once(self, entry: SessionEntry) -> None:
        with self._file_state.lock:
            with self._interprocess_lock():
                self._append_entry_locked(entry)

    def _append_compaction_entry_once(
        self,
        entry: SessionEntry,
        expected_context_entry_ids: tuple[str, ...],
    ) -> None:
        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                if self._entry_is_persisted_locked(entry):
                    return
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                replay = replay_session_entries(entries)
                if replay.context_entry_ids != expected_context_entry_ids:
                    raise StaleCompactionError(
                        "Compaction plan is stale: expected context entry ids "
                        f"{expected_context_entry_ids}, found {replay.context_entry_ids}"
                    )
                replay_session_entries((*entries, entry))
                self._append_entry_locked(entry)

    def _append_entry_locked(self, entry: SessionEntry) -> None:
        _ensure_private_directory(self.path.parent)
        self._refresh_entry_index()
        if self._entry_is_persisted_locked(entry):
            return

        try:
            self._append_line(entry.model_dump_json(exclude_none=True))
            info = self._validate_session_file()
            if info is None:
                raise SessionError(f"Session file disappeared after append: {self.path}")
        except Exception:
            self._file_state.generation += 1
            self._invalidate_entry_index()
            raise
        self._file_state.generation += 1
        assert self._entry_index is not None
        self._entry_index[entry.id] = entry
        self._entry_index_generation = self._file_state.generation
        self._entry_index_signature = _session_file_signature(info)

    def _entry_is_persisted_locked(self, entry: SessionEntry) -> bool:
        assert self._entry_index is not None
        existing = self._entry_index.get(entry.id)
        if existing is None:
            return False
        if existing == entry:
            return True
        raise SessionError(f"Session entry id conflicts with persisted data: {entry.id}")

    def _refresh_entry_index(self) -> None:
        info = self._validate_session_file()
        if info is None:
            self._entry_index = {}
            self._entry_index_generation = self._file_state.generation
            self._entry_index_signature = None
            return

        signature = _session_file_signature(info)
        if (
            self._entry_index is None
            or self._entry_index_generation != self._file_state.generation
            or self._entry_index_signature != signature
        ):
            self._entry_index = self._load_entry_index()
            self._entry_index_generation = self._file_state.generation
            self._entry_index_signature = signature

    def _invalidate_entry_index(self) -> None:
        self._entry_index = None
        self._entry_index_generation = None
        self._entry_index_signature = None

    def _load_entry_index(self) -> dict[str, SessionEntry]:
        entries: dict[str, SessionEntry] = {}
        for entry in _read_entries(self.path):
            if entry.id in entries:
                raise SessionError(f"Duplicate session entry id: {entry.id}")
            entries[entry.id] = entry
        return entries

    def _validate_session_file(self) -> os.stat_result | None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SessionError(f"Could not inspect session file: {self.path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SessionError(f"Session file is not a regular file: {self.path}")
        return info

    def _truncate_entries_once(self, count: int) -> None:
        with self._file_state.lock:
            with self._interprocess_lock():
                try:
                    self._truncate_entries(count)
                finally:
                    self._file_state.generation += 1
                    self._invalidate_entry_index()

    def _truncate_operation_entries_once(self, count: int, operation_id: str) -> bool:
        with self._file_state.lock:
            with self._interprocess_lock():
                if not self.path.is_file():
                    return False
                entries = self.read_entries()
                suffix = entries[count:]
                if not suffix or any(entry.operation_id != operation_id for entry in suffix):
                    return False
                try:
                    self._truncate_entries(count)
                finally:
                    self._file_state.generation += 1
                    self._invalidate_entry_index()
                return True

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        """Serialize session mutations across cooperating Wisp processes."""

        _ensure_private_directory(self.path.parent)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, PRIVATE_FILE_MODE)
        unlock: Callable[[], object] | None = None
        try:
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SessionError(f"Session lock is not a regular file: {lock_path}")
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                unlock = partial(fcntl.flock, fd, fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                unlock = partial(
                    msvcrt.locking,  # type: ignore[attr-defined]
                    fd,
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
            yield
        finally:
            if unlock is not None:
                unlock()
            os.close(fd)

    def _truncate_entries(self, count: int) -> None:
        if not self.path.is_file():
            return
        entries = self.read_entries()[:count]
        if not entries:
            self.path.unlink(missing_ok=True)
            return
        self._replace_lines([entry.model_dump_json(exclude_none=True) for entry in entries])

    def _replace_lines(self, lines: list[str]) -> None:
        _ensure_private_directory(self.path.parent)
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, PRIVATE_FILE_MODE)
        try:
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as session_file:
                fd = -1
                for line in lines:
                    session_file.write(line)
                    session_file.write("\n")
        finally:
            if fd != -1:
                os.close(fd)


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            pass
        _validate_private_directory(directory)
    _validate_private_directory(path)


def _validate_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionError(f"Could not inspect session directory: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SessionError(f"Session directory is not a directory: {path}")
    if os.name != "posix":
        return
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise SessionError(f"Session directory is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        path.chmod(PRIVATE_DIR_MODE)
        info = path.lstat()
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SessionError(f"Session directory is not private: {path}")


def _session_file_signature(info: os.stat_result) -> _FileSignature:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _matches_reference(path: Path, reference: str) -> bool:
    if path.name == reference or path.stem == reference:
        return True
    if path.name.startswith(reference) or path.stem.startswith(reference):
        return True
    try:
        session_id = _read_session_id(path)
    except SessionError:
        return False
    return session_id.startswith(reference)


def _read_session_id(path: Path) -> str:
    for entry in _read_entries(path, limit=1):
        return entry.session_id
    raise SessionError(f"Session file is empty: {path}")


def _read_entries(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
    if not path.is_file():
        raise SessionNotFoundError(f"Session file does not exist: {path}")

    entries: list[SessionEntry] = []
    session_id: str | None = None
    seen_entry_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                if not line.strip():
                    continue
                source = f"{path}:{line_number}"
                entry = session_entry_from_json(line, source=source)
                if session_id is None:
                    session_id = entry.session_id
                elif entry.session_id != session_id:
                    raise MalformedSessionEntryError(
                        f"Session entry at {source} belongs to {entry.session_id}, "
                        f"expected {session_id}"
                    )
                if entry.id in seen_entry_ids:
                    raise MalformedSessionEntryError(
                        f"Duplicate session entry id {entry.id} at {source}"
                    )
                seen_entry_ids.add(entry.id)
                entries.append(entry)
                if limit is not None and len(entries) >= limit:
                    break
    except UnicodeDecodeError as exc:
        raise SessionError(f"Session file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Could not read session file: {path}") from exc
    return entries
