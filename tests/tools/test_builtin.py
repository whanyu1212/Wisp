from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from tests.tools.support import run_tool
from wisp.tools.builtin import (
    BashTool,
    EditTool,
    FindTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
)
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult


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
        with pytest.raises(ToolError, match="outside the tool working directory") as raised:
            run_tool(tool, arguments, context)
        assert raised.value.failure_code == "path_outside_workspace"
        assert raised.value.retryable is False
        assert raised.value.recovery_hint == "Use a path inside the session working directory."


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


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ReadTool(), {"path": "notes.txt", "offset": "first"}),
        (GrepTool(), {"pattern": "text", "max_results": 0}),
        (
            EditTool(),
            {"path": "notes.txt", "edits": [{"oldText": "", "newText": "text"}]},
        ),
        (BashTool(), {"command": "pwd", "timeout": 0}),
    ],
)
def test_builtin_argument_validation_is_actionable(
    tmp_path: Path,
    tool: object,
    arguments: dict[str, object],
) -> None:
    (tmp_path / "notes.txt").write_text("text\n", encoding="utf-8")

    with pytest.raises(ToolError) as caught:
        run_tool(tool, arguments, ToolContext(cwd=tmp_path))

    assert caught.value.failure_code == "invalid_arguments"
    assert caught.value.retryable is True
    assert caught.value.recovery_hint == (
        "Retry with arguments that match the tool's input schema."
    )


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
