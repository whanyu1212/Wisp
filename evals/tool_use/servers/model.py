"""The data model of a synthetic MCP catalog: its tools and their parameters.

Kept apart from the catalogs themselves so each catalog module can import it.
Nothing here may import Wisp or the MCP SDK.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

type Arguments = Mapping[str, object]
type ParameterKind = Literal["string", "integer", "boolean", "string_list", "object"]


@dataclass(frozen=True, slots=True)
class Parameter:
    """One parameter of a tool's input schema.

    Attributes:
        name (str): The parameter's name.
        kind (ParameterKind): Its JSON type; `object` holds the nested ``fields``.
        description (str): What the model reads about it.
        required (bool): Whether a call must pass it.
        fields (tuple[Parameter, ...]): An object parameter's own fields.
    """

    name: str
    kind: ParameterKind = "string"
    description: str = ""
    required: bool = True
    fields: tuple[Parameter, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogTool:
    """One tool a synthetic server offers.

    Attributes:
        name (str): The tool's name on its server.
        description (str): What the model reads to choose it.
        respond (Callable[[Arguments], str]): The tool's answer for its arguments.
        parameters (tuple[Parameter, ...] | None): The tool's own parameters, or
            None to take its catalog's shared ones.
    """

    name: str
    description: str
    respond: Callable[[Arguments], str]
    parameters: tuple[Parameter, ...] | None = None


@dataclass(frozen=True, slots=True)
class Catalog:
    """A server's tools.

    Attributes:
        parameters (tuple[str, ...]): Required string parameters shared by every
            tool that does not declare its own.
        tools (tuple[CatalogTool, ...]): The tools, in the order the server lists them.
    """

    parameters: tuple[str, ...]
    tools: tuple[CatalogTool, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def parameters_for(self, tool: CatalogTool) -> tuple[Parameter, ...]:
        """The parameters ``tool`` takes: its own, or the catalog's shared ones."""

        if tool.parameters is not None:
            return tool.parameters
        return tuple(Parameter(name) for name in self.parameters)
