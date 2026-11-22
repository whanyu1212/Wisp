"""The codemode tool: the model writes a Python script that calls other tools.

A script can fan out many calls with `asyncio.gather`, chain calls on earlier
answers, and filter what it reads, so only what it prints and returns reaches the
model. Each call it makes is a separate call under the run's tool policy (see
`ConfiguredToolExecutor.run_codemode`), so the script itself needs no approval and
grants none. Scripts may call permitted read, mutation and command tools.
"""

from __future__ import annotations

import keyword
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from wisp.tool_types import ToolSafety
from wisp.tools.base import Tool, ToolArguments, ToolInputSchema, ToolPromptMetadata
from wisp.tools.codemode.calls import (
    CODEMODE,
    SCRIPT_RESULT_LIMITS,
    NestedCallResult,
    NestedToolCaller,
)
from wisp.tools.codemode.output import save_output, with_footer
from wisp.tools.codemode.result_types import STUB_IMPORTS, ResultType, result_type
from wisp.tools.codemode.sandbox import ScriptFunction, ScriptLimits, ScriptRun, run_script
from wisp.tools.codemode.state import ScriptState
from wisp.tools.context import ToolContext
from wisp.tools.output_store import OutputStore, SavedOutput, saved_output_notice
from wisp.tools.result import ToolError, ToolResult

_JSON_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list[object]",
    "object": "dict[str, object]",
}

_HELPER_NAMES = frozenset({"store", "load", "delete", "gather_settled", "_asyncio", "_TOOL_NAMES"})
# A loaded key has no statically known shape. Any lets scripts use ordinary dict
# and list operations; the host validates strict JSON when a value is stored.
_STATE_STUBS = """\
async def store(key: str, value: object) -> None: ...
async def load(key: str) -> Any: ...
async def delete(key: str) -> bool: ...
"""

# A failed script's message stays under the executor's cap on the errors a model is
# shown (2,000 characters), so that cap never cuts off the list of calls it made.
_FAILURE_MESSAGE_CHARS = 1_900
# Room for the "[N more characters]" markers that shortened parts carry.
_SHORTENED_MARKER_CHARS = 64
# The most a failure's list of calls may take, so the error always keeps room.
_FAILURE_CALLS_CHARS = 600

# Run in the sandbox before every script. `asyncio.gather` ends on the first failed
# call and drops the results of the rest, and the sandbox's `gather` takes no
# `return_exceptions`, so each call is wrapped to hand its error back as a value.
#
# Only a failed tool call is handed back, which `CodemodeTool._function` raises as
# a plain RuntimeError reading "<tool> failed: ...". The sandbox also raises
# RuntimeError, or a subclass, for bugs in the script itself (endless recursion,
# a dict changed while iterating, a coroutine awaited twice), and those must still
# end it. The alias keeps `asyncio` itself for the script to import.
_HELPERS = """\
import asyncio as _asyncio


async def gather_settled(*calls):
    async def settled(call):
        try:
            return await call
        except RuntimeError as error:
            tool, failed, _ = str(error).partition(" failed: ")
            if type(error).__name__ != "RuntimeError" or not failed or tool not in _TOOL_NAMES:
                raise
            return error

    return await _asyncio.gather(*[settled(call) for call in calls])
"""


def _helpers(tool_names: tuple[str, ...]) -> str:
    """Return the helper code for a run whose scripts may call ``tool_names``.

    A failed call reaches the script as a plain `RuntimeError` reading
    `<tool> failed: …`: monty keeps only the message of an error the host raises,
    so no mark on it survives. `gather_settled` therefore hands back only errors
    that name one of the run's own tools. A script that raises that message
    itself is handed back too, which is the remaining, harmless gap.
    """

    # `repr` quotes each name, so no name can change the helper's code.
    return f"_TOOL_NAMES = {tool_names!r}\n{_HELPERS}"


# The last line of a result that is only part of the tool's answer, so a script
# can check for it before counting or searching the rest.
INCOMPLETE_MARKER = "[result incomplete]"

_logger = logging.getLogger(__name__)

CODEMODE_PROMPT = ToolPromptMetadata(
    prompt_snippet="codemode: run a Python script that calls the permitted tools.",
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
    # How its structured result is typed, or None when it returns text.
    result: ResultType | None = None


@dataclass(slots=True)
class _CallLog:
    """The calls one script made, for its result."""

    by_tool: Counter[str] = field(default_factory=Counter)
    failed: int = 0
    incomplete: int = 0

    @property
    def total(self) -> int:
        return sum(self.by_tool.values())

    def line(self, max_chars: int | None = None) -> str:
        """Summarize the calls, naming tools until ``max_chars`` and counting the rest."""

        if not self.by_tool:
            return "No tool calls."
        noun = "call" if self.total == 1 else "calls"
        notes = [
            f"{count} {label}"
            for count, label in ((self.failed, "failed"), (self.incomplete, "incomplete"))
            if count
        ]
        counted = f" ({', '.join(notes)})" if notes else ""
        head = f"{self.total} tool {noun}{counted}: "
        entries = [
            name if count == 1 else f"{name} ×{count}" for name, count in self.by_tool.items()
        ]
        line = head + ", ".join(entries)
        if max_chars is None or len(line) <= max_chars:
            return line
        # Leave room for the ", and N more tools" that ends a shortened list.
        budget = max_chars - len(head) - len(f", and {len(entries)} more tools")
        named: list[str] = []
        for entry in entries:
            if len(", ".join([*named, entry])) > budget:
                break
            named.append(entry)
        rest = f"{len(entries) - len(named)} more tools"
        return head + (f"{', '.join(named)}, and {rest}" if named else rest)


class CodemodeTool:
    """Run a Python script whose only way out is calling the run's permitted tools."""

    name = CODEMODE
    # The script itself touches nothing; every call it makes is checked on its own.
    safety: ToolSafety = "read"
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python script. Its last expression is its result.",
            },
            "title": {
                "type": "string",
                "description": "What the script does, in a few words, such as "
                "'count TODOs per crate'. Shown to the user.",
            },
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
        state: ScriptState | None = None,
        output_store: OutputStore | None = None,
        discovery_tools: Sequence[Tool] = (),
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
            state (ScriptState | None): Shared transactional JSON state. None uses
                fresh ephemeral state retained by this tool instance.
            output_store (OutputStore | None): Shared store exposed by read_output.
                None leaves truncated output unrecoverable.
            discovery_tools (Sequence[Tool]): Per-run catalog readers, offered beside
                codemode and callable from scripts without granting target permissions.
        """

        self.discovery_tools = tuple(discovery_tools)
        tools = (*tools, *self.discovery_tools)
        taken = _HELPER_NAMES | frozenset(tool.name for tool in (*tools, *discoverable))
        listed = tuple(
            signature for tool in tools if (signature := _signature(tool, taken)) is not None
        )
        found = tuple(
            signature for tool in discoverable if (signature := _signature(tool, taken)) is not None
        )
        self._signatures = (*listed, *found)
        self._results = {
            signature.name: signature.result
            for signature in self._signatures
            if signature.result is not None
        }
        self._call = call
        self._limits = limits or ScriptLimits()
        self._state = state if state is not None else ScriptState()
        self._output_store = output_store
        self.description = _description(
            listed, self._limits, discoverable=bool(found), unified_discovery=bool(discovery_tools)
        )
        declarations = [
            declaration
            for signature in self._signatures
            if signature.result is not None
            for declaration in signature.result.declarations
        ]
        self._stubs = (
            STUB_IMPORTS
            + _STATE_STUBS
            + "\n".join((*declarations, *(signature.declaration for signature in self._signatures)))
            + "\n"
        )

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
        transaction = self._state.transaction()
        try:
            run = await run_script(
                code,
                functions={
                    **{name: self._function(name, log) for name in self.tool_names},
                    "store": transaction.store,
                    "load": transaction.load,
                    "delete": transaction.delete,
                },
                stubs=self._stubs,
                limits=self._limits,
                helpers=_helpers(self.tool_names),
            )
            if run.failure == "internal":
                # Wisp's own helpers did not load, so the script never ran: not a
                # mistake the model can fix by rewriting it.
                _logger.error("codemode's helpers did not load: %s", run.error)
                raise ToolError(
                    "codemode could not start: its own setup failed, so no script can run. "
                    "Call the tools directly.",
                    failure_code="internal_error",
                )
            if run.failure is not None:
                # External effects already performed stand; only script state rolls back.
                status = await save_output(run.printed, self._output_store) if run.printed else None
                notice = saved_output_notice([("script", status)])
                raise ToolError(_failure_message(run, log, notice))
            body = "\n".join(part for part in (run.printed.rstrip("\n"), run.value) if part)
            # The list of calls is cut last: it says how many failed or came back
            # incomplete, which the model needs most when the script's output is long.
            footer = f"[{log.line()}]"
            text, truncated = with_footer(body, footer, context)
            data: dict[str, object] = {"calls": log.total, "failed_calls": log.failed}
            if truncated and body:
                status = await save_output(body, self._output_store)
                text, _ = with_footer(body, footer, context, status)
                if isinstance(status, SavedOutput):
                    data["output_id"] = status.output_id
            result = ToolResult(
                text=text,
                data=data,
                truncated=truncated,
            )
            transaction.commit()
            return result
        finally:
            transaction.close()

    def _function(self, name: str, log: _CallLog) -> ScriptFunction:
        call = self._call
        returns = self._results.get(name)

        async def call_tool(*args: object, **kwargs: object) -> object:
            if args:
                raise TypeError(f"Call {name} with keyword arguments, such as {name}(path=...).")
            log.by_tool[name] += 1
            result = await call(name, kwargs)
            if result.is_error:
                log.failed += 1
                raise RuntimeError(f"{name} failed: {result.text}")
            if returns is not None:
                value, problem = _structured(result, returns)
                if problem is not None:
                    log.failed += 1
                    raise RuntimeError(f"{name} failed: {problem}")
                return value
            if not result.incomplete:
                return result.text
            log.incomplete += 1
            separator = "" if not result.text or result.text.endswith("\n") else "\n"
            return f"{result.text}{separator}{INCOMPLETE_MARKER}"

        return call_tool


def _failure_message(run: ScriptRun, log: _CallLog, notice: str | None = None) -> str:
    """Describe a failed script: its error, what it printed, and the calls it made.

    The list of calls is kept whole and the rest shares what room is left: the error
    keeps its start, where the cause is named, and the printout its end, closest to
    where the script failed. Without a printout the error has all the room, so a type
    check reporting several mistakes shows more than one.
    """

    heading = f"Script failed ({run.failure}): "
    printed_label = "Printed before it failed:\n"
    calls = f"Calls made before it failed: {log.line(_FAILURE_CALLS_CHARS)}"
    effects = "External effects are not rolled back. Inspect completed calls before retrying."
    recovery = ([notice] if notice else []) + ([effects] if log.total else [])
    room = (
        _FAILURE_MESSAGE_CHARS
        - len(heading)
        - len(calls)
        - sum(len(item) + 1 for item in recovery)
        - _SHORTENED_MARKER_CHARS
        - 4
    )
    if not run.printed:
        return "\n".join([heading + _head(run.error or "", room), *recovery, calls])
    error_room = room * 2 // 5
    printed_room = room - error_room - len(printed_label)
    return "\n".join(
        [
            heading + _head(run.error or "", error_room),
            printed_label + _tail(run.printed, printed_room),
            *recovery,
            calls,
        ]
    )


def _head(text: str, limit: int) -> str:
    """The first ``limit`` characters of ``text``, marked when shortened."""

    limit = max(0, limit)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more characters]"


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` characters of ``text``, marked when shortened."""

    limit = max(0, limit)
    if len(text) <= limit:
        return text
    tail = text[-limit:] if limit else ""
    return f"[{len(text) - limit} earlier characters] …{tail}"


def callable_by_name(tool: ScriptableTool) -> bool:
    """Report whether a script can call ``tool`` as a function of its own name.

    Returns:
        bool: False when the tool's name or one of its parameters is not a Python
        identifier. A script then reaches it only through `mcp_call`, which a run
        has when it finds its MCP tools on demand.
    """

    return _signature(tool, frozenset()) is not None


def _signature(tool: ScriptableTool, taken: frozenset[str]) -> _Signature | None:
    """Declare ``tool`` as a keyword-only async Python function, or None if it can't be.

    A tool with an output schema returns its structured result, typed from that
    schema (see `result_types`); any other returns its text. ``taken`` holds the
    names a result type's declarations must not reuse.
    """

    if not _is_identifier(tool.name) or tool.name in _HELPER_NAMES:
        return None
    properties = tool.input_schema.get("properties")
    required = tool.input_schema.get("required")
    properties = properties if isinstance(properties, Mapping) else {}
    # MCP servers send schemas of any draft; older ones nest lists in `required`.
    required_names = (
        {name for name in required if isinstance(name, str)}
        if isinstance(required, list)
        else set()
    )
    parameters: list[str] = []
    # Required parameters first, so the declaration is valid Python.
    for name in sorted(properties, key=lambda name: name not in required_names):
        parameter = _parameter(name, properties[name], required=name in required_names)
        if parameter is None:
            return None
        parameters.append(parameter)
    listed = f"*, {', '.join(parameters)}" if parameters else ""
    output_schema = getattr(tool, "output_schema", None)
    returns = result_type(
        tool.name, output_schema if isinstance(output_schema, Mapping) else None, taken=taken
    )
    annotation = returns.annotation if returns is not None else "str"
    return _Signature(
        name=tool.name,
        declaration=f"async def {tool.name}({listed}) -> {annotation}: ...",
        result=returns,
    )


def _structured(result: NestedCallResult, returns: ResultType) -> tuple[object, str | None]:
    """The value a structured tool's call gives its script, or why it has none.

    The stub promises the script a value, so a call without one fails rather than
    falling back to text.
    """

    if result.value_error is not None:
        return None, result.value_error
    value = result.value
    if not isinstance(value, dict):
        return None, "it returned no structured result"
    if returns.kind == "unwrapped":
        if "result" not in value:
            return None, "its structured result has no `result` field"
        return value["result"], None
    return value, None


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
    signatures: Sequence[_Signature],
    limits: ScriptLimits,
    *,
    discoverable: bool = False,
    unified_discovery: bool = False,
) -> str:
    """Describe the tool, naming the tools a script may call.

    Each listed tool is also offered directly, so the model already has its full
    schema; repeating every signature here would resend it on every request. The
    declarations still reach the sandbox, which type-checks scripts against them.
    With ``discoverable``, MCP tools found on demand are callable too, unlisted.
    """

    if unified_discovery:
        # Exact names and schemas are available on demand; do not repeat a large
        # catalog or server-controlled names in every model request.
        signatures = ()
    lines = [
        "Run a Python script that calls other tools, and return what it prints and its "
        "last expression. Use it to make many calls at once, to chain calls on earlier "
        "answers, or to keep large results out of the conversation by filtering them in "
        "the script.",
        "",
        "Each available tool is an async function with the tool's own name and "
        "parameters, called directly with keyword arguments only; no tool import is needed "
        "and there is no `tools` module. Return values follow the tool's described "
        "contract: text or typed structured data. Prefer the named callable when "
        "available; `mcp_call` returns text, which is not necessarily JSON. Text results "
        f"are limited to {_kib(SCRIPT_RESULT_LIMITS.max_bytes)}, more than a direct call "
        "returns. A failed call raises RuntimeError; catch it to carry on. Text that is "
        f"only part of the answer ends with the line `{INCOMPLETE_MARKER}`. Run "
        "independent calls at once with `await asyncio.gather(...)` after "
        "`import asyncio`: one failed call ends it and loses the other results. "
        "`await gather_settled(...)` takes the same calls and returns every outcome in "
        "order, a failed call's RuntimeError in place of its result.",
        "",
        "The script runs in a sandbox with a subset of Python: no files, network, "
        "classes with inheritance or third-party packages. Some builtins are missing: "
        "map(), filter(), ascii(), format(), getattr(), hasattr(), setattr(), callable(), "
        "dir() and vars(); use comprehensions, f-strings and plain attribute access. "
        "Stdlib modules json, re, collections, itertools and math can be imported (they are "
        "not pre-imported; write e.g. 'import json'). The entire script is statically "
        "type-checked before running; annotate heterogeneous containers (e.g. "
        "'rows: list[dict[str, object]] = []') so later appends do not fail type inference.",
        "",
        "State: `await store(key, value)`, `await load(key)` (KeyError if missing), "
        "and `await delete(key)` (whether it existed). Values are copied strict JSON. "
        "Writes commit together only when the script succeeds; failure, timeout or "
        "cancellation discards them. External effects (file writes, commands and MCP "
        "actions) are NOT rolled back. Inspect completed calls before retrying; never "
        "blindly rerun a failed script with mutations. Each child follows normal "
        "permissions and approvals; approving codemode does not approve its children. "
        "State lasts across scripts in this session; in persisted sessions it survives "
        "compaction and resuming the same branch after restart. Try `await load(key)` "
        "before refetching retained data unless fresh data is required; refetch if the "
        "key is missing. "
        "Quotas: 128 keys; non-empty keys up to 128 UTF-8 bytes; values up to 64 KiB "
        "canonical UTF-8 JSON, 256 KiB total, and 32 nested containers.",
        f"Limits: {limits.max_calls} tool calls, {limits.wall_seconds:g} seconds and "
        f"{_kib(limits.max_printed_bytes)} combined printed/final output per script.",
        "",
        "Callable tools:",
        *_grouped_names(signature.name for signature in signatures),
    ]
    if unified_discovery:
        lines[-1:] = [
            "Use tool_search(query=...) for the unified catalog of built-in and MCP "
            "functions, or an empty query to list them. tool_describe(name=...) gives "
            "input/result schemas, return semantics, guidance and confirmation requirements. "
            "Tools with structured results return typed data instead of text. "
            "Both are callable here too. Discovery grants no execution permission. "
            "Use its callable_name with keyword arguments. For invocation=mcp_call, "
            "use mcp_call(name=the discovered name, arguments={...}); this returns text."
        ]
    structured = [signature for signature in signatures if signature.result is not None]
    if structured:
        lines += [
            "",
            "These return their structured result instead of text, typed so a script "
            "using a field they lack fails its type check:",
            *(
                f"- {signature.name}: {signature.result.summary}"
                for signature in structured
                if signature.result is not None
            ),
        ]
    if discoverable:
        lines += [
            "",
            "An eligible MCP tool that mcp_search finds is callable too, by its full name "
            "(such as mcp__server__tool_name), when that name and its parameters are "
            "Python identifiers; call any other through mcp_call(name=..., "
            "arguments=...). Calls by name are type-checked: mcp_describe shows a tool's "
            "parameter types, and what a script calling it receives when that is not text.",
        ]
    return "\n".join(lines)


def _kib(size: int) -> str:
    return f"{size // 1024} KiB"


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
