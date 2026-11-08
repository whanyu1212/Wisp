"""Serve one synthetic tool catalog over MCP stdio.

Run as `python -m evals.tool_use.servers.serve <catalog>` with the repository root on
`PYTHONPATH`; the runner configures each MCP task's servers that way.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, create_model

from evals.tool_use.servers.catalogs import CATALOGS, CatalogTool, Parameter


def _tool_function(tool: CatalogTool, parameters: tuple[Parameter, ...]) -> Callable[..., str]:
    """Wrap a catalog tool in a function whose signature declares its parameters.

    The MCP SDK builds each tool's input schema from its function's signature, so
    each parameter becomes a typed, described field; optional ones default to None.
    """

    def call(**arguments: object) -> str:
        return tool.respond(
            {
                name: value.model_dump(exclude_none=True) if isinstance(value, BaseModel) else value
                for name, value in arguments.items()
                if value is not None
            }
        )

    signature = [_signature_parameter(tool.name, parameter) for parameter in parameters]
    call.__signature__ = inspect.Signature(signature, return_annotation=str)  # type: ignore[attr-defined]
    call.__annotations__ = {
        **{parameter.name: parameter.annotation for parameter in signature},
        "return": str,
    }
    return call


def _signature_parameter(tool_name: str, parameter: Parameter) -> inspect.Parameter:
    # Built at run time from catalog data, so the types are opaque to the type checker.
    kind: Any = _annotation(tool_name, parameter)
    # The description wraps the whole type, so an optional one is not hidden in a branch.
    annotation: Any = Annotated[
        kind if parameter.required else kind | None, Field(description=parameter.description)
    ]
    return inspect.Parameter(
        parameter.name,
        inspect.Parameter.KEYWORD_ONLY,
        annotation=annotation,
        default=inspect.Parameter.empty if parameter.required else None,
    )


def _annotation(tool_name: str, parameter: Parameter) -> Any:
    match parameter.kind:
        case "string":
            return str
        case "integer":
            return int
        case "boolean":
            return bool
        case "string_list":
            return list[str]
        case "object":
            fields: dict[str, Any] = {
                field.name: (
                    _annotation(tool_name, field)
                    if field.required
                    else _annotation(tool_name, field) | None,
                    Field(default=... if field.required else None, description=field.description),
                )
                for field in parameter.fields
            }
            return create_model(f"{tool_name}_{parameter.name}", **fields)


def build_server(name: str) -> MCPServer:
    """Build the server for one catalog in `CATALOGS`."""

    catalog = CATALOGS[name]
    server = MCPServer(name)
    for tool in catalog.tools:
        server.add_tool(
            _tool_function(tool, catalog.parameters_for(tool)),
            name=tool.name,
            description=tool.description,
        )
    return server


if __name__ == "__main__":
    build_server(sys.argv[1]).run()
