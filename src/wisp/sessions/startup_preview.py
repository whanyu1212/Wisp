"""Read-only startup excerpts passed between Wisp processes over a private pipe."""

from __future__ import annotations

import gc
import hashlib
import io
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, BinaryIO

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from wisp.messages import Message
from wisp.sessions.entries import MessageSessionEntry, SessionEntry
from wisp.sessions.errors import SessionError
from wisp.sessions.file_io import FileSignature, session_file_signature
from wisp.sessions.jsonl import decode_session_entries
from wisp.sessions.pagination import DEFAULT_SESSION_MESSAGE_PAGE_LIMIT
from wisp.sessions.replay import replay_session_entries

_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_PACKET_BYTES = 512 * 1024
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_MESSAGES = 8
_MAX_EXCERPT_CHARS = 4096
# Leave line breaks intact. Replace terminal controls and Unicode bidi overrides.
_CONTROLS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_REQUEST = TypeAdapter(tuple[str, str])


class _Packet(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: str
    session_id: str
    cwd: str
    cwd_identity: tuple[int, int]
    signature: FileSignature
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    excerpts: tuple[
        Annotated[str, Field(pattern=r"(?s)^(User|Assistant): .", max_length=_MAX_EXCERPT_CHARS)],
        ...,
    ] = Field(min_length=1, max_length=_MAX_MESSAGES)


def _working_directory(cwd: Path) -> tuple[str, tuple[int, int]]:
    if not cwd.is_absolute():
        raise ValueError("An absolute working directory is required")
    canonical = cwd.resolve(strict=True)
    info = canonical.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Expected a working directory")
    return str(canonical), (info.st_dev, info.st_ino)


@contextmanager
def _parent_directory(path: Path) -> Iterator[int]:
    """Pin each component before opening its child so ancestor links cannot redirect a read."""
    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise ValueError("Expected an absolute session path without parent traversal")
    flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY
    directory = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def _file_signature(info: os.stat_result) -> FileSignature:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SessionError("Expected a regular, unaliased session file")
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SessionError("Expected a user-owned session without group or other write access")
    if not 0 < info.st_size <= _MAX_FILE_BYTES:
        raise SessionError("Session size is outside the startup preview budget")
    return session_file_signature(info)


@contextmanager
def _snapshot(path: Path) -> Iterator[tuple[BinaryIO, FileSignature]]:
    # A platform without safe descriptor-relative opens falls back to normal hydration.
    if os.name != "posix":
        raise SessionError("Descriptor-relative session reads are unavailable")
    with _parent_directory(path) as parent:
        selected = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        signature = _file_signature(selected)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path.name, flags, dir_fd=parent)
    with os.fdopen(fd, "rb", buffering=0) as stream:
        opened = os.fstat(stream.fileno())
        if _file_signature(opened) != signature or opened.st_ctime_ns != selected.st_ctime_ns:
            raise SessionError("Session changed while opening")
        yield stream, signature
        # Re-walk from the root. The original parent can itself have been replaced.
        with _parent_directory(path) as parent:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if _file_signature(current) != signature or current.st_ctime_ns != opened.st_ctime_ns:
            raise SessionError("Session changed while reading")


def _display_excerpt(message: Message) -> str | None:
    if message.role not in ("user", "assistant"):
        return None
    text = message.user_visible_content
    if not text or text.isspace():
        return None
    label = f"{message.role.capitalize()}: "
    available = _MAX_EXCERPT_CHARS - len(label)
    if len(text) > available:
        text = "…" + text[-(available - 1) :]
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return label + _CONTROLS.sub("�", text)


def _excerpts(entries: list[SessionEntry], cwd: str) -> tuple[str, ...]:
    replay = replay_session_entries(entries)
    if replay.cwd is not None and replay.cwd != Path(cwd):
        return ()
    active = set(replay.path_entry_ids)
    messages = [
        entry.message
        for entry in entries
        if isinstance(entry, MessageSessionEntry) and entry.id in active
    ]
    if len(messages) <= DEFAULT_SESSION_MESSAGE_PAGE_LIMIT:
        return ()

    latest: list[str] = []
    for message in reversed(messages):
        excerpt = _display_excerpt(message)
        if excerpt is not None:
            latest.append(excerpt)
            if len(latest) == _MAX_MESSAGES:
                break
    return tuple(reversed(latest))


def prepare_startup_preview(path: Path, cwd: Path) -> bytes | None:
    """Replay a stable saved session and prepare excerpts for the private worker pipe.

    Args:
        path (Path): Absolute session file selected by the parent.
        cwd (Path): Absolute working directory for this launch.

    Returns:
        bytes | None: Bounded JSON, or None when normal hydration should take over.
    """
    try:
        directory, identity = _working_directory(cwd)
        with _snapshot(path) as (stream, signature):
            size = signature[2]
            raw = stream.read(size)
            if len(raw) != size or not raw.endswith(b"\n"):
                return None
            digest = hashlib.sha256(raw).hexdigest()
            with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8") as lines:
                entries = decode_session_entries(lines, path)
            excerpts = _excerpts(entries, directory)
        if not excerpts or _working_directory(cwd) != (directory, identity):
            return None
        packet = _Packet(
            path=str(path),
            session_id=entries[0].session_id,
            cwd=directory,
            cwd_identity=identity,
            signature=signature,
            sha256=digest,
            excerpts=excerpts,
        )
        data = packet.model_dump_json().encode()
        return data if len(data) <= _MAX_PACKET_BYTES else None
    except (OSError, ValueError, RuntimeError, TypeError, OverflowError):
        return None


def verify_startup_preview(
    data: bytes, path: Path, session_id: str, cwd: Path
) -> tuple[str, ...] | None:
    """Check the worker's snapshot against the backend selection without replaying JSONL.

    Args:
        data (bytes): Packet from a trusted Wisp worker over a parent-created anonymous
            pipe. The digest binds the source file, and the pipe authenticates the excerpts.
        path (Path): Session file independently selected by the backend.
        session_id (str): ID captured by the backend when it selected that file.
        cwd (Path): Backend working directory.

    Returns:
        tuple[str, ...] | None: Display-only excerpts, or None if verification fails.
    """
    if not 0 < len(data) <= _MAX_PACKET_BYTES:
        return None
    try:
        packet = _Packet.model_validate_json(data)
        directory = (packet.cwd, packet.cwd_identity)
        if packet.path != str(path) or packet.session_id != session_id:
            return None
        if directory != _working_directory(cwd):
            return None
        with _snapshot(path) as (stream, signature):
            if signature != packet.signature:
                return None
            remaining = signature[2]
            digest = hashlib.sha256()
            while remaining:
                chunk = stream.read(min(remaining, 256 * 1024))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
        if digest.hexdigest() == packet.sha256 and directory == _working_directory(cwd):
            return packet.excerpts
    except (OSError, ValueError, RuntimeError, OverflowError):
        pass
    return None


def _main() -> None:
    try:
        request = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(request) > _MAX_REQUEST_BYTES:
            return
        path, cwd = _REQUEST.validate_json(request, strict=True)
        # This one-request worker exits after replay. Do not repeatedly trace its
        # live JSONL tree while building it; the backend keeps its normal GC policy.
        gc.disable()
        packet = prepare_startup_preview(Path(path), Path(cwd))
        if packet is not None:
            sys.stdout.buffer.write(packet)
            sys.stdout.buffer.flush()
    except (OSError, ValueError, RuntimeError):
        return


if __name__ == "__main__":
    _main()
