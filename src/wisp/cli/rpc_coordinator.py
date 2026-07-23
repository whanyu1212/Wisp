"""State machine for scheduling RPC commands independently from stdin transport."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import anyio

from wisp.agent.messages import Message
from wisp.rpc.commands import QUEUE_RPC_COMMAND_TYPES
from wisp.sessions.jsonl import JsonlSession


@dataclass(frozen=True)
class _RpcInputCommand:
    command: dict[str, object]


@dataclass(frozen=True)
class _RpcInputClosed:
    pass


@dataclass(frozen=True)
class _RpcCommandCompleted:
    command_id: str
    command_type: Literal["prompt", "compact", "get_session_stats"]
    ok: bool
    history: tuple[Message, ...] | None
    entry_count: int


@dataclass
class _RpcSessionState:
    session: JsonlSession | None
    history: tuple[Message, ...]
    entry_count: int


@dataclass(frozen=True)
class _RpcRunningCommand:
    command_id: str
    command_type: Literal["prompt", "compact", "get_session_stats"]
    cancel_scope: anyio.CancelScope


@dataclass(frozen=True)
class _RpcDispatchResult:
    running_command: _RpcRunningCommand | None
    selected_session: JsonlSession | None = None
    should_shutdown: bool = False


@dataclass(frozen=True)
class _RpcCancelResult:
    outcome: Literal["running", "queued", "missing"]
    command: dict[str, object] | None = None


type _RpcControlEvent = _RpcInputCommand | _RpcInputClosed | _RpcCommandCompleted
type RpcDispatch = Callable[
    [dict[str, object], _RpcRunningCommand | None],
    _RpcDispatchResult,
]
type RpcReject = Callable[[dict[str, object], str], None]
type RpcCommandType = Callable[[dict[str, object]], str]

_MAX_QUEUED_RPC_COMMANDS = 100
_BYPASS_QUEUE_COMMANDS = QUEUE_RPC_COMMAND_TYPES | {"approval", "cancel", "trust"}


class RpcControlReceiver(Protocol):
    async def receive(self) -> _RpcControlEvent: ...


class RpcCoordinator:
    """Own active-command, queue, session, and input-closure transitions."""

    def __init__(
        self,
        session_state: _RpcSessionState,
        *,
        input_closed_handlers: tuple[Callable[[], None], ...] = (),
        max_queued_commands: int = _MAX_QUEUED_RPC_COMMANDS,
        input_closed_type: type[object] = _RpcInputClosed,
        command_completed_type: type[object] = _RpcCommandCompleted,
    ) -> None:
        if max_queued_commands < 0:
            raise ValueError("max_queued_commands must be non-negative")
        self.session_state = session_state
        self.running_command: _RpcRunningCommand | None = None
        self.queued_commands: deque[dict[str, object]] = deque()
        self.input_closed = False
        self._input_closed_handlers = input_closed_handlers
        self._max_queued_commands = max_queued_commands
        self._input_closed_type = input_closed_type
        self._command_completed_type = command_completed_type

    async def run(
        self,
        receive: RpcControlReceiver,
        *,
        dispatch: RpcDispatch,
        reject: RpcReject,
        command_type: RpcCommandType,
    ) -> bool:
        """Process control events until EOF drains or shutdown is dispatched."""

        while True:
            if self.running_command is None and self.queued_commands:
                if self._dispatch(self.queued_commands.popleft(), dispatch=dispatch):
                    return True
                continue
            if self.input_closed and self.running_command is None and not self.queued_commands:
                return False
            event = await receive.receive()
            if self.handle_event(
                event,
                dispatch=dispatch,
                reject=reject,
                command_type=command_type,
            ):
                return True

    def handle_event(
        self,
        event: _RpcControlEvent,
        *,
        dispatch: RpcDispatch,
        reject: RpcReject,
        command_type: RpcCommandType,
    ) -> bool:
        """Apply one typed coordinator event and return whether to shut down."""

        if isinstance(event, self._input_closed_type):
            if not self.input_closed:
                self.input_closed = True
                for handler in self._input_closed_handlers:
                    handler()
            return False
        if isinstance(event, self._command_completed_type):
            completed = cast(_RpcCommandCompleted, event)
            running = self.running_command
            if (
                running is not None
                and completed.command_id == running.command_id
                and completed.command_type == running.command_type
            ):
                self.running_command = None
                self.session_state.entry_count = completed.entry_count
                if completed.history is not None:
                    self.session_state.history = completed.history
            return False

        command = cast(_RpcInputCommand, event).command
        selected_type = command_type(command)
        if self.running_command is not None and selected_type not in _BYPASS_QUEUE_COMMANDS:
            if len(self.queued_commands) >= self._max_queued_commands:
                reject(command, "RPC command queue is full while another RPC command is running")
            else:
                self.queued_commands.append(command)
            return False
        return self._dispatch(command, dispatch=dispatch)

    def cancel(self, target_id: str) -> _RpcCancelResult:
        """Cancel an active command or remove the first queued command with this id."""

        if self.running_command is not None and self.running_command.command_id == target_id:
            self.running_command.cancel_scope.cancel()
            return _RpcCancelResult("running")
        queued_target = next(
            (queued for queued in self.queued_commands if queued.get("id") == target_id),
            None,
        )
        if queued_target is None:
            return _RpcCancelResult("missing")
        self.queued_commands.remove(queued_target)
        return _RpcCancelResult("queued", command=queued_target)

    def _dispatch(self, command: dict[str, object], *, dispatch: RpcDispatch) -> bool:
        result = dispatch(command, self.running_command)
        self.running_command = result.running_command
        if result.selected_session is not None:
            self.session_state.session = result.selected_session
        return result.should_shutdown


__all__ = ["RpcCoordinator"]
