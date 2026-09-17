"""Authorization ordering and changed-request regressions for chapter 5."""

from pathlib import Path

import pytest

from examples.crafting_agents.checkpoint_02 import create_fixture
from examples.crafting_agents.core import ToolCall
from examples.crafting_agents.side_effects import ApprovalRequest, ControlledTools, ExecutionPolicy


def edit() -> ToolCall:
    return ToolCall(
        "e", "edit", {"path": "calculator.py", "old": "return a - b", "new": "return a + b"}
    )


@pytest.mark.parametrize("name", ["edit", "test"])
@pytest.mark.parametrize("policy_allows", [False, True])
def test_denial_never_dispatches_or_prompts_for_disallowed_tools(
    tmp_path: Path, name: str, policy_allows: bool
) -> None:
    create_fixture(tmp_path)
    approvals: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> bool:
        approvals.append(request)
        return False

    tools = ControlledTools(
        tmp_path, ExecutionPolicy(frozenset({name}) if policy_allows else frozenset()), approve
    )

    def forbidden(call: ToolCall) -> str:
        raise AssertionError("denied tool dispatched")

    tools.fixture.execute = forbidden  # type: ignore[method-assign]
    result = tools.execute(edit() if name == "edit" else ToolCall("t", "test", {}))
    assert ("approval_denied" if policy_allows else "policy_denied") in result
    assert len(approvals) == int(policy_allows)


def test_approval_cannot_mutate_the_dispatched_arguments(tmp_path: Path) -> None:
    create_fixture(tmp_path)
    call = edit()

    def approve(request: ApprovalRequest) -> bool:
        call.arguments["new"] = "return 999"
        assert dict(request.arguments)["new"] == "return a + b"
        return True

    tools = ControlledTools(tmp_path, ExecutionPolicy(frozenset({"edit"})), approve)
    assert "edited calculator.py" in tools.execute(call)
    assert "return a + b" in (tmp_path / "calculator.py").read_text()


@pytest.mark.parametrize(
    "name,file",
    [("edit", "calculator.py"), ("test", "calculator.py"), ("test", "test_calculator.py")],
)
def test_changed_input_requires_a_new_request(tmp_path: Path, name: str, file: str) -> None:
    create_fixture(tmp_path)

    def approve(request: ApprovalRequest) -> bool:
        (tmp_path / file).write_text("# concurrent host change\n")
        return True

    tools = ControlledTools(tmp_path, ExecutionPolicy(frozenset({name})), approve)
    assert "stale_input" in tools.execute(edit() if name == "edit" else ToolCall("t", "test", {}))
    assert (tmp_path / file).read_text() == "# concurrent host change\n"


@pytest.mark.parametrize("path", ["../calculator.py", "/calculator.py", ".env"])
def test_invalid_paths_do_not_reach_approval(tmp_path: Path, path: str) -> None:
    create_fixture(tmp_path)

    def approve(request: ApprovalRequest) -> bool:
        raise AssertionError("invalid path reached approval")

    tools = ControlledTools(tmp_path, ExecutionPolicy(frozenset({"edit"})), approve)
    call = edit()
    call.arguments["path"] = path
    assert "only calculator.py" in tools.execute(call)


@pytest.mark.parametrize("source", ["approve", "report"])
@pytest.mark.parametrize("error", [OSError("backend disconnected"), UnicodeError("bad response")])
def test_callback_failures_propagate_without_dispatch(
    tmp_path: Path, source: str, error: Exception
) -> None:
    create_fixture(tmp_path)

    def approve(request: ApprovalRequest) -> bool:
        if source == "approve":
            raise error
        return True

    def report(message: str) -> None:
        if source == "report":
            raise error

    tools = ControlledTools(tmp_path, ExecutionPolicy(frozenset({"edit"})), approve, report=report)
    with pytest.raises(type(error)) as caught:
        tools.execute(edit())
    assert caught.value is error
    assert "return a - b" in (tmp_path / "calculator.py").read_text()


def test_invalid_file_encoding_remains_a_tool_observation(tmp_path: Path) -> None:
    create_fixture(tmp_path)
    (tmp_path / "calculator.py").write_bytes(b"\xff")
    tools = ControlledTools(tmp_path, ExecutionPolicy(), lambda request: True)
    result = tools.execute(ToolCall("r", "read", {"path": "calculator.py"}))
    assert "error:" in result
    assert "decode" in result


def test_symlink_replacement_during_approval_is_rejected(tmp_path: Path) -> None:
    create_fixture(tmp_path)
    target = tmp_path / "other.py"
    target.write_text("untouched")

    def approve(request: ApprovalRequest) -> bool:
        (tmp_path / "calculator.py").unlink()
        (tmp_path / "calculator.py").symlink_to(target)
        return True

    tools = ControlledTools(tmp_path, ExecutionPolicy(frozenset({"edit"})), approve)
    assert "not symlinks" in tools.execute(edit())
    assert target.read_text() == "untouched"
