"""Reload requests must be rejected, not deferred, while work is active."""

from __future__ import annotations

from typing import Literal

import anyio
import pytest

from tests.rpc.coordinator_support import input_command, parsed_command
from wisp.rpc.commands import ParsedRpcCommand
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _RpcDispatchResult,
    _RpcRunningCommand,
    _RpcSessionState,
)


@pytest.mark.parametrize("busy", ["prompt", "compact", "read", "discovery", "queued", "shutdown"])
def test_reload_rejects_active_or_pending_work(busy: str) -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        scope = anyio.CancelScope()
        if busy in {"prompt", "compact"}:
            kind: Literal["prompt", "compact"] = "prompt" if busy == "prompt" else "compact"
            coordinator.running_command = _RpcRunningCommand("active", kind, scope)
        elif busy in {"read", "discovery"}:
            coordinator.auxiliary_commands["active"] = _RpcRunningCommand(
                "active", "get_git_diff" if busy == "discovery" else "get_messages", scope
            )
        else:
            payload: dict[str, object] = {"type": "shutdown", "id": "queued"}
            if busy == "queued":
                payload = {"type": "prompt", "id": "queued", "prompt": "later"}
            coordinator.queued_commands.append(parsed_command(payload))
        rejected: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand, running: _RpcRunningCommand | None
        ) -> _RpcDispatchResult:
            raise AssertionError("busy reload must not dispatch")

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            assert command.command_type == "reload"
            rejected.append(message)

        assert not await coordinator.handle_event(
            input_command({"type": "reload", "id": "r"}), dispatch=dispatch, reject=reject
        )
        assert len(rejected) == 1 and "idle-only" in rejected[0]
        assert not any(c.command_type == "reload" for c in coordinator.queued_commands)
        assert not scope.cancel_called

    anyio.run(scenario)


def test_idle_reload_dispatches_immediately() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(None, (), 0))
        dispatched: list[str] = []

        async def dispatch(
            command: ParsedRpcCommand, running: _RpcRunningCommand | None
        ) -> _RpcDispatchResult:
            dispatched.append(command.command_type)
            assert running is None
            return _RpcDispatchResult(None)

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            raise AssertionError(message)

        assert not await coordinator.handle_event(
            input_command({"type": "reload", "id": "r"}), dispatch=dispatch, reject=reject
        )
        assert dispatched == ["reload"] and not coordinator.queued_commands

    anyio.run(scenario)
