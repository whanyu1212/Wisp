"""Background-process exits: announced to frontends, held for the model's next prompt."""

from __future__ import annotations

from collections.abc import Callable

from wisp.events import (
    PROCESS_COMMAND_SUMMARY_MAX_CHARS,
    EndedProcessState,
    ProcessEnded,
    WispEvent,
)
from wisp.messages import ProcessNotice
from wisp.tools.shell.supervisor import ProcessExit, ProcessState, ProcessSupervisor


class ProcessExitNotices:
    """Publish each background process's exit and keep the ones the model asked for.

    The supervisor calls ``record`` when a started process stops on its own. Every
    exit is published as ``process.ended``. One the model started with
    ``notify_on_exit`` is also held until the session's next prompt, which ``take``
    hands to the coding session. A poll that already returned the exit makes its
    notice redundant, so ``take`` drops those.

    Processes belong to the session that started them: the host cancels them when
    the session changes and calls ``clear``, so no notice reaches another session.
    """

    def __init__(self, supervisor: ProcessSupervisor, publish: Callable[[WispEvent], None]) -> None:
        self._supervisor = supervisor
        self._publish = publish
        self._pending: list[ProcessNotice] = []

    def record(self, exit: ProcessExit) -> None:
        """Publish one exit, and hold its notice if the model asked to hear of it.

        Args:
            exit (ProcessExit): The process that stopped.
        """

        # Held before publishing, so a frontend that fails to take the event
        # cannot cost the model its notice.
        if exit.notify:
            self._pending.append(
                ProcessNotice(
                    process_id=exit.process_id,
                    command=exit.command,
                    state=_ended_state(exit.state),
                    exit_code=exit.exit_code,
                )
            )
        duration = exit.ended_at - exit.started_at
        self._publish(
            ProcessEnded(
                process_id=exit.process_id,
                command=_command_summary(exit.command),
                state=_ended_state(exit.state),
                exit_code=exit.exit_code,
                duration_ms=max(0, int(duration.total_seconds() * 1000)),
                notify=exit.notify,
            )
        )

    def take(self) -> tuple[ProcessNotice, ...]:
        """Return and forget the held notices whose exit no poll has returned yet.

        Returns:
            tuple[ProcessNotice, ...]: The notices, in the order the processes stopped.
        """

        pending, self._pending = self._pending, []
        return tuple(
            notice
            for notice in pending
            if not self._supervisor.is_terminal_reported(notice.process_id)
        )

    def clear(self) -> None:
        """Forget every held notice, as when the session changes."""

        self._pending.clear()


def _ended_state(state: ProcessState) -> EndedProcessState:
    # The supervisor announces neither a running nor a cancelled process.
    if state == "running" or state == "cancelled":
        raise ValueError(f"A process that is {state} has not ended on its own")
    return state


def _command_summary(command: str) -> str:
    if len(command) <= PROCESS_COMMAND_SUMMARY_MAX_CHARS:
        return command
    return command[: PROCESS_COMMAND_SUMMARY_MAX_CHARS - 1] + "…"
