"""Remember live-browser addresses inside Wisp's private browser profile."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from wisp.coding.browser_profile import browser_profile, clear_browser_profile
from wisp.coding.browser_recents import (
    RECENT_ADDRESSES_FILENAME,
    RecentAddressesBusyError,
    load_recent_addresses,
    record_recent_address,
)
from wisp.sessions.errors import SessionError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="remembered profiles require POSIX")


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    """An existing Wisp profile, created the way the live browser creates it."""
    directory = tmp_path / ".wisp" / "browser-profile"
    with browser_profile(directory):
        pass
    return directory


def _write_raw(profile: Path, content: str) -> None:
    (profile / RECENT_ADDRESSES_FILENAME).write_text(content, encoding="utf-8")


@pytest.fixture
def held_lock(profile: Path) -> Iterator[int]:
    """Another open file description holding the list's lock, as another process would."""
    fd = os.open(profile / f"{RECENT_ADDRESSES_FILENAME}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield fd
    finally:
        os.close(fd)


def test_missing_profile_or_file(tmp_path: Path, profile: Path) -> None:
    assert load_recent_addresses(tmp_path / "absent") == ()
    assert load_recent_addresses(profile) == ()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("not json", ()),
        ("\xff", ()),
        ('{"url": "https://a.test/"}', ()),
        ('"https://a.test/"', ()),
        ("[" * 100_000 + "]" * 100_000, ()),
        # A lone surrogate cannot be encoded for the report event.
        ('["https://a.test/\\ud800", "https://b.test/"]', ("https://b.test/",)),
        (
            json.dumps(
                [
                    "https://a.test/",
                    7,
                    None,
                    ["https://nested.test/"],
                    "ftp://files.test/",
                    "https://user:secret@b.test/",
                    "https://" + "c" * 2041,
                    "https://a.test/",
                    "http://localhost:3000",
                ]
            ),
            ("https://a.test/", "http://localhost:3000"),
        ),
    ],
    ids=["invalid", "binary", "object", "string", "deep", "surrogate", "entries"],
)
def test_bad_content_is_dropped(profile: Path, content: str, expected: tuple[str, ...]) -> None:
    _write_raw(profile, content)
    assert load_recent_addresses(profile) == expected


@pytest.mark.parametrize(
    ("size", "expected"), [(256 * 1024, ("https://a.test/",)), (256 * 1024 + 1, ())]
)
def test_file_size_limit(profile: Path, size: int, expected: tuple[str, ...]) -> None:
    content = json.dumps(["https://a.test/"])
    _write_raw(profile, content + " " * (size - len(content)))
    assert load_recent_addresses(profile) == expected


def test_longest_valid_entry_is_kept(profile: Path) -> None:
    longest = "https://" + "c" * 2040
    _write_raw(profile, json.dumps([longest]))
    assert load_recent_addresses(profile) == (longest,)


def test_newest_first_and_deduplicated(profile: Path) -> None:
    for url in ("https://a.test/", "https://b.test/", "https://c.test/", "https://a.test/"):
        record_recent_address(profile, url)
    assert load_recent_addresses(profile) == (
        "https://a.test/",
        "https://c.test/",
        "https://b.test/",
    )
    record_recent_address(profile, "https://a.test/")
    assert load_recent_addresses(profile) == (
        "https://a.test/",
        "https://c.test/",
        "https://b.test/",
    )


def test_capped_at_twenty(profile: Path) -> None:
    urls = [f"https://site{index}.test/" for index in range(25)]
    for url in urls:
        record_recent_address(profile, url)
    expected = tuple(reversed(urls))[:20]
    assert load_recent_addresses(profile) == expected
    stored = json.loads((profile / RECENT_ADDRESSES_FILENAME).read_text(encoding="utf-8"))
    assert stored == list(expected)


def test_loading_stops_at_twenty(profile: Path) -> None:
    urls = [f"https://site{index}.test/" for index in range(25)]
    _write_raw(profile, json.dumps(urls))
    assert load_recent_addresses(profile) == tuple(urls[:20])


def test_non_ascii_list_round_trips(profile: Path) -> None:
    """Twenty maximum-length URLs of non-BMP characters stay within the read limit."""
    urls = [f"https://site{index:02}.test/" for index in range(20)]
    urls = [url + "\U0001f600" * (2048 - len(url)) for url in urls]
    for url in urls:
        record_recent_address(profile, url)
    assert load_recent_addresses(profile) == tuple(reversed(urls))


@pytest.mark.parametrize("marker", ["missing", "directory", "symlink"])
def test_unmarked_directory_reads_nothing(tmp_path: Path, marker: str) -> None:
    directory = tmp_path / "not-a-profile"
    directory.mkdir()
    _write_raw(directory, json.dumps(["https://a.test/"]))
    if marker == "directory":
        (directory / ".wisp-browser-profile").mkdir()
    elif marker == "symlink":
        target = tmp_path / "elsewhere"
        target.touch()
        (directory / ".wisp-browser-profile").symlink_to(target)

    assert load_recent_addresses(directory) == ()


@pytest.mark.timeout(10)
@pytest.mark.parametrize("kind", ["symlink", "fifo", "hard-link"])
def test_unsafe_list_reads_nothing(tmp_path: Path, profile: Path, kind: str) -> None:
    path = profile / RECENT_ADDRESSES_FILENAME
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(["https://a.test/"]), encoding="utf-8")
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hard-link":
        path.hardlink_to(outside)
    elif hasattr(os, "mkfifo"):
        # Opening a FIFO for reading blocks until a writer appears.
        os.mkfifo(path)
    else:
        pytest.skip("FIFOs require os.mkfifo")

    assert load_recent_addresses(profile) == ()


def test_corrupt_file_is_replaced(profile: Path) -> None:
    _write_raw(profile, "{")
    record_recent_address(profile, "https://a.test/")
    assert load_recent_addresses(profile) == ("https://a.test/",)


def test_unrelated_temp_file_untouched(profile: Path) -> None:
    """A file at a predictable temporary name, perhaps a hard link, is never reused."""
    path = profile / RECENT_ADDRESSES_FILENAME
    path.write_text("[]", encoding="utf-8")
    path.chmod(0o644)
    unrelated = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    unrelated.write_text("unrelated", encoding="utf-8")
    unrelated.chmod(0o644)

    record_recent_address(profile, "https://a.test/")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_recent_addresses(profile) == ("https://a.test/",)
    assert unrelated.read_text(encoding="utf-8") == "unrelated"
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644
    assert [entry.name for entry in profile.iterdir() if entry.name.endswith(".tmp")] == [
        unrelated.name
    ]


@pytest.mark.timeout(10)
def test_contended_lock_gives_up(profile: Path, held_lock: int) -> None:
    started = time.monotonic()
    with pytest.raises(RecentAddressesBusyError):
        record_recent_address(profile, "https://a.test/")
    assert 0.5 <= time.monotonic() - started < 3
    assert not (profile / RECENT_ADDRESSES_FILENAME).exists()


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    ("kind", "error"), [("symlink", OSError), ("fifo", SessionError), ("hard-link", SessionError)]
)
def test_unsafe_lock_is_refused(
    tmp_path: Path, profile: Path, kind: str, error: type[Exception]
) -> None:
    lock = profile / f"{RECENT_ADDRESSES_FILENAME}.lock"
    outside = tmp_path / "outside"
    outside.write_text("unrelated", encoding="utf-8")
    outside.chmod(0o644)
    if kind == "symlink":
        lock.symlink_to(outside)
    elif kind == "hard-link":
        lock.hardlink_to(outside)
    else:
        os.mkfifo(lock)

    with pytest.raises(error):
        record_recent_address(profile, "https://a.test/")

    assert outside.read_text(encoding="utf-8") == "unrelated"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert not (profile / RECENT_ADDRESSES_FILENAME).exists()


@pytest.mark.timeout(10)
def test_lock_released_while_waiting(profile: Path, held_lock: int) -> None:
    release = threading.Timer(0.2, fcntl.flock, (held_lock, fcntl.LOCK_UN))
    release.start()
    try:
        record_recent_address(profile, "https://a.test/")
    finally:
        release.join()
    assert load_recent_addresses(profile) == ("https://a.test/",)


@pytest.mark.parametrize("marker", ["missing", "directory", "symlink"])
def test_no_write_without_profile_marker(tmp_path: Path, marker: str) -> None:
    directory = tmp_path / "not-a-profile"
    directory.mkdir()
    if marker == "directory":
        (directory / ".wisp-browser-profile").mkdir()
    elif marker == "symlink":
        target = tmp_path / "elsewhere"
        target.touch()
        (directory / ".wisp-browser-profile").symlink_to(target)

    record_recent_address(directory, "https://a.test/")
    record_recent_address(tmp_path / "absent", "https://a.test/")

    assert {entry.name for entry in directory.iterdir()} <= {".wisp-browser-profile"}
    assert not (tmp_path / "absent").exists()


def test_write_failure_raises(profile: Path) -> None:
    (profile / RECENT_ADDRESSES_FILENAME).mkdir()
    with pytest.raises(OSError):
        record_recent_address(profile, "https://a.test/")
    assert [entry.name for entry in profile.iterdir() if entry.name.endswith(".tmp")] == []


def test_clear_removes_addresses_and_profile_still_opens(profile: Path) -> None:
    record_recent_address(profile, "https://a.test/")
    assert (profile / f"{RECENT_ADDRESSES_FILENAME}.lock").exists()

    clear_browser_profile(profile)

    assert {entry.name for entry in profile.iterdir()} == {".wisp-browser-profile"}
    assert load_recent_addresses(profile) == ()
    record_recent_address(profile, "https://b.test/")
    with browser_profile(profile) as opened:
        assert opened.path == profile
    assert load_recent_addresses(profile) == ("https://b.test/",)


def test_recording_while_profile_is_in_use(profile: Path) -> None:
    """Chromium holds the profile lock while it runs; recording must not need it."""
    with browser_profile(profile):
        record_recent_address(profile, "https://a.test/")
    assert load_recent_addresses(profile) == ("https://a.test/",)
