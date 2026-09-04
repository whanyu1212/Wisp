from __future__ import annotations

import json
import shutil
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from tests.rpc_support import (
    RpcExecutorFixture,
    build_rpc_executor_fixture,
    preserve_running_command,
    reject_unexpected_command,
)
from wisp.agent.harness import QueuedMessages
from wisp.agent.messages import Message
from wisp.auth.openai_codex import DeviceCodeInfo
from wisp.auth.storage import ApiKeyCredential, OAuthCredential
from wisp.coding import CodingSession
from wisp.coding.session import _RetainedQueueState
from wisp.config import WispConfig
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    QueueItemsRemoved,
    QueueUpdated,
    RpcCommandFinished,
    RpcCommandsReported,
    RpcCommandStarted,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcDeviceCodeProgressReported,
    RpcDeviceCodeReported,
    RpcMcpStatusReported,
    RpcMessagesReported,
    RpcModelCatalogReported,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSessionTreeNavigated,
    RpcSessionTreeReported,
    RpcSessionTreeUnreverted,
    RpcSkillsReported,
    RpcStateReported,
    RpcStateSnapshot,
    ToolCallSnapshot,
    ToolExecutionEnded,
    WispEvent,
)
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.providers.base import ToolSpec
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.fake import FakeProvider
from wisp.rpc import execution as rpc_execution_module
from wisp.rpc.commands import (
    MAX_RPC_COMMAND_TYPE_CHARS,
    ConfigureCommand,
    ParsedRpcCommand,
    RpcCommandAdapter,
)
from wisp.rpc.configuration import _RpcConfigureOverrides
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.execution import (
    RpcCommandExecutor,
    handle_rpc_store_api_key_command,
    rpc_selected_session_state,
)
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.commands import CommandArgument, CommandCategory, CommandDescriptor
from wisp.runtime.event_bus import EventBus
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.sessions.entries import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionTreeNavigation
from wisp.skills.models import SkillCatalog, SkillDiagnostic, SkillEntry
from wisp.tools.context import ToolContext
from wisp.tools.file_ops import CreateOnlyWriteReceipt


class _ApprovalResolver:
    def has_pending_approval(self, **_kwargs: object) -> bool:
        return False

    def resolve_approval(self, **_kwargs: object) -> bool:
        return False


class _TrustResolver:
    async def resolve(self) -> bool:
        return True

    def resolve_request(self, **_kwargs: object) -> bool:
        return False


def _parsed_command(payload: dict[str, object]) -> ParsedRpcCommand:
    try:
        command = RpcCommandAdapter.validate_json(json.dumps(payload))
    except ValidationError:
        return ParsedRpcCommand.from_unknown(payload)
    return ParsedRpcCommand.from_known(command, payload=payload)


async def _dispatch_parsed(
    executor: RpcCommandExecutor,
    payload: dict[str, object],
    running: _RpcRunningCommand | None = None,
) -> _RpcDispatchResult:
    return await executor.dispatch_parsed(_parsed_command(payload), running)


def _enable_project_init(fixture: RpcExecutorFixture, project_root: Path) -> None:
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tools=(ToolSpec.from_tool(fixture.runtime.tools.get("write")),),
        tool_context=ToolContext(cwd=project_root),
        project_context_root=project_root,
    )


def test_rpc_command_identity_replaces_oversized_ids_before_lifecycle_events() -> None:
    oversized_id = "x" * 257

    command_id, error = rpc_execution_module.rpc_command_id({"id": oversized_id, "type": "prompt"})

    assert command_id != oversized_id
    assert len(command_id) == 32
    assert error == "RPC command id must contain at most 256 characters"


def test_rpc_control_bounds_oversized_types_before_lifecycle_events() -> None:
    events: list[WispEvent] = []
    oversized_type = "x" * (MAX_RPC_COMMAND_TYPE_CHARS + 1)

    should_shutdown = rpc_execution_module.handle_rpc_control_command(
        {"id": "command-1", "type": oversized_type},
        running_command=None,
        approval_policy=_ApprovalResolver(),
        write_event=events.append,
    )

    assert should_shutdown is False
    assert [type(event) for event in events] == [
        RpcCommandStarted,
        ErrorEvent,
        RpcCommandFinished,
    ]
    assert all(
        event.command_type == "unknown"
        for event in events
        if isinstance(event, (RpcCommandStarted, RpcCommandFinished))
    )


def test_rpc_command_errors_bound_echoed_reference_fields() -> None:
    events: list[WispEvent] = []
    oversized_reference = "x" * (rpc_execution_module._MAX_RPC_COMMAND_ERROR_CHARS + 1)

    rpc_execution_module.handle_rpc_approval_command(
        {
            "id": "approval-1",
            "type": "approval",
            "call_id": oversized_reference,
            "approved": True,
        },
        command_id="approval-1",
        command_type="approval",
        approval_policy=_ApprovalResolver(),
        write_event=events.append,
    )

    assert len(events) == 2
    error, finished = events
    assert isinstance(error, ErrorEvent)
    assert isinstance(finished, RpcCommandFinished)
    assert len(error.message) == rpc_execution_module._MAX_RPC_COMMAND_ERROR_CHARS
    assert error.message.endswith("...")
    assert finished.error == error.message


def test_approval_resolution_waits_for_lifecycle_flush() -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    resolved: list[dict[str, object]] = []

    class PendingApproval:
        def has_pending_approval(self, *, call_id: str) -> bool:
            return call_id == "call-1"

        def resolve_approval(self, **kwargs: object) -> bool:
            resolved.append(kwargs)
            return True

    rpc_execution_module.handle_rpc_approval_command(
        {"id": "approval-1", "type": "approval", "call_id": "call-1", "approved": True},
        command_id="approval-1",
        command_type="approval",
        approval_policy=PendingApproval(),
        write_event=events.append,
        defer_resolution=deferred.append,
    )

    assert len(events) == 1
    assert isinstance(events[0], RpcCommandFinished)
    assert events[0].command_id == "approval-1"
    assert events[0].ok is True
    assert not resolved
    assert len(deferred) == 1

    deferred[0]()

    assert resolved == [
        {
            "call_id": "call-1",
            "approved": True,
            "reason": None,
            "scope": "once",
        }
    ]


def test_approval_resolution_runs_without_post_flush_callback() -> None:
    events: list[WispEvent] = []
    resolved: list[dict[str, object]] = []

    class PendingApproval:
        def has_pending_approval(self, *, call_id: str) -> bool:
            return call_id == "call-1"

        def resolve_approval(self, **kwargs: object) -> bool:
            resolved.append(kwargs)
            return True

    rpc_execution_module.handle_rpc_approval_command(
        {"id": "approval-1", "type": "approval", "call_id": "call-1", "approved": True},
        command_id="approval-1",
        command_type="approval",
        approval_policy=PendingApproval(),
        write_event=events.append,
    )

    assert resolved == [
        {
            "call_id": "call-1",
            "approved": True,
            "reason": None,
            "scope": "once",
        }
    ]
    assert len(events) == 1
    assert isinstance(events[0], RpcCommandFinished)
    assert events[0].ok is True


def test_active_cancellation_waits_for_lifecycle_flush() -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []

    class RecordingCancelScope:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    cancel_scope = RecordingCancelScope()
    running_command = _RpcRunningCommand(
        command_id="prompt-1",
        command_type="prompt",
        cancel_scope=cancel_scope,
    )
    rpc_execution_module.handle_rpc_cancel_command(
        {"id": "cancel-1", "type": "cancel", "target_id": "prompt-1"},
        command_id="cancel-1",
        command_type="cancel",
        running_command=running_command,
        write_event=events.append,
        defer_cancellation=deferred.append,
    )

    assert len(events) == 1
    assert isinstance(events[0], RpcCommandFinished)
    assert events[0].command_id == "cancel-1"
    assert events[0].ok is True
    assert cancel_scope.cancelled is False
    assert len(deferred) == 1

    deferred[0]()

    assert cancel_scope.cancelled is True


def test_executor_dispatches_validation_and_shutdown_without_stdin(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            invalid = await executor.dispatch({"id": "bad", "type": "prompt"}, None)
            shutdown = await executor.dispatch({"id": "bye", "type": "shutdown"}, None)

            assert invalid.running_command is None
            assert invalid.should_shutdown is False
            assert shutdown.should_shutdown is True
            task_group.cancel_scope.cancel()

        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
            RpcCommandStarted,
            RpcCommandFinished,
        ]
        error_event = fixture.events[1]
        finished_event = fixture.events[-1]
        assert isinstance(error_event, ErrorEvent)
        assert isinstance(finished_event, RpcCommandFinished)
        assert error_event.message == "RPC prompt command requires string field: prompt"
        assert finished_event.command_id == "bye"

    anyio.run(scenario)


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
        _enable_project_init(fixture, project)
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
            assert context.allowed_write_paths == (target,)
            assert context.conflicting_write_paths == (target.with_name("AGENTS.MD"),)
            assert context.require_create_only_writes is True
            assert context.require_non_empty_writes is True
            receipt = context.create_only_write_receipt
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
            result = await executor.dispatch({"id": "init-1", "type": "init"}, None)
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
    completion = rpc_execution_module._ProjectInitCompletion(
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
    completion = rpc_execution_module._ProjectInitCompletion(
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
        _enable_project_init(fixture, project)

        async def no_op_run(
            _prompt: str,
            **_kwargs: object,
        ) -> AsyncIterator[WispEvent]:
            if False:
                yield ErrorEvent(message="unreachable")

        monkeypatch.setattr(fixture.agent, "run", no_op_run)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
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
        _enable_project_init(fixture, project)

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
            await fixture.executor(task_group=task_group, send=send).dispatch(
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
        _enable_project_init(fixture, project)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
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
        _enable_project_init(fixture, project)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
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
            plan_result = await executor.dispatch({"id": "plan-init", "type": "init"}, None)
            fixture.agent.mode = "build"
            no_write_result = await executor.dispatch({"id": "no-write-init", "type": "init"}, None)
            task_group.cancel_scope.cancel()

        assert plan_result.running_command is None
        assert no_write_result.running_command is None
        errors = [event.message for event in fixture.events if isinstance(event, ErrorEvent)]
        assert errors == [
            "Project initialization requires build mode. Run /build first.",
            "Project initialization requires the write tool.",
        ]

    anyio.run(scenario)


def test_prompt_worker_converts_unexpected_exception_to_failed_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)

        def fail_run(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unexpected prompt failure")

        monkeypatch.setattr(fixture.agent, "run", fail_run)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {"id": "prompt-1", "type": "prompt", "prompt": "hello"},
                None,
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.command_id == "prompt-1"
        assert completed.ok is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("prompt-1", False, "unexpected prompt failure")
        ]

    anyio.run(scenario)


def test_prompt_worker_converts_unexpected_renderer_exception_to_failed_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)

        async def fail_render(_events: AsyncIterator[WispEvent]) -> None:
            raise RuntimeError("renderer failed")

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = RpcCommandExecutor(
                agent=fixture.agent,
                runtime=fixture.runtime,
                sessions=fixture.sessions,
                session_state=fixture.session_state,
                task_group=task_group,
                send=send,
                approval_policy=fixture.approval_policy,
                trust_gate=fixture.trust_gate,
                configure_overrides=fixture.configure_overrides,
                coordinator=fixture.coordinator,
                write_event=fixture.writer,
                render_events=fail_render,
            )
            result = await executor.dispatch(
                {"id": "prompt-1", "type": "prompt", "prompt": "hello"},
                None,
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.ok is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("prompt-1", False, "renderer failed")
        ]

    anyio.run(scenario)


def test_executor_queue_state_is_idle_safe_and_mutations_fail_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            state_result = await executor.dispatch(
                {"id": "state", "type": "get_queue_state"},
                None,
            )
            mutation_commands: list[dict[str, object]] = [
                {"id": "steer", "type": "steer", "content": "redirect"},
                {"id": "follow", "type": "follow_up", "content": "continue"},
                {
                    "id": "mode",
                    "type": "set_queue_mode",
                    "kind": "steering",
                    "mode": "all",
                },
                {"id": "pop", "type": "pop_queue", "kind": "steering"},
                {"id": "clear", "type": "clear_queue"},
            ]
            results = [await executor.dispatch(command, None) for command in mutation_commands]
            task_group.cancel_scope.cancel()

        assert state_result.running_command is None
        assert all(result.running_command is None for result in results)
        state_event = next(event for event in fixture.events if isinstance(event, QueueUpdated))
        assert state_event.steering == ()
        assert state_event.follow_up == ()
        assert state_event.steering_mode == "one_at_a_time"
        assert state_event.follow_up_mode == "one_at_a_time"
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("state", True, None),
            *[
                (command_id, False, "CodingSession has no active agent run")
                for command_id in ("steer", "follow", "mode", "pop", "clear")
            ],
        ]

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["prompt", "compact", "get_session_stats"])
def test_executor_reports_state_without_replacing_running_command(
    tmp_path: Path,
    command_type: Literal["prompt", "compact", "get_session_stats"],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        scope = anyio.CancelScope()
        running = _RpcRunningCommand("active-1", command_type, scope)
        if command_type == "prompt":
            scope.cancel()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "state-1", "type": "get_state"}, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcStateReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcStateReported)
        assert report.state == RpcStateSnapshot(
            provider="fake",
            model="fake",
            effort=None,
            auto_compaction_enabled=True,
            steering_mode="one_at_a_time",
            follow_up_mode="one_at_a_time",
            pending_steering_count=0,
            pending_follow_up_count=0,
            session_id=selected_session.session_id,
            session_path=selected_session.path,
            active_command_id="active-1",
            active_command_type=command_type,
            cancel_requested=command_type == "prompt",
        )

    anyio.run(scenario)


def test_executor_rejects_runtime_configuration_while_busy(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch_parsed(
                _parsed_command(
                    {
                        "id": "configure-1",
                        "type": "configure",
                        "auto_compaction_enabled": False,
                    }
                ),
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert finished.error == "Cannot configure while another RPC operation is active"
        assert fixture.agent.auto_compaction_enabled is True

    anyio.run(scenario)


def test_executor_new_session_resets_selected_state_and_rejects_while_busy(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="previous"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="previous"),)
        fixture.session_state.entry_count = 1
        fixture.session_state.name = "Previous"
        fixture.agent._last_session_id = selected.session_id  # noqa: SLF001
        fixture.agent._retained_queues[selected.session_id] = _RetainedQueueState(  # noqa: SLF001
            messages=QueuedMessages(follow_up=(Message(role="user", content="stale follow-up"),))
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            busy = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())

            rejected = await executor.dispatch(
                {"id": "new-busy", "type": "new_session"},
                busy,
            )
            accepted = await executor.dispatch(
                {"id": "new-1", "type": "new_session"},
                None,
            )
            assert rejected.reset_session is False
            assert accepted.reset_session is True
            fixture.coordinator._reset_session_state()
            task_group.cancel_scope.cancel()

        assert fixture.session_state.session is None
        assert fixture.session_state.history == ()
        assert fixture.session_state.entry_count == 0
        assert fixture.session_state.name is None
        assert fixture.agent.state_snapshot().pending_follow_up_count == 0
        assert fixture.agent._last_session_id is None  # noqa: SLF001
        assert not fixture.agent._retained_queues  # noqa: SLF001
        assert selected.path.is_file()
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            (
                "new-busy",
                False,
                "Cannot start a new session while another RPC operation is active",
            ),
            ("new-1", True, None),
        ]

    anyio.run(scenario)


def test_executor_configures_agent_mode_and_reports_it_in_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch_parsed(
                _parsed_command({"id": "configure-1", "type": "configure", "mode": "plan"}),
                None,
            )
            await executor.dispatch({"id": "state-1", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert fixture.agent.mode == "plan"
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.mode == "plan"

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("mode", "RPC configure command field mode must be 'build' or 'plan'"),
        (
            "auto_compaction_enabled",
            "RPC configure command field auto_compaction_enabled must be a boolean",
        ),
    ],
)
def test_executor_preserves_explicit_null_configure_rejections(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch_parsed(
                _parsed_command(
                    {
                        "id": "configure-1",
                        "type": "configure",
                        "model": "custom-model",
                        field: None,
                    }
                ),
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.agent.model is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "configure-1"
        assert finished.ok is False
        assert finished.error == message

    anyio.run(scenario)


def test_executor_parsed_entry_delegates_non_configure_and_unknown_commands(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch_parsed(
                _parsed_command({"id": "state-1", "type": "get_state"}),
                None,
            )
            await executor.dispatch_parsed(
                ParsedRpcCommand.from_unknown({"id": "future-1", "type": "future_command"}),
                None,
            )
            task_group.cancel_scope.cancel()

        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcStateReported,
            RpcCommandFinished,
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        future_finished = fixture.events[-1]
        assert isinstance(future_finished, RpcCommandFinished)
        assert future_finished.command_id == "future-1"
        assert future_finished.command_type == "future_command"
        assert future_finished.ok is False
        assert future_finished.error == "Unknown RPC command: future_command"

    anyio.run(scenario)


def test_executor_state_projects_prompt_startup_queue_buffer(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        buffered_commands: list[dict[str, object]] = [
            {"id": [], "type": "steer", "content": "not real"},
            {
                "id": "",
                "type": "set_queue_mode",
                "kind": "follow_up",
                "mode": "all",
            },
            {"type": "steer", "content": "anonymous"},
            {"id": "steer-1", "type": "steer", "content": "redirect"},
            {"id": "follow-1", "type": "follow_up", "content": "continue"},
            {
                "id": "mode",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "all",
            },
            {"id": "steer-2", "type": "steer", "content": "refine"},
            {"id": "pop", "type": "pop_queue", "kind": "steering"},
            {"id": "clear", "type": "clear_queue", "kind": "follow_up"},
            {"id": "invalid", "type": "follow_up"},
        ]
        parsed_commands = [_parsed_command(command) for command in buffered_commands]
        fixture.coordinator.pending_prompt_queue_commands.extend(parsed_commands)
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "state-1", "type": "get_state"}, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.pending_steering_count == 2
        assert report.state.pending_follow_up_count == 0
        assert report.state.steering_mode == "all"
        assert report.state.follow_up_mode == "one_at_a_time"
        assert [
            command.to_legacy_dict()
            for command in fixture.coordinator.pending_prompt_queue_commands
        ] == buffered_commands

    anyio.run(scenario)


def test_executor_state_is_idle_safe_and_reports_snapshot_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            idle = await executor.dispatch({"id": "idle", "type": "get_state"}, None)
            malformed = await executor.dispatch({"id": [], "type": "get_state"}, None)

            def fail_snapshot(_session: object = None) -> object:
                raise RuntimeError("snapshot failed")

            monkeypatch.setattr(fixture.agent, "state_snapshot", fail_snapshot)
            failed = await executor.dispatch({"id": "failed", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert idle.running_command is None
        assert malformed.running_command is None
        assert failed.running_command is None
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.session_id is None
        assert report.state.session_path is None
        assert report.state.active_command_id is None
        assert report.state.active_command_type is None
        assert report.state.cancel_requested is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("get_state", True, None),
            ("get_state", False, "RPC command id must be a non-empty string"),
            ("get_state", False, "snapshot failed"),
        ]

    anyio.run(scenario)


def test_executor_reports_commands_from_runtime_registry_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.runtime.api.register_command(
            CommandDescriptor(
                name="inspect",
                title="Inspect",
                description="Inspect command registry metadata",
                category=CommandCategory.general,
                aliases=("i", ":inspect"),
                arguments=(
                    CommandArgument(
                        name="target",
                        description="Optional target",
                    ),
                ),
                accepts_arguments=True,
                prefill_on_partial_enter=True,
                order=5,
            )
        )
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "commands-1", "type": "get_commands"}, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcCommandsReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcCommandsReported)
        assert report.command_id == "commands-1"
        assert report.commands[0].name == "inspect"
        inspected = report.commands[0]
        assert inspected.title == "Inspect"
        assert inspected.description == "Inspect command registry metadata"
        assert inspected.category == "general"
        assert inspected.aliases == ("i", ":inspect")
        assert inspected.slash_command == "/inspect"
        assert inspected.slash_aliases == ("/i", ":inspect")
        assert inspected.arguments[0].name == "target"
        assert inspected.accepts_arguments is True
        assert inspected.prefill_on_partial_enter is True
        assert [descriptor.name for descriptor in report.commands[1:]] == [
            "help",
            "init",
            "compact",
            "context",
            "history",
            "update",
            "skills",
            "mcp",
            "plan",
            "build",
            "model",
            "new",
            "resume",
            "provider",
            "auth",
            "connect",
            "disconnect",
            "quit",
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "commands-1"
        assert finished.command_type == "get_commands"
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_reports_model_catalog_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
                {"id": "models-1", "type": "get_model_catalog"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcModelCatalogReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcModelCatalogReported)
        assert report.catalog.selection.provider == "fake"
        assert tuple(provider.name for provider in report.catalog.providers) == tuple(
            provider.name for provider in fixture.runtime.models.providers()
        )
        assert all(
            provider.available == fixture.runtime.providers.is_registered(provider.name)
            for provider in report.catalog.providers
        )

    anyio.run(scenario)


def test_executor_reports_connection_catalog_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
                {"id": "connections-1", "type": "get_connection_catalog"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcConnectionCatalogReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcConnectionCatalogReported)
        assert report.catalog.providers
        assert fixture.runtime.openai_compatible_provider is None
        assert "openai-compatible" not in {provider.id for provider in report.catalog.providers}
        assert all(
            method.environment_variable is None or "KEY" in method.environment_variable
            for family in report.catalog.providers
            for method in family.methods
        )

    anyio.run(scenario)


def test_executor_stores_api_key_without_leaking_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        secret = "sentinel-secret-key"
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "anthropic",
                    "api_key": secret,
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is None
        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped
        report = next(
            event for event in fixture.events if isinstance(event, RpcConnectionCatalogReported)
        )
        anthropic = next(family for family in report.catalog.providers if family.id == "anthropic")
        assert anthropic.methods[0].has_stored_credential is True
        assert fixture.runtime.auth_store is not None
        credential = fixture.runtime.auth_store.get("anthropic")
        assert credential is not None

    anyio.run(scenario)


def test_executor_rejects_api_key_for_unregistered_openai_compatible_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        secret = "unregistered-provider-secret"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await fixture.executor(task_group=task_group, send=send).dispatch(
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "openai-compatible",
                    "api_key": secret,
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store is not None
        assert fixture.runtime.auth_store.get("openai-compatible") is None
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is False
        assert finished.error == ("API-key connection is not supported for openai-compatible.")
        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped

    anyio.run(scenario)


def test_executor_rejects_api_key_for_keyless_openai_compatible_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await build_runtime(
            auth_path=tmp_path / "auth.json",
            openai_compatible=OpenAICompatibleSettings(
                provider_name="local-models",
                base_url="http://localhost:11434/v1",
                default_model="local-model",
                requires_api_key=False,
            ),
        )
        events: list[WispEvent] = []
        secret = "irrelevant-secret"
        try:
            handle_rpc_store_api_key_command(
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "local-models",
                    "api_key": secret,
                },
                running_command=None,
                runtime=runtime,
                write_event=events.append,
            )

            assert runtime.auth_store is not None
            assert runtime.auth_store.get("local-models") is None
            finished = events[-1]
            assert isinstance(finished, RpcCommandFinished)
            assert finished.ok is False
            assert finished.error == "API-key connection is not supported for local-models."
            dumped = json.dumps([event.model_dump(mode="json") for event in events])
            assert secret not in dumped
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_executor_keeps_api_key_success_when_catalog_refresh_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    secret = "sentinel-refresh-secret"

    def fail_catalog(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
        raise OverflowError(secret)

    monkeypatch.setattr(rpc_execution_module, "rpc_connection_catalog_snapshot", fail_catalog)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await fixture.executor(task_group=task_group, send=send).dispatch(
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "anthropic",
                    "api_key": secret,
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store is not None
        assert fixture.runtime.auth_store.get("anthropic") == ApiKeyCredential(key=secret)
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        warning = fixture.events[1]
        assert isinstance(warning, ErrorEvent)
        assert warning.message == (
            "API key stored; connection catalog unavailable: status refresh failed"
        )
        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_keeps_disconnect_success_when_catalog_refresh_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_catalog(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
        raise OverflowError("unsafe diagnostic")

    monkeypatch.setattr(rpc_execution_module, "rpc_connection_catalog_snapshot", fail_catalog)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        assert fixture.runtime.auth_store is not None
        fixture.runtime.auth_store.set("anthropic", ApiKeyCredential(key="stored-key"))
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await fixture.executor(task_group=task_group, send=send).dispatch(
                {
                    "id": "disconnect-1",
                    "type": "disconnect_provider",
                    "provider": "anthropic",
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store.get("anthropic") is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        warning = fixture.events[1]
        assert isinstance(warning, ErrorEvent)
        assert warning.message == (
            "Credentials disconnected; connection catalog unavailable: status refresh failed"
        )
        assert "unsafe diagnostic" not in warning.message
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_reports_device_code_progress_and_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_login(
        *, on_device_code: Callable[[DeviceCodeInfo], None], on_progress: Callable[[int], None]
    ) -> OAuthCredential:
        on_device_code(DeviceCodeInfo("ABCD-1234", "https://example.test/device", 1, 900))
        on_progress(1)
        return OAuthCredential(access="access", refresh="refresh", expires=4_102_444_800_000)

    monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcDeviceCodeReported,
            RpcDeviceCodeProgressReported,
            RpcConnectionCatalogReported,
            RpcCommandFinished,
        ]
        progress = fixture.events[2]
        assert isinstance(progress, RpcDeviceCodeProgressReported)
        assert progress.attempt == 1

    anyio.run(scenario)


def test_executor_keeps_device_login_success_when_catalog_refresh_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    credential = OAuthCredential(access="access", refresh="refresh", expires=4_102_444_800_000)

    async def fake_login(**_kwargs: object) -> OAuthCredential:
        return credential

    def fail_catalog(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
        raise OverflowError("unsafe diagnostic")

    monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", fake_login)
    monkeypatch.setattr(rpc_execution_module, "rpc_connection_catalog_snapshot", fail_catalog)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await fixture.executor(task_group=task_group, send=send).dispatch(
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await receive.receive()
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store is not None
        assert fixture.runtime.auth_store.get("openai-codex") == credential
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        warning = fixture.events[1]
        assert isinstance(warning, ErrorEvent)
        assert warning.message == (
            "Device login completed; connection catalog unavailable: status refresh failed"
        )
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_sanitizes_device_code_provider_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    secret = "sentinel-provider-secret"

    async def fake_login(**_kwargs: object) -> OAuthCredential:
        raise RuntimeError(secret)

    monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await fixture.executor(task_group=task_group, send=send).dispatch(
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await receive.receive()
            task_group.cancel_scope.cancel()

        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert finished.error == "Provider connection failed"

    anyio.run(scenario)


def test_executor_cancels_device_code_without_replacing_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = anyio.Event()

    async def fake_login(**_kwargs: object) -> OAuthCredential:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        assert fixture.runtime.auth_store is not None
        existing = OAuthCredential(access="old", refresh="old", expires=4_102_444_800_000)
        fixture.runtime.auth_store.set("openai-codex", existing)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await started.wait()
            assert result.running_command is not None
            result.running_command.cancel_scope.cancel()
            await receive.receive()
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store.get("openai-codex") == existing
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert finished.error == "RPC command cancelled: device-1"

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command",
    [
        {"id": "store-1", "type": "store_api_key", "provider": "anthropic", "api_key": "secret"},
        {"id": "disconnect-1", "type": "disconnect_provider", "provider": "anthropic"},
        {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
    ],
)
def test_executor_rejects_connection_mutations_while_an_operation_is_active(
    tmp_path: Path,
    command: dict[str, object],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch(
                dict(command),
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert "another RPC operation is active" in (finished.error or "")

    anyio.run(scenario)


def test_model_catalog_marks_unregistered_providers_without_constructing_deferred_ones() -> None:
    providers = ProviderRegistry()
    current = FakeProvider()
    providers.register(current)
    constructed = False

    def construct_deferred() -> FakeProvider:
        nonlocal constructed
        constructed = True
        return FakeProvider()

    providers.register_factory("deferred", construct_deferred)
    models = ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=tuple(
                ModelCatalogProviderEntry(
                    name=name,
                    display_name=name.title(),
                    default_model=model,
                    docs_url=f"https://example.test/{name}",
                    models=(model,),
                )
                for name, model in (
                    ("fake", "fake"),
                    ("catalog-only", "catalog-model"),
                    ("deferred", "deferred-model"),
                )
            ),
        )
    )
    tools = ToolRegistry()
    events = EventBus()
    runtime = WispRuntime(
        providers=providers,
        tools=tools,
        events=events,
        api=ExtensionAPI(
            providers=providers,
            tools=tools,
            events=events,
        ),
        models=models,
    )

    snapshot = rpc_execution_module.rpc_model_catalog_snapshot(
        runtime=runtime,
        provider=current,
        model="custom-model",
        effort="custom-effort",
    )

    assert [(provider.name, provider.available) for provider in snapshot.providers] == [
        ("fake", True),
        ("catalog-only", False),
        ("deferred", True),
    ]
    assert snapshot.selection.catalog_model is None
    assert snapshot.selection.effort == "custom-effort"
    assert constructed is False


def test_oversized_model_catalog_does_not_block_typed_configuration(tmp_path: Path) -> None:
    provider = FakeProvider()
    providers = ProviderRegistry()
    providers.register(provider)
    model_ids = ("fake", *(f"model-{index}" for index in range(512)))
    models = ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=(
                ModelCatalogProviderEntry(
                    name="fake",
                    display_name="Fake",
                    default_model="fake",
                    docs_url="https://example.test/fake",
                    models=model_ids,
                ),
            ),
        )
    )
    tools = ToolRegistry()
    events = EventBus()
    runtime = WispRuntime(
        providers=providers,
        tools=tools,
        events=events,
        api=ExtensionAPI(providers=providers, tools=tools, events=events),
        models=models,
    )
    agent = CodingSession(provider=provider, sessions=JsonlSessionStore(tmp_path))
    overrides = _RpcConfigureOverrides()
    discovery_events: list[WispEvent] = []
    rpc_execution_module.handle_rpc_model_catalog_command(
        {"type": "get_model_catalog", "id": "catalog-1"},
        agent=agent,
        runtime=runtime,
        write_event=discovery_events.append,
    )
    assert not any(isinstance(event, RpcModelCatalogReported) for event in discovery_events)
    assert isinstance(discovery_events[-1], RpcCommandFinished)
    assert discovery_events[-1].ok is False

    emitted: list[WispEvent] = []

    parsed = _parsed_command(
        {
            "id": "configure-1",
            "type": "configure",
            "model": "custom-model",
            "effort": "custom-effort",
        }
    )
    command = parsed.known
    assert isinstance(command, ConfigureCommand)
    rpc_execution_module.handle_rpc_configure_command(
        command,
        command_id="configure-1",
        provided_fields=parsed.provided_fields,
        agent=agent,
        runtime=runtime,
        configure_overrides=overrides,
        write_event=emitted.append,
    )

    assert agent.model == "custom-model"
    assert agent.effort == "custom-effort"
    assert overrides.model == "custom-model"
    assert overrides.effort == "custom-effort"
    assert not any(isinstance(event, RpcModelCatalogReported) for event in emitted)
    assert isinstance(emitted[-2], ErrorEvent)
    assert "Configuration applied; model catalog unavailable" in emitted[-2].message
    assert isinstance(emitted[-1], RpcCommandFinished)
    assert emitted[-1].ok is True


def test_executor_reports_active_skill_catalog_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.agent.skill_catalog = SkillCatalog(
            entries=(
                SkillEntry(
                    name="review",
                    description="Review [literal] output",
                    source="user:wisp",
                    root=tmp_path / "review",
                ),
            ),
            diagnostics=(
                SkillDiagnostic(
                    code="invalid-yaml",
                    severity="warning",
                    message="broken [literal] metadata",
                    source="project:wisp",
                    path=tmp_path / "bad" / "SKILL.md",
                ),
            ),
        )
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "skills-1", "type": "get_skills"}, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSkillsReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSkillsReported)
        assert report.catalog.entries[0].name == "review"
        assert report.catalog.entries[0].description == "Review [literal] output"
        assert report.catalog.diagnostics[0].message == "broken [literal] metadata"
        assert report.catalog.project_trusted is False

    anyio.run(scenario)


def test_executor_reports_empty_mcp_status_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "mcp-1", "type": "get_mcp_status"}, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcMcpStatusReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcMcpStatusReported)
        assert report.status.servers == ()

    anyio.run(scenario)


def test_executor_commands_reports_malformed_id_and_registry_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            malformed = await executor.dispatch({"id": [], "type": "get_commands"}, None)

            def fail_commands() -> object:
                raise RuntimeError("registry failed")

            monkeypatch.setattr(fixture.runtime.commands, "all", fail_commands)
            failed = await executor.dispatch({"id": "commands-1", "type": "get_commands"}, None)
            task_group.cancel_scope.cancel()

        assert malformed.running_command is None
        assert failed.running_command is None
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("get_commands", False, "RPC command id must be a non-empty string"),
            ("get_commands", False, "registry failed"),
        ]

    anyio.run(scenario)


def test_executor_messages_reports_empty_without_selected_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await _dispatch_parsed(executor, {"id": "messages", "type": "get_messages"})
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "get_messages"
        assert completed.command_id == "messages"
        assert completed.command_type == "get_messages"
        assert completed.ok is True
        assert completed.history is None
        assert completed.entry_count == 0
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcMessagesReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcMessagesReported)
        assert report.session_id is None
        assert report.session_path is None
        assert report.active_leaf_id is None
        assert report.messages == ()
        assert report.truncated is False
        assert report.next_before_entry_id is None

    anyio.run(scenario)


def test_executor_messages_cancelled_before_publish_reports_failed_without_payload(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await _dispatch_parsed(executor, {"id": "messages", "type": "get_messages"})
            assert result.running_command is not None
            result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert completed.history is None
        assert completed.entry_count == 0
        assert not any(isinstance(event, RpcMessagesReported) for event in fixture.events)
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("messages", False, "RPC get_messages command cancelled")
        ]

    anyio.run(scenario)


def test_executor_messages_reads_selected_and_explicit_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        other = fixture.sessions.create()

        async def write() -> None:
            await selected.append_message(Message(role="user", content="one"))
            await selected.append_message(Message(role="assistant", content="two"))
            await selected.append_message(Message(role="user", content="three"))
            await other.append_message(Message(role="user", content="other"))

        await write()
        fixture.session_state.session = selected
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            selected_result = await _dispatch_parsed(
                executor,
                {"id": "selected", "type": "get_messages", "limit": 2},
            )
            selected_completed = await receive.receive()
            explicit_result = await _dispatch_parsed(
                executor,
                {
                    "id": "explicit",
                    "type": "get_messages",
                    "session_id": other.session_id,
                },
            )
            explicit_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert selected_result.selected_session is None
        assert explicit_result.selected_session is None
        assert selected_completed.history is not None
        assert [message.content for message in selected_completed.history] == [
            "one",
            "two",
            "three",
        ]
        assert selected_completed.entry_count == 3
        assert explicit_completed.history is None
        assert explicit_completed.entry_count == 0
        reports = [event for event in fixture.events if isinstance(event, RpcMessagesReported)]
        assert [message.content for message in reports[0].messages] == ["two", "three"]
        assert reports[0].session_id == selected.session_id
        assert reports[0].session_path == selected.path
        assert reports[0].truncated is True
        assert reports[0].next_before_entry_id == reports[0].messages[0].entry_id
        assert [message.content for message in reports[1].messages] == ["other"]
        assert reports[1].session_id == other.session_id
        assert reports[1].session_path == other.path
        assert fixture.session_state.session is selected

    anyio.run(scenario)


def test_executor_messages_reads_forward_after_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        entries = []
        for index in range(5):
            entries.append(
                await selected.append_message(Message(role="user", content=f"message-{index}"))
            )
        fixture.session_state.session = selected

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await _dispatch_parsed(
                executor,
                {
                    "id": "newer",
                    "type": "get_messages",
                    "limit": 2,
                    "after_entry_id": entries[1].id,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert completed.ok
        report = next(
            event
            for event in fixture.events
            if isinstance(event, RpcMessagesReported) and event.command_id == "newer"
        )
        assert [message.content for message in report.messages] == ["message-2", "message-3"]
        assert report.next_before_entry_id is None
        assert report.next_after_entry_id == entries[3].id

    anyio.run(scenario)


def test_executor_messages_reads_one_exact_full_content_row(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        exact_content = "x" * 70_000
        entry = await selected.append_message(Message(role="tool", content=exact_content))
        fixture.session_state.session = selected

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(
                executor,
                {
                    "id": "detail",
                    "type": "get_messages",
                    "entry_ids": [entry.id],
                    "complete_structure": True,
                    "full_content": True,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is True
        report = next(
            event
            for event in fixture.events
            if isinstance(event, RpcMessagesReported) and event.command_id == "detail"
        )
        assert len(report.messages) == 1
        assert report.messages[0].entry_id == entry.id
        assert report.messages[0].content == exact_content
        assert report.messages[0].content_truncated is False

    anyio.run(scenario)


def test_executor_messages_historical_page_refreshes_selected_session_after_external_append(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()

        await selected.append_message(Message(role="user", content="one"))
        await selected.append_message(Message(role="assistant", content="two"))
        await selected.append_message(Message(role="user", content="three"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="cached"),)
        fixture.session_state.entry_count = 3

        newest = selected.read_message_page(limit=2)
        assert newest.next_before_entry_id is not None
        entries = selected.read_entries()
        await fixture.sessions.load(selected.session_id).append_entry(
            MessageSessionEntry(
                session_id=selected.session_id,
                parent_id=entries[-1].id,
                message=Message(role="assistant", content="external"),
            )
        )

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(
                executor,
                {
                    "id": "older",
                    "type": "get_messages",
                    "limit": 2,
                    "before_entry_id": newest.next_before_entry_id,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is True
        assert completed.history is not None
        assert [message.content for message in completed.history] == [
            "one",
            "two",
            "three",
            "external",
        ]
        assert completed.entry_count == 4
        reports = [event for event in fixture.events if isinstance(event, RpcMessagesReported)]
        assert [message.content for message in reports[-1].messages] == ["one"]

    anyio.run(scenario)


def test_updated_rpc_session_state_reads_entries_once(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="two"))

    anyio.run(write)
    original_read_entry_snapshot = session.read_entry_snapshot
    read_count = 0

    def count_reads() -> tuple[object, ...]:
        nonlocal read_count
        read_count += 1
        return original_read_entry_snapshot()

    monkeypatch.setattr(session, "read_entry_snapshot", count_reads)

    entry_count, history = rpc_execution_module.updated_rpc_session_state(session, (), 0)

    assert entry_count == 2
    assert [message.content for message in history] == ["one", "two"]
    assert read_count == 1


def test_executor_messages_reports_read_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="hello"))
        fixture.session_state.session = selected
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            missing = await _dispatch_parsed(
                executor,
                {"id": "missing", "type": "get_messages", "session_id": "missing"},
            )
            missing_completed = await receive.receive()
            bad_cursor = await _dispatch_parsed(
                executor,
                {"id": "cursor", "type": "get_messages", "before_entry_id": "missing"},
            )
            cursor_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert missing.running_command is not None
        assert bad_cursor.running_command is not None
        assert missing_completed.ok is False
        assert cursor_completed.ok is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("get_messages", False, "Session not found: missing"),
            ("get_messages", False, "Session message cursor not found: missing"),
        ]

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        (
            "entry_ids",
            "RPC get_messages command field entry_ids must contain non-empty strings",
        ),
        (
            "complete_structure",
            "RPC get_messages complete_structure and full_content fields must be booleans",
        ),
        (
            "full_content",
            "RPC get_messages complete_structure and full_content fields must be booleans",
        ),
    ],
)
def test_executor_messages_preserves_explicit_null_rejections(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "messages-null", "type": "get_messages", field: None},
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "messages-null"
        assert finished.ok is False
        assert finished.error == message

    anyio.run(scenario)


def test_executor_messages_accepts_explicit_null_optional_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "id": "messages-null",
                    "type": "get_messages",
                    "session_id": None,
                    "before_entry_id": None,
                    "after_entry_id": None,
                    "allow_during_prompt": None,
                },
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is True
        report = fixture.events[1]
        assert isinstance(report, RpcMessagesReported)
        assert report.messages == ()

    anyio.run(scenario)


def test_executor_sessions_reports_empty_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await _dispatch_parsed(executor, {"id": "sessions", "type": "get_sessions"})
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "get_sessions"
        assert completed.command_id == "sessions"
        assert completed.command_type == "get_sessions"
        assert completed.ok is True
        assert completed.history is None
        assert completed.entry_count == 0
        assert completed.selected_session is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionsReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionsReported)
        assert report.sessions == ()
        assert report.selected_session_id is None
        assert report.selected_session_path is None

    anyio.run(scenario)


def test_executor_sessions_cancel_abandons_blocked_catalog_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        started = threading.Event()
        release = threading.Event()
        original_summaries = fixture.sessions.summaries

        def blocked_summaries(*, limit: int | None = None) -> object:
            started.set()
            release.wait(timeout=5)
            return original_summaries(limit=limit)

        monkeypatch.setattr(fixture.sessions, "summaries", blocked_summaries)

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(executor, {"id": "sessions", "type": "get_sessions"})
            assert result.running_command is not None

            try:
                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)

                result.running_command.cancel_scope.cancel()

                with anyio.fail_after(1):
                    completed = await receive.receive()
            finally:
                release.set()
                task_group.cancel_scope.cancel()

        assert completed.command_id == "sessions"
        assert completed.command_type == "get_sessions"
        assert completed.ok is False
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC get_sessions command cancelled"

    anyio.run(scenario)


def test_executor_sessions_reports_bounded_catalog_with_selected_metadata(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        first = fixture.sessions.create()
        await first.append_message(Message(role="user", content="one"))
        second = fixture.sessions.create()
        await second.append_message(Message(role="user", content="two"))
        fixture.session_state.session = first
        fixture.session_state.entry_count = 1
        event_loop_thread = threading.get_ident()

        def read_name_from_worker() -> str:
            assert threading.get_ident() != event_loop_thread
            return "Selected"

        monkeypatch.setattr(first, "read_name", read_name_from_worker)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await _dispatch_parsed(
                executor,
                {"id": "sessions", "type": "get_sessions", "limit": 1},
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.selected_session is None
        assert completed.ok is True
        assert completed.history is None
        assert completed.entry_count == 1
        report = next(event for event in fixture.events if isinstance(event, RpcSessionsReported))
        assert len(report.sessions) == 1
        assert report.sessions[0].session_id in {first.session_id, second.session_id}
        assert report.sessions[0].session_path in {first.path, second.path}
        assert report.sessions[0].entry_count == 1
        assert report.sessions[0].active_leaf_id is not None
        assert report.selected_session_id == first.session_id
        assert report.selected_session_path == first.path
        assert report.selected_session_name == "Selected"
        assert fixture.session_state.session is first

    anyio.run(scenario)


def test_executor_select_session_updates_coordinator_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="one"))
        await selected.append_message(Message(role="assistant", content="two"))
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {"id": "select", "type": "select_session", "session_id": selected.session_id},
                None,
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionSelected) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "select_session"
        assert completed.ok is True
        assert completed.post_apply_events
        assert completed.selected_session is not None
        assert completed.selected_session.session_id == selected.session_id
        assert completed.selected_session.path == selected.path
        assert [message.content for message in completed.history or ()] == ["one", "two"]
        assert completed.entry_count == 2
        assert fixture.session_state.session is completed.selected_session
        assert [message.content for message in fixture.session_state.history] == ["one", "two"]
        assert fixture.session_state.entry_count == 2
        report = next(event for event in fixture.events if isinstance(event, RpcSessionSelected))
        assert report.command_id == "select"
        assert report.session_id == selected.session_id
        assert report.session_path == selected.path
        assert report.active_leaf_id is not None
        assert report.entry_count == 2

    anyio.run(scenario)


def test_executor_set_session_name_updates_selected_cached_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="one"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="one"),)
        fixture.session_state.entry_count = 1
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {"id": "name", "type": "set_session_name", "name": "  Alpha\r\nBeta  "},
                None,
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionNameChanged) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            await executor.dispatch({"id": "state", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.session_name == "Alpha Beta"
        assert completed.session_name_updated is True
        assert fixture.session_state.session is selected
        assert fixture.session_state.name == "Alpha Beta"
        assert fixture.session_state.entry_count == 2
        name_events = [
            event for event in fixture.events if isinstance(event, RpcSessionNameChanged)
        ]
        changed = name_events[0]
        assert changed.command_id == "name"
        assert changed.session_id == selected.session_id
        assert changed.previous_name is None
        assert changed.name == "Alpha Beta"
        assert changed.entry_count == 2
        state = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert state.state.session_name == "Alpha Beta"

    anyio.run(scenario)


def test_executor_set_session_name_with_explicit_id_does_not_switch_selection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="selected"))
        await selected.set_name("Selected")
        other = fixture.sessions.create()
        await other.append_message(Message(role="user", content="other"))
        fixture.session_state.session = selected
        fixture.session_state.history = (Message(role="user", content="selected"),)
        fixture.session_state.entry_count = 2
        fixture.session_state.name = "Selected"
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": other.session_id,
                    "name": "Other",
                },
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.session_name_updated is False
        assert fixture.session_state.session is selected
        assert fixture.session_state.name == "Selected"
        assert fixture.session_state.entry_count == 2
        assert other.read_name() == "Other"
        name_events = [
            event for event in fixture.events if isinstance(event, RpcSessionNameChanged)
        ]
        changed = name_events[0]
        assert changed.session_id == other.session_id
        assert changed.name == "Other"

    anyio.run(scenario)


def test_executor_set_session_name_with_explicit_path_requires_same_selected_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        await selected.append_message(Message(role="user", content="selected"))
        await selected.set_name("Selected")
        copied_path = tmp_path / "copied-session.jsonl"
        shutil.copy2(selected.path, copied_path)
        copied = fixture.sessions.load(copied_path)
        assert copied.session_id == selected.session_id
        fixture.session_state.session = selected
        fixture.session_state.history = selected.read_context_messages()
        fixture.session_state.entry_count = len(selected.read_entries())
        fixture.session_state.name = "Selected"
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": str(copied_path),
                    "name": "Copied",
                },
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.session_name_updated is False
        assert fixture.session_state.session is selected
        assert fixture.session_state.history == selected.read_context_messages()
        assert fixture.session_state.entry_count == 2
        assert fixture.session_state.name == "Selected"
        assert selected.read_name() == "Selected"
        assert copied.read_name() == "Copied"

    anyio.run(scenario)


def test_executor_set_session_name_matches_selected_session_by_normalized_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.chdir(tmp_path)
        relative_store = JsonlSessionStore(Path("sessions"))
        fixture = await build_rpc_executor_fixture(tmp_path / "unused")
        fixture.sessions = relative_store
        selected = relative_store.create()
        await selected.append_message(Message(role="user", content="selected"))
        await selected.set_name("Selected")
        fixture.session_state.session = selected
        fixture.session_state.history = selected.read_context_messages()
        fixture.session_state.entry_count = len(selected.read_entries())
        fixture.session_state.name = "Selected"
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": str(selected.path.resolve(strict=False)),
                    "name": "Renamed",
                },
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.session_name_updated is True
        assert fixture.session_state.session is selected
        assert fixture.session_state.entry_count == 3
        assert fixture.session_state.name == "Renamed"
        assert selected.read_name() == "Renamed"

    anyio.run(scenario)


def test_rpc_selected_session_state_reads_name_from_the_same_entry_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="selected"))
        await session.set_name("Selected")

    anyio.run(write)

    def fail_second_name_read() -> str | None:
        raise AssertionError("name should come from the already-read entries")

    monkeypatch.setattr(session, "read_name", fail_second_name_read)

    entry_count, history, active_leaf_id, name = rpc_selected_session_state(session)

    assert entry_count == 2
    assert [message.content for message in history] == ["selected"]
    assert active_leaf_id is not None
    assert name == "Selected"


def test_executor_select_session_reports_validation_and_load_failures(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        previous = fixture.sessions.create()
        await previous.append_message(Message(role="user", content="previous"))
        fixture.session_state.session = previous
        fixture.session_state.history = (Message(role="user", content="previous"),)
        fixture.session_state.entry_count = 1
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            assert (
                await executor.dispatch({"id": [], "type": "select_session"}, None)
            ).running_command is None
            assert (
                await executor.dispatch(
                    {"id": "empty", "type": "select_session", "session_id": ""},
                    None,
                )
            ).running_command is None
            missing = await executor.dispatch(
                {"id": "missing", "type": "select_session", "session_id": "missing"},
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = missing.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert missing.running_command is not None
        assert completed.ok is False
        assert completed.selected_session is None
        assert completed.history is None
        assert completed.entry_count == 1
        assert fixture.session_state.session is previous
        assert [message.content for message in fixture.session_state.history] == ["previous"]
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("select_session", False, "RPC command id must be a non-empty string"),
            (
                "select_session",
                False,
                "RPC select_session command field session_id must be a non-empty string",
            ),
            ("select_session", False, "Session not found: missing"),
        ]
        assert not any(isinstance(event, RpcSessionSelected) for event in fixture.events)

    anyio.run(scenario)


def test_executor_clone_session_applies_target_before_reporting_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="one"))
        await source.append_message(Message(role="assistant", content="two"))
        source_before = source.path.read_bytes()
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        fixture.agent._retained_queues[source.session_id] = _RetainedQueueState(  # noqa: SLF001
            messages=QueuedMessages(
                steering=(Message(role="user", content="source steering"),),
                follow_up=(Message(role="user", content="source follow-up"),),
            ),
            steering_mode="all",
            follow_up_mode="all",
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "clone", "type": "clone_session"}, None)
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionCloned) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "clone_session"
        assert completed.ok is True
        assert completed.selected_session is not None
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session.session_id != source.session_id
        assert completed.selected_session.read_context_messages() == source.read_context_messages()
        assert fixture.session_state.history == source.read_context_messages()
        assert fixture.session_state.entry_count == 2
        assert source.path.read_bytes() == source_before
        assert fixture.agent.queue_state(source).steering == ("source steering",)
        assert fixture.agent.queue_state(source).follow_up == ("source follow-up",)
        assert fixture.agent.queue_state(completed.selected_session).steering == ()
        assert fixture.agent.queue_state(completed.selected_session).follow_up == ()
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionCloned,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionCloned)
        assert report.source_session_id == source.session_id
        assert report.source_session_path == source.path
        assert report.session_id == completed.selected_session.session_id
        assert report.session_path == completed.selected_session.path
        assert report.active_leaf_id == completed.selected_session.read_active_leaf_id()
        assert report.entry_count == 2

    anyio.run(scenario)


def test_executor_clone_session_reports_name_from_inherited_clone_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="one"))
        await source.set_name("Named Source")
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())

        def stale_source_name_read() -> str | None:
            raise AssertionError("clone event must not pre-read the source name")

        monkeypatch.setattr(source, "read_name", stale_source_name_read)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "clone", "type": "clone_session"}, None)
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert fixture.session_state.name == "Named Source"
        report = next(event for event in fixture.events if isinstance(event, RpcSessionCloned))
        assert report.source_session_name == "Named Source"
        assert report.session_name == "Named Source"
        assert report.entry_count == 2
        assert completed.selected_session.read_name() == "Named Source"

    anyio.run(scenario)


def test_executor_fork_session_returns_prompt_and_selects_parent_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="first"))
        answer = await source.append_message(Message(role="assistant", content="answer"))
        editable_prompt = "edit me 漢🙂" * 2_000
        selected = await source.append_message(Message(role="user", content=editable_prompt))
        abandoned = await source.append_message(Message(role="assistant", content="old answer"))
        await source.select_active_leaf(
            answer.id,
            expected_active_leaf_id=abandoned.id,
        )
        await source.append_message(Message(role="user", content="active alternative"))
        await source.append_message(Message(role="assistant", content="new answer"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
                None,
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionForked) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert [
            message.content for message in completed.selected_session.read_context_messages()
        ] == ["first", "answer"]
        assert [message.content for message in fixture.session_state.history] == [
            "first",
            "answer",
        ]
        report = next(event for event in fixture.events if isinstance(event, RpcSessionForked))
        assert report.source_session_id == source.session_id
        assert report.source_active_leaf_id == source.read_active_leaf_id()
        assert report.selected_entry_id == selected.id
        assert report.selected_prompt == editable_prompt
        assert report.session_id == completed.selected_session.session_id
        assert report.active_leaf_id == completed.selected_session.read_active_leaf_id()

    anyio.run(scenario)


def test_executor_fork_session_reports_source_name_from_branch_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.set_name("Named Source")
        await source.append_message(Message(role="user", content="first"))
        answer = await source.append_message(Message(role="assistant", content="answer"))
        selected = await source.append_message(Message(role="user", content="edit me"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())

        def stale_source_name_read() -> str | None:
            raise AssertionError("fork event must not pre-read the source name")

        monkeypatch.setattr(source, "read_name", stale_source_name_read)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert completed.selected_session.read_name() is None
        assert fixture.session_state.name is None
        assert completed.selected_session.read_active_leaf_id() == answer.id
        report = next(event for event in fixture.events if isinstance(event, RpcSessionForked))
        assert report.source_session_name == "Named Source"
        assert report.session_name is None
        assert report.selected_entry_id == selected.id
        assert report.selected_prompt == "edit me"

    anyio.run(scenario)


def test_executor_first_message_fork_selects_reserved_empty_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        selected = await source.append_message(Message(role="user", content="edit first"))
        await source.append_message(Message(role="assistant", content="old answer"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert not completed.selected_session.path.exists()
        assert fixture.session_state.history == ()
        assert fixture.session_state.entry_count == 0
        report = next(event for event in fixture.events if isinstance(event, RpcSessionForked))
        assert report.active_leaf_id is None
        assert report.entry_count == 0
        assert report.selected_prompt == "edit first"

    anyio.run(scenario)


def test_executor_fork_rejects_a_reserved_empty_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        reserved = fixture.sessions.create()
        fixture.session_state.session = reserved
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {"id": "fork", "type": "fork_session", "entry_id": "entry"},
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert fixture.session_state.session is reserved
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "Cannot fork an empty session"

    anyio.run(scenario)


def test_executor_session_derivation_reports_validation_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            assert (
                await executor.dispatch({"id": [], "type": "clone_session"}, None)
            ).running_command is None
            assert (
                await executor.dispatch({"id": "clone", "type": "clone_session"}, None)
            ).running_command is None
            assert (
                await executor.dispatch(
                    {"id": "fork-empty", "type": "fork_session", "entry_id": ""},
                    None,
                )
            ).running_command is None
            assert (
                await executor.dispatch(
                    {"id": "fork", "type": "fork_session", "entry_id": "entry"},
                    None,
                )
            ).running_command is None

            empty = fixture.sessions.create()
            fixture.session_state.session = empty
            clone_empty = await executor.dispatch(
                {"id": "clone-empty", "type": "clone_session"},
                None,
            )
            clone_completed = await receive.receive()
            fixture.coordinator.running_command = clone_empty.running_command
            await fixture.coordinator.handle_event(
                clone_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert clone_completed.ok is False
        assert fixture.session_state.session is empty
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
            ("clone_session", False, "RPC command id must be a non-empty string"),
            (
                "clone_session",
                False,
                "RPC clone_session command requires a selected session",
            ),
            (
                "fork_session",
                False,
                "RPC fork_session command field entry_id must be a non-empty string",
            ),
            (
                "fork_session",
                False,
                "RPC fork_session command requires a selected session",
            ),
            ("clone_session", False, "Cannot clone an empty session"),
        ]
        assert not any(
            isinstance(event, RpcSessionCloned | RpcSessionForked) for event in fixture.events
        )

    anyio.run(scenario)


def test_executor_clone_rejects_concurrent_source_leaf_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="before"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = len(source.read_entries())
        original_clone = fixture.sessions.clone

        async def change_source_before_clone(
            selected: JsonlSession,
            *,
            expected_active_leaf_id: str | None,
        ) -> JsonlSession:
            assert selected is source
            await source.append_message(Message(role="assistant", content="concurrent"))
            return await original_clone(
                source,
                expected_active_leaf_id=expected_active_leaf_id,
            )

        monkeypatch.setattr(fixture.sessions, "clone", change_source_before_clone)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch({"id": "clone", "type": "clone_session"}, None)
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert completed.selected_session is None
        assert fixture.session_state.session is source
        assert [message.content for message in fixture.session_state.history] == ["before"]
        assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error is not None
        assert finished.error.startswith("Session tree changed: expected active leaf")

    anyio.run(scenario)


def test_executor_fork_rejects_non_user_and_missing_entries(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="one"))
        assistant = await source.append_message(Message(role="assistant", content="two"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = 2
        send, receive = anyio.create_memory_object_stream(10)
        completions: list[_RpcCommandCompleted] = []
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for index, entry_id in enumerate((assistant.id, "missing"), start=1):
                result = await executor.dispatch(
                    {
                        "id": f"fork-{index}",
                        "type": "fork_session",
                        "entry_id": entry_id,
                    },
                    None,
                )
                completed = await receive.receive()
                completions.append(completed)
                fixture.coordinator.running_command = result.running_command
                await fixture.coordinator.handle_event(
                    completed,
                    dispatch=preserve_running_command,
                    reject=reject_unexpected_command,
                )
            task_group.cancel_scope.cancel()

        assert all(not completion.ok for completion in completions)
        assert fixture.session_state.session is source
        assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
            f"Session fork entry must be a persisted user message: {assistant.id}",
            "Session fork entry must be a persisted user message: missing",
        ]
        assert not any(isinstance(event, RpcSessionForked) for event in fixture.events)

    anyio.run(scenario)


def test_executor_session_tree_reports_empty_and_bounded_selected_pages(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            empty = await _dispatch_parsed(
                executor,
                {"id": "tree-empty", "type": "get_session_tree"},
            )
            empty_completed = await receive.receive()
            fixture.coordinator.running_command = empty.running_command
            await fixture.coordinator.handle_event(
                empty_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )

            session = fixture.sessions.create()
            fixture.session_state.session = session
            reserved = await _dispatch_parsed(
                executor,
                {"id": "tree-reserved", "type": "get_session_tree"},
            )
            reserved_completed = await receive.receive()
            fixture.coordinator.running_command = reserved.running_command
            await fixture.coordinator.handle_event(
                reserved_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            first = await session.append_message(Message(role="user", content="first"))
            await session.append_message(Message(role="assistant", content="answer"))
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 2
            selected = await _dispatch_parsed(
                executor,
                {"id": "tree-selected", "type": "get_session_tree", "limit": 1},
            )
            selected_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        reports = [event for event in fixture.events if isinstance(event, RpcSessionTreeReported)]
        assert empty_completed.ok is True
        assert reports[0].session_id is None
        assert reports[0].session_path is None
        assert reports[0].nodes == ()
        assert reports[0].total_node_count == 0
        assert reserved_completed.ok is True
        assert reports[1].session_id == session.session_id
        assert reports[1].session_path == session.path
        assert reports[1].active_leaf_id is None
        assert reports[1].nodes == ()
        assert reports[1].total_node_count == 0
        assert selected.running_command is not None
        assert selected.running_command.command_type == "get_session_tree"
        assert selected_completed.ok is True
        assert selected_completed.history == session.read_context_messages()
        assert selected_completed.entry_count == 2
        assert reports[2].session_id == session.session_id
        assert reports[2].session_path == session.path
        assert reports[2].total_node_count == 2
        assert [node.entry_id for node in reports[2].nodes] == [first.id]
        assert reports[2].truncated is True
        assert reports[2].next_after_entry_id == first.id

    anyio.run(scenario)


def test_executor_navigation_applies_history_before_reporting_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        editable = "edit this exact prompt 漢🙂"
        selected = await session.append_message(Message(role="user", content=editable))
        await session.append_message(Message(role="assistant", content="old answer"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 4
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await executor.dispatch(
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
                None,
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "navigate_session_tree"
        assert completed.ok is True
        assert [message.content for message in fixture.session_state.history] == [
            "first",
            "answer",
        ]
        assert fixture.session_state.entry_count == 5
        assert session.read_active_leaf_id() == answer.id
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionTreeNavigated,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionTreeNavigated)
        assert report.session_id == session.session_id
        assert report.session_path == session.path
        assert report.selected_entry_id == selected.id
        assert report.previous_active_leaf_id is not None
        assert report.active_leaf_id == answer.id
        assert report.editor_text == editable
        assert report.changed is True
        assert report.entry_count == 5

    anyio.run(scenario)


def test_executor_unrevert_applies_history_before_reporting_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        original_history = session.read_context_messages()
        await session.navigate_tree(
            selected.id,
            expected_active_leaf_id=leaf.id,
            operation_id="navigate",
        )
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {"id": "unrevert", "type": "unrevert_session_tree"},
                None,
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert result.running_command.command_type == "unrevert_session_tree"
        assert completed.ok is True
        assert fixture.session_state.history == original_history
        assert fixture.session_state.entry_count == 6
        assert session.read_active_leaf_id() == leaf.id
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcSessionTreeUnreverted,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcSessionTreeUnreverted)
        assert report.previous_active_leaf_id == answer.id
        assert report.active_leaf_id == leaf.id
        assert report.entry_count == 6

    anyio.run(scenario)


def test_executor_unrevert_requires_selected_session_and_eligible_navigation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            assert (
                await executor.dispatch(
                    {"id": "unrevert-none", "type": "unrevert_session_tree"},
                    None,
                )
            ).running_command is None
            session = fixture.sessions.create()
            await session.append_message(Message(role="user", content="first"))
            fixture.session_state.session = session
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 1
            result = await executor.dispatch(
                {"id": "unrevert-missing", "type": "unrevert_session_tree"},
                None,
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert completed.ok is False
        assert fixture.session_state.entry_count == 1
        assert len(session.read_entries()) == 1
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
            "RPC unrevert_session_tree command requires an existing persisted session",
            "No explicit session-tree navigation is available to unrevert",
        ]

    anyio.run(scenario)


def test_executor_unrevert_rejects_concurrent_leaf_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        previous_history = session.read_context_messages()
        previous_leaf_id = session.read_active_leaf_id()
        fixture.session_state.session = session
        fixture.session_state.history = previous_history
        fixture.session_state.entry_count = 5
        original_unrevert = session.unrevert_tree

        async def change_before_unrevert(
            *,
            expected_active_leaf_id: str | None,
            operation_id: str | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> object:
            await session.append_message(Message(role="assistant", content="concurrent"))
            return await original_unrevert(
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=operation_id,
                cancel_requested=cancel_requested,
            )

        monkeypatch.setattr(session, "unrevert_tree", change_before_unrevert)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {"id": "unrevert", "type": "unrevert_session_tree"},
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert fixture.session_state.history == previous_history
        assert fixture.session_state.entry_count == 5
        assert session.read_active_leaf_id() != previous_leaf_id
        assert not any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error is not None
        assert finished.error.startswith("Session tree changed: expected active leaf")

    anyio.run(scenario)


def test_executor_unrevert_cancellation_before_commit_preserves_leaf(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            async with session._append_lock:
                result = await executor.dispatch(
                    {"id": "unrevert", "type": "unrevert_session_tree"},
                    None,
                )
                assert result.running_command is not None
                while session._append_lock.statistics().tasks_waiting == 0:
                    await anyio.sleep(0)
                result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert session.read_active_leaf_id() == answer.id
        assert fixture.session_state.entry_count == 5
        assert not any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC unrevert_session_tree command cancelled"

    anyio.run(scenario)


def test_executor_unrevert_cancellation_after_commit_reports_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        original_history = session.read_context_messages()
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        original_selected_state = rpc_execution_module.rpc_selected_session_state
        committed = threading.Event()
        release = threading.Event()

        def pause_after_unrevert(selected_session: JsonlSession) -> object:
            committed.set()
            assert release.wait(timeout=5)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_execution_module,
            "rpc_selected_session_state",
            pause_after_unrevert,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {"id": "unrevert", "type": "unrevert_session_tree"},
                None,
            )
            assert result.running_command is not None
            assert await anyio.to_thread.run_sync(lambda: committed.wait(timeout=5))
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert session.read_active_leaf_id() == leaf.id
        assert fixture.session_state.history == original_history
        assert fixture.session_state.entry_count == 6
        assert any(isinstance(event, RpcSessionTreeUnreverted) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True
        assert finished.error is None

    anyio.run(scenario)


def test_executor_unrevert_reports_committed_leaf_after_concurrent_navigation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 5
        original_selected_state = rpc_execution_module.rpc_selected_session_state
        concurrent_session = JsonlSession(session_id=session.session_id, path=session.path)

        async def supersede_unrevert() -> None:
            await concurrent_session.select_active_leaf(
                answer.id,
                expected_active_leaf_id=leaf.id,
            )

        def navigate_after_unrevert(selected_session: JsonlSession) -> object:
            anyio.run(supersede_unrevert)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_execution_module,
            "rpc_selected_session_state",
            navigate_after_unrevert,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {"id": "unrevert", "type": "unrevert_session_tree"},
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert session.read_active_leaf_id() == answer.id
        assert fixture.session_state.history == session.read_context_messages()
        assert fixture.session_state.entry_count == 7
        event = next(
            event for event in fixture.events if isinstance(event, RpcSessionTreeUnreverted)
        )
        assert event.previous_active_leaf_id == answer.id
        assert event.active_leaf_id == leaf.id

    anyio.run(scenario)


def test_executor_session_tree_reports_validation_and_lookup_failures(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        invalid_commands: list[dict[str, object]] = [
            {"id": "navigate-empty", "type": "navigate_session_tree", "entry_id": ""},
            {"id": "navigate-none", "type": "navigate_session_tree", "entry_id": "entry"},
        ]
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for command in invalid_commands:
                assert (await executor.dispatch(command, None)).running_command is None

            no_session_cursor = await _dispatch_parsed(
                executor,
                {
                    "id": "no-session-cursor",
                    "type": "get_session_tree",
                    "after_entry_id": "missing",
                },
            )
            no_session_cursor_completed = await receive.receive()
            fixture.coordinator.running_command = no_session_cursor.running_command
            await fixture.coordinator.handle_event(
                no_session_cursor_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            session = fixture.sessions.create()
            await session.append_message(Message(role="user", content="one"))
            fixture.session_state.session = session
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 1
            unknown_cursor = await _dispatch_parsed(
                executor,
                {
                    "id": "unknown-cursor",
                    "type": "get_session_tree",
                    "after_entry_id": "missing",
                },
            )
            cursor_completed = await receive.receive()
            fixture.coordinator.running_command = unknown_cursor.running_command
            await fixture.coordinator.handle_event(
                cursor_completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            await executor.dispatch(
                {
                    "id": "missing-entry",
                    "type": "navigate_session_tree",
                    "entry_id": "missing",
                },
                None,
            )
            entry_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert no_session_cursor_completed.ok is False
        assert cursor_completed.ok is False
        assert entry_completed.ok is False
        assert fixture.session_state.history == session.read_context_messages()
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
            ("RPC navigate_session_tree command field entry_id must be a non-empty string"),
            ("RPC navigate_session_tree command requires an existing persisted session"),
            "Session tree cursor not found: missing",
            "Session tree cursor not found: missing",
            "Session tree entry not found: missing",
        ]

    anyio.run(scenario)


def test_executor_navigation_rejects_concurrent_leaf_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        selected = await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="answer"))
        previous_history = session.read_context_messages()
        fixture.session_state.session = session
        fixture.session_state.history = previous_history
        fixture.session_state.entry_count = 2
        original_navigate = session.navigate_tree

        async def change_before_navigation(
            entry_id: str,
            *,
            expected_active_leaf_id: str | None,
            operation_id: str | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> SessionTreeNavigation:
            await session.append_message(Message(role="assistant", content="concurrent"))
            return await original_navigate(
                entry_id,
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=operation_id,
                cancel_requested=cancel_requested,
            )

        monkeypatch.setattr(session, "navigate_tree", change_before_navigation)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
                None,
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert fixture.session_state.history == previous_history
        assert fixture.session_state.entry_count == 2
        assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error is not None
        assert finished.error.startswith("Session tree changed: expected active leaf")

    anyio.run(scenario)


def test_executor_navigation_cancellation_before_commit_preserves_leaf(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        selected = await session.append_message(Message(role="user", content="one"))
        active = await session.append_message(Message(role="assistant", content="answer"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 2
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            async with session._append_lock:
                result = await executor.dispatch(
                    {
                        "id": "navigate",
                        "type": "navigate_session_tree",
                        "entry_id": selected.id,
                    },
                    None,
                )
                assert result.running_command is not None
                while session._append_lock.statistics().tasks_waiting == 0:
                    await anyio.sleep(0)
                result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert session.read_active_leaf_id() == active.id
        assert fixture.session_state.entry_count == 2
        assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC navigate_session_tree command cancelled"

    anyio.run(scenario)


def test_executor_navigation_cancellation_after_commit_reports_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        selected = await session.append_message(Message(role="user", content="one"))
        await session.append_message(Message(role="assistant", content="answer"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 2
        original_selected_state = rpc_execution_module.rpc_selected_session_state
        committed = threading.Event()
        release = threading.Event()

        def pause_after_navigation(
            selected_session: JsonlSession,
        ) -> tuple[int, tuple[Message, ...], str | None]:
            committed.set()
            assert release.wait(timeout=5)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_execution_module,
            "rpc_selected_session_state",
            pause_after_navigation,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
                None,
            )
            assert result.running_command is not None
            assert await anyio.to_thread.run_sync(lambda: committed.wait(timeout=5))
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert session.read_active_leaf_id() is None
        assert fixture.session_state.history == ()
        assert fixture.session_state.entry_count == 3
        report = next(
            event for event in fixture.events if isinstance(event, RpcSessionTreeNavigated)
        )
        assert report.changed is True
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True
        assert finished.error is None

    anyio.run(scenario)


def test_executor_navigation_cancellation_during_no_op_reports_cancelled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        await session.append_message(Message(role="user", content="one"))
        active = await session.append_message(Message(role="assistant", content="answer"))
        previous_history = session.read_context_messages()
        fixture.session_state.session = session
        fixture.session_state.history = previous_history
        fixture.session_state.entry_count = 2
        original_navigate = session.navigate_tree
        no_op_complete = anyio.Event()
        release = anyio.Event()

        async def pause_after_no_op(
            entry_id: str,
            *,
            expected_active_leaf_id: str | None,
            operation_id: str | None = None,
            cancel_requested: Callable[[], bool] | None = None,
        ) -> SessionTreeNavigation:
            result = await original_navigate(
                entry_id,
                expected_active_leaf_id=expected_active_leaf_id,
                operation_id=operation_id,
                cancel_requested=cancel_requested,
            )
            assert result.changed is False
            no_op_complete.set()
            await release.wait()
            return result

        monkeypatch.setattr(session, "navigate_tree", pause_after_no_op)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch(
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": active.id,
                },
                None,
            )
            assert result.running_command is not None
            await no_op_complete.wait()
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert session.read_active_leaf_id() == active.id
        assert fixture.session_state.history == previous_history
        assert fixture.session_state.entry_count == 2
        assert not any(isinstance(event, RpcSessionTreeNavigated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is False
        assert finished.error == "RPC navigate_session_tree command cancelled"

    anyio.run(scenario)


def test_executor_clone_cancellation_before_commit_preserves_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="before"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = 1
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch({"id": "clone", "type": "clone_session"}, None)
            assert result.running_command is not None
            result.running_command.cancel_scope.cancel()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is False
        assert completed.selected_session is None
        assert fixture.session_state.session is source
        assert tuple(tmp_path.glob("*.jsonl")) == (source.path,)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC clone_session command cancelled"

    anyio.run(scenario)


def test_executor_clone_cancellation_after_commit_reports_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        source = fixture.sessions.create()
        await source.append_message(Message(role="user", content="before"))
        fixture.session_state.session = source
        fixture.session_state.history = source.read_context_messages()
        fixture.session_state.entry_count = 1
        original_clone = fixture.sessions.clone
        published = anyio.Event()
        release = anyio.Event()

        async def pause_after_clone(
            selected: JsonlSession,
            *,
            expected_active_leaf_id: str | None,
        ) -> JsonlSession:
            cloned = await original_clone(
                selected,
                expected_active_leaf_id=expected_active_leaf_id,
            )
            published.set()
            await release.wait()
            return cloned

        monkeypatch.setattr(fixture.sessions, "clone", pause_after_clone)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch({"id": "clone", "type": "clone_session"}, None)
            assert result.running_command is not None
            await published.wait()
            result.running_command.cancel_scope.cancel()
            release.set()
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()

        assert completed.ok is True
        assert completed.selected_session is fixture.session_state.session
        assert completed.selected_session is not None
        assert completed.selected_session.session_id != source.session_id
        assert len(tuple(tmp_path.glob("*.jsonl"))) == 2
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True
        assert finished.error is None

    anyio.run(scenario)


def test_executor_queue_commands_delegate_and_report_removed_items(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected_session = fixture.sessions.create()
        fixture.session_state.session = selected_session
        calls: list[tuple[object, ...]] = []

        def state(session: object = None) -> QueueUpdated:
            calls.append(("state", session))
            return QueueUpdated(steering=("one", "two"), follow_up=("later",))

        async def steer(content: str) -> QueueUpdated:
            calls.append(("steer", content))
            return QueueUpdated(steering=(content,))

        async def follow_up(content: str) -> QueueUpdated:
            calls.append(("follow_up", content))
            return QueueUpdated(follow_up=(content,))

        def set_mode(kind: object, mode: object) -> QueueUpdated:
            calls.append(("set_queue_mode", kind, mode))
            return QueueUpdated(steering_mode="all")

        def pop(kind: object) -> tuple[Message, QueueUpdated]:
            calls.append(("pop_queue", kind))
            return Message(role="user", content="two"), QueueUpdated(steering=("one",))

        def clear(kind: object = None) -> tuple[QueuedMessages, QueueUpdated]:
            calls.append(("clear_queue", kind))
            return (
                QueuedMessages(follow_up=(Message(role="user", content="later"),)),
                QueueUpdated(),
            )

        monkeypatch.setattr(fixture.agent, "queue_state", state)
        monkeypatch.setattr(fixture.agent, "steer", steer)
        monkeypatch.setattr(fixture.agent, "follow_up", follow_up)
        monkeypatch.setattr(fixture.agent, "set_queue_mode", set_mode)
        monkeypatch.setattr(fixture.agent, "pop_queue", pop)
        monkeypatch.setattr(fixture.agent, "clear_queue", clear)

        commands: list[dict[str, object]] = [
            {"id": "state", "type": "get_queue_state"},
            {"id": "steer", "type": "steer", "content": "redirect"},
            {"id": "follow", "type": "follow_up", "content": "continue"},
            {
                "id": "mode",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "all",
            },
            {"id": "pop", "type": "pop_queue", "kind": "steering"},
            {"id": "clear", "type": "clear_queue", "kind": "follow_up"},
        ]
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            results = [await executor.dispatch(command, running) for command in commands]
            task_group.cancel_scope.cancel()

        assert all(result.running_command is running for result in results)
        assert calls == [
            ("state", selected_session),
            ("steer", "redirect"),
            ("follow_up", "continue"),
            ("set_queue_mode", "steering", "all"),
            ("pop_queue", "steering"),
            ("clear_queue", "follow_up"),
        ]
        removed = [event for event in fixture.events if isinstance(event, QueueItemsRemoved)]
        assert [
            (
                event.command_id,
                event.operation,
                event.kind,
                event.steering,
                event.follow_up,
            )
            for event in removed
        ] == [
            ("pop", "pop", "steering", ("two",), ()),
            ("clear", "clear", "follow_up", (), ("later",)),
        ]
        assert all(event.ok for event in fixture.events if isinstance(event, RpcCommandFinished))

    anyio.run(scenario)


def test_executor_rejects_invalid_raw_queue_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        commands: list[dict[str, object]] = [
            {"id": "steer", "type": "steer"},
            {"id": "mode-kind", "type": "set_queue_mode", "kind": "unknown", "mode": "all"},
            {
                "id": "mode-value",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": "invalid",
            },
            {"id": "pop", "type": "pop_queue"},
            {"id": "clear", "type": "clear_queue", "kind": "unknown"},
            {
                "id": "mode-kind-container",
                "type": "set_queue_mode",
                "kind": [],
                "mode": "all",
            },
            {
                "id": "mode-value-container",
                "type": "set_queue_mode",
                "kind": "steering",
                "mode": {},
            },
            {"id": "pop-container", "type": "pop_queue", "kind": []},
            {"id": "clear-container", "type": "clear_queue", "kind": {}},
        ]
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for command in commands:
                await executor.dispatch(command, None)
            await executor.dispatch({"id": "state", "type": "get_queue_state"}, None)
            task_group.cancel_scope.cancel()

        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        errors = [event.error for event in finished if not event.ok]
        assert errors == [
            "RPC steer command requires string field: content",
            "RPC set_queue_mode command field kind must be 'steering' or 'follow_up'",
            "RPC set_queue_mode command field mode must be 'one_at_a_time' or 'all'",
            "RPC pop_queue command field kind must be 'steering' or 'follow_up'",
            "RPC clear_queue command field kind must be 'steering' or 'follow_up'",
            "RPC set_queue_mode command field kind must be 'steering' or 'follow_up'",
            "RPC set_queue_mode command field mode must be 'one_at_a_time' or 'all'",
            "RPC pop_queue command field kind must be 'steering' or 'follow_up'",
            "RPC clear_queue command field kind must be 'steering' or 'follow_up'",
        ]
        assert (finished[-1].command_id, finished[-1].ok) == ("state", True)

    anyio.run(scenario)


def test_executor_reports_empty_pop_and_clear_as_success(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(
            fixture.agent,
            "pop_queue",
            lambda _kind: (None, QueueUpdated()),
        )
        monkeypatch.setattr(
            fixture.agent,
            "clear_queue",
            lambda _kind=None: (QueuedMessages(), QueueUpdated()),
        )
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch(
                {"id": "pop", "type": "pop_queue", "kind": "steering"},
                running,
            )
            await executor.dispatch({"id": "clear", "type": "clear_queue"}, running)
            task_group.cancel_scope.cancel()

        removed = [event for event in fixture.events if isinstance(event, QueueItemsRemoved)]
        assert [
            (event.command_id, event.kind, event.steering, event.follow_up) for event in removed
        ] == [
            ("pop", "steering", (), ()),
            ("clear", None, (), ()),
        ]
        assert [
            (event.command_id, event.ok)
            for event in fixture.events
            if isinstance(event, RpcCommandFinished)
        ] == [("pop", True), ("clear", True)]

    anyio.run(scenario)


def test_executor_routes_queued_cancellation_through_coordinator(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = WispConfig(provider="fake", session_dir=tmp_path)
        runtime = await build_runtime(
            auth_path=config.auth_path,
            retry_policy=config.retry_policy,
        )
        sessions = JsonlSessionStore(tmp_path)
        agent = CodingSession(provider=runtime.providers.get("fake"), sessions=sessions)
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        coordinator.queued_commands.append(_parsed_command({"id": "queued", "type": "prompt"}))
        events: list[WispEvent] = []

        async def render_events(stream: AsyncIterator[WispEvent]) -> None:
            async for event in stream:
                events.append(event)

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = RpcCommandExecutor(
                agent=agent,
                runtime=runtime,
                sessions=sessions,
                session_state=state,
                task_group=task_group,
                send=send,
                approval_policy=_ApprovalResolver(),
                trust_gate=_TrustResolver(),
                configure_overrides=_RpcConfigureOverrides(),
                coordinator=coordinator,
                write_event=events.append,
                render_events=render_events,
            )

            result = await executor.dispatch(
                {"id": "cancel", "type": "cancel", "target_id": "queued"},
                None,
            )

            assert result.running_command is None
            assert not coordinator.queued_commands
            task_group.cancel_scope.cancel()

        finished = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok) for event in finished] == [
            ("queued", False),
            ("cancel", True),
        ]

    anyio.run(scenario)


def test_executor_synchronizes_running_command_before_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = WispConfig(provider="fake", session_dir=tmp_path)
        runtime = await build_runtime(
            auth_path=config.auth_path,
            retry_policy=config.retry_policy,
        )
        sessions = JsonlSessionStore(tmp_path)
        agent = CodingSession(provider=runtime.providers.get("fake"), sessions=sessions)
        state = _RpcSessionState(None, (), 0)
        coordinator = RpcCoordinator(state)
        events: list[WispEvent] = []
        active_scope = anyio.CancelScope()
        running = _RpcRunningCommand("active", "prompt", active_scope)

        async def render_events(stream: AsyncIterator[WispEvent]) -> None:
            async for event in stream:
                events.append(event)

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = RpcCommandExecutor(
                agent=agent,
                runtime=runtime,
                sessions=sessions,
                session_state=state,
                task_group=task_group,
                send=send,
                approval_policy=_ApprovalResolver(),
                trust_gate=_TrustResolver(),
                configure_overrides=_RpcConfigureOverrides(),
                coordinator=coordinator,
                write_event=events.append,
                render_events=render_events,
            )

            await executor.dispatch(
                {"id": "cancel", "type": "cancel", "target_id": "active"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert active_scope.cancel_called is True
        assert coordinator.running_command is running
        finished = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok) for event in finished] == [("cancel", True)]

    anyio.run(scenario)
