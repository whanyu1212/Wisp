"""Verify persistent browser ownership, contention, and clearing through its lifecycle."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from wisp.coding.browser_control import BrowserControlError
from wisp.coding.browser_profile import browser_profile, clear_browser_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="remembered profiles require POSIX")


def test_retains_only_saved_data(tmp_path: Path) -> None:
    profile = tmp_path / ".wisp" / "browser-profile"
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep").touch()
    clear_browser_profile(profile)
    assert not profile.parent.exists()

    with browser_profile(profile) as first:
        assert stat.S_IMODE(first.path.stat().st_mode) == 0o700
        data = first.path / "Default" / "nested"
        data.mkdir(parents=True)
        (data / "site-data").touch()
        (data / "linked").symlink_to(outside, target_is_directory=True)
        (first.path / "linked").symlink_to(outside, target_is_directory=True)
        (first.path / "DevToolsActivePort").write_text("1234\n/devtools/browser/old\n")
    with browser_profile(profile) as second:
        assert second.path == first.path and (data / "site-data").exists()
        assert not (second.path / "DevToolsActivePort").exists()
    clear_browser_profile(profile)
    clear_browser_profile(profile)
    assert {p.name for p in profile.iterdir()} == {".wisp-browser-profile"}
    assert not data.exists() and (outside / "keep").exists()


def test_active_session_excludes_others(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile"
    with browser_profile(profile):
        endpoint = profile / "DevToolsActivePort"
        endpoint.write_text("active")
        with pytest.raises(BrowserControlError, match="in use"), browser_profile(profile):
            pytest.fail("another session acquired an active profile")
        with pytest.raises(BrowserControlError, match="in use"):
            clear_browser_profile(profile)
        assert endpoint.read_text() == "active"
    clear_browser_profile(profile)
    assert not endpoint.exists()


@pytest.mark.parametrize("linked", ["parent", "profile", "marker", "unmarked", "permissive"])
def test_refuses_unsafe_directories(tmp_path: Path, linked: str) -> None:
    outside = tmp_path / "personal"
    outside.mkdir(mode=0o700)
    keep = outside / "keep"
    keep.touch()
    parent = tmp_path / ".wisp"
    profile = parent / "browser-profile"
    if linked == "parent":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o700)
        if linked == "profile":
            profile.symlink_to(outside, target_is_directory=True)
        else:
            profile.mkdir(mode=0o700)
            if linked == "marker":
                (profile / ".wisp-browser-profile").symlink_to(keep)
            if linked == "permissive":
                (profile / ".wisp-browser-profile").touch(mode=0o600)
                profile.chmod(0o755)
            (profile / "keep").touch()
    with pytest.raises(BrowserControlError), browser_profile(profile):
        pytest.fail("unsafe directory was opened")
    with pytest.raises(BrowserControlError):
        clear_browser_profile(profile)
    assert keep.exists()
    if linked in ("marker", "unmarked", "permissive"):
        assert (profile / "keep").exists()
    if linked == "parent":
        assert not (outside / "browser-profile").exists()


def test_orphan_chrome_blocks_clear(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile"
    with browser_profile(profile):
        (profile / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    with pytest.raises(BrowserControlError, match="in use"):
        clear_browser_profile(profile)
    with pytest.raises(BrowserControlError, match="in use"), browser_profile(profile):
        pytest.fail("claimed a profile still in use by Chromium")
    (profile / "SingletonLock").unlink()
    clear_browser_profile(profile)
    with browser_profile(profile) as reopened:
        assert reopened.path == profile


@pytest.mark.process
def test_child_holds_startup_lock(tmp_path: Path) -> None:
    profile = tmp_path / "browser-profile"
    with browser_profile(profile) as launch:
        # The child deliberately has no Chromium SingletonLock yet.
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE,
            pass_fds=launch.pass_fds,
        )
    try:
        with pytest.raises(BrowserControlError, match="in use"):
            clear_browser_profile(profile)
    finally:
        child.communicate(b"x", timeout=5)
    assert child.returncode == 0
    clear_browser_profile(profile)


@pytest.mark.parametrize("kind", ["directory", "fifo", "nonempty", "hardlink", "public"])
def test_refuses_invalid_marker(tmp_path: Path, kind: str) -> None:
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    data = profile / "unrelated"
    data.touch(mode=0o600)
    marker = profile / ".wisp-browser-profile"
    if kind == "directory":
        marker.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(marker, mode=0o600)
    elif kind == "hardlink":
        os.link(data, marker)
    else:
        marker.touch(mode=0o600)
        if kind == "nonempty":
            marker.write_bytes(b"unrelated")
        else:
            marker.chmod(0o644)
    with pytest.raises(BrowserControlError, match="Wisp-owned"):
        clear_browser_profile(profile)
    assert data.exists() and marker.exists()


def test_refuses_writable_parent(tmp_path: Path) -> None:
    parent = tmp_path / ".wisp"
    parent.mkdir()
    parent.chmod(0o777)
    with (
        pytest.raises(BrowserControlError, match="writable only by you"),
        browser_profile(parent / "profile"),
    ):
        pytest.fail("created profile under unsafe parent")
    assert not (parent / "profile").exists()
