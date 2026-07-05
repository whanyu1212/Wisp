from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import anyio

from wisp.events import RpcCommandFinished, RpcCommandStarted, wisp_event_from_json
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController
from wisp.rpc.commands import (
    ApprovalCommand,
    CancelCommand,
    ConfigureCommand,
    PromptCommand,
    RpcCommand,
    ShutdownCommand,
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


def test_rpc_commands_allow_protocol_optional_id() -> None:
    command = PromptCommand(prompt="hello")

    line = command.to_json_line()
    parsed = rpc_command_from_json('{"type":"prompt","prompt":"hello"}')

    assert json.loads(line) == {"type": "prompt", "prompt": "hello"}
    assert command.id is None
    assert parsed == command


def test_wisp_event_from_json_returns_typed_event() -> None:
    event = wisp_event_from_json(
        '{"type":"rpc.command.finished","command_id":"cmd-1","command_type":"prompt","ok":true}'
    )

    assert isinstance(event, RpcCommandFinished)
    assert event.command_id == "cmd-1"
    assert event.ok is True


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
    "type": "rpc.command.started",
    "command_id": command["id"],
    "command_type": command["type"],
}
finished = {
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
