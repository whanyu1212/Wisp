from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from wisp.events import (
    ProjectConfigApplied,
    ProviderRetrying,
    RpcCommandFinished,
    RpcCommandStarted,
    TrustRequested,
    TrustResolved,
    wisp_event_from_json,
)
from wisp.rpc import ConfigureCommand, JsonlSubprocessRpcTransport, RpcController
from wisp.rpc.commands import (
    ApprovalCommand,
    CancelCommand,
    PromptCommand,
    RpcCommand,
    ShutdownCommand,
    TrustCommand,
    rpc_command_from_json,
)


class RecordingTransport:
    def __init__(self, events: list[object] | None = None) -> None:
        self.commands: list[RpcCommand] = []
        self._events = events or []
        self.closed = False

    async def send(self, command: RpcCommand) -> None:
        self.commands.append(command)

    async def close(self) -> None:
        self.closed = True

    def events(self) -> AsyncIterator[object]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event


def test_rpc_commands_serialize_as_jsonl_and_parse() -> None:
    command = ApprovalCommand(
        id="approval-1",
        call_id="call-1",
        approved=False,
        reason="not safe",
    )

    line = command.to_json_line()

    assert line.endswith("\n")
    assert json.loads(line) == {
        "id": "approval-1",
        "type": "approval",
        "call_id": "call-1",
        "approved": False,
        "reason": "not safe",
    }
    assert rpc_command_from_json(line) == command


def test_trust_command_serializes_as_jsonl_and_parses() -> None:
    command = TrustCommand(id="trust-1", request_id="req-1", trusted=True)

    line = command.to_json_line()

    assert json.loads(line) == {
        "id": "trust-1",
        "type": "trust",
        "request_id": "req-1",
        "trusted": True,
    }
    assert rpc_command_from_json(line) == command


def test_transient_trust_command_serializes_as_jsonl_and_parses() -> None:
    command = TrustCommand(
        id="trust-1",
        request_id="req-1",
        trusted=False,
        reason="Trust prompt closed",
        transient=True,
    )

    line = command.to_json_line()

    assert json.loads(line) == {
        "id": "trust-1",
        "type": "trust",
        "request_id": "req-1",
        "trusted": False,
        "reason": "Trust prompt closed",
        "transient": True,
    }
    assert rpc_command_from_json(line) == command


def test_trust_events_round_trip_through_json() -> None:
    requested = TrustRequested(request_id="req-1", project_path=Path("/repo"))
    resolved = TrustResolved(request_id="req-1", project_path=Path("/repo"), trusted=True)

    assert wisp_event_from_json(requested.model_dump_json()) == requested
    assert wisp_event_from_json(resolved.model_dump_json()) == resolved


def test_project_config_applied_round_trips_through_json() -> None:
    applied = ProjectConfigApplied(
        provider="openai", model="gpt-5.5", auth_path=Path("/home/u/.wisp/auth.json")
    )

    assert wisp_event_from_json(applied.model_dump_json()) == applied
    # model is optional (provider default).
    minimal = ProjectConfigApplied(provider="fake", auth_path=Path("/tmp/auth.json"))
    assert wisp_event_from_json(minimal.model_dump_json()) == minimal


def test_rpc_commands_allow_protocol_optional_id() -> None:
    command = PromptCommand(prompt="hello")

    line = command.to_json_line()
    parsed = rpc_command_from_json('{"type":"prompt","prompt":"hello"}')

    assert json.loads(line) == {"type": "prompt", "prompt": "hello"}
    assert command.id is None
    assert parsed == command


def test_wisp_event_from_json_returns_typed_event() -> None:
    event = wisp_event_from_json(
        '{"schema_version":3,"type":"rpc.command.finished","command_id":"cmd-1",'
        '"command_type":"prompt","ok":true}'
    )

    assert isinstance(event, RpcCommandFinished)
    assert event.command_id == "cmd-1"
    assert event.ok is True


def test_wisp_event_from_json_parses_provider_retry_progress() -> None:
    retry = ProviderRetrying(
        turn=1,
        provider="openai",
        attempt=2,
        max_attempts=3,
        delay_seconds=0.5,
        reason="rate_limit",
        status_code=429,
    )

    assert wisp_event_from_json(retry.model_dump_json()) == retry


@pytest.mark.parametrize("schema_version", [None, 1, 2, 4])
def test_wisp_event_from_json_rejects_unsupported_schema_version(
    schema_version: int | None,
) -> None:
    payload: dict[str, object] = {
        "type": "rpc.command.finished",
        "command_id": "cmd-1",
        "command_type": "prompt",
        "ok": True,
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version

    with pytest.raises(ValueError, match="Unsupported Wisp event schema_version"):
        wisp_event_from_json(json.dumps(payload))


def test_rpc_controller_sends_typed_commands_and_closes_transport() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        prompt_id = await controller.prompt("hello")
        cancel_id = await controller.cancel(prompt_id)
        approval_id = await controller.approve("call-1", approved=False, reason="not safe")
        configure_id = await controller.configure(provider="openai-codex", model="gpt-5.5")
        shutdown_id = await controller.shutdown()
        await controller.close()

        assert [prompt_id, cancel_id, approval_id, configure_id, shutdown_id] == [
            "prompt-id",
            "cancel-id",
            "approval-id",
            "configure-id",
            "shutdown-id",
        ]
        assert transport.commands == [
            PromptCommand(id="prompt-id", prompt="hello"),
            CancelCommand(id="cancel-id", target_id="prompt-id"),
            ApprovalCommand(
                id="approval-id",
                call_id="call-1",
                approved=False,
                reason="not safe",
            ),
            ConfigureCommand(id="configure-id", provider="openai-codex", model="gpt-5.5"),
            ShutdownCommand(id="shutdown-id"),
        ]
        assert transport.closed is True

    anyio.run(run)


def test_rpc_controller_exposes_transport_events() -> None:
    async def run() -> None:
        expected_events = [
            RpcCommandStarted(command_id="cmd-1", command_type="prompt"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=True),
        ]
        controller = RpcController(RecordingTransport(events=expected_events))

        events = [event async for event in controller.events()]

        assert events == expected_events

    anyio.run(run)


def test_jsonl_subprocess_rpc_transport_round_trips_events(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
command = json.loads(sys.stdin.readline())
started = {
    "schema_version": 3,
    "type": "rpc.command.started",
    "command_id": command["id"],
    "command_type": command["type"],
}
finished = {
    "schema_version": 3,
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}
print(json.dumps(started), flush=True)
print(json.dumps(finished), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == [
            "rpc.command.started",
            "rpc.command.finished",
        ]
        assert isinstance(events[0], RpcCommandStarted)
        assert isinstance(events[1], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[1].ok is True

    anyio.run(run)


def test_jsonl_subprocess_rpc_transport_does_not_block_on_stderr(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
sys.stderr.write("x" * 200000)
sys.stderr.flush()
command = json.loads(sys.stdin.readline())
print(json.dumps({
    "schema_version": 3,
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        with anyio.fail_after(5):
            events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == ["rpc.command.finished"]
        assert isinstance(events[0], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[0].ok is True

    anyio.run(run)
