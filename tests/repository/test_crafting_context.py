"""Contracts for chapter 3's request construction and separate host permissions."""

import asyncio
import json
from pathlib import Path

import pytest

from examples.crafting_agents.checkpoint_01 import BUGGY_SOURCE
from examples.crafting_agents.checkpoint_03 import (
    PERMISSION_CLAIM,
    PROJECT_GUIDANCE,
    TASK,
    TOOLS,
    ContextTools,
    create_context_fixture,
    run_checkpoint,
)
from examples.crafting_agents.context import (
    BOUNDARY,
    CORE,
    METADATA_CHARS,
    PROJECT_CHARS,
    TOOL_GUIDANCE_CHARS,
    TRUNCATED,
    bounded,
    build_instructions,
)
from examples.crafting_agents.core import ToolCall


def test_large_project_guidance_cannot_crowd_out_other_request_blocks(tmp_path: Path) -> None:
    create_context_fixture(tmp_path)
    normal = build_instructions(tmp_path, TOOLS, trusted=True)
    (tmp_path / "AGENTS.md").write_text("é" * 10_000, encoding="utf-8")
    large = build_instructions(tmp_path, TOOLS, trusted=True)

    assert large[0] == CORE
    assert large[-1] == BOUNDARY
    assert [block.splitlines()[0] for block in large] == [
        "[CORE]",
        "[PROJECT METADATA]",
        "[PROJECT GUIDANCE: AGENTS.md]",
        "[TOOL GUIDANCE]",
        "[AUTHORITY]",
    ]
    for index, budget in ((1, METADATA_CHARS), (2, PROJECT_CHARS), (3, TOOL_GUIDANCE_CHARS)):
        assert len(large[index].split("\n", 1)[1]) <= budget
    assert large[2].endswith(TRUNCATED)
    assert large[:2] == normal[:2]
    assert large[3:] == normal[3:]
    assert all(tool.name in large[3] for tool in TOOLS)


def test_untrusted_assembly_performs_no_project_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_context_fixture(tmp_path)

    def unexpected_inspection(*args: object, **kwargs: object) -> None:
        raise AssertionError("untrusted assembly touched the filesystem")

    for method in ("open", "is_file", "is_symlink", "stat", "iterdir", "resolve"):
        monkeypatch.setattr(Path, method, unexpected_inspection)
    blocks = build_instructions(tmp_path, TOOLS, trusted=False)

    assert "not trusted" in blocks[1]
    assert "not trusted" in blocks[2]
    assert PROJECT_GUIDANCE not in "\n".join(blocks)
    assert PERMISSION_CLAIM not in "\n".join(blocks)
    assert str(tmp_path) not in "\n".join(blocks)
    assert "discover:" in blocks[3]


def test_guidance_describes_only_exposed_tools(tmp_path: Path) -> None:
    blocks = build_instructions(tmp_path, TOOLS[:1], trusted=False)
    assert "discover:" in blocks[3]
    assert "edit:" not in blocks[3]
    assert "test:" not in blocks[3]
    empty = build_instructions(tmp_path, (), trusted=False)
    assert "No tools exposed." in empty[3]


@pytest.mark.parametrize(
    "text,limit,expected", [("short", 12, "short"), ("x" * 13, 12, "\n[truncated]")]
)
def test_character_budget_includes_marker(text: str, limit: int, expected: str) -> None:
    assert bounded(text, limit) == expected


@pytest.mark.parametrize("trusted", [True, False])
def test_invalid_budget_rejected_before_project_access(tmp_path: Path, trusted: bool) -> None:
    with pytest.raises(ValueError, match="truncation marker"):
        build_instructions(tmp_path / "absent", TOOLS, trusted=trusted, project_limit=3)


@pytest.mark.parametrize("condition", ["missing", "invalid_utf8", "symlink"])
def test_unusable_instruction_file_produces_notice_not_content(
    tmp_path: Path, condition: str
) -> None:
    create_context_fixture(tmp_path)
    path = tmp_path / "AGENTS.md"
    path.unlink()
    if condition == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif condition == "symlink":
        target = tmp_path / ".env"
        target.write_text("SYNTHETIC_PRIVATE_VALUE", encoding="utf-8")
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
    blocks = build_instructions(tmp_path, TOOLS, trusted=True)
    assert "no project guidance loaded" in blocks[2]
    assert "SYNTHETIC_PRIVATE_VALUE" not in "\n".join(blocks)


def test_discovery_is_bounded_to_fixture_files_and_reads_are_checked(tmp_path: Path) -> None:
    create_context_fixture(tmp_path)
    (tmp_path / ".env").write_text("SYNTHETIC_PRIVATE_VALUE", encoding="utf-8")
    tools = ContextTools(tmp_path, approve_edits=False)
    assert tools.execute(ToolCall("d", "discover", {})).splitlines() == [
        "README.md",
        "calculator.py",
        "test_calculator.py",
    ]
    for call in (
        ToolCall("d", "discover", {"root": ".."}),
        ToolCall("r", "read", {"path": "../README.md"}),
        ToolCall("r", "read", {"path": "AGENTS.md"}),
        ToolCall("r", "read", {"path": ".env"}),
        ToolCall("r", "read", {"path": "README.md", "approved": "true"}),
        ToolCall("r", "read", {"path": 5}),
    ):
        assert "error:" in tools.execute(call)
    (tmp_path / "README.md").write_text("é" * 10_000, encoding="utf-8")
    header, body = tools.execute(ToolCall("r", "read", {"path": "README.md"})).split("\n", 1)
    assert header == "truncated=true"
    assert len(body.encode("utf-8")) <= 2_000


@pytest.mark.process
@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("approve_edits", [True, False])
@pytest.mark.parametrize("long_guidance", [True, False])
def test_request_trace_and_permissions_across_context_scenarios(
    tmp_path: Path, trusted: bool, approve_edits: bool, long_guidance: bool
) -> None:
    create_context_fixture(tmp_path, long_guidance=long_guidance)
    trace: list[str] = []
    result = asyncio.run(
        run_checkpoint(tmp_path, trusted=trusted, approve_edits=approve_edits, report=trace.append)
    )

    first_request = json.loads(trace[1])
    assert first_request["messages"][-1]["content"] == TASK
    assert [message["role"] for message in first_request["messages"]] == ["system"] * 5 + ["user"]
    assert [tool["name"] for tool in first_request["tools"]] == [tool.name for tool in TOOLS]
    assert sum(message.role == "system" for message in result.history) == 5
    project = first_request["messages"][2]["content"]
    assert (PERMISSION_CLAIM in project) == trusted
    assert (TRUNCATED in project) == (trusted and long_guidance)
    if not trusted:
        assert PERMISSION_CLAIM not in "\n".join(trace)
    calls = [call for message in result.history for call in message.tool_calls]
    assert [call.name for call in calls[:3]] == ["discover", "read", "test"]
    assert len({call.id for call in calls}) == len(calls)
    source = (tmp_path / "calculator.py").read_text(encoding="utf-8")
    if approve_edits:
        assert "return a + b" in source
        assert "\nOK" in result.history[-2].content
    else:
        assert source == BUGGY_SOURCE
        assert "edit denied by the host" in result.history[-2].content
        assert "bug remains" in result.history[-1].content
    assert result.stop_reason == "model_finished"
