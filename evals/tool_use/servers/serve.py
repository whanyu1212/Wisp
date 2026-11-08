"""Serve one synthetic tool catalog over MCP stdio.

Run as `python -m evals.tool_use.servers.serve <catalog>` with the repository root on
`PYTHONPATH`; the runner configures each MCP task's servers that way.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

from evals.tool_use.servers.catalogs import CATALOGS, CatalogTool


def _tool_function(tool: CatalogTool, parameters: tuple[str, ...]) -> Callable[..., str]:
    """Wrap a catalog tool in a function whose signature names its string parameters.

    The MCP SDK builds each tool's input schema from its function's signature.
    """

    def call(**arguments: str) -> str:
        return tool.respond(arguments)

    call.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str)
            for name in parameters
        ],
        return_annotation=str,
    )
    call.__annotations__ = {**dict.fromkeys(parameters, str), "return": str}
    return call


def build_server(name: str) -> MCPServer:
    """Build the server for one catalog in `CATALOGS`."""

    catalog = CATALOGS[name]
    server = MCPServer(name)
    for tool in catalog.tools:
        server.add_tool(
            _tool_function(tool, catalog.parameters),
            name=tool.name,
            description=tool.description,
        )
    return server


if __name__ == "__main__":
    build_server(sys.argv[1]).run()
