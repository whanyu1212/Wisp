from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools import file_ops as file_ops_module
from wisp.tools import search as search_module
from wisp.tools import secure_fs as secure_fs_module
from wisp.tools.context import ToolContext
from wisp.tools.file_ops import EditTool, ReadTool, WriteTool
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.search import FindTool, GrepTool, LsTool


def run_tool(tool: object, arguments: dict[str, object], context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        return await tool.run(arguments, context)  # type: ignore[attr-defined]

    return anyio.run(run)


def test_read_rejects_final_symlink_even_when_outside_access_is_allowed(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ToolError, match="Symbolic links are not allowed"):
        run_tool(
            ReadTool(), {"path": "link.txt"}, ToolContext(cwd=tmp_path, allow_outside_cwd=True)
        )


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="requires no-follow descriptors")
def test_read_rejects_ancestor_replaced_with_symlink(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    safe = workspace / "safe"
    safe.mkdir(parents=True)
    (safe / "data.txt").write_text("ordinary\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("secret\n", encoding="utf-8")
    real_open_child = secure_fs_module._open_child_directory
    swapped = False

    def swap_then_open(parent_fd: int, name: str, *, display: str) -> int:
        nonlocal swapped
        if name == "safe" and not swapped:
            swapped = True
            safe.rename(workspace / "safe-original")
            safe.symlink_to(outside, target_is_directory=True)
        return real_open_child(parent_fd, name, display=display)

    monkeypatch.setattr(secure_fs_module, "_open_child_directory", swap_then_open)

    with pytest.raises(ToolError, match="symbolic link|non-directory"):
        run_tool(ReadTool(), {"path": "safe/data.txt"}, ToolContext(cwd=workspace))


def test_overwrite_publish_failure_preserves_original(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file_ops_module.os, "replace", fail_replace)

    with pytest.raises(ToolError, match="Could not create file"):
        run_tool(
            WriteTool(),
            {"path": "target.txt", "content": "replacement\n"},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "original\n"
    assert not list(tmp_path.glob(".wisp-write-*"))


def test_overwrite_preserves_existing_permission_bits(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    target.chmod(0o640)

    run_tool(
        WriteTool(),
        {"path": "target.txt", "content": "replacement\n"},
        ToolContext(cwd=tmp_path),
    )

    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode-bit semantics")
def test_overwrite_does_not_require_read_permission(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    target.chmod(0o200)

    result = run_tool(
        WriteTool(),
        {"path": "target.txt", "content": "replacement\n"},
        ToolContext(cwd=tmp_path),
    )

    assert "before_text" not in result.data
    assert target.stat().st_mode & 0o777 == 0o200
    target.chmod(0o600)
    assert target.read_text(encoding="utf-8") == "replacement\n"


def test_overwrite_rejects_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ToolError, match="Symbolic links are not allowed"):
        run_tool(
            WriteTool(),
            {"path": "link.txt", "content": "replacement\n"},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "original\n"


def test_guarded_windows_atomic_write_and_edit_algorithm(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    secure_path = secure_fs_module.secure_tool_path("target.txt", ToolContext(cwd=tmp_path))

    @contextmanager
    def held_parent(_path: object, *, create: bool = False) -> Iterator[Path]:
        del create
        yield tmp_path

    monkeypatch.setattr(file_ops_module, "open_windows_parent", held_parent)

    outcome = file_ops_module._atomic_write_windows(secure_path, "replacement\n", overwrite=True)
    file_ops_module._atomic_edit_windows(secure_path, [("replacement", "edited")])

    assert outcome.created is False
    assert outcome.before_text == "original\n"
    assert target.read_text(encoding="utf-8") == "edited\n"
    assert not list(tmp_path.glob(".wisp-write-*"))


def test_write_allows_missing_conflict_parent(tmp_path: Path) -> None:
    context = ToolContext(
        cwd=tmp_path,
        conflicting_write_paths=(Path("nested/AGENTS.MD"),),
    )

    run_tool(
        WriteTool(),
        {"path": "nested/AGENTS.md", "content": "guidance\n", "overwrite": False},
        context,
    )

    assert (tmp_path / "nested/AGENTS.md").read_text(encoding="utf-8") == "guidance\n"


def test_edit_rejects_destination_replaced_before_publish(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original\n", encoding="utf-8")
    real_stat_leaf = file_ops_module.stat_leaf
    calls = 0

    def replace_before_second_stat(parent: object) -> os.stat_result | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.unlink()
            target.write_text("concurrent\n", encoding="utf-8")
        return real_stat_leaf(parent)  # type: ignore[arg-type]

    monkeypatch.setattr(file_ops_module, "stat_leaf", replace_before_second_stat)

    with pytest.raises(ToolError, match="File changed while editing"):
        run_tool(
            EditTool(),
            {"path": "target.txt", "edits": [{"oldText": "original", "newText": "edited"}]},
            ToolContext(cwd=tmp_path),
        )

    assert target.read_text(encoding="utf-8") == "concurrent\n"
    assert not list(tmp_path.glob(".wisp-write-*"))


def test_recursive_tools_skip_directory_symlinks_even_when_opted_out(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)

    assert result.text == "No matches"


def test_recursive_tools_honor_repository_ignore_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / ".ignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / ".rgignore").write_text("rg-only.py\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "match.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "rg-only.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert grep.data["matches"] == ["visible.py:1:needle"]
    assert find.data["files"] == ["visible.py"]


def test_subdirectory_search_inherits_ancestor_ignore_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("subdir/ignored.txt\n", encoding="utf-8")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (subdir / "visible.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": "subdir", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": "subdir", "pattern": "*.txt"}, context)

    assert grep.data["matches"] == ["subdir/visible.txt:1:needle"]
    assert find.data["files"] == ["subdir/visible.txt"]


def test_recursive_tools_honor_repository_local_excludes(tmp_path: Path) -> None:
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("local-only.txt\n", encoding="utf-8")
    (tmp_path / "local-only.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.txt"}, context)

    assert grep.data["matches"] == ["visible.txt:1:needle"]
    assert find.data["files"] == ["visible.txt"]


def test_recursive_tools_preserve_negated_files_below_double_star_ignore(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("foo/**\n!foo/bar.txt\n", encoding="utf-8")
    foo = tmp_path / "foo"
    foo.mkdir()
    (foo / "bar.txt").write_text("needle\n", encoding="utf-8")
    (foo / "ignored.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.txt"}, context)

    assert grep.data["matches"] == ["foo/bar.txt:1:needle"]
    assert find.data["files"] == ["foo/bar.txt"]


def test_recursive_tools_do_not_reinclude_below_ignored_parent(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("foo/\n!foo/bar.txt\n", encoding="utf-8")
    foo = tmp_path / "foo"
    foo.mkdir()
    (foo / "bar.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.txt"}, context)

    assert grep.text == "No matches"
    assert find.text == "No files found"


def test_recursive_tools_search_unignored_common_directory_names(tmp_path: Path) -> None:
    for name in ("build", "node_modules", "target", "vendor"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "result.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.txt"}, context)

    expected_files = [
        "build/result.txt",
        "node_modules/result.txt",
        "target/result.txt",
        "vendor/result.txt",
    ]
    assert grep.data["matches"] == [f"{path}:1:needle" for path in expected_files]
    assert find.data["files"] == expected_files


def test_grep_explicit_glob_overrides_repository_ignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "result.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    default = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    explicit = run_tool(
        GrepTool(),
        {"path": ".", "pattern": "needle", "glob": "generated/result.txt"},
        context,
    )

    assert default.text == "No matches"
    assert explicit.data["matches"] == ["generated/result.txt:1:needle"]


def test_grep_explicit_glob_reincludes_hidden_files(tmp_path: Path) -> None:
    hidden_directory = tmp_path / ".hidden"
    hidden_directory.mkdir()
    (hidden_directory / "result.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    default = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    explicit = run_tool(
        GrepTool(),
        {"path": ".", "pattern": "needle", "glob": "*.txt"},
        context,
    )

    assert default.text == "No matches"
    assert explicit.data["matches"] == [".hidden.txt:1:needle"]


def test_grep_negated_glob_excludes_matches_without_overriding_ignores(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "excluded.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"path": ".", "pattern": "needle", "glob": "!*.txt"},
        context,
    )

    assert result.data["matches"] == ["visible.py:1:needle"]


def test_recursive_tools_preserve_valid_non_utf8_ignore_rules(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_bytes(b"ignored.txt\n\xff\n")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    grep = run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)
    find = run_tool(FindTool(), {"path": ".", "pattern": "*.txt"}, context)

    assert grep.data["matches"] == ["visible.txt:1:needle"]
    assert find.data["files"] == ["visible.txt"]


def test_recursive_tools_reject_oversized_ignore_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_MAX_IGNORE_FILE_BYTES", 32)
    (tmp_path / ".gitignore").write_text("x" * 33, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match=r"\.gitignore exceeds 32 bytes"):
        run_tool(FindTool(), {"path": "."}, context)


def test_recursive_tools_reject_too_many_repository_exclude_patterns(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_MAX_IGNORE_FILE_PATTERNS", 2)
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("one\ntwo\nthree\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match=r"exclude exceeds 2 patterns"):
        run_tool(GrepTool(), {"path": ".", "pattern": "needle"}, context)


def test_subdirectory_search_propagates_ancestor_ignore_file_limit(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_MAX_IGNORE_FILE_BYTES", 8)
    (tmp_path / ".gitignore").write_text("x" * 9, encoding="utf-8")
    (tmp_path / "nested").mkdir()

    with pytest.raises(ToolError, match=r"\.gitignore exceeds 8 bytes"):
        run_tool(
            FindTool(),
            {"path": "nested"},
            ToolContext(cwd=tmp_path),
        )


def test_path_fallback_propagates_nested_ignore_file_limit(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_MAX_IGNORE_FILE_BYTES", 8)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text("x" * 9, encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match=r"nested/\.gitignore exceeds 8 bytes"):
        tuple(
            search_module._walk_directory(
                tmp_path,
                tmp_path,
                context,
                ignore_specs=(),
                ignore_override_glob=None,
            )
        )


def test_recursive_tools_reject_directory_over_entry_limit(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "_MAX_DIRECTORY_ENTRIES", 2)
    for name in ("one.txt", "two.txt", "three.txt"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")

    with pytest.raises(ToolError, match=r"exceeds 2 entries"):
        run_tool(FindTool(), {"path": "."}, ToolContext(cwd=tmp_path))


def test_regex_search_times_out_pathological_backtracking(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text(("a" * 200_000) + "!\n", encoding="utf-8")

    with pytest.raises(ToolError, match="regex evaluation time limit"):
        run_tool(
            GrepTool(),
            {"path": ".", "pattern": "(a+)+$"},
            ToolContext(cwd=tmp_path),
        )


def test_grep_rejects_unbounded_newline_free_file(tmp_path: Path) -> None:
    (tmp_path / "minified.txt").write_text("x" * 1_000_001, encoding="utf-8")

    with pytest.raises(ToolError, match="line longer than 1000000 characters"):
        run_tool(
            GrepTool(),
            {"path": ".", "pattern": "needle", "literal": True},
            ToolContext(cwd=tmp_path),
        )


def test_grep_stops_reading_after_output_is_truncated(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text(
        "needle\n" + ("x" * 1_000_001),
        encoding="utf-8",
    )

    result = run_tool(
        GrepTool(),
        {"path": ".", "pattern": "needle", "literal": True},
        ToolContext(cwd=tmp_path, max_output_bytes=1),
    )

    assert result.truncated is True


def test_grep_skips_binary_file_after_pending_match(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"needle\n" + (b"x" * 70_000) + b"\0")
    (tmp_path / "text.txt").write_text("needle\n", encoding="utf-8")

    result = run_tool(
        GrepTool(),
        {"path": ".", "pattern": "needle", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.data["matches"] == ["text.txt:1:needle"]


def test_ls_displays_but_does_not_follow_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "linked"
    link.symlink_to(outside, target_is_directory=True)

    listing = run_tool(LsTool(), {"path": "."}, ToolContext(cwd=workspace))
    assert listing.text == "linked"

    with pytest.raises(ToolError, match="symbolic link|non-directory"):
        run_tool(LsTool(), {"path": "linked"}, ToolContext(cwd=workspace))
