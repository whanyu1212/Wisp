"""Append-only JSONL session persistence."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import anyio

from wisp.agent.messages import Message
from wisp.events import (
    JsonObject,
    KnownWispEvent,
    WispEvent,
)
from wisp.sessions.branching import (
    SessionBranchProjection,
    project_fork_from_user_message,
    project_session_path,
)
from wisp.sessions.catalog import SessionCatalogPage, read_catalog_page
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    SessionInfoSessionEntry,
    is_session_tree_entry,
    session_entry_from_json,
    session_entry_to_json,
    typed_event_from_envelope,
)
from wisp.sessions.errors import (
    AmbiguousSessionError,
    MalformedSessionEntryError,
    SessionError,
    SessionNavigationCancelledError,
    SessionNotFoundError,
    SessionUnrevertUnavailableError,
    StaleSessionTreeError,
    StaleSessionWriterError,
)
from wisp.sessions.file_io import (
    PRIVATE_FILE_MODE,
    FileSignature,
    ensure_private_directory,
    interprocess_lock,
    prepare_session_file,
    recover_incomplete_tail,
    session_file_signature,
    session_file_state,
    sync_directory,
    sync_file,
    unlink_expected_file,
    unlink_if_same_file,
    write_all,
)
from wisp.sessions.pagination import (
    DEFAULT_SESSION_MESSAGE_PAGE_LIMIT,
    DEFAULT_SESSION_TREE_PAGE_LIMIT,
    MAX_SESSION_MESSAGE_PAGE_LIMIT,
    MAX_SESSION_TREE_PAGE_LIMIT,
    MessagePageIndex,
    SessionMessagePage,
    SessionTreeNodeSummary,
    SessionTreePage,
    message_page_from_index,
    message_page_index_from_entries,
    tree_page_from_entries,
    validate_message_page_limit,
)
from wisp.sessions.replay import (
    SessionReplay,
    SessionReplayError,
    StaleCompactionError,
    replay_session_entries,
    resolve_session_tree,
)
from wisp.sessions.summaries import read_session_summary_metadata


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
    name: str | None = None


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

    def catalog_page(
        self,
        *,
        limit: int = 50,
        query: str = "",
        cursor: str | None = None,
        check_cancelled: Callable[[], None] = lambda: None,
    ) -> SessionCatalogPage:
        """Search current session names and IDs using bounded, stable pages.

        Args:
            limit (int): Maximum summaries, from zero through 200.
            query (str): Case-insensitive literal name or ID substring.
            cursor (str | None): Opaque continuation from an earlier result.
            check_cancelled (Callable): Raise to stop scanning cancelled work.

        Returns:
            SessionCatalogPage: Results with opaque forward/backward cursors.

        Raises:
            ValueError: Query or limit bounds are invalid.
            SessionError: Metadata is invalid or the catalog cursor is stale.
        """
        return read_catalog_page(
            files=self._session_files,
            summary=lambda path: self._summary_for_path(path, check_cancelled=check_cancelled),
            limit=limit,
            query=query,
            cursor=cursor,
            check_cancelled=check_cancelled,
        )

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

    def _summary_for_path(
        self, path: Path, *, check_cancelled: Callable[[], None] | None = None
    ) -> SessionSummary:
        info = path.stat()
        metadata = read_session_summary_metadata(path, check_cancelled=check_cancelled)
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
        files = (path for path in self.root.glob("*.jsonl") if prepare_session_file(path))
        return tuple(sorted(files, key=lambda path: path.name))


class JsonlSession:
    """A single append-only JSONL session file."""

    def __init__(self, *, session_id: str, path: Path) -> None:
        self.session_id = session_id
        self.path = path
        self._append_lock = anyio.Lock()
        self._file_state = session_file_state(path)
        self._entry_index: dict[str, SessionEntry] | None = None
        self._entry_index_generation: int | None = None
        self._entry_index_signature: FileSignature | None = None
        self._message_page_index: MessagePageIndex | None = None

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
                    name=_session_name_from_entries(entries),
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
        """Read an active-path transcript page in chronological order.

        Complete-structure readers receive the original text and tool arguments;
        pagination bounds the number of messages without discarding their content.

        Args:
            limit (int): Maximum number of messages in this page.
            before_entry_id (str | None): Exclusive cursor for an older page.
            after_entry_id (str | None): Exclusive cursor for a newer page.
            entry_ids (tuple[str, ...]): Exact active-path entries to retrieve instead
                of a cursor-based page.
            complete_structure (bool): Preserve all tool calls, text, and arguments.
            full_content (bool): Retrieve one exact entry without preview limits.

        Returns:
            SessionMessagePage: Chronological messages and continuation cursors.

        Raises:
            ValueError: Pagination bounds or selectors are invalid.
            SessionError: The session or requested active-path entry cannot be read.
        """

        validate_message_page_limit(limit)
        with self._file_state.lock:
            with self._interprocess_lock(prepare_parent=False):
                if self._validate_session_file() is None:
                    raise SessionNotFoundError(f"Session file does not exist: {self.path}")
                self._refresh_entry_index()
                if self._message_page_index is None:
                    assert self._entry_index is not None
                    self._message_page_index = message_page_index_from_entries(
                        self._entry_index.values()
                    )
                return message_page_from_index(
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
        return tree_page_from_entries(
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

        ensure_private_directory(self.path.parent)
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
                unlink_if_same_file(temp_path, created_signature)
                if published:
                    unlink_if_same_file(self.path, created_signature)
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
        self._entry_index_signature = session_file_signature(final_info)

    def _append_line(self, line: str) -> None:
        """Append and sync one newline-committed record, rolling back on failure."""

        ensure_private_directory(self.path.parent)
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
            write_all(fd, data)
            sync_file(fd)
            if not existed:
                sync_directory(self.path.parent)
        except Exception as append_error:
            try:
                os.ftruncate(fd, original_size)
                sync_file(fd)
            except OSError as rollback_error:
                raise SessionError(
                    "Session append failed and rollback could not be synchronized for "
                    f"{self.path}: append error: {append_error}"
                ) from rollback_error
            finally:
                os.close(fd)
            if not existed and original_size == 0:
                unlink_if_same_file(self.path, signature)
                sync_directory(self.path.parent)
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
        ensure_private_directory(self.path.parent)
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
        # Index what a later read of the file returns, not the in-memory object:
        # fields excluded from serialization (such as a message's transient
        # `prompt_cache_boundary`) must not survive only in this cache, or
        # branch projections would no longer match their own re-read copy.
        self._entry_index[persisted.id] = _as_persisted(persisted)
        self._entry_index_generation = self._file_state.generation
        self._entry_index_signature = session_file_signature(info)
        self._invalidate_message_page_index()
        return persisted

    def _persisted_entry_locked(self, entry: SessionEntry) -> SessionEntry | None:
        assert self._entry_index is not None
        existing = self._entry_index.get(entry.id)
        if existing is None:
            return None
        # The index holds entries as persisted; compare the retry the same way.
        candidate = _as_persisted(entry)
        if existing == candidate or _matches_detached_entry(existing, candidate):
            return existing
        raise SessionError(f"Session entry id conflicts with persisted data: {entry.id}")

    def _refresh_entry_index(self) -> None:
        self._recover_incomplete_tail_locked()
        info = self._validate_session_file()
        if info is None:
            self._entry_index = {}
            self._entry_index_generation = self._file_state.generation
            self._entry_index_signature = None
            self._invalidate_message_page_index()
            return

        signature = session_file_signature(info)
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
        if recover_incomplete_tail(self.path):
            self._file_state.generation += 1
            self._invalidate_entry_index()

    @contextmanager
    def _interprocess_lock(self, *, prepare_parent: bool = True) -> Iterator[None]:
        """Serialize session access across cooperating Wisp processes."""

        with interprocess_lock(self.path, prepare_parent=prepare_parent):
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
                unlink_expected_file(self.path, (info.st_dev, info.st_ino))
                sync_directory(self.path.parent)
            return
        self._replace_lines([session_entry_to_json(entry) for entry in entries])

    def _replace_lines(self, lines: list[str]) -> None:
        """Atomically publish a complete replacement for the live session file."""

        ensure_private_directory(self.path.parent)
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
            write_all(fd, data)
            sync_file(fd)
            os.close(fd)
            fd = -1
            # Validate the staged JSONL before replacing the last committed file.
            _read_entries_unlocked(temp_path)
            os.replace(temp_path, self.path)
            signature = None
            sync_directory(self.path.parent)
        finally:
            if fd != -1:
                os.close(fd)
            if signature is not None:
                unlink_if_same_file(temp_path, signature)


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


def _as_persisted(entry: SessionEntry) -> SessionEntry:
    """Return ``entry`` as it reads back from disk, dropping serialization-excluded fields."""

    if isinstance(entry, MessageSessionEntry) and entry.message.prompt_cache_boundary:
        return entry.model_copy(
            update={"message": entry.message.model_copy(update={"prompt_cache_boundary": False})}
        )
    return entry


def _read_session_id(path: Path) -> str:
    for entry in _read_entries(path, limit=1):
        return entry.session_id
    raise SessionError(f"Session file is empty: {path}")


def _read_entries(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
    state = session_file_state(path)
    with state.lock:
        with interprocess_lock(path, prepare_parent=False):
            if recover_incomplete_tail(path):
                state.generation += 1
            return _read_entries_unlocked(path, limit=limit)


def _read_entries_unlocked(path: Path, *, limit: int | None = None) -> list[SessionEntry]:
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
    resolve_session_tree(entries)
    return entries
