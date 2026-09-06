"""Typed start, error, and finish helpers for RPC command execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, Self
from uuid import uuid4

from wisp.events import ErrorEvent, RpcCommandFinished, RpcCommandStarted, WispEvent

_MAX_RPC_COMMAND_ERROR_CHARS = 1_000

type RpcEventWriter = Callable[[WispEvent], None]


class RpcLifecycleCommand(Protocol):
    @property
    def id(self) -> str | None: ...

    @property
    def type(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RpcCommandLifecycle:
    """One command's Started/Finished identity after the ingress boundary."""

    command_id: str
    command_type: str
    write_event: RpcEventWriter

    @classmethod
    def start(
        cls,
        *,
        command_id: str | None,
        command_type: str,
        write_event: RpcEventWriter,
    ) -> Self:
        resolved_id = command_id or uuid4().hex
        write_event(RpcCommandStarted(command_id=resolved_id, command_type=command_type))
        return cls(command_id=resolved_id, command_type=command_type, write_event=write_event)

    @classmethod
    def for_command(cls, command: RpcLifecycleCommand, *, write_event: RpcEventWriter) -> Self:
        return cls.start(
            command_id=command.id,
            command_type=command.type,
            write_event=write_event,
        )

    @classmethod
    def bind(
        cls,
        *,
        command_id: str,
        command_type: str,
        write_event: RpcEventWriter,
    ) -> Self:
        """Attach to a command whose Started event was already emitted."""

        return cls(command_id=command_id, command_type=command_type, write_event=write_event)

    def fail(self, message: str) -> None:
        write_rpc_command_error(
            command_id=self.command_id,
            command_type=self.command_type,
            message=message,
            write_event=self.write_event,
        )

    def finish(self, *, ok: bool = True, error: str | None = None) -> None:
        self.write_event(
            RpcCommandFinished(
                command_id=self.command_id,
                command_type=self.command_type,
                ok=ok,
                error=error,
            )
        )


def write_rpc_command_error(
    *,
    command_id: str,
    command_type: str,
    message: str,
    write_event: RpcEventWriter,
) -> None:
    if len(message) > _MAX_RPC_COMMAND_ERROR_CHARS:
        message = message[: _MAX_RPC_COMMAND_ERROR_CHARS - 3] + "..."
    write_event(ErrorEvent(message=message))
    write_event(
        RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=False,
            error=message,
        )
    )
