"""Client/controller abstractions for Wisp JSONL RPC."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import anyio
from anyio.abc import Process

from wisp.events import KnownWispEvent, wisp_event_from_json
from wisp.rpc.commands import (
    ApprovalCommand,
    ApprovalScope,
    CancelCommand,
    ConfigureCommand,
    PromptCommand,
    RpcCommand,
    ShutdownCommand,
    TrustCommand,
)


class RpcTransport(Protocol):
    """Transport used by `RpcController` to send commands and receive events."""

    async def send(self, command: RpcCommand) -> None:
        """Send one typed RPC command."""

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield typed Wisp events from the RPC event stream."""

    async def close(self) -> None:
        """Close the underlying transport."""


class RpcController:
    """High-level controller API for prompt, cancel, approval, and shutdown commands."""

    def __init__(
        self,
        transport: RpcTransport,
        *,
        command_id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._transport = transport
        self._command_id_factory = command_id_factory or _default_command_id

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
        """Send a prompt command and return its command id."""

        selected_id = command_id or self._command_id_factory("prompt")
        await self._transport.send(PromptCommand(id=selected_id, prompt=prompt))
        return selected_id

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str:
        """Request cancellation of a running prompt command."""

        selected_id = command_id or self._command_id_factory("cancel")
        await self._transport.send(CancelCommand(id=selected_id, target_id=target_id))
        return selected_id

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str:
        """Approve or deny a pending tool approval request."""

        selected_id = command_id or self._command_id_factory("approval")
        await self._transport.send(
            ApprovalCommand(
                id=selected_id,
                call_id=call_id,
                approved=approved,
                reason=reason,
                scope=scope,
            )
        )
        return selected_id

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str:
        """Resolve a pending project-trust request."""

        selected_id = command_id or self._command_id_factory("trust")
        await self._transport.send(
            TrustCommand(
                id=selected_id,
                request_id=request_id,
                trusted=trusted,
                reason=reason,
                transient=True if transient else None,
            )
        )
        return selected_id

    async def shutdown(self, *, command_id: str | None = None) -> str:
        """Ask the RPC process to exit cleanly."""

        selected_id = command_id or self._command_id_factory("shutdown")
        await self._transport.send(ShutdownCommand(id=selected_id))
        return selected_id

    async def configure(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear_effort: bool = False,
        command_id: str | None = None,
    ) -> str:
        """Update provider/model/effort settings for future prompt commands.

        ``effort=None`` (the default) leaves any previously configured effort
        tier untouched -- the command's JSON serialization omits unset fields,
        so there is no wire difference between "not specified" and "None."
        Pass ``clear_effort=True`` to explicitly reset effort back to the
        provider's own default.
        """

        selected_id = command_id or self._command_id_factory("configure")
        await self._transport.send(
            ConfigureCommand(
                id=selected_id,
                provider=provider,
                model=model,
                effort=effort,
                clear_effort=clear_effort,
            )
        )
        return selected_id

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield typed events from the underlying transport."""

        return self._transport.events()

    async def close(self) -> None:
        """Close the underlying transport."""

        await self._transport.close()


class JsonlSubprocessRpcTransport:
    """Subprocess transport for `wisp --mode rpc` JSONL stdin/stdout."""

    def __init__(self, process: Process) -> None:
        self._process = process

    @classmethod
    async def start(
        cls,
        command: Sequence[str] | None = None,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stderr: int | None = subprocess.DEVNULL,
    ) -> JsonlSubprocessRpcTransport:
        """Start a subprocess running Wisp RPC mode.

        Stderr defaults to ``DEVNULL`` so an undrained stderr pipe cannot block
        the RPC event stream. Pass ``stderr=subprocess.PIPE`` only if another
        task will drain it.
        """

        selected_command = tuple(command) if command is not None else _default_rpc_command()
        process = await anyio.open_process(
            selected_command,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stderr=stderr,
        )
        return cls(process)

    async def send(self, command: RpcCommand) -> None:
        """Send one command as a JSONL line to subprocess stdin."""

        if self._process.stdin is None:
            raise RuntimeError("RPC subprocess stdin is closed")
        await self._process.stdin.send(command.to_json_line().encode("utf-8"))

    async def close(self) -> None:
        """Close stdin and wait briefly for the subprocess to exit."""

        if self._process.stdin is not None:
            await self._process.stdin.aclose()
        with anyio.move_on_after(2) as cancel_scope:
            await self._process.wait()
        if cancel_scope.cancel_called:
            self._process.terminate()
            await self._process.wait()

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield typed events parsed from subprocess stdout."""

        return self._events()

    async def _events(self) -> AsyncIterator[KnownWispEvent]:
        if self._process.stdout is None:
            return
        buffer = bytearray()
        while True:
            try:
                chunk = await self._process.stdout.receive()
            except anyio.EndOfStream:
                break
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break
                line = bytes(buffer[:newline_index]).decode("utf-8", errors="replace")
                del buffer[: newline_index + 1]
                if line:
                    yield wisp_event_from_json(line)
        if buffer:
            line = bytes(buffer).decode("utf-8", errors="replace")
            if line:
                yield wisp_event_from_json(line)


def _default_command_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _default_rpc_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "wisp", "--mode", "rpc")
