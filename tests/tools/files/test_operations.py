from __future__ import annotations

import errno
import threading
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.builtin import (
    EditTool,
    ReadTool,
    WriteTool,
)
from wisp.tools.context import ToolContext
from wisp.tools.files import operations as file_ops_module
from wisp.tools.result import ToolError


def test_read_tool_supports_offset_limit_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(ReadTool(), {"path": "notes.txt", "offset": 2, "limit": 3}, context)

    assert result.text == "two\n[truncated] Use offset=3 to continue."
    assert result.truncated is True
    assert "line_count" not in result.data
    assert result.data["selected_count"] == 1


def test_read_tool_stops_after_requested_slice(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    path.write_text("".join(f"line {index}\n" for index in range(10_000)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(ReadTool(), {"path": "large.log", "offset": 5000, "limit": 2}, context)

    assert result.text == "line 4999\nline 5000\n[More lines follow. Use offset=5002 to continue.]"
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


def test_read_exact_fit_has_no_notice(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=3)

    result = run_tool(ReadTool(), {"path": "notes.txt"}, context)

    assert result.text == "one\ntwo\nthree\n"
    assert result.truncated is False


def test_read_small_budget_keeps_notice(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("x" * 1000 + "\nafter\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=100)

    result = run_tool(ReadTool(), {"path": "min.js"}, context)

    assert result.text == "x" * 36 + "\n[truncated] Line 1 is too long; use offset=2 to skip it."


def test_read_offset_past_eof(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\n", encoding="utf-8")

    with pytest.raises(ToolError, match=r"beyond the end of notes.txt \(2 lines\)"):
        run_tool(ReadTool(), {"path": "notes.txt", "offset": 3}, ToolContext(cwd=tmp_path))


def test_read_oversized_line_hint(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("short\n" + "x" * 5000 + "\nafter\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=1000, max_output_lines=100)

    first = run_tool(ReadTool(), {"path": "min.js"}, context)
    second = run_tool(ReadTool(), {"path": "min.js", "offset": 2}, context)

    assert first.text == "short\n[truncated] Output limit reached. Use offset=2 to continue."
    assert second.text.endswith(
        "\n[truncated] Line 2 is longer than the output limit. Use offset=3 to skip it, "
        "or read it in parts with bash, for example: sed -n '2p' min.js | cut -c1-738"
    )
    assert second.data["selected_count"] == 0
    assert len(second.text.encode("utf-8")) <= context.max_output_bytes


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
    from wisp.tools.files.operations import _WRITE_SNAPSHOT_MAX_CHARS

    context = ToolContext(cwd=tmp_path)
    (tmp_path / "big.txt").write_text("x" * (_WRITE_SNAPSHOT_MAX_CHARS + 1), encoding="utf-8")

    result = run_tool(WriteTool(), {"path": "big.txt", "content": "small\n"}, context)

    assert "before_text" not in result.data
    assert (tmp_path / "big.txt").read_text(encoding="utf-8") == "small\n"


def test_overlapping_edit_arguments_are_actionable(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("abc", encoding="utf-8")

    with pytest.raises(ToolError) as caught:
        run_tool(
            EditTool(),
            {
                "path": "notes.txt",
                "edits": [
                    {"oldText": "abc", "newText": "first"},
                    {"oldText": "bc", "newText": "second"},
                ],
            },
            ToolContext(cwd=tmp_path),
        )

    assert caught.value.failure_code == "invalid_arguments"
    assert caught.value.retryable is True
    assert caught.value.recovery_hint == (
        "Retry with arguments that match the tool's input schema."
    )


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

    with pytest.raises(ToolError, match=r"edits\[0\].oldText matches 2 places") as raised:
        run_tool(
            EditTool(),
            {"path": "dupes.txt", "edits": [{"oldText": "same", "newText": "once"}]},
            context,
        )
    assert raised.value.failure_code == "invalid_arguments"
    assert raised.value.retryable is True
    assert "surrounding lines" in str(raised.value.recovery_hint)


def _edit(tmp_path: Path, content: bytes, *edits: tuple[str, str]) -> bytes:
    path = tmp_path / "file.txt"
    path.write_bytes(content)
    run_tool(
        EditTool(),
        {"path": "file.txt", "edits": [{"oldText": old, "newText": new} for old, new in edits]},
        ToolContext(cwd=tmp_path),
    )
    return path.read_bytes()


def test_edit_crlf_multiline(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"a\r\nb\r\nc\r\n", ("a\nb", "x\ny"))

    assert edited == b"x\r\ny\r\nc\r\n"


def test_edit_crlf_inserted_lines(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"a\r\nb\r\n", ("b", "b\nb2"))

    assert edited == b"a\r\nb\r\nb2\r\n"


def test_edit_mixed_endings_untouched(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"a\nb\r\nc\n", ("a", "A\nA2"), ("b\nc", "B\nC"))

    assert edited == b"A\nA2\nB\r\nC\n"


def test_edit_keeps_bom(tmp_path: Path) -> None:
    edited = _edit(tmp_path, "\ufeffhello\n".encode(), ("hello", "hi"))

    assert edited == "\ufeffhi\n".encode()


def test_edit_missing_names_index(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match=r"edits\[1\].oldText was not found") as raised:
        _edit(tmp_path, b"alpha beta\n", ("alpha", "A"), ("gamma", "G"))

    assert raised.value.failure_code == "stale_input"
    assert (tmp_path / "file.txt").read_bytes() == b"alpha beta\n"


def test_edit_overlap_names_indexes(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match=r"edits\[0\] and edits\[2\] overlap"):
        _edit(tmp_path, b"abcdef\n", ("abc", "1"), ("f", "2"), ("bc", "3"))


def test_edit_ignores_trailing_whitespace(tmp_path: Path) -> None:
    edited = _edit(
        tmp_path,
        b"keep  \ndef f():  \n    return 1\t\nend  \n",
        ("def f():\n    return 1\n", "def g():\n    return 2\n"),
    )

    assert edited == b"keep  \ndef g():\n    return 2\nend  \n"


def test_edit_whitespace_match_deletes_lines(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"a\nb  \nc\n", ("b\n", ""))

    assert edited == b"a\nc\n"


def test_edit_exact_match_wins(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"x = 1   \ny = 2\n", ("x = 1", "x = 3"))

    assert edited == b"x = 3   \ny = 2\n"


def test_edit_whitespace_match_crlf(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"a \r\nb\r\nc\r\n", ("a\nb\n", "x\n"))

    assert edited == b"x\r\nc\r\n"


def test_edit_whitespace_match_keeps_crlf_region(tmp_path: Path) -> None:
    edited = _edit(tmp_path, b"header\nalpha  \r\nbeta\r\n", ("alpha\nbeta\n", "ALPHA\nbeta\n"))

    assert edited == b"header\nALPHA\r\nbeta\r\n"


def test_edit_whitespace_match_ambiguous(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match=r"edits\[0\].oldText matches 2 places"):
        _edit(tmp_path, b"pass \npass  \n", ("pass", "return"))


def test_edit_reports_indentation_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="matches line 2 only if indentation is ignored") as raised:
        _edit(tmp_path, b"def f():\n\treturn 1\n", ("    return 1", "    return 2"))

    assert raised.value.failure_code == "stale_input"
    assert (tmp_path / "file.txt").read_bytes() == b"def f():\n\treturn 1\n"


def test_edit_rejects_no_op(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="leave the file unchanged") as raised:
        _edit(tmp_path, b"same\n", ("same", "same"))

    assert raised.value.failure_code == "invalid_arguments"
