"""Remember the addresses the user opened in the live browser, inside its saved profile.

The list lives in the profile directory, so file tools treat it as protected and
``wisp browser clear`` removes it with the rest of the browsing data.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wisp.coding.browser_control import BrowserControlError
from wisp.coding.browser_live import validate_browser_url
from wisp.coding.browser_profile import PROFILE_MARKER_NAME
from wisp.events import MAX_BROWSER_URL_CHARS, MAX_RECENT_BROWSER_ADDRESSES
from wisp.sessions.errors import SessionError
from wisp.sessions.file_io import PRIVATE_FILE_MODE, sync_file, write_all

RECENT_ADDRESSES_FILENAME = "recent-addresses.json"
# Twenty maximum-length URLs fit comfortably; anything larger is not Wisp's file.
_MAX_FILE_BYTES = 256 * 1024
# The page is already open when the list is saved, so a busy lock must not hold it up.
_LOCK_TIMEOUT_SECONDS = 1.0
_LOCK_POLL_SECONDS = 0.05
# O_NONBLOCK keeps a FIFO planted at either path from blocking the open.
_NO_FOLLOW_NONBLOCK = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class RecentAddressesBusyError(Exception):
    """Another process held the list's lock for too long, so nothing was written."""


def load_recent_addresses(profile_dir: Path) -> tuple[str, ...]:
    """Read the remembered addresses, newest first.

    Bad content is ignored rather than reported. A directory that is not a Wisp
    profile yields no addresses, as does a list that is missing, unreadable,
    oversized, malformed, or not a regular file owned by the current user with a
    single link. Entries that are not valid HTTP or HTTPS URLs of at most 2,048
    characters, or that cannot be encoded as UTF-8, are dropped.

    Args:
        profile_dir (Path): Wisp's browser profile directory.

    Returns:
        tuple[str, ...]: At most 20 distinct URLs, newest first.
    """
    try:
        if not _is_profile(profile_dir):
            return ()
        content = _read_private_file(profile_dir / RECENT_ADDRESSES_FILENAME)
        entries = json.loads(content) if content is not None else None
    except (OSError, ValueError, RecursionError):
        return ()
    if not isinstance(entries, list):
        return ()
    addresses: list[str] = []
    for entry in entries:
        if len(addresses) == MAX_RECENT_BROWSER_ADDRESSES:
            break
        if (
            isinstance(entry, str)
            and len(entry) <= MAX_BROWSER_URL_CHARS
            and entry not in addresses
            and _is_valid_entry(entry)
        ):
            addresses.append(entry)
    return tuple(addresses)


def record_recent_address(profile_dir: Path, url: str) -> None:
    """Move ``url`` to the front of the remembered addresses. Safe to repeat.

    Nothing is written unless ``profile_dir`` is an existing Wisp profile, so this
    never creates the profile or writes into an unrelated directory. Concurrent
    Wisp processes serialize the read-modify-write on a lock file beside the list,
    not on the profile lock that Chromium holds while it runs. Waiting for that
    lock is bounded to about a second.

    Args:
        profile_dir (Path): Wisp's browser profile directory.
        url (str): Validated HTTP or HTTPS URL the browser opened.

    Raises:
        OSError: The list or its lock file could not be opened or written.
        SessionError: The lock file is not a private regular file with one link.
        RecentAddressesBusyError: Another process held the lock for too long.
    """
    if not _is_profile(profile_dir):
        return
    path = profile_dir / RECENT_ADDRESSES_FILENAME
    with _bounded_lock(path):
        previous = load_recent_addresses(profile_dir)
        addresses = [url, *(address for address in previous if address != url)]
        # Raw UTF-8 keeps non-ASCII URLs within the size the reader accepts.
        content = json.dumps(addresses[:MAX_RECENT_BROWSER_ADDRESSES], ensure_ascii=False)
        _replace_private_file(path, content.encode())


def _is_profile(profile_dir: Path) -> bool:
    try:
        marker = (profile_dir / PROFILE_MARKER_NAME).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(marker.st_mode)


def _read_private_file(path: Path) -> bytes | None:
    with open(os.open(path, os.O_RDONLY | _NO_FOLLOW_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            return None
        content = file.read(_MAX_FILE_BYTES + 1)
    return content if len(content) <= _MAX_FILE_BYTES else None


def _is_valid_entry(url: str) -> bool:
    try:
        # A lone surrogate survives JSON decoding but cannot be sent in an event.
        url.encode()
        validate_browser_url(url)
    except (UnicodeEncodeError, BrowserControlError):
        return False
    return True


@contextmanager
def _bounded_lock(path: Path) -> Iterator[None]:
    import fcntl

    lock_path = path.with_name(f"{path.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | _NO_FOLLOW_NONBLOCK, PRIVATE_FILE_MODE)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SessionError(f"Recent addresses lock is not a single-link file: {lock_path}")
        os.fchmod(fd, PRIVATE_FILE_MODE)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RecentAddressesBusyError(
                        f"Another process held {lock_path} for {_LOCK_TIMEOUT_SECONDS:g}s"
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        # Closing the only descriptor also releases the lock.
        os.close(fd)


def _replace_private_file(path: Path, content: bytes) -> None:
    # mkstemp creates a new 0600 file with O_EXCL, so no existing file is reused.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        try:
            write_all(fd, content)
            sync_file(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
