from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from wisp.cli import _print_mode_tool_registry, app
from wisp.runtime.registry import ToolRegistry
from wisp.tools.builtin import BashTool, EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool


def test_print_mode_outputs_response_and_writes_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.output == "fake response to: hello\n"

    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1

    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["message"]["role"] for record in records] == ["user", "assistant"]


def test_print_mode_exposes_only_read_only_tools() -> None:
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
    ):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry)

    assert filtered.names() == ("read", "grep", "find", "ls")


def test_print_mode_reports_unknown_provider(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--provider", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Unknown provider: missing" in result.output
