"""Bounded discovery of the exact tools available to one codemode run.

The caller supplies the filtered catalog. Discovery never grants permission or
executes catalog tools; it only reports their schemas and invocation contracts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from wisp.tool_types import ToolSafety
from wisp.tools.base import Tool, ToolArguments, ToolInputSchema
from wisp.tools.codemode.result_types import result_type, script_receives
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult

TOOL_SEARCH = "tool_search"
TOOL_DESCRIBE = "tool_describe"
DISCOVERY_TOOL_NAMES = (TOOL_SEARCH, TOOL_DESCRIBE)
DEFAULT_SEARCH_RESULTS = 8
MAX_SEARCH_RESULTS = 20
MAX_DISCOVERY_BYTES = 16_384
_MAX_SUMMARY_CHARS = 240
_WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True, slots=True)
class DiscoveryEntry:
    """A callable tool and the run-specific metadata its owner already resolved."""

    tool: Tool
    namespace: str
    guidelines: tuple[str, ...] = ()
    requires_confirmation: bool = False
    connected: bool = True
    invocation: Literal["function", "mcp_call"] = "function"

    @property
    def name(self) -> str:
        return self.tool.name


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """The permitted tools for a run, preserving registration order on ties.

    Duplicate callable names are rejected: a namespace is metadata, not an
    alternate way to address an ambiguous script function.
    """

    entries: tuple[DiscoveryEntry, ...] = ()
    _by_name: dict[str, DiscoveryEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_name: dict[str, DiscoveryEntry] = {}
        for entry in self.entries:
            if entry.name in by_name:
                raise ValueError(f"Duplicate discovery tool name: {entry.name}")
            by_name[entry.name] = entry
        object.__setattr__(self, "_by_name", by_name)

    def get(self, name: str) -> DiscoveryEntry | None:
        return self._by_name.get(name)

    def search(self, query: str) -> tuple[DiscoveryEntry, ...]:
        """Rank name, namespace, and description matches; an empty query lists all."""

        if not query.strip():
            return self.entries
        words = set(_WORD.findall(query.casefold()))
        scored: list[tuple[int, int, DiscoveryEntry]] = []
        for position, entry in enumerate(self.entries):
            name = entry.name.casefold()
            name_words = set(_WORD.findall(name))
            namespace_words = set(_WORD.findall(entry.namespace.casefold()))
            description_words = set(_WORD.findall(entry.tool.description.casefold()))
            score = sum(
                3 * (word in name_words)
                + (word in name)
                + 2 * (word in namespace_words)
                + (word in description_words)
                for word in words
            )
            if score:
                scored.append((-score, position, entry))
        return tuple(entry for _score, _position, entry in sorted(scored))


def _metadata(entry: DiscoveryEntry) -> dict[str, object]:
    return {
        "name": entry.name,
        "callable_name": entry.name if entry.invocation == "function" else "mcp_call",
        "invocation": entry.invocation,
        "namespace": entry.namespace,
        "safety": entry.tool.safety,
        "requires_confirmation": entry.requires_confirmation,
        "connected": entry.connected,
    }


def _json(value: object) -> str:
    # JSON permits these Unicode line separators literally, but the surrounding
    # tool-output line budget counts them as line breaks.
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("\u0085", "\\u0085")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _size(value: object) -> int:
    return len(_json(value).encode("utf-8"))


def _budget(context: ToolContext) -> int:
    if context.max_output_lines < 1:
        raise ToolError("Discovery needs at least one output line", failure_code="invalid_result")
    return min(MAX_DISCOVERY_BYTES, max(0, context.max_output_bytes))


def _result(payload: dict[str, object], budget: int, *, truncated: bool = False) -> ToolResult:
    text = _json(payload)
    if len(text.encode("utf-8")) > budget:
        raise ToolError(
            "Discovery output budget is too small for a complete JSON response; "
            "increase max_output_bytes",
            failure_code="invalid_result",
        )
    return ToolResult(text=text, structured=payload, truncated=truncated)


def _arguments(arguments: ToolArguments, allowed: set[str]) -> None:
    if arguments.keys() - allowed:
        raise ToolArgumentError("Discovery received unknown argument fields")


def _integer(arguments: ToolArguments, name: str, default: int, *, minimum: int) -> int:
    value = arguments.get(name, default)
    if type(value) is not int or value < minimum or (name == "limit" and value > 20):
        suffix = " through 20" if name == "limit" else " or greater"
        raise ToolArgumentError(f"Discovery {name} must be an integer from {minimum}{suffix}")
    return value


class ToolSearchTool:
    """Find callable tools without loading their schemas into the prompt."""

    name = TOOL_SEARCH
    description = (
        "Search all tools callable from codemode by name, namespace or description. "
        "An empty query enumerates tools. Results are paged summaries without schemas; "
        "use tool_describe for the exact input and return contract. Call invocation=function "
        "tools by callable_name; for invocation=mcp_call use "
        "mcp_call(name=the exact tool name, arguments={...})."
    )
    safety: ToolSafety = "read"
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    output_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "tools": {"type": "array", "items": {"type": "object"}},
            "total_matches": {"type": "integer"},
            "next_offset": {"type": ["integer", "null"]},
        },
        "required": ["tools", "total_matches", "next_offset"],
    }

    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        _arguments(arguments, {"query", "limit", "offset"})
        query = arguments.get("query")
        if not isinstance(query, str):
            raise ToolArgumentError("tool_search.query must be a string")
        limit = _integer(arguments, "limit", DEFAULT_SEARCH_RESULTS, minimum=1)
        offset = _integer(arguments, "offset", 0, minimum=0)
        budget = _budget(context)
        matches = self._catalog.search(query)
        summaries: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "tools": summaries,
            "total_matches": len(matches),
            "next_offset": None,
        }
        for entry in matches[offset : offset + limit]:
            summary = (entry.tool.description.splitlines() or [""])[0].strip()
            record = _metadata(entry)
            record["summary"] = summary[:_MAX_SUMMARY_CHARS]
            if len(summary) > _MAX_SUMMARY_CHARS:
                record["summary_omitted_chars"] = len(summary) - _MAX_SUMMARY_CHARS
            summaries.append(record)
            payload["next_offset"] = offset + len(summaries)
            if _size(payload) > budget:
                record.pop("summary", None)
                record["summary_omitted_chars"] = len(summary)
            if _size(payload) > budget:
                summaries.pop()
                break
        next_offset = offset + len(summaries)
        payload["next_offset"] = next_offset if next_offset < len(matches) else None
        if not summaries and offset < len(matches):
            raise ToolError(
                "Discovery output budget is too small for one tool summary; "
                "increase max_output_bytes",
                failure_code="invalid_result",
            )
        return _result(payload, budget, truncated=payload["next_offset"] is not None)


class ToolDescribeTool:
    """Describe one exact callable name, with bounded, navigable schema results."""

    name = TOOL_DESCRIBE
    description = (
        "Describe one exact tool name: inputs, output schema, script return semantics, "
        "guidelines and permissions. Omitted fields are explicit. To retrieve a large "
        "schema, set schema_path to a JSON Pointer rooted at /input_schema or "
        "/output_schema. Oversized subtrees return paged child paths; follow a path "
        "or next_offset with offset. A returned schema_value is always complete. "
        "For invocation=mcp_call use mcp_call(name=the exact tool name, arguments={...}); "
        "input_schema and output_schema describe the target, while the wrapper returns text."
    )
    safety: ToolSafety = "read"
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "schema_path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    # Describe has two shapes (whole tool and schema page). An open object keeps
    # codemode's generated return annotation honest for both.
    output_schema: ToolInputSchema = {"type": "object", "additionalProperties": True}

    def __init__(self, catalog: ToolCatalog) -> None:
        self._catalog = catalog

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        _arguments(arguments, {"name", "schema_path", "offset", "limit"})
        name = arguments.get("name")
        if not isinstance(name, str):
            raise ToolArgumentError("tool_describe.name must be a string")
        entry = self._catalog.get(name)
        if entry is None:
            raise ToolError(
                "No tool with that exact name is available in this run",
                failure_code="unknown_tool",
                retryable=True,
                recovery_hint="Find the callable name with tool_search.",
            )
        offset = _integer(arguments, "offset", 0, minimum=0)
        limit = _integer(arguments, "limit", DEFAULT_SEARCH_RESULTS, minimum=1)
        budget = _budget(context)
        declared_output: object = getattr(entry.tool, "output_schema", None)
        output_schema = dict(declared_output) if isinstance(declared_output, Mapping) else None
        schemas: dict[str, object] = {
            "input_schema": dict(entry.tool.input_schema),
            "output_schema": output_schema,
        }
        if "schema_path" in arguments:
            path = arguments["schema_path"]
            if not isinstance(path, str):
                raise ToolArgumentError("tool_describe.schema_path must be a JSON Pointer string")
            return _schema_result(schemas, path, offset=offset, limit=limit, budget=budget)
        if "offset" in arguments or "limit" in arguments:
            raise ToolArgumentError("Describe offset and limit require schema_path")
        payload = _metadata(entry)
        script_schema = output_schema if entry.invocation == "function" else None
        returns = result_type(entry.name, script_schema, taken=frozenset())
        receives = script_receives(entry.name, script_schema) or (
            "A codemode script calling it by name receives its text."
        )
        if entry.invocation == "mcp_call":
            receives = (
                "A codemode script calling mcp_call(name=the exact tool name, arguments={...}) "
                "receives the wrapper's text, even when the target declares an output schema."
            )
        payload.update(
            description=entry.tool.description,
            guidelines=list(entry.guidelines),
            **schemas,
            script_receives=receives,
            result_type=returns.kind if returns is not None else "text",
        )
        omitted: dict[str, object] = {}
        payload["omitted_fields"] = omitted
        for field_name in (
            "input_schema",
            "output_schema",
            "description",
            "guidelines",
            "script_receives",
        ):
            if _size(payload) <= budget:
                break
            value = payload.pop(field_name)
            omitted[field_name] = {"size_bytes": _size(value)}
            if field_name.endswith("_schema"):
                omitted[field_name] = {"size_bytes": _size(value), "schema_path": f"/{field_name}"}
        return _result(payload, budget, truncated=bool(omitted))


def _pointer(schemas: dict[str, object], path: str) -> object:
    """Resolve an RFC 6901 pointer without accepting ambiguous array indexes."""

    if not path.startswith(("/input_schema", "/output_schema")):
        raise ToolArgumentError("schema_path must start with /input_schema or /output_schema")
    value: object = schemas
    for token in path[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ToolArgumentError("schema_path has an invalid JSON Pointer escape")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            if len(key) > len(str(len(value))):
                raise ToolArgumentError("schema_path does not exist")
            index = int(key)
            if index >= len(value):
                raise ToolArgumentError("schema_path does not exist")
            value = value[index]
        else:
            raise ToolArgumentError("schema_path does not exist")
    return value


def _schema_result(
    schemas: dict[str, object], path: str, *, offset: int, limit: int, budget: int
) -> ToolResult:
    value = _pointer(schemas, path)
    complete: dict[str, object] = {"schema_path": path, "schema_value": value}
    if offset == 0 and _size(complete) <= budget:
        return _result(complete, budget)
    children: list[tuple[str, object]]
    if isinstance(value, dict):
        children = list(value.items())
        kind = "object"
    elif isinstance(value, list):
        children = [(str(index), child) for index, child in enumerate(value)]
        kind = "array"
    else:
        return _result(
            {"schema_path": path, "omitted": True, "size_bytes": _size(value)},
            budget,
            truncated=True,
        )
    items: list[dict[str, object]] = []
    payload: dict[str, object] = {
        "schema_path": path,
        "schema_kind": kind,
        "schema_items": items,
        "total_items": len(children),
        "next_offset": None,
    }
    for key, child in children[offset : offset + limit]:
        child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
        item: dict[str, object] = {"path": child_path, "value": child}
        items.append(item)
        payload["next_offset"] = offset + len(items)
        if _size(payload) > budget:
            item.pop("value")
            item.update(omitted=True, size_bytes=_size(child))
        if _size(payload) > budget:
            items.pop()
            break
    next_offset = offset + len(items)
    payload["next_offset"] = next_offset if next_offset < len(children) else None
    if not items and offset < len(children):
        raise ToolError(
            "Discovery output budget is too small for a schema child path; "
            "increase max_output_bytes",
            failure_code="invalid_result",
        )
    return _result(payload, budget, truncated=True)


def discovery_tools(catalog: ToolCatalog) -> tuple[Tool, ...]:
    """Return the two read-only discovery tools bound to this run's catalog."""

    return (ToolSearchTool(catalog), ToolDescribeTool(catalog))
