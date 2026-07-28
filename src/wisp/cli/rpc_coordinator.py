"""State machine for scheduling RPC commands independently from stdin transport."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import anyio

from wisp.agent.messages import Message
from wisp.events import WispEvent
from wisp.rpc.commands import QUEUE_RPC_COMMAND_TYPES
from wisp.sessions.jsonl import JsonlSession

type _SequentialRpcCommandType = Literal[
    "prompt",
    "compact",
    "get_session_stats",
    "get_messages",
    "get_sessions",
    "select_session",
    "clone_session",
    "fork_session",
    "get_session_tree",
    "navigate_session_tree",
    "set_session_name",
]


@dataclass(frozen=True)
class _RpcInputCommand:
    command: dict[str, object]


@dataclass(frozen=True)
class _RpcInputClosed:
    pass


@dataclass(frozen=True)
class _RpcCommandCompleted:
    command_id: str
    command_type: _SequentialRpcCommandType
    ok: bool
    history: tuple[Message, ...] | None
    entry_count: int
    selected_session: JsonlSession | None = None
    post_apply_events: tuple[WispEvent, ...] = ()
    session_name: str | None = None
    session_name_updated: bool = False


@dataclass(frozen=True)
class _RpcPromptReady:
    command_id: str


@dataclass
class _RpcSessionState:
    session: JsonlSession | None
    history: tuple[Message, ...]
    entry_count: int
    name: str | None = None


@dataclass(frozen=True)
class _RpcRunningCommand:
    command_id: str
    command_type: _SequentialRpcCommandType
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


type _RpcControlEvent = _RpcInputCommand | _RpcInputClosed | _RpcCommandCompleted | _RpcPromptReady
type RpcDispatch = Callable[
    [dict[str, object], _RpcRunningCommand | None],
    _RpcDispatchResult,
]
type RpcReject = Callable[[dict[str, object], str], None]
type RpcCommandType = Callable[[dict[str, object]], str]
type RpcCompletionEventWriter = Callable[[WispEvent], None]

_MAX_QUEUED_RPC_COMMANDS = 100
_ACTIVE_COMMAND_BYPASS_COMMANDS = QUEUE_RPC_COMMAND_TYPES | {
    "approval",
    "cancel",
    "get_commands",
    "get_state",
    "trust",
}


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
        prompt_ready_type: type[object] = _RpcPromptReady,
        completion_event_writer: RpcCompletionEventWriter | None = None,
    ) -> None:
        if max_queued_commands < 0:
            raise ValueError("max_queued_commands must be non-negative")
        self.session_state = session_state
        self.running_command: _RpcRunningCommand | None = None
        self.queued_commands: deque[dict[str, object]] = deque()
        self.pending_prompt_queue_commands: deque[dict[str, object]] = deque()
        self.input_closed = False
        self._input_closed_handlers = input_closed_handlers
        self._max_queued_commands = max_queued_commands
        self._input_closed_type = input_closed_type
        self._command_completed_type = command_completed_type
        self._prompt_ready_type = prompt_ready_type
        self._prompt_queue_ready = False
        self._completion_event_writer = completion_event_writer

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
            if self.running_command is None and self.pending_prompt_queue_commands:
                if self._dispatch(
                    self.pending_prompt_queue_commands.popleft(),
                    dispatch=dispatch,
                ):
                    return True
                continue
            if self.running_command is None and self.queued_commands:
                if self._dispatch(self.queued_commands.popleft(), dispatch=dispatch):
                    return True
                continue
            if (
                self.input_closed
                and self.running_command is None
                and not self.pending_prompt_queue_commands
                and not self.queued_commands
            ):
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
                self._prompt_queue_ready = False
                self.session_state.entry_count = completed.entry_count
                if completed.history is not None:
                    self.session_state.history = completed.history
                if getattr(completed, "session_name_updated", False):
                    self.session_state.name = getattr(completed, "session_name", None)
                selected_session = getattr(completed, "selected_session", None)
                if completed.ok and selected_session is not None:
                    self.session_state.session = selected_session
                if self._completion_event_writer is not None:
                    for queued_event in getattr(completed, "post_apply_events", ()):
                        self._completion_event_writer(queued_event)
            return False
        if isinstance(event, self._prompt_ready_type):
            ready = cast(_RpcPromptReady, event)
            running = self.running_command
            if (
                running is not None
                and running.command_type == "prompt"
                and ready.command_id == running.command_id
            ):
                self._prompt_queue_ready = True
                return self._dispatch_pending_queue_commands(dispatch=dispatch)
            return False

        command = cast(_RpcInputCommand, event).command
        selected_type = command_type(command)
        running = self.running_command
        prompt_queue_not_ready = (
            running is not None
            and running.command_type == "prompt"
            and selected_type in QUEUE_RPC_COMMAND_TYPES
            and not self._prompt_queue_ready
        )
        if prompt_queue_not_ready:
            self._enqueue_command(
                command,
                queue=self.pending_prompt_queue_commands,
                reject=reject,
            )
            return False
        if running is not None and (selected_type not in _ACTIVE_COMMAND_BYPASS_COMMANDS):
            self._enqueue_command(command, queue=self.queued_commands, reject=reject)
            return False
        return self._dispatch(command, dispatch=dispatch)

    def cancel(self, target_id: str) -> _RpcCancelResult:
        """Cancel an active command or remove the first queued command with this id."""

        if self.running_command is not None and self.running_command.command_id == target_id:
            self.running_command.cancel_scope.cancel()
            return _RpcCancelResult("running")
        for queue in (self.pending_prompt_queue_commands, self.queued_commands):
            queued_target = next(
                (queued for queued in queue if queued.get("id") == target_id),
                None,
            )
            if queued_target is not None:
                queue.remove(queued_target)
                return _RpcCancelResult("queued", command=queued_target)
        return _RpcCancelResult("missing")

    def _dispatch(self, command: dict[str, object], *, dispatch: RpcDispatch) -> bool:
        previous_running = self.running_command
        result = dispatch(command, previous_running)
        self.running_command = result.running_command
        if result.running_command is not previous_running:
            self._prompt_queue_ready = False
        if result.selected_session is not None:
            self.session_state.session = result.selected_session
        return result.should_shutdown

    def _dispatch_pending_queue_commands(
        self,
        *,
        dispatch: RpcDispatch,
    ) -> bool:
        while self.pending_prompt_queue_commands:
            command = self.pending_prompt_queue_commands.popleft()
            if self._dispatch(command, dispatch=dispatch):
                return True
        return False

    def _enqueue_command(
        self,
        command: dict[str, object],
        *,
        queue: deque[dict[str, object]],
        reject: RpcReject,
    ) -> None:
        queued_count = len(self.pending_prompt_queue_commands) + len(self.queued_commands)
        if queued_count >= self._max_queued_commands:
            reject(command, "RPC command queue is full while another RPC command is running")
            return
        queue.append(command)


__all__ = ["RpcCoordinator"]
