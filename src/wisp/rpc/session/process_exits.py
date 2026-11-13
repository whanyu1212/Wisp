"""Background processes: exits announced to frontends and held for the next prompt."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from wisp.events import (
    PROCESS_COMMAND_SUMMARY_MAX_CHARS,
    BackgroundProcess,
    EndedProcessState,
    ProcessEnded,
    WispEvent,
)
from wisp.messages import ProcessNotice
from wisp.tools.shell.supervisor import ProcessExit, ProcessState, ProcessSupervisor

# Wake runs that may start between the user's prompts.
MAX_WAKE_RUNS = 3


class ProcessExitNotices:
    """Publish each background process's exit and keep the ones the model asked for.

    The supervisor calls ``record`` when a started process stops on its own. Every
    exit is published as ``process.ended``. One the model started with
    ``notify_on_exit`` is also held until the session saves a prompt carrying it:
    the coding session reads ``pending``, calls ``mark_delivered`` once that
    prompt is saved, and calls ``restore`` if the run rolls the prompt back. A
    poll that already returned the exit makes its notice redundant, so
    ``pending`` drops those.

    Such an exit also makes a wake due: the host asks ``take_wake`` whenever it is
    idle and, if it may, starts a run that carries the notices. At most
    ``MAX_WAKE_RUNS`` wake runs start between the user's prompts, so a process that
    keeps restarting itself cannot keep the agent running unattended.

    Processes belong to the session that started them: the host cancels them when
    the session changes and calls ``clear``, so no notice reaches another session.
    """

    def __init__(self, supervisor: ProcessSupervisor, publish: Callable[[WispEvent], None]) -> None:
        self._supervisor = supervisor
        self._publish = publish
        self._pending: list[ProcessNotice] = []
        self._wake_due = False
        self._wake_runs = 0
        self._wake_listener: Callable[[], None] | None = None

    def set_wake_listener(self, listener: Callable[[], None] | None) -> None:
        """Call ``listener`` each time an exit makes a wake due.

        Args:
            listener (Callable[[], None] | None): Tells the host to look for a wake,
                or None to stop telling it.
        """

        self._wake_listener = listener

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
            self._wake_due = True
        duration = exit.ended_at - exit.started_at
        self._publish(
            ProcessEnded(
                process_id=exit.process_id,
                command=command_summary(exit.command),
                state=_ended_state(exit.state),
                exit_code=exit.exit_code,
                duration_ms=max(0, int(duration.total_seconds() * 1000)),
                notify=exit.notify,
            )
        )
        if exit.notify and self._wake_listener is not None:
            self._wake_listener()

    def pending(self) -> tuple[ProcessNotice, ...]:
        """Return the held notices whose exit no poll has returned yet.

        Notices a poll made redundant are forgotten; the rest stay held until
        ``mark_delivered``.

        Returns:
            tuple[ProcessNotice, ...]: The notices, in the order the processes stopped.
        """

        self._pending = [
            notice
            for notice in self._pending
            if not self._supervisor.is_terminal_reported(notice.process_id)
        ]
        return tuple(self._pending)

    def mark_delivered(self, notices: Sequence[ProcessNotice]) -> None:
        """Forget ``notices``, which a saved prompt now carries.

        Exits recorded since ``pending`` returned them stay held.

        Args:
            notices (Sequence[ProcessNotice]): Notices ``pending`` returned.
        """

        delivered = {notice.process_id for notice in notices}
        self._pending = [notice for notice in self._pending if notice.process_id not in delivered]

    def restore(self, notices: Sequence[ProcessNotice]) -> None:
        """Hold ``notices`` again, ahead of any held since, after their prompt was rolled back.

        Args:
            notices (Sequence[ProcessNotice]): Notices ``mark_delivered`` had forgotten.
        """

        held = {notice.process_id for notice in self._pending}
        self._pending = [
            *(notice for notice in notices if notice.process_id not in held),
            *self._pending,
        ]

    @property
    def wake_due(self) -> bool:
        """Whether an exit has made a wake due that ``take_wake`` has not claimed."""

        return self._wake_due

    def take_wake(self) -> bool:
        """Claim the wake an exit made due, if a wake run may start for it.

        A due wake is claimed once, whether or not it is allowed, so a run that
        fails or is cancelled does not wake the agent again for the same exits.
        It is allowed while notices are still held and fewer than
        ``MAX_WAKE_RUNS`` wake runs have started since the user's last prompt.

        Returns:
            bool: True when the host should start a wake run now.
        """

        if not self._wake_due:
            return False
        self._wake_due = False
        if self._wake_runs >= MAX_WAKE_RUNS or not self.pending():
            return False
        self._wake_runs += 1
        return True

    def user_prompted(self) -> None:
        """Allow wake runs again, after the user sent a prompt of their own."""

        self._wake_runs = 0

    def clear(self) -> None:
        """Forget every held notice and wake, as when the session changes."""

        self._pending.clear()
        self._wake_due = False
        self._wake_runs = 0


def _ended_state(state: ProcessState) -> EndedProcessState:
    # The supervisor announces neither a running nor a cancelled process.
    if state == "running" or state == "cancelled":
        raise ValueError(f"A process that is {state} has not ended on its own")
    return state


def background_processes(supervisor: ProcessSupervisor) -> tuple[BackgroundProcess, ...]:
    """Describe the supervisor's running background processes for frontends.

    Args:
        supervisor (ProcessSupervisor): The supervisor that started them.

    Returns:
        tuple[BackgroundProcess, ...]: One entry per running process, oldest first.
    """

    return tuple(
        BackgroundProcess(
            process_id=process.process_id,
            command=command_summary(process.command),
            started_at_ms=max(0, int(process.started_at.timestamp() * 1000)),
            notify=process.notify,
        )
        for process in supervisor.running_processes()
    )


def command_summary(command: str) -> str:
    """Cut a command to the length events carry for display.

    Args:
        command (str): The command as started.

    Returns:
        str: The command, or its start and an ellipsis.
    """

    if len(command) <= PROCESS_COMMAND_SUMMARY_MAX_CHARS:
        return command
    return command[: PROCESS_COMMAND_SUMMARY_MAX_CHARS - 1] + "…"
