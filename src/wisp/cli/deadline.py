"""Stop a one-shot run at a wall-clock deadline (`wisp -p ... --timeout SECONDS`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import anyio

DEADLINE_EXIT_CODE = 124
"""Exit status for a run stopped by its deadline, as coreutils `timeout` uses."""

_RETRY_SECONDS = 0.05


class StoppableRun(Protocol):
    """The part of `CodingSession` a deadline needs: a cooperative stop request."""

    def cancel_run(self) -> bool:
        """Ask the active run to stop; return whether a running loop accepted it."""
        ...


@dataclass(slots=True)
class RunDeadline:
    """A wall-clock limit on one run that stops it cooperatively, then forcibly.

    At the deadline the run is asked to stop through `CodingSession.cancel_run`,
    which settles running tools and still publishes `AgentCompleted` with the
    run's totals. A run that has not reached its loop yet does not accept the
    request, so it is retried. If the run has not finished `grace_seconds` after
    the deadline, the work is cancelled outright, and no `AgentCompleted` follows.

    Attributes:
        seconds (float): Time from the start of `run` until the deadline.
        report (Callable[[str], None]): Shows the deadline message once, when it
            is reached, before the run's own cancellation events.
        grace_seconds (float): How long a stopping run may take to finish.
        reached (bool): Whether the deadline was reached.
        forced (bool): Whether the run had to be cancelled outright.
    """

    seconds: float
    report: Callable[[str], None]
    grace_seconds: float = 10.0
    reached: bool = False
    forced: bool = False
    _agent: StoppableRun | None = field(default=None, init=False)

    def attach(self, agent: StoppableRun) -> None:
        """Name the session whose run the deadline stops."""

        self._agent = agent

    async def run(self, work: Callable[[], Awaitable[None]]) -> None:
        """Run ``work`` under the deadline.

        Args:
            work (Callable[[], Awaitable[None]]): Builds the session, calls
                `attach`, and consumes its run.

        Raises:
            Exception: Whatever ``work`` raised, unless the deadline was reached.
                A run stopped by its deadline is expected to end as cancelled or
                with an error, and the deadline message already explains why.
        """

        failure: Exception | None = None
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(self._enforce, task_group.cancel_scope)
            try:
                await work()
            except Exception as exc:  # noqa: BLE001 - re-raised below, outside the group
                failure = exc
            finally:
                # The work is over, so the watchdog has nothing left to stop.
                task_group.cancel_scope.cancel()
        if failure is not None and not self.reached:
            raise failure

    async def _enforce(self, scope: anyio.CancelScope) -> None:
        await anyio.sleep(self.seconds)
        self.reached = True
        self.report(f"Run stopped: it reached its {self.seconds:g} s time limit (--timeout).")
        with anyio.move_on_after(self.grace_seconds):
            while self._agent is None or not self._agent.cancel_run():
                await anyio.sleep(_RETRY_SECONDS)
            # The accepted run winds down; finishing the work cancels this task.
            await anyio.sleep_forever()
        self.forced = True
        scope.cancel()
