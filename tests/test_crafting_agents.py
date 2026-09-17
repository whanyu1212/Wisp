"""Observable contracts for the book's executable teaching checkpoints."""

import asyncio
from pathlib import Path

import pytest

from examples.crafting_agents.checkpoint_01 import BUGGY_SOURCE, READ, read_fixture
from examples.crafting_agents.checkpoint_01 import make_provider as read_provider
from examples.crafting_agents.checkpoint_02 import (
    TOOLS,
    FixtureTools,
    bound_output,
    create_fixture,
)
from examples.crafting_agents.checkpoint_02 import make_provider as repair_provider
from examples.crafting_agents.core import Message, ScriptedProvider, ScriptStep, ToolCall, run_agent


def test_read_failure_is_correlated_and_available_to_next_turn() -> None:
    trace: list[str] = []
    result = asyncio.run(
        run_agent("fix add", read_provider(), (READ,), read_fixture, report=trace.append)
    )

    observations = [message for message in result.history if message.role == "tool"]
    assert [message.tool_call_id for message in observations] == ["1", "2"]
    assert observations[0].content.startswith("error:")
    assert observations[1].content == BUGGY_SOURCE
    assert "no file has been changed" in result.history[-1].content
    assert result.stop_reason == "model_finished"


def test_turn_budget_retains_result_without_claiming_completion() -> None:
    result = asyncio.run(
        run_agent(
            "fix add", read_provider(), (READ,), read_fixture, max_turns=1, report=lambda _: None
        )
    )
    assert result.stop_reason == "turn_limit"
    assert [message.role for message in result.history] == ["user", "assistant", "tool"]
    assert result.history[-1].tool_call_id == "1"


def test_unexpected_executor_bug_propagates() -> None:
    def broken_executor(call: ToolCall) -> str:
        raise RuntimeError("executor bug")

    with pytest.raises(RuntimeError, match="executor bug"):
        asyncio.run(
            run_agent("fix add", read_provider(), (READ,), broken_executor, report=lambda _: None)
        )


def test_script_cannot_claim_success_after_unexpected_observation() -> None:
    provider = ScriptedProvider((ScriptStep(Message("assistant", "fixed"), after="\nOK"),))
    with pytest.raises(AssertionError, match="expected tool observation"):
        asyncio.run(provider.complete((Message("tool", "FAILED", tool_call_id="1"),), TOOLS))


@pytest.mark.process
@pytest.mark.parametrize("approved", [True, False])
def test_repair_and_denial_have_distinct_file_and_test_outcomes(
    tmp_path: Path, approved: bool
) -> None:
    create_fixture(tmp_path)
    tools = FixtureTools(tmp_path, approve_edits=approved)
    result = asyncio.run(
        run_agent(
            "fix add",
            repair_provider(approve_edits=approved),
            TOOLS,
            tools.execute,
            report=lambda _: None,
        )
    )

    observations = [message.content for message in result.history if message.role == "tool"]
    assert "exit_code=1" in observations[0]
    assert "FAILED" in observations[0]
    source = (tmp_path / "calculator.py").read_text(encoding="utf-8")
    independent_test = tools.execute(ToolCall("verify", "test", {}))
    if approved:
        assert "return a + b" in source
        assert "exit_code=0" in observations[-1]
        assert "exit_code=0" in independent_test
        assert "\nOK" in independent_test
    else:
        assert source == BUGGY_SOURCE
        assert "edit denied" in observations[-1]
        assert "bug remains" in result.history[-1].content
        assert "exit_code=1" in independent_test
    assert result.stop_reason == "model_finished"


@pytest.mark.parametrize(
    "call",
    [
        ToolCall("1", "unknown", {}),
        ToolCall("1", "read", {"path": "calculator.py", "approved": "true"}),
        ToolCall("1", "read", {"path": 123}),
        ToolCall("1", "edit", {"path": "../outside.py", "old": "old", "new": "new"}),
        ToolCall("1", "edit", {"path": "calculator.py", "old": "missing", "new": "new"}),
        ToolCall("1", "edit", {"path": "calculator.py", "old": "", "new": "new"}),
    ],
)
def test_invalid_calls_leave_source_unchanged(tmp_path: Path, call: ToolCall) -> None:
    create_fixture(tmp_path)
    result = FixtureTools(tmp_path).execute(call)
    assert "error:" in result
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == BUGGY_SOURCE


def test_ambiguous_edit_requires_reread(tmp_path: Path) -> None:
    create_fixture(tmp_path)
    result = FixtureTools(tmp_path).execute(
        ToolCall("1", "edit", {"path": "calculator.py", "old": "a", "new": "x"})
    )
    assert "reread calculator.py" in result
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == BUGGY_SOURCE


def test_symlink_edit_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text(BUGGY_SOURCE, encoding="utf-8")
    try:
        (tmp_path / "calculator.py").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = FixtureTools(tmp_path).execute(
        ToolCall(
            "1", "edit", {"path": "calculator.py", "old": "return a - b", "new": "return a + b"}
        )
    )
    assert "must not be a symlink" in result
    assert target.read_text(encoding="utf-8") == BUGGY_SOURCE


@pytest.mark.parametrize(
    "text,expected",
    [("é" * 100 + "\nx\ny\n", "é" * 4), ("a\nb\nc\n", "a\nb\n"), ("short", "short")],
)
def test_output_caps_apply_together_without_splitting_utf8(text: str, expected: str) -> None:
    result = bound_output(text, max_bytes=9, max_lines=2)
    assert result.text == expected
    assert len(result.text.encode("utf-8")) <= 9
    assert len(result.text.splitlines()) <= 2
    assert result.truncated == (text != expected)


def test_error_output_is_also_bounded(tmp_path: Path) -> None:
    result = FixtureTools(tmp_path).execute(ToolCall("1", "é" * 10_000, {}))
    header, body = result.split("\n", 1)
    assert header == "truncated=true"
    assert len(body.encode("utf-8")) <= 2_000


def test_fixture_setup_does_not_overwrite_existing_source(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("existing work", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_fixture(tmp_path)
    assert source.read_text(encoding="utf-8") == "existing work"
