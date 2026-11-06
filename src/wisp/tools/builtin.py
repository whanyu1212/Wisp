"""Built-in local tools registered by Wisp."""

from __future__ import annotations

from wisp.tools.base import Tool
from wisp.tools.files.operations import EditTool, ReadTool, WriteTool
from wisp.tools.read_output import ReadOutputTool
from wisp.tools.search.tools import FindTool, GrepTool, LsTool
from wisp.tools.shell.process import ProcessResult
from wisp.tools.shell.supervisor import ProcessSupervisor
from wisp.tools.shell.tool import BashTool


def builtin_tools(*, process_supervisor: ProcessSupervisor | None = None) -> tuple[Tool, ...]:
    """Return Wisp's built-in local tools.

    Args:
        process_supervisor (ProcessSupervisor | None, optional): Supervisor shared by
            bash and read_output, which reads the output bash saves. Defaults to a
            private one-process supervisor.

    Returns:
        tuple[Tool, ...]: The tools in registration order.
    """

    supervisor = (
        process_supervisor if process_supervisor is not None else ProcessSupervisor(max_processes=1)
    )
    return (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(supervisor),
        GrepTool(),
        FindTool(),
        LsTool(),
        ReadOutputTool(supervisor.output_store),
    )


__all__ = [
    "BashTool",
    "EditTool",
    "FindTool",
    "GrepTool",
    "LsTool",
    "ProcessResult",
    "ReadOutputTool",
    "ReadTool",
    "WriteTool",
    "builtin_tools",
]
