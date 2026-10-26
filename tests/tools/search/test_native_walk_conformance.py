"""Conformance tests for the optional native directory walk.

Each test walks the same tree with the Python walk and the native one and expects the
same ordered files, descriptor identities and errors. Seeded random trees cover nested
ignore files with negations, repository markers, hidden and protected names, links and
undecodable names (on file systems that allow them).
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from typing import Literal

import pytest
from pytest import MonkeyPatch

from wisp.tools.context import ToolContext
from wisp.tools.files.secure_fs import secure_tool_path
from wisp.tools.result import ToolError
from wisp.tools.search import tools as search_tools_module

native = pytest.importorskip("wisp._native")
if not hasattr(native, "NativeWalk"):
    pytest.skip("installed native extension does not include the walk", allow_module_level=True)

_NATIVE_WALK = search_tools_module._load_native_walk()
type _Backend = Literal["python", "native"]
type _Walked = list[tuple[str, tuple[int, int] | None]] | str

_NAMES = (
    "a",
    "b",
    "c.py",
    "d.txt",
    "keep.log",
    "z.log",
    "build",
    "node",
    ".hidden",
    "x y",
    "é",
    "[x]",
    "-n",
    ".env",
    "secret.key",
)
_RAW_NAMES = (b"\x80", b"a\xffb")
_PATTERNS = (
    "*.log",
    "!keep.log",
    "build/",
    "/a",
    "b/**",
    "!b/c.py",
    "!b/a/",
    "*.py",
    "!*.txt",
    "[a-c]*",
    "**/x y",
    "node/",
    "é*",
    "\\#x",
    "foo\\",
    "[z-a]",
    "!/build/keep.log",
    "*",
    "!*/",
    "?",
    ".hidden",
)


def _walk(
    root: Path,
    path: str,
    monkeypatch: MonkeyPatch,
    backend: _Backend,
    *,
    include_ignored: bool = False,
    cancel_event: Event | None = None,
) -> _Walked:
    """Return the walked files relative to ``root`` with their identities, or the error."""

    monkeypatch.setattr(
        search_tools_module, "_NATIVE_WALK", _NATIVE_WALK if backend == "native" else None
    )
    context = ToolContext(cwd=root)
    override = (lambda candidate: candidate.suffix == ".log") if include_ignored else None
    try:
        walked = search_tools_module._iter_walked_files(
            secure_tool_path(path, context, follow_links=True),
            context,
            ignore_override_matcher=override,
            cancel_event=cancel_event,
        )
        return [
            (file.path.relative_to(root.resolve()).as_posix(), file.expected_identity)
            for file in walked
        ]
    except ToolError as exc:
        return f"{type(exc).__name__}: {exc}"


def _assert_backends_agree(
    root: Path, path: str, monkeypatch: MonkeyPatch, *, include_ignored: bool = False
) -> _Walked:
    python = _walk(root, path, monkeypatch, "python", include_ignored=include_ignored)
    walked = _walk(root, path, monkeypatch, "native", include_ignored=include_ignored)
    assert walked == python
    return walked


def _make_name(directory: Path, generator: random.Random) -> Path | None:
    if generator.random() < 0.1:
        raw = os.path.join(os.fsencode(directory), generator.choice(_RAW_NAMES))
        try:
            os.close(os.open(raw, os.O_CREAT | os.O_WRONLY, 0o600))
        except OSError:
            # APFS and some other file systems reject names that are not UTF-8.
            return None
        return Path(os.fsdecode(raw))
    return directory / generator.choice(_NAMES)


def _build_tree(directory: Path, generator: random.Random, depth: int) -> None:
    for _ in range(generator.randint(0, 6)):
        path = _make_name(directory, generator)
        if path is None or path.exists() or path.is_symlink():
            continue
        roll = generator.random()
        if roll < 0.3 and depth < 3:
            path.mkdir()
            _build_tree(path, generator, depth + 1)
        elif roll < 0.4:
            target = generator.choice(("../d.txt", "a", "missing", "/"))
            path.symlink_to(target)
        else:
            path.write_text("needle\n", encoding="utf-8")
    for name in (".gitignore", ".ignore", ".rgignore"):
        if generator.random() < 0.3:
            lines = generator.sample(_PATTERNS, generator.randint(1, 4))
            (directory / name).write_text("".join(f"{line}\n" for line in lines), "utf-8")
    roll = generator.random()
    if (directory / ".git").exists():
        return
    if roll < 0.1:
        (directory / ".git" / "info").mkdir(parents=True)
        exclude = generator.sample(_PATTERNS, 2)
        (directory / ".git" / "info" / "exclude").write_text("\n".join(exclude), "utf-8")
    elif roll < 0.15:
        (directory / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="the native walk is POSIX only")
@pytest.mark.parametrize("seed", range(40))
def test_random_trees_match(tmp_path: Path, monkeypatch: MonkeyPatch, seed: int) -> None:
    generator = random.Random(seed)
    root = tmp_path / "root"
    root.mkdir()
    if generator.random() < 0.5:
        (root / ".git").mkdir()
    _build_tree(root, generator, 0)
    subdirectories = sorted(path for path in root.rglob("*") if path.is_dir())

    walked = _assert_backends_agree(root, ".", monkeypatch)
    _assert_backends_agree(root, ".", monkeypatch, include_ignored=True)
    for subdirectory in subdirectories[:3]:
        if ".git" not in subdirectory.parts and not subdirectory.is_symlink():
            _assert_backends_agree(root, str(subdirectory.relative_to(root)), monkeypatch)

    assert isinstance(walked, list)
    for relative, identity in walked:
        info = os.lstat(root / relative)
        assert identity == (info.st_dev, info.st_ino)


def test_walk_order_interleaves_directories(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    for name in ("a.txt", "b/inner.txt", "b/deeper/x.txt", "c.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")

    walked = _assert_backends_agree(tmp_path, ".", monkeypatch)

    assert [relative for relative, _ in walked] == [
        "a.txt",
        "b/deeper/x.txt",
        "b/inner.txt",
        "c.txt",
    ]


def test_ancestor_ignore_files_apply(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("sub/deep/*.log\n!sub/deep/keep.log\n", "utf-8")
    (tmp_path / "sub" / ".ignore").parent.mkdir()
    (tmp_path / "sub" / ".ignore").write_text("skip.txt\n", "utf-8")
    deep = tmp_path / "sub" / "deep"
    deep.mkdir()
    for name in ("a.log", "keep.log", "skip.txt", "x.txt"):
        (deep / name).write_text("", encoding="utf-8")

    walked = _assert_backends_agree(tmp_path, "sub/deep", monkeypatch)

    assert [relative for relative, _ in walked] == ["sub/deep/keep.log", "sub/deep/x.txt"]


def test_subfolder_cwd_inherits_repository(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("build/\npkg/*.log\n!pkg/keep.log\n", "utf-8")
    package = tmp_path / "pkg"
    (package / "build").mkdir(parents=True)
    for name in ("build/out.txt", "drop.log", "keep.log", "x.txt"):
        (package / name).write_text("", encoding="utf-8")

    walked = _assert_backends_agree(package, ".", monkeypatch)

    assert [relative for relative, _ in walked] == ["keep.log", "x.txt"]


def test_ignored_directory_pruning_matches(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # A negation re-includes files below an ignored directory only after "dir/**".
    (tmp_path / ".ignore").write_text("build/\n!build/keep.log\nout/**\n!out/keep.log\n", "utf-8")
    for name in ("build/keep.log", "out/keep.log", "out/drop.log"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")

    walked = _assert_backends_agree(tmp_path, ".", monkeypatch)

    assert [relative for relative, _ in walked] == ["out/keep.log"]


def test_nested_gitignore_inherits_repository(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (nested / ".gitignore").write_text("drop.txt\n", encoding="utf-8")
    for name in ("drop.txt", "keep.txt"):
        (nested / name).write_text("", encoding="utf-8")

    walked = _assert_backends_agree(tmp_path, ".", monkeypatch)

    assert [relative for relative, _ in walked] == ["a/b/keep.txt"]


@pytest.mark.parametrize(
    ("limit", "value", "setup"),
    [
        ("_MAX_DIRECTORY_ENTRIES", 2, "entries"),
        ("_MAX_IGNORE_FILE_BYTES", 4, "bytes"),
        ("_MAX_IGNORE_FILE_PATTERNS", 1, "patterns"),
    ],
)
def test_limit_errors_match(
    tmp_path: Path, monkeypatch: MonkeyPatch, limit: str, value: int, setup: str
) -> None:
    monkeypatch.setattr(search_tools_module, limit, value)
    nested = tmp_path / "nested"
    nested.mkdir()
    for name in ("a", "b"):
        (tmp_path / name).write_text("", encoding="utf-8")
    if setup == "entries":
        (nested / "c").write_text("", encoding="utf-8")
        (nested / "d").write_text("", encoding="utf-8")
        (nested / "e").write_text("", encoding="utf-8")
    else:
        (nested / ".ignore").write_text("one\ntwo\n", encoding="utf-8")

    walked = _assert_backends_agree(tmp_path, ".", monkeypatch)

    assert isinstance(walked, str)
    assert "exceeds" in walked


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_stat_errors_match(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    unsearchable = tmp_path / "sub"
    unsearchable.mkdir()
    (unsearchable / "name.txt").write_text("", encoding="utf-8")
    # Readable but not searchable: the directory lists, but its entries cannot be stat'd.
    unsearchable.chmod(0o600)
    errors = []
    try:
        for backend in ("python", "native"):
            with pytest.raises(OSError) as caught:
                _walk(tmp_path, ".", monkeypatch, backend)
            errors.append(str(caught.value))
    finally:
        unsearchable.chmod(0o700)

    assert errors[0] == errors[1] == "[Errno 13] Permission denied: 'name.txt'"


@pytest.mark.parametrize("backend", ["python", "native"])
def test_swapped_directory_is_skipped(
    tmp_path: Path, monkeypatch: MonkeyPatch, backend: _Backend
) -> None:
    root = tmp_path / "root"
    (root / "b").mkdir(parents=True)
    (root / "a.txt").write_text("", encoding="utf-8")
    (root / "b" / "inner.txt").write_text("", encoding="utf-8")
    (root / "c.txt").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        search_tools_module, "_NATIVE_WALK", _NATIVE_WALK if backend == "native" else None
    )
    context = ToolContext(cwd=root)

    seen: list[str] = []
    walked: Iterator[search_tools_module._WalkedFile] = iter(
        search_tools_module._iter_walked_files(secure_tool_path(".", context), context)
    )
    for file in walked:
        seen.append(file.path.name)
        if file.path.name == "a.txt":
            # The walk listed the root already; b becomes a link before it is opened.
            (root / "b").rename(tmp_path / "moved")
            (root / "b").symlink_to(outside, target_is_directory=True)

    assert seen == ["a.txt", "c.txt"]


@pytest.mark.parametrize("backend", ["python", "native"])
def test_walk_stops_when_cancelled(
    tmp_path: Path, monkeypatch: MonkeyPatch, backend: _Backend
) -> None:
    for name in ("a/1.txt", "b/2.txt", "c/3.txt"):
        (tmp_path / name).parent.mkdir()
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(
        search_tools_module, "_NATIVE_WALK", _NATIVE_WALK if backend == "native" else None
    )
    context = ToolContext(cwd=tmp_path)
    cancel_event = Event()

    seen = []
    for file in search_tools_module._iter_walked_files(
        secure_tool_path(".", context), context, cancel_event=cancel_event
    ):
        seen.append(file.path.name)
        cancel_event.set()

    assert seen == ["1.txt"]


def test_walk_falls_back_when_handoff_fails(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / "a.txt").write_text("", encoding="utf-8")

    class RefusingWalk:
        NativeWalkError = native.NativeWalkError

        def NativeWalk(self, descriptor: int, **_: object) -> object:
            raise OSError("descriptor unavailable")

    monkeypatch.setattr(search_tools_module, "_NATIVE_WALK", RefusingWalk())
    context = ToolContext(cwd=tmp_path)

    walked = list(search_tools_module._iter_walked_files(secure_tool_path(".", context), context))

    assert [file.path.name for file in walked] == ["a.txt"]
