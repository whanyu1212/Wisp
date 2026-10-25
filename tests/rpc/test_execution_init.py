from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import (
    dispatch_parsed,
    enable_project_init,
)
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    RpcCommandFinished,
    RpcCommandStarted,
    ToolCallSnapshot,
    ToolExecutionEnded,
    WispEvent,
)
from wisp.rpc.coordinator import _RpcCommandCompleted
from wisp.rpc.session import run as rpc_session_run_module
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import CreateOnlyWriteReceipt


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_init_dispatches_repository_specific_create_only_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        subdirectory = project / "src"
        subdirectory.mkdir()
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        enable_project_init(fixture, project)
        fixture.agent.project_context_root = None
        fixture.agent.tool_context = ToolContext(
            cwd=subdirectory,
            protected_paths=("stale-secret",),
        )

        class UpdatingTrustResolver:
            async def resolve(self) -> bool:
                fixture.agent.tool_context = ToolContext(
                    cwd=subdirectory,
                    protected_paths=("trusted-secret",),
                )
                return True

            def resolve_request(self, **_kwargs: object) -> bool:
                return False

        fixture.trust_gate = UpdatingTrustResolver()  # type: ignore[assignment]
        prompts: list[str] = []
        instructions: list[str] = []
        operation_contexts: list[ToolContext] = []

        async def capture_run(
            prompt: str,
            **kwargs: object,
        ) -> AsyncIterator[WispEvent]:
            prompts.append(prompt)
            context = kwargs.get("tool_context")
            assert isinstance(context, ToolContext)
            operation_contexts.append(context)
            operation_instructions = kwargs.get("operation_instructions")
            assert isinstance(operation_instructions, str)
            instructions.append(operation_instructions)
            assert kwargs.get("operation_tool_names") == frozenset(
                {"read", "grep", "find", "ls", "write"}
            )
            target = context.cwd / "AGENTS.md"
            constraint = context.write_constraint
            assert constraint is not None
            assert constraint.allowed_paths == (target,)
            assert constraint.conflicting_paths == (target.with_name("AGENTS.MD"),)
            assert constraint.create_only is True
            assert constraint.non_empty is True
            receipt = constraint.receipt
            assert receipt is not None
            target.write_text("# Agent guidance\n", encoding="utf-8")
            info = target.lstat()
            receipt.record(target, (info.st_dev, info.st_ino))
            call = ToolCallSnapshot(
                call_id="write-1",
                name="write",
                arguments={
                    "path": "./AGENTS.md",
                    "content": "# Agent guidance\n",
                    "overwrite": False,
                },
            )
            yield MessageCompleted(
                turn=1,
                content="",
                finish_reason="tool_calls",
                tool_calls=(call,),
            )
            yield ToolExecutionEnded(
                call_id=call.call_id,
                name="write",
                output="Wrote 17 bytes to AGENTS.md",
                is_error=False,
                created=True,
            )

        monkeypatch.setattr(fixture.agent, "run", capture_run)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(executor, {"id": "init-1", "type": "init"}, None)
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "init"
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.command_id == "init-1"
        assert completed.ok is True
        assert prompts == ["/init"]
        assert [context.cwd for context in operation_contexts] == [project]
        assert [context.protected_paths for context in operation_contexts] == [("trusted-secret",)]
        assert len(instructions) == 1
        assert json.dumps(str(project / "AGENTS.md"), ensure_ascii=False) in instructions[0]
        assert "overwrite=false" in instructions[0]
        assert "Modify no other file" in instructions[0]

    anyio.run(scenario)


def test_init_completion_reports_conflict_without_unsafe_path_cleanup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    conflict = tmp_path / "conflict.md"
    receipt = CreateOnlyWriteReceipt()
    completion = rpc_session_run_module._ProjectInitCompletion(
        target,
        conflicting_paths=(conflict,),
        receipt=receipt,
    )
    call = ToolCallSnapshot(
        call_id="write-1",
        name="write",
        arguments={"path": "AGENTS.md", "content": "generated\n", "overwrite": False},
    )
    target.write_text("generated\n", encoding="utf-8")
    info = target.lstat()
    receipt.record(target, (info.st_dev, info.st_ino))
    completion.observe(
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(call,),
        )
    )
    completion.observe(
        ToolExecutionEnded(
            call_id=call.call_id,
            name="write",
            output="Wrote 10 bytes to AGENTS.md",
            is_error=False,
            created=True,
        )
    )
    conflict.write_text("concurrent\n", encoding="utf-8")

    assert completion.error() == (
        f"Conflicting project guidance appeared during initialization: {conflict}"
    )
    assert target.read_text(encoding="utf-8") == "generated\n"
    assert conflict.read_text(encoding="utf-8") == "concurrent\n"


def test_init_completion_rejects_replacement_before_tool_event(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    receipt = CreateOnlyWriteReceipt()
    completion = rpc_session_run_module._ProjectInitCompletion(
        target,
        conflicting_paths=(),
        receipt=receipt,
    )
    target.write_text("generated\n", encoding="utf-8")
    created_info = target.lstat()
    receipt.record(target, (created_info.st_dev, created_info.st_ino))
    replacement = tmp_path / "replacement.md"
    replacement.write_text("replacement\n", encoding="utf-8")
    replacement.replace(target)
    call = ToolCallSnapshot(
        call_id="write-1",
        name="write",
        arguments={"path": "AGENTS.md", "content": "generated\n", "overwrite": False},
    )
    completion.observe(
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="tool_calls",
            tool_calls=(call,),
        )
    )
    completion.observe(
        ToolExecutionEnded(
            call_id=call.call_id,
            name="write",
            output="Wrote 10 bytes to AGENTS.md",
            is_error=False,
            created=True,
        )
    )

    assert (
        completion.error() == f"Generated project guidance was replaced before completion: {target}"
    )
    assert target.read_text(encoding="utf-8") == "replacement\n"


def test_init_fails_when_agent_does_not_create_guidance(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        enable_project_init(fixture, project)

        async def no_op_run(
            _prompt: str,
            **_kwargs: object,
        ) -> AsyncIterator[WispEvent]:
            if False:
                yield ErrorEvent(message="unreachable")

        monkeypatch.setattr(fixture.agent, "run", no_op_run)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "init-1", "type": "init"},
                None,
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.command_type == "init"
        assert completed.ok is False
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.error == (
            "Project initialization completed without a successful create-only write to "
            f"{project / 'AGENTS.md'}"
        )

    anyio.run(scenario)


def test_init_does_not_accept_a_concurrently_created_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        enable_project_init(fixture, project)

        async def raced_run(
            _prompt: str,
            **_kwargs: object,
        ) -> AsyncIterator[WispEvent]:
            (project / "AGENTS.md").write_text("Created elsewhere.\n", encoding="utf-8")
            if False:
                yield ErrorEvent(message="unreachable")

        monkeypatch.setattr(fixture.agent, "run", raced_run)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "init-1", "type": "init"},
                None,
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.ok is False
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.error == (
            "Project initialization completed without a successful create-only write to "
            f"{project / 'AGENTS.md'}"
        )

    anyio.run(scenario)


@pytest.mark.parametrize("existing_name", ["AGENTS.md", "AGENTS.MD"])
def test_init_refuses_existing_project_guidance(
    tmp_path: Path,
    existing_name: str,
) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        existing = project / existing_name
        existing.write_text("Keep me.\n", encoding="utf-8")
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        enable_project_init(fixture, project)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "init-1", "type": "init"},
                None,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is None
        assert fixture.session_state.session is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        error = fixture.events[1]
        finished = fixture.events[2]
        assert isinstance(error, ErrorEvent)
        assert isinstance(finished, RpcCommandFinished)
        assert str(existing).casefold() in error.message.casefold()
        assert finished.command_type == "init"
        assert finished.ok is False
        assert existing.read_text(encoding="utf-8") == "Keep me.\n"

    anyio.run(scenario)


@pytest.mark.parametrize("entry_kind", ["directory", "symlink"])
def test_init_refuses_non_file_guidance_entries(tmp_path: Path, entry_kind: str) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        target = project / "AGENTS.md"
        if entry_kind == "directory":
            target.mkdir()
        else:
            target.symlink_to(project / "missing-guidance")
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        enable_project_init(fixture, project)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "init-1", "type": "init"},
                None,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is None
        error = next(event for event in fixture.events if isinstance(event, ErrorEvent))
        assert error.message == f"Project guidance already exists: {target}"
        if entry_kind == "directory":
            assert target.is_dir()
        else:
            assert target.is_symlink()

    anyio.run(scenario)


def test_init_requires_build_mode_and_write_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.agent.mode = "plan"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            plan_result = await dispatch_parsed(executor, {"id": "plan-init", "type": "init"}, None)
            fixture.agent.mode = "build"
            no_write_result = await dispatch_parsed(
                executor, {"id": "no-write-init", "type": "init"}, None
            )
            task_group.cancel_scope.cancel()

        assert plan_result.running_command is None
        assert no_write_result.running_command is None
        errors = [event.message for event in fixture.events if isinstance(event, ErrorEvent)]
        assert errors == [
            "Project initialization requires build mode. Run /build first.",
            "Project initialization requires the write tool.",
        ]

    anyio.run(scenario)
