"""Append-only JSONL session persistence."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import cast
from uuid import uuid4
from weakref import WeakValueDictionary

import anyio

from wisp.agent.messages import Message
from wisp.events import JsonObject, KnownWispEvent, WispEvent
from wisp.sessions.branching import (
    SessionBranchProjection,
    project_fork_from_user_message,
    project_session_path,
)
from wisp.sessions.entries import (
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    SessionTreeEntry,
    is_session_tree_entry,
    session_entry_from_json,
    session_entry_to_json,
    typed_event_from_envelope,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    SessionError,
    StaleSessionTreeError,
    UnsupportedPersistedEventVersionError,
    UnsupportedSessionEntryVersionError,
)
from wisp.sessions.replay import (
    SessionReplay,
    SessionReplayError,
    StaleCompactionError,
    replay_session_entries,
    resolve_session_tree,
)

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_FileSignature = tuple[int, int, int, int]

__all__ = [
    "AmbiguousSessionError",
    "JsonlSession",
    "JsonlSessionStore",
    "SessionForkResult",
    "SessionSummary",
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


@dataclass(frozen=True, slots=True)
class SessionForkResult:
    """A forked session plus the selected user prompt to edit and resubmit."""

    session: JsonlSession
    source_session_id: str
    source_active_leaf_id: str | None
    fork_leaf_id: str | None
    selected_entry_id: str
    selected_prompt: str


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Bounded metadata for listing persisted sessions without loading messages."""

    session_id: str
    path: Path
    updated_at: datetime
    entry_count: int
    active_leaf_id: str | None


@dataclass(frozen=True, slots=True)
class _SessionSummaryEntryMetadata:
    id: str
    session_id: str
    kind: str
    parent_id: str | None = None
    previous_leaf_id: str | None = None
    active_leaf_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SessionSummaryMetadata:
    session_id: str
    entry_count: int
    active_leaf_id: str | None


class JsonlSessionStore:
    """Creates and opens JSONL-backed Wisp sessions."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self) -> JsonlSession:
        session_id = uuid4().hex
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = self.root / f"{timestamp}-{session_id[:8]}.jsonl"
        return JsonlSession(session_id=session_id, path=path)

    async def clone(
        self,
        source: JsonlSession,
        *,
        expected_active_leaf_id: str | None,
    ) -> JsonlSession:
        """Copy the source's complete active branch into a new session."""

        return await anyio.to_thread.run_sync(
            self._clone_once,
            source,
            expected_active_leaf_id,
            None,
        )

    async def clone_to_leaf(
        self,
        source: JsonlSession,
        leaf_id: str,
        *,
        expected_active_leaf_id: str | None,
    ) -> JsonlSession:
        """Copy one explicit source root-to-leaf path into a new session."""

        return await anyio.to_thread.run_sync(
            self._clone_once,
            source,
            expected_active_leaf_id,
            leaf_id,
        )

    async def fork_from_user_message(
        self,
        source: JsonlSession,
        entry_id: str,
        *,
        expected_active_leaf_id: str | None,
    ) -> SessionForkResult:
        """Fork before one user message and return its editable prompt text."""

        return await anyio.to_thread.run_sync(
            self._fork_from_user_message_once,
            source,
            entry_id,
            expected_active_leaf_id,
        )

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

    def summaries(self, limit: int | None = None) -> tuple[SessionSummary, ...]:
        """Return newest-first metadata for persisted sessions."""

        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        files = tuple(
            sorted(
                self._session_files(),
                key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
                reverse=True,
            )
        )
        selected = files[:limit] if limit is not None else files
        return tuple(self._summary_for_path(path) for path in selected)

    def _clone_once(
        self,
        source: JsonlSession,
        expected_active_leaf_id: str | None,
        leaf_id: str | None,
    ) -> JsonlSession:
        entries, active_leaf_id = source._snapshot_entries_for_branch_once(  # noqa: SLF001
            expected_active_leaf_id
        )
        selected_leaf_id = active_leaf_id if leaf_id is None else leaf_id
        if selected_leaf_id is None:
            raise SessionError("Cannot clone an empty session")
        projection = project_session_path(entries, leaf_id=selected_leaf_id)
        return self._persist_projection(projection)

    def _fork_from_user_message_once(
        self,
        source: JsonlSession,
        entry_id: str,
        expected_active_leaf_id: str | None,
    ) -> SessionForkResult:
        entries, source_active_leaf_id = source._snapshot_entries_for_branch_once(  # noqa: SLF001
            expected_active_leaf_id
        )
        projection = project_fork_from_user_message(entries, entry_id=entry_id)
        target = self._persist_projection(projection)
        assert projection.selected_entry_id is not None
        assert projection.selected_prompt is not None
        return SessionForkResult(
            session=target,
            source_session_id=source.session_id,
            source_active_leaf_id=source_active_leaf_id,
            fork_leaf_id=projection.source_leaf_id,
            selected_entry_id=projection.selected_entry_id,
            selected_prompt=projection.selected_prompt,
        )

    def _persist_projection(self, projection: SessionBranchProjection) -> JsonlSession:
        target = self.create()
        if not projection.entries:
            return target
        copied_entries = tuple(
            entry.model_copy(update={"session_id": target.session_id})
            for entry in projection.entries
        )
        target._create_with_entries_once(copied_entries)  # noqa: SLF001
        return target

    def _summary_for_path(self, path: Path) -> SessionSummary:
        info = path.stat()
        metadata = _read_session_summary_metadata(path)
        return SessionSummary(
            session_id=metadata.session_id,
            path=path.resolve(strict=False),
            updated_at=datetime.fromtimestamp(info.st_mtime, UTC),
            entry_count=metadata.entry_count,
            active_leaf_id=metadata.active_leaf_id,
        )

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
            return await anyio.to_thread.run_sync(self._append_entry_once, entry)

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
            return await anyio.to_thread.run_sync(
                self._append_compaction_entry_once,
                entry,
                expected,
            )

    async def select_active_leaf(
        self,
        active_leaf_id: str | None,
        *,
        expected_active_leaf_id: str | None,
        operation_id: str | None = None,
    ) -> ActiveLeafSessionEntry:
        """Atomically select an existing tree node through append-only state."""

        entry = ActiveLeafSessionEntry(
            session_id=self.session_id,
            previous_leaf_id=expected_active_leaf_id,
            active_leaf_id=active_leaf_id,
            operation_id=operation_id,
        )
        persisted = await self.append_entry(entry)
        assert isinstance(persisted, ActiveLeafSessionEntry)
        return persisted

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

    async def restore_active_leaf_for_operation(
        self,
        count: int,
        active_leaf_id: str | None,
        *,
        operation_id: str,
    ) -> bool:
        """Restore a run's starting leaf if its complete suffix is still owned."""

        if count < 0:
            raise ValueError("Session entry count cannot be negative")
        async with self._append_lock:
            return await anyio.to_thread.run_sync(
                self._restore_active_leaf_for_operation_once,
                count,
                active_leaf_id,
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

    def read_active_leaf_id(self) -> str | None:
        """Read the append-only selected leaf for subsequent replay and appends."""

        return resolve_session_tree(self.read_entries()).active_leaf_id

    def read_active_path(self) -> tuple[SessionEntry, ...]:
        """Read the selected root-to-leaf tree path, excluding state records."""

        return tuple(resolve_session_tree(self.read_entries()).active_path)

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

    def _snapshot_entries_for_branch_once(
        self,
        expected_active_leaf_id: str | None,
    ) -> tuple[tuple[SessionEntry, ...], str | None]:
        """Read one coherent tree snapshot and reject stale branch requests."""

        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                active_leaf_id = _active_leaf_id(self._entry_index)
                if active_leaf_id != expected_active_leaf_id:
                    raise StaleSessionTreeError(
                        "Session tree changed: expected active leaf "
                        f"{expected_active_leaf_id!r}, found {active_leaf_id!r}"
                    )
                resolve_session_tree(entries)
                return entries, active_leaf_id

    def _create_with_entries_once(self, entries: tuple[SessionTreeEntry, ...]) -> None:
        """Exclusively publish a complete projected session file."""

        with self._file_state.lock:
            with self._interprocess_lock():
                self._create_with_entries_locked(entries)

    def _create_with_entries_locked(self, entries: tuple[SessionTreeEntry, ...]) -> None:
        """Publish projected entries while holding the destination mutation locks."""

        if not entries:
            raise ValueError("Projected session entries cannot be empty")
        if any(entry.session_id != self.session_id for entry in entries):
            raise SessionError("Projected entries do not belong to the target session")
        # Validate both the tree and provider-visible compaction semantics before
        # making the target path discoverable.
        replay_session_entries(entries)
        lines = tuple(session_entry_to_json(entry) for entry in entries)

        _ensure_private_directory(self.path.parent)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temp_path = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        fd = -1
        created_signature: tuple[int, int] | None = None
        published = False
        try:
            fd = os.open(temp_path, flags, PRIVATE_FILE_MODE)
            info = os.fstat(fd)
            created_signature = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode):
                raise SessionError(f"Session file is not a regular file: {temp_path}")
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as session_file:
                fd = -1
                for line in lines:
                    session_file.write(line)
                    session_file.write("\n")

            persisted = tuple(_read_entries(temp_path))
            if persisted != entries:
                raise SessionError(f"Projected session validation failed: {temp_path}")
            # A hard link publishes the already complete inode without replacing
            # any destination another process may have created concurrently.
            os.link(temp_path, self.path)
            published = True
            temp_path.unlink()
        except Exception:
            if fd != -1:
                os.close(fd)
            if created_signature is not None:
                _unlink_if_same_file(temp_path, created_signature)
                if published:
                    _unlink_if_same_file(self.path, created_signature)
            raise

        self._file_state.generation += 1
        self._entry_index = {entry.id: entry for entry in entries}
        self._entry_index_generation = self._file_state.generation
        final_info = self._validate_session_file()
        if final_info is None:
            self._invalidate_entry_index()
            raise SessionError(f"Projected session disappeared after creation: {self.path}")
        if created_signature != (final_info.st_dev, final_info.st_ino):
            self._invalidate_entry_index()
            raise SessionError(f"Projected session was replaced during creation: {self.path}")
        self._entry_index_signature = _session_file_signature(final_info)

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

    def _append_entry_once(self, entry: SessionEntry) -> SessionEntry:
        with self._file_state.lock:
            with self._interprocess_lock():
                return self._append_entry_locked(entry)

    def _append_compaction_entry_once(
        self,
        entry: SessionEntry,
        expected_context_entry_ids: tuple[str, ...],
    ) -> SessionEntry:
        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                existing = self._persisted_entry_locked(entry)
                if existing is not None:
                    return existing
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                replay = replay_session_entries(entries)
                if replay.context_entry_ids != expected_context_entry_ids:
                    raise StaleCompactionError(
                        "Compaction plan is stale: expected context entry ids "
                        f"{expected_context_entry_ids}, found {replay.context_entry_ids}"
                    )
                return self._append_entry_locked(entry)

    def _append_entry_locked(self, entry: SessionEntry) -> SessionEntry:
        _ensure_private_directory(self.path.parent)
        self._refresh_entry_index()
        existing = self._persisted_entry_locked(entry)
        if existing is not None:
            return existing

        assert self._entry_index is not None
        active_leaf_id = _active_leaf_id(self._entry_index)
        if (
            is_session_tree_entry(entry)
            and entry.parent_id is not None
            and entry.parent_id != active_leaf_id
        ):
            raise SessionError(
                f"Session entry {entry.id} specifies parent {entry.parent_id!r}, "
                f"but the active leaf is {active_leaf_id!r}"
            )
        persisted = (
            entry.model_copy(update={"parent_id": active_leaf_id})
            if is_session_tree_entry(entry)
            else entry
        )
        _validate_append_transition(
            persisted,
            entry_index=self._entry_index,
            active_leaf_id=active_leaf_id,
        )
        if isinstance(persisted, CompactionSessionEntry):
            replay_session_entries((*self._entry_index.values(), persisted))

        try:
            self._append_line(session_entry_to_json(persisted))
            info = self._validate_session_file()
            if info is None:
                raise SessionError(f"Session file disappeared after append: {self.path}")
        except Exception:
            self._file_state.generation += 1
            self._invalidate_entry_index()
            raise
        self._file_state.generation += 1
        self._entry_index[persisted.id] = persisted
        self._entry_index_generation = self._file_state.generation
        self._entry_index_signature = _session_file_signature(info)
        return persisted

    def _persisted_entry_locked(self, entry: SessionEntry) -> SessionEntry | None:
        assert self._entry_index is not None
        existing = self._entry_index.get(entry.id)
        if existing is None:
            return None
        if existing == entry or _matches_detached_entry(existing, entry):
            return existing
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

    def _restore_active_leaf_for_operation_once(
        self,
        count: int,
        active_leaf_id: str | None,
        operation_id: str,
    ) -> bool:
        with self._file_state.lock:
            with self._interprocess_lock():
                if not self.path.is_file():
                    return False
                self._refresh_entry_index()
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                suffix = entries[count:]
                if not suffix or any(entry.operation_id != operation_id for entry in suffix):
                    return False
                tree = resolve_session_tree(entries)
                if active_leaf_id is not None and all(
                    entry.id != active_leaf_id for entry in tree.nodes
                ):
                    return False
                if tree.active_leaf_id == active_leaf_id:
                    return True
                self._append_entry_locked(
                    ActiveLeafSessionEntry(
                        session_id=self.session_id,
                        operation_id=operation_id,
                        previous_leaf_id=tree.active_leaf_id,
                        active_leaf_id=active_leaf_id,
                    )
                )
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
        self._replace_lines([session_entry_to_json(entry) for entry in entries])

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


def _unlink_if_same_file(path: Path, expected: tuple[int, int]) -> None:
    """Remove a failed new file only while it is still the inode we created."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == expected:
        try:
            path.unlink()
        except OSError:
            pass


def _session_file_signature(info: os.stat_result) -> _FileSignature:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _active_leaf_id(entry_index: dict[str, SessionEntry]) -> str | None:
    """Read current leaf state from the last validated transition in constant time."""

    if not entry_index:
        return None
    last_entry = entry_index[next(reversed(entry_index))]
    if is_session_tree_entry(last_entry):
        return last_entry.id
    assert isinstance(last_entry, ActiveLeafSessionEntry)
    return last_entry.active_leaf_id


def _validate_append_transition(
    entry: SessionEntry,
    *,
    entry_index: dict[str, SessionEntry],
    active_leaf_id: str | None,
) -> None:
    """Validate one proposed transition against an already validated entry index."""

    if is_session_tree_entry(entry):
        if entry.parent_id != active_leaf_id:
            raise SessionReplayError(
                f"Session entry {entry.id} has parent {entry.parent_id!r}, "
                f"expected active leaf {active_leaf_id!r}"
            )
        return

    assert isinstance(entry, ActiveLeafSessionEntry)
    if entry.previous_leaf_id != active_leaf_id:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} expected previous leaf "
            f"{entry.previous_leaf_id!r}, found {active_leaf_id!r}"
        )
    if entry.active_leaf_id is None:
        return
    target = entry_index.get(entry.active_leaf_id)
    if target is None or not is_session_tree_entry(target):
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} references unknown leaf {entry.active_leaf_id}"
        )


def _matches_detached_entry(existing: SessionEntry, candidate: SessionEntry) -> bool:
    """Compare retry payloads while ignoring the parent assigned during persistence."""

    if not is_session_tree_entry(existing) or not is_session_tree_entry(candidate):
        return False
    if candidate.parent_id is not None:
        return False
    return candidate.model_copy(update={"parent_id": existing.parent_id}) == existing


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


_SESSION_TREE_ENTRY_KINDS = frozenset({"message", "event", "compaction"})


def _read_session_summary_metadata(path: Path) -> _SessionSummaryMetadata:
    if not path.is_file():
        raise SessionNotFoundError(f"Session file does not exist: {path}")

    session_id: str | None = None
    entry_count = 0
    active_leaf_id: str | None = None
    node_ids: set[str] = set()
    seen_entry_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                if not line.strip():
                    continue
                source = f"{path}:{line_number}"
                entry = _summary_entry_metadata_from_json(
                    line,
                    source=source,
                    legacy_parent_id=active_leaf_id,
                )
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
                active_leaf_id = _resolve_summary_entry(
                    entry,
                    active_leaf_id=active_leaf_id,
                    node_ids=node_ids,
                )
                entry_count += 1
    except UnicodeDecodeError as exc:
        raise SessionError(f"Session file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Could not read session file: {path}") from exc
    if session_id is None:
        raise SessionError(f"Session file is empty: {path}")
    return _SessionSummaryMetadata(
        session_id=session_id,
        entry_count=entry_count,
        active_leaf_id=active_leaf_id,
    )


def _summary_entry_metadata_from_json(
    line: str,
    *,
    source: str,
    legacy_parent_id: str | None,
) -> _SessionSummaryEntryMetadata:
    location = f" at {source}"
    try:
        raw_value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise MalformedSessionEntryError(f"Malformed session entry JSON{location}") from exc
    if not isinstance(raw_value, dict):
        raise MalformedSessionEntryError(f"Malformed session entry JSON{location}")
    raw = cast(JsonObject, raw_value)

    if "schema_version" not in raw:
        return _legacy_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)

    version = raw["schema_version"]
    if type(version) is not int:
        raise MalformedSessionEntryError(
            f"Session entry schema_version must be an integer{location}"
        )
    if version == 1:
        return _v1_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version != SESSION_ENTRY_SCHEMA_VERSION:
        raise UnsupportedSessionEntryVersionError(
            f"Unsupported session entry schema_version {version}{location}; "
            f"expected {SESSION_ENTRY_SCHEMA_VERSION}"
        )
    return _v2_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)


def _legacy_summary_entry_metadata(
    raw: JsonObject,
    *,
    location: str,
    parent_id: str | None,
) -> _SessionSummaryEntryMetadata:
    _require_summary_base_fields(raw, location=location)
    kind = raw.get("kind", "message")
    if kind not in _SESSION_TREE_ENTRY_KINDS:
        raise MalformedSessionEntryError(f"Unknown legacy session entry kind {kind!r}{location}")
    populated = tuple(name for name in _SESSION_TREE_ENTRY_KINDS if raw.get(name) is not None)
    if populated != (kind,):
        raise MalformedSessionEntryError(
            f"Legacy {kind} session entries require exactly a {kind} payload{location}"
        )
    return _SessionSummaryEntryMetadata(
        id=_required_summary_string(raw, "id", location=location),
        session_id=_required_summary_string(raw, "session_id", location=location),
        kind=kind,
        parent_id=parent_id,
    )


def _v1_summary_entry_metadata(
    raw: JsonObject,
    *,
    location: str,
    parent_id: str | None,
) -> _SessionSummaryEntryMetadata:
    _require_summary_base_fields(raw, location=location)
    kind = raw.get("kind")
    if kind not in _SESSION_TREE_ENTRY_KINDS:
        raise MalformedSessionEntryError(f"Unknown v1 session entry kind {kind!r}{location}")
    _require_summary_declared_payload(raw, kind, location=location)
    if kind == "event":
        _require_summary_event_envelope(raw, location=location)
    forbidden = tuple(
        field for field in ("parent_id", "previous_leaf_id", "active_leaf_id") if field in raw
    )
    if forbidden:
        fields = ", ".join(forbidden)
        raise MalformedSessionEntryError(
            f"V1 session entry contains v2 structural field(s) {fields}{location}"
        )
    return _SessionSummaryEntryMetadata(
        id=_required_summary_string(raw, "id", location=location),
        session_id=_required_summary_string(raw, "session_id", location=location),
        kind=kind,
        parent_id=parent_id,
    )


def _v2_summary_entry_metadata(
    raw: JsonObject,
    *,
    location: str,
    parent_id: str | None,
) -> _SessionSummaryEntryMetadata:
    _require_summary_base_fields(raw, location=location)
    kind = raw.get("kind")
    if kind in _SESSION_TREE_ENTRY_KINDS:
        _require_summary_declared_payload(raw, kind, location=location)
        if kind == "event":
            _require_summary_event_envelope(raw, location=location)
        return _SessionSummaryEntryMetadata(
            id=_required_summary_string(raw, "id", location=location),
            session_id=_required_summary_string(raw, "session_id", location=location),
            kind=kind,
            parent_id=_optional_summary_string(
                raw,
                "parent_id",
                location=location,
                default=parent_id,
            ),
        )
    if kind == "active_leaf":
        return _SessionSummaryEntryMetadata(
            id=_required_summary_string(raw, "id", location=location),
            session_id=_required_summary_string(raw, "session_id", location=location),
            kind=kind,
            previous_leaf_id=_optional_summary_string(
                raw,
                "previous_leaf_id",
                location=location,
                default=parent_id,
            ),
            active_leaf_id=_optional_summary_string(
                raw,
                "active_leaf_id",
                location=location,
            ),
        )
    raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _require_summary_base_fields(raw: JsonObject, *, location: str) -> None:
    missing = tuple(field for field in ("id", "session_id", "created_at") if field not in raw)
    if missing:
        fields = ", ".join(missing)
        raise MalformedSessionEntryError(
            f"Persisted session entry is missing required field(s) {fields}{location}"
        )


def _require_summary_event_envelope(raw: JsonObject, *, location: str) -> None:
    event = raw.get("event")
    if not isinstance(event, dict):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    version = event.get("schema_version")
    if type(version) is not int:
        raise MalformedPersistedEventError(
            f"Persisted event envelope schema_version must be an integer{location}"
        )
    if version != PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION:
        raise UnsupportedPersistedEventVersionError(
            f"Unsupported persisted event envelope schema_version {version}{location}; "
            f"expected {PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION}"
        )
    if not isinstance(event.get("payload"), dict):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _require_summary_declared_payload(raw: JsonObject, kind: object, *, location: str) -> None:
    assert isinstance(kind, str)
    if not isinstance(raw.get(kind), dict):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _required_summary_string(raw: JsonObject, field: str, *, location: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    return value


def _optional_summary_string(
    raw: JsonObject,
    field: str,
    *,
    location: str,
    default: str | None = None,
) -> str | None:
    value = raw.get(field, default)
    if value is None or isinstance(value, str):
        return value
    raise MalformedSessionEntryError(f"Malformed session entry{location}")


def _resolve_summary_entry(
    entry: _SessionSummaryEntryMetadata,
    *,
    active_leaf_id: str | None,
    node_ids: set[str],
) -> str | None:
    if entry.kind in _SESSION_TREE_ENTRY_KINDS:
        parent_id = entry.parent_id
        if parent_id == entry.id:
            raise SessionReplayError(f"Session entry {entry.id} cannot parent itself")
        if parent_id is not None and parent_id not in node_ids:
            raise SessionReplayError(
                f"Session entry {entry.id} references unknown parent {parent_id}"
            )
        if parent_id != active_leaf_id:
            raise SessionReplayError(
                f"Session entry {entry.id} has parent {parent_id!r}, "
                f"expected active leaf {active_leaf_id!r}"
            )
        node_ids.add(entry.id)
        return entry.id
    if entry.previous_leaf_id != active_leaf_id:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} expected previous leaf "
            f"{entry.previous_leaf_id!r}, found {active_leaf_id!r}"
        )
    if entry.active_leaf_id is not None and entry.active_leaf_id not in node_ids:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} references unknown leaf {entry.active_leaf_id}"
        )
    return entry.active_leaf_id


def _read_entries(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
    if not path.is_file():
        raise SessionNotFoundError(f"Session file does not exist: {path}")

    entries: list[SessionEntry] = []
    session_id: str | None = None
    active_leaf_id: str | None = None
    seen_entry_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                if not line.strip():
                    continue
                source = f"{path}:{line_number}"
                entry = session_entry_from_json(
                    line,
                    source=source,
                    legacy_parent_id=active_leaf_id,
                )
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
                if is_session_tree_entry(entry):
                    active_leaf_id = entry.id
                elif isinstance(entry, ActiveLeafSessionEntry):
                    active_leaf_id = entry.active_leaf_id
                if limit is not None and len(entries) >= limit:
                    break
    except UnicodeDecodeError as exc:
        raise SessionError(f"Session file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Could not read session file: {path}") from exc
    resolve_session_tree(entries)
    return entries
