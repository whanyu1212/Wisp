"""Private credential storage for Wisp providers."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal
from uuid import uuid4
from weakref import WeakValueDictionary

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class AuthStorageError(RuntimeError):
    """Raised when Wisp auth credentials cannot be read or written."""


@dataclass(frozen=True)
class ApiKeyCredential:
    """Persisted API key credential."""

    key: str
    type: Literal["api_key"] = "api_key"

    def to_json(self) -> dict[str, object]:
        return {"type": self.type, "key": self.key}


@dataclass(frozen=True)
class OAuthCredential:
    """Persisted OAuth credential.

    ``expires`` is milliseconds since the Unix epoch, matching the credential
    shape used by Codex-compatible auth flows.
    """

    access: str
    refresh: str
    expires: int
    account_id: str | None = None
    type: Literal["oauth"] = "oauth"

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "type": self.type,
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
        }
        if self.account_id is not None:
            data["accountId"] = self.account_id
        return data


type AuthCredential = ApiKeyCredential | OAuthCredential


class _AuthFileState:
    def __init__(self) -> None:
        self.lock = threading.Lock()


_AUTH_FILE_STATES_GUARD = threading.Lock()
_AUTH_FILE_STATES: WeakValueDictionary[Path, _AuthFileState] = WeakValueDictionary()


def _auth_file_state(path: Path) -> _AuthFileState:
    key = Path(os.path.abspath(path))
    with _AUTH_FILE_STATES_GUARD:
        state = _AUTH_FILE_STATES.get(key)
        if state is None:
            state = _AuthFileState()
            _AUTH_FILE_STATES[key] = state
        return state


class JsonAuthStore:
    """Locked, JSON-backed credential store with private file permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._file_state = _auth_file_state(self.path)

    def get(self, provider: str) -> AuthCredential | None:
        with self._file_state.lock:
            return self._load_unlocked().get(provider)

    def providers(self) -> tuple[str, ...]:
        with self._file_state.lock:
            return tuple(sorted(self._load_unlocked()))

    def set(self, provider: str, credential: AuthCredential) -> None:
        with self._mutation_lock():
            credentials = self._load_unlocked()
            credentials[provider] = credential
            self._publish_unlocked(credentials)

    def compare_and_set(
        self,
        provider: str,
        *,
        expected: AuthCredential,
        replacement: AuthCredential,
    ) -> bool:
        """Replace one credential only if it still equals the caller's snapshot."""

        with self._mutation_lock():
            credentials = self._load_unlocked()
            if credentials.get(provider) != expected:
                return False
            credentials[provider] = replacement
            self._publish_unlocked(credentials)
            return True

    def delete(self, provider: str) -> bool:
        with self._mutation_lock():
            credentials = self._load_unlocked()
            if provider not in credentials:
                return False
            del credentials[provider]
            self._publish_unlocked(credentials)
            return True

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        with self._file_state.lock:
            with _interprocess_lock(self.path):
                yield

    def _load_unlocked(self) -> dict[str, AuthCredential]:
        raw_text = _read_auth_file(self.path)
        if raw_text is None:
            return {}
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise AuthStorageError(f"Invalid auth file JSON: {self.path}") from exc
        if not isinstance(raw, dict):
            raise AuthStorageError(f"Auth file must contain a JSON object: {self.path}")
        return {
            str(provider): _credential_from_json(value, provider=str(provider))
            for provider, value in raw.items()
        }

    def _publish_unlocked(self, credentials: dict[str, AuthCredential]) -> None:
        _ensure_private_parent(self.path)
        payload = {
            provider: credential.to_json() for provider, credential in sorted(credentials.items())
        }
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
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
                raise AuthStorageError(f"Auth temporary file is not private: {temp_path}")
            if os.name == "posix":
                os.fchmod(fd, PRIVATE_FILE_MODE)
            _write_all(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp_path, self.path)
            signature = None
            _sync_directory(self.path.parent)
        except AuthStorageError:
            raise
        except OSError as exc:
            raise AuthStorageError(f"Could not write auth file: {self.path}") from exc
        finally:
            if fd != -1:
                os.close(fd)
            if signature is not None:
                _unlink_if_same_file(temp_path, signature)


def _credential_from_json(value: object, *, provider: str) -> AuthCredential:
    if not isinstance(value, dict):
        raise AuthStorageError(f"Invalid credential for provider {provider}: expected object")
    credential_type = value.get("type")
    if credential_type == "api_key":
        key = value.get("key")
        if not isinstance(key, str) or not key:
            raise AuthStorageError(f"Invalid API key credential for provider {provider}")
        return ApiKeyCredential(key=key)
    if credential_type == "oauth":
        access = value.get("access")
        refresh = value.get("refresh")
        expires = value.get("expires")
        account_id = value.get("accountId", value.get("account_id"))
        if not isinstance(access, str) or not access:
            raise AuthStorageError(f"Invalid OAuth access token for provider {provider}")
        if not isinstance(refresh, str) or not refresh:
            raise AuthStorageError(f"Invalid OAuth refresh token for provider {provider}")
        if not isinstance(expires, int):
            raise AuthStorageError(f"Invalid OAuth expiry for provider {provider}")
        if account_id is not None and not isinstance(account_id, str):
            raise AuthStorageError(f"Invalid OAuth account id for provider {provider}")
        return OAuthCredential(
            access=access,
            refresh=refresh,
            expires=expires,
            account_id=account_id,
        )
    raise AuthStorageError(
        f"Unsupported credential type for provider {provider}: {credential_type}"
    )


def _read_auth_file(path: Path) -> str | None:
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthStorageError(f"Could not inspect auth file: {path}") from exc
    _validate_auth_file_metadata(path, path_info)
    expected = (path_info.st_dev, path_info.st_ino)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AuthStorageError(f"Could not read auth file: {path}") from exc
    try:
        info = os.fstat(fd)
        try:
            current_info = path.lstat()
        except OSError as exc:
            raise AuthStorageError(f"Could not inspect auth file after opening: {path}") from exc
        if (info.st_dev, info.st_ino) != expected or (
            current_info.st_dev,
            current_info.st_ino,
        ) != expected:
            raise AuthStorageError(f"Auth file changed while being opened: {path}")
        _validate_auth_file_metadata(path, info)
        with os.fdopen(fd, "r", encoding="utf-8") as auth_file:
            fd = -1
            return auth_file.read()
    except UnicodeDecodeError as exc:
        raise AuthStorageError(f"Auth file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise AuthStorageError(f"Could not read auth file: {path}") from exc
    finally:
        if fd != -1:
            os.close(fd)


def _validate_auth_file_metadata(path: Path, info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise AuthStorageError(f"Auth file is not a regular file: {path}")
    if info.st_nlink != 1:
        raise AuthStorageError(f"Auth file has multiple hard links: {path}")
    if os.name != "posix":
        return
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise AuthStorageError(f"Auth file is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise AuthStorageError(f"Auth file is not private (expected mode 0600): {path}")


def _ensure_private_parent(path: Path) -> None:
    parent = path.parent
    try:
        parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise AuthStorageError(f"Could not create auth directory: {parent}") from exc
    try:
        info = parent.lstat()
    except OSError as exc:
        raise AuthStorageError(f"Could not inspect auth directory: {parent}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AuthStorageError(f"Auth directory is not a directory: {parent}")
    if os.name != "posix":
        return
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise AuthStorageError(f"Auth directory is not owned by the current user: {parent}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            parent.chmod(PRIVATE_DIR_MODE)
            info = parent.lstat()
        except OSError as exc:
            raise AuthStorageError(f"Could not secure auth directory: {parent}") from exc
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AuthStorageError(f"Auth directory is not private: {parent}")


@contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    """Serialize auth read-modify-write transactions across Wisp processes."""

    _ensure_private_parent(path)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise AuthStorageError(f"Could not inspect auth lock: {lock_path}") from exc
    if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise AuthStorageError(f"Auth lock is not a private regular file: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise AuthStorageError(f"Could not open auth lock: {lock_path}") from exc
    unlock: Callable[[], object] | None = None
    try:
        info = os.fstat(fd)
        try:
            current = lock_path.lstat()
        except OSError as exc:
            raise AuthStorageError(f"Could not inspect opened auth lock: {lock_path}") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or current.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
            or (before is not None and (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino))
        ):
            raise AuthStorageError(f"Auth lock changed while being opened: {lock_path}")
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
    except AuthStorageError:
        raise
    except OSError as exc:
        raise AuthStorageError(f"Could not lock auth file: {path}") from exc
    finally:
        if unlock is not None:
            unlock()
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("Auth write made no progress")
        view = view[written:]


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_if_same_file(path: Path, expected: tuple[int, int]) -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == expected:
        try:
            path.unlink()
        except OSError:
            pass


__all__ = [
    "ApiKeyCredential",
    "AuthCredential",
    "AuthStorageError",
    "JsonAuthStore",
    "OAuthCredential",
]
