"""Serve one synthetic tool catalog over MCP stdio.

Run as `python -m evals.tool_use.servers.serve <catalog> [--structured]` with the
repository root on `PYTHONPATH`; the runner configures each MCP task's servers that
way. With `--structured`, each tool that has a structured answer declares an output
schema and returns its value beside the same text; the rest answer as before.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, Field, create_model

from evals.tool_use.servers.catalogs import CATALOGS, CatalogTool, Parameter
from evals.tool_use.servers.model import Arguments, Structured


def _tool_function(
    tool: CatalogTool, parameters: tuple[Parameter, ...], *, structured: bool
) -> Callable[..., object]:
    """Wrap a catalog tool in a function whose signature declares its parameters.

    The MCP SDK builds each tool's input schema from its function's signature, so
    each parameter becomes a typed, described field; optional ones default to None.
    It builds the output schema from the return annotation: none for text, and the
    tool's result type when it answers structured.
    """

    answer = (
        _structured_answer(tool.structured)
        if structured and tool.structured is not None
        else tool.respond
    )
    # Built at run time from catalog data, so the types are opaque to the type checker.
    returns: Any = (
        Annotated[CallToolResult, tool.structured.result_type]
        if structured and tool.structured is not None
        else str
    )

    def call(**arguments: object) -> object:
        return answer(
            {
                name: value.model_dump(exclude_none=True) if isinstance(value, BaseModel) else value
                for name, value in arguments.items()
                if value is not None
            }
        )

    signature = [_signature_parameter(tool.name, parameter) for parameter in parameters]
    call.__signature__ = inspect.Signature(signature, return_annotation=returns)  # type: ignore[attr-defined]
    call.__annotations__ = {
        **{parameter.name: parameter.annotation for parameter in signature},
        "return": returns,
    }
    return call


def _structured_answer(structured: Structured) -> Callable[[Arguments], CallToolResult]:
    """Answer with the reply's text and its value; a reply without one is a failed call."""

    def answer(arguments: Arguments) -> CallToolResult:
        reply = structured.reply(arguments)
        if reply.value is None:
            raise ToolError(reply.text)
        return CallToolResult(
            content=[TextContent(type="text", text=reply.text)],
            structured_content=dict(reply.value),
        )

    return answer


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


def build_server(name: str, *, structured: bool = False) -> MCPServer:
    """Build the server for one catalog in `CATALOGS`.

    Args:
        name (str): The catalog.
        structured (bool): Whether tools with a structured answer return it, under
            an output schema.

    Returns:
        MCPServer: The server, not yet running.
    """

    catalog = CATALOGS[name]
    server = MCPServer(name)
    for tool in catalog.tools:
        server.add_tool(
            _tool_function(tool, catalog.parameters_for(tool), structured=structured),
            name=tool.name,
            description=tool.description,
        )
    return server


if __name__ == "__main__":
    build_server(sys.argv[1], structured="--structured" in sys.argv[2:]).run()
