from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.tools import process as process_tools_module
from wisp.tools import search as search_tools_module
from wisp.tools.builtin import BashTool, EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult


def run_tool(tool: object, arguments: dict[str, object], context: ToolContext) -> ToolResult:
    async def run() -> ToolResult:
        result = await tool.run(arguments, context)  # type: ignore[attr-defined]
        assert isinstance(result, ToolResult)
        return result

    return anyio.run(run)


def test_read_tool_supports_offset_limit_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(ReadTool(), {"path": "notes.txt", "offset": 2, "limit": 3}, context)

    assert result.text == "two\nthree\n[truncated]"
    assert result.truncated is True
    assert result.data["line_count"] == 4


def test_read_tool_streams_requested_slice_with_line_count(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    path.write_text("".join(f"line {index}\n" for index in range(10_000)), encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    result = run_tool(ReadTool(), {"path": "large.log", "offset": 5000, "limit": 2}, context)

    assert result.text == "line 4999\nline 5000\n"
    assert result.data["line_count"] == 10_000


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


def test_write_tool_preserves_exact_newline_bytes(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path)

    result = run_tool(WriteTool(), {"path": "mixed.txt", "content": "one\ntwo\r\n"}, context)

    assert result.data["bytes"] == len(b"one\ntwo\r\n")
    assert (tmp_path / "mixed.txt").read_bytes() == b"one\ntwo\r\n"


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
    assert "out" in result.text
    assert "err" in result.text


def test_bash_tool_retruncates_combined_stdout_and_stderr(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, max_output_bytes=40, max_output_lines=100)
    python = shlex.quote(sys.executable)
    code = "import sys; sys.stdout.write('o' * 100); sys.stderr.write('e' * 100)"
    command = f"{python} -c {shlex.quote(code)}"

    result = run_tool(BashTool(), {"command": command}, context)

    assert len(result.text.encode("utf-8")) <= context.max_output_bytes
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


def test_bash_tool_uses_taskkill_for_windows_process_tree_cleanup(
    monkeypatch: MonkeyPatch,
) -> None:
    class DummyProcess:
        returncode = None
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

    process_tools_module._kill_process_tree(process)  # noqa: SLF001

    assert calls == [["taskkill", "/F", "/T", "/PID", "123"]]
    assert process.killed is False


def test_exec_helper_bounds_stderr_before_buffering(tmp_path: Path) -> None:
    async def run() -> process_tools_module.ProcessResult:
        return await process_tools_module._run_exec_limited_stdout(  # noqa: SLF001
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('e' * 10000)",
            ],
            cwd=tmp_path,
            max_stdout_lines=1,
            max_buffered_stderr_bytes=20,
            max_buffered_stderr_lines=100,
        )

    result = anyio.run(run)

    assert len(result.stderr.encode("utf-8")) <= 20
    assert result.stderr_truncated is True


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


def test_grep_tool_ripgrep_bounds_stdout_before_buffering(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, int | None, int | None, int | None, int | None]] = []

    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        max_stdout_lines: int,
        stdout_count_filter: object = None,
        max_buffered_stdout_bytes: int | None = None,
        max_buffered_stdout_lines: int | None = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
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

    assert result.text == "data.txt:1:[Omitted long matching line]"
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


def test_grep_tool_ripgrep_drops_context_for_omitted_merged_match(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        max_stdout_lines: int,
        stdout_count_filter: object = None,
        max_buffered_stdout_bytes: int | None = None,
        max_buffered_stdout_lines: int | None = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
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


def test_grep_tool_ripgrep_follows_symlinked_files_when_opted_out(tmp_path: Path) -> None:
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

    assert result.text == "link.txt:1:secret"
    assert result.data["matches"] == ["link.txt:1:secret"]


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


def test_grep_tool_python_fallback_allows_symlinked_files_when_opted_out(
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

    assert result.text == f"{link}:1:secret"


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


def test_find_tool_ripgrep_follows_symlinked_files_when_opted_out(tmp_path: Path) -> None:
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

    assert result.text == str(link)
    assert result.data == {"count": 1, "files": [str(link)]}


def test_find_tool_ripgrep_bounds_stdout_before_buffering(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, int | None, int | None]] = []

    async def fake_run(
        command: list[str],
        *,
        cwd: Path,
        max_stdout_lines: int,
        stdout_line_filter: object = None,
        max_buffered_stderr_bytes: int | None = None,
        max_buffered_stderr_lines: int | None = None,
    ) -> search_tools_module.ProcessResult:
        assert cwd == tmp_path
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
