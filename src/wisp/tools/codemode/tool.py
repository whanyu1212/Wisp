"""The codemode tool: the model writes a Python script that calls other tools.

A script can fan out many calls with `asyncio.gather`, chain calls on earlier
answers, and filter what it reads, so only what it prints and returns reaches the
model. Each call it makes is a separate call under the run's tool policy (see
`ConfiguredToolExecutor.run_nested`), so the script itself needs no approval and
grants none. The run offers only read-only tools to scripts.
"""

from __future__ import annotations

import keyword
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from wisp.tool_types import ToolSafety
from wisp.tools.base import ToolArguments, ToolInputSchema, ToolPromptMetadata
from wisp.tools.codemode.calls import CODEMODE, NestedToolCaller
from wisp.tools.codemode.sandbox import ScriptFunction, ScriptLimits, run_script
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text

_JSON_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list[object]",
    "object": "dict[str, object]",
}

CODEMODE_PROMPT = ToolPromptMetadata(
    prompt_snippet="codemode: run a Python script that calls the read-only tools.",
    guidelines=(
        "Use codemode when a task needs many tool calls, or calls that depend on "
        "earlier results: one script replaces many turns, and only what it prints "
        "and returns reaches you, so filter large results inside the script.",
    ),
)


class ScriptableTool(Protocol):
    """What a script needs to know about a tool it may call."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> ToolInputSchema: ...


@dataclass(frozen=True, slots=True)
class _Signature:
    """One callable tool as a script sees it."""

    name: str
    declaration: str


@dataclass(slots=True)
class _CallLog:
    """The calls one script made, for its result."""

    by_tool: Counter[str] = field(default_factory=Counter)
    failed: int = 0

    @property
    def total(self) -> int:
        return sum(self.by_tool.values())

    def line(self) -> str:
        if not self.by_tool:
            return "No tool calls."
        calls = ", ".join(
            name if count == 1 else f"{name} ×{count}" for name, count in self.by_tool.items()
        )
        noun = "call" if self.total == 1 else "calls"
        failed = f" ({self.failed} failed)" if self.failed else ""
        return f"{self.total} tool {noun}{failed}: {calls}"


class CodemodeTool:
    """Run a Python script whose only way out is calling the run's read-only tools."""

    name = CODEMODE
    # The script itself touches nothing; every call it makes is checked on its own.
    safety: ToolSafety = "read"
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python script. Its last expression is its result.",
            }
        },
        "required": ["code"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        tools: Sequence[ScriptableTool],
        call: NestedToolCaller,
        *,
        discoverable: Sequence[ScriptableTool] = (),
        limits: ScriptLimits | None = None,
    ) -> None:
        """Offer ``tools`` to scripts, running each call a script makes with ``call``.

        Args:
            tools (Sequence[ScriptableTool]): The tools a script may call, named in
                the description. A tool whose name or parameters are not Python
                identifiers is left out, here and in ``discoverable``.
            call (NestedToolCaller): Runs one call under the run's tool policy.
            discoverable (Sequence[ScriptableTool]): Further tools a script may call
                by name without the description listing them: the MCP tools a run
                finds on demand, which `mcp_search` describes. Leaving them out
                keeps the description short and the same from run to run.
            limits (ScriptLimits | None): The bounds scripts run under; None uses
                the defaults.
        """

        listed = tuple(signature for tool in tools if (signature := _signature(tool)) is not None)
        found = tuple(
            signature for tool in discoverable if (signature := _signature(tool)) is not None
        )
        self._signatures = (*listed, *found)
        self._call = call
        self._limits = limits or ScriptLimits()
        self.description = _description(listed, self._limits, discoverable=bool(found))
        self._stubs = "\n".join(signature.declaration for signature in self._signatures) + "\n"

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The tools a script may call."""

        return tuple(signature.name for signature in self._signatures)

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ToolError(
                "codemode needs a non-empty `code` string.",
                failure_code="invalid_arguments",
                retryable=True,
            )
        log = _CallLog()
        run = await run_script(
            code,
            functions={name: self._function(name, log) for name in self.tool_names},
            stubs=self._stubs,
            limits=self._limits,
        )
        if run.failure is not None:
            # The calls already made stand: every tool a script reaches is read-only.
            message = [f"Script failed ({run.failure}): {run.error}"]
            if run.printed:
                message.append(f"Printed before it failed:\n{run.printed}")
            message.append(f"Calls made before it failed: {log.line()}")
            raise ToolError("\n".join(message))
        parts = [part for part in (run.printed.rstrip("\n"), run.value) if part]
        parts.append(f"[{log.line()}]")
        bounded = truncate_text(
            "\n".join(parts),
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=bounded.text,
            data={"calls": log.total, "failed_calls": log.failed},
            truncated=bounded.truncated,
        )

    def _function(self, name: str, log: _CallLog) -> ScriptFunction:
        call = self._call

        async def call_tool(*args: object, **kwargs: object) -> str:
            if args:
                raise TypeError(f"Call {name} with keyword arguments, such as {name}(path=...).")
            log.by_tool[name] += 1
            result = await call(name, kwargs)
            if result.is_error:
                log.failed += 1
                raise RuntimeError(f"{name} failed: {result.text}")
            return result.text

        return call_tool


def _signature(tool: ScriptableTool) -> _Signature | None:
    """Declare ``tool`` as a keyword-only async Python function, or None if it can't be."""

    if not _is_identifier(tool.name):
        return None
    properties = tool.input_schema.get("properties")
    required = tool.input_schema.get("required")
    properties = properties if isinstance(properties, Mapping) else {}
    required_names = set(required) if isinstance(required, list) else set()
    parameters: list[str] = []
    # Required parameters first, so the declaration is valid Python.
    for name in sorted(properties, key=lambda name: name not in required_names):
        parameter = _parameter(name, properties[name], required=name in required_names)
        if parameter is None:
            return None
        parameters.append(parameter)
    listed = f"*, {', '.join(parameters)}" if parameters else ""
    return _Signature(
        name=tool.name,
        declaration=f"async def {tool.name}({listed}) -> str: ...",
    )


def _parameter(name: object, schema: object, *, required: bool) -> str | None:
    """One keyword parameter's declaration, or None when ``name`` is not an identifier."""

    if not isinstance(name, str) or not _is_identifier(name):
        return None
    json_type = schema.get("type") if isinstance(schema, Mapping) else None
    kind = _JSON_TYPES.get(json_type, "object") if isinstance(json_type, str) else "object"
    return f"{name}: {kind}" if required else f"{name}: {kind} | None = None"


def _is_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _description(
    signatures: Sequence[_Signature], limits: ScriptLimits, *, discoverable: bool = False
) -> str:
    """Describe the tool, naming the tools a script may call.

    Each listed tool is also offered directly, so the model already has its full
    schema; repeating every signature here would resend it on every request. The
    declarations still reach the sandbox, which type-checks scripts against them.
    With ``discoverable``, MCP tools found on demand are callable too, unlisted.
    """

    lines = [
        "Run a Python script that calls other tools, and return what it prints and its "
        "last expression. Use it to make many calls at once, to chain calls on earlier "
        "answers, or to keep large results out of the conversation by filtering them in "
        "the script.",
        "",
        "Each tool named below is an async function with the tool's own name and "
        "parameters, called with keyword arguments only. It returns the text the tool "
        "would return to you, and raises RuntimeError when the call fails; catch it to "
        "carry on. Run independent calls at once with `await asyncio.gather(...)` after "
        "`import asyncio`.",
        "",
        "The script runs in a sandbox with a subset of Python: no files, network, "
        "classes with inheritance or third-party packages, and no map() or filter(), so "
        "use comprehensions; json, re, collections, itertools and math are available. "
        "It is type-checked against the tools' parameters before it runs.",
        f"Limits: {limits.max_calls} tool calls and {limits.wall_seconds:g} seconds per script.",
        "",
        "Callable tools:",
        *_grouped_names(signature.name for signature in signatures),
    ]
    if discoverable:
        lines += [
            "",
            "A read-only MCP tool that mcp_search finds is callable too, by its full name "
            "(such as mcp__server__tool_name), when that name and its parameters are "
            "Python identifiers; call any other through mcp_call(name=..., "
            "arguments=...). Calls by name are type-checked: mcp_describe shows a tool's "
            "parameter types.",
        ]
    return "\n".join(lines)


def _grouped_names(names: Iterable[str]) -> list[str]:
    """One line per MCP server's tools, then one for the rest, in the given order."""

    groups: dict[str, list[str]] = {}
    for name in names:
        prefix, separator, tool = name.rpartition("__")
        if separator and prefix.startswith("mcp__"):
            groups.setdefault(f"{prefix}__", []).append(tool)
        else:
            groups.setdefault("", []).append(name)
    return [
        f"- {prefix}{{{', '.join(tools)}}}" if prefix else f"- {', '.join(tools)}"
        for prefix, tools in groups.items()
    ]
