"""Edit-only automatic approval is a call-level grant, never a tool-wide grant."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.coding.tool_progress import LiveEventSink
from wisp.events import ToolApprovalRequested, ToolApprovalResolved, ToolExecutionEnded, WispEvent
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext, WriteConstraint
from wisp.tools.files.operations import EditTool, ReadTool, WriteTool
from wisp.tools.policy import ToolPolicy
from wisp.tools.seen_files import SeenFiles


def _executor(
    context: ToolContext,
    *,
    tool: Tool | None = None,
    approval: ToolApprovalPolicy | None = None,
    policy: ToolPolicy | None = None,
    progress_sink: LiveEventSink | None = None,
) -> ConfiguredToolExecutor:
    registry = ToolRegistry()
    registry.register(tool or WriteTool())
    return ConfiguredToolExecutor(
        registry=registry,
        context=context,
        policy=policy or ToolPolicy.allow_all_tools(),
        approval_policy=approval or ToolApprovalPolicy.auto_edits(),
        progress_sink=progress_sink,
    )


def _execute(executor: ConfiguredToolExecutor, call: ToolCall) -> list[WispEvent]:
    async def run() -> list[WispEvent]:
        return [event async for event in executor.execute(call)]

    return anyio.run(run)


def _write(path: str = "file.txt", *, call_id: str = "call") -> ToolCall:
    return ToolCall(call_id=call_id, name="write", arguments={"path": path, "content": "new"})


def test_auto_create_has_resolution_without_request_and_no_blanket_grant(tmp_path: Path) -> None:
    policy = ToolApprovalPolicy.auto_edits()
    events = _execute(_executor(ToolContext(cwd=tmp_path), approval=policy), _write())
    assert [type(event) for event in events] == [ToolApprovalResolved, ToolExecutionEnded]
    decision, result = events
    assert isinstance(decision, ToolApprovalResolved)
    assert decision.approved and decision.source == "auto"
    assert decision.rule_id == "workspace_edit_v1"
    assert isinstance(result, ToolExecutionEnded) and not result.is_error
    assert (tmp_path / "file.txt").read_text() == "new"
    assert not policy.approves(WriteTool())
    assert not policy.approved_tools and not policy.approved_safety


def test_ask_never_automatically_approves_a_builtin_edit(tmp_path: Path) -> None:
    executor = _executor(ToolContext(cwd=tmp_path), approval=ToolApprovalPolicy.require_approval())
    events = _execute(executor, _write())
    assert isinstance(events[0], ToolApprovalRequested)
    assert not any(
        isinstance(event, ToolApprovalResolved) and event.source == "auto" for event in events
    )
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error
    assert not (tmp_path / "file.txt").exists()


def test_mandatory_confirmation_on_an_exact_builtin_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = WriteTool()
    monkeypatch.setattr(tool, "confirm_every_call", True, raising=False)
    events = _execute(_executor(ToolContext(cwd=tmp_path), tool=tool), _write())
    assert isinstance(events[0], ToolApprovalRequested) and not events[0].grants_allowed
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error
    assert not (tmp_path / "file.txt").exists()


@pytest.mark.parametrize("path", ["", None, 42])
def test_malformed_destinations_cannot_receive_automatic_grants(
    tmp_path: Path, path: object
) -> None:
    events = _execute(
        _executor(ToolContext(cwd=tmp_path)),
        ToolCall(
            call_id="bad",
            name="write",
            arguments={"path": path, "content": "new"},
        ),
    )
    assert isinstance(events[0], ToolApprovalRequested)
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error


def test_a_control_directory_cannot_be_declared_as_the_scratch_root(tmp_path: Path) -> None:
    scratch = (Path.home() / ".wisp").resolve()
    scratch.mkdir(exist_ok=True)
    context = ToolContext(cwd=tmp_path, scratch_root=scratch)
    target = scratch / "not-session-scratch.txt"
    events = _execute(_executor(context), _write(str(target)))
    assert isinstance(events[0], ToolApprovalRequested)
    assert not target.exists()


def test_auto_edit_still_requires_a_current_read(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old")
    context = ToolContext(cwd=tmp_path, seen_files=SeenFiles())
    executor = _executor(context, tool=EditTool())
    call = ToolCall(
        call_id="edit",
        name="edit",
        arguments={"path": "file.txt", "edits": [{"oldText": "old", "newText": "new"}]},
    )
    events = _execute(executor, call)
    assert isinstance(events[-1], ToolExecutionEnded)
    assert events[-1].failure_code == "stale_input"
    assert target.read_text() == "old"

    anyio.run(ReadTool().run, {"path": "file.txt"}, context)
    target.write_text("old with user changes")
    stale = _execute(executor, call)[-1]
    assert isinstance(stale, ToolExecutionEnded) and stale.failure_code == "stale_input"
    anyio.run(ReadTool().run, {"path": "file.txt"}, context)
    events = _execute(executor, call)
    assert isinstance(events[-1], ToolExecutionEnded) and not events[-1].is_error
    assert target.read_text() == "new with user changes"


@pytest.mark.parametrize(
    "path", ["AGENTS.md", "nested/CLAUDE.md", ".git/config", ".agents/task.md"]
)
def test_control_files_need_human_approval(tmp_path: Path, path: str) -> None:
    events = _execute(_executor(ToolContext(cwd=tmp_path)), _write(path))
    assert isinstance(events[0], ToolApprovalRequested)
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error
    assert not (tmp_path / path).exists()


@pytest.mark.parametrize("path", [".env", ".wisp/settings.json", "../outside.txt"])
def test_auto_does_not_prompt_or_mutate_rejected_paths(tmp_path: Path, path: str) -> None:
    events = _execute(_executor(ToolContext(cwd=tmp_path)), _write(path))
    assert not any(isinstance(event, ToolApprovalRequested) for event in events)
    assert isinstance(events[0], ToolApprovalResolved) and not events[0].approved
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error
    assert not (tmp_path / path).exists()


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "link").symlink_to(tmp_path, target_is_directory=True)
    events = _execute(_executor(ToolContext(cwd=workspace)), _write("link/outside.txt"))
    assert isinstance(events[0], ToolApprovalResolved) and not events[0].approved
    assert not (tmp_path / "outside.txt").exists()


def test_embedder_outside_access_does_not_widen_auto(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ToolContext(cwd=workspace, allow_outside_cwd=True)
    events = _execute(_executor(context), _write(str(tmp_path / "outside.txt")))
    assert isinstance(events[0], ToolApprovalRequested)
    assert not (tmp_path / "outside.txt").exists()


def test_broad_root_auto_only_allows_session_scratch() -> None:
    scratch = Path.home() / ".wisp" / "sessions" / "scratch" / "session"
    scratch.mkdir(parents=True)
    scratch = scratch.resolve()
    context = ToolContext(cwd=Path.home(), scratch_root=scratch)
    events = _execute(_executor(context), _write("unattended.txt"))
    assert isinstance(events[0], ToolApprovalRequested)
    assert not (Path.home() / "unattended.txt").exists()
    events = _execute(_executor(context), _write(str(scratch / "notes.txt")))
    assert isinstance(events[0], ToolApprovalResolved) and events[0].approved
    assert (scratch / "notes.txt").read_text() == "new"


def test_auto_preserves_write_constraint(tmp_path: Path) -> None:
    context = ToolContext(cwd=tmp_path, write_constraint=WriteConstraint(allowed_paths=()))
    events = _execute(_executor(context), _write())
    assert isinstance(events[0], ToolApprovalResolved) and not events[0].approved
    assert not (tmp_path / "file.txt").exists()


class _CustomWrite(WriteTool):
    """Even a subclass with the same name and safety cannot opt itself into Auto."""


class _ConfirmationWrite(WriteTool):
    confirm_every_call = True


@pytest.mark.parametrize("tool", [_CustomWrite(), _ConfirmationWrite()])
def test_replaced_and_confirmation_tools_do_not_qualify(tmp_path: Path, tool: Tool) -> None:
    events = _execute(_executor(ToolContext(cwd=tmp_path), tool=tool), _write())
    assert isinstance(events[0], ToolApprovalRequested)
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error
    assert not (tmp_path / "file.txt").exists()


def test_auto_does_not_bypass_plan_tool_policy(tmp_path: Path) -> None:
    executor = _executor(ToolContext(cwd=tmp_path), policy=ToolPolicy.allow_read_tools())
    events = _execute(executor, _write())
    assert len(events) == 1
    assert isinstance(events[0], ToolExecutionEnded) and events[0].failure_code == "policy_denied"
    assert not (tmp_path / "file.txt").exists()


def test_auto_subagents_remain_read_only(tmp_path: Path) -> None:
    executor = _executor(ToolContext(cwd=tmp_path))
    result = anyio.run(executor.run_nested, _write())
    assert result.is_error and "read-only" in result.text
    assert not (tmp_path / "file.txt").exists()


def test_codemode_auto_works_without_a_human_approval_channel(tmp_path: Path) -> None:
    executor = _executor(ToolContext(cwd=tmp_path))
    result = anyio.run(executor.run_codemode, _write())
    assert not result.is_error
    assert (tmp_path / "file.txt").read_text() == "new"


@pytest.mark.parametrize("cancel", [False, True])
def test_auto_child_cannot_run_after_failed_or_cancelled_audit_delivery(
    tmp_path: Path, cancel: bool
) -> None:
    async def run() -> None:
        with anyio.CancelScope() as scope:

            async def live(event: WispEvent) -> None:
                assert isinstance(event, ToolApprovalResolved) and event.source == "auto"
                if cancel:
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
                raise RuntimeError("audit channel unavailable")

            executor = _executor(ToolContext(cwd=tmp_path), progress_sink=live)
            if cancel:
                await executor.run_codemode(_write())
                pytest.fail("cancelled delivery must not return a successful child")
            else:
                with pytest.raises(RuntimeError, match="audit channel unavailable"):
                    await executor.run_codemode(_write())

    anyio.run(run)
    assert not (tmp_path / "file.txt").exists()


def test_auto_is_revoked_while_preflight_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wisp.tools.auto_edits as auto_edits

    policy = ToolApprovalPolicy.auto_edits()

    def change_mode(tool: Tool, arguments: object, context: ToolContext) -> bool:
        policy.mode = "ask"
        return True

    monkeypatch.setattr(auto_edits, "eligible_workspace_edit", change_mode)
    events = _execute(_executor(ToolContext(cwd=tmp_path), approval=policy), _write())
    assert isinstance(events[0], ToolApprovalRequested)
    assert not (tmp_path / "file.txt").exists()


def test_auto_prepared_call_has_no_effect_before_execution(tmp_path: Path) -> None:
    from wisp.agent.tool_contracts import PreparedToolExecution

    executor = _executor(ToolContext(cwd=tmp_path))

    async def run() -> None:
        prepared = [event async for event in executor.prepare(_write())]
        assert isinstance(prepared[0], ToolApprovalResolved)
        assert isinstance(prepared[-1], PreparedToolExecution)
        assert not (tmp_path / "file.txt").exists()
        with anyio.CancelScope() as scope:
            scope.cancel()
            await anyio.lowlevel.checkpoint()
            await prepared[-1].run()
        assert not (tmp_path / "file.txt").exists()

    anyio.run(run)


def test_full_session_auto_audit_survives_reload(tmp_path: Path) -> None:
    from tests.agent.loop.support import tool_call_stream
    from wisp.coding.session import CodingSession
    from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted
    from wisp.providers.fake import ScriptedProvider
    from wisp.sessions.jsonl import JsonlSessionStore

    registry = ToolRegistry()
    registry.register(WriteTool())
    store = JsonlSessionStore(tmp_path / "sessions")
    session = CodingSession(
        provider=ScriptedProvider(
            [
                tool_call_stream(_write()),
                [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")],
            ]
        ),
        sessions=store,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.auto_edits(),
    )

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("write a file")]

    events = anyio.run(run)
    assert not any(isinstance(event, ToolApprovalRequested) for event in events)
    assert any(
        isinstance(event, ToolApprovalResolved) and event.source == "auto" for event in events
    )
    persisted = store.latest()
    assert persisted is not None
    snapshot = next(
        message for message in persisted.read_message_page().messages if message.role == "tool"
    )
    assert snapshot.auto_approved
    assert (tmp_path / "file.txt").read_text() == "new"


def test_shell_does_not_qualify_for_auto(tmp_path: Path) -> None:
    from wisp.tools.shell.tool import BashTool

    executor = _executor(ToolContext(cwd=tmp_path), tool=BashTool())
    events = _execute(
        executor, ToolCall(call_id="shell", name="bash", arguments={"command": "echo unexpected"})
    )
    assert isinstance(events[0], ToolApprovalRequested)
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error


def test_hardlinked_file_needs_confirmation(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("unchanged")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "alias.txt").hardlink_to(target)
    events = _execute(_executor(ToolContext(cwd=workspace)), _write("alias.txt"))
    assert isinstance(events[0], ToolApprovalRequested)
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("edit", [False, True])
def test_hardlink_created_after_auto_preflight_cannot_mutate_shared_inode(
    tmp_path: Path, edit: bool
) -> None:
    from wisp.agent.tool_contracts import PreparedToolExecution

    target = tmp_path / "file.txt"
    target.write_text("unchanged")
    executor = _executor(ToolContext(cwd=tmp_path), tool=EditTool() if edit else WriteTool())
    call = (
        ToolCall(
            call_id="edit",
            name="edit",
            arguments={"path": "file.txt", "edits": [{"oldText": "unchanged", "newText": "new"}]},
        )
        if edit
        else _write()
    )

    async def run() -> None:
        prepared = [event async for event in executor.prepare(call)]
        assert isinstance(prepared[0], ToolApprovalResolved)
        assert isinstance(prepared[-1], PreparedToolExecution)
        (tmp_path / "alias.txt").hardlink_to(target)
        result = await prepared[-1].run()
        assert result.is_error

    anyio.run(run)
    assert target.read_text() == "unchanged"
    assert (tmp_path / "alias.txt").read_text() == "unchanged"


def test_parallel_calls_do_not_grant_sensitive_edits(tmp_path: Path) -> None:
    executor = _executor(ToolContext(cwd=tmp_path))

    async def run() -> None:
        async def execute(call: ToolCall, approved: bool) -> None:
            events = [event async for event in executor.execute(call)]
            result = events[-1]
            assert isinstance(result, ToolExecutionEnded) and result.is_error is not approved

        async with anyio.create_task_group() as group:
            group.start_soon(execute, _write("a.txt", call_id="a"), True)
            group.start_soon(execute, _write("b.txt", call_id="b"), True)
            group.start_soon(execute, _write("AGENTS.md", call_id="sensitive"), False)

    anyio.run(run)
    assert not (tmp_path / "AGENTS.md").exists()
