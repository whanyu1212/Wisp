"""Filesystem safety and bounded metadata discovery, independent of any UI."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from wisp import project_files
from wisp.project_files import (
    FileIndexConfig,
    ProjectScanCancelled,
    ProjectScanTimedOut,
    ProjectSnapshot,
    collect_project_directory,
    collect_project_snapshot,
)
from wisp.tools.context import ToolContext
from wisp.tools.files import secure_fs
from wisp.tools.result import ToolError


def config(root: Path) -> FileIndexConfig:
    return FileIndexConfig(root=root, context=ToolContext(cwd=root))


def _display_paths(snapshot: ProjectSnapshot) -> tuple[str, ...]:
    return tuple(entry.display_path for entry in snapshot.entries)


def test_enumeration_cap_counts_denied_names_and_omits_whole_directory(tmp_path: Path) -> None:
    for i in range(15):
        (tmp_path / f".env.{i}").touch()
    (tmp_path / "visible.py").touch()
    snapshot = collect_project_snapshot(replace(config(tmp_path), max_examined_entries=10))
    assert snapshot.entries == ()
    assert snapshot.truncation.enumeration_limit_reached


def test_bounded_enumeration_does_not_depend_on_directory_order(tmp_path: Path) -> None:
    for name in ("z", "a", "n"):
        (tmp_path / name).touch()
    snapshot = collect_project_snapshot(replace(config(tmp_path), max_entries=2))
    assert _display_paths(snapshot) == ("a", "n")
    assert snapshot.truncated


def test_cancel_and_timeout_discard_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "file").touch()
    cancelled = Event()
    cancelled.set()
    with pytest.raises(ProjectScanCancelled):
        collect_project_snapshot(config(tmp_path), cancelled=cancelled)
    ticks = iter((0.0, 0.0, 0.0, 10.0))
    monkeypatch.setattr(project_files, "monotonic", lambda: next(ticks))
    with pytest.raises(ProjectScanTimedOut):
        collect_project_snapshot(config(tmp_path))


def test_unrepresentable_names_and_special_files_are_omitted(tmp_path: Path) -> None:
    (tmp_path / "资料").mkdir()
    (tmp_path / "资料" / 'say "hi".py').touch()
    for name in ("line\nbreak", "escape\x1b[31m", "bidi\u202ename", "back\\slash"):
        (tmp_path / name).touch()
    if os.name != "nt":
        os.mkfifo(tmp_path / "pipe")
        try:
            fd = os.open(os.fsencode(tmp_path) + b"/bad\xffname", os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError as exc:
            # APFS rejects non-UTF-8 names at creation; Linux allows the fixture.
            assert exc.errno == errno.EILSEQ
        else:
            os.close(fd)
        assert not project_files.is_display_safe_path("bad\udcffname")
    assert _display_paths(collect_project_snapshot(config(tmp_path))) == (
        "资料/",
        '资料/say "hi".py',
    )


def test_root_symlink_is_never_enumerated(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "private-name").touch()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert collect_project_snapshot(config(link)).entries == ()


@pytest.mark.parametrize("direct", [False, True])
def test_ancestor_swap_cannot_enumerate_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct: bool
) -> None:
    root = tmp_path / "project"
    (root / "parent" / "child").mkdir(parents=True)
    (root / "parent" / "child" / "safe").touch()
    outside = tmp_path / "outside"
    (outside / "child").mkdir(parents=True)
    (outside / "child" / "outside-secret-name").touch()
    original = secure_fs.open_directory
    swapped = False

    @contextmanager
    def racing_open(path):  # type: ignore[no-untyped-def]
        nonlocal swapped
        if path.path == root / "parent" / "child":
            (root / "parent").rename(root / "moved")
            (root / "parent").symlink_to(outside, target_is_directory=True)
            swapped = True
        with original(path) as directory:
            yield directory

    monkeypatch.setattr(secure_fs, "open_directory", racing_open)
    if direct:
        with pytest.raises(ToolError):
            collect_project_directory(config(root), "parent/child")
    else:
        snapshot = collect_project_snapshot(config(root))
        assert not any("outside" in path for path in _display_paths(snapshot))
        assert "parent/child/safe" not in _display_paths(snapshot)
    assert swapped


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race test")
def test_reopens_directory_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned = tmp_path / "visible"
    scanned.mkdir()
    (scanned / "file.py").touch()
    original = secure_fs.open_directory
    moved = tmp_path / "moved"

    @contextmanager
    def racing_open(path):  # type: ignore[no-untyped-def]
        with original(path) as directory:
            if path.path == scanned and not moved.exists():
                scanned.rename(moved)
                scanned.symlink_to(moved, target_is_directory=True)
            yield directory

    monkeypatch.setattr(secure_fs, "open_directory", racing_open)
    with pytest.raises(ToolError):
        collect_project_directory(config(tmp_path), "visible")
    assert moved.exists()


@pytest.mark.parametrize("path", ["/absolute", "../secret", "a/../secret", "a/./b", "a//b", "a/"])
def test_relative_path_contract_rejects_ambiguous_paths(path: str) -> None:
    assert not project_files.is_display_safe_path(path)


def test_directory_respects_entry_limit(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("z", "a", "n"):
        (tmp_path / "src" / name).touch()
    snapshot = collect_project_directory(replace(config(tmp_path), max_entries=2), "src")
    assert _display_paths(snapshot) == ("src/a", "src/n")
    assert snapshot.truncation.entry_limit_reached


def test_listing_respects_depth_limit(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deeper.py").touch()
    snapshot = collect_project_directory(replace(config(tmp_path), max_depth=1), "src")
    assert snapshot.entries == ()
    assert snapshot.truncation.depth_limit_reached


def test_protected_root_denies_children(tmp_path: Path) -> None:
    root = tmp_path / "private"
    (root / "src").mkdir(parents=True)
    (root / "src" / "file.py").touch()
    options = config(root)
    options = replace(options, context=replace(options.context, protected_paths=("private",)))

    with pytest.raises(ValueError, match="excluded by policy"):
        collect_project_directory(options, "src")
