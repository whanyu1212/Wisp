from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import ApprovalResolver
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.auth.storage import ApiKeyCredential
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandStarted,
    WispEvent,
)
from wisp.rpc import lifecycle as rpc_lifecycle_module
from wisp.rpc.commands import (
    ApprovalCommand,
    ParsedRpcCommand,
    StoreApiKeyCommand,
)
from wisp.rpc.coordinator import _RpcRunningCommand
from wisp.rpc.handlers import control as rpc_control_module
from wisp.rpc.lifecycle import _MAX_RPC_COMMAND_ERROR_CHARS


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_rpc_command_errors_bound_echoed_reference_fields() -> None:
    events: list[WispEvent] = []
    oversized_reference = "x" * (_MAX_RPC_COMMAND_ERROR_CHARS + 1)

    rpc_control_module.handle_rpc_approval_command(
        ApprovalCommand(id="approval-1", call_id=oversized_reference, approved=True),
        command_id="approval-1",
        command_type="approval",
        approval_policy=ApprovalResolver(),
        write_event=events.append,
    )

    assert len(events) == 2
    error, finished = events
    assert isinstance(error, ErrorEvent)
    assert isinstance(finished, RpcCommandFinished)
    assert len(error.message) == _MAX_RPC_COMMAND_ERROR_CHARS
    assert error.message.endswith("...")
    assert finished.error == error.message


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
