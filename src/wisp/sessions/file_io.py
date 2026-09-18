"""Durable file primitives for JSONL session storage.

These helpers own the reliability and security boundary beneath the session
store: fsync ordering, cross-process locking, private directory and file
modes, and recovery of a partially written final line. They know nothing
about session entries.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from weakref import WeakValueDictionary

from wisp.sessions.errors import SessionError

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


FileSignature = tuple[int, int, int, int]


class SessionFileState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0


_SESSION_FILE_STATES_GUARD = threading.Lock()
_SESSION_FILE_STATES: WeakValueDictionary[Path, SessionFileState] = WeakValueDictionary()


def session_file_state(path: Path) -> SessionFileState:
    key = Path(os.path.abspath(path))
    with _SESSION_FILE_STATES_GUARD:
        state = _SESSION_FILE_STATES.get(key)
        if state is None:
            state = SessionFileState()
            _SESSION_FILE_STATES[key] = state
        return state


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("Session write made no progress")
        view = view[written:]


def read_exact(fd: int, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise OSError("Session read ended before the expected file size")
        data.extend(chunk)
    return bytes(data)


def sync_file(fd: int) -> None:
    os.fsync(fd)


def sync_directory(path: Path) -> None:
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
def interprocess_lock(path: Path, *, prepare_parent: bool = True) -> Iterator[None]:
    """Serialize access to one session across cooperating Wisp processes."""

    if prepare_parent:
        ensure_private_directory(path.parent)
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
        return read_exact(fd, 1) == b"\n"
    finally:
        os.close(fd)


def recover_incomplete_tail(path: Path) -> bool:
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
            if read_exact(fd, 1) == b"\n":
                return False
            position = info.st_size
            committed_size = 0
            while position:
                chunk_size = min(position, 64 * 1024)
                position -= chunk_size
                os.lseek(fd, position, os.SEEK_SET)
                chunk = read_exact(fd, chunk_size)
                newline = chunk.rfind(b"\n")
                if newline != -1:
                    committed_size = position + newline + 1
                    break
            if os.fstat(fd).st_nlink != 1:
                raise SessionError(f"Session file has multiple hard links: {path}")
            os.ftruncate(fd, committed_size)
            os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))
            sync_file(fd)
        signature = (info.st_dev, info.st_ino)
    finally:
        os.close(fd)

    if committed_size == 0:
        unlink_if_same_file(path, signature)
        sync_directory(path.parent)
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


def prepare_session_file(path: Path) -> bool:
    state = session_file_state(path)
    with state.lock:
        with interprocess_lock(path, prepare_parent=False):
            changed = recover_incomplete_tail(path)
            if changed:
                state.generation += 1
            try:
                return path.stat().st_size > 0
            except FileNotFoundError:
                return False


def ensure_private_directory(path: Path) -> None:
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


def unlink_expected_file(path: Path, expected: tuple[int, int]) -> None:
    """Remove the expected live file, reporting disappearance or replacement."""

    try:
        info = path.lstat()
    except OSError as exc:
        raise SessionError(f"Could not inspect session file before deletion: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expected:
        raise SessionError(f"Session file changed before deletion: {path}")
    path.unlink()


def unlink_if_same_file(path: Path, expected: tuple[int, int]) -> None:
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


def session_file_signature(info: os.stat_result) -> FileSignature:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
