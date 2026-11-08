"""On-demand MCP tools: a per-run catalog and the tools that search, describe, and call it.

In on-demand mode the model is not sent every MCP tool's schema. It gets three
small tools instead: `mcp_search` and `mcp_describe` read this run's catalog, and
`mcp_call` names a catalog tool to run. `mcp_call` never runs anything itself:
the executor resolves it to the named tool before any check, so that tool's own
policy, plan-mode rule, approval, and execution metadata apply exactly as for a
direct call. The catalog is built once per run from the MCP tools direct mode
would have exposed, so the proxy can reach nothing a direct call could not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from wisp.tool_types import ToolSafety
from wisp.tools.base import Tool, ToolArguments, ToolInputSchema, ToolPromptMetadata
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text

MCP_SEARCH = "mcp_search"
MCP_DESCRIBE = "mcp_describe"
MCP_CALL = "mcp_call"
DISCOVERY_TOOL_NAMES = (MCP_SEARCH, MCP_DESCRIBE, MCP_CALL)

DEFAULT_SEARCH_RESULTS = 8
MAX_SEARCH_RESULTS = 20
# The prefix `adapt_mcp_tool` puts on every description; results show the rest.
_DESCRIPTION_PREFIX = re.compile(r"^MCP server [a-z0-9-]+: ")
_WORD = re.compile(r"[a-z0-9]+")
# A JSON Schema primitive type and the Python values it accepts. `bool` is an `int`
# in Python, so integer and number checks exclude it explicitly.
_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    # JSON has one number type, so 2.0 is a valid integer.
    "integer": lambda value: (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, float) and value.is_integer())
    ),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
    "null": lambda value: value is None,
}


@runtime_checkable
class McpCatalogTool(Protocol):
    """A registered MCP tool, as `wisp.mcp.tool.McpTool` provides it.

    A protocol rather than `McpTool` itself, so importing on-demand discovery does
    not load the MCP SDK: the CLI must start without it.
    """

    name: str
    description: str
    input_schema: ToolInputSchema
    safety: ToolSafety
    server_name: str
    remote_name: str

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One MCP tool a run may use.

    Attributes:
        tool (McpCatalogTool): The registered tool.
        connected (bool): Whether its server's connection was live when the run began.
    """

    tool: McpCatalogTool
    connected: bool = True

    @property
    def name(self) -> str:
        return self.tool.name

    @property
    def server(self) -> str:
        return self.tool.server_name

    @property
    def summary(self) -> str:
        """The description's first line, without the `MCP server X:` prefix."""

        first_line = (self.tool.description.splitlines() or [""])[0]
        return _DESCRIPTION_PREFIX.sub("", first_line).strip()

    @property
    def parameters(self) -> tuple[tuple[str, bool], ...]:
        """Top-level parameter names, each with whether it is required."""

        properties = self.tool.input_schema.get("properties")
        required = self.tool.input_schema.get("required")
        required_names = (
            {name for name in required if isinstance(name, str)}
            if isinstance(required, list)
            else set()
        )
        if not isinstance(properties, dict):
            return ()
        return tuple((str(name), name in required_names) for name in properties)


@dataclass(frozen=True, slots=True)
class McpCatalog:
    """The MCP tools one run may use, in registration order."""

    entries: tuple[CatalogEntry, ...] = ()
    _by_name: dict[str, CatalogEntry] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_name.update((entry.name, entry) for entry in self.entries)

    @classmethod
    def from_tools(
        cls, tools: Iterable[Tool], *, connected: Callable[[str], bool] | None = None
    ) -> McpCatalog:
        """Build a catalog from the MCP tools among ``tools``.

        Args:
            tools (Iterable[Tool]): Tools this run may use; only MCP tools are kept.
            connected (Callable[[str], bool] | None): Reports whether a server's
                connection is live, or None to treat every server as connected.

        Returns:
            McpCatalog: The run's catalog.
        """

        return cls(
            tuple(
                CatalogEntry(tool, connected=connected(tool.server_name) if connected else True)
                for tool in tools
                if isinstance(tool, McpCatalogTool)
            )
        )

    def __bool__(self) -> bool:
        return bool(self.entries)

    def get(self, name: str) -> CatalogEntry | None:
        return self._by_name.get(name)

    def servers(self) -> tuple[tuple[str, int], ...]:
        """Each server in the catalog with its tool count, in catalog order."""

        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.server] = counts.get(entry.server, 0) + 1
        return tuple(counts.items())

    def search(self, query: str, *, limit: int) -> tuple[CatalogEntry, ...]:
        """Rank entries by the query's words: name matches weigh most, then server and summary."""

        words = set(_WORD.findall(query.lower()))
        scored: list[tuple[int, int, CatalogEntry]] = []
        for position, entry in enumerate(self.entries):
            name_words = set(_WORD.findall(entry.tool.remote_name.lower()))
            server_words = set(_WORD.findall(entry.server))
            summary_words = set(_WORD.findall(entry.summary.lower()))
            score = sum(
                3 * (word in name_words)
                + (word in entry.tool.remote_name.lower())
                + 2 * (word in server_words)
                + (word in summary_words)
                for word in words
            )
            if score:
                scored.append((-score, position, entry))
        return tuple(entry for _score, _position, entry in sorted(scored)[:limit])


def catalog_guidance(catalog: McpCatalog) -> str:
    """One line telling the model which MCP servers it can search, or "" for none."""

    if not catalog:
        return ""
    servers = ", ".join(
        f"{server} ({count} tool{'s' if count != 1 else ''})" for server, count in catalog.servers()
    )
    return f"MCP servers, found with {MCP_SEARCH} and run with {MCP_CALL}: {servers}."


def resolve_target(catalog: McpCatalog | None, arguments: ToolArguments) -> str | None:
    """Return the catalog tool an `mcp_call` names, or None; never raises.

    Only a name in the catalog is returned, so a model-chosen string never reaches
    events as a target.
    """

    if catalog is None:
        return None
    name = arguments.get("name")
    if not isinstance(name, str):
        return None
    return name if catalog.get(name) is not None else None


def check_proxied_arguments(entry: CatalogEntry, arguments: object) -> str | None:
    """Check proxied arguments lightly against the target's schema, before approval.

    Only the shape is checked: an object, required top-level properties present,
    and each top-level property's primitive `type`. Full JSON Schema validation is
    left to the server, as for a direct call: a server-supplied `pattern` could
    hang the event loop, and an unresolvable `$ref` would raise.

    Returns:
        str | None: A model-facing problem, or None when the call may proceed.
    """

    if not isinstance(arguments, dict):
        return f"{MCP_CALL}.arguments must be an object of {entry.name}'s parameters"
    schema = entry.tool.input_schema
    required = schema.get("required")
    if isinstance(required, list):
        missing = [name for name in required if isinstance(name, str) and name not in arguments]
        if missing:
            return f"{entry.name} needs {', '.join(missing)}; see {MCP_DESCRIBE} for its schema"
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, value in arguments.items():
            expected = properties.get(name)
            declared = expected.get("type") if isinstance(expected, dict) else None
            types = [declared] if isinstance(declared, str) else declared
            if not isinstance(types, list):
                continue
            # Older drafts allow schemas inside a `type` list; only names are checked.
            checks = [
                _JSON_TYPES[kind] for kind in types if isinstance(kind, str) and kind in _JSON_TYPES
            ]
            if checks and not any(check(value) for check in checks):
                named = [kind for kind in types if isinstance(kind, str)]
                return f"{entry.name}.{name} must be {' or '.join(named)}"
    return None


class McpSearchTool:
    """Find MCP tools in this run's catalog by query."""

    name = MCP_SEARCH
    description = (
        "Search the MCP tools available in this run. Returns each match's name, server, "
        "summary and parameters (* marks required ones). An empty query lists servers."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words describing the tool you need."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SEARCH_RESULTS,
                "description": f"Most results to return (default {DEFAULT_SEARCH_RESULTS}).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    safety: ToolSafety = "read"

    def __init__(self, catalog: McpCatalog) -> None:
        self._catalog = catalog

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        query = arguments.get("query")
        limit = arguments.get("limit", DEFAULT_SEARCH_RESULTS)
        if not isinstance(query, str):
            raise ToolError(
                f"{MCP_SEARCH}.query must be a string", failure_code="invalid_arguments"
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_SEARCH_RESULTS
        ):
            raise ToolError(
                f"{MCP_SEARCH}.limit must be from 1 to {MAX_SEARCH_RESULTS}",
                failure_code="invalid_arguments",
            )
        if not query.strip():
            return _bounded(_server_list(self._catalog), context)
        matches = self._catalog.search(query, limit=limit)
        if not matches:
            return _bounded(
                f"No MCP tool matches {query!r}. Try other words, or an empty query to "
                "list servers.",
                context,
            )
        lines = [f"{len(matches)} of {len(self._catalog.entries)} MCP tools match {query!r}:"]
        lines.extend(_search_line(entry) for entry in matches)
        lines.append(
            f'Run one with {MCP_CALL} {{"name": ..., "arguments": {{...}}}}; '
            f"{MCP_DESCRIBE} gives its full schema."
        )
        return _bounded("\n".join(lines), context)


class McpDescribeTool:
    """Return one catalog tool's full description and input schema."""

    name = MCP_DESCRIBE
    description = "Return an MCP tool's full description and input schema, by its name."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The tool's name."}},
        "required": ["name"],
        "additionalProperties": False,
    }
    safety: ToolSafety = "read"

    def __init__(self, catalog: McpCatalog) -> None:
        self._catalog = catalog

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        name = arguments.get("name")
        entry = self._catalog.get(name) if isinstance(name, str) else None
        if entry is None:
            raise ToolError(
                f"No MCP tool named {name!r} is available in this run",
                failure_code="unknown_tool",
                retryable=True,
                recovery_hint=f"Find the tool's name with {MCP_SEARCH}.",
            )
        text = "\n".join(
            [
                f"{entry.name} (server {entry.server}"
                f"{', disconnected' if not entry.connected else ''})",
                entry.tool.description,
                "Input schema: "
                + json.dumps(entry.tool.input_schema, ensure_ascii=False, separators=(",", ":")),
            ]
        )
        return _bounded(text, context)


class McpCallTool:
    """Name an MCP tool to run. The executor resolves the call; this never runs itself."""

    name = MCP_CALL
    description = (
        "Run an MCP tool found with mcp_search, by its name, with its arguments. "
        "It goes through the same approval as calling the tool directly."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The MCP tool's name."},
            "arguments": {"type": "object", "description": "The tool's own arguments."},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
    }
    # Plan mode lists only read tools; the target's own safety decides each call.
    safety: ToolSafety = "read"

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        raise ToolError(
            f"{MCP_CALL} was not resolved to an MCP tool", failure_code="internal_error"
        )


def discovery_tools(catalog: McpCatalog) -> tuple[Tool, ...]:
    """The three tools bound to ``catalog``, in their offered order."""

    return (McpSearchTool(catalog), McpDescribeTool(catalog), McpCallTool())


DISCOVERY_PROMPT = ToolPromptMetadata(
    guidelines=(
        f"MCP tools are not listed directly: find one with {MCP_SEARCH}, check its "
        f"parameters (or {MCP_DESCRIBE} for the full schema), then run it with {MCP_CALL}.",
        f"Each {MCP_CALL} needs the same approval as calling that MCP tool directly.",
    )
)


def _search_line(entry: CatalogEntry) -> str:
    parameters = ", ".join(
        f"{name}{'*' if required else ''}" for name, required in entry.parameters
    )
    disconnected = " [server disconnected]" if not entry.connected else ""
    return (
        f"- {entry.name} ({entry.server}){disconnected}: {entry.summary}"
        f" Parameters: {parameters or 'none'}"
    )


def _server_list(catalog: McpCatalog) -> str:
    if not catalog:
        return "No MCP tools are available in this run."
    lines = ["MCP servers in this run:"]
    lines.extend(
        f"- {server}: {count} tool{'s' if count != 1 else ''}"
        for server, count in catalog.servers()
    )
    return "\n".join(lines)


def _bounded(text: str, context: ToolContext) -> ToolResult:
    bounded = truncate_text(
        text,
        max_bytes=max(0, context.max_output_bytes),
        max_lines=max(0, context.max_output_lines),
    )
    return ToolResult(text=bounded.text, truncated=bounded.truncated)


__all__ = [
    "DISCOVERY_PROMPT",
    "DISCOVERY_TOOL_NAMES",
    "MCP_CALL",
    "MCP_DESCRIBE",
    "MCP_SEARCH",
    "CatalogEntry",
    "McpCallTool",
    "McpCatalogTool",
    "McpCatalog",
    "McpDescribeTool",
    "McpSearchTool",
    "catalog_guidance",
    "check_proxied_arguments",
    "discovery_tools",
    "resolve_target",
]
