"""Pure presentation state for resumable Bash process lifecycles.

The RPC and persisted transcript retain every individual tool call for auditability.
This module only coalesces those observations for frontend display, keyed by the
stable managed-process identifier carried by Bash poll/cancel arguments.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from wisp.events import ManagedProcessState
from wisp.tools.truncation import truncate_text_tail

ProcessOperation = Literal["poll", "cancel"]
ProcessDisplayState = Literal[
    "polling",
    "cancelling",
    "running",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "poll_denied",
    "cancel_denied",
    "poll_interrupted",
    "cancel_interrupted",
    "poll_failed",
    "cancel_failed",
    "observed",
]

PROCESS_OUTPUT_MAX_BYTES = 64 * 1024
PROCESS_OUTPUT_MAX_LINES = 500
_PROCESS_PREVIEW_MAX_BYTES = 2_000
_PROCESS_PREVIEW_MAX_LINES = 8


@dataclass(frozen=True, slots=True)
class ProcessCallIdentity:
    """Validated process correlation carried by one Bash request."""

    process_id: str
    operation: ProcessOperation


@dataclass(frozen=True, slots=True)
class ProcessLifecyclePresentation:
    """Bounded immutable snapshot consumed by a process lifecycle card."""

    process_id: str
    display_state: ProcessDisplayState
    poll_count: int
    call_count: int
    detail: str
    full_output: str
    retained_output: str
    source_truncated: bool
    ui_dropped_bytes: int

    @property
    def terminal(self) -> bool:
        return self.display_state in {"completed", "failed", "timed_out", "cancelled"}

    @property
    def operation_settled(self) -> bool:
        """Whether the audited poll/cancel call has reached a resolved UI state."""

        return self.display_state in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "poll_denied",
            "cancel_denied",
            "poll_interrupted",
            "cancel_interrupted",
            "poll_failed",
            "cancel_failed",
        }


@dataclass(slots=True)
class ProcessLifecycle:
    """Accumulate incremental process observations under explicit display bounds."""

    process_id: str
    poll_count: int = 0
    call_count: int = 0
    display_state: ProcessDisplayState = "observed"
    _output: str = ""
    _source_truncated: bool = False
    _ui_dropped_bytes: int = 0

    @classmethod
    def from_presentation(
        cls,
        presentation: ProcessLifecyclePresentation,
    ) -> ProcessLifecycle:
        """Restore retained presentation state when history transfers to live output."""

        return cls(
            process_id=presentation.process_id,
            poll_count=presentation.poll_count,
            call_count=presentation.call_count,
            display_state=presentation.display_state,
            _output=presentation.retained_output,
            _source_truncated=presentation.source_truncated,
            _ui_dropped_bytes=presentation.ui_dropped_bytes,
        )

    def begin(self, operation: ProcessOperation) -> ProcessLifecyclePresentation:
        self.call_count += 1
        if operation == "poll":
            self.poll_count += 1
            self.display_state = "polling"
        else:
            self.display_state = "cancelling"
        return self.presentation()

    def observe(
        self,
        *,
        operation: ProcessOperation,
        state: ManagedProcessState | None,
        stdout: str = "",
        stderr: str = "",
        source_truncated: bool = False,
        source_dropped_bytes: int = 0,
        fallback_output: str = "",
        failure_reason: str = "",
        failed: bool = False,
    ) -> ProcessLifecyclePresentation:
        output = _process_output_chunk(stdout, stderr) or fallback_output
        if failure_reason and not (
            output == failure_reason or output.startswith(f"{failure_reason}\n")
        ):
            output = f"{failure_reason}\n{output}" if output else failure_reason
        if output:
            self._append_output(output)
        self._source_truncated = (
            self._source_truncated or source_truncated or source_dropped_bytes > 0
        )
        if failed and state == "completed":
            self.display_state = "failed"
        elif state is not None:
            self.display_state = state
        elif failed:
            self.display_state = "poll_failed" if operation == "poll" else "cancel_failed"
        else:
            self.display_state = "observed"
        return self.presentation()

    def deny(
        self,
        operation: ProcessOperation,
        reason: str = "",
    ) -> ProcessLifecyclePresentation:
        if reason:
            self._append_output(reason)
        self.display_state = "poll_denied" if operation == "poll" else "cancel_denied"
        return self.presentation()

    def interrupt(self, operation: ProcessOperation) -> ProcessLifecyclePresentation:
        self.display_state = "poll_interrupted" if operation == "poll" else "cancel_interrupted"
        return self.presentation()

    def presentation(self) -> ProcessLifecyclePresentation:
        full_output = self._display_output()
        return ProcessLifecyclePresentation(
            process_id=self.process_id,
            display_state=self.display_state,
            poll_count=self.poll_count,
            call_count=self.call_count,
            detail=_tail_preview(full_output) if full_output else "(no process output yet)",
            full_output=full_output,
            retained_output=self._output,
            source_truncated=self._source_truncated,
            ui_dropped_bytes=self._ui_dropped_bytes,
        )

    def _append_output(self, output: str) -> None:
        normalized = output.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not normalized:
            return
        combined = f"{self._output}\n{normalized}" if self._output else normalized
        before_bytes = len(combined.encode("utf-8"))
        bounded = truncate_text_tail(
            combined,
            max_bytes=PROCESS_OUTPUT_MAX_BYTES,
            max_lines=PROCESS_OUTPUT_MAX_LINES,
        )
        retained_bytes = len(bounded.text.encode("utf-8"))
        if bounded.truncated:
            self._ui_dropped_bytes += max(0, before_bytes - retained_bytes)
        self._output = bounded.text.strip("\n")

    def _display_output(self) -> str:
        if self._ui_dropped_bytes <= 0:
            return self._output
        marker = f"… {self._ui_dropped_bytes} earlier process-output bytes omitted by TUI"
        return f"{marker}\n{self._output}" if self._output else marker


def process_call_identity(name: str, arguments: object) -> ProcessCallIdentity | None:
    """Return a process key only for a well-formed Bash poll/cancel request."""

    if name != "bash" or not isinstance(arguments, Mapping):
        return None
    operation = arguments.get("operation")
    process_id = arguments.get("process_id")
    if operation not in {"poll", "cancel"}:
        return None
    if not isinstance(process_id, str) or not process_id.strip():
        return None
    resolved_operation: ProcessOperation = "poll" if operation == "poll" else "cancel"
    return ProcessCallIdentity(process_id=process_id, operation=resolved_operation)


def historical_process_observation(
    process_id: str,
    output: str,
) -> tuple[ManagedProcessState | None, str]:
    """Parse only Wisp's managed-process envelope, validated against its call ID."""

    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    first, separator, remainder = normalized.partition("\n")
    prefix = f"Process {process_id}"
    if first == f"{prefix} is still running":
        state: ManagedProcessState | None = "running"
    elif first.startswith(f"{prefix} completed with exit code "):
        exit_code_text = first.removeprefix(f"{prefix} completed with exit code ")
        try:
            exit_code = int(exit_code_text)
        except ValueError:
            return None, normalized
        if str(exit_code) != exit_code_text:
            return None, normalized
        state = "completed"
    elif first == f"{prefix} timed out":
        state = "timed_out"
    elif first == f"{prefix} cancelled":
        state = "cancelled"
    elif first == f"{prefix} failed" or first.startswith(f"{prefix} failed: "):
        state = "failed"
    else:
        return None, normalized
    output_chunk = _historical_output_chunk(remainder) if separator else ""
    if first.startswith(f"{prefix} failed: "):
        failure_reason = first.removeprefix(f"{prefix} failed: ")
        output_chunk = f"{failure_reason}\n{output_chunk}" if output_chunk else failure_reason
    return state, output_chunk


def _historical_output_chunk(output: str) -> str:
    """Preserve Wisp's stream labels so accumulated chunks remain unambiguous."""

    if output.startswith(("stdout:\n", "stderr:\n")):
        return output
    return output


def _process_output_chunk(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(f"stdout:\n{stdout.rstrip(chr(10))}")
    if stderr:
        parts.append(f"stderr:\n{stderr.rstrip(chr(10))}")
    return "\n".join(part for part in parts if part)


def _tail_preview(output: str) -> str:
    bounded = truncate_text_tail(
        output,
        max_bytes=_PROCESS_PREVIEW_MAX_BYTES,
        max_lines=_PROCESS_PREVIEW_MAX_LINES,
    )
    preview = bounded.text.strip("\n")
    if not bounded.truncated:
        return preview
    return f"… earlier process output hidden\n{preview}"


__all__ = [
    "PROCESS_OUTPUT_MAX_BYTES",
    "PROCESS_OUTPUT_MAX_LINES",
    "ProcessCallIdentity",
    "ProcessDisplayState",
    "ProcessLifecycle",
    "ProcessLifecyclePresentation",
    "ProcessOperation",
    "historical_process_observation",
    "process_call_identity",
]
