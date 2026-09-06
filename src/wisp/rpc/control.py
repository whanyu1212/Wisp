"""Control command execution (cancellation, approval, trust, shutdown) for the RPC frontend."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Protocol, assert_never

from wisp.rpc.commands import (
    ApprovalCommand,
    ApprovalScope,
    CancelCommand,
    ShutdownCommand,
    TrustCommand,
)
from wisp.rpc.coordinator import RpcCoordinator, _RpcRunningCommand
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter

type _RpcControlCommand = CancelCommand | ApprovalCommand | TrustCommand | ShutdownCommand


class RpcApprovalResolver(Protocol):
    def has_pending_approval(self, *, call_id: str) -> bool: ...

    def resolve_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: str | None = None,
        scope: ApprovalScope = "once",
    ) -> bool: ...


class RpcTrustResolver(Protocol):
    async def resolve(self) -> bool: ...

    def resolve_request(
        self,
        *,
        request_id: str,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        release: bool = True,
    ) -> bool: ...

    def release_request(self, *, request_id: str) -> None: ...


def handle_rpc_control_command(
    command: _RpcControlCommand,
    *,
    running_command: _RpcRunningCommand | None,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    trust_gate: RpcTrustResolver | None = None,
    coordinator: RpcCoordinator | None = None,
    defer_until_after_flush: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    command_type = command.type
    if isinstance(command, ShutdownCommand):
        lifecycle.finish()
        return True
    if isinstance(command, CancelCommand):
        handle_rpc_cancel_command(
            command,
            command_id=command_id,
            command_type=command_type,
            running_command=running_command,
            coordinator=coordinator,
            write_event=write_event,
            defer_cancellation=defer_until_after_flush,
        )
        return False
    if isinstance(command, ApprovalCommand):
        handle_rpc_approval_command(
            command,
            command_id=command_id,
            command_type=command_type,
            approval_policy=approval_policy,
            write_event=write_event,
            defer_resolution=defer_until_after_flush,
        )
        return False
    if isinstance(command, TrustCommand):
        if trust_gate is None:
            lifecycle.fail("RPC trust command requires an active trust gate")
            return False
        handle_rpc_trust_command(
            command,
            command_id=command_id,
            command_type=command_type,
            trust_gate=trust_gate,
            write_event=write_event,
            defer_resolution=defer_until_after_flush,
        )
        return False
    assert_never(command)


def handle_rpc_approval_command(
    command: ApprovalCommand,
    *,
    command_id: str,
    command_type: str,
    approval_policy: RpcApprovalResolver,
    write_event: RpcEventWriter,
    defer_resolution: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type=command_type,
        write_event=write_event,
    )
    call_id = command.call_id
    approved = command.approved
    reason = command.reason
    scope = command.scope or "once"
    if not approval_policy.has_pending_approval(call_id=call_id):
        lifecycle.fail(f"No pending tool approval with call_id: {call_id}")
        return
    resolve = partial(
        approval_policy.resolve_approval,
        call_id=call_id,
        approved=approved,
        reason=reason,
        scope=scope,
    )
    if defer_resolution is None:
        if not resolve():
            lifecycle.fail(f"No pending tool approval with call_id: {call_id}")
            return
        lifecycle.finish()
        return

    lifecycle.finish()

    def resolve_after_flush() -> None:
        resolve()

    defer_resolution(resolve_after_flush)


def handle_rpc_trust_command(
    command: TrustCommand,
    *,
    command_id: str,
    command_type: str,
    trust_gate: RpcTrustResolver,
    write_event: RpcEventWriter,
    defer_resolution: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type=command_type,
        write_event=write_event,
    )
    request_id = command.request_id
    trusted = command.trusted
    reason = command.reason
    transient = command.transient
    defer_release = defer_resolution is not None
    if not trust_gate.resolve_request(
        request_id=request_id,
        trusted=trusted,
        reason=reason,
        transient=transient is True,
        release=not defer_release,
    ):
        lifecycle.fail(f"No pending trust request with request_id: {request_id}")
        return
    lifecycle.finish()
    if defer_resolution is not None:
        defer_resolution(partial(trust_gate.release_request, request_id=request_id))


def handle_rpc_cancel_command(
    command: CancelCommand,
    *,
    command_id: str,
    command_type: str,
    running_command: _RpcRunningCommand | None,
    write_event: RpcEventWriter,
    coordinator: RpcCoordinator | None = None,
    defer_cancellation: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type=command_type,
        write_event=write_event,
    )
    target_id = command.target_id
    if (
        running_command is not None
        and running_command.command_id == target_id
        and defer_cancellation is not None
    ):
        lifecycle.finish()
        defer_cancellation(running_command.cancel_scope.cancel)
        return
    if coordinator is None:
        raise RuntimeError("RPC cancellation requires the shared coordinator")
    result = coordinator.cancel(target_id)
    if result.outcome == "running":
        lifecycle.finish()
        return
    queued_target = result.command
    if queued_target is None:
        lifecycle.fail(f"No running or queued RPC command with id: {target_id}")
        return
    target = RpcCommandLifecycle.start(
        command_id=target_id,
        command_type=queued_target.command_type,
        write_event=write_event,
    )
    target.finish(ok=False, error=f"RPC command cancelled: {target_id}")
    lifecycle.finish()
