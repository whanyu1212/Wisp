"""Associate successful script transactions with their durable tool results."""

from collections.abc import Mapping

from pydantic import JsonValue

from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import ToolExecutionEnded
from wisp.tools.codemode.calls import CODEMODE
from wisp.tools.codemode.state import ScriptState


class RunScriptState:
    """Own one run's state and snapshots awaiting successful result persistence.

    Scripts run serially. A transaction stages its snapshot here after successful
    execution; CodingSession attaches it to the same entry as the successful tool
    result. Interrupted calls never acquire a durable snapshot through repair.
    """

    def __init__(self, initial: Mapping[str, JsonValue]) -> None:
        self._pending: dict[str, dict[str, JsonValue]] = {}
        self.state = ScriptState(initial, on_commit=self._stage)

    def _stage(self, snapshot: dict[str, JsonValue]) -> None:
        call_id = ConfiguredToolExecutor.nested_host_call_id()
        if call_id is None:
            raise RuntimeError("Codemode state commit has no owning tool call")
        self._pending[call_id] = snapshot

    def take_result(self, event: ToolExecutionEnded) -> dict[str, JsonValue] | None:
        """Consume a snapshot only for a successful codemode result."""
        if event.name != CODEMODE:
            return None
        snapshot = self._pending.pop(event.call_id, None)
        return None if event.is_error else snapshot
