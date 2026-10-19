# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.cli.rpc_mode_support import (
    BlockingOperationProvider,
    runtime_with_provider,
)
from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization
from wisp.rpc.host import RpcTrustGate


def test_rpc_queue_commands_wait_for_prompt_readiness_then_bypass(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = BlockingOperationProvider()

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    original_resolve = RpcTrustGate.resolve

    async def delayed_resolve(self: RpcTrustGate) -> bool:
        await anyio.sleep(0.05)
        return await original_resolve(self)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    monkeypatch.setattr(RpcTrustGate, "resolve", delayed_resolve)
    commands = [
        {"id": "prompt-1", "type": "prompt", "prompt": "block"},
        {"id": "rpc-state-1", "type": "get_state"},
        {"id": "messages-1", "type": "get_messages"},
        {"id": "steer-1", "type": "steer", "content": "one"},
        {"id": "steer-2", "type": "steer", "content": "two"},
        {"id": "follow-1", "type": "follow_up", "content": "later"},
        {
            "id": "mode-1",
            "type": "set_queue_mode",
            "kind": "follow_up",
            "mode": "all",
        },
        {"id": "state-1", "type": "get_queue_state"},
        {"id": "pop-1", "type": "pop_queue", "kind": "steering"},
        {"id": "clear-1", "type": "clear_queue", "kind": "follow_up"},
    ]
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input="".join(f"{json.dumps(command)}\n" for command in commands),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)

    def command_records(command_id: str) -> list[dict[str, object]]:
        start = next(
            index
            for index, record in enumerate(records)
            if record["type"] == "rpc.command.started" and record["command_id"] == command_id
        )
        finish = next(
            index
            for index, record in enumerate(records[start:], start=start)
            if record["type"] == "rpc.command.finished" and record["command_id"] == command_id
        )
        return records[start : finish + 1]

    state_records = command_records("state-1")
    assert [record["type"] for record in state_records] == [
        "rpc.command.started",
        "queue.updated",
        "rpc.command.finished",
    ]
    assert state_records[1]["steering"] == ["one", "two"]
    assert state_records[1]["follow_up"] == ["later"]
    assert state_records[1]["follow_up_mode"] == "all"

    rpc_state_records = command_records("rpc-state-1")
    assert [record["type"] for record in rpc_state_records] == [
        "rpc.command.started",
        "rpc.state",
        "rpc.command.finished",
    ]
    assert rpc_state_records[1]["state"]["active_command_id"] == "prompt-1"
    assert rpc_state_records[1]["state"]["active_command_type"] == "prompt"
    assert rpc_state_records[1]["state"]["cancel_requested"] is False
    assert rpc_state_records[1]["state"]["session_id"]
    assert rpc_state_records[1]["state"]["session_path"]

    pop_records = command_records("pop-1")
    assert [record["type"] for record in pop_records] == [
        "rpc.command.started",
        "queue.items.removed",
        "queue.updated",
        "rpc.command.finished",
    ]
    assert pop_records[1]["operation"] == "pop"
    assert pop_records[1]["steering"] == ["two"]
    assert pop_records[2]["steering"] == ["one"]

    clear_records = command_records("clear-1")
    assert [record["type"] for record in clear_records] == [
        "rpc.command.started",
        "queue.items.removed",
        "queue.updated",
        "rpc.command.finished",
    ]
    assert clear_records[1]["operation"] == "clear"
    assert clear_records[1]["follow_up"] == ["later"]
    assert clear_records[2]["follow_up"] == []

    prompt_finish_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.finished" and record["command_id"] == "prompt-1"
    )
    for command_id in (
        "rpc-state-1",
        "steer-1",
        "steer-2",
        "follow-1",
        "mode-1",
        "state-1",
        "pop-1",
        "clear-1",
    ):
        queue_finish_index = next(
            index
            for index, record in enumerate(records)
            if record["type"] == "rpc.command.finished" and record["command_id"] == command_id
        )
        assert queue_finish_index < prompt_finish_index
        assert records[queue_finish_index]["ok"] is True
    messages_finish_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.finished" and record["command_id"] == "messages-1"
    )
    assert messages_finish_index > prompt_finish_index
    messages_report = next(record for record in records if record["type"] == "rpc.messages")
    assert messages_report["command_id"] == "messages-1"
    assert messages_report["session_id"]


def test_rpc_rejects_container_queue_fields_and_remains_usable(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"kind","type":"pop_queue","kind":[]}\n'
            '{"id":"mode","type":"set_queue_mode","kind":"steering","mode":{}}\n'
            '{"id":"state","type":"get_queue_state"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("state", True),
    ]
    assert [record["message"] for record in records if record["type"] == "error"] == [
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
    ]


def test_held_steers_stay_bounded(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = BlockingOperationProvider(block_prompt=True)

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    # The prompt waits in its trust check until the cancel ends it, so it never becomes
    # ready and every steer stays held. A prompt that could become ready raced the
    # cancel, which under load sometimes arrived first and failed the held steers.
    async def unresolved_trust(self: RpcTrustGate) -> bool:
        await anyio.sleep_forever()
        raise AssertionError("cancel ends the prompt before trust resolves")

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    monkeypatch.setattr(RpcTrustGate, "resolve", unresolved_trust)
    commands = [{"id": "prompt", "type": "prompt", "prompt": "block"}]
    commands.extend(
        {"id": f"steer-{index}", "type": "steer", "content": str(index)} for index in range(101)
    )
    commands.append({"id": "cancel", "type": "cancel", "target_id": "prompt"})
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input="".join(f"{json.dumps(command)}\n" for command in commands),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    terminals = [record for record in records if record["type"] == "rpc.command.finished"]
    terminal_ids = [record["command_id"] for record in terminals]
    assert len(terminal_ids) == len(set(terminal_ids)), terminal_ids
    finished = {record["command_id"]: record for record in terminals}
    queue_full = "Agent queue is full (maximum 100 pending messages)"
    # Only the steer past the run's message limit is refused while held.
    assert [record["command_id"] for record in terminals if record.get("error") == queue_full] == [
        "steer-100"
    ]
    # The cancelled prompt never accepted the held steers, so each one finishes unapplied.
    assert all(f"steer-{index}" in finished for index in range(100))
    assert not any(finished[f"steer-{index}"]["ok"] for index in range(100))
    assert finished["prompt"]["ok"] is False
    assert finished["cancel"]["ok"] is True


def test_rpc_mode_rejects_duplicate_outstanding_command_id(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = BlockingOperationProvider(block_prompt=True)

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"same","type":"prompt","prompt":"block"}\n'
            '{"id":"same","type":"prompt","prompt":"duplicate"}\n'
            '{"id":"cancel","type":"cancel","target_id":"same"}\n'
        ),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    terminals = [record for record in records if record["type"] == "rpc.command.finished"]
    original = [record for record in terminals if record["command_id"] == "same"]
    duplicate = [
        record
        for record in terminals
        if record.get("error") == "RPC command id is already outstanding: same"
    ]
    assert len(original) == 1
    assert original[0]["ok"] is False
    assert original[0]["error"] == "RPC command cancelled: same"
    assert len(duplicate) == 1
    assert duplicate[0]["ok"] is False
    assert duplicate[0]["command_id"] not in {"same", "cancel"}
    assert any(record["command_id"] == "cancel" and record["ok"] is True for record in terminals)


def test_rpc_mode_rejects_commands_beyond_queue_cap_while_prompt_runs(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)
    monkeypatch.setattr(rpc_host_module, "_MAX_QUEUED_RPC_COMMANDS", 2)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cmd-3","type":"prompt","prompt":"third"}\n'
            '{"id":"cmd-overflow","type":"prompt","prompt":"overflow"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    overflow_error = next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "cmd-overflow"
    )
    assert overflow_error["ok"] is False
    assert overflow_error["error"] == (
        "RPC command queue is full while another RPC command is running"
    )
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert ("cancel-1", True) in [(record["command_id"], record["ok"]) for record in finished]
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second", "done third"]


def test_rpc_mode_processes_queued_shutdown_after_running_prompt_finishes(
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
            '{"id":"shutdown-1","type":"shutdown"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert "RPC prompt command requires string field: prompt" not in [
        record.get("message") for record in records
    ]
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == [
        "cmd-1",
        "cancel-1",
        "shutdown-1",
    ]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("shutdown-1", True),
    ]


def test_rpc_mode_queues_prompts_while_canceling_running_prompt(
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
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("cmd-2", True),
    ]


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
