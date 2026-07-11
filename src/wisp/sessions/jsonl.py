"""Append-only JSONL session persistence."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import anyio
from pydantic import ValidationError

from wisp.agent.messages import Message, SessionEntry
from wisp.events import JsonObject, WispEvent

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class SessionError(RuntimeError):
    """Base error for session loading and persistence failures."""


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
        self._entry_index: dict[str, SessionEntry] | None = None

    async def append_message(self, message: Message) -> SessionEntry:
        entry = SessionEntry(session_id=self.session_id, message=message)
        return await self.append_entry(entry)

    async def append_event(self, event: WispEvent) -> SessionEntry:
        """Persist a structured runtime event for audit/debugging."""

        entry = SessionEntry(
            session_id=self.session_id,
            kind="event",
            event=event.model_dump(mode="json"),
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

    async def truncate_entries(self, count: int) -> None:
        """Remove entries after count, preserving the first count entries."""

        if count < 0:
            raise ValueError("Session entry count cannot be negative")
        async with self._append_lock:
            try:
                await anyio.to_thread.run_sync(self._truncate_entries, count)
            finally:
                self._entry_index = None

    def read_entries(self) -> tuple[SessionEntry, ...]:
        """Read all persisted entries from the session file."""

        return tuple(_read_entries(self.path))

    def read_messages(self) -> tuple[Message, ...]:
        """Read all persisted messages from the session file."""

        return tuple(
            entry.message
            for entry in self.read_entries()
            if entry.kind == "message" and entry.message is not None
        )

    def read_events(self) -> tuple[JsonObject, ...]:
        """Read all persisted structured events from the session file."""

        return tuple(
            entry.event
            for entry in self.read_entries()
            if entry.kind == "event" and entry.event is not None
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
        _ensure_private_directory(self.path.parent)
        if not self._validate_session_file():
            self._entry_index = {}
        elif self._entry_index is None:
            self._entry_index = self._load_entry_index()

        existing = self._entry_index.get(entry.id)
        if existing is not None:
            if existing == entry:
                return
            raise SessionError(f"Session entry id conflicts with persisted data: {entry.id}")

        try:
            self._append_line(entry.model_dump_json(exclude_none=True))
        except Exception:
            self._entry_index = None
            raise
        self._entry_index[entry.id] = entry

    def _load_entry_index(self) -> dict[str, SessionEntry]:
        entries: dict[str, SessionEntry] = {}
        for entry in _read_entries(self.path):
            if entry.id in entries:
                raise SessionError(f"Duplicate session entry id: {entry.id}")
            entries[entry.id] = entry
        return entries

    def _validate_session_file(self) -> bool:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SessionError(f"Could not inspect session file: {self.path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SessionError(f"Session file is not a regular file: {self.path}")
        return True

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
    try:
        with path.open("r", encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                if not line.strip():
                    continue
                try:
                    entries.append(SessionEntry.model_validate_json(line))
                except ValidationError as exc:
                    raise SessionError(f"Invalid session entry at {path}:{line_number}") from exc
                if limit is not None and len(entries) >= limit:
                    break
    except UnicodeDecodeError as exc:
        raise SessionError(f"Session file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise SessionError(f"Could not read session file: {path}") from exc
    return entries
