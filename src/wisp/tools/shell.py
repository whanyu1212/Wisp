"""Shell command built-in tool."""

from __future__ import annotations

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_int, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.process import _format_process_output_bounded, _run_shell
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text


class BashTool:
    """Run shell commands in the tool working directory."""

    name = "bash"
    safety: ToolSafety = "command"
    description = (
        "Run a shell command and capture stdout, stderr, and an explicit completion exit code. "
        "A timeout is reported separately and is not a completed command."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "default": 30},
        },
        "required": ["command"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        command = _required_string(arguments, "command")
        timeout = _optional_int(arguments, "timeout", default=30)
        if timeout is None or timeout < 1:
            raise ToolError("bash.timeout must be greater than or equal to 1")

        result = await _run_shell(
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
