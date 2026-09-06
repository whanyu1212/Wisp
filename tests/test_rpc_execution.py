from __future__ import annotations

import json
import shutil
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

import anyio
import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from tests.rpc_support import (
    RpcExecutorFixture,
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
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
    SessionStatsReported,
    ToolCallSnapshot,
    ToolExecutionEnded,
    WispEvent,
)
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.providers.base import ToolSpec
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.fake import FakeProvider
from wisp.rpc import execution as rpc_execution_module
from wisp.rpc import lifecycle as rpc_lifecycle_module
from wisp.rpc import session_mutation as rpc_session_mutation_module
from wisp.rpc.commands import (
    MAX_RPC_COMMAND_TYPE_CHARS,
    ApprovalCommand,
    CancelCommand,
    ConfigureCommand,
    GetModelCatalogCommand,
    ParsedRpcCommand,
    RpcCommandAdapter,
    StoreApiKeyCommand,
    TrustCommand,
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
)
from wisp.rpc.host import RpcHost
from wisp.rpc.lifecycle import _MAX_RPC_COMMAND_ERROR_CHARS
from wisp.rpc.session_state import rpc_selected_session_state
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
    except ValidationError as exc:
        if isinstance(payload.get("type"), str) and any(
            error["type"] == "union_tag_invalid" for error in exc.errors(include_input=False)
        ):
            return ParsedRpcCommand.from_unknown(payload)
        raise
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


def test_executor_bounds_unknown_types_before_lifecycle_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch_parsed(
                ParsedRpcCommand.from_unknown(
                    {"id": "command-1", "type": "x" * (MAX_RPC_COMMAND_TYPE_CHARS + 1)}
                ),
                None,
            )
            assert result.should_shutdown is False
            task_group.cancel_scope.cancel()
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        assert all(
            event.command_type == "unknown"
            for event in fixture.events
            if isinstance(event, (RpcCommandStarted, RpcCommandFinished))
        )

    anyio.run(scenario)


def test_rpc_command_errors_bound_echoed_reference_fields() -> None:
    events: list[WispEvent] = []
    oversized_reference = "x" * (_MAX_RPC_COMMAND_ERROR_CHARS + 1)

    rpc_execution_module.handle_rpc_approval_command(
        ApprovalCommand(id="approval-1", call_id=oversized_reference, approved=True),
        command_id="approval-1",
        command_type="approval",
        approval_policy=_ApprovalResolver(),
        write_event=events.append,
    )

    assert len(events) == 2
    error, finished = events
    assert isinstance(error, ErrorEvent)
    assert isinstance(finished, RpcCommandFinished)
    assert len(error.message) == _MAX_RPC_COMMAND_ERROR_CHARS
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
        ApprovalCommand(id="approval-1", call_id="call-1", approved=True),
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
        ApprovalCommand(id="approval-1", call_id="call-1", approved=True),
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
        CancelCommand(id="cancel-1", target_id="prompt-1"),
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
            result = await _dispatch_parsed(executor, {"id": "init-1", "type": "init"}, None)
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
            result = await _dispatch_parsed(
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
            await _dispatch_parsed(
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
        _enable_project_init(fixture, project)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
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
        _enable_project_init(fixture, project)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
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
            plan_result = await _dispatch_parsed(
                executor, {"id": "plan-init", "type": "init"}, None
            )
            fixture.agent.mode = "build"
            no_write_result = await _dispatch_parsed(
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
            result = await _dispatch_parsed(
                executor, {"id": "prompt-1", "type": "prompt", "prompt": "hello"}, None
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
            result = await _dispatch_parsed(
                executor, {"id": "prompt-1", "type": "prompt", "prompt": "hello"}, None
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

            state_result = await _dispatch_parsed(
                executor, {"id": "state", "type": "get_queue_state"}, None
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
            results = [await _dispatch_parsed(executor, command) for command in mutation_commands]
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

            result = await _dispatch_parsed(
                executor, {"id": "state-1", "type": "get_state"}, running
            )
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

            rejected = await _dispatch_parsed(
                executor,
                {"id": "new-busy", "type": "new_session"},
                busy,
            )
            accepted = await _dispatch_parsed(
                executor,
                {"id": "new-1", "type": "new_session"},
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
            await _dispatch_parsed(executor, {"id": "state-1", "type": "get_state"}, None)
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


@pytest.mark.parametrize(
    ("command_type", "report_type"),
    [
        ("get_state", RpcStateReported),
        ("get_commands", RpcCommandsReported),
        ("get_model_catalog", RpcModelCatalogReported),
        ("get_connection_catalog", RpcConnectionCatalogReported),
        ("get_skills", RpcSkillsReported),
        ("get_mcp_status", RpcMcpStatusReported),
    ],
)
@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "inspection-1"}])
@pytest.mark.parametrize("active", [False, True])
def test_executor_typed_inspection_preserves_identity_and_lifecycle(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    command_type: str,
    report_type: type[WispEvent],
    id_fields: dict[str, object],
    active: bool,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope()) if active else None
        parsed = _parsed_command({"type": command_type, **id_fields})
        assert parsed.known is not None

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch_parsed(parsed, running)
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert fixture.coordinator.running_command is running
        assert result.selected_session is None
        assert not result.reset_session
        assert not result.should_shutdown
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            report_type,
            RpcCommandFinished,
        ]
        started, report, finished = fixture.events
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_type == finished.command_type == command_type
        assert started.command_id
        assert report.model_dump()["command_id"] == started.command_id == finished.command_id
        if id_fields.get("id") is not None:
            assert started.command_id == id_fields["id"]
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_parsed_entry_preserves_unknown_fallback(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await executor.dispatch_parsed(
                ParsedRpcCommand.from_unknown({"id": "future-1", "type": "future_command"}),
                None,
            )
            task_group.cancel_scope.cancel()

        assert [type(event) for event in fixture.events[-3:]] == [
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
        parsed_commands = []
        for payload in buffered_commands:
            try:
                parsed_commands.append(_parsed_command(payload))
            except ValidationError:
                # Deliberately injected internal corruption, never accepted at ingress.
                parsed_commands.append(ParsedRpcCommand.from_unknown(payload))
        fixture.coordinator.pending_prompt_queue_commands.extend(parsed_commands)
        running = _RpcRunningCommand("prompt", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            result = await _dispatch_parsed(
                executor, {"id": "state-1", "type": "get_state"}, running
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        report = next(event for event in fixture.events if isinstance(event, RpcStateReported))
        assert report.state.pending_steering_count == 2
        assert report.state.pending_follow_up_count == 0
        assert report.state.steering_mode == "all"
        assert report.state.follow_up_mode == "one_at_a_time"
        assert list(fixture.coordinator.pending_prompt_queue_commands) == parsed_commands

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

            idle = await _dispatch_parsed(executor, {"id": "idle", "type": "get_state"}, None)

            def fail_snapshot(_session: object = None) -> object:
                raise RuntimeError("snapshot failed")

            monkeypatch.setattr(fixture.agent, "state_snapshot", fail_snapshot)
            failed = await _dispatch_parsed(executor, {"id": "failed", "type": "get_state"}, None)
            task_group.cancel_scope.cancel()

        assert idle.running_command is None
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

            result = await _dispatch_parsed(
                executor, {"id": "commands-1", "type": "get_commands"}, running
            )
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
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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


@pytest.mark.parametrize("fail_catalog", [False, True])
def test_executor_typed_connection_catalog_never_reports_credentials(
    tmp_path: Path, monkeypatch: MonkeyPatch, fail_catalog: bool
) -> None:
    secret = "sentinel-inspection-secret"

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        store.set("anthropic", ApiKeyCredential(key=secret))
        if fail_catalog:

            def fail_snapshot(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
                raise RuntimeError(secret)

            monkeypatch.setattr(
                rpc_execution_module, "rpc_connection_catalog_snapshot", fail_snapshot
            )

        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"type": "get_connection_catalog", "id": "connections-1"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent if fail_catalog else RpcConnectionCatalogReported,
            RpcCommandFinished,
        ]
        assert secret not in "\n".join(event.model_dump_json() for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "connections-1"
        assert finished.command_type == "get_connection_catalog"
        assert finished.ok is not fail_catalog
        assert finished.error == ("Provider connection failed" if fail_catalog else None)

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command_type", ["store_api_key", "disconnect_provider", "begin_device_code"]
)
@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "connection-1"}])
def test_typed_connection_commands_preserve_lifecycle_without_legacy_conversion(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str, id_fields: dict[str, object]
) -> None:
    async def fake_login(
        *, on_device_code: Callable[[DeviceCodeInfo], None], on_progress: Callable[[int], None]
    ) -> OAuthCredential:
        on_device_code(DeviceCodeInfo("CODE-123", "https://example.test/device", 1, 900))
        on_progress(1)
        return OAuthCredential(
            access="sentinel-access", refresh="sentinel-refresh", expires=4_102_444_800_000
        )

    monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        store.set("anthropic", ApiKeyCredential(key="old-key"))
        payload: dict[str, object] = {
            "type": command_type,
            "provider": "openai-codex" if command_type == "begin_device_code" else "anthropic",
            **id_fields,
        }
        if command_type == "store_api_key":
            payload["api_key"] = "  sentinel-new-key \t"
        parsed = _parsed_command(payload)
        assert parsed.known is not None
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch_parsed(parsed, None)
            if command_type == "begin_device_code":
                with anyio.fail_after(2):
                    completed = await receive.receive()
                assert isinstance(completed, _RpcCommandCompleted)
                assert result.running_command is not None
                assert completed.command_id == result.running_command.command_id
                assert completed.history == fixture.session_state.history
                assert completed.entry_count == fixture.session_state.entry_count
                assert completed.ok is True
            else:
                assert result.running_command is None
            task_group.cancel_scope.cancel()
        expected: list[type[WispEvent]] = [RpcCommandStarted]
        if command_type == "begin_device_code":
            expected.extend([RpcDeviceCodeReported, RpcDeviceCodeProgressReported])
        expected.extend([RpcConnectionCatalogReported, RpcCommandFinished])
        assert [type(event) for event in fixture.events] == expected
        started = fixture.events[0]
        finished = fixture.events[-1]
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_id
        assert started.command_type == finished.command_type == command_type
        assert all(
            event.model_dump()["command_id"] == started.command_id for event in fixture.events
        )
        if id_fields.get("id") is not None:
            assert started.command_id == id_fields["id"]
        assert finished.ok is True
        if command_type == "store_api_key":
            assert store.get("anthropic") == ApiKeyCredential(key="sentinel-new-key")
        elif command_type == "disconnect_provider":
            assert store.get("anthropic") is None
        else:
            assert store.get("openai-codex") == OAuthCredential(
                access="sentinel-access", refresh="sentinel-refresh", expires=4_102_444_800_000
            )
        rendered = repr(parsed) + "".join(event.model_dump_json() for event in fixture.events)
        assert all(
            secret not in rendered
            for secret in ("sentinel-new-key", "sentinel-access", "sentinel-refresh")
        )

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command_type", ["store_api_key", "disconnect_provider", "begin_device_code"]
)
@pytest.mark.parametrize("failure", ["missing-store", "storage-error"])
def test_typed_connection_storage_failures_preserve_credentials(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str, failure: str
) -> None:
    async def fake_login(**_kwargs: object) -> OAuthCredential:
        return OAuthCredential(
            access="sentinel-new-access", refresh="sentinel-new-refresh", expires=4_102_444_800_000
        )

    def fail_mutation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sentinel-storage-secret")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        provider = "openai-codex" if command_type == "begin_device_code" else "anthropic"
        existing = ApiKeyCredential(key="sentinel-old-key")
        store.set(provider, existing)
        if failure == "missing-store":
            fixture.runtime = replace(fixture.runtime, auth_store=None)
        else:
            monkeypatch.setattr(
                store, "delete" if command_type == "disconnect_provider" else "set", fail_mutation
            )
        monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", fake_login)
        payload: dict[str, object] = {"type": command_type, "provider": provider, "id": "failure-1"}
        if command_type == "store_api_key":
            payload["api_key"] = "sentinel-new-key"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send), payload
            )
            if result.running_command is not None:
                with anyio.fail_after(2):
                    await receive.receive()
            task_group.cancel_scope.cancel()
        assert store.get(provider) == existing
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "failure-1"
        assert finished.ok is False
        assert finished.error == (
            f"RPC {command_type} command requires an auth store"
            if failure == "missing-store"
            else "Provider connection failed"
        )
        assert "sentinel-" not in "".join(event.model_dump_json() for event in fixture.events)

    anyio.run(scenario)


@pytest.mark.parametrize("provider", ["anthropic", "unsupported-provider"])
def test_typed_store_rejects_whitespace_before_provider_checks(
    tmp_path: Path, provider: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        existing = ApiKeyCredential(key="old-key")
        store.set(provider, existing)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "type": "store_api_key",
                    "id": "blank-1",
                    "provider": provider,
                    "api_key": " \t\n",
                },
            )
            task_group.cancel_scope.cancel()
        assert store.get(provider) == existing
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC store_api_key command requires a non-empty api_key"
        assert finished.ok is False

    anyio.run(scenario)


def test_executor_stores_api_key_without_leaking_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        secret = "sentinel-secret-key"
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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


@pytest.mark.parametrize("command_type", ["store_api_key", "begin_device_code"])
def test_typed_connection_rejects_unsupported_provider_without_side_effects(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unsupported providers must not start login or write credentials")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        monkeypatch.setattr(store, "set", unexpected)
        monkeypatch.setattr(rpc_execution_module, "login_openai_codex_device_code", unexpected)
        payload: dict[str, object] = {
            "type": command_type,
            "provider": "unsupported",
            "id": "unsupported-1",
        }
        if command_type == "store_api_key":
            payload["api_key"] = "sentinel-unsupported-key"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send), payload
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
        assert finished.ok is False
        assert finished.error == (
            "API-key connection is not supported for unsupported."
            if command_type == "store_api_key"
            else "OAuth connection is not supported for unsupported."
        )
        assert "sentinel-unsupported-key" not in "".join(
            event.model_dump_json() for event in fixture.events
        )

    anyio.run(scenario)


def test_executor_rejects_api_key_for_unregistered_openai_compatible_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        secret = "unregistered-provider-secret"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
                StoreApiKeyCommand(id="store-1", provider="local-models", api_key=secret),
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
            await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
            await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
            await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
            await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
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
        {"id": "store-blank", "type": "store_api_key", "provider": "unsupported", "api_key": "   "},
        {"id": "disconnect-1", "type": "disconnect_provider", "provider": "anthropic"},
        {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
    ],
)
def test_executor_rejects_connection_mutations_while_an_operation_is_active(
    tmp_path: Path,
    command: dict[str, object],
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        existing = ApiKeyCredential(key="old-key")
        store.set(str(command["provider"]), existing)

        def unexpected_side_effect(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Busy connection commands must not mutate credentials or start login")

        monkeypatch.setattr(store, "set", unexpected_side_effect)
        monkeypatch.setattr(store, "delete", unexpected_side_effect)
        monkeypatch.setattr(
            rpc_execution_module, "login_openai_codex_device_code", unexpected_side_effect
        )
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                dict(command),
                running,
            )
            task_group.cancel_scope.cancel()

        assert store.get(str(command["provider"])) == existing
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
        GetModelCatalogCommand(id="catalog-1"),
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

            result = await _dispatch_parsed(
                executor, {"id": "skills-1", "type": "get_skills"}, running
            )
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

            result = await _dispatch_parsed(
                executor, {"id": "mcp-1", "type": "get_mcp_status"}, running
            )
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


def test_executor_commands_reports_registry_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)

            def fail_commands() -> object:
                raise RuntimeError("registry failed")

            monkeypatch.setattr(fixture.runtime.commands, "all", fail_commands)
            failed = await _dispatch_parsed(
                executor, {"id": "commands-1", "type": "get_commands"}, None
            )
            task_group.cancel_scope.cancel()

        assert failed.running_command is None
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_type, event.ok, event.error) for event in finished] == [
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

            result = await _dispatch_parsed(
                executor,
                {"id": "select", "type": "select_session", "session_id": selected.session_id},
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

            result = await _dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "name": "  Alpha\r\nBeta  ",
                    "session_id": None,
                },
            )
            completed = await receive.receive()
            assert not any(isinstance(event, RpcSessionNameChanged) for event in fixture.events)
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            await _dispatch_parsed(executor, {"id": "state", "type": "get_state"}, None)
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

            result = await _dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": other.session_id,
                    "name": "Other",
                },
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

            result = await _dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": str(copied_path),
                    "name": "Copied",
                },
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

            result = await _dispatch_parsed(
                executor,
                {
                    "id": "name",
                    "type": "set_session_name",
                    "session_id": str(selected.path.resolve(strict=False)),
                    "name": "Renamed",
                },
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

            missing = await _dispatch_parsed(
                executor,
                {"id": "missing", "type": "select_session", "session_id": "missing"},
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

            result = await _dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
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

            result = await _dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
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

            result = await _dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
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

            result = await _dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
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

            result = await _dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": selected.id},
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

            result = await _dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": "entry"},
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

            clone_without_selection = await _dispatch_parsed(
                executor,
                {"id": "clone", "type": "clone_session"},
            )
            fork_without_selection = await _dispatch_parsed(
                executor,
                {"id": "fork", "type": "fork_session", "entry_id": "entry"},
            )
            assert clone_without_selection.running_command is None
            assert fork_without_selection.running_command is None

            empty = fixture.sessions.create()
            fixture.session_state.session = empty
            clone_empty = await _dispatch_parsed(
                executor,
                {"id": "clone-empty", "type": "clone_session"},
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
            (
                "clone_session",
                False,
                "RPC clone_session command requires a selected session",
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

            result = await _dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
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
                result = await _dispatch_parsed(
                    executor,
                    {
                        "id": f"fork-{index}",
                        "type": "fork_session",
                        "entry_id": entry_id,
                    },
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

            result = await _dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
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
            result = await _dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
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
                await _dispatch_parsed(
                    executor,
                    {"id": "unrevert-none", "type": "unrevert_session_tree"},
                )
            ).running_command is None
            session = fixture.sessions.create()
            await session.append_message(Message(role="user", content="first"))
            fixture.session_state.session = session
            fixture.session_state.history = session.read_context_messages()
            fixture.session_state.entry_count = 1
            result = await _dispatch_parsed(
                executor,
                {"id": "unrevert-missing", "type": "unrevert_session_tree"},
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
            result = await _dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
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
                result = await _dispatch_parsed(
                    executor,
                    {"id": "unrevert", "type": "unrevert_session_tree"},
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
        original_selected_state = rpc_session_mutation_module.rpc_selected_session_state
        committed = threading.Event()
        release = threading.Event()

        def pause_after_unrevert(selected_session: JsonlSession) -> object:
            committed.set()
            assert release.wait(timeout=5)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_session_mutation_module,
            "rpc_selected_session_state",
            pause_after_unrevert,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
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
        original_selected_state = rpc_session_mutation_module.rpc_selected_session_state
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
            rpc_session_mutation_module,
            "rpc_selected_session_state",
            navigate_after_unrevert,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(
                executor,
                {"id": "unrevert", "type": "unrevert_session_tree"},
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
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            navigate_without_session = await _dispatch_parsed(
                executor,
                {"id": "navigate-none", "type": "navigate_session_tree", "entry_id": "entry"},
            )
            assert navigate_without_session.running_command is None

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
            await _dispatch_parsed(
                executor,
                {
                    "id": "missing-entry",
                    "type": "navigate_session_tree",
                    "entry_id": "missing",
                },
            )
            entry_completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert no_session_cursor_completed.ok is False
        assert cursor_completed.ok is False
        assert entry_completed.ok is False
        assert fixture.session_state.history == session.read_context_messages()
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [event.error for event in finished] == [
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
            result = await _dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
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
                result = await _dispatch_parsed(
                    executor,
                    {
                        "id": "navigate",
                        "type": "navigate_session_tree",
                        "entry_id": selected.id,
                    },
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
        original_selected_state = rpc_session_mutation_module.rpc_selected_session_state
        committed = threading.Event()
        release = threading.Event()

        def pause_after_navigation(
            selected_session: JsonlSession,
        ) -> tuple[int, tuple[Message, ...], str | None]:
            committed.set()
            assert release.wait(timeout=5)
            return original_selected_state(selected_session)

        monkeypatch.setattr(
            rpc_session_mutation_module,
            "rpc_selected_session_state",
            pause_after_navigation,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                },
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
            result = await _dispatch_parsed(
                executor,
                {
                    "id": "navigate",
                    "type": "navigate_session_tree",
                    "entry_id": active.id,
                },
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
            result = await _dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
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
            result = await _dispatch_parsed(executor, {"id": "clone", "type": "clone_session"})
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


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "queue-1"}])
@pytest.mark.parametrize("content", ["", " \t", "你好 🌸"])
def test_typed_queue_identity_content_and_order(
    tmp_path: Path, monkeypatch: MonkeyPatch, id_fields: dict[str, object], content: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        calls: list[object] = []
        state = QueueUpdated()

        async def enqueue(value: str) -> QueueUpdated:
            calls.append(value)
            return state

        def mode(kind: object, value: object) -> QueueUpdated:
            calls.append((kind, value))
            return state

        def pop(kind: object) -> tuple[Message, QueueUpdated]:
            calls.append(kind)
            return Message(role="user", content=content), state

        def clear(kind: object) -> tuple[QueuedMessages, QueueUpdated]:
            calls.append(kind)
            return QueuedMessages(
                steering=(Message(role="user", content=content),)
                if kind in (None, "steering")
                else (),
                follow_up=(Message(role="user", content="later"),)
                if kind in (None, "follow_up")
                else (),
            ), state

        monkeypatch.setattr(fixture.agent, "steer", enqueue)
        monkeypatch.setattr(fixture.agent, "follow_up", enqueue)
        monkeypatch.setattr(fixture.agent, "set_queue_mode", mode)
        monkeypatch.setattr(fixture.agent, "pop_queue", pop)
        monkeypatch.setattr(fixture.agent, "clear_queue", clear)
        payloads: list[dict[str, object]] = [
            {"type": "get_queue_state"},
            {"type": "steer", "content": content},
            {"type": "follow_up", "content": content},
            *[
                {"type": "set_queue_mode", "kind": kind, "mode": value}
                for kind in ("steering", "follow_up")
                for value in ("all", "one_at_a_time")
            ],
            *[{"type": "pop_queue", "kind": kind} for kind in ("steering", "follow_up")],
            *[
                {"type": "clear_queue", **fields}
                for fields in ({}, {"kind": None}, {"kind": "steering"}, {"kind": "follow_up"})
            ],
        ]
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for payload in payloads:
                start = len(fixture.events)
                result = await _dispatch_parsed(executor, {**payload, **id_fields}, running)
                assert result.running_command is running
                assert fixture.coordinator.running_command is running
                emitted = fixture.events[start:]
                removal = payload["type"] in {"pop_queue", "clear_queue"}
                assert [type(event) for event in emitted] == [
                    RpcCommandStarted,
                    *([QueueItemsRemoved] if removal else []),
                    QueueUpdated,
                    RpcCommandFinished,
                ]
                first, last = emitted[0], emitted[-1]
                assert isinstance(first, RpcCommandStarted)
                assert isinstance(last, RpcCommandFinished)
                assert first.command_id == last.command_id
                assert first.command_id
                assert first.command_type == last.command_type == payload["type"]
                assert last.ok is True
                if id_fields.get("id") is not None:
                    assert first.command_id == id_fields["id"]
                if removal:
                    removed = emitted[1]
                    assert isinstance(removed, QueueItemsRemoved)
                    assert removed.command_id == first.command_id
                    assert removed.kind == payload.get("kind")
                    if payload["type"] == "pop_queue":
                        assert (removed.steering, removed.follow_up) == (
                            ((content,), ()) if payload["kind"] == "steering" else ((), (content,))
                        )
                    else:
                        assert removed.steering == (
                            (content,) if payload.get("kind") in (None, "steering") else ()
                        )
                        assert removed.follow_up == (
                            ("later",) if payload.get("kind") in (None, "follow_up") else ()
                        )
            task_group.cancel_scope.cancel()
        assert calls == [
            content,
            content,
            ("steering", "all"),
            ("steering", "one_at_a_time"),
            ("follow_up", "all"),
            ("follow_up", "one_at_a_time"),
            "steering",
            "follow_up",
            None,
            None,
            "steering",
            "follow_up",
        ]

    anyio.run(scenario)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("command_type", ["steer", "follow_up"])
def test_typed_queue_runtime_failure_and_cancellation(
    tmp_path: Path, monkeypatch: MonkeyPatch, error_type: type[Exception], command_type: str
) -> None:
    async def fail(_content: str) -> QueueUpdated:
        raise error_type("queue failed")

    async def blocked(_content: str) -> QueueUpdated:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(fixture.agent, command_type, fail)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await _dispatch_parsed(
                executor, {"type": command_type, "id": "failed", "content": "text"}
            )
            assert [type(event) for event in fixture.events] == [
                RpcCommandStarted,
                ErrorEvent,
                RpcCommandFinished,
            ]
            last = fixture.events[-1]
            assert isinstance(last, RpcCommandFinished)
            assert last.ok is False and last.error == "queue failed"
            monkeypatch.setattr(fixture.agent, command_type, blocked)
            with anyio.CancelScope() as scope:
                scope.cancel()
                with pytest.raises(anyio.get_cancelled_exc_class()):
                    await _dispatch_parsed(
                        executor, {"type": command_type, "id": "cancelled", "content": "text"}
                    )
            assert len(fixture.events) == 4
            assert isinstance(fixture.events[-1], RpcCommandStarted)
            task_group.cancel_scope.cancel()

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
            results = [await _dispatch_parsed(executor, command, running) for command in commands]
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
            await _dispatch_parsed(
                executor, {"id": "pop", "type": "pop_queue", "kind": "steering"}, running
            )
            await _dispatch_parsed(executor, {"id": "clear", "type": "clear_queue"}, running)
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


@pytest.mark.parametrize("buffered", [False, True])
def test_executor_routes_queued_cancellation_through_coordinator(
    tmp_path: Path, buffered: bool
) -> None:
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
        queued = _parsed_command(
            {"id": "queued", "type": "steer", "content": "text"}
            if buffered
            else {"id": "queued", "type": "prompt", "prompt": "text"}
        )
        queue = (
            coordinator.pending_prompt_queue_commands if buffered else coordinator.queued_commands
        )
        await coordinator._enqueue_command(queued, queue=queue, reject=reject_unexpected_command)
        assert coordinator._queued_command_bytes == queued.payload_size
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

            result = await _dispatch_parsed(
                executor, {"id": "cancel", "type": "cancel", "target_id": "queued"}, None
            )

            assert result.running_command is None
            assert not queue
            assert coordinator._queued_command_bytes == 0
            assert coordinator._duplicate_outstanding_id(queued) is None
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

            await _dispatch_parsed(
                executor, {"id": "cancel", "type": "cancel", "target_id": "active"}, running
            )
            task_group.cancel_scope.cancel()

        assert active_scope.cancel_called is True
        assert coordinator.running_command is running
        finished = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok) for event in finished] == [("cancel", True)]

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "control-1"}])
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "cancel", "target_id": "active"},
        {"type": "approval", "call_id": "call", "approved": True},
        {"type": "trust", "request_id": "request", "trusted": True},
        {"type": "shutdown"},
    ],
)
def test_control_commands_stay_typed_and_correlated(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    id_fields: dict[str, object],
    payload: dict[str, object],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        effects: list[str] = []
        deferred: list[Callable[[], None]] = []

        class Approval:
            def has_pending_approval(self, **_kwargs: object) -> bool:
                return True

            def resolve_approval(self, **_kwargs: object) -> bool:
                effects.append("approval")
                return True

        class Trust:
            def resolve_request(self, **kwargs: object) -> bool:
                assert kwargs["release"] is False
                effects.append("decision")
                return True

            def release_request(self, **_kwargs: object) -> None:
                effects.append("release")

        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            executor.approval_policy = Approval()
            executor.trust_gate = Trust()
            executor.defer_until_after_flush = deferred.append
            # Validate directly: invalid known payloads must never become unknown envelopes.
            command = RpcCommandAdapter.validate_python({**payload, **id_fields})
            result = await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), running)
            assert result.running_command is running
            assert fixture.coordinator.running_command is running
            assert result.should_shutdown is (payload["type"] == "shutdown")
            assert [type(event) for event in fixture.events] == [
                RpcCommandStarted,
                RpcCommandFinished,
            ]
            first, last = fixture.events
            assert first.command_id == last.command_id
            assert first.command_id
            assert first.command_type == last.command_type == payload["type"]
            assert last.ok is True
            if id_fields.get("id") is not None:
                assert first.command_id == id_fields["id"]
            assert effects == (["decision"] if payload["type"] == "trust" else [])
            assert running.cancel_scope.cancel_called is False
            assert len(deferred) == (0 if payload["type"] == "shutdown" else 1)
            for callback in deferred:
                callback()
            assert effects == {"trust": ["decision", "release"], "approval": ["approval"]}.get(
                payload["type"], []
            )
            assert running.cancel_scope.cancel_called is (payload["type"] == "cancel")
            task_group.cancel_scope.cancel()

    anyio.run(scenario)


@pytest.mark.parametrize(
    "fields",
    [
        {"approved": True},
        {"approved": False},
        {"approved": True, "scope": None},
        {"approved": False, "scope": None},
        *[{"approved": True, "scope": scope} for scope in ("once", "tool_session", "all_session")],
    ],
)
@pytest.mark.parametrize("deferred_mode", [False, True])
def test_typed_approval_defaults_and_resolution(
    fields: dict[str, object], deferred_mode: bool
) -> None:
    command = ApprovalCommand.model_validate({"call_id": "call", "reason": " reason ", **fields})
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    decisions: list[dict[str, object]] = []

    class Approval:
        def has_pending_approval(self, **_kwargs: object) -> bool:
            return True

        def resolve_approval(self, **kwargs: object) -> bool:
            decisions.append(kwargs)
            return True

    rpc_execution_module.handle_rpc_control_command(
        command,
        running_command=None,
        approval_policy=Approval(),
        write_event=events.append,
        defer_until_after_flush=deferred.append if deferred_mode else None,
    )
    assert bool(decisions) is not deferred_mode
    for callback in deferred:
        callback()
    assert decisions == [
        {
            "call_id": "call",
            "approved": fields["approved"],
            "reason": " reason ",
            "scope": fields.get("scope") or "once",
        }
    ]
    assert events[-1].ok is True


@pytest.mark.parametrize(
    "pending,resolve_ok,deferred_mode",
    [(False, True, False), (True, False, False), (True, False, True)],
)
def test_typed_approval_missing_or_lost_decision(
    pending: bool, resolve_ok: bool, deferred_mode: bool
) -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    resolutions: list[bool] = []

    class Approval:
        def has_pending_approval(self, **_kwargs: object) -> bool:
            return pending

        def resolve_approval(self, **_kwargs: object) -> bool:
            resolutions.append(True)
            return resolve_ok

    rpc_execution_module.handle_rpc_control_command(
        ApprovalCommand(call_id="missing", approved=True),
        running_command=None,
        approval_policy=Approval(),
        write_event=events.append,
        defer_until_after_flush=deferred.append if deferred_mode else None,
    )
    before = list(events)
    for callback in deferred:
        callback()
    assert events == before
    assert events[-1].ok is deferred_mode
    assert resolutions == ([True] if pending else [])
    if not deferred_mode:
        assert events[-1].error == "No pending tool approval with call_id: missing"


@pytest.mark.parametrize(
    "transient_fields", [{}, {"transient": None}, {"transient": False}, {"transient": True}]
)
@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("deferred_mode", [False, True])
def test_typed_trust_decision_and_release_order(
    transient_fields: dict[str, object], trusted: bool, deferred_mode: bool
) -> None:
    trace: list[object] = []
    deferred: list[Callable[[], None]] = []

    class Trust:
        def resolve_request(self, **kwargs: object) -> bool:
            trace.append(kwargs)
            return True

        def release_request(self, **kwargs: object) -> None:
            trace.append(kwargs)

    command = TrustCommand.model_validate(
        {"request_id": "request", "trusted": trusted, "reason": " reason ", **transient_fields}
    )
    rpc_execution_module.handle_rpc_control_command(
        command,
        running_command=None,
        approval_policy=_ApprovalResolver(),
        trust_gate=Trust(),
        write_event=trace.append,
        defer_until_after_flush=deferred.append if deferred_mode else None,
    )
    assert isinstance(trace[0], RpcCommandStarted)
    assert trace[1] == {
        "request_id": "request",
        "trusted": trusted,
        "reason": " reason ",
        "transient": transient_fields.get("transient") is True,
        "release": not deferred_mode,
    }
    assert isinstance(trace[2], RpcCommandFinished) and trace[2].ok
    assert len(trace) == 3
    for callback in deferred:
        callback()
    if deferred_mode:
        assert trace[3:] == [{"request_id": "request"}]


@pytest.mark.parametrize("has_gate", [False, True])
def test_typed_trust_rejects_missing_gate_or_request(has_gate: bool) -> None:
    events: list[WispEvent] = []
    deferred: list[Callable[[], None]] = []
    rpc_execution_module.handle_rpc_control_command(
        TrustCommand(request_id="missing", trusted=True),
        running_command=None,
        approval_policy=_ApprovalResolver(),
        trust_gate=_TrustResolver() if has_gate else None,
        write_event=events.append,
        defer_until_after_flush=deferred.append,
    )
    assert not deferred
    assert events[-1].ok is False
    assert events[-1].error == (
        "No pending trust request with request_id: missing"
        if has_gate
        else "RPC trust command requires an active trust gate"
    )


def test_typed_cancellation_requires_coordinator_without_deferred_active_target() -> None:
    with pytest.raises(RuntimeError, match="requires the shared coordinator"):
        rpc_execution_module.handle_rpc_control_command(
            CancelCommand(target_id="missing"),
            running_command=None,
            approval_policy=_ApprovalResolver(),
            write_event=lambda _event: None,
        )


@pytest.mark.parametrize("command_type", ["cancel", "approval", "trust"])
@pytest.mark.parametrize("fail_flush", [False, True])
def test_control_host_flush_gates_side_effects(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str, fail_flush: bool
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        effects: list[str] = []
        rendered: list[WispEvent] = []
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())

        class Approval:
            def has_pending_approval(self, **_kwargs: object) -> bool:
                return True

            def resolve_approval(self, **_kwargs: object) -> bool:
                effects.append("approval")
                return True

        class Trust:
            def resolve_request(self, **kwargs: object) -> bool:
                assert kwargs["release"] is False
                effects.append("decision")
                return True

            def release_request(self, **_kwargs: object) -> None:
                effects.append("release")

        async def render(events: AsyncIterator[WispEvent]) -> None:
            async for event in events:
                rendered.append(event)
                await anyio.lowlevel.checkpoint()
                assert effects == (["decision"] if command_type == "trust" else [])
                assert not running.cancel_scope.cancel_called
                if isinstance(event, RpcCommandFinished) and fail_flush:
                    raise RuntimeError("flush failed")

        payload = {
            "cancel": {"type": "cancel", "target_id": "active"},
            "approval": {"type": "approval", "call_id": "call", "approved": True},
            "trust": {"type": "trust", "request_id": "request", "trusted": True},
        }[command_type]

        async def run(_receive: object, *, dispatch: object, reject: object) -> bool:
            await dispatch(
                ParsedRpcCommand.from_known(RpcCommandAdapter.validate_python(payload)), running
            )
            return False

        monkeypatch.setattr(fixture.coordinator, "run", run)
        host = RpcHost(
            runtime=fixture.runtime,
            sessions=fixture.sessions,
            agent=fixture.agent,
            approval_policy=Approval(),
            trust_gate=Trust(),
            configure_overrides=_RpcConfigureOverrides(),
            coordinator=fixture.coordinator,
            write_event=fixture.events.append,
            render_events=render,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            if fail_flush:
                with pytest.raises(RuntimeError, match="flush failed"):
                    await host.run_with_streams(receive, send=send, task_group=task_group)
            else:
                await host.run_with_streams(receive, send=send, task_group=task_group)
            task_group.cancel_scope.cancel()
        assert [type(event) for event in rendered] == [RpcCommandStarted, RpcCommandFinished]
        assert running.cancel_scope.cancel_called is (command_type == "cancel" and not fail_flush)
        assert effects == (
            (["decision"] if fail_flush else ["decision", "release"])
            if command_type == "trust"
            else (["approval"] if command_type == "approval" and not fail_flush else [])
        )

    anyio.run(scenario)


@pytest.mark.parametrize("invalid_id", [[], "", "x" * 257])
def test_unknown_rejection_preserves_invalid_id_precedence(
    tmp_path: Path, invalid_id: object
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch_parsed(
                ParsedRpcCommand.from_unknown({"type": "future", "id": invalid_id}), running
            )
            assert result.running_command is running
            assert result.should_shutdown is False
            assert fixture.coordinator.running_command is running
            task_group.cancel_scope.cancel()
        started, error, finished = fixture.events
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(error, ErrorEvent)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_id == finished.command_id
        assert len(started.command_id) == 32
        assert started.command_type == finished.command_type == "future"
        assert finished.ok is False
        assert (
            error.message
            == finished.error
            == (
                "RPC command id must contain at most 256 characters"
                if isinstance(invalid_id, str) and len(invalid_id) > 256
                else "RPC command id must be a non-empty string"
            )
        )

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "run-1"}])
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "prompt", "prompt": ""},
        {"type": "prompt", "prompt": " \t"},
        {"type": "prompt", "prompt": "你好 🌸"},
        {"type": "init"},
        *[
            {"type": "compact", **fields}
            for fields in (
                {},
                {"instructions": None},
                {"instructions": ""},
                {"instructions": " \t"},
                {"instructions": "  retain 日本語  "},
            )
        ],
        {"type": "get_session_stats"},
    ],
)
def test_run_commands_stay_typed_through_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    id_fields: dict[str, object],
    payload: dict[str, object],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        if payload["type"] == "init":
            project = tmp_path / "project"
            project.mkdir()
            _enable_project_init(fixture, project)
        selected = fixture.sessions.create()
        selected.path.parent.mkdir(parents=True, exist_ok=True)
        selected.path.touch()
        fixture.session_state.session = selected
        calls: list[object] = []

        async def run(prompt: str, **kwargs: object) -> AsyncIterator[WispEvent]:
            calls.append((prompt, kwargs["operation_id"]))
            yield MessageCompleted(turn=1, content="done", finish_reason="stop")

        async def compact(
            session: JsonlSession, *, instructions: str | None = None
        ) -> AsyncIterator[WispEvent]:
            assert session is selected
            calls.append(instructions)
            if False:
                yield MessageCompleted(turn=1, content="done", finish_reason="stop")

        monkeypatch.setattr(fixture.agent, "run", run)
        monkeypatch.setattr(fixture.agent, "compact", compact)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            command = RpcCommandAdapter.validate_python({**payload, **id_fields})
            result = await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), None)
            with anyio.fail_after(2):
                completed = await receive.receive()
                while not isinstance(completed, _RpcCommandCompleted):
                    completed = await receive.receive()
            task_group.cancel_scope.cancel()
        started = fixture.events[0]
        finished = fixture.events[-1]
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(finished, RpcCommandFinished)
        assert result.running_command is not None
        assert (
            started.command_id
            == result.running_command.command_id
            == completed.command_id
            == finished.command_id
        )
        assert (
            started.command_type
            == result.running_command.command_type
            == completed.command_type
            == finished.command_type
            == payload["type"]
        )
        assert started.command_id
        if id_fields.get("id") is not None:
            assert started.command_id == id_fields["id"]
        # The fake init run creates no guidance; retain its init completion failure.
        assert finished.ok is completed.ok is (payload["type"] != "init")
        if payload["type"] in {"prompt", "init"}:
            assert calls == [(payload.get("prompt", "/init"), started.command_id)]
            assert result.selected_session is selected
        elif payload["type"] == "compact":
            raw = payload.get("instructions")
            assert calls == [raw.strip() or None if isinstance(raw, str) else None]
        else:
            assert [type(event) for event in fixture.events] == [
                RpcCommandStarted,
                SessionStatsReported,
                RpcCommandFinished,
            ]
            assert fixture.events[1].command_id == started.command_id

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "rejected-1"}])
@pytest.mark.parametrize("command_type", ["init", "compact"])
def test_typed_run_preflight_failure_does_not_start_work(
    tmp_path: Path, monkeypatch: MonkeyPatch, id_fields: dict[str, object], command_type: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)

        def fail(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Preflight rejection must neither convert nor launch work")

        monkeypatch.setattr(fixture.sessions, "create", fail)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            monkeypatch.setattr(task_group, "start_soon", fail)
            command = RpcCommandAdapter.validate_python({"type": command_type, **id_fields})
            result = await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), None)
            assert result.running_command is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        first, _, last = fixture.events
        assert first.command_id == last.command_id
        assert first.command_type == last.command_type == command_type
        assert last.ok is False
        if id_fields.get("id") is not None:
            assert first.command_id == id_fields["id"]

    anyio.run(scenario)


@pytest.mark.parametrize("has_session", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_typed_statistics_worker_outcomes(
    tmp_path: Path, monkeypatch: MonkeyPatch, has_session: bool, outcome: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create() if has_session else None
        if selected is not None:
            selected.path.touch()
        fixture.session_state.session = selected
        entered = anyio.Event()
        original = fixture.agent.get_session_stats

        async def stats(session: JsonlSession | None = None) -> object:
            assert session is selected
            entered.set()
            if outcome == "failure":
                raise RuntimeError("stats failed")
            if outcome == "cancel":
                await anyio.sleep_forever()
            return await original(session)

        monkeypatch.setattr(fixture.agent, "get_session_stats", stats)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await _dispatch_parsed(executor, {"type": "get_session_stats", "id": "stats"})
            assert result.running_command is not None
            await entered.wait()
            if outcome == "cancel":
                result.running_command.cancel_scope.cancel()
            with anyio.fail_after(2):
                completed = await receive.receive()
            task_group.cancel_scope.cancel()
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.command_id == "stats"
        assert completed.ok is (outcome == "success")
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            *([SessionStatsReported] if outcome == "success" else []),
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert (
            finished.error
            == {
                "success": None,
                "failure": "stats failed",
                "cancel": "RPC get_session_stats command cancelled",
            }[outcome]
        )

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["prompt", "init", "compact", "get_session_stats"])
def test_run_workers_wait_for_started_event_flush(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        if command_type == "init":
            project = tmp_path / "project"
            project.mkdir()
            _enable_project_init(fixture, project)
        selected = fixture.sessions.create()
        selected.path.touch()
        fixture.session_state.session = selected
        rendered: list[WispEvent] = []
        entered = anyio.Event()

        async def worker(*args: object) -> None:
            assert len(rendered) == 1
            assert isinstance(rendered[0], RpcCommandStarted)
            assert rendered[0].command_type == command_type
            entered.set()
            # The starter clones the completion stream; this fake worker owns that clone.
            for arg in args:
                if isinstance(arg, anyio.streams.memory.MemoryObjectSendStream):
                    await arg.aclose()

        async def render(events: AsyncIterator[WispEvent]) -> None:
            async for event in events:
                assert not entered.is_set()
                await anyio.lowlevel.checkpoint()
                assert not entered.is_set()
                rendered.append(event)

        worker_name = {
            "prompt": "prompt",
            "init": "prompt",
            "compact": "compact",
            "get_session_stats": "session_stats",
        }[command_type]
        monkeypatch.setattr(rpc_execution_module, f"run_rpc_{worker_name}_command", worker)
        payload = {"type": command_type, **({"prompt": "text"} if command_type == "prompt" else {})}

        async def run(_receive: object, *, dispatch: object, reject: object) -> bool:
            result = await dispatch(
                ParsedRpcCommand.from_known(RpcCommandAdapter.validate_python(payload)), None
            )
            assert result.running_command is not None
            with anyio.fail_after(2):
                await entered.wait()
            return False

        monkeypatch.setattr(fixture.coordinator, "run", run)
        host = RpcHost(
            runtime=fixture.runtime,
            sessions=fixture.sessions,
            agent=fixture.agent,
            approval_policy=fixture.approval_policy,
            trust_gate=fixture.trust_gate,
            configure_overrides=fixture.configure_overrides,
            coordinator=fixture.coordinator,
            write_event=fixture.events.append,
            render_events=render,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await host.run_with_streams(receive, send=send, task_group=task_group)
            task_group.cancel_scope.cancel()
        assert entered.is_set()

    anyio.run(scenario)


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_typed_compact_rejects_non_file_session(
    tmp_path: Path, monkeypatch: MonkeyPatch, path_kind: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        if path_kind == "directory":
            selected.path.mkdir()
        fixture.session_state.session = selected

        def fail(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Invalid persisted session must not launch a worker")

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            monkeypatch.setattr(task_group, "start_soon", fail)
            result = await _dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"type": "compact", "id": "compact"},
            )
            assert result.running_command is None
        assert (
            fixture.events[-1].error == "RPC compact command requires an existing persisted session"
        )

    anyio.run(scenario)


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


@pytest.mark.parametrize(
    ("id_fields", "id_error"),
    [
        ({}, None),
        ({"id": None}, None),
        ({"id": "supplied"}, None),
        ({"id": " "}, None),
        ({"id": "界" * 256}, None),
        ({"id": ""}, "RPC command id must be a non-empty string"),
        ({"id": False}, "RPC command id must be a non-empty string"),
        ({"id": 7}, "RPC command id must be a non-empty string"),
        ({"id": []}, "RPC command id must be a non-empty string"),
        ({"id": {}}, "RPC command id must be a non-empty string"),
        ({"id": "界" * 257}, "RPC command id must contain at most 256 characters"),
    ],
)
@pytest.mark.parametrize("dispatch_unknown", [False, True])
def test_parsed_rejection_preserves_id_rules_and_error_precedence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    id_fields: dict[str, object],
    id_error: str | None,
    dispatch_unknown: bool,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        generated: list[str] = []
        original_uuid4 = rpc_lifecycle_module.uuid4

        def generate_id():
            result = original_uuid4()
            generated.append(result.hex)
            return result

        monkeypatch.setattr(rpc_lifecycle_module, "uuid4", generate_id)
        parsed = ParsedRpcCommand.from_unknown({"type": "future_command", **id_fields})
        assert parsed.command_id_error == id_error
        raw_id = id_fields.get("id")
        assert parsed.command_id == (raw_id if isinstance(raw_id, str) and raw_id else None)
        assert generated == []
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            if dispatch_unknown:
                result = await executor.dispatch_parsed(parsed, running)
                assert result.running_command is running
                assert result.should_shutdown is False
                assert fixture.coordinator.running_command is running
            else:
                fixture.coordinator.running_command = running
                executor.reject_parsed(parsed, "RPC command queue is full")
                assert fixture.coordinator.running_command is running
            assert not running.cancel_scope.cancel_called
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        started, error, finished = fixture.events
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(error, ErrorEvent)
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == started.command_id
        assert finished.command_type == started.command_type == "future_command"
        assert finished.ok is False
        assert (
            finished.error
            == error.message
            == (
                id_error
                or (
                    "Unknown RPC command: future_command"
                    if dispatch_unknown
                    else "RPC command queue is full"
                )
            )
        )
        if raw_id is not None and id_error is None:
            assert started.command_id == raw_id
            assert generated == []
        else:
            assert generated == [started.command_id]
            assert len(started.command_id) == 32

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("fields", "normalized"),
    [
        ({}, "unknown"),
        ({"type": None}, "unknown"),
        ({"type": 1}, "unknown"),
        ({"type": ""}, "unknown"),
        ({"type": " future "}, " future "),
        ({"type": "界" * 64}, "界" * 64),
        ({"type": "界" * 65}, "unknown"),
    ],
)
def test_unknown_rejection_uses_normalized_discriminator(
    tmp_path: Path,
    fields: dict[str, object],
    normalized: str,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        parsed = ParsedRpcCommand.from_unknown({"id": "future", **fields})
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await fixture.executor(task_group=task_group, send=send).dispatch_parsed(
                parsed, None
            )
        assert result.running_command is None
        assert result.should_shutdown is False
        started, error, finished = fixture.events
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(error, ErrorEvent)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_type == finished.command_type == normalized
        assert error.message == finished.error == f"Unknown RPC command: {normalized}"

    anyio.run(scenario)


@pytest.mark.parametrize("detach_id", [False, True])
def test_rejecting_credential_command_preserves_secrets_identity_and_store(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    detach_id: bool,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        store.set("anthropic", ApiKeyCredential(key="existing-key"))
        secret = "sentinel-rejected-api-key"
        command = StoreApiKeyCommand(id="original", provider="anthropic", api_key=secret)
        parsed = ParsedRpcCommand.from_known(command)
        rejected = fixture.coordinator._duplicate_rejection_command(parsed) if detach_id else parsed
        assert parsed.command_id == "original"
        assert rejected.command_id == (None if detach_id else "original")
        assert rejected.command_id_error is None
        assert rejected.known is not None
        assert secret in rejected.known.to_json_line()  # Intentional wire serialization.
        assert secret not in repr(rejected)
        assert secret not in repr(rejected.known)
        assert rejected.provided_fields == (
            {"type", "provider", "api_key"} | (set() if detach_id else {"id"})
        )
        assert rejected.payload_size == len(
            json.dumps(
                {
                    "type": "store_api_key",
                    "provider": "anthropic",
                    "_api_key": secret,
                    **({} if detach_id else {"id": "original"}),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )

        def fail_mutation(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Rejected credential commands must not mutate credentials")

        monkeypatch.setattr(store, "set", fail_mutation)
        monkeypatch.setattr(store, "delete", fail_mutation)
        message = (
            "RPC command id is already outstanding: original"
            if detach_id
            else ("RPC command queue byte limit exceeded " + "x" * _MAX_RPC_COMMAND_ERROR_CHARS)
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            fixture.executor(task_group=task_group, send=send).reject_parsed(rejected, message)
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        started, error, finished = fixture.events
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(error, ErrorEvent)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_id == finished.command_id
        assert (finished.command_id != "original") is detach_id
        expected = message if detach_id else message[: _MAX_RPC_COMMAND_ERROR_CHARS - 3] + "..."
        assert finished.error == error.message == expected
        assert all(secret not in repr(event) for event in fixture.events)
        assert store.get("anthropic") == ApiKeyCredential(key="existing-key")

    anyio.run(scenario)
