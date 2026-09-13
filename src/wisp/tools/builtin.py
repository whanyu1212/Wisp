"""Built-in local tools registered by Wisp."""

from __future__ import annotations

from wisp.tools.base import Tool
from wisp.tools.files.operations import EditTool, ReadTool, WriteTool
from wisp.tools.search.tools import FindTool, GrepTool, LsTool
from wisp.tools.shell.process import ProcessResult, _kill_process_tree, _run_exec_limited_stdout
from wisp.tools.shell.supervisor import ProcessSupervisor
from wisp.tools.shell.tool import BashTool


def builtin_tools(*, process_supervisor: ProcessSupervisor | None = None) -> tuple[Tool, ...]:
    """Return Wisp's built-in local tools."""

    return (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool() if process_supervisor is None else BashTool(process_supervisor),
        GrepTool(process_supervisor),
        FindTool(process_supervisor),
        LsTool(),
    )


__all__ = [
    "BashTool",
    "EditTool",
    "FindTool",
    "GrepTool",
    "LsTool",
    "ProcessResult",
    "ReadTool",
    "WriteTool",
    "_kill_process_tree",
    "_run_exec_limited_stdout",
    "builtin_tools",
]
