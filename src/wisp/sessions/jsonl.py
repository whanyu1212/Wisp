"""Append-only JSONL session persistence."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
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
    RpcSkillInvocationSnapshot,
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
    StaleSessionWriterError,
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
from wisp.skills.models import SkillInvocationEvidence

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
# Bumping this retires every existing newest-page cache. The cache is a pure
# accelerator, so a retired one costs a slow read, never a wrong one.
SESSION_PAGE_CACHE_SCHEMA_VERSION = 1
SESSION_PAGE_CACHE_SUFFIX = ".page-cache"
_FileSignature = tuple[int, int, int, int]


class _UnconditionalAppend:
    """Sentinel distinguishing no concurrency check from an expected empty leaf."""


_UNCONDITIONAL_APPEND = _UnconditionalAppend()
_EXPECTED_APPEND_LEAF: ContextVar[str | None | _UnconditionalAppend] = ContextVar(
    "wisp_expected_append_leaf",
    default=_UNCONDITIONAL_APPEND,
)
_EXPECTED_COMPACTION_LEAF: ContextVar[str | None | _UnconditionalAppend] = ContextVar(
    "wisp_expected_compaction_leaf",
    default=_UNCONDITIONAL_APPEND,
)

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
    "SessionRunSnapshot",
    "SessionSummary",
    "SessionTreeNavigation",
    "SessionTreeNodeSummary",
    "SessionTreeUnrevert",
    "SessionTreePage",
    "SessionError",
    "SessionNotFoundError",
    "StaleCompactionError",
    "StaleSessionWriterError",
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
class SessionRunSnapshot:
    """One coherent provider context and durable position for starting a run."""

    entry_count: int
    active_leaf_id: str | None
    replay: SessionReplay


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
    next_after_entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class _MessagePageIndex:
    """Resolved active-path messages and their stable cursor positions."""

    active_leaf_id: str | None
    messages: tuple[MessageSessionEntry, ...]
    positions: dict[str, int]


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
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SessionError(f"Could not inspect session file: {candidate}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise SessionError(f"Session file is not a regular file: {candidate}")
            if stat.S_ISREG(info.st_mode):
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
        files = (path for path in self.root.glob("*.jsonl") if _prepare_session_file(path))
        return tuple(sorted(files, key=lambda path: path.name))


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
        self._message_page_index: _MessagePageIndex | None = None

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
        expected_active_leaf_id = _EXPECTED_APPEND_LEAF.get()
        if isinstance(expected_active_leaf_id, _UnconditionalAppend):
            return await self.append_entry(entry)
        return await self.append_entry_if_current(
            entry,
            expected_active_leaf_id=expected_active_leaf_id,
        )

    async def append_message_if_current(
        self,
        message: Message,
        *,
        expected_active_leaf_id: str | None,
        operation_id: str | None = None,
    ) -> SessionEntry:
        """Append a message through the public seam while enforcing run ownership."""

        token = _EXPECTED_APPEND_LEAF.set(expected_active_leaf_id)
        try:
            return await self.append_message(message, operation_id=operation_id)
        finally:
            _EXPECTED_APPEND_LEAF.reset(token)

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
            return await anyio.to_thread.run_sync(
                self._append_entry_once,
                entry,
                _EXPECTED_APPEND_LEAF.get(),
            )

    async def append_entry_if_current(
        self,
        entry: SessionEntry,
        *,
        expected_active_leaf_id: str | None,
    ) -> SessionEntry:
        """Persist an entry only against the active leaf observed by its run."""

        token = _EXPECTED_APPEND_LEAF.set(expected_active_leaf_id)
        try:
            return await self.append_entry(entry)
        finally:
            _EXPECTED_APPEND_LEAF.reset(token)

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
                _EXPECTED_COMPACTION_LEAF.get(),
            )

    async def append_compaction_entry_if_current(
        self,
        entry: SessionEntry,
        *,
        expected_context_entry_ids: Sequence[str],
        expected_active_leaf_id: str | None,
    ) -> SessionEntry:
        """Append a compaction only against the active leaf observed by its run."""

        token = _EXPECTED_COMPACTION_LEAF.set(expected_active_leaf_id)
        try:
            return await self.append_compaction_entry(
                entry,
                expected_context_entry_ids=expected_context_entry_ids,
            )
        finally:
            _EXPECTED_COMPACTION_LEAF.reset(token)

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
        """Read all committed entries, repairing an incomplete final record first.

        Served from the validated entry index so repeated reads of one session
        parse the file once. Resuming a long session previously re-parsed it for
        every derived read (context replay, name, entry count), which dominated
        startup: three full passes over a 10 MB transcript before the first
        frame. The index refreshes itself whenever the file's signature or
        generation changes, so callers still observe external writes.
        """

        return self.read_entry_snapshot()

    def read_run_snapshot(self) -> SessionRunSnapshot:
        """Read provider context and its active leaf under one session lock."""

        with self._file_state.lock:
            with self._interprocess_lock():
                self._refresh_entry_index()
                assert self._entry_index is not None
                entries = tuple(self._entry_index.values())
                replay = replay_session_entries(entries)
                return SessionRunSnapshot(
                    entry_count=len(entries),
                    active_leaf_id=replay.active_leaf_id,
                    replay=replay,
                )

    def read_entry_snapshot(self) -> tuple[SessionEntry, ...]:
        """Read an append-ordered snapshot through the validated entry index."""

        with self._file_state.lock:
            with self._interprocess_lock(prepare_parent=False):
                if self._validate_session_file() is None:
                    raise SessionNotFoundError(f"Session file does not exist: {self.path}")
                self._refresh_entry_index()
                assert self._entry_index is not None
                return tuple(self._entry_index.values())

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
        after_entry_id: str | None = None,
        entry_ids: tuple[str, ...] = (),
        complete_structure: bool = False,
        full_content: bool = False,
    ) -> SessionMessagePage:
        """Read a bounded active-path transcript page in chronological order."""

        _validate_message_page_limit(limit)
        with self._file_state.lock:
            with self._interprocess_lock(prepare_parent=False):
                info = self._validate_session_file()
                if info is None:
                    raise SessionNotFoundError(f"Session file does not exist: {self.path}")
                cached_page = self._cached_newest_page(
                    info,
                    limit=limit,
                    before_entry_id=before_entry_id,
                    after_entry_id=after_entry_id,
                    entry_ids=entry_ids,
                    complete_structure=complete_structure,
                    full_content=full_content,
                )
                if cached_page is not None:
                    return cached_page
                self._refresh_entry_index()
                if self._message_page_index is None:
                    assert self._entry_index is not None
                    self._message_page_index = _message_page_index_from_entries(
                        self._entry_index.values()
                    )
                    self._publish_page_cache(self._message_page_index)
                return _message_page_from_index(
                    self._message_page_index,
                    session_id=self.session_id,
                    path=self.path,
                    limit=limit,
                    before_entry_id=before_entry_id,
                    after_entry_id=after_entry_id,
                    entry_ids=entry_ids,
                    complete_structure=complete_structure,
                    full_content=full_content,
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
            with self._interprocess_lock(prepare_parent=False):
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

            persisted = tuple(_read_entries_unlocked(temp_path))
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
        """Append and sync one newline-committed record, rolling back on failure."""

        _ensure_private_directory(self.path.parent)
        data = f"{line}\n".encode()
        existed = self._validate_session_file() is not None
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, PRIVATE_FILE_MODE)
        try:
            info = os.fstat(fd)
        except Exception:
            os.close(fd)
            raise
        signature = (info.st_dev, info.st_ino)
        original_size = info.st_size
        try:
            if not stat.S_ISREG(info.st_mode):
                raise SessionError(f"Session file is not a regular file: {self.path}")
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            _write_all(fd, data)
            _sync_file(fd)
            if not existed:
                _sync_directory(self.path.parent)
        except Exception as append_error:
            try:
                os.ftruncate(fd, original_size)
                _sync_file(fd)
            except OSError as rollback_error:
                raise SessionError(
                    "Session append failed and rollback could not be synchronized for "
                    f"{self.path}: append error: {append_error}"
                ) from rollback_error
            finally:
                os.close(fd)
            if not existed and original_size == 0:
                _unlink_if_same_file(self.path, signature)
                _sync_directory(self.path.parent)
            raise
        else:
            os.close(fd)

    def _append_entry_once(
        self,
        entry: SessionEntry,
        expected_active_leaf_id: str | None | _UnconditionalAppend,
    ) -> SessionEntry:
        with self._file_state.lock:
            with self._interprocess_lock():
                return self._append_entry_locked(
                    entry,
                    expected_active_leaf_id=expected_active_leaf_id,
                )

    def _append_compaction_entry_once(
        self,
        entry: SessionEntry,
        expected_context_entry_ids: tuple[str, ...],
        expected_active_leaf_id: str | None | _UnconditionalAppend,
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
                return self._append_entry_locked(
                    entry,
                    expected_active_leaf_id=expected_active_leaf_id,
                )

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

    def _append_entry_locked(
        self,
        entry: SessionEntry,
        *,
        expected_active_leaf_id: str | None | _UnconditionalAppend = _UNCONDITIONAL_APPEND,
    ) -> SessionEntry:
        _ensure_private_directory(self.path.parent)
        self._refresh_entry_index()
        existing = self._persisted_entry_locked(entry)
        if existing is not None:
            return existing

        assert self._entry_index is not None
        active_leaf_id = _active_leaf_id(self._entry_index)
        if (
            not isinstance(expected_active_leaf_id, _UnconditionalAppend)
            and active_leaf_id != expected_active_leaf_id
        ):
            raise StaleSessionWriterError(
                f"Session {self.session_id} changed before operation "
                f"{entry.operation_id!r} could append: expected active leaf "
                f"{expected_active_leaf_id!r}, found {active_leaf_id!r}"
            )
        if (
            is_session_tree_entry(entry)
            and entry.parent_id is not None
            and entry.parent_id != active_leaf_id
        ):
            if isinstance(expected_active_leaf_id, _UnconditionalAppend):
                raise SessionError(
                    f"Session entry {entry.id} specifies parent {entry.parent_id!r}, "
                    f"but the active leaf is {active_leaf_id!r}"
                )
            raise StaleSessionWriterError(
                f"Session {self.session_id} changed before operation "
                f"{entry.operation_id!r} could append: expected active leaf "
                f"{entry.parent_id!r}, found {active_leaf_id!r}"
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
        self._invalidate_message_page_index()
        return persisted

    def _persisted_entry_locked(self, entry: SessionEntry) -> SessionEntry | None:
        assert self._entry_index is not None
        existing = self._entry_index.get(entry.id)
        if existing is None:
            return None
        if existing == entry or _matches_detached_entry(existing, entry):
            return existing
        raise SessionError(f"Session entry id conflicts with persisted data: {entry.id}")

    def _publish_page_cache(self, index: _MessagePageIndex) -> None:
        """Record the newest active-path messages so the next process can skip this.

        Written only after a complete read has resolved the tree, so the cache always
        describes a fully validated active path. Signed with the session file's
        identity and size/mtime, which is what makes a later read able to trust it.
        """

        info = self._validate_session_file()
        if info is None:
            return
        newest = index.messages[-MAX_SESSION_MESSAGE_PAGE_LIMIT:]
        if not newest:
            return
        _write_page_cache(
            self.path,
            _session_file_signature(info),
            newest,
            len(index.messages),
        )

    def _cached_newest_page(
        self,
        info: os.stat_result,
        *,
        limit: int,
        before_entry_id: str | None,
        after_entry_id: str | None,
        entry_ids: tuple[str, ...],
        complete_structure: bool,
        full_content: bool,
    ) -> SessionMessagePage | None:
        """Serve the newest page from the sidecar cache, or ``None`` to read fully.

        Resuming a session otherwise parses every persisted entry before it can
        return the newest page, because the active path is only known once the whole
        tree is resolved. That cost scales with total session size rather than with
        the page requested. The cache short-circuits exactly that one request shape;
        anything else, and any doubt at all about the cache, reads normally.
        """

        if self._entry_index is not None:
            # The index this process already built is authoritative and cheaper than
            # re-reading a file; the cache only exists to avoid building it at all.
            return None
        signature = _session_file_signature(info)
        restored = _read_page_cache(self.path, signature)
        if restored is None:
            return None
        cached, total_message_count = restored
        if not _serves_newest_page(
            limit=limit,
            before_entry_id=before_entry_id,
            after_entry_id=after_entry_id,
            entry_ids=entry_ids,
            full_content=full_content,
            cached_count=len(cached),
        ):
            return None
        # `_message_page_from_index` derives `truncated` (and therefore the backward
        # cursor) from how much active path it was given. The cache holds only the
        # newest slice, so pad the positions with the count the complete read saw:
        # otherwise a session with older history would report that it has none.
        selected = cached[-limit:]
        offset = total_message_count - len(selected)
        return _message_page_from_index(
            _MessagePageIndex(
                active_leaf_id=None,
                messages=selected,
                positions={entry.id: offset + index for index, entry in enumerate(selected)},
            ),
            session_id=self.session_id,
            path=self.path,
            limit=limit,
            before_entry_id=None,
            after_entry_id=None,
            entry_ids=(),
            complete_structure=complete_structure,
            full_content=False,
            truncated_override=total_message_count > len(selected),
        )

    def _refresh_entry_index(self) -> None:
        self._recover_incomplete_tail_locked()
        info = self._validate_session_file()
        if info is None:
            self._entry_index = {}
            self._entry_index_generation = self._file_state.generation
            self._entry_index_signature = None
            self._invalidate_message_page_index()
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
            self._invalidate_message_page_index()

    def _invalidate_entry_index(self) -> None:
        self._entry_index = None
        self._entry_index_generation = None
        self._entry_index_signature = None
        self._invalidate_message_page_index()

    def _invalidate_message_page_index(self) -> None:
        self._message_page_index = None

    def _load_entry_index(self) -> dict[str, SessionEntry]:
        entries: dict[str, SessionEntry] = {}
        for entry in _read_entries_unlocked(self.path):
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
                self._recover_incomplete_tail_locked()
                if not self.path.is_file():
                    return False
                entries = tuple(_read_entries_unlocked(self.path))
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

    def _recover_incomplete_tail_locked(self) -> None:
        if _recover_incomplete_tail(self.path):
            self._file_state.generation += 1
            self._invalidate_entry_index()

    @contextmanager
    def _interprocess_lock(self, *, prepare_parent: bool = True) -> Iterator[None]:
        """Serialize session access across cooperating Wisp processes."""

        with _interprocess_lock(self.path, prepare_parent=prepare_parent):
            yield

    def _truncate_entries(self, count: int) -> None:
        if not self.path.is_file():
            return
        self._recover_incomplete_tail_locked()
        if not self.path.is_file():
            return
        entries = tuple(_read_entries_unlocked(self.path))[:count]
        if not entries:
            info = self._validate_session_file()
            if info is not None:
                _unlink_expected_file(self.path, (info.st_dev, info.st_ino))
                _sync_directory(self.path.parent)
            return
        self._replace_lines([session_entry_to_json(entry) for entry in entries])

    def _replace_lines(self, lines: list[str]) -> None:
        """Atomically publish a complete replacement for the live session file."""

        _ensure_private_directory(self.path.parent)
        data = "".join(f"{line}\n" for line in lines).encode()
        temp_path = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        signature: tuple[int, int] | None = None
        try:
            fd = os.open(temp_path, flags, PRIVATE_FILE_MODE)
            info = os.fstat(fd)
            signature = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SessionError(f"Session temporary file is not regular: {temp_path}")
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            _write_all(fd, data)
            _sync_file(fd)
            os.close(fd)
            fd = -1
            # Validate the staged JSONL before replacing the last committed file.
            _read_entries_unlocked(temp_path)
            os.replace(temp_path, self.path)
            signature = None
            _sync_directory(self.path.parent)
        finally:
            if fd != -1:
                os.close(fd)
            if signature is not None:
                _unlink_if_same_file(temp_path, signature)


def _write_page_cache(
    path: Path,
    signature: _FileSignature,
    messages: tuple[MessageSessionEntry, ...],
    total_message_count: int,
) -> None:
    """Publish the newest-page cache beside its session, atomically and privately.

    Best effort by definition: the cache only saves work on a later read, so any
    failure to write one is dropped rather than surfaced. A partially written cache
    would be worse than none, so it is staged and renamed like the session file.
    """

    cache_path = _page_cache_path(path)
    header = json.dumps(
        {
            "cache_schema_version": SESSION_PAGE_CACHE_SCHEMA_VERSION,
            "entry_schema_version": SESSION_ENTRY_SCHEMA_VERSION,
            "signature": list(signature),
            "message_count": len(messages),
            "total_message_count": total_message_count,
        },
        sort_keys=True,
    )
    lines = [header, *(session_entry_to_json(entry) for entry in messages)]
    data = "".join(f"{line}\n" for line in lines).encode()
    temp_path = cache_path.with_name(f".{cache_path.name}.{uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    staged: tuple[int, int] | None = None
    try:
        fd = os.open(temp_path, flags, PRIVATE_FILE_MODE)
        info = os.fstat(fd)
        staged = (info.st_dev, info.st_ino)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return
        if os.name == "posix":
            os.fchmod(fd, PRIVATE_FILE_MODE)
        _write_all(fd, data)
        _sync_file(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_path, cache_path)
        staged = None
    except OSError:
        return
    finally:
        if fd != -1:
            with suppress(OSError):
                os.close(fd)
        if staged is not None:
            with suppress(OSError):
                _unlink_if_same_file(temp_path, staged)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("Session write made no progress")
        view = view[written:]


def _read_exact(fd: int, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise OSError("Session read ended before the expected file size")
        data.extend(chunk)
    return bytes(data)


def _sync_file(fd: int) -> None:
    os.fsync(fd)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _interprocess_lock(path: Path, *, prepare_parent: bool = True) -> Iterator[None]:
    """Serialize access to one session across cooperating Wisp processes."""

    if prepare_parent:
        _ensure_private_directory(path.parent)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    try:
        path_info = lock_path.lstat()
    except FileNotFoundError:
        path_info = None
    except OSError as exc:
        raise SessionError(f"Could not inspect session lock: {lock_path}") from exc
    if path_info is not None and (
        stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode)
    ):
        raise SessionError(f"Session lock is not a regular file: {lock_path}")
    if path_info is not None and path_info.st_nlink != 1:
        raise SessionError(f"Session lock has multiple hard links: {lock_path}")

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        if not prepare_parent and exc.errno in {
            errno.EACCES,
            errno.EPERM,
            errno.EROFS,
        }:
            lock_unavailable = path_info is not None
            if path_info is None:
                try:
                    lock_path.lstat()
                except FileNotFoundError:
                    lock_unavailable = True
                except OSError:
                    pass
            if lock_unavailable and _session_file_has_complete_tail(path):
                yield
                return
        raise
    unlock: Callable[[], object] | None = None
    try:
        info = os.fstat(fd)
        try:
            current_info = lock_path.lstat()
        except OSError as exc:
            raise SessionError(
                f"Could not inspect session lock after opening: {lock_path}"
            ) from exc
        if info.st_nlink != 1 or current_info.st_nlink != 1:
            raise SessionError(f"Session lock has multiple hard links: {lock_path}")
        if (
            not stat.S_ISREG(info.st_mode)
            or not stat.S_ISREG(current_info.st_mode)
            or (info.st_dev, info.st_ino) != (current_info.st_dev, current_info.st_ino)
            or (
                path_info is not None
                and (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
            )
        ):
            raise SessionError(f"Session lock changed while being opened: {lock_path}")
        if os.name == "posix":
            os.fchmod(fd, PRIVATE_FILE_MODE)
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


def _session_file_has_complete_tail(path: Path) -> bool:
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SessionError(f"Could not inspect session file: {path}") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise SessionError(f"Session file is not a regular file: {path}")

    expected_signature = (path_info.st_dev, path_info.st_ino)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SessionError(f"Session file is not a regular file: {path}") from exc
    try:
        info = _validated_session_file_info(path, fd, expected_signature)
        if info.st_size == 0:
            return False
        os.lseek(fd, -1, os.SEEK_END)
        return _read_exact(fd, 1) == b"\n"
    finally:
        os.close(fd)


def _recover_incomplete_tail(path: Path) -> bool:
    """Discard bytes after the final newline; return whether the file changed."""

    if _session_file_has_complete_tail(path):
        return False
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SessionError(f"Could not inspect session file: {path}") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise SessionError(f"Session file is not a regular file: {path}")
    if path_info.st_nlink != 1:
        raise SessionError(f"Session file has multiple hard links: {path}")
    expected_signature = (path_info.st_dev, path_info.st_ino)
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SessionError(f"Could not recover incomplete session file: {path}") from exc
    try:
        info = _validated_session_file_info(path, fd, expected_signature)
        if info.st_size == 0:
            committed_size = 0
        else:
            os.lseek(fd, -1, os.SEEK_END)
            if _read_exact(fd, 1) == b"\n":
                return False
            position = info.st_size
            committed_size = 0
            while position:
                chunk_size = min(position, 64 * 1024)
                position -= chunk_size
                os.lseek(fd, position, os.SEEK_SET)
                chunk = _read_exact(fd, chunk_size)
                newline = chunk.rfind(b"\n")
                if newline != -1:
                    committed_size = position + newline + 1
                    break
            if os.fstat(fd).st_nlink != 1:
                raise SessionError(f"Session file has multiple hard links: {path}")
            os.ftruncate(fd, committed_size)
            os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))
            _sync_file(fd)
        signature = (info.st_dev, info.st_ino)
    finally:
        os.close(fd)

    if committed_size == 0:
        _unlink_if_same_file(path, signature)
        _sync_directory(path.parent)
    return True


def _validated_session_file_info(
    path: Path,
    fd: int,
    expected_signature: tuple[int, int],
) -> os.stat_result:
    info = os.fstat(fd)
    try:
        current_info = path.lstat()
    except OSError as exc:
        raise SessionError(f"Could not inspect session file after opening: {path}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or not stat.S_ISREG(current_info.st_mode)
        or (info.st_dev, info.st_ino) != expected_signature
        or (current_info.st_dev, current_info.st_ino) != expected_signature
    ):
        raise SessionError(f"Session file changed while being opened: {path}")
    return info


def _prepare_session_file(path: Path) -> bool:
    state = _session_file_state(path)
    with state.lock:
        with _interprocess_lock(path, prepare_parent=False):
            changed = _recover_incomplete_tail(path)
            if changed:
                state.generation += 1
            try:
                return path.stat().st_size > 0
            except FileNotFoundError:
                return False


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


def _unlink_expected_file(path: Path, expected: tuple[int, int]) -> None:
    """Remove the expected live file, reporting disappearance or replacement."""

    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionError(f"Could not inspect session file before deletion: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expected:
        raise SessionError(f"Session file changed before deletion: {path}")
    path.unlink()


def _unlink_if_same_file(path: Path, expected: tuple[int, int]) -> None:
    """Best-effort cleanup only while the path still names the expected inode."""

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


def _page_cache_path(path: Path) -> Path:
    return path.with_suffix(f"{path.suffix}{SESSION_PAGE_CACHE_SUFFIX}")


def _serves_newest_page(
    *,
    limit: int,
    before_entry_id: str | None,
    after_entry_id: str | None,
    entry_ids: tuple[str, ...],
    full_content: bool,
    cached_count: int,
) -> bool:
    """Return whether a request is exactly the newest page a cache can answer.

    The cache holds only the newest slice of the active path, so it can satisfy an
    uncursored read no deeper than that slice. Every other shape — a cursor, an
    exact-id lookup, full content, or a larger limit — needs the resolved tree and
    must fall through to a complete read.
    """

    return (
        before_entry_id is None
        and after_entry_id is None
        and not entry_ids
        and not full_content
        and limit <= cached_count
    )


def _read_page_cache(
    path: Path, signature: _FileSignature
) -> tuple[tuple[MessageSessionEntry, ...], int] | None:
    """Return cached newest-page messages, or ``None`` whenever anything is off.

    Every failure mode — missing, unreadable, truncated, corrupt, stale, or written
    by a different schema — resolves to ``None`` so the caller performs the ordinary
    complete read. The cache can make a session load faster; it must never change
    what the session says.
    """

    cache_path = _page_cache_path(path)
    try:
        info = cache_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        raw = cache_path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError):
        return None
    lines = raw.splitlines()
    if not lines:
        return None
    try:
        header = json.loads(lines[0])
    except ValueError:
        return None
    if not isinstance(header, dict):
        return None
    if header.get("cache_schema_version") != SESSION_PAGE_CACHE_SCHEMA_VERSION:
        return None
    if header.get("entry_schema_version") != SESSION_ENTRY_SCHEMA_VERSION:
        return None
    if tuple(header.get("signature") or ()) != signature:
        return None
    messages: list[MessageSessionEntry] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            entry = session_entry_from_json(line)
        except SessionError:
            return None
        if not isinstance(entry, MessageSessionEntry):
            return None
        messages.append(entry)
    if len(messages) != header.get("message_count"):
        # A partially written cache is indistinguishable from a complete one without
        # this check, and would silently serve a short page.
        return None
    total = header.get("total_message_count")
    if not isinstance(total, int) or total < len(messages):
        return None
    return tuple(messages), total


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


def _validate_message_page_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("message page limit must be at least 1")
    if limit > MAX_SESSION_MESSAGE_PAGE_LIMIT:
        raise ValueError(f"message page limit cannot exceed {MAX_SESSION_MESSAGE_PAGE_LIMIT}")


def _message_page_index_from_entries(
    entries: Iterable[SessionEntry],
) -> _MessagePageIndex:
    tree = resolve_session_tree(tuple(entries))
    messages = tuple(entry for entry in tree.active_path if isinstance(entry, MessageSessionEntry))
    return _MessagePageIndex(
        active_leaf_id=tree.active_leaf_id,
        messages=messages,
        positions={entry.id: index for index, entry in enumerate(messages)},
    )


def _message_page_from_index(
    index: _MessagePageIndex,
    *,
    session_id: str,
    path: Path,
    limit: int,
    before_entry_id: str | None,
    after_entry_id: str | None,
    entry_ids: tuple[str, ...] = (),
    complete_structure: bool = False,
    full_content: bool = False,
    truncated_override: bool | None = None,
) -> SessionMessagePage:
    """Build one page from a resolved active path.

    ``truncated_override`` states that older active-path history exists beyond the
    messages supplied. Only the newest-page cache needs it: it holds a slice rather
    than the whole path, so the usual ``len(candidates) > limit`` test would report
    a complete session and strand the reader with no way to page back.
    """

    _validate_message_page_limit(limit)
    if before_entry_id is not None and after_entry_id is not None:
        raise ValueError("message page cursors are mutually exclusive")
    if entry_ids and (before_entry_id is not None or after_entry_id is not None):
        raise ValueError("exact message entry IDs cannot be combined with page cursors")
    if full_content and not entry_ids:
        raise ValueError("full message content requires exact entry IDs")
    active_messages = index.messages
    if entry_ids:
        if len(entry_ids) > 16:
            raise ValueError("exact message entry lookup cannot exceed 16 entries")
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("exact message entry IDs must be unique")
        if full_content and len(entry_ids) != 1:
            raise ValueError("full message content requires exactly one entry ID")
        missing = tuple(entry_id for entry_id in entry_ids if entry_id not in index.positions)
        if missing:
            raise SessionError(f"Session message entry not found on active path: {missing[0]}")
        requested = frozenset(entry_ids)
        selected = tuple(entry for entry in active_messages if entry.id in requested)
        truncated = False
    elif after_entry_id is not None:
        cursor_index = index.positions.get(after_entry_id)
        if cursor_index is None:
            raise SessionError(f"Session message cursor not found: {after_entry_id}")
        candidates = active_messages[cursor_index + 1 :]
        truncated = len(candidates) > limit
        selected = candidates[:limit]
    else:
        if before_entry_id is None:
            candidates = active_messages
        else:
            cursor_index = index.positions.get(before_entry_id)
            if cursor_index is None:
                raise SessionError(f"Session message cursor not found: {before_entry_id}")
            candidates = active_messages[:cursor_index]
        truncated = len(candidates) > limit
        selected = candidates[-limit:]
        if truncated_override is not None:
            truncated = truncated_override
    structural_argument_bytes = (
        _complete_structure_argument_bytes(selected) if complete_structure else 0
    )
    text_budget = (
        None
        if full_content
        else _MessagePageTextBudget(
            remaining=max(MESSAGE_PAGE_TEXT_BYTE_LIMIT - structural_argument_bytes, 0)
        )
    )
    newest_first_messages = tuple(
        _rpc_message_snapshot(
            entry,
            text_budget=text_budget,
            complete_structure=complete_structure or full_content,
        )
        for entry in reversed(selected)
    )
    return SessionMessagePage(
        session_id=session_id,
        path=path,
        active_leaf_id=index.active_leaf_id,
        messages=tuple(reversed(newest_first_messages)),
        truncated=truncated,
        next_before_entry_id=(
            selected[0].id
            if not entry_ids and after_entry_id is None and truncated and selected
            else None
        ),
        next_after_entry_id=(
            selected[-1].id
            if not entry_ids and after_entry_id is not None and truncated and selected
            else None
        ),
    )


def _rpc_message_snapshot(
    entry: MessageSessionEntry,
    *,
    text_budget: _MessagePageTextBudget | None,
    complete_structure: bool = False,
) -> RpcMessageSnapshot:
    message = entry.message
    skill_invocation = _rpc_skill_invocation_snapshot(
        message.skill_invocation,
        text_budget=text_budget,
    )
    if text_budget is None:
        content = message.content
        content_original_bytes = len(content.encode("utf-8"))
        content_truncated = False
    else:
        content, content_original_bytes, content_truncated = _clip_text_with_budget(
            message.content,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
    tool_calls = message.tool_calls or ()
    selected_tool_calls = tool_calls if complete_structure else tool_calls[:MESSAGE_TOOL_CALL_LIMIT]
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
            _rpc_tool_call_snapshot(
                tool_call,
                text_budget=text_budget,
                preserve_process_identity=complete_structure,
            )
            for tool_call in selected_tool_calls
        ),
        tool_calls_original_count=len(tool_calls),
        tool_calls_truncated=not complete_structure and len(tool_calls) > MESSAGE_TOOL_CALL_LIMIT,
        response_id=message.response_id,
        finish_reason=message.finish_reason,
        is_error=message.is_error,
        usage=message.usage,
        cost=message.cost,
        tool_result=_rpc_tool_result_snapshot(entry.tool_result, text_budget=text_budget),
        skill_invocation=skill_invocation,
    )


def _rpc_skill_invocation_snapshot(
    invocation: SkillInvocationEvidence | None,
    *,
    text_budget: _MessagePageTextBudget | None,
) -> RpcSkillInvocationSnapshot | None:
    if invocation is None:
        return None
    if text_budget is None:
        request = invocation.request
        request_bytes = len(request.encode("utf-8"))
        request_truncated = False
        original = invocation.original_content
        original_bytes = len(original.encode("utf-8"))
        original_truncated = False
    else:
        request, request_bytes, request_truncated = _clip_text_with_budget(
            invocation.request,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        original, original_bytes, original_truncated = _clip_text_with_budget(
            invocation.original_content,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
    return RpcSkillInvocationSnapshot(
        name=invocation.name,
        original_content=original,
        original_content_bytes=original_bytes,
        original_content_truncated=original_truncated,
        request=request,
        request_bytes=request_bytes,
        request_truncated=request_truncated,
        content_sha256=invocation.content_sha256,
        instructions_truncated=invocation.instructions_truncated,
    )


def _rpc_tool_result_snapshot(
    tool_result: ToolResultPresentationSnapshot | None,
    *,
    text_budget: _MessagePageTextBudget | None,
) -> RpcMessageToolResultSnapshot | None:
    if tool_result is None:
        return None
    before_text = tool_result.before_text
    truncated = tool_result.truncated
    if before_text is not None and text_budget is not None:
        clipped_before_text, _, before_text_truncated = _clip_text_with_budget(
            before_text,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        before_text = None if before_text_truncated else clipped_before_text
        truncated = truncated or before_text_truncated
    summary = tool_result.summary
    if summary is not None and text_budget is not None:
        summary, _, summary_truncated = _clip_text_with_budget(
            summary,
            limit=MESSAGE_CONTENT_BYTE_LIMIT,
            text_budget=text_budget,
        )
        truncated = truncated or summary_truncated
    return RpcMessageToolResultSnapshot(
        status=tool_result.status,
        exit_code=tool_result.exit_code,
        output_has_exit_status=tool_result.output_has_exit_status,
        before_text=before_text,
        created=tool_result.created,
        summary=summary,
        truncated=truncated,
    )


def _rpc_tool_call_snapshot(
    tool_call: ToolCallSnapshot,
    *,
    text_budget: _MessagePageTextBudget | None,
    preserve_process_identity: bool = False,
) -> RpcMessageToolCallSnapshot:
    process_identity = (
        _process_tool_identity_arguments(tool_call) if preserve_process_identity else None
    )
    if text_budget is None:
        clipped_arguments = tool_call.arguments
        original_bytes = len(
            json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        truncated = False
    elif process_identity is not None:
        original_bytes = _json_object_byte_count(tool_call.arguments)
        preview_arguments = {
            key: value for key, value in tool_call.arguments.items() if key not in process_identity
        }
        if preview_arguments:
            clipped_preview, _preview_bytes, truncated = _clip_json_object(
                preview_arguments,
                limit=TOOL_ARGUMENTS_BYTE_LIMIT,
                text_budget=text_budget,
            )
        else:
            clipped_preview = {}
            truncated = False
        clipped_arguments = {**clipped_preview, **process_identity}
    else:
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


def _complete_structure_argument_bytes(entries: Sequence[MessageSessionEntry]) -> int:
    """Reserve the bounded process keys needed to group complete-history rows."""

    return sum(
        _json_object_byte_count(identity)
        for entry in entries
        for tool_call in entry.message.tool_calls or ()
        if (identity := _process_tool_identity_arguments(tool_call)) is not None
    )


def _process_tool_identity_arguments(tool_call: ToolCallSnapshot) -> JsonObject | None:
    """Return the structural Bash poll/cancel keys needed by transcript replay."""

    if tool_call.name != "bash":
        return None
    operation = tool_call.arguments.get("operation")
    process_id = tool_call.arguments.get("process_id")
    if not isinstance(operation, str) or operation not in {"poll", "cancel"}:
        return None
    if not isinstance(process_id, str) or not process_id.strip():
        return None
    return {"operation": operation, "process_id": process_id}


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
    state = _session_file_state(path)
    with state.lock:
        with _interprocess_lock(path, prepare_parent=False):
            if _recover_incomplete_tail(path):
                state.generation += 1
            return _read_session_summary_metadata_unlocked(path)


def _read_session_summary_metadata_unlocked(path: Path) -> _SessionSummaryMetadata:
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
    message_payload = raw.get("message")
    if version <= 5 and isinstance(message_payload, dict) and "skill_invocation" in message_payload:
        raise MalformedSessionEntryError(
            f"V{version} message session entries cannot include skill_invocation{location}"
        )
    if version == 1:
        return _v1_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version == 2:
        return _v2_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version in {3, 4}:
        return _v3_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
    if version == 5:
        if raw.get("kind") == "active_leaf" and "reason" not in raw:
            raise MalformedSessionEntryError(
                f"V5 active-leaf session entries require reason{location}"
            )
        return _v5_summary_entry_metadata(raw, location=location, parent_id=legacy_parent_id)
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
    state = _session_file_state(path)
    with state.lock:
        with _interprocess_lock(path, prepare_parent=False):
            if _recover_incomplete_tail(path):
                state.generation += 1
            return _read_entries_unlocked(path, limit=limit)


def _read_entries_unlocked(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
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
