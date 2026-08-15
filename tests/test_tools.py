from __future__ import annotations

import asyncio
import errno
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools import file_ops as file_ops_module
from wisp.tools import process as process_tools_module
from wisp.tools import process_manager as process_manager_module
from wisp.tools import search as search_tools_module
from wisp.tools import shell as shell_tools_module
from wisp.tools.builtin import (
    BashTool,
    EditTool,
    FindTool,
    GrepTool,
    LsTool,
    ProcessResult,
    ReadTool,
    WriteTool,
)
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text_tail

pytestmark = pytest.mark.process


def run_tool(tool: object, arguments: dict[str, object], context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        result = await tool.run(arguments, context)  # type: ignore[attr-defined]
        assert isinstance(result, ToolResult)
        return result

    return anyio.run(run)


def test_tail_truncation_with_marker_only_budget_stays_bounded() -> None:
    result = truncate_text_tail("diagnostic tail", max_bytes=12, max_lines=10)

    assert result.text == "[truncated]"
    assert len(result.text.encode("utf-8")) <= 12
    assert result.truncated is True


def test_process_result_preserves_public_positional_stdout_count_slot() -> None:
    result = ProcessResult(0, "out", "err", False, False, 7)

    assert result.stdout_count == 7
    assert result.stdout_dropped_bytes == 0
    assert result.stderr_dropped_bytes == 0


def test_read_tool_supports_offset_limit_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(ReadTool(), {"path": "notes.txt", "offset": 2, "limit": 3}, context)

    assert result.text == "two\nthree\n[truncated]"
    assert result.truncated is True
    assert "line_count" not in result.data
    assert result.data["selected_count"] == 3


def test_summary_module_reads_the_real_tool_data_keys(tmp_path: Path) -> None:
    # Guard against the formatter and the tools drifting on data-key names (grep uses
    # "matches", find uses "files", etc.): run each read-type tool for real and
    # confirm summarize_tool_result produces a sensible summary from its actual data.
    from wisp.tools.summary import summarize_tool_result

    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("alpha again\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    def summary_of(name: str, result: ToolResult) -> str | None:
        # Call it exactly as the executor does — with the tool's own truncated flag.
        return summarize_tool_result(name, result.data, truncated=result.truncated)

    read = run_tool(ReadTool(), {"path": "notes.txt"}, context)
    assert summary_of("read", read) == "read 3 lines from notes.txt"

    # Paging a slice of a file must report the returned lines and the file total, not
    # the whole-file count — the P1 the review caught (summary replaces the dump).
    paged = run_tool(ReadTool(), {"path": "notes.txt", "offset": 2, "limit": 1}, context)
    assert paged.text == "beta\n"
    assert summary_of("read", paged) == "read 1 line from notes.txt"

    grep = run_tool(GrepTool(), {"pattern": "alpha"}, context)
    assert summary_of("grep", grep) == "grep: 2 matches"

    find = run_tool(FindTool(), {"pattern": "*.txt"}, context)
    assert summary_of("find", find) == "find: 2 files"

    ls = run_tool(LsTool(), {"path": "."}, context)
    ls_summary = summary_of("ls", ls)
    assert ls_summary is not None and ls_summary.startswith("ls: 2 entries in ")

    grep_empty = run_tool(GrepTool(), {"pattern": "no-such-token-xyz"}, context)
    assert summary_of("grep", grep_empty) == "grep: no matches"

    # A capped grep sets ToolResult.truncated; the summary must carry the "+ more"
    # cue — the P2 the review caught (summary replaces the raw [truncated] marker).
    for index in range(20):
        (tmp_path / f"hit-{index}.txt").write_text("needle here\n", encoding="utf-8")
    capped = run_tool(GrepTool(), {"pattern": "needle", "max_results": 5}, context)
    assert capped.truncated is True
    capped_summary = summary_of("grep", capped)
    assert capped_summary is not None and capped_summary.endswith("(+ more)")


def test_read_tool_stops_after_requested_slice(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    path.write_text("".join(f"line {index}\n" for index in range(10_000)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(ReadTool(), {"path": "large.log", "offset": 5000, "limit": 2}, context)

    assert result.text == "line 4999\nline 5000\n"
    assert "line_count" not in result.data
    assert result.data["selected_count"] == 2


def test_read_line_slice_consumes_only_one_line_beyond_limit() -> None:
    consumed = 0

    class CountingLines:
        def __enter__(self) -> CountingLines:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> CountingLines:
            return self

        def __next__(self) -> str:
            nonlocal consumed
            consumed += 1
            return f"line {consumed}\n"

    class CountingPath:
        def open(self, *_args: object, **_kwargs: object) -> CountingLines:
            return CountingLines()

    result = file_ops_module._read_line_slice(
        CountingPath(),  # type: ignore[arg-type]
        offset=1,
        limit=2,
        max_bytes=50_000,
        max_lines=2_000,
    )

    assert result.text == "line 1\nline 2\n"
    assert result.line_count is None
    assert result.selected_count == 2
    assert consumed == 3


def test_read_tool_keeps_exact_line_count_when_slice_reaches_eof(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")

    result = run_tool(
        ReadTool(),
        {"path": "notes.txt", "offset": 2, "limit": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "two\n"
    assert result.data["line_count"] == 2
    assert result.data["selected_count"] == 1


def test_read_tool_runs_filesystem_scan_off_event_loop(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    original = file_ops_module._read_line_slice

    def blocking_read(*args: object, **kwargs: object) -> object:
        started.set()
        release.wait(timeout=2)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_ops_module, "_read_line_slice", blocking_read)

    async def scenario() -> bool:
        completed = anyio.Event()

        async def read_file() -> None:
            await ReadTool().run({"path": "notes.txt"}, ToolContext(cwd=tmp_path))
            completed.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(read_file)
            assert await anyio.to_thread.run_sync(started.wait, 1)
            await anyio.sleep(0)
            responsive = not completed.is_set()
            release.set()
        return responsive

    assert anyio.run(scenario) is True


def test_read_tool_abandons_worker_wait_on_cancel(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    (tmp_path / "notes.txt").write_text("one\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def blocking_read(*_args: object, **_kwargs: object) -> object:
        started.set()
        release.wait(timeout=2)
        return file_ops_module._ReadSlice("", None, 0, False)

    monkeypatch.setattr(file_ops_module, "_read_line_slice", blocking_read)

    async def scenario() -> None:
        async def read_file() -> None:
            await ReadTool().run({"path": "notes.txt"}, ToolContext(cwd=tmp_path))

        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(read_file)
                assert await anyio.to_thread.run_sync(started.wait, 1)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)


def test_read_tool_preserves_crlf_line_endings_for_edit_workflow(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    context = ToolContext(cwd=tmp_path)

    read_result = run_tool(ReadTool(), {"path": "notes.txt"}, context)
    edit_result = run_tool(
        EditTool(),
        {
            "path": "notes.txt",
            "edits": [{"oldText": read_result.text, "newText": "uno\r\ndos\r\n"}],
        },
        context,
    )

    assert read_result.text == "one\r\ntwo\r\n"
    assert edit_result.data["edits"] == 1
    assert path.read_bytes() == b"uno\r\ndos\r\n"


def test_write_tool_creates_parent_directories_and_overwrites(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    first = run_tool(WriteTool(), {"path": "nested/file.txt", "content": "first"}, context)
    second = run_tool(WriteTool(), {"path": "nested/file.txt", "content": "second"}, context)

    assert first.data["bytes"] == 5
    assert second.data["bytes"] == 6
    assert (tmp_path / "nested/file.txt").read_text(encoding="utf-8") == "second"


def test_write_tool_create_only_creates_new_file(tmp_path: Path) -> None:
    receipt = file_ops_module.CreateOnlyWriteReceipt()
    context = ToolContext(cwd=tmp_path, create_only_write_receipt=receipt)

    result = run_tool(
        WriteTool(),
        {"path": "nested/new.txt", "content": "new\n", "overwrite": False},
        context,
    )

    assert result.data["created"] is True
    assert "before_text" not in result.data
    path = tmp_path / "nested/new.txt"
    assert path.read_text(encoding="utf-8") == "new\n"
    info = path.lstat()
    assert receipt.path == path
    assert receipt.file_id == (info.st_dev, info.st_ino)


def test_write_tool_honors_operation_write_path_restriction(tmp_path: Path) -> None:
    allowed = tmp_path / "AGENTS.md"
    context = ToolContext(
        cwd=tmp_path,
        allowed_write_paths=(allowed,),
        require_create_only_writes=True,
        require_non_empty_writes=True,
    )

    run_tool(
        WriteTool(),
        {"path": "AGENTS.md", "content": "allowed\n", "overwrite": False},
        context,
    )
    with pytest.raises(ToolError, match="Write path is not allowed for this operation"):
        run_tool(
            WriteTool(),
            {"path": "other.txt", "content": "blocked\n", "overwrite": False},
            context,
        )
    with pytest.raises(ToolError, match="requires write calls with overwrite=false"):
        run_tool(
            WriteTool(),
            {"path": "AGENTS.md", "content": "overwrite\n"},
            context,
        )
    with pytest.raises(ToolError, match="requires non-empty write content"):
        run_tool(
            WriteTool(),
            {"path": "AGENTS.md", "content": "", "overwrite": False},
            context,
        )

    assert allowed.read_text(encoding="utf-8") == "allowed\n"
    assert not (tmp_path / "other.txt").exists()


def test_write_tool_refuses_operation_conflicting_path(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    conflict = tmp_path / "other-guidance.md"
    conflict.write_text("existing\n", encoding="utf-8")
    context = ToolContext(
        cwd=tmp_path,
        allowed_write_paths=(target,),
        conflicting_write_paths=(conflict,),
        require_create_only_writes=True,
    )

    with pytest.raises(ToolError, match="Conflicting write path already exists"):
        run_tool(
            WriteTool(),
            {"path": "AGENTS.md", "content": "generated\n", "overwrite": False},
            context,
        )

    assert not target.exists()
    assert conflict.read_text(encoding="utf-8") == "existing\n"


def test_write_tool_failed_create_only_content_write_leaves_no_partial_target(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    context = ToolContext(cwd=tmp_path)
    real_fdopen = file_ops_module.os.fdopen

    class FailingWriter:
        def __init__(self, descriptor: int, *args: object, **kwargs: object) -> None:
            self.file = real_fdopen(descriptor, *args, **kwargs)

        def __enter__(self) -> FailingWriter:
            return self

        def __exit__(self, *_args: object) -> None:
            self.file.close()

        def write(self, _content: str) -> int:
            raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file_ops_module.os, "fdopen", FailingWriter)

    with pytest.raises(ToolError, match="Could not create file: target.txt"):
        run_tool(
            WriteTool(),
            {"path": "target.txt", "content": "partial\n", "overwrite": False},
            context,
        )

    assert not (tmp_path / "target.txt").exists()
    assert list(tmp_path.iterdir()) == []


def test_write_tool_failed_create_only_publish_leaves_no_partial_target(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    context = ToolContext(cwd=tmp_path)

    def fail_link(_source: object, _target: object) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(file_ops_module.os, "link", fail_link)

    with pytest.raises(ToolError, match="Could not create file: target.txt"):
        run_tool(
            WriteTool(),
            {"path": "target.txt", "content": "partial\n", "overwrite": False},
            context,
        )

    assert not (tmp_path / "target.txt").exists()
    assert list(tmp_path.iterdir()) == []


def test_write_tool_reports_temporary_cleanup_failure_after_publish(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    context = ToolContext(cwd=tmp_path)
    real_unlink = file_ops_module.os.unlink

    def fail_temporary_unlink(path: str, *args: object, **kwargs: object) -> None:
        if path.startswith(".wisp-write-"):
            raise PermissionError(errno.EACCES, "permission denied")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(file_ops_module.os, "unlink", fail_temporary_unlink)

    with pytest.raises(ToolError, match="temporary-link cleanup failed"):
        run_tool(
            WriteTool(),
            {"path": "target.txt", "content": "complete\n", "overwrite": False},
            context,
        )

    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "complete\n"
    assert len(tuple(tmp_path.glob(".wisp-write-*"))) == 1


def test_write_tool_create_only_preserves_existing_file(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    path = tmp_path / "existing.txt"
    path.write_text("original\n", encoding="utf-8")

    with pytest.raises(ToolError, match="File already exists: existing.txt"):
        run_tool(
            WriteTool(),
            {"path": "existing.txt", "content": "replacement\n", "overwrite": False},
            context,
        )

    assert path.read_text(encoding="utf-8") == "original\n"


def test_write_tool_create_only_refuses_dangling_symlink(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "target.txt"
    link = tmp_path / "new.txt"
    link.symlink_to(target)

    with pytest.raises(ToolError, match="File already exists: new.txt"):
        run_tool(
            WriteTool(),
            {"path": "new.txt", "content": "content\n", "overwrite": False},
            context,
        )

    assert link.is_symlink()
    assert not target.exists()


def test_write_tool_preserves_exact_newline_bytes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    result = run_tool(WriteTool(), {"path": "mixed.txt", "content": "one\ntwo\r\n"}, context)

    assert result.data["bytes"] == len(b"one\ntwo\r\n")
    assert (tmp_path / "mixed.txt").read_bytes() == b"one\ntwo\r\n"


def test_write_tool_snapshots_prior_content_on_overwrite(tmp_path: Path) -> None:
    # An overwrite captures the file's prior text into data["before_text"] so the
    # TUI can render a before/after diff; the overwrite itself still happens.
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")

    result = run_tool(WriteTool(), {"path": "f.txt", "content": "new\n"}, context)

    assert result.data["before_text"] == "old\n"
    assert result.data["created"] is False
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\n"


def test_write_tool_omits_snapshot_when_creating_new_file(tmp_path: Path) -> None:
    # A create has no prior content: before_text must be absent, but created=True so
    # the renderer knows to preview it as a pure addition rather than fall back.
    context = ToolContext(cwd=tmp_path)

    result = run_tool(WriteTool(), {"path": "new.txt", "content": "hello\n"}, context)

    assert "before_text" not in result.data
    assert result.data["created"] is True


def test_write_tool_reports_overwrite_of_unsnapshotable_file(tmp_path: Path) -> None:
    # The exact Codex P2: overwriting a binary file yields no snapshot AND created is
    # False, so the renderer falls back to the summary instead of a pure-add diff
    # that would falsely read as a create.
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "f.bin").write_bytes(b"\xff\xfe\x00data")

    result = run_tool(WriteTool(), {"path": "f.bin", "content": "text\n"}, context)

    assert "before_text" not in result.data
    assert result.data["created"] is False


def test_write_tool_snapshot_preserves_prior_newline_bytes(tmp_path: Path) -> None:
    # The snapshot is read with newline="" so the diff reflects real terminator
    # changes; CRLF in the prior file must survive verbatim into before_text.
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "f.txt").write_bytes(b"a\r\nb\r\n")

    result = run_tool(WriteTool(), {"path": "f.txt", "content": "x\n"}, context)

    assert result.data["before_text"] == "a\r\nb\r\n"


def test_write_tool_skips_snapshot_for_non_utf8_prior_file(tmp_path: Path) -> None:
    # A binary/non-UTF-8 prior file can't be diffed as text: omit the snapshot and
    # let the write proceed rather than crash or ship garbage.
    context = ToolContext(cwd=tmp_path)
    (tmp_path / "f.bin").write_bytes(b"\xff\xfe\x00data")

    result = run_tool(WriteTool(), {"path": "f.bin", "content": "text\n"}, context)

    assert "before_text" not in result.data
    assert (tmp_path / "f.bin").read_text(encoding="utf-8") == "text\n"


def test_write_tool_skips_snapshot_for_oversize_prior_file(tmp_path: Path) -> None:
    # A prior file too large to diff (past the snapshot cap) is dropped rather than
    # shipped over the wire; the write still succeeds.
    from wisp.tools.file_ops import _WRITE_SNAPSHOT_MAX_CHARS

    context = ToolContext(cwd=tmp_path)
    (tmp_path / "big.txt").write_text("x" * (_WRITE_SNAPSHOT_MAX_CHARS + 1), encoding="utf-8")

    result = run_tool(WriteTool(), {"path": "big.txt", "content": "small\n"}, context)

    assert "before_text" not in result.data
    assert (tmp_path / "big.txt").read_text(encoding="utf-8") == "small\n"


def test_file_tools_reject_paths_outside_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    context = ToolContext(cwd=workspace)

    cases: tuple[tuple[object, dict[str, object]], ...] = (
        (ReadTool(), {"path": str(outside_file)}),
        (WriteTool(), {"path": str(outside_file), "content": "overwrite"}),
        (
            EditTool(),
            {"path": str(outside_file), "edits": [{"oldText": "outside", "newText": "inside"}]},
        ),
        (GrepTool(), {"pattern": "outside", "path": str(outside_file)}),
        (FindTool(), {"path": str(outside_dir)}),
        (LsTool(), {"path": str(outside_dir)}),
    )

    for tool, arguments in cases:
        with pytest.raises(ToolError, match="outside the tool working directory"):
            run_tool(tool, arguments, context)


def test_file_tools_allow_absolute_paths_inside_cwd(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("inside\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(ReadTool(), {"path": str(path)}, context)

    assert result.text == "inside\n"


def test_file_tools_can_opt_out_of_cwd_containment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(ReadTool(), {"path": str(outside_file)}, context)

    assert result.text == "outside\n"


def test_builtin_tools_have_safety_metadata() -> None:
    assert {tool.name: tool.safety for tool in (ReadTool(), GrepTool(), FindTool(), LsTool())} == {
        "read": "read",
        "grep": "read",
        "find": "read",
        "ls": "read",
    }
    assert WriteTool().safety == "mutating"
    assert EditTool().safety == "mutating"
    assert BashTool().safety == "command"


def test_bash_tool_schema_requires_operation_specific_arguments() -> None:
    schema = BashTool.input_schema

    assert schema["oneOf"] == [
        {
            "properties": {"operation": {"enum": ["run"]}},
            "required": ["command"],
        },
        {
            "properties": {"operation": {"enum": ["start"]}},
            "required": ["operation", "command"],
        },
        {
            "properties": {"operation": {"enum": ["poll"]}},
            "required": ["operation", "process_id"],
        },
        {
            "properties": {"operation": {"enum": ["cancel"]}},
            "required": ["operation", "process_id"],
        },
    ]


def test_edit_tool_applies_unique_replacements_from_original(tmp_path: Path) -> None:
    path = tmp_path / "story.txt"
    path.write_text("hello brave world\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        EditTool(),
        {
            "path": "story.txt",
            "edits": [
                {"oldText": "hello", "newText": "hi"},
                {"oldText": "world", "newText": "Wisp"},
            ],
        },
        context,
    )

    assert result.data["edits"] == 2
    assert path.read_text(encoding="utf-8") == "hi brave Wisp\n"


def test_edit_tool_preserves_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "crlf.txt"
    path.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        EditTool(),
        {"path": "crlf.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]},
        context,
    )

    assert result.data["edits"] == 1
    assert path.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_edit_tool_rejects_non_unique_replacement(tmp_path: Path) -> None:
    path = tmp_path / "dupes.txt"
    path.write_text("same same\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="found 2 matches"):
        run_tool(
            EditTool(),
            {"path": "dupes.txt", "edits": [{"oldText": "same", "newText": "once"}]},
            context,
        )


def test_bash_tool_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = (
        f"{python} -c \"import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)\""
    )

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.data["exit_code"] == 3
    assert result.data["stdout"] == "out\n"
    assert result.data["stderr"] == "err\n"
    assert result.text == "Command exited with code 3: out\nerr"


def test_bash_tool_reports_successful_exit_code_with_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = f"{python} -c \"print('verified')\""

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.text == "Command exited with code 0: verified"
    assert result.data["exit_code"] == 0


def test_bash_tool_reports_successful_exit_code_without_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = f'{python} -c "pass"'

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.text == "Command exited with code 0"
    assert result.data["exit_code"] == 0


def test_bash_tool_starts_polls_and_completes_resumable_process(tmp_path: Path) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        code = (
            "import time; print('first', flush=True); time.sleep(0.2); print('second', flush=True)"
        )
        command = f"{python} -u -c {shlex.quote(code)}"

        try:
            start = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 0,
                    "lifetime_seconds": 5,
                },
                context,
            )
            process_id = str(start.data["process_id"])
            chunks = [str(start.data["stdout"])]
            final: ToolResult | None = start if start.data["process_state"] == "completed" else None

            for _ in range(20):
                if final is not None:
                    break
                poll = await tool.run(
                    {
                        "operation": "poll",
                        "process_id": process_id,
                        "wait_seconds": 0.2,
                    },
                    context,
                )
                chunks.append(str(poll.data["stdout"]))
                if poll.data["process_state"] == "completed":
                    final = poll

            assert final is not None
            assert final.text.startswith(f"Process {process_id} completed with exit code 0")
            assert final.data["process_id"] == process_id
            assert final.data["process_state"] == "completed"
            assert final.data["exit_code"] == 0
            assert final.data["output_has_exit_status"] is False
            assert final.data["stdout_truncated"] is False
            assert final.data["stderr_truncated"] is False
            combined = "".join(chunks)
            assert combined.count("first\n") == 1
            assert combined.count("second\n") == 1
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_managed_update_preserves_retained_stdout_after_label(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=5, max_output_lines=1)
    update = process_manager_module.ProcessUpdate(
        process_id="p123",
        state="running",
        stdout="tail\n",
    )

    result = shell_tools_module._managed_update_result(update, context=context)

    assert result.text == "Process p123 is still running\nstdout:\ntail\n"
    assert result.data["stdout"] == "tail\n"
    assert result.truncated is False


def test_bash_managed_update_adds_poll_time_dropped_bytes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=20, max_output_lines=100)
    update = process_manager_module.ProcessUpdate(
        process_id="p123",
        state="running",
        stdout="x" * 100,
        stdout_dropped_bytes=3,
    )

    result = shell_tools_module._managed_update_result(update, context=context)
    stdout = str(result.data["stdout"])

    assert result.data["stdout_truncated"] is True
    retained_tail_bytes = len(stdout.removeprefix("[truncated] ").encode("utf-8"))
    assert (
        result.data["stdout_dropped_bytes"]
        == 3 + len(update.stdout.encode("utf-8")) - retained_tail_bytes
    )
    assert result.truncated is True


def test_bash_managed_update_counts_marker_only_output_as_dropped_bytes(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=5, max_output_lines=0)
    update = process_manager_module.ProcessUpdate(
        process_id="p123",
        state="running",
        stdout="abcde",
    )

    result = shell_tools_module._managed_update_result(update, context=context)

    assert result.data["stdout"] == "[trun"
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == len(update.stdout.encode("utf-8"))
    assert result.truncated is True


def test_bash_tool_cancels_resumable_process(tmp_path: Path) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(30)"'

        try:
            start = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 0,
                    "lifetime_seconds": 30,
                },
                context,
            )
            process_id = str(start.data["process_id"])

            cancelled = await tool.run(
                {"operation": "cancel", "process_id": process_id},
                context,
            )

            assert cancelled.text == f"Process {process_id} cancelled"
            assert cancelled.data["process_id"] == process_id
            assert cancelled.data["process_state"] == "cancelled"
            assert "exit_code" not in cancelled.data
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_cancels_started_process_when_initial_poll_is_cancelled(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(30)"'

        try:
            start_task = asyncio.create_task(
                tool.run(
                    {
                        "operation": "start",
                        "command": command,
                        "yield_seconds": 30,
                        "lifetime_seconds": 60,
                    },
                    context,
                )
            )
            supervisor = tool._process_supervisor  # noqa: SLF001
            assert supervisor is not None
            with anyio.fail_after(5):
                while not supervisor._managed:  # noqa: SLF001
                    await asyncio.sleep(0.01)
            process_id = next(iter(supervisor._managed))  # noqa: SLF001

            start_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start_task

            update = await supervisor.poll(process_id)
            assert update.state == "cancelled"
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_shields_initial_poll_cleanup_under_anyio_cancellation(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(30)"'

        try:
            supervisor = tool._process_supervisor  # noqa: SLF001
            assert supervisor is not None

            async def start() -> None:
                await tool.run(
                    {
                        "operation": "start",
                        "command": command,
                        "yield_seconds": 30,
                        "lifetime_seconds": 60,
                    },
                    context,
                )

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(start)
                with anyio.fail_after(5):
                    while not supervisor._managed:  # noqa: SLF001
                        await anyio.sleep(0.01)
                process_id = next(iter(supervisor._managed))  # noqa: SLF001
                task_group.cancel_scope.cancel()

            update = await supervisor.poll(process_id)
            assert update.state == "cancelled"
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_reports_resumable_timeout_as_terminal_state(tmp_path: Path) -> None:
    async def run() -> None:
        context = ToolContext(cwd=tmp_path)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        command = f'{python} -c "import time; time.sleep(5)"'

        try:
            result = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 1,
                    "lifetime_seconds": 0.1,
                },
                context,
            )
            process_id = str(result.data["process_id"])
            for _ in range(20):
                if result.data["process_state"] == "timed_out":
                    break
                result = await tool.run(
                    {
                        "operation": "poll",
                        "process_id": process_id,
                        "wait_seconds": 0.1,
                    },
                    context,
                )

            assert result.text.startswith(f"Process {process_id} timed out")
            assert result.data["process_state"] == "timed_out"
            assert result.data["output_has_exit_status"] is False
            assert "exit_code" not in result.data
        finally:
            await tool.aclose()

    anyio.run(run)


def test_bash_tool_requires_supervisor_for_resumable_operations(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="bash.operation=poll requires a process supervisor"):
        run_tool(BashTool(None), {"operation": "poll", "process_id": "p1"}, context)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"operation": "bogus"}, "bash.operation must be one of"),
        (
            {"operation": "start", "command": "pwd", "yield_seconds": -1},
            "bash.yield_seconds must be greater than or equal to zero",
        ),
        (
            {"operation": "poll", "process_id": "p1", "wait_seconds": float("inf")},
            "bash.wait_seconds must be finite",
        ),
    ],
)
def test_bash_tool_validates_resumable_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match=message):
        run_tool(BashTool(), arguments, context)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell signal assertion")
@pytest.mark.parametrize(("signal_name", "exit_code"), [("HUP", 129), ("INT", 130), ("TERM", 143)])
def test_bash_tool_preserves_posix_shell_signal_exit(
    tmp_path: Path,
    signal_name: str,
    exit_code: int,
) -> None:
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        BashTool(),
        {"command": f"kill -{signal_name} $$; echo survived"},
        context,
    )

    assert result.data["exit_code"] == exit_code
    assert result.data["stdout"] == ""


def test_bash_tool_preserves_exit_code_outside_tiny_body_budget(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(exit_code=7, stdout="", stderr="")

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=0)

    result = run_tool(BashTool(None), {"command": "ignored"}, context)

    assert result.text == "Command exited with code 7"
    assert result.data["output_has_exit_status"] is True
    assert result.truncated is False


def test_bash_tool_does_not_add_separator_for_newline_only_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    command = f'{python} -c "print()"'

    result = run_tool(BashTool(), {"command": command}, context)

    assert result.text == "Command exited with code 0"
    assert result.data["stdout"] == "\n"


def test_bash_tool_retruncates_combined_stdout_and_stderr(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=40, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.write('o' * 100); sys.stderr.write('e' * 100)"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command}, context)

    status_overhead = len(f"Command exited with code {result.data['exit_code']}: ".encode())
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes + status_overhead
    assert result.text.startswith(f"Command exited with code {result.data['exit_code']}:")
    assert result.truncated is True


def test_bash_tool_reserves_status_space_without_losing_diagnostic_tail(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(
            exit_code=2,
            stdout="setup output " * 8,
            stderr="traceback final diagnostic 尾",
            stdout_truncated=True,
        )

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=100)

    result = run_tool(BashTool(None), {"command": "ignored"}, context)

    status_overhead = len(b"Command exited with code 2: ")
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes + status_overhead
    assert result.text.startswith("Command exited with code 2: [truncated] ")
    assert result.text.endswith("traceback final diagnostic 尾")
    assert "\ufffd" not in result.text
    assert result.truncated is True


def test_bash_tool_bounds_output_before_buffering(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=1000)
    python = shlex.quote(sys.executable)
    code = (
        "import sys; "
        "\nfor _ in range(10000): "
        "\n    sys.stdout.write('x' * 1000 + '\\n'); sys.stdout.flush()"
    )
    command = f"{python} -u -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert len(str(result.data["stdout"]).encode("utf-8")) <= context.max_output_bytes
    assert result.truncated is True


def test_bash_tool_reports_one_shot_stream_truncation_metadata(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=80, max_output_lines=1000)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.write('x' * 10000)"
    command = f"{python} -u -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout_truncated"] is True
    assert result.data["stderr_truncated"] is False
    assert result.data["stdout_dropped_bytes"] > 0
    assert result.data["stderr_dropped_bytes"] == 0
    assert result.truncated is True


def test_bash_tool_counts_marker_only_output_as_dropped_bytes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(exit_code=0, stdout="abcde", stderr="")

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=5, max_output_lines=0)

    result = run_tool(BashTool(None), {"command": "ignored"}, context)

    assert result.data["stdout"] == "[trun"
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == 5
    assert result.truncated is True


def test_bash_tool_counts_source_bytes_when_utf8_clip_decodes_replacement(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.buffer.write(bytes([0xc3, 0xa9]))"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout"] == "["
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == 2
    assert result.truncated is True


def test_bash_tool_counts_retruncated_replacement_source_bytes(
    tmp_path: Path,
) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=15, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.buffer.write(bytes([0xff] * 15))"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout"] == "\ufffd\n[truncated]"
    assert result.data["stdout_truncated"] is True
    assert result.data["stdout_dropped_bytes"] == 14
    assert result.truncated is True


def test_bash_tool_counts_managed_source_bytes_when_utf8_clip_decodes_replacement(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[str, int, bool]:
        context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=100)
        tool = BashTool()
        python = shlex.quote(sys.executable)
        code = "import sys; sys.stdout.buffer.write(bytes([0xff, 0x61])); sys.stdout.flush()"
        command = f"{python} -c {shlex.quote(code)}"
        try:
            result = await tool.run(
                {
                    "operation": "start",
                    "command": command,
                    "yield_seconds": 0.2,
                    "lifetime_seconds": 5,
                },
                context,
            )
            process_id = str(result.data["process_id"])
            results = [result]
            for _ in range(20):
                if result.data["process_state"] == "completed":
                    break
                result = await tool.run(
                    {
                        "operation": "poll",
                        "process_id": process_id,
                        "wait_seconds": 0.2,
                    },
                    context,
                )
                results.append(result)
            return (
                "".join(str(item.data["stdout"]) for item in results),
                sum(int(item.data["stdout_dropped_bytes"]) for item in results),
                any(item.truncated for item in results),
            )
        finally:
            await tool.aclose()

    stdout, stdout_dropped_bytes, truncated = anyio.run(run)

    assert stdout == "a"
    assert stdout_dropped_bytes == 1
    assert truncated is True


def test_bash_tool_does_not_kill_process_at_exact_output_limit(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=1)
    python = shlex.quote(sys.executable)
    marker = tmp_path / "finished.txt"
    code = (
        "import pathlib, time; "
        "print('done'); time.sleep(0.2); "
        f"pathlib.Path({str(marker)!r}).write_text('ok')"
    )
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)

    assert result.data["stdout"] == "done\n"
    assert result.truncated is False
    assert marker.read_text(encoding="utf-8") == "ok"


def test_bash_tool_reports_timeout_and_kills_child_processes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    marker = tmp_path / "child-survived.txt"
    child_code = (
        f"import pathlib, time; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(5)"
    )
    command = f"{python} -c {shlex.quote(parent_code)}"

    with pytest.raises(ToolError, match="timed out"):
        run_tool(BashTool(), {"command": command, "timeout": 1}, context)
    time.sleep(1.0)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_bash_completion_kills_background_child_with_redirected_output(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    child_pid_path = tmp_path / "background.pid"
    command = f"sleep 30 >/dev/null 2>&1 & echo $! > {shlex.quote(str(child_pid_path))}"

    result = run_tool(BashTool(), {"command": command, "timeout": 5}, context)
    child_pid = int(child_pid_path.read_text())

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("background child remained alive after bash completion")

    assert result.data["exit_code"] == 0


def test_bash_tool_reports_process_tree_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0

    async def fail_terminate(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_attempts > 1

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_terminate)

    context = ToolContext(cwd=tmp_path)
    source = "print('done')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    tool = BashTool()

    with pytest.raises(ToolError, match="Failed to terminate process tree"):
        run_tool(tool, {"command": command, "timeout": 5}, context)
    supervisor = tool._process_supervisor  # noqa: SLF001
    assert supervisor is not None
    assert len(supervisor._one_shot) == 1  # noqa: SLF001
    anyio.run(tool.aclose)
    assert len(supervisor._one_shot) == 0  # noqa: SLF001
    assert cleanup_attempts == 2


def test_search_tools_expose_process_cleanup() -> None:
    class DummySupervisor:
        def __init__(self) -> None:
            self.close_count = 0

        async def aclose(self) -> None:
            self.close_count += 1

    async def run() -> tuple[int, int]:
        grep_supervisor = DummySupervisor()
        find_supervisor = DummySupervisor()
        await GrepTool(grep_supervisor).aclose()  # type: ignore[arg-type]
        await FindTool(find_supervisor).aclose()  # type: ignore[arg-type]
        return grep_supervisor.close_count, find_supervisor.close_count

    assert anyio.run(run) == (1, 1)


def test_kill_process_tree_and_wait_returns_failure_without_waiting(
    monkeypatch: MonkeyPatch,
) -> None:
    async def fail_terminate(_process: asyncio.subprocess.Process) -> bool:
        return False

    class DummyProcess:
        stdout = None
        stderr = None
        wait_called = False

        async def wait(self) -> int:
            self.wait_called = True
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process = DummyProcess()
    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", fail_terminate)

    async def run() -> bool:
        with anyio.fail_after(0.1):
            return await process_tools_module._kill_process_tree_and_wait(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert anyio.run(run) is False
    assert process.wait_called is False


def test_posix_descendant_discovery_failure_reports_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, tmp_path / "jobs")  # noqa: SLF001
    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: None)

    async def run() -> bool:
        return await process_tools_module._terminate_process_tree(  # type: ignore[arg-type]  # noqa: SLF001
            process,
            force=True,
        )

    assert anyio.run(run) is False


def test_posix_records_jobs_file_holder_pids(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: (234,))
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: (345,))

    recorded = process_tools_module._record_posix_descendant_pids(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert recorded is True
    assert jobs_file.read_text(encoding="utf-8").splitlines() == ["234", "345"]


def test_posix_holder_discovery_failure_reports_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: ())
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: None)

    assert process_tools_module._record_posix_descendant_pids(process) is False  # type: ignore[arg-type]  # noqa: SLF001


def test_posix_recorded_jobs_prune_stale_pids(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n345\n456\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", lambda _pid: (234,))
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: (345,))
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(process_tools_module.signal, "pidfd_send_signal", None, raising=False)
    kills: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(234, signal.SIGKILL), (345, signal.SIGKILL)]


def test_posix_recorded_jobs_skip_descendant_scan_after_leader_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = 0

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n345\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001

    def fail_descendant_scan(_pid: int) -> tuple[int, ...]:
        raise AssertionError("exited leader pid must not be traversed")

    monkeypatch.setattr(process_tools_module, "_posix_descendant_pids", fail_descendant_scan)
    monkeypatch.setattr(process_tools_module, "_posix_jobs_file_holder_pids", lambda _path: (345,))
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(process_tools_module.signal, "pidfd_send_signal", None, raising=False)
    kills: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(345, signal.SIGKILL)]


def test_posix_recorded_jobs_revalidate_after_pidfd_open(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    events: list[str] = []
    closed_fds: list[int] = []

    def current_owned(_process: object) -> set[int]:
        events.append("snapshot")
        return set()

    def open_pidfd(_pid: int, _flags: int) -> int:
        events.append("open")
        return 789

    def fail_pidfd_send_signal(
        _pidfd: int,
        _selected_signal: signal.Signals,
        _siginfo: object,
        _flags: int,
    ) -> None:
        raise AssertionError("stale pidfd must not be signaled")

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", current_owned)
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", open_pidfd, raising=False)
    monkeypatch.setattr(
        process_tools_module.signal,
        "pidfd_send_signal",
        fail_pidfd_send_signal,
        raising=False,
    )
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert events == ["open", "snapshot"]
    assert closed_fds == [789]


def test_posix_recorded_jobs_fall_back_when_pidfd_open_is_unsupported(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    kills: list[tuple[int, signal.Signals]] = []

    def unsupported_pidfd_open(_pid: int, _flags: int) -> int:
        raise OSError(errno.ENOSYS, "pidfd_open unavailable")

    def fail_pidfd_send_signal(*_args: object) -> None:
        raise AssertionError("pidfd_send_signal should not run after pidfd_open fails")

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", lambda _process: {234})
    monkeypatch.setattr(
        process_tools_module.os, "pidfd_open", unsupported_pidfd_open, raising=False
    )
    monkeypatch.setattr(
        process_tools_module.signal,
        "pidfd_send_signal",
        fail_pidfd_send_signal,
        raising=False,
    )
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(234, signal.SIGKILL)]


def test_posix_recorded_jobs_fall_back_when_pidfd_send_signal_is_unsupported(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    kills: list[tuple[int, signal.Signals]] = []
    closed_fds: list[int] = []

    def unsupported_pidfd_send_signal(
        _pidfd: int,
        _selected_signal: signal.Signals,
        _siginfo: object,
        _flags: int,
    ) -> None:
        raise OSError(errno.ENOSYS, "pidfd_send_signal unavailable")

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", lambda _process: {234})
    monkeypatch.setattr(
        process_tools_module.os, "pidfd_open", lambda _pid, _flags: 789, raising=False
    )
    monkeypatch.setattr(
        process_tools_module.signal,
        "pidfd_send_signal",
        unsupported_pidfd_send_signal,
        raising=False,
    )
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert kills == [(234, signal.SIGKILL)]
    assert closed_fds == [789]


def test_posix_recorded_jobs_batch_ownership_before_signaling(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("234\n345\n", encoding="utf-8")
    process = DummyProcess()
    setattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR, jobs_file)  # noqa: SLF001
    kills: list[tuple[int, signal.Signals]] = []
    ownership_calls = 0

    def current_owned(_process: object) -> set[int]:
        nonlocal ownership_calls
        ownership_calls += 1
        return {234, 345}

    monkeypatch.setattr(process_tools_module, "_posix_current_owned_pids", current_owned)
    monkeypatch.setattr(process_tools_module.os, "pidfd_open", None, raising=False)
    monkeypatch.setattr(process_tools_module.signal, "pidfd_send_signal", None, raising=False)
    monkeypatch.setattr(process_tools_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    signaled = process_tools_module._signal_posix_recorded_jobs(  # type: ignore[arg-type]  # noqa: SLF001
        process,
        signal.SIGKILL,
    )

    assert signaled is True
    assert ownership_calls == 1
    assert kills == [(234, signal.SIGKILL), (345, signal.SIGKILL)]


def test_open_posix_jobs_fd_uses_descriptor_below_soft_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import fcntl
    import resource

    jobs_file = tmp_path / "jobs"
    jobs_file.write_text("", encoding="utf-8")
    closed_fds: list[int] = []
    minimum_fds: list[int] = []

    def fake_fcntl(_fd: int, command: int, minimum_fd: int) -> int:
        assert command == fcntl.F_DUPFD
        minimum_fds.append(minimum_fd)
        if minimum_fd >= 64:
            raise OSError(errno.EINVAL, "minimum fd is above soft limit")
        return 63

    monkeypatch.setattr(process_tools_module.os, "open", lambda _path, _flags: 3)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(resource, "getrlimit", lambda _limit: (64, 64))
    monkeypatch.setattr(fcntl, "fcntl", fake_fcntl)

    jobs_fd = process_tools_module._open_posix_jobs_fd(jobs_file)  # noqa: SLF001

    assert jobs_fd == 63
    assert minimum_fds == [63]
    assert closed_fds == [3]


def test_posix_recorded_job_verification_runs_off_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None

    worker_thread_ids: list[int] = []

    def fake_record(_process: object) -> bool:
        return True

    def fake_signal_group(_process: object, _selected_signal: signal.Signals) -> bool:
        return True

    def fake_signal_recorded(_process: object, _selected_signal: signal.Signals) -> bool:
        worker_thread_ids.append(threading.get_ident())
        return True

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module, "_record_posix_descendant_pids", fake_record)
    monkeypatch.setattr(process_tools_module, "_signal_posix_process_group", fake_signal_group)
    monkeypatch.setattr(process_tools_module, "_signal_posix_recorded_jobs", fake_signal_recorded)

    async def run() -> int:
        event_loop_thread_id = threading.get_ident()
        assert await process_tools_module._terminate_process_tree(DummyProcess()) is True  # type: ignore[arg-type]  # noqa: SLF001
        return event_loop_thread_id

    event_loop_thread_id = anyio.run(run)

    assert worker_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in worker_thread_ids)


def test_posix_jobs_file_holder_pids_uses_lsof(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="234\nnot-a-pid\n345\n")

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: None
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) == (234, 345)  # noqa: SLF001
    assert calls == [["lsof", "-t", "--", str(jobs_file)]]


def test_posix_jobs_file_holder_pids_uses_proc_before_external_tools(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"

    def fail_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("external holder probe should not run")

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: (234,)
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fail_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) == (234,)  # noqa: SLF001


def test_posix_jobs_file_holder_pids_falls_back_to_fuser(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "lsof":
            raise OSError
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{jobs_file}: 234 345\n",
            stderr="",
        )

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: None
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) == (234, 345)  # noqa: SLF001
    assert calls == [["lsof", "-t", "--", str(jobs_file)], ["fuser", str(jobs_file)]]


def test_posix_jobs_file_holder_pids_reports_probe_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    jobs_file = tmp_path / "jobs"

    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError

    monkeypatch.setattr(
        process_tools_module, "_posix_jobs_file_holder_pids_from_proc", lambda _path: None
    )
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    assert process_tools_module._posix_jobs_file_holder_pids(jobs_file) is None  # noqa: SLF001


def test_posix_permission_error_reports_cleanup_failure_for_running_leader(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = None
        killed = False

        def kill(self) -> None:
            self.killed = True

    def fail_killpg(_pid: int, _signal: int) -> None:
        raise PermissionError

    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.os, "killpg", fail_killpg)

    assert process_tools_module._kill_process_tree(process) is False  # type: ignore[arg-type]  # noqa: SLF001
    assert process.killed is True


def test_posix_group_signal_skips_exited_leader_pid(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pid = 123
        returncode = 0

    def fail_killpg(_pid: int, _signal: int) -> None:
        raise AssertionError("exited leader pid must not be signaled as a process group")

    monkeypatch.setattr(process_tools_module.os, "killpg", fail_killpg)

    assert (
        process_tools_module._signal_posix_process_group(  # type: ignore[arg-type]  # noqa: SLF001
            DummyProcess(),
            signal.SIGKILL,
        )
        is True
    )


def test_posix_shell_uses_hidden_high_jobs_fd(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = 0

    creation_calls: list[dict[str, object]] = []
    closed_fds: list[int] = []
    process = DummyProcess()

    async def fake_create_subprocess_shell(
        command: str,
        **kwargs: object,
    ) -> DummyProcess:
        assert "exec 9" not in command
        creation_calls.append(kwargs)
        return process

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.sys, "platform", "darwin")
    monkeypatch.setattr(process_tools_module, "_open_posix_jobs_fd", lambda _path: 123)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )

    async def run() -> DummyProcess:
        return await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001

    result = anyio.run(run)

    assert result is process
    assert creation_calls[0]["pass_fds"] == (123,)
    assert creation_calls[0]["start_new_session"] is True
    assert closed_fds == [123]
    process_tools_module._remove_posix_jobs_file(  # noqa: SLF001
        getattr(process, process_tools_module._POSIX_JOBS_FILE_ATTR),  # noqa: SLF001
    )


def test_linux_posix_shell_startup_uses_exec_helper_instead_of_preexec(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        pass

    process = DummyProcess()
    exec_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    closed_fds: list[int] = []

    async def create_exec(*command: str, **kwargs: object) -> DummyProcess:
        exec_calls.append((command, kwargs))
        return process

    async def fail_shell(*_args: object, **_kwargs: object) -> DummyProcess:
        raise AssertionError("Linux POSIX startup should use exec helper")

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.sys, "platform", "linux")
    monkeypatch.setattr(process_tools_module, "_open_posix_jobs_fd", lambda _path: 123)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", create_exec)
    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_shell", fail_shell)

    async def run() -> DummyProcess:
        return await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001

    result = anyio.run(run)
    jobs_file = getattr(result, process_tools_module._POSIX_JOBS_FILE_ATTR)  # noqa: SLF001
    jobs_file.unlink(missing_ok=True)

    assert result is process
    assert len(exec_calls) == 1
    command, kwargs = exec_calls[0]
    assert command[:3] == (
        process_tools_module.sys.executable,
        "-c",
        process_tools_module._POSIX_SUBREAPER_HELPER,  # noqa: SLF001
    )
    assert "echo hi" in command[3]
    assert "preexec_fn" not in kwargs
    assert kwargs["pass_fds"] == (123,)
    assert kwargs["start_new_session"] is True
    assert closed_fds == [123]


def test_posix_shell_spawn_cancellation_cleans_tracking_resources(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_fds: list[int] = []
    removed_files: list[object] = []

    async def cancelled_create_subprocess_exec(
        *_command: str,
        **_kwargs: object,
    ) -> object:
        raise asyncio.CancelledError

    async def fail_create_subprocess_shell(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Linux POSIX startup should use exec helper")

    monkeypatch.setattr(process_tools_module.os, "name", "posix")
    monkeypatch.setattr(process_tools_module.sys, "platform", "linux")
    monkeypatch.setattr(process_tools_module, "_open_posix_jobs_fd", lambda _path: 123)
    monkeypatch.setattr(process_tools_module, "_close_posix_fd", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(
        process_tools_module, "_remove_posix_jobs_file", lambda path: removed_files.append(path)
    )
    monkeypatch.setattr(
        process_tools_module.asyncio,
        "create_subprocess_exec",
        cancelled_create_subprocess_exec,
    )
    monkeypatch.setattr(
        process_tools_module.asyncio,
        "create_subprocess_shell",
        fail_create_subprocess_shell,
    )

    async def run() -> None:
        await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        anyio.run(run)

    assert closed_fds == [123]
    assert len(removed_files) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group assertion")
def test_bash_timeout_kills_background_child_after_shell_exits(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)
    marker = tmp_path / "background-child-survived.txt"
    command = f"(sleep 1.5; echo alive > {shlex.quote(str(marker))}) &"

    with pytest.raises(ToolError, match="timed out"):
        run_tool(BashTool(), {"command": command, "timeout": 1}, context)
    time.sleep(1.0)

    assert not marker.exists()


def test_bash_tool_cancellation_kills_child_processes(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX process-group cancellation regression")
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    ready = tmp_path / "parent-ready.txt"
    marker = tmp_path / "child-survived.txt"
    child_code = (
        f"import pathlib, time; time.sleep(1.0); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(5)"
    )
    command = f"{python} -c {shlex.quote(parent_code)}"

    async def run_and_cancel() -> None:
        async def run_bash() -> None:
            try:
                await BashTool().run({"command": command, "timeout": 10}, context)
            except anyio.get_cancelled_exc_class():
                pass

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_bash)
            with anyio.fail_after(2):
                while not ready.exists():
                    await anyio.sleep(0.05)
            task_group.cancel_scope.cancel()

    anyio.run(run_and_cancel)
    time.sleep(1.3)

    assert not marker.exists()


def test_direct_bash_cancellation_surfaces_cleanup_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pass

    process = DummyProcess()

    async def create_process(_command: str, *, cwd: Path) -> DummyProcess:
        assert cwd == tmp_path
        return process

    async def fail_cleanup(captured_process: object) -> bool:
        assert captured_process is process
        return False

    monkeypatch.setattr(process_tools_module, "_create_shell_process", create_process)
    monkeypatch.setattr(process_tools_module, "_kill_process_tree_and_wait", fail_cleanup)

    async def run_and_cancel() -> None:
        collect_started = asyncio.Event()

        async def collect_output(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
            collect_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(process_tools_module, "_collect_limited_output", collect_output)
        task = asyncio.create_task(
            process_tools_module._run_shell(  # noqa: SLF001
                "ignored",
                cwd=tmp_path,
                timeout=10,
                max_output_bytes=100,
                max_output_lines=100,
            )
        )
        await collect_started.wait()
        task.cancel()
        with pytest.raises(ToolError, match="Failed to terminate process tree"):
            await task

    anyio.run(run_and_cancel)


def test_direct_bash_cleanup_finishes_before_raw_task_cancellation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    context = ToolContext(cwd=tmp_path)
    python = shlex.quote(sys.executable)
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = False
    original_terminate = process_tools_module._terminate_process_tree  # type: ignore[attr-defined]

    async def delayed_terminate(
        process: asyncio.subprocess.Process,
        *,
        force: bool = False,
    ) -> bool:
        nonlocal cleanup_finished
        cleanup_started.set()
        await allow_cleanup.wait()
        cleanup_succeeded = await original_terminate(process, force=force)
        cleanup_finished = True
        return cleanup_succeeded

    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", delayed_terminate)

    async def run_and_cancel() -> None:
        task = asyncio.create_task(
            BashTool(None).run(
                {"command": f"{python} -c {shlex.quote("print('done')")}", "timeout": 10},
                context,
            )
        )
        await cleanup_started.wait()
        task.cancel()
        await anyio.sleep(0)
        assert task.done() is False

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(run_and_cancel)

    assert cleanup_finished is True


def test_direct_bash_cleans_process_after_capture_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyStream:
        def at_eof(self) -> bool:
            return True

    class DummyProcess:
        returncode: int | None = None
        stdout = DummyStream()
        stderr = DummyStream()
        wait_count = 0

        async def wait(self) -> None:
            self.wait_count += 1
            self.returncode = -15

    process = DummyProcess()
    cleanup_calls = 0

    async def create_process(_command: str, *, cwd: Path) -> DummyProcess:
        assert cwd == tmp_path
        return process

    async def fail_collect(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        raise ToolError("capture failed")

    async def terminate_process(
        captured_process: object,
        *,
        force: bool = False,
    ) -> bool:
        nonlocal cleanup_calls
        assert captured_process is process
        assert force is False
        cleanup_calls += 1
        return True

    monkeypatch.setattr(process_tools_module, "_create_shell_process", create_process)
    monkeypatch.setattr(process_tools_module, "_collect_limited_output", fail_collect)
    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", terminate_process)

    async def run() -> None:
        await process_tools_module._run_shell(  # noqa: SLF001
            "ignored",
            cwd=tmp_path,
            timeout=5,
            max_output_bytes=100,
            max_output_lines=100,
        )

    with pytest.raises(ToolError, match="capture failed"):
        anyio.run(run)
    assert cleanup_calls == 1
    assert process.wait_count == 1


def test_bash_tool_uses_taskkill_for_windows_process_tree_cleanup(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._kill_process_tree(process)  # noqa: SLF001

    assert kernel32.calls == [("wait", 456, 0)]
    assert calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert process.killed is False


def test_bash_tool_skips_taskkill_after_windows_job_termination(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = 0
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("close", 789),
    ]
    assert taskkill_calls == []
    assert process.killed is False


def test_bash_tool_retains_windows_job_handle_when_termination_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 0

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    terminated = process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert terminated is False
    assert getattr(process, process_tools_module._WINDOWS_JOB_HANDLE_ATTR) == 789  # noqa: SLF001
    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("wait", 456, 0),
    ]
    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert process.killed is True


def test_bash_tool_closes_windows_job_handle_after_taskkill_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 0

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    terminated = process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert terminated is True
    assert getattr(process, process_tools_module._WINDOWS_JOB_HANDLE_ATTR) is None  # noqa: SLF001
    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("wait", 456, 0),
        ("close", 789),
    ]
    assert process.killed is False


def test_bash_tool_assigns_windows_job_before_resuming_shell(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456

        async def wait(self) -> int:
            return 0

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create_job",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def CreateToolhelp32Snapshot(self, flags: int, process_id: int) -> int:
            self.calls.append(("snapshot", flags, process_id))
            return 111

        def Thread32First(
            self,
            snapshot: int,
            entry: object,
        ) -> int:
            self.calls.append(("thread_first", snapshot))
            entry.contents.th32OwnerProcessID = 123  # type: ignore[attr-defined]
            entry.contents.th32ThreadID = 654  # type: ignore[attr-defined]
            return 1

        def Thread32Next(self, snapshot: int, _entry: object) -> int:
            self.calls.append(("thread_next", snapshot))
            return 0

        def OpenThread(self, access: int, inherit: bool, thread_id: int) -> int:
            self.calls.append(("open_thread", access, inherit, thread_id))
            return 222

        def ResumeThread(self, thread: int) -> int:
            self.calls.append(("resume", thread))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    creation_calls: list[dict[str, object]] = []
    process = DummyProcess()
    kernel32 = FakeKernel32()

    async def fake_create_subprocess_shell(
        _command: str,
        **kwargs: object,
    ) -> DummyProcess:
        creation_calls.append(kwargs)
        return process

    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    async def run() -> DummyProcess:
        return await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001

    result = anyio.run(run)

    assert result is process
    assert creation_calls == [
        {
            "cwd": str(tmp_path),
            "start_new_session": False,
            "stdout": process_tools_module.asyncio.subprocess.PIPE,
            "stderr": process_tools_module.asyncio.subprocess.PIPE,
            "creationflags": process_tools_module._WINDOWS_CREATE_SUSPENDED,  # noqa: SLF001
        }
    ]
    assert kernel32.calls == [
        ("create_job",),
        ("assign", 789, 456),
        ("snapshot", process_tools_module._WINDOWS_TH32CS_SNAPTHREAD, 0),  # noqa: SLF001
        ("thread_first", 111),
        (
            "open_thread",
            process_tools_module._WINDOWS_THREAD_SUSPEND_RESUME,  # noqa: SLF001
            False,
            654,
        ),
        ("close", 111),
        ("resume", 222),
        ("close", 222),
    ]


def test_bash_tool_runs_windows_resume_setup_off_event_loop(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456

        async def wait(self) -> int:
            return 0

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create_job",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def ResumeThread(self, thread: int) -> int:
            self.calls.append(("resume", thread))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    process = DummyProcess()
    kernel32 = FakeKernel32()
    open_thread_ids: list[int] = []

    async def fake_create_subprocess_shell(
        _command: str,
        **_kwargs: object,
    ) -> DummyProcess:
        return process

    def fake_open_windows_process_thread(
        _process: object,
        _kernel32: object,
    ) -> int:
        open_thread_ids.append(threading.get_ident())
        return 222

    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)
    monkeypatch.setattr(
        process_tools_module,
        "_open_windows_process_thread",
        fake_open_windows_process_thread,
    )

    async def run() -> tuple[DummyProcess, int]:
        event_loop_thread_id = threading.get_ident()
        result = await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # type: ignore[return-value]  # noqa: SLF001
        return result, event_loop_thread_id

    result, event_loop_thread_id = anyio.run(run)

    assert result is process
    assert open_thread_ids
    assert all(thread_id != event_loop_thread_id for thread_id in open_thread_ids)
    assert kernel32.calls == [
        ("create_job",),
        ("assign", 789, 456),
        ("resume", 222),
        ("close", 222),
    ]


def test_bash_tool_aborts_suspended_windows_shell_when_job_assignment_fails(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = 1
            return 1

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create_job",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 0

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    process = DummyProcess()
    kernel32 = FakeKernel32()

    async def fake_create_subprocess_shell(
        _command: str,
        **_kwargs: object,
    ) -> DummyProcess:
        return process

    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(
        process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
    )
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    async def run() -> None:
        await process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # noqa: SLF001

    with pytest.raises(ToolError, match="Failed to attach command process to Windows job"):
        anyio.run(run)

    assert kernel32.calls == [
        ("create_job",),
        ("assign", 789, 456),
        ("close", 789),
    ]
    assert process.killed is True


def test_bash_tool_cleans_windows_job_after_resume_setup_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.returncode = 1
            return 1

    class FakeKernel32:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def CreateJobObjectW(self, _attributes: object, _name: object) -> int:
            self.calls.append(("create",))
            return 789

        def AssignProcessToJobObject(self, job: int, process: int) -> int:
            self.calls.append(("assign", job, process))
            return 1

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.calls.append(("terminate", job, exit_code))
            return 0

        def WaitForSingleObject(self, process: int, milliseconds: int) -> int:
            self.calls.append(("wait", process, milliseconds))
            return process_tools_module._WINDOWS_WAIT_TIMEOUT  # noqa: SLF001

        def CloseHandle(self, handle: int) -> int:
            self.calls.append(("close", handle))
            return 1

    taskkill_calls: list[list[str]] = []
    kernel32 = FakeKernel32()
    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module, "_windows_kernel32", lambda: kernel32)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._attach_windows_job(process)  # type: ignore[arg-type]  # noqa: SLF001
    cleanup_succeeded = anyio.run(
        process_tools_module._cleanup_failed_windows_process_setup,  # type: ignore[arg-type]  # noqa: SLF001
        process,
    )

    assert cleanup_succeeded is True
    assert getattr(process, process_tools_module._WINDOWS_JOB_HANDLE_ATTR) is None  # noqa: SLF001
    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert kernel32.calls == [
        ("create",),
        ("assign", 789, 456),
        ("terminate", 789, 1),
        ("wait", 456, 0),
        ("close", 789),
    ]


def test_bash_tool_surfaces_windows_setup_cleanup_failure_after_cancellation(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pass

    async def run() -> None:
        setup_started = asyncio.Event()
        allow_setup_finish = asyncio.Event()

        async def fake_to_thread(_function: object, _process: object) -> None:
            setup_started.set()
            await allow_setup_finish.wait()

        async def fail_cleanup(_process: object) -> bool:
            return False

        monkeypatch.setattr(process_tools_module.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(
            process_tools_module, "_cleanup_failed_windows_process_setup", fail_cleanup
        )

        setup_task = asyncio.create_task(
            process_tools_module._run_windows_process_setup(DummyProcess())  # type: ignore[arg-type]  # noqa: SLF001
        )
        await setup_started.wait()
        setup_task.cancel()
        allow_setup_finish.set()
        with pytest.raises(ToolError, match="terminate process tree"):
            await setup_task

    anyio.run(run)


def test_bash_tool_finishes_windows_setup_cleanup_after_repeated_cancellation(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        pass

    async def run() -> bool:
        setup_started = asyncio.Event()
        allow_setup_finish = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup_finish = asyncio.Event()
        cleanup_finished = False

        async def fake_to_thread(_function: object, _process: object) -> None:
            setup_started.set()
            await allow_setup_finish.wait()

        async def cleanup(_process: object) -> bool:
            nonlocal cleanup_finished
            cleanup_started.set()
            await allow_cleanup_finish.wait()
            cleanup_finished = True
            return True

        monkeypatch.setattr(process_tools_module.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(process_tools_module, "_cleanup_failed_windows_process_setup", cleanup)

        setup_task = asyncio.create_task(
            process_tools_module._run_windows_process_setup(DummyProcess())  # type: ignore[arg-type]  # noqa: SLF001
        )
        await setup_started.wait()
        setup_task.cancel()
        allow_setup_finish.set()
        await cleanup_started.wait()
        setup_task.cancel()
        await asyncio.sleep(0)
        assert not setup_task.done()
        allow_cleanup_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await setup_task
        return cleanup_finished

    assert anyio.run(run) is True


def test_bash_tool_delays_cancellation_during_windows_setup_error_cleanup(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        pass

    async def run() -> bool:
        cleanup_started = asyncio.Event()
        allow_cleanup_finish = asyncio.Event()
        cleanup_finished = False
        process = DummyProcess()

        async def fake_create_subprocess_shell(
            _command: str,
            **_kwargs: object,
        ) -> DummyProcess:
            return process

        async def setup(_process: object) -> str:
            return "Failed to resume command process"

        async def cleanup(_process: object) -> bool:
            nonlocal cleanup_finished
            cleanup_started.set()
            await allow_cleanup_finish.wait()
            cleanup_finished = True
            return True

        monkeypatch.setattr(process_tools_module.os, "name", "nt")
        monkeypatch.setattr(
            process_tools_module.asyncio, "create_subprocess_shell", fake_create_subprocess_shell
        )
        monkeypatch.setattr(process_tools_module, "_run_windows_process_setup", setup)
        monkeypatch.setattr(process_tools_module, "_cleanup_failed_windows_process_setup", cleanup)

        setup_task = asyncio.create_task(
            process_tools_module._create_shell_process("echo hi", cwd=tmp_path)  # noqa: SLF001
        )
        await cleanup_started.wait()
        setup_task.cancel()
        await asyncio.sleep(0)
        assert not setup_task.done()
        allow_cleanup_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await setup_task
        return cleanup_finished

    assert anyio.run(run) is True


def test_windows_kernel32_configures_pointer_sized_job_api_signatures(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeFunction:
        def __init__(self, result: int) -> None:
            self.result = result
            self.restype: object = None
            self.argtypes: list[object] | None = None

        def __call__(self, *_args: object) -> int:
            return self.result

    class FakeKernel32:
        def __init__(self) -> None:
            self.CreateJobObjectW = FakeFunction(789)
            self.AssignProcessToJobObject = FakeFunction(1)
            self.TerminateJobObject = FakeFunction(1)
            self.CloseHandle = FakeFunction(1)
            self.CreateToolhelp32Snapshot = FakeFunction(1)
            self.OpenThread = FakeFunction(1)
            self.ResumeThread = FakeFunction(1)
            self.Thread32First = FakeFunction(1)
            self.Thread32Next = FakeFunction(0)
            self.WaitForSingleObject = FakeFunction(process_tools_module._WINDOWS_WAIT_TIMEOUT)  # noqa: SLF001

    kernel32 = FakeKernel32()

    def fake_windll(name: str, *, use_last_error: bool) -> FakeKernel32:
        assert name == "kernel32"
        assert use_last_error is True
        return kernel32

    monkeypatch.setattr(process_tools_module.ctypes, "WinDLL", fake_windll, raising=False)

    assert process_tools_module._windows_kernel32() is kernel32  # noqa: SLF001
    assert kernel32.CreateJobObjectW.restype is process_tools_module.wintypes.HANDLE
    assert kernel32.CreateJobObjectW.argtypes == [
        process_tools_module.ctypes.c_void_p,
        process_tools_module.wintypes.LPCWSTR,
    ]
    assert kernel32.AssignProcessToJobObject.restype is process_tools_module.wintypes.BOOL
    assert kernel32.AssignProcessToJobObject.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.wintypes.HANDLE,
    ]
    assert kernel32.TerminateJobObject.restype is process_tools_module.wintypes.BOOL
    assert kernel32.TerminateJobObject.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.wintypes.UINT,
    ]
    assert kernel32.CloseHandle.restype is process_tools_module.wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [process_tools_module.wintypes.HANDLE]
    assert kernel32.CreateToolhelp32Snapshot.restype is process_tools_module.wintypes.HANDLE
    assert kernel32.CreateToolhelp32Snapshot.argtypes == [
        process_tools_module.wintypes.DWORD,
        process_tools_module.wintypes.DWORD,
    ]
    assert kernel32.OpenThread.restype is process_tools_module.wintypes.HANDLE
    assert kernel32.OpenThread.argtypes == [
        process_tools_module.wintypes.DWORD,
        process_tools_module.wintypes.BOOL,
        process_tools_module.wintypes.DWORD,
    ]
    assert kernel32.ResumeThread.restype is process_tools_module.wintypes.DWORD
    assert kernel32.ResumeThread.argtypes == [process_tools_module.wintypes.HANDLE]
    assert kernel32.Thread32First.restype is process_tools_module.wintypes.BOOL
    assert kernel32.Thread32First.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.ctypes.POINTER(process_tools_module._WindowsThreadEntry32),  # noqa: SLF001
    ]
    assert kernel32.Thread32Next.restype is process_tools_module.wintypes.BOOL
    assert kernel32.Thread32Next.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.ctypes.POINTER(process_tools_module._WindowsThreadEntry32),  # noqa: SLF001
    ]
    assert kernel32.WaitForSingleObject.restype is process_tools_module.wintypes.DWORD
    assert kernel32.WaitForSingleObject.argtypes == [
        process_tools_module.wintypes.HANDLE,
        process_tools_module.wintypes.DWORD,
    ]


def test_bash_tool_skips_taskkill_for_exited_windows_leader_without_job(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = 0
        pid = 123
        killed = False

        def kill(self) -> None:
            self.killed = True

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    process = DummyProcess()
    monkeypatch.setattr(process_tools_module.os, "name", "nt")
    monkeypatch.setattr(process_tools_module.subprocess, "run", fake_run)

    process_tools_module._kill_process_tree(process)  # type: ignore[arg-type]  # noqa: SLF001

    assert calls == []
    assert process.killed is False


def test_async_windows_tree_cleanup_does_not_block_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123

    monkeypatch.setattr(process_tools_module.os, "name", "nt")

    def slow_kill(_process: object) -> None:
        time.sleep(0.1)

    monkeypatch.setattr(process_tools_module, "_kill_process_tree", slow_kill)

    async def run() -> bool:
        completed = anyio.Event()

        async def terminate() -> None:
            await process_tools_module._terminate_process_tree(DummyProcess())  # type: ignore[arg-type]  # noqa: SLF001
            completed.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(terminate)
            await anyio.sleep(0.01)
            event_loop_remained_responsive = not completed.is_set()
        return event_loop_remained_responsive

    assert anyio.run(run) is True


def test_exec_helper_bounds_stderr_before_buffering(tmp_path: Path) -> None:
    async def run() -> process_tools_module.ProcessResult:
        supervisor = process_manager_module.ProcessSupervisor()
        return await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('e' * 10000)",
            ],
            cwd=tmp_path,
            process_supervisor=supervisor,
            max_stdout_lines=1,
            max_buffered_stderr_bytes=20,
            max_buffered_stderr_lines=100,
        )

    result = anyio.run(run)

    assert len(result.stderr.encode("utf-8")) <= 20
    assert result.stderr_truncated is True


def test_exec_helper_reports_failed_output_limit_termination(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"first\nsecond\n")
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self.returncode = None
            self.wait_called = False

        async def wait(self) -> int:
            self.wait_called = True
            if self.returncode is not None:
                return self.returncode
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process: DummyProcess | None = None

    async def fake_spawn(*_args: object, **_kwargs: object) -> DummyProcess:
        nonlocal process
        process = DummyProcess()
        return process

    cleanup_succeeds = False

    async def fail_terminate(_process: object) -> bool:
        if cleanup_succeeds:
            assert process is not None
            process.returncode = -9
            return True
        return False

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(process_tools_module, "_terminate_process_tree", fail_terminate)
    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_terminate)

    async def run() -> int:
        nonlocal cleanup_succeeds
        supervisor = process_manager_module.ProcessSupervisor()

        async def execute() -> None:
            await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                ["command"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
                max_buffered_stdout_lines=1,
            )

        with anyio.fail_after(1):
            with pytest.raises(ToolError, match="Failed to terminate process tree"):
                await asyncio.create_task(execute())
        retained = len(supervisor._one_shot)  # noqa: SLF001
        cleanup_succeeds = True
        await supervisor.aclose()
        return retained

    retained = anyio.run(run)

    assert process is not None
    assert process.wait_called is True
    assert retained == 1


def test_exec_helper_cleans_process_when_cancelled_during_registration(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_spawn = process_tools_module.asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None
    spawned = asyncio.Event()
    registration_started = asyncio.Event()
    allow_registration = asyncio.Event()

    async def record_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal process
        process = await original_spawn(*args, **kwargs)
        spawned.set()
        return process

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", record_spawn)

    async def run() -> tuple[int | None, int]:
        supervisor = process_manager_module.ProcessSupervisor()
        original_track = supervisor._track_one_shot  # noqa: SLF001

        async def delayed_track(
            tracked_process: asyncio.subprocess.Process,
            task: asyncio.Task[object],
            *,
            reserved: bool = False,
        ) -> None:
            registration_started.set()
            await allow_registration.wait()
            await original_track(tracked_process, task, reserved=reserved)

        monkeypatch.setattr(supervisor, "_track_one_shot", delayed_track)
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        await spawned.wait()
        await registration_started.wait()
        execute.cancel()
        await anyio.sleep(0)
        assert execute.done() is False

        allow_registration.set()
        with pytest.raises(asyncio.CancelledError):
            await execute
        assert process is not None
        return process.returncode, len(supervisor._one_shot)  # noqa: SLF001

    returncode, retained = anyio.run(run)

    assert returncode is not None
    assert retained == 0


def test_exec_helper_cleans_process_when_registration_finds_closed_supervisor(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_spawn = process_tools_module.asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None

    async def record_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal process
        process = await original_spawn(*args, **kwargs)
        return process

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", record_spawn)

    async def run() -> int:
        supervisor = process_manager_module.ProcessSupervisor()
        await supervisor.aclose()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        return len(supervisor._one_shot)  # noqa: SLF001

    retained = anyio.run(run)

    assert process is None
    assert retained == 0


def test_exec_helper_finishes_reservation_rollback_after_repeated_cancel(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    spawn_started = asyncio.Event()

    async def pending_spawn(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        spawn_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", pending_spawn)

    async def run() -> int:
        supervisor = process_manager_module.ProcessSupervisor()
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        await spawn_started.wait()
        async with supervisor._lock:  # noqa: SLF001
            execute.cancel()
            await anyio.sleep(0)
            execute.cancel()
            await anyio.sleep(0)
            assert execute.done() is False

        with pytest.raises(asyncio.CancelledError):
            await execute
        with anyio.fail_after(1):
            await supervisor.aclose()
        return supervisor._pending_one_shot_starts  # noqa: SLF001

    assert anyio.run(run) == 0


def test_aclose_waits_for_one_shot_reserved_before_close(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_spawn = process_tools_module.asyncio.create_subprocess_exec
    process: asyncio.subprocess.Process | None = None
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal process
        spawn_started.set()
        await allow_spawn.wait()
        process = await original_spawn(*args, **kwargs)
        return process

    monkeypatch.setattr(process_tools_module.asyncio, "create_subprocess_exec", delayed_spawn)

    async def run() -> int:
        supervisor = process_manager_module.ProcessSupervisor()
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        await spawn_started.wait()
        close_task = asyncio.create_task(supervisor.aclose())
        await anyio.sleep(0.05)
        assert close_task.done() is False

        allow_spawn.set()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await execute
        await close_task
        assert process is not None
        assert process.returncode is not None
        return len(supervisor._one_shot)  # noqa: SLF001

    retained = anyio.run(run)

    assert retained == 0


def test_exec_helper_delays_repeated_cancellation_until_cleanup_finishes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    supervisor = process_manager_module.ProcessSupervisor()
    original_terminate = supervisor._terminate_one_shot  # noqa: SLF001
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def delayed_terminate(
        process: asyncio.subprocess.Process,
        *,
        wait: bool = False,
    ) -> bool:
        cleanup_started.set()
        await allow_cleanup.wait()
        return await original_terminate(process, wait=wait)

    monkeypatch.setattr(supervisor, "_terminate_one_shot", delayed_terminate)

    async def run() -> int:
        execute = asyncio.create_task(
            process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                process_supervisor=supervisor,
                max_stdout_lines=10,
            )
        )
        with anyio.fail_after(1):
            while not supervisor._one_shot:  # noqa: SLF001
                await anyio.sleep(0)
        execute.cancel()
        await cleanup_started.wait()
        execute.cancel()
        await anyio.sleep(0)
        assert execute.done() is False

        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await execute
        return len(supervisor._one_shot)  # noqa: SLF001

    retained = anyio.run(run)

    assert retained == 0


def test_grep_tool_python_fallback_supports_literal_ignore_case_and_glob(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "alpha.txt").write_text("Needle here\n", encoding="utf-8")
    (tmp_path / "beta.md").write_text("needle elsewhere\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {
            "pattern": "needle",
            "path": ".",
            "glob": "*.txt",
            "literal": True,
            "ignore_case": True,
        },
        context,
    )

    assert result.data["count"] == 1
    assert result.text == "alpha.txt:1:Needle here"


def test_grep_tool_ripgrep_treats_option_like_pattern_as_literal(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("--help\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "--help", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:--help"
    assert "Usage:" not in result.text


def test_grep_tool_ripgrep_includes_filename_for_single_file_search(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("match\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": "data.txt", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:match"


@pytest.mark.skip(reason="ripgrep backend removed for descriptor-safe traversal")
def test_grep_tool_ripgrep_bounds_stdout_before_buffering(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, int | None, int | None, int | None, int | None]] = []

    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        process_supervisor: object,
        max_stdout_lines: int,
        stdout_line_filter: object = None,
        stdout_count_filter: object = None,
        max_buffered_stdout_bytes: int | None = None,
        max_buffered_stdout_lines: int | None = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
        assert isinstance(process_supervisor, process_manager_module.ProcessSupervisor)
        assert callable(stdout_count_filter)
        calls.append(
            (
                command,
                max_stdout_lines,
                max_buffered_stdout_bytes,
                max_buffered_stdout_lines,
                max_buffered_stderr_bytes,
                max_buffered_stderr_lines,
            )
        )
        return search_tools_module.ProcessResult(
            exit_code=-9,
            stdout="one.txt:1:e\ntwo.txt:1:e\nthree.txt:1:e\n",
            stderr="",
            stdout_truncated=True,
        )

    monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(search_tools_module, "_run_exec_limited_stdout", fake_run)
    # This test asserts the exact rg argv for stdout bounding; opt out of the
    # protected-path default so its --glob exclusions don't clutter the assertion.
    context = ToolContext(cwd=tmp_path, protected_paths=())

    result = run_tool(GrepTool(), {"pattern": "e", "path": ".", "max_results": 2}, context)

    assert calls == [
        (
            [
                "rg",
                "--no-config",
                "--no-follow",
                "--line-number",
                "--no-heading",
                "--color=never",
                "--with-filename",
                "--field-match-separator",
                "\x1f",
                "--field-context-separator",
                "\x1e",
                "--max-columns",
                "50000",
                "--",
                "e",
                ".",
            ],
            3,
            50000,
            2000,
            50000,
            2000,
        )
    ]
    assert result.text == "one.txt:1:e\ntwo.txt:1:e\n[truncated]"
    assert result.truncated is True


def test_grep_tool_ripgrep_bounds_long_lines_before_buffering(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("needle" + ("x" * 10_000) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=80)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text.startswith("data.txt:1:needle")
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes


def test_grep_tool_ripgrep_preserves_oversized_match_when_truncated(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("needle" + ("x" * 10_000) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=35)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text != "No matches"
    assert result.text.startswith("data.txt:1:")
    assert result.text.endswith("[truncated]")
    assert result.data["count"] == 1
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes


def test_grep_tool_ripgrep_counts_match_when_budget_cuts_record_prefix(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("needle" + ("x" * 10_000) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=5)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text != "No matches"
    assert result.data["count"] == 1
    assert result.truncated is True
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes


def test_grep_tool_ripgrep_bounds_context_output_before_buffering(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    lines = [f"before {index}" for index in range(50)]
    lines.append("needle")
    lines.extend(f"after {index}" for index in range(50))
    (tmp_path / "data.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_lines=10)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1000, "literal": True},
        context,
    )

    assert len(result.text.splitlines()) <= context.max_output_lines
    assert "data.txt:51:needle" in result.text
    assert "data.txt-1-before 0" not in result.text
    assert result.text.endswith("[truncated]")
    assert result.truncated is True


def test_grep_tool_ripgrep_does_not_count_context_text_as_match(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("context :123: text\nneedle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert "data.txt-1-context :123: text" in result.text
    assert "data.txt:2:needle" in result.text
    assert result.data["count"] == 1


def test_grep_tool_ripgrep_counts_matches_separately_from_context_lines(
    tmp_path: Path,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("before\nmatch\nafter\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert "data.txt-1-before" in result.text
    assert "data.txt:2:match" in result.text
    assert "data.txt-3-after" in result.text


@pytest.mark.skip(reason="ripgrep backend removed for descriptor-safe traversal")
def test_grep_tool_ripgrep_drops_context_for_omitted_merged_match(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        process_supervisor: object,
        max_stdout_lines: int,
        stdout_line_filter: object = None,
        stdout_count_filter: object = None,
        max_buffered_stdout_bytes: int | None = None,
        max_buffered_stdout_lines: int | None = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
        assert isinstance(process_supervisor, process_manager_module.ProcessSupervisor)
        assert max_stdout_lines == 2
        assert callable(stdout_count_filter)
        assert max_buffered_stdout_bytes == 50000
        assert max_buffered_stdout_lines == 2000
        assert max_buffered_stderr_bytes == 50000
        assert max_buffered_stderr_lines == 2000
        return search_tools_module.ProcessResult(
            exit_code=-9,
            stdout=(
                "data.txt\x1f1\x1fneedle one\n"
                "data.txt\x1e2\x1ebridge\n"
                "data.txt\x1f3\x1fneedle two\n"
            ),
            stderr="",
            stdout_truncated=True,
        )

    monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(search_tools_module, "_run_exec_limited_stdout", fake_run)
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert result.text == "data.txt:1:needle one\n[truncated]"
    assert result.data["matches"] == ["data.txt:1:needle one"]
    assert result.data["count"] == 1
    assert result.truncated is True


def test_grep_tool_ripgrep_preserves_whitespace_only_patterns(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "data.txt").write_text("a b\nab\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": " ", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:a b"


def test_grep_tool_ripgrep_ignores_config_that_follows_symlinks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = tmp_path / "ripgreprc"
    config.write_text("--follow\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    context = ToolContext(cwd=workspace)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_skips_symlinked_files_when_opted_out(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_skips_symlinked_files_outside_cwd(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_find_tool_python_fallback_skips_symlinked_files_outside_cwd(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    link = workspace / "outside.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


def test_grep_tool_python_fallback_skips_symlinked_files_when_opted_out(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_does_not_count_context_text_as_match(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("context :123: text\nneedle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert "data.txt-1-context :123: text" in result.text
    assert "data.txt:2:needle" in result.text
    assert result.data["count"] == 1


def test_grep_tool_python_fallback_counts_match_when_path_contains_separator(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    package_dir = tmp_path / "pkg-1-test"
    package_dir.mkdir()
    (package_dir / "a.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "path": ".", "literal": True},
        context,
    )

    assert result.text == "pkg-1-test/a.txt:1:needle"
    assert result.data["count"] == 1


def test_grep_tool_python_fallback_counts_matches_separately_from_context_lines(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("before\nmatch\nafter\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "context": 1, "literal": True, "max_results": 1},
        context,
    )

    assert "data.txt-1-before" in result.text
    assert "data.txt:2:match" in result.text
    assert "data.txt-3-after" in result.text


def test_grep_tool_python_fallback_preserves_whitespace_only_patterns(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("\tindent\nplain\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "\t", "path": ".", "literal": True},
        context,
    )

    assert result.text == "data.txt:1:\tindent"


def test_grep_tool_rejects_empty_pattern(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    with pytest.raises(ToolError, match="pattern must not be empty"):
        run_tool(GrepTool(), {"pattern": ""}, context)


def test_grep_tool_python_fallback_stops_after_extra_match(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    first = tmp_path / "first.txt"
    first.write_text("match\nmatch\n", encoding="utf-8")
    visited_later = False

    def files(_path: Path, _context: ToolContext) -> Iterable[Path]:
        nonlocal visited_later
        yield first
        visited_later = True
        yield tmp_path / "later.txt"

    monkeypatch.setattr(search_tools_module, "_iter_files", files)
    result = run_tool(
        GrepTool(),
        {"pattern": "match", "literal": True, "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "first.txt:1:match\n[truncated]"
    assert result.truncated is True
    assert visited_later is False


def test_grep_tool_python_fallback_does_not_materialize_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")

    def fail_read_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("Python grep must stream files")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    result = run_tool(
        GrepTool(),
        {"pattern": "needle", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "data.txt:1:needle"


def test_grep_tool_python_fallback_runs_off_event_loop(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    original = search_tools_module._python_grep

    def blocking_grep(**kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_tools_module, "_python_grep", blocking_grep)

    async def scenario() -> bool:
        completed = anyio.Event()

        async def grep_files() -> None:
            await GrepTool().run({"pattern": "needle", "literal": True}, ToolContext(cwd=tmp_path))
            completed.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(grep_files)
            assert await anyio.to_thread.run_sync(started.wait, 1)
            await anyio.sleep(0)
            responsive = not completed.is_set()
            release.set()
        return responsive

    assert anyio.run(scenario) is True


def test_grep_tool_python_fallback_abandons_worker_wait_on_cancel(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("needle\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def blocking_grep(**_kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return ToolResult(text="No matches")

    monkeypatch.setattr(search_tools_module, "_python_grep", blocking_grep)

    async def scenario() -> None:
        async def grep_files() -> None:
            await GrepTool().run({"pattern": "needle", "literal": True}, ToolContext(cwd=tmp_path))

        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(grep_files)
                assert await anyio.to_thread.run_sync(started.wait, 1)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)


def test_grep_tool_python_fallback_does_not_truncate_exact_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("match\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "literal": True, "max_results": 1},
        context,
    )

    assert result.text == "data.txt:1:match"
    assert result.truncated is False


def test_grep_tool_python_fallback_truncates_when_another_match_exists(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("match\nmatch\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "path": ".", "literal": True, "max_results": 1},
        context,
    )

    assert result.text == "data.txt:1:match\n[truncated]"
    assert result.truncated is True


def test_grep_tool_python_fallback_skips_hidden_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / ".env").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("secret\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(GrepTool(), {"pattern": "secret", "path": ".", "literal": True}, context)

    assert result.text == "visible.txt:1:secret"
    assert result.data["matches"] == ["visible.txt:1:secret"]


def test_find_tool_python_fallback_filters_glob(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "two.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "pkg/one.py"
    assert result.data["files"] == ["pkg/one.py"]


def test_find_tool_python_fallback_skips_hidden_entries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / ".hidden.py").write_text("", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.py").write_text("", encoding="utf-8")
    (tmp_path / "visible.py").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "visible.py"
    assert result.data["files"] == ["visible.py"]


def test_find_tool_ripgrep_handles_option_like_paths(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    dash_dir = tmp_path / "-dash"
    dash_dir.mkdir()
    (dash_dir / "tool.py").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": "-dash", "pattern": "*.py"}, context)

    assert result.text == "-dash/tool.py"


def test_find_tool_ripgrep_returns_no_files_for_empty_directory(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    context = ToolContext(cwd=tmp_path)

    result = run_tool(FindTool(), {"path": "empty", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


def test_find_tool_ripgrep_ignores_config_that_follows_symlinks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    link = workspace / "outside.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = tmp_path / "ripgreprc"
    config.write_text("--follow\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    context = ToolContext(cwd=workspace)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


def test_find_tool_skips_symlinked_files_when_opted_out(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    link = workspace / "outside.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py"}, context)

    assert result.text == "No files found"
    assert result.data == {"count": 0, "files": []}


@pytest.mark.skip(reason="ripgrep backend removed for descriptor-safe traversal")
def test_find_tool_ripgrep_bounds_stdout_before_buffering(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, int | None, int | None]] = []

    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        process_supervisor: object,
        max_stdout_lines: int,
        stdout_line_filter: object = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
        assert isinstance(process_supervisor, process_manager_module.ProcessSupervisor)
        assert callable(stdout_line_filter)
        calls.append(
            (command, max_stdout_lines, max_buffered_stderr_bytes, max_buffered_stderr_lines)
        )
        selected = [line for line in ["a.py", "b.txt", "c.py", "d.py"] if stdout_line_filter(line)]
        return search_tools_module.ProcessResult(
            exit_code=-9,
            stdout="\n".join(selected[:max_stdout_lines]) + "\n",
            stderr="",
            stdout_truncated=True,
        )

    monkeypatch.setattr(search_tools_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(search_tools_module, "_run_exec_limited_stdout", fake_run)
    # This test asserts the exact rg argv for stdout bounding; opt out of the
    # protected-path default so its --glob exclusions don't clutter the assertion.
    context = ToolContext(cwd=tmp_path, protected_paths=())

    result = run_tool(FindTool(), {"path": ".", "pattern": "*.py", "max_results": 2}, context)

    assert calls == [(["rg", "--no-config", "--no-follow", "--files", "--", "."], 3, 50000, 2000)]
    assert result.text == "a.py\nc.py\n[truncated]"
    assert result.truncated is True


def test_ls_tool_lists_sorted_entries_with_directory_suffix(tmp_path: Path) -> None:
    (tmp_path / "zeta.txt").write_text("", encoding="utf-8")
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".hidden").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(LsTool(), {"path": "."}, context)

    assert result.text == "alpha/\nzeta.txt"
    assert result.data["entries"] == ["alpha/", "zeta.txt"]


def test_ls_tool_truncates_large_output(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    (tmp_path / "b.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=1)

    result = run_tool(LsTool(), {"path": "."}, context)

    assert result.text == "a.txt\n[truncated]"
    assert result.truncated is True


def test_grep_tool_python_fallback_bounds_eof_context_to_requested_radius(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text("one\ntwo\nthree\nfour\nmatch\n", encoding="utf-8")

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "literal": True, "context": 2},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "data.txt-3-three\ndata.txt-4-four\ndata.txt:5:match"


def test_grep_tool_python_fallback_discards_matches_from_invalid_utf8_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_bytes(b"match\nmatch\n\xff")

    result = run_tool(
        GrepTool(),
        {"pattern": "match", "literal": True, "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "No matches"
    assert result.data == {"count": 0, "matches": []}


def test_grep_tool_python_fallback_preserves_splitlines_boundaries(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "data.txt").write_text(
        "first\vvertical\fform\x1cfile\x1dgroup\x1erecord\x85next\u2028line\u2029paragraph",
        encoding="utf-8",
    )

    result = run_tool(
        GrepTool(),
        {"pattern": "^(vertical|form|file|group|record|next|line|paragraph)$"},
        ToolContext(cwd=tmp_path),
    )

    assert result.text.splitlines() == [
        "data.txt:2:vertical",
        "data.txt:3:form",
        "data.txt:4:file",
        "data.txt:5:group",
        "data.txt:6:record",
        "data.txt:7:next",
        "data.txt:8:line",
        "data.txt:9:paragraph",
    ]


def test_python_grep_splitlines_preserves_crlf_across_chunks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "data.txt"
    path.write_bytes(b"one\r\ntwo\rthree")
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", 4)

    assert list(search_tools_module._iter_utf8_splitlines(path)) == ["one", "two", "three"]


def test_python_grep_splitlines_scans_long_lines_once_per_chunk(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "data.txt"
    path.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(search_tools_module, "_PYTHON_GREP_CHUNK_BYTES", 8)
    scanned_chars = 0
    original = search_tools_module._yield_splitline_chunk

    def tracking_chunk(
        text: str,
        line_parts: list[str],
        *,
        final: bool = False,
    ) -> object:
        nonlocal scanned_chars
        scanned_chars += len(text)
        return original(text, line_parts, final=final)

    monkeypatch.setattr(search_tools_module, "_yield_splitline_chunk", tracking_chunk)

    assert list(search_tools_module._iter_utf8_splitlines(path)) == ["x" * 100]
    assert scanned_chars == 100


def test_find_tool_python_fallback_bounds_retained_matches(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    visited: list[str] = []

    def files(_path: Path, _context: ToolContext) -> Iterable[Path]:
        for name in ("skip.txt", "c.py", "secret.key", "b.py", "a.py"):
            visited.append(name)
            yield tmp_path / name

    monkeypatch.setattr(search_tools_module, "_iter_files", files)
    context = ToolContext(cwd=tmp_path, protected_paths=("*.key",))

    result = run_tool(
        FindTool(),
        {"path": ".", "pattern": "*.py", "max_results": 1},
        context,
    )

    assert result.text == "a.py\n[truncated]"
    assert result.data == {"count": 2, "files": ["a.py"]}
    assert result.truncated is True
    assert visited == ["skip.txt", "c.py", "secret.key", "b.py", "a.py"]


def test_find_tool_python_fallback_preserves_global_sorted_prefix(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "first.py").write_text("", encoding="utf-8")
    (tmp_path / "y.py").write_text("", encoding="utf-8")
    (tmp_path / "z.py").write_text("", encoding="utf-8")

    result = run_tool(
        FindTool(),
        {"path": ".", "pattern": "*.py", "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "a/first.py\n[truncated]"
    assert result.data == {"count": 2, "files": ["a/first.py"]}


def test_find_tool_python_fallback_skips_file_symlinks(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("", encoding="utf-8")
    (sub / "c.py").write_text("", encoding="utf-8")
    try:
        (sub / "z.py").symlink_to(tmp_path / "a.py")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = run_tool(
        FindTool(),
        {"path": "sub", "pattern": "*.py", "max_results": 1},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == "sub/b.py\n[truncated]"
    assert result.data == {"count": 2, "files": ["sub/b.py"]}


def test_find_tool_python_fallback_runs_off_event_loop(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    started = threading.Event()
    release = threading.Event()
    original = search_tools_module._python_find

    def blocking_find(**kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search_tools_module, "_python_find", blocking_find)

    async def scenario() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(FindTool().run, {}, ToolContext(cwd=tmp_path))
            assert await anyio.to_thread.run_sync(started.wait, 1)
            with anyio.fail_after(0.5):
                await anyio.sleep(0)
            release.set()

    anyio.run(scenario)


def test_find_tool_python_fallback_abandons_worker_wait_on_cancel(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    started = threading.Event()
    release = threading.Event()

    def blocking_find(**_kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return ToolResult(text="No files found")

    monkeypatch.setattr(search_tools_module, "_python_find", blocking_find)

    async def scenario() -> None:
        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(FindTool().run, {}, ToolContext(cwd=tmp_path))
                assert await anyio.to_thread.run_sync(started.wait, 1)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)


def test_ls_tool_bounds_retained_entries_and_reports_exact_count(tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"entry-{index:02}.txt").write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(LsTool(), {"path": "."}, context)

    assert result.text == "entry-00.txt\nentry-01.txt\n[truncated]"
    assert result.data == {
        "path": ".",
        "entries": ["entry-00.txt", "entry-01.txt"],
        "entry_count": 20,
    }
    assert result.truncated is True


def test_ls_tool_preserves_case_insensitive_order_and_hidden_option(tmp_path: Path) -> None:
    for name in ("a", "C", "b", ".hidden"):
        (tmp_path / name).write_text("", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    visible = run_tool(LsTool(), {"path": "."}, context)
    all_entries = run_tool(LsTool(), {"path": ".", "all": True}, context)

    assert visible.data["entries"] == ["a", "b", "C"]
    assert visible.data["entry_count"] == 3
    assert all_entries.data["entries"] == [".hidden", "a", "b", "C"]
    assert all_entries.data["entry_count"] == 4


def test_ls_tool_runs_off_event_loop_and_abandons_worker_wait_on_cancel(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_ls(**_kwargs: object) -> ToolResult:
        started.set()
        release.wait(timeout=2)
        return ToolResult(text="", data={"entries": [], "entry_count": 0})

    monkeypatch.setattr(search_tools_module, "_python_ls", blocking_ls)

    async def scenario() -> None:
        with anyio.fail_after(0.5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(LsTool().run, {}, ToolContext(cwd=tmp_path))
                assert await anyio.to_thread.run_sync(started.wait, 1)
                await anyio.sleep(0)
                task_group.cancel_scope.cancel()
        release.set()

    anyio.run(scenario)
