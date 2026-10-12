# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.cli.rpc_mode_support import (
    create_two_turn_session,
)
from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization
from wisp.agent.messages import CompactionRecord
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import ContextBudget, ContextEstimate, ToolCallSnapshot
from wisp.rpc.coordinator import _RpcPromptReady
from wisp.rpc.session import run as rpc_session_run_module
from wisp.rpc.session.run import (
    rpc_has_durable_completion,
    run_rpc_prompt_command,
)
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
)


def test_cancelled_prompt_leaf_restore_preserves_concurrent_compaction(tmp_path: Path) -> None:
    session = create_two_turn_session(tmp_path)
    initial_entries = session.read_entries()
    entry_start = len(initial_entries)
    initial_leaf_id = session.read_active_leaf_id()

    async def append_prompt_and_compaction() -> None:
        await session.append_message(
            Message(role="system", content="current system prompt"),
            operation_id="prompt-1",
        )
        pending_user = await session.append_message(
            Message(role="user", content="cancel me"),
            operation_id="prompt-1",
        )
        context = session.read_context()
        await session.append_compaction_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                kind="compaction",
                compaction=CompactionRecord(
                    summary="durable summary",
                    replaced_entry_ids=(initial_entries[0].id, initial_entries[1].id),
                    provider="test",
                ),
            ),
            expected_context_entry_ids=context.context_entry_ids,
        )
        assert pending_user.id in session.read_context().context_entry_ids
        restored = await session.restore_active_leaf_for_operation(
            entry_start,
            initial_leaf_id,
            operation_id="prompt-1",
        )
        assert restored is False

    anyio.run(append_prompt_and_compaction)

    assert rpc_has_durable_completion(session, entry_start, "prompt-1") is False
    assert any(entry.kind == "compaction" for entry in session.read_entries()[entry_start:])


def test_rpc_cancellation_restores_leaf_before_unanswered_overflow_compaction(
    tmp_path: Path,
) -> None:
    session = create_two_turn_session(tmp_path)
    entry_start = len(session.read_entries())
    initial_context_ids = session.read_context().context_entry_ids
    initial_leaf_id = session.read_active_leaf_id()

    async def append_overflow_compaction() -> None:
        context = session.read_context()
        await session.append_compaction_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                kind="compaction",
                operation_id="prompt-1",
                compaction=CompactionRecord(
                    summary="durable overflow summary",
                    replaced_entry_ids=context.context_entry_ids[:2],
                    provider="test",
                    reason="overflow",
                    trigger_budget=ContextBudget(
                        estimate=ContextEstimate(
                            system_tokens=1,
                            message_tokens=2,
                            tool_schema_tokens=0,
                            total_tokens=3,
                        ),
                        context_window=100,
                        reserve_tokens=20,
                        remaining_tokens=77,
                        estimated_percent=3,
                        over_budget=False,
                    ),
                ),
            ),
            expected_context_entry_ids=context.context_entry_ids,
        )

    anyio.run(append_overflow_compaction)

    assert rpc_has_durable_completion(session, entry_start, "prompt-1") is False

    async def restore() -> bool:
        return await session.restore_active_leaf_for_operation(
            entry_start,
            initial_leaf_id,
            operation_id="prompt-1",
        )

    assert anyio.run(restore) is True
    suffix = session.read_entries()[entry_start:]
    assert any(entry.kind == "compaction" for entry in suffix)
    assert isinstance(suffix[-1], ActiveLeafSessionEntry)
    assert session.read_context().context_entry_ids == initial_context_ids


def test_operation_leaf_restore_fails_cleanly_if_starting_leaf_was_removed(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def mutate() -> bool:
        await session.append_message(Message(role="user", content="first"))
        starting_leaf = await session.append_message(Message(role="assistant", content="second"))
        starting_count = len(session.read_entries())
        await session.truncate_entries(1)
        await session.append_message(
            Message(role="user", content="replacement"),
            operation_id="prompt-1",
        )
        await session.append_message(
            Message(role="assistant", content="replacement answer"),
            operation_id="prompt-1",
        )
        return await session.restore_active_leaf_for_operation(
            starting_count,
            starting_leaf.id,
            operation_id="prompt-1",
        )

    assert anyio.run(mutate) is False


def test_rpc_mode_cancels_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert "RPC command cancelled: cmd-1" in errors
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    cancel_finished = next(record for record in finished if record["command_id"] == "cancel-1")
    prompt_finished = next(record for record in finished if record["command_id"] == "cmd-1")
    cancel_started_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.started" and record["command_id"] == "cancel-1"
    )
    cancel_finished_index = records.index(cancel_finished)
    prompt_finished_index = records.index(prompt_finished)
    assert cancel_finished["ok"] is True
    assert prompt_finished["ok"] is False
    assert prompt_finished["error"] == "RPC command cancelled: cmd-1"
    assert cancel_started_index < cancel_finished_index < prompt_finished_index


class _TrustedGate:
    async def resolve(self) -> bool:
        return True


def test_rpc_prompt_cancellation_restores_active_leaf_before_completion_boundary(
    tmp_path: Path,
) -> None:
    class PreResponseCancellableProvider(CancellableProvider):
        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def stream(
            self,
            messages: Sequence[Message],
            *,
            model: str | None = None,
            tools: Sequence[ToolSpec] = (),
            tool_results: Sequence[ToolCallResult] = (),
            previous_response_id: str | None = None,
            effort: str | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            self.started.set()
            await anyio.sleep_forever()
            async for event in super().stream(
                messages,
                model=model,
                tools=tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=effort,
            ):
                yield event

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        agent = CodingSession(
            provider=PreResponseCancellableProvider(started),
            sessions=JsonlSessionStore(tmp_path),
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    (),
                    0,
                    "slow",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_rpc_module._write_json_event,
                    cli_rpc_module._render_json_events,
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        entries = session.read_entries()
        assert session.read_context_messages() == ()
        assert session.read_active_leaf_id() is None
        assert isinstance(entries[-1], ActiveLeafSessionEntry)
        assert entries[-1].active_leaf_id is None
        return completed, len(entries)

    completed, audit_entry_count = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.command_id == "cmd-1"
    assert completed.history is not None
    assert completed.history == ()
    assert completed.entry_count == audit_entry_count


def test_rpc_cancellation_during_run_snapshot_preserves_existing_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run_prompt() -> object:
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="existing prompt"))
        committed_history = session.read_context_messages()
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=CancellableProvider(),
            sessions=JsonlSessionStore(tmp_path),
        )
        snapshot_started = anyio.Event()
        release_snapshot = anyio.Event()
        execution = rpc_session_run_module
        original_run_sync = execution.anyio.to_thread.run_sync

        async def delayed_run_sync(func: object, *args: object, **kwargs: object) -> object:
            if func is execution.rpc_session_run_start:
                snapshot_started.set()
                await release_snapshot.wait()
            return await original_run_sync(func, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(execution.anyio.to_thread, "run_sync", delayed_run_sync)
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    committed_history,
                    entry_start,
                    "slow",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_rpc_module._write_json_event,
                    cli_rpc_module._render_json_events,
                )
                await snapshot_started.wait()
                cancel_scope.cancel()
                release_snapshot.set()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        assert session.read_context_messages() == committed_history
        assert not any(
            isinstance(entry, ActiveLeafSessionEntry) and entry.active_leaf_id is None
            for entry in session.read_entries()
        )
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.history is not None
    assert [message.content for message in completed.history] == ["existing prompt"]


def test_rpc_prestart_cancellation_preserves_loaded_tool_repair(tmp_path: Path) -> None:
    class PreResponseCancellableProvider(CancellableProvider):
        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def stream(
            self,
            messages: Sequence[Message],
            *,
            model: str | None = None,
            tools: Sequence[ToolSpec] = (),
            tool_results: Sequence[ToolCallResult] = (),
            previous_response_id: str | None = None,
            effort: str | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            self.started.set()
            await anyio.sleep_forever()
            async for event in super().stream(
                messages,
                model=model,
                tools=tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=effort,
            ):
                yield event

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="old prompt"))
        await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        committed_history = session.read_messages()
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=PreResponseCancellableProvider(started),
            sessions=JsonlSessionStore(tmp_path),
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    committed_history,
                    entry_start,
                    "cancelled prompt",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_rpc_module._write_json_event,
                    cli_rpc_module._render_json_events,
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        audit_messages = session.read_messages()
        assert [message.role for message in audit_messages[:3]] == ["user", "assistant", "tool"]
        assert len(audit_messages) > 3
        context_messages = session.read_context_messages()
        assert [message.role for message in context_messages] == ["user", "assistant", "tool"]
        assert context_messages[-1].content == INTERRUPTED_TOOL_RESULT_TEXT
        return completed, len(session.read_entries())

    completed, audit_entry_count = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.history is not None
    assert [message.role for message in completed.history] == ["user", "assistant", "tool"]
    assert completed.entry_count == audit_entry_count


def test_rpc_prompt_cancellation_retains_entries_after_completion_boundary(
    tmp_path: Path,
) -> None:
    class BlockingDangerTool:
        name = "danger"
        safety = "read"
        description = "Block after the provider completes its tool-call turn."
        input_schema: ToolInputSchema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
            self.started.set()
            await anyio.sleep_forever()
            return ToolResult(text="unused")

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        tools = ToolRegistry()
        tools.register(BlockingDangerTool(started))
        agent = CodingSession(
            provider=ToolCallingProvider(),
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    (),
                    0,
                    "use tool",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_rpc_module._write_json_event,
                    cli_rpc_module._render_json_events,
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        retained = [
            message
            for message in session.read_messages()
            if message.role in {"user", "assistant", "tool"}
        ]
        assert [message.role for message in retained] == ["user", "assistant", "tool"]
        assert retained[0].content == "use tool"
        assert retained[1].tool_calls is not None
        assert [call.call_id for call in retained[1].tool_calls] == ["call-1"]
        assert retained[2].tool_call_id == "call-1"
        assert retained[2].content == INTERRUPTED_TOOL_RESULT_TEXT
        assert retained[2].is_error is True
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.command_id == "cmd-1"
    assert completed.history is not None
    assert [
        message.role
        for message in completed.history
        if message.role in {"user", "assistant", "tool"}
    ] == ["user", "assistant", "tool"]
    assert completed.entry_count > 0


def test_rpc_mode_cancel_reports_unknown_target(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cancel-1","type":"cancel","target_id":"missing"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "No running or queued RPC command with id: missing"
    assert records[2]["command_id"] == "cancel-1"
    assert records[2]["ok"] is False
    assert records[2]["error"] == "No running or queued RPC command with id: missing"


def test_rpc_mode_cancel_requires_target_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cancel-1","type":"cancel"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["error"]
    assert records[0]["message"] == "RPC command does not match the negotiated schema"


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
