"""Shell command built-in tool."""

from __future__ import annotations

import math
from typing import Final, Literal, cast, overload

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_int, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.process import _format_process_output_bounded, _run_shell
from wisp.tools.process_manager import ProcessSupervisor, ProcessUpdate
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import TruncatedText, truncate_text

_DEFAULT_PROCESS_SUPERVISOR: Final = object()
_DEFAULT_BASH_TIMEOUT_SECONDS = 30
_DEFAULT_MANAGED_LIFETIME_SECONDS = 300.0
_DEFAULT_MANAGED_YIELD_SECONDS = 1.0
_DEFAULT_MANAGED_WAIT_SECONDS = 0.0

BashOperation = Literal["run", "start", "poll", "cancel"]
_BASH_OPERATIONS: Final = frozenset({"run", "start", "poll", "cancel"})


class BashTool:
    """Run shell commands in the tool working directory."""

    name = "bash"
    safety: ToolSafety = "command"
    description = (
        "Run a shell command and capture stdout, stderr, and an explicit completion exit code. "
        "Use operation=start to launch a resumable command, operation=poll to read incremental "
        "output by process_id, and operation=cancel to terminate it."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["run", "start", "poll", "cancel"],
                "default": "run",
            },
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "default": 30},
            "process_id": {"type": "string"},
            "lifetime_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "default": 300,
            },
            "yield_seconds": {
                "type": "number",
                "minimum": 0,
                "default": 1,
            },
            "wait_seconds": {
                "type": "number",
                "minimum": 0,
                "default": 0,
            },
        },
    }

    @overload
    def __init__(self) -> None: ...

    @overload
    def __init__(self, process_supervisor: ProcessSupervisor | None) -> None: ...

    def __init__(
        self,
        process_supervisor: ProcessSupervisor | None | object = _DEFAULT_PROCESS_SUPERVISOR,
    ) -> None:
        if process_supervisor is _DEFAULT_PROCESS_SUPERVISOR:
            process_supervisor = ProcessSupervisor(max_processes=1)
        self._process_supervisor = cast(ProcessSupervisor | None, process_supervisor)

    async def aclose(self) -> None:
        """Retry and release any retained default-supervisor process cleanup."""

        if self._process_supervisor is not None:
            await self._process_supervisor.aclose()

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        operation = _bash_operation(arguments)
        if operation == "run":
            return await self._run_to_completion(arguments, context)
        if operation == "start":
            return await self._start(arguments, context)
        if operation == "poll":
            return await self._poll(arguments, context)
        return await self._cancel(arguments, context)

    async def _run_to_completion(
        self, arguments: ToolArguments, context: ToolContext
    ) -> ToolResult:
        command = _required_string(arguments, "command")
        timeout = _optional_int(arguments, "timeout", default=_DEFAULT_BASH_TIMEOUT_SECONDS)
        if timeout is None or timeout < 1:
            raise ToolError("bash.timeout must be greater than or equal to 1")

        if self._process_supervisor is None:
            # Preserve the direct-construction and monkeypatch seam used by
            # embeddings and focused tool tests. Runtime-built Bash tools receive
            # the shared supervisor below.
            result = await _run_shell(
                command,
                cwd=context.cwd,
                timeout=float(timeout),
                max_output_bytes=context.max_output_bytes,
                max_output_lines=context.max_output_lines,
            )
        else:
            result = await self._process_supervisor.run_to_completion(
                command,
                cwd=context.cwd,
                timeout=float(timeout),
                max_output_bytes=context.max_output_bytes,
                max_output_lines=context.max_output_lines,
            )
        stdout = truncate_text(
            result.stdout,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        stderr = truncate_text(
            result.stderr,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        output = _format_process_output_bounded(
            result.exit_code,
            stdout.text,
            stderr.text,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=output.text,
            data={
                "exit_code": result.exit_code,
                "output_has_exit_status": True,
                "stdout": stdout.text,
                "stderr": stderr.text,
            },
            truncated=(
                result.stdout_truncated
                or result.stderr_truncated
                or stdout.truncated
                or stderr.truncated
                or output.truncated
            ),
        )

    async def _start(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        supervisor = self._require_process_supervisor("start")
        command = _required_string(arguments, "command")
        lifetime_seconds = _optional_number(
            arguments,
            "lifetime_seconds",
            default=_DEFAULT_MANAGED_LIFETIME_SECONDS,
        )
        yield_seconds = _optional_number(
            arguments,
            "yield_seconds",
            default=_DEFAULT_MANAGED_YIELD_SECONDS,
        )
        if lifetime_seconds is None or lifetime_seconds <= 0:
            raise ToolError("bash.lifetime_seconds must be greater than zero")
        if yield_seconds is None or yield_seconds < 0:
            raise ToolError("bash.yield_seconds must be greater than or equal to zero")

        process_id = await supervisor.start(
            command,
            cwd=context.cwd,
            timeout=lifetime_seconds,
            max_retained_bytes=context.max_output_bytes,
            max_retained_lines=context.max_output_lines,
        )
        update = await supervisor.poll(process_id, wait_seconds=yield_seconds)
        return _managed_update_result(update, context=context)

    async def _poll(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        supervisor = self._require_process_supervisor("poll")
        process_id = _required_string(arguments, "process_id")
        wait_seconds = _optional_number(
            arguments,
            "wait_seconds",
            default=_DEFAULT_MANAGED_WAIT_SECONDS,
        )
        if wait_seconds is None or wait_seconds < 0:
            raise ToolError("bash.wait_seconds must be greater than or equal to zero")
        update = await supervisor.poll(process_id, wait_seconds=wait_seconds)
        return _managed_update_result(update, context=context)

    async def _cancel(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        supervisor = self._require_process_supervisor("cancel")
        process_id = _required_string(arguments, "process_id")
        update = await supervisor.cancel(process_id)
        return _managed_update_result(update, context=context)

    def _require_process_supervisor(self, operation: BashOperation) -> ProcessSupervisor:
        if self._process_supervisor is None:
            raise ToolError(f"bash.operation={operation} requires a process supervisor")
        return self._process_supervisor


def _bash_operation(arguments: ToolArguments) -> BashOperation:
    value = arguments.get("operation")
    if value is None:
        return "run"
    if not isinstance(value, str):
        raise ToolError("bash.operation must be a string")
    if value not in _BASH_OPERATIONS:
        raise ToolError("bash.operation must be one of: run, start, poll, cancel")
    return cast(BashOperation, value)


def _optional_number(
    arguments: ToolArguments,
    name: str,
    *,
    default: float | None = None,
) -> float | None:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolError(f"bash.{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ToolError(f"bash.{name} must be finite")
    return number


def _managed_update_result(update: ProcessUpdate, *, context: ToolContext) -> ToolResult:
    stdout = truncate_text(
        update.stdout,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    stderr = truncate_text(
        update.stderr,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    output = _format_managed_update(update, stdout=stdout.text, stderr=stderr.text, context=context)
    stdout_truncated = update.stdout_truncated or stdout.truncated
    stderr_truncated = update.stderr_truncated or stderr.truncated
    data: dict[str, object] = {
        "process_id": update.process_id,
        "process_state": update.state,
        "process_error": update.error,
        "stdout": stdout.text,
        "stderr": stderr.text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_dropped_bytes": update.stdout_dropped_bytes,
        "stderr_dropped_bytes": update.stderr_dropped_bytes,
        "output_has_exit_status": False,
    }
    if update.exit_code is not None:
        data["exit_code"] = update.exit_code
    return ToolResult(
        text=output.text,
        data=data,
        truncated=stdout_truncated or stderr_truncated or output.truncated,
    )


def _format_managed_update(
    update: ProcessUpdate,
    *,
    stdout: str,
    stderr: str,
    context: ToolContext,
) -> TruncatedText:
    header = _managed_update_header(update)
    parts: list[str] = []
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if not parts:
        return TruncatedText(header, truncated=False)

    body = truncate_text(
        "\n".join(parts),
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    return TruncatedText(f"{header}\n{body.text}", truncated=body.truncated)


def _managed_update_header(update: ProcessUpdate) -> str:
    if update.state == "running":
        return f"Process {update.process_id} is still running"
    if update.state == "completed":
        exit_code = update.exit_code if update.exit_code is not None else -1
        return f"Process {update.process_id} completed with exit code {exit_code}"
    if update.state == "timed_out":
        return f"Process {update.process_id} timed out"
    if update.state == "cancelled":
        return f"Process {update.process_id} cancelled"
    if update.error:
        return f"Process {update.process_id} failed: {update.error}"
    return f"Process {update.process_id} failed"
