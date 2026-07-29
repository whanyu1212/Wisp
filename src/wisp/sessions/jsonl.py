"""Append-only JSONL session persistence."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4
from weakref import WeakValueDictionary

import anyio

from wisp.agent.messages import Message, Role
from wisp.events import (
    JsonObject,
    KnownWispEvent,
    RpcMessageSnapshot,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    ToolCallSnapshot,
    WispEvent,
)
from wisp.sessions.branching import (
    SessionBranchProjection,
    project_fork_from_user_message,
    project_session_path,
)
from wisp.sessions.entries import (
    MAX_SESSION_NAME_BYTES,
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    SessionInfoSessionEntry,
    SessionTreeEntry,
    ToolResultPresentationSnapshot,
    is_session_tree_entry,
    normalize_session_name,
    session_entry_from_json,
    session_entry_to_json,
    typed_event_from_envelope,
)
from wisp.sessions.errors import (
    MalformedPersistedEventError,
    MalformedSessionEntryError,
    SessionError,
    SessionNavigationCancelledError,
    SessionUnrevertUnavailableError,
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
DEFAULT_SESSION_MESSAGE_PAGE_LIMIT = 200
MAX_SESSION_MESSAGE_PAGE_LIMIT = 500
DEFAULT_SESSION_TREE_PAGE_LIMIT = 200
MAX_SESSION_TREE_PAGE_LIMIT = 500
SESSION_TREE_PREVIEW_BYTE_LIMIT = 512
MESSAGE_CONTENT_BYTE_LIMIT = 64 * 1024
TOOL_ARGUMENTS_BYTE_LIMIT = 64 * 1024
MESSAGE_TOOL_CALL_LIMIT = 16
MESSAGE_PAGE_TEXT_BYTE_LIMIT = 512 * 1024
_FileSignature = tuple[int, int, int, int]

__all__ = [
    "AmbiguousSessionError",
    "DEFAULT_SESSION_MESSAGE_PAGE_LIMIT",
    "DEFAULT_SESSION_TREE_PAGE_LIMIT",
    "JsonlSession",
    "JsonlSessionStore",
    "MAX_SESSION_MESSAGE_PAGE_LIMIT",
    "MAX_SESSION_TREE_PAGE_LIMIT",
    "SessionForkResult",
    "SessionMessagePage",
    "SessionNameChange",
    "SessionSummary",
    "SessionTreeNavigation",
    "SessionTreeNodeSummary",
    "SessionTreeUnrevert",
    "SessionTreePage",
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
    source_session_name: str | None
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
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SessionNameChange:
    """The durable result of appending one session display-name metadata record."""

    session_id: str
    path: Path
    previous_name: str | None
    name: str | None
    entry_count: int


@dataclass(frozen=True, slots=True)
class SessionMessagePage:
    """Bounded active transcript page for frontend and RPC consumers."""

    session_id: str | None
    path: Path | None
    active_leaf_id: str | None
    messages: tuple[RpcMessageSnapshot, ...]
    truncated: bool
    next_before_entry_id: str | None


@dataclass(frozen=True, slots=True)
class SessionTreeNodeSummary:
    """Bounded frontend-facing metadata for one persisted session tree node."""

    entry_id: str
    parent_id: str | None
    operation_id: str | None
    created_at: datetime
    kind: Literal["message", "event", "compaction"]
    role: Role | None
    preview: str
    preview_truncated: bool


@dataclass(frozen=True, slots=True)
class SessionTreePage:
    """One append-ordered page of a selected session tree."""

    session_id: str | None
    path: Path | None
    active_leaf_id: str | None
    total_node_count: int
    nodes: tuple[SessionTreeNodeSummary, ...]
    truncated: bool
    next_after_entry_id: str | None


@dataclass(frozen=True, slots=True)
class SessionTreeNavigation:
    """The durable result of navigating within one append-only session tree."""

    selected_entry_id: str
    previous_active_leaf_id: str | None
    active_leaf_id: str | None
    editor_text: str | None
    changed: bool
    entry_count: int


@dataclass(frozen=True, slots=True)
class SessionTreeUnrevert:
    """The durable result of reversing the latest explicit tree navigation."""

    source_transition_id: str
    previous_active_leaf_id: str | None
    active_leaf_id: str | None
    entry_count: int


@dataclass(slots=True)
class _MessagePageTextBudget:
    remaining: int


@dataclass(frozen=True, slots=True)
class _SessionSummaryEntryMetadata:
    id: str
    session_id: str
    kind: str
    parent_id: str | None = None
    previous_leaf_id: str | None = None
    active_leaf_id: str | None = None
    name: str | None = None
    message_role: str | None = None
    reason: str | None = None
    selected_entry_id: str | None = None
    source_transition_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SessionSummaryMetadata:
    session_id: str
    entry_count: int
    active_leaf_id: str | None
    name: str | None


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
        return self._persist_projection(projection, name=_session_name_from_entries(entries))

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
        target = self._persist_projection(projection, name=None)
        assert projection.selected_entry_id is not None
        assert projection.selected_prompt is not None
        return SessionForkResult(
            session=target,
            source_session_id=source.session_id,
            source_active_leaf_id=source_active_leaf_id,
            source_session_name=_session_name_from_entries(entries),
            fork_leaf_id=projection.source_leaf_id,
            selected_entry_id=projection.selected_entry_id,
            selected_prompt=projection.selected_prompt,
        )

    def _persist_projection(
        self,
        projection: SessionBranchProjection,
        *,
        name: str | None,
    ) -> JsonlSession:
        target = self.create()
        if not projection.entries and name is None:
            return target
        copied_tree_entries = tuple(
            entry.model_copy(update={"session_id": target.session_id})
            for entry in projection.entries
        )
        copied_entries: tuple[SessionEntry, ...] = copied_tree_entries
        if name is not None:
            copied_entries = (
                *copied_entries,
                SessionInfoSessionEntry(session_id=target.session_id, name=name),
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
            name=metadata.name,
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

    async def set_name(
        self,
        name: str,
        *,
        operation_id: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SessionNameChange:
        """Append a session display-name metadata record."""

        entry = SessionInfoSessionEntry(
            session_id=self.session_id,
            name=name,
            operation_id=operation_id,
        )
        async with self._append_lock:
            return await anyio.to_thread.run_sync(
                self._set_name_once,
                entry,
                cancel_requested,
            )

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

    async def navigate_tree(
        self,
        entry_id: str,
        *,
        expected_active_leaf_id: str | None,
        operation_id: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SessionTreeNavigation:
        """Atomically navigate to one stored entry using Pi-style prompt restoration."""

        if not entry_id:
            raise ValueError("Session tree entry id must be non-empty")
        async with self._append_lock:
            return await anyio.to_thread.run_sync(
                self._navigate_tree_once,
                entry_id,
                expected_active_leaf_id,
                operation_id,
                cancel_requested,
            )

    async def unrevert_tree(
        self,
        *,
        expected_active_leaf_id: str | None,
        operation_id: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> SessionTreeUnrevert:
        """Reverse the latest eligible explicit navigation through append-only state."""

        async with self._append_lock:
            return await anyio.to_thread.run_sync(
                self._unrevert_tree_once,
                expected_active_leaf_id,
                operation_id,
                cancel_requested,
            )

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

    def read_name(self) -> str | None:
        """Read the latest append-only display name for this session."""

        if not self.path.is_file():
            return None
        return _session_name_from_entries(self.read_entries())

    def read_context(self) -> SessionReplay:
        """Replay the active provider context while preserving durable entry ids."""

        return replay_session_entries(self.read_entries())

    def read_active_leaf_id(self) -> str | None:
        """Read the append-only selected leaf for subsequent replay and appends."""

        return resolve_session_tree(self.read_entries()).active_leaf_id

    def read_active_path(self) -> tuple[SessionEntry, ...]:
        """Read the selected root-to-leaf tree path, excluding state records."""

        return tuple(resolve_session_tree(self.read_entries()).active_path)

    def read_message_page(
        self,
        *,
        limit: int = DEFAULT_SESSION_MESSAGE_PAGE_LIMIT,
        before_entry_id: str | None = None,
    ) -> SessionMessagePage:
        """Read a bounded active-path transcript page in chronological order."""

        return _message_page_from_entries(
            self.read_entries(),
            session_id=self.session_id,
            path=self.path,
            limit=limit,
            before_entry_id=before_entry_id,
        )

    def read_tree_page(
        self,
        *,
        limit: int = DEFAULT_SESSION_TREE_PAGE_LIMIT,
        after_entry_id: str | None = None,
    ) -> SessionTreePage:
        """Read bounded tree-node metadata in persisted append order."""

        try:
            self.path.lstat()
        except FileNotFoundError:
            entries: tuple[SessionEntry, ...] = ()
        else:
            entries = self.read_entries()
        return _tree_page_from_entries(
            entries,
            session_id=self.session_id,
            path=self.path,
            limit=limit,
            after_entry_id=after_entry_id,
        )

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

    def _create_with_entries_once(self, entries: tuple[SessionEntry, ...]) -> None:
        """Exclusively publish a complete projected session file."""

        with self._file_state.lock:
            with self._interprocess_lock():
                self._create_with_entries_locked(entries)

    def _create_with_entries_locked(self, entries: tuple[SessionEntry, ...]) -> None:
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

    def _set_name_once(
        self,
        entry: SessionInfoSessionEntry,
        cancel_requested: Callable[[], bool] | None,
    ) -> SessionNameChange:
        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                assert self._entry_index is not None
                previous_name = _session_name_from_entries(tuple(self._entry_index.values()))
                _raise_if_session_name_cancelled(cancel_requested)
                persisted = self._append_entry_locked(entry)
                assert isinstance(persisted, SessionInfoSessionEntry)
                return SessionNameChange(
                    session_id=self.session_id,
                    path=self.path,
                    previous_name=previous_name,
                    name=persisted.name,
                    entry_count=len(self._entry_index),
                )

    def _navigate_tree_once(
        self,
        entry_id: str,
        expected_active_leaf_id: str | None,
        operation_id: str | None,
        cancel_requested: Callable[[], bool] | None,
    ) -> SessionTreeNavigation:
        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                tree = resolve_session_tree(entries)
                previous_active_leaf_id = tree.active_leaf_id
                if previous_active_leaf_id != expected_active_leaf_id:
                    raise StaleSessionTreeError(
                        "Session tree changed: expected active leaf "
                        f"{expected_active_leaf_id!r}, found {previous_active_leaf_id!r}"
                    )

                target = next((node for node in tree.nodes if node.id == entry_id), None)
                if target is None:
                    raise SessionReplayError(f"Session tree entry not found: {entry_id}")
                if entry_id == previous_active_leaf_id:
                    _raise_if_navigation_cancelled(cancel_requested)
                    return SessionTreeNavigation(
                        selected_entry_id=entry_id,
                        previous_active_leaf_id=previous_active_leaf_id,
                        active_leaf_id=previous_active_leaf_id,
                        editor_text=None,
                        changed=False,
                        entry_count=len(entries),
                    )

                editor_text: str | None = None
                active_leaf_id: str | None = entry_id
                if isinstance(target, MessageSessionEntry) and target.message.role == "user":
                    active_leaf_id = target.parent_id
                    editor_text = target.message.content

                if active_leaf_id == previous_active_leaf_id:
                    _raise_if_navigation_cancelled(cancel_requested)
                    return SessionTreeNavigation(
                        selected_entry_id=entry_id,
                        previous_active_leaf_id=previous_active_leaf_id,
                        active_leaf_id=active_leaf_id,
                        editor_text=editor_text,
                        changed=False,
                        entry_count=len(entries),
                    )

                selection = ActiveLeafSessionEntry(
                    session_id=self.session_id,
                    previous_leaf_id=previous_active_leaf_id,
                    active_leaf_id=active_leaf_id,
                    operation_id=operation_id,
                    reason="navigation",
                    selected_entry_id=entry_id,
                )
                replay_session_entries((*entries, selection))
                _raise_if_navigation_cancelled(cancel_requested)
                persisted = self._append_entry_locked(selection)
                assert isinstance(persisted, ActiveLeafSessionEntry)
                return SessionTreeNavigation(
                    selected_entry_id=entry_id,
                    previous_active_leaf_id=previous_active_leaf_id,
                    active_leaf_id=active_leaf_id,
                    editor_text=editor_text,
                    changed=True,
                    entry_count=len(self._entry_index),
                )

    def _unrevert_tree_once(
        self,
        expected_active_leaf_id: str | None,
        operation_id: str | None,
        cancel_requested: Callable[[], bool] | None,
    ) -> SessionTreeUnrevert:
        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                tree = resolve_session_tree(entries)
                if tree.active_leaf_id != expected_active_leaf_id:
                    raise StaleSessionTreeError(
                        "Session tree changed: expected active leaf "
                        f"{expected_active_leaf_id!r}, found {tree.active_leaf_id!r}"
                    )

                latest_change = next(
                    (
                        entry
                        for entry in reversed(entries)
                        if not isinstance(entry, SessionInfoSessionEntry)
                    ),
                    None,
                )
                if not isinstance(latest_change, ActiveLeafSessionEntry) or (
                    latest_change.reason != "navigation"
                ):
                    raise SessionUnrevertUnavailableError(
                        "No explicit session-tree navigation is available to unrevert"
                    )
                if latest_change.active_leaf_id != tree.active_leaf_id:
                    raise SessionUnrevertUnavailableError(
                        "The latest session-tree navigation is no longer active"
                    )

                selection = ActiveLeafSessionEntry(
                    session_id=self.session_id,
                    previous_leaf_id=tree.active_leaf_id,
                    active_leaf_id=latest_change.previous_leaf_id,
                    operation_id=operation_id,
                    reason="unrevert",
                    source_transition_id=latest_change.id,
                )
                replay_session_entries((*entries, selection))
                _raise_if_navigation_cancelled(cancel_requested)
                persisted = self._append_entry_locked(selection)
                assert isinstance(persisted, ActiveLeafSessionEntry)
                return SessionTreeUnrevert(
                    source_transition_id=latest_change.id,
                    previous_active_leaf_id=tree.active_leaf_id,
                    active_leaf_id=persisted.active_leaf_id,
                    entry_count=len(self._entry_index),
                )

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

    for entry in reversed(entry_index.values()):
        if is_session_tree_entry(entry):
            return entry.id
        if isinstance(entry, ActiveLeafSessionEntry):
            return entry.active_leaf_id
    return None


def _session_name_from_entries(entries: Sequence[SessionEntry]) -> str | None:
    name: str | None = None
    for entry in entries:
        if isinstance(entry, SessionInfoSessionEntry):
            name = entry.name
    return name


def _message_page_from_entries(
    entries: Sequence[SessionEntry],
    *,
    session_id: str,
    path: Path,
    limit: int,
    before_entry_id: str | None,
) -> SessionMessagePage:
    if limit < 1:
        raise ValueError("message page limit must be at least 1")
    if limit > MAX_SESSION_MESSAGE_PAGE_LIMIT:
        raise ValueError(f"message page limit cannot exceed {MAX_SESSION_MESSAGE_PAGE_LIMIT}")

    tree = resolve_session_tree(entries)
    active_messages = tuple(
        entry for entry in tree.active_path if isinstance(entry, MessageSessionEntry)
    )
    if before_entry_id is None:
        candidates = active_messages
    else:
        cursor_index = next(
            (index for index, entry in enumerate(active_messages) if entry.id == before_entry_id),
            None,
        )
        if cursor_index is None:
            raise SessionError(f"Session message cursor not found: {before_entry_id}")
        candidates = active_messages[:cursor_index]

    truncated = len(candidates) > limit
    selected = candidates[-limit:]
    text_budget = _MessagePageTextBudget(remaining=MESSAGE_PAGE_TEXT_BYTE_LIMIT)
    newest_first_messages = tuple(
        _rpc_message_snapshot(entry, text_budget=text_budget) for entry in reversed(selected)
    )
    return SessionMessagePage(
        session_id=session_id,
        path=path,
        active_leaf_id=tree.active_leaf_id,
        messages=tuple(reversed(newest_first_messages)),
        truncated=truncated,
        next_before_entry_id=selected[0].id if truncated and selected else None,
    )


def _rpc_message_snapshot(
    entry: MessageSessionEntry,
    *,
    text_budget: _MessagePageTextBudget,
) -> RpcMessageSnapshot:
    message = entry.message
    content, content_original_bytes, content_truncated = _clip_text_with_budget(
        message.content,
        limit=MESSAGE_CONTENT_BYTE_LIMIT,
        text_budget=text_budget,
    )
    tool_calls = message.tool_calls or ()
    selected_tool_calls = tool_calls[:MESSAGE_TOOL_CALL_LIMIT]
    return RpcMessageSnapshot(
        entry_id=entry.id,
        parent_id=entry.parent_id,
        operation_id=entry.operation_id,
        created_at=entry.created_at,
        role=message.role,
        content=content,
        content_original_bytes=content_original_bytes,
        content_truncated=content_truncated,
        tool_call_id=message.tool_call_id,
        tool_name=message.tool_name,
        tool_calls=tuple(
            _rpc_tool_call_snapshot(tool_call, text_budget=text_budget)
            for tool_call in selected_tool_calls
        ),
        tool_calls_original_count=len(tool_calls),
        tool_calls_truncated=len(tool_calls) > MESSAGE_TOOL_CALL_LIMIT,
        response_id=message.response_id,
        finish_reason=message.finish_reason,
        is_error=message.is_error,
        usage=message.usage,
        cost=message.cost,
        tool_result=_rpc_tool_result_snapshot(entry.tool_result, text_budget=text_budget),
    )


def _rpc_tool_result_snapshot(
    tool_result: ToolResultPresentationSnapshot | None,
    *,
    text_budget: _MessagePageTextBudget,
) -> RpcMessageToolResultSnapshot | None:
    if tool_result is None:
        return None
    before_text = tool_result.before_text
    truncated = tool_result.truncated
    if before_text is not None:
        clipped_before_text, _, before_text_truncated = _clip_text_with_budget(
            before_text,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        before_text = None if before_text_truncated else clipped_before_text
        truncated = truncated or before_text_truncated
    summary = tool_result.summary
    if summary is not None:
        summary, _, summary_truncated = _clip_text_with_budget(
            summary,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        truncated = truncated or summary_truncated
    return RpcMessageToolResultSnapshot(
        status=tool_result.status,
        exit_code=tool_result.exit_code,
        before_text=before_text,
        created=tool_result.created,
        summary=summary,
        truncated=truncated,
    )


def _rpc_tool_call_snapshot(
    tool_call: ToolCallSnapshot,
    *,
    text_budget: _MessagePageTextBudget,
) -> RpcMessageToolCallSnapshot:
    clipped_arguments, original_bytes, truncated = _clip_json_object(
        tool_call.arguments,
        limit=TOOL_ARGUMENTS_BYTE_LIMIT,
        text_budget=text_budget,
    )
    return RpcMessageToolCallSnapshot(
        call_id=tool_call.call_id,
        name=tool_call.name,
        arguments=clipped_arguments,
        arguments_original_bytes=original_bytes,
        arguments_truncated=truncated,
        parse_error=tool_call.parse_error,
    )


def _clip_text_with_budget(
    text: str,
    *,
    limit: int,
    text_budget: _MessagePageTextBudget,
) -> tuple[str, int, bool]:
    effective_limit = min(limit, max(text_budget.remaining, 0))
    clipped, original_bytes, truncated = _clip_text(text, limit=effective_limit)
    text_budget.remaining = max(text_budget.remaining - len(clipped.encode("utf-8")), 0)
    return clipped, original_bytes, truncated


def _clip_text(text: str, *, limit: int) -> tuple[str, int, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, len(encoded), False
    return encoded[:limit].decode("utf-8", errors="ignore"), len(encoded), True


def _raise_if_navigation_cancelled(
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if cancel_requested is not None and cancel_requested():
        raise SessionNavigationCancelledError("Session tree navigation cancelled")


def _raise_if_session_name_cancelled(
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if cancel_requested is not None and cancel_requested():
        raise SessionNavigationCancelledError("Session name update cancelled")


def _tree_page_from_entries(
    entries: Sequence[SessionEntry],
    *,
    session_id: str | None,
    path: Path | None,
    limit: int,
    after_entry_id: str | None,
) -> SessionTreePage:
    if type(limit) is not int or limit < 1 or limit > MAX_SESSION_TREE_PAGE_LIMIT:
        raise ValueError(
            f"Session tree page limit must be between 1 and {MAX_SESSION_TREE_PAGE_LIMIT}"
        )
    if after_entry_id is not None and not after_entry_id:
        raise ValueError("Session tree cursor must be non-empty")

    tree = resolve_session_tree(entries)
    start = 0
    if after_entry_id is not None:
        cursor_index = next(
            (index for index, node in enumerate(tree.nodes) if node.id == after_entry_id),
            None,
        )
        if cursor_index is None:
            raise SessionError(f"Session tree cursor not found: {after_entry_id}")
        start = cursor_index + 1

    selected = tree.nodes[start : start + limit]
    end = start + len(selected)
    truncated = end < len(tree.nodes)
    return SessionTreePage(
        session_id=session_id,
        path=path,
        active_leaf_id=tree.active_leaf_id,
        total_node_count=len(tree.nodes),
        nodes=tuple(_session_tree_node_summary(node) for node in selected),
        truncated=truncated,
        next_after_entry_id=selected[-1].id if truncated and selected else None,
    )


def _session_tree_node_summary(entry: SessionTreeEntry) -> SessionTreeNodeSummary:
    role: Role | None = None
    if isinstance(entry, MessageSessionEntry):
        role = entry.message.role
        preview = entry.message.content
        if not preview and entry.message.tool_calls:
            names = ", ".join(call.name for call in entry.message.tool_calls)
            preview = f"[tool calls: {names}]"
        kind: Literal["message", "event", "compaction"] = "message"
    elif isinstance(entry, EventSessionEntry):
        event_type = entry.event.payload.get("type")
        preview = event_type if isinstance(event_type, str) else "event"
        kind = "event"
    else:
        assert isinstance(entry, CompactionSessionEntry)
        preview = entry.compaction.summary
        kind = "compaction"

    clipped, _original_bytes, truncated = _clip_text(
        preview,
        limit=SESSION_TREE_PREVIEW_BYTE_LIMIT,
    )
    return SessionTreeNodeSummary(
        entry_id=entry.id,
        parent_id=entry.parent_id,
        operation_id=entry.operation_id,
        created_at=entry.created_at,
        kind=kind,
        role=role,
        preview=clipped,
        preview_truncated=truncated,
    )


def _clip_json_object(
    arguments: JsonObject,
    *,
    limit: int,
    text_budget: _MessagePageTextBudget,
) -> tuple[JsonObject, int, bool]:
    rendered = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = rendered.encode("utf-8")
    effective_limit = min(limit, max(text_budget.remaining, 0))
    if len(encoded) <= effective_limit:
        text_budget.remaining = max(text_budget.remaining - len(encoded), 0)
        return dict(arguments), len(encoded), False
    preview_arguments, preview_bytes = _clip_json_preview_wrapper(
        rendered,
        limit=effective_limit,
    )
    text_budget.remaining = max(text_budget.remaining - preview_bytes, 0)
    return preview_arguments, len(encoded), True


def _clip_json_preview_wrapper(rendered: str, *, limit: int) -> tuple[JsonObject, int]:
    empty_preview: JsonObject = {"truncated_json_preview": ""}
    empty_preview_bytes = _json_object_byte_count(empty_preview)
    if limit < empty_preview_bytes:
        return {}, 0

    rendered_bytes = rendered.encode("utf-8")
    low = 0
    high = len(rendered_bytes)
    best_preview = ""
    best_byte_count = empty_preview_bytes
    while low <= high:
        midpoint = (low + high) // 2
        preview = rendered_bytes[:midpoint].decode("utf-8", errors="ignore")
        candidate: JsonObject = {"truncated_json_preview": preview}
        candidate_byte_count = _json_object_byte_count(candidate)
        if candidate_byte_count <= limit:
            best_preview = preview
            best_byte_count = candidate_byte_count
            low = midpoint + 1
        else:
            high = midpoint - 1
    return {"truncated_json_preview": best_preview}, best_byte_count


def _json_object_byte_count(value: JsonObject) -> int:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(rendered.encode("utf-8"))


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
    if isinstance(entry, SessionInfoSessionEntry):
        return

    assert isinstance(entry, ActiveLeafSessionEntry)
    resolve_session_tree((*entry_index.values(), entry))


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
    name: str | None = None
    node_ids: set[str] = set()
    node_metadata: dict[str, _SessionSummaryEntryMetadata] = {}
    transition_metadata: dict[str, _SessionSummaryEntryMetadata] = {}
    latest_history_entry_id: str | None = None
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
                active_leaf_id, latest_history_entry_id = _resolve_summary_entry(
                    entry,
                    active_leaf_id=active_leaf_id,
                    node_ids=node_ids,
                    node_metadata=node_metadata,
                    transition_metadata=transition_metadata,
                    latest_history_entry_id=latest_history_entry_id,
                )
                if entry.kind == "session_info":
                    name = entry.name
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
        name=name,
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
    if version in {1, 2, 3, 4}:
        forbidden = tuple(
            field
            for field in ("reason", "selected_entry_id", "source_transition_id")
            if field in raw
        )
        if forbidden:
            fields = ", ".join(forbidden)
            raise MalformedSessionEntryError(
                f"V{version} session entry contains v5 transition field(s) {fields}{location}"
            )
    if version == 1:
        return _v1_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version == 2:
        return _v2_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version in {3, 4}:
        return _v3_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version != SESSION_ENTRY_SCHEMA_VERSION:
        raise UnsupportedSessionEntryVersionError(
            f"Unsupported session entry schema_version {version}{location}; "
            f"expected {SESSION_ENTRY_SCHEMA_VERSION}"
        )
    return _v5_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)


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
        message_role=_summary_message_role(raw, kind),
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
        message_role=_summary_message_role(raw, kind),
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
            message_role=_summary_message_role(raw, kind),
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


def _v3_summary_entry_metadata(
    raw: JsonObject,
    *,
    location: str,
    parent_id: str | None,
) -> _SessionSummaryEntryMetadata:
    kind = raw.get("kind")
    if kind == "session_info":
        _require_summary_base_fields(raw, location=location)
        return _SessionSummaryEntryMetadata(
            id=_required_summary_string(raw, "id", location=location),
            session_id=_required_summary_string(raw, "session_id", location=location),
            kind=kind,
            name=_summary_session_name(raw, location=location),
        )
    return _v2_summary_entry_metadata(raw, location=location, parent_id=parent_id)


def _v5_summary_entry_metadata(
    raw: JsonObject,
    *,
    location: str,
    parent_id: str | None,
) -> _SessionSummaryEntryMetadata:
    base = _v3_summary_entry_metadata(raw, location=location, parent_id=parent_id)
    if raw.get("kind") == "active_leaf":
        reason = raw.get("reason")
        selected_entry_id = raw.get("selected_entry_id")
        source_transition_id = raw.get("source_transition_id")
        if reason == "system":
            valid = selected_entry_id is None and source_transition_id is None
        elif reason == "navigation":
            valid = (
                isinstance(selected_entry_id, str)
                and bool(selected_entry_id)
                and source_transition_id is None
            )
        elif reason == "unrevert":
            valid = (
                isinstance(source_transition_id, str)
                and bool(source_transition_id)
                and selected_entry_id is None
            )
        else:
            valid = False
        if not valid:
            raise MalformedSessionEntryError(
                f"Malformed v5 active-leaf transition metadata{location}"
            )
        return replace(
            base,
            reason=cast(str | None, reason),
            selected_entry_id=cast(str | None, selected_entry_id),
            source_transition_id=cast(str | None, source_transition_id),
        )
    return base


def _require_summary_base_fields(raw: JsonObject, *, location: str) -> None:
    missing = tuple(field for field in ("id", "session_id", "created_at") if field not in raw)
    if missing:
        fields = ", ".join(missing)
        raise MalformedSessionEntryError(
            f"Persisted session entry is missing required field(s) {fields}{location}"
        )


def _summary_message_role(raw: JsonObject, kind: object) -> str | None:
    if kind != "message":
        return None
    message = raw.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    return role if isinstance(role, str) else None


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


def _summary_session_name(raw: JsonObject, *, location: str) -> str | None:
    if "name" not in raw:
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    value = raw["name"]
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedSessionEntryError(f"Malformed session entry{location}")
    normalized = normalize_session_name(value)
    if normalized is None:
        return None
    if len(normalized.encode("utf-8")) > MAX_SESSION_NAME_BYTES:
        raise MalformedSessionEntryError(
            f"session name cannot exceed {MAX_SESSION_NAME_BYTES} UTF-8 bytes{location}"
        )
    return normalized


def _resolve_summary_entry(
    entry: _SessionSummaryEntryMetadata,
    *,
    active_leaf_id: str | None,
    node_ids: set[str],
    node_metadata: dict[str, _SessionSummaryEntryMetadata],
    transition_metadata: dict[str, _SessionSummaryEntryMetadata],
    latest_history_entry_id: str | None,
) -> tuple[str | None, str | None]:
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
        node_metadata[entry.id] = entry
        return entry.id, entry.id
    if entry.kind == "session_info":
        return active_leaf_id, latest_history_entry_id
    if entry.previous_leaf_id != active_leaf_id:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} expected previous leaf "
            f"{entry.previous_leaf_id!r}, found {active_leaf_id!r}"
        )
    if entry.active_leaf_id is not None and entry.active_leaf_id not in node_ids:
        raise SessionReplayError(
            f"Active-leaf entry {entry.id} references unknown leaf {entry.active_leaf_id}"
        )
    if entry.reason == "navigation":
        selected_entry_id = entry.selected_entry_id
        assert selected_entry_id is not None
        selected = node_metadata.get(selected_entry_id)
        if selected is None:
            raise SessionReplayError(
                f"Navigation entry {entry.id} references unknown selected entry {selected_entry_id}"
            )
        expected_leaf_id: str | None = selected.id
        if selected.kind == "message" and selected.message_role == "user":
            expected_leaf_id = selected.parent_id
        if entry.active_leaf_id != expected_leaf_id:
            raise SessionReplayError(
                f"Navigation entry {entry.id} selects {selected_entry_id} but activates "
                f"{entry.active_leaf_id!r}, expected {expected_leaf_id!r}"
            )
        if entry.active_leaf_id == entry.previous_leaf_id:
            raise SessionReplayError(f"Navigation entry {entry.id} records a no-op selection")
    elif entry.reason == "unrevert":
        source_transition_id = entry.source_transition_id
        assert source_transition_id is not None
        source = transition_metadata.get(source_transition_id)
        if source is None or source.reason != "navigation":
            raise SessionReplayError(
                f"Unrevert entry {entry.id} references invalid navigation transition "
                f"{source_transition_id}"
            )
        if latest_history_entry_id != source_transition_id:
            raise SessionReplayError(
                f"Unrevert entry {entry.id} does not reverse the latest history change"
            )
        if (
            entry.previous_leaf_id != source.active_leaf_id
            or entry.active_leaf_id != source.previous_leaf_id
        ):
            raise SessionReplayError(
                f"Unrevert entry {entry.id} is not the inverse of navigation {source_transition_id}"
            )
    transition_metadata[entry.id] = entry
    return entry.active_leaf_id, entry.id


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
