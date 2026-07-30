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
from wisp.tools import shell as shell_tools_module
from wisp.tools.builtin import BashTool, EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text_tail


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


def test_read_tool_supports_offset_limit_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path, max_output_bytes=100, max_output_lines=2)

    result = run_tool(ReadTool(), {"path": "notes.txt", "offset": 2, "limit": 3}, context)

    assert result.text == "two\nthree\n[truncated]"
    assert result.truncated is True
    assert result.data["line_count"] == 4


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
    assert summary_of("read", paged) == "read 1 line of 3 from notes.txt"

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


def test_bash_tool_preserves_exit_code_outside_tiny_body_budget(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_run_shell(*args: object, **kwargs: object) -> process_tools_module.ProcessResult:
        return process_tools_module.ProcessResult(exit_code=7, stdout="", stderr="")

    monkeypatch.setattr(shell_tools_module, "_run_shell", fake_run_shell)
    context = ToolContext(cwd=tmp_path, max_output_bytes=1, max_output_lines=0)

    result = run_tool(BashTool(), {"command": "ignored"}, context)

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

    status_overhead = len(b"Command exited with code -9: ")
    assert len(result.text.encode("utf-8")) <= context.max_output_bytes + status_overhead
    assert result.text.startswith("Command exited with code -9:")
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

    result = run_tool(BashTool(), {"command": "ignored"}, context)

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


def test_bash_tool_assigns_windows_job_before_resuming_shell(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyProcess:
        returncode = None
        pid = 123
        _handle = 456
        _thread = 654

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
        ("resume", 654),
    ]


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
            self.ResumeThread = FakeFunction(1)
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
    assert kernel32.ResumeThread.restype is process_tools_module.wintypes.DWORD
    assert kernel32.ResumeThread.argtypes == [process_tools_module.wintypes.HANDLE]
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
        stdout_line_filter: object = None,
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
        stdout_line_filter: object = None,
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
