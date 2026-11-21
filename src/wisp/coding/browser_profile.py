"""Manage the private Chrome profile for remembered Wisp live-browser sessions."""

from __future__ import annotations

import os
import shutil
import socket
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

from wisp.coding.browser_control import BrowserControlError

# An empty private file that marks a directory as Wisp's own browser profile.
PROFILE_MARKER_NAME = ".wisp-browser-profile"
_IN_USE = "The remembered profile is in use. Close its live browser and try again."


def _check_chromium_lock(profile_fd: int) -> None:
    # Chrome can outlive a killed Wisp host. Its POSIX SingletonLock is a symlink
    # to HOST-PID. Only a dead local process is proof that the lock is stale.
    try:
        target = os.readlink("SingletonLock", dir_fd=profile_fd)
    except FileNotFoundError:
        return
    host, _, pid = target.rpartition("-")
    if host == socket.gethostname() and pid.isascii() and pid.isdecimal():
        number = int(pid)
        if 0 < number < 2**31:
            try:
                os.kill(number, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
    raise BrowserControlError(_IN_USE)


class BrowserProfile(NamedTuple):
    path: Path
    pass_fds: tuple[int, ...]


def _lock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise BrowserControlError(_IN_USE) from exc


def _open_profile(profile: Path, parent_fd: int, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(profile.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
    # Publish the directory and its ownership marker together. An interrupted
    # first launch cannot leave an unmarked directory at the real profile path.
    with TemporaryDirectory(prefix=".wisp-browser-init-", dir=profile.parent) as temporary:
        staging = Path(temporary) / "profile"
        staging.mkdir(mode=0o700)
        (staging / PROFILE_MARKER_NAME).touch(mode=0o600)
        os.rename(staging, profile.name, dst_dir_fd=parent_fd)
    return os.open(profile.name, flags, dir_fd=parent_fd)


def _lock_marker(profile_fd: int, stack: ExitStack) -> int:
    info = os.fstat(profile_fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BrowserControlError("The remembered profile must be a private, user-owned directory")
    try:
        fd = os.open(
            PROFILE_MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=profile_fd
        )
    except FileNotFoundError as exc:
        raise BrowserControlError("The directory is not a Wisp-owned browser profile") from exc
    stack.callback(os.close, fd)
    marker = os.fstat(fd)
    if (
        not stat.S_ISREG(marker.st_mode)
        or marker.st_uid != os.getuid()
        or marker.st_mode & 0o077
        or marker.st_nlink != 1
        or marker.st_size != 0
    ):
        raise BrowserControlError("The directory is not a Wisp-owned browser profile")
    _lock(fd)
    return fd


@contextmanager
def _locked_profile(profile: Path, *, create: bool) -> Iterator[tuple[int, int] | None]:
    if os.name != "posix":
        raise BrowserControlError("Remembered browser profiles require macOS or Linux")
    if not profile.is_absolute() or ".." in profile.parts or not profile.name:
        raise BrowserControlError("The remembered profile needs an absolute user-local directory")
    with ExitStack() as stack:
        with _profile_errors():
            try:
                if create:
                    profile.parent.mkdir(mode=0o700, exist_ok=True)
                parent_fd = os.open(profile.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(parent_fd)
                    if info.st_uid != os.getuid() or info.st_mode & 0o022:
                        raise BrowserControlError(
                            "The Wisp profile parent must be owned and writable only by you"
                        )
                    _lock(parent_fd)
                    profile_fd = _open_profile(profile, parent_fd, create=create)
                    stack.callback(os.close, profile_fd)
                    marker_fd = _lock_marker(profile_fd, stack)
                finally:
                    os.close(parent_fd)
                _check_chromium_lock(profile_fd)
                handles: tuple[int, int] | None = (profile_fd, marker_fd)
            except FileNotFoundError:
                if create:
                    raise
                handles = None
        yield handles


@contextmanager
def browser_profile(profile_dir: Path) -> Iterator[BrowserProfile]:
    """Hold exclusive use of the saved profile, creating it on first use.

    Args:
        profile_dir: Wisp's user-local profile directory.

    Yields:
        The Chromium user-data directory and lock descriptor to pass to its process.

    Raises:
        BrowserControlError: The saved profile is unsafe, busy, or unavailable.
    """
    with _locked_profile(profile_dir, create=True) as handles:
        assert handles is not None
        profile_fd, marker_fd = handles
        with _profile_errors():
            try:
                os.unlink("DevToolsActivePort", dir_fd=profile_fd)
            except FileNotFoundError:
                pass
        # Inherit only the marker descriptor across exec. It covers the launch
        # handoff if Wisp exits before Chromium creates its own SingletonLock.
        yield BrowserProfile(profile_dir, (marker_fd,))


def clear_browser_profile(profile_dir: Path) -> None:
    """Remove all site data from an idle, Wisp-owned profile. Safe to repeat.

    Args:
        profile_dir: The user-local directory reserved for Wisp's browser.

    Raises:
        BrowserControlError: The profile is unsafe, in use, or could not be removed.
    """
    with _profile_errors(), _locked_profile(profile_dir, create=False) as handles:
        if handles is not None:
            profile_fd, _ = handles
            # Keep ownership verifiable if disk errors interrupt a clear.
            with os.scandir(profile_fd) as entries:
                for entry in entries:
                    if entry.name == PROFILE_MARKER_NAME:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.name, dir_fd=profile_fd)
                    else:
                        os.unlink(entry.name, dir_fd=profile_fd)


@contextmanager
def _profile_errors() -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise BrowserControlError(
            "Could not safely access the remembered Wisp browser profile"
        ) from exc
