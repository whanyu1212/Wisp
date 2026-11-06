"""Progress a running tool can report while it works."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OutputProgress:
    """A tool's output so far.

    Attributes:
        stdout_tail (str): The end of standard output so far.
        stderr_tail (str): The end of standard error so far.
        stdout_bytes (int): Bytes standard output has produced so far.
        stderr_bytes (int): Bytes standard error has produced so far.
    """

    stdout_tail: str
    stderr_tail: str
    stdout_bytes: int
    stderr_bytes: int


class ToolProgress(Protocol):
    """Where a running tool reports progress.

    ``report`` never blocks and never fails: the newest report replaces any the
    frontend has not received yet, and reports after the call ends are ignored.
    """

    def report(self, progress: OutputProgress) -> None:
        """Replace the call's latest progress.

        Args:
            progress (OutputProgress): The tool's output so far.
        """
        ...
