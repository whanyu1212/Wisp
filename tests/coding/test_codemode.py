"""The experimental codemode tool: scripts reach only what a direct call could.

These drive a real `CodingSession` with fake tools and a scripted model, and the
executor's nested calls directly, with scripts running in monty.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentMode,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolNestedCallEnded,
    ToolNestedCallStarted,
    ToolResultReady,
    WispEvent,
)
from wisp.mcp.config import McpToolExposure
from wisp.mcp.discovery import MCP_CALL, McpCatalog, discovery_tools
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions import MessageSessionEntry
from wisp.sessions.entries import session_entry_from_json
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tool_types import ToolSafety
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolArguments, ToolExecutionMetadata, ToolInputSchema
from wisp.tools.codemode.calls import (
    CODEMODE,
    SCRIPT_RESULT_LIMITS,
    NestedCallResult,
    ResultLimits,
)
from wisp.tools.codemode.sandbox import ScriptLimits
from wisp.tools.codemode.tool import INCOMPLETE_MARKER, CodemodeTool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text

pytest.importorskip("pydantic_monty")

_SCHEMA: ToolInputSchema = {
    "type": "object",
    "properties": {"table": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["table"],
}


@dataclass
class _Tool:
    name: str
    safety: ToolSafety = "read"
    description: str = "Query a table."
    input_schema: ToolInputSchema = field(default_factory=lambda: dict(_SCHEMA))
    delay: float = 0.0
    reply: str | None = None
    # An MCP tool's output schema, and the structured result it returns beside text.
    output_schema: dict[str, object] | None = None
    structured: object = None
    confirm_every_call: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)
    running: int = 0
    most_running: int = 0

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.calls.append(dict(arguments))
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            await anyio.sleep(self.delay)
        finally:
            self.running -= 1
        return ToolResult(
            text=self.reply or f"{self.name}:{arguments.get('table')}",
            structured=self.structured,
        )


@dataclass
class _McpTool(_Tool):
    """A tool from MCP server `db`, which on-demand exposure finds in the catalog."""

    server_name: str = "db"

    @property
    def remote_name(self) -> str:
        return self.name.rsplit("__", 1)[-1]


@dataclass
class _Tools:
    query: _Tool = field(default_factory=lambda: _McpTool("mcp__db__query"))
    drop: _Tool = field(default_factory=lambda: _McpTool("mcp__db__drop", safety="command"))
    lookup: _Tool = field(default_factory=lambda: _Tool("lookup"))

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(self.query)
        registry.register(self.drop)
        registry.register(self.lookup, execution=ToolExecutionMetadata(parallel_safe=True))
        return registry


def _script(code: str) -> list[ProviderEvent]:
    return tool_call_stream(
        ToolCall(call_id="call-script", name=CODEMODE, arguments={"code": code})
    )


def _done() -> list[ProviderEvent]:
    return [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]


def _run(
    tmp_path: Path,
    tools: _Tools,
    streams: Sequence[list[ProviderEvent]],
    *,
    codemode: bool | None = True,
    mode: AgentMode = "build",
    exposure: McpToolExposure = "direct",
    timeline: list[WispEvent] | None = None,
) -> tuple[list[WispEvent], ScriptedProvider]:
    provider = ScriptedProvider([*streams, _done()])
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.require_approval(),
        mcp_tool_exposure=exposure,
        codemode=codemode,
        mode=mode,
    )

    async def live(event: WispEvent) -> None:
        if timeline is not None:
            timeline.append(event)

    async def collect() -> list[WispEvent]:
        events = []
        async for event in session.run("go", live_event_sink=live):
            events.append(event)
            if timeline is not None:
                timeline.append(event)
        return events

    return anyio.run(collect), provider


def _result(events: Sequence[WispEvent]) -> ToolResultReady:
    (result,) = [event for event in events if isinstance(event, ToolResultReady)]
    return result


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_offered_only_when_on(tmp_path: Path, enabled: bool) -> None:
    _, provider = _run(tmp_path, _Tools(), [], codemode=enabled)

    offered = {tool.name: tool for tool in provider.calls[0].tools}
    assert (CODEMODE in offered) is enabled
    if enabled:
        description = offered[CODEMODE].description
        # Only read-only tools are callable, named but not re-declared: the model
        # already has each one's schema as a direct tool.
        assert description.endswith("Callable tools:\n- mcp__db__{query}\n- lookup")
        assert "async def" not in description


@pytest.mark.parametrize(
    ("codemode", "exposure", "offered"),
    [
        (None, "on_demand", True),
        (None, "direct", True),
        # The default exposure keeps a small catalog direct, which offers it too.
        (None, "auto", True),
        (False, "on_demand", False),
        (True, "direct", True),
    ],
    ids=["auto-on-demand", "auto-direct", "auto-small-catalog", "off-on-demand", "on-direct"],
)
def test_automatic_with_mcp_tools(
    tmp_path: Path, codemode: bool | None, exposure: McpToolExposure, offered: bool
) -> None:
    """Unset, codemode is offered when a script may call one of the run's MCP tools."""

    _, provider = _run(tmp_path, _Tools(), [], codemode=codemode, exposure=exposure)

    assert (CODEMODE in {tool.name for tool in provider.calls[0].tools}) is offered


@pytest.mark.parametrize(
    ("query", "allowed"),
    [
        # Only built-in read tools: the evals found no gain there.
        (None, None),
        # Sent directly, an MCP tool a script cannot call by name does not count.
        (_McpTool("mcp__db__query-rows"), None),
        (_McpTool("mcp__db__query", input_schema={"properties": {"from": {}}}), None),
        # Nor does one the policy keeps from the run.
        (_McpTool("mcp__db__query"), {"lookup"}),
    ],
    ids=["builtins-only", "uncallable-name", "uncallable-parameter", "policy-removed"],
)
def test_automatic_needs_callable_mcp(
    tmp_path: Path, query: _Tool | None, allowed: set[str] | None
) -> None:
    """Unset, the built-in read tools alone do not offer codemode; set, they do."""

    tools = _Tools()
    registry = ToolRegistry()
    registry.register(tools.lookup)
    if query is not None:
        registry.register(query)
    session = CodingSession(
        provider=ScriptedProvider([_done()]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        mcp_tool_exposure="direct",
        **({} if allowed is None else {"tool_policy": ToolPolicy.allow_tool_names(allowed)}),
    )

    assert session.codemode_offered() is False
    session.codemode = True
    assert session.codemode_offered() is True


def test_older_schema_drafts_scriptable(tmp_path: Path) -> None:
    """A draft-03 schema, with lists in `required`, neither crashes nor blocks codemode."""

    tools = _Tools()
    tools.query.input_schema = {
        "$schema": "http://json-schema.org/draft-03/schema#",
        "properties": {"table": {"type": [{"type": "string"}, "string"]}},
        "required": [["table"], "table"],
    }

    events, provider = _run(tmp_path, tools, [_script("await mcp__db__query(table='a')")])

    assert CODEMODE in {tool.name for tool in provider.calls[0].tools}
    assert not _result(events).is_error


def test_script_fans_out(tmp_path: Path) -> None:
    tools = _Tools()
    code = (
        "import asyncio\n"
        "rows = await asyncio.gather(*[mcp__db__query(table=t) for t in ['a', 'b', 'c']])\n"
        "print(len(rows))\n"
        "[row for row in rows if not row.endswith('b')]"
    )

    events, _ = _run(tmp_path, tools, [_script(code)])

    result = _result(events)
    assert not result.is_error
    assert result.output.splitlines() == [
        "3",
        '["mcp__db__query:a", "mcp__db__query:c"]',
        "[3 tool calls: mcp__db__query ×3]",
    ]
    assert [call["table"] for call in tools.query.calls] == ["a", "b", "c"]
    (ended,) = [event for event in events if isinstance(event, ToolExecutionEnded)]
    assert ended.summary == "codemode: 3 tool calls"


def test_unlisted_tool_unreachable(tmp_path: Path) -> None:
    tools = _Tools()

    events, _ = _run(tmp_path, tools, [_script("await mcp__db__drop(table='users')")])

    result = _result(events)
    assert result.is_error and "Script failed (type)" in result.output
    assert tools.drop.calls == []


def test_type_error_runs_nothing(tmp_path: Path) -> None:
    tools = _Tools()
    code = "await mcp__db__query(table='a')\nawait mcp__db__query(tabel='b')"

    events, _ = _run(tmp_path, tools, [_script(code)])

    assert "Script failed (type)" in _result(events).output
    assert tools.query.calls == []


def test_failure_reports_calls(tmp_path: Path) -> None:
    tools = _Tools()
    code = "await mcp__db__query(table='a')\nprint('half way')\nraise ValueError('stop here')"

    events, _ = _run(tmp_path, tools, [_script(code)])

    output = _result(events).output
    assert "Script failed (runtime)" in output and "stop here" in output
    assert "half way" in output and "1 tool call: mcp__db__query" in output


@pytest.mark.parametrize(("codemode", "warns"), [(True, True), (None, False)], ids=["on", "auto"])
def test_not_offered_without_monty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    codemode: bool | None,
    warns: bool,
) -> None:
    """Without the extra, codemode is left out; only an explicit choice warns."""

    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: object) -> object:
        return None if name == "pydantic_monty" else real_find_spec(name, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    _, provider = _run(tmp_path, _Tools(), [], codemode=codemode, exposure="on_demand")

    assert CODEMODE not in [tool.name for tool in provider.calls[0].tools]
    assert ("codemode extra is not installed" in caplog.text) is warns


@pytest.mark.parametrize(
    ("codemode", "exposure", "mcp_safety", "offered"),
    [
        (True, "direct", "read", True),
        (None, "direct", "read", True),
        (None, "direct", "command", False),
        (None, "on_demand", "read", True),
        (False, "on_demand", "read", False),
        # Unset, the built-in read tools are not enough: with no MCP tool a script
        # may call, every call a script made to the catalog would be refused.
        (None, "on_demand", "command", False),
        (True, "on_demand", "command", True),
    ],
)
def test_offered_matches_the_run(
    tmp_path: Path,
    codemode: bool | None,
    exposure: McpToolExposure,
    mcp_safety: ToolSafety,
    offered: bool,
) -> None:
    """The session predicts, without a run, whether its next run offers codemode."""

    tools = _Tools()
    tools.query.safety = mcp_safety
    provider = ScriptedProvider([_done()])
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        mcp_tool_exposure=exposure,
        codemode=codemode,
    )

    predicted = session.codemode_offered()
    scriptable, _ = session.scriptable_mcp_tools()
    if codemode is None:
        # The count a frontend shows must agree with why automatic mode is on or off.
        assert (scriptable > 0) is offered

    async def run() -> None:
        async for _ in session.run("go"):
            pass

    anyio.run(run)
    assert predicted is offered
    assert (CODEMODE in [tool.name for tool in provider.calls[0].tools]) is offered


@pytest.mark.parametrize(
    ("mcp_safety", "mode", "exposure", "counts"),
    [
        ("read", "build", "direct", (1, 2)),
        ("read", "build", "on_demand", (1, 2)),
        ("command", "build", "on_demand", (0, 2)),
        # Plan mode offers no command tool, so only the read-only MCP tool is left.
        ("read", "plan", "direct", (1, 1)),
    ],
    ids=["direct", "on-demand", "none-scriptable", "plan-mode"],
)
def test_counts_scriptable_mcp_tools(
    tmp_path: Path,
    mcp_safety: ToolSafety,
    mode: AgentMode,
    exposure: McpToolExposure,
    counts: tuple[int, int],
) -> None:
    """The session counts the MCP tools its next run may use, and those marked read-only."""

    tools = _Tools()
    tools.query.safety = mcp_safety
    session = CodingSession(
        provider=ScriptedProvider([]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        mcp_tool_exposure=exposure,
        mode=mode,
    )

    assert session.scriptable_mcp_tools() == counts


@pytest.mark.parametrize(("exposure", "scriptable"), [("direct", 1), ("on_demand", 2)])
def test_direct_counts_callable_names(
    tmp_path: Path, exposure: McpToolExposure, scriptable: int
) -> None:
    """Sent directly, a script reaches a tool only by a name Python accepts.

    Found on demand, `mcp_call` reaches the read-only tool of server `docs-site` too.
    """

    registry = _Tools().registry()
    registry.register(_McpTool("mcp__docs-site__search"))
    session = CodingSession(
        provider=ScriptedProvider([]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        mcp_tool_exposure=exposure,
    )

    assert session.scriptable_mcp_tools() == (scriptable, 3)


def test_policy_limits_counted_tools(tmp_path: Path) -> None:
    """A tool the policy keeps from the run is not one of its MCP tools."""

    session = CodingSession(
        provider=ScriptedProvider([]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_tool_names({"lookup", "mcp__db__drop"}),
    )

    assert session.scriptable_mcp_tools() == (0, 1)


def test_no_registry_counts_no_mcp_tools(tmp_path: Path) -> None:
    session = CodingSession(
        provider=ScriptedProvider([]), sessions=JsonlSessionStore(tmp_path / "sessions")
    )

    assert session.scriptable_mcp_tools() == (0, 0)


def test_script_is_safe_to_replay(tmp_path: Path) -> None:
    """A script reaches only read-only tools, so it never blocks overflow recovery."""

    session = CodingSession(
        provider=ScriptedProvider([]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        codemode=True,
    )

    assert not session._tool_is_unsafe(CODEMODE)
    assert session._tool_is_unsafe("mcp__db__drop")


def test_on_demand_scripts_call_catalog(tmp_path: Path) -> None:
    """On demand, a script calls a read-only catalog tool by name, unlisted."""

    tools = _Tools()
    code = "await mcp__db__query(table='a')"

    events, provider = _run(tmp_path, tools, [_script(code)], exposure="on_demand")

    offered = {tool.name: tool for tool in provider.calls[0].tools}
    assert "mcp__db__query" not in offered
    description = offered[CODEMODE].description
    assert "mcp__db__query" not in description and "mcp_search finds" in description
    result = _result(events)
    assert (result.is_error, result.output.splitlines()[0]) == (False, "mcp__db__query:a")
    assert tools.query.calls == [{"table": "a"}]


def test_on_demand_command_tool_unreachable(tmp_path: Path) -> None:
    tools = _Tools()

    events, _ = _run(
        tmp_path, tools, [_script("await mcp__db__drop(table='users')")], exposure="on_demand"
    )

    assert "Script failed (type)" in _result(events).output
    assert tools.drop.calls == []


def test_eval_reaches_only_given_tools(tmp_path: Path) -> None:
    """``eval`` skips the type check, but sees only the tools a script was given."""

    tools = _Tools()
    code = "await eval(\"mcp__db__drop(table='a')\")"

    events, _ = _run(tmp_path, tools, [_script(code)])

    assert "NameError" in _result(events).output
    assert tools.drop.calls == []


@pytest.mark.parametrize(
    ("size", "script_sees", "incomplete"),
    [
        # More than a direct call returns, so the script can filter all of it.
        (200_000, 200_000, False),
        # Cut at the script's own limit, and the last line says so.
        (5_000_000, None, True),
    ],
    ids=["above-model-limit", "above-script-limit"],
)
def test_script_reads_more_than_the_model(
    tmp_path: Path, size: int, script_sees: int | None, incomplete: bool
) -> None:
    """A script gets more of a result than the model would, and the model still gets its cap."""

    tools = _Tools(lookup=_Tool("lookup", reply="r" * size))
    code = (
        "text = await lookup(table='a')\n"
        f"print(len(text.encode()), text.endswith('{INCOMPLETE_MARKER}'))\n"
        "text"
    )

    events, _ = _run(tmp_path, tools, [_script(code)])

    output = _result(events).output
    seen, marked = output.splitlines()[0].split()
    if script_sees is None:
        assert ToolContext(cwd=tmp_path).max_output_bytes < int(seen)
        assert int(seen) <= SCRIPT_RESULT_LIMITS.max_bytes + 64
    else:
        assert int(seen) == script_sees
    assert marked == str(incomplete)
    # What the script returned is cut for the model as any result is, keeping the
    # list of calls, which says whether a result came back incomplete.
    assert len(output.encode()) <= ToolContext(cwd=tmp_path).max_output_bytes
    counted = " (1 incomplete)" if incomplete else ""
    assert "[truncated]\n" in output
    assert "saved-output recovery is unavailable" in output
    assert output.endswith(f"[1 tool call{counted}: lookup]")


@pytest.mark.parametrize(
    "code",
    [
        "for i in range(3000):\n    print(i)",
        "for i in range(1998):\n    print(i)\n'last\\n'",
        "print('x' * 200_000)",
    ],
    ids=["many-lines", "boundary-lines", "one-long-line"],
)
def test_footer_survives_cut_output(tmp_path: Path, code: str) -> None:
    """However the script's output is cut for the model, the list of calls stays last."""

    events, _ = _run(tmp_path, _Tools(), [_script(f"await lookup(table='a')\n{code}")])

    output = _result(events).output
    context = ToolContext(cwd=tmp_path)
    assert output.endswith("\n[1 tool call: lookup]")
    assert len(output.splitlines()) <= context.max_output_lines
    assert len(output.encode()) <= context.max_output_bytes


@dataclass
class _SelfCappingTool(_Tool):
    """A tool that cuts its own output to the context's limit, as `read` and MCP tools do."""

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        bounded = truncate_text(
            self.reply or "",
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(text=bounded.text, truncated=bounded.truncated)


def test_tool_runs_with_script_budget(tmp_path: Path) -> None:
    """The tool itself sees the larger budget, not only the step that bounds its result."""

    tools = _Tools(lookup=_SelfCappingTool("lookup", reply="r" * 200_000))

    events, _ = _run(tmp_path, tools, [_script("len(await lookup(table='a'))")])

    assert _result(events).output.startswith("200000\n")


def test_footer_counts_incomplete_calls() -> None:
    """The model sees how many results were only part of the answer, even if the script didn't."""

    async def partial(name: str, arguments: object) -> NestedCallResult:
        return NestedCallResult(text="first page\n", incomplete=True)

    result = _run_tool(_tool(partial), "await query(table='a')")

    assert result.text == f"first page\n{INCOMPLETE_MARKER}\n[1 tool call (1 incomplete): query]"


def test_incomplete_through_mcp_call(tmp_path: Path) -> None:
    """A result reached through `mcp_call` gets the same budget and marker as one by name."""

    tools = _Tools()
    tools.query.reply = "q" * 5_000_000
    code = (
        "text = await mcp_call(name='mcp__db__query', arguments={'table': 'a'})\n"
        f"[len(text.encode()) > 100_000, text.endswith('{INCOMPLETE_MARKER}')]"
    )

    events, _ = _run(tmp_path, tools, [_script(code)], exposure="on_demand")

    assert _result(events).output.startswith("[true, true]")


@pytest.mark.parametrize(
    ("limits", "seen"),
    [
        (None, ToolContext(cwd=Path(".")).max_output_bytes),
        (SCRIPT_RESULT_LIMITS, SCRIPT_RESULT_LIMITS.max_bytes),
    ],
    ids=["subagent", "script"],
)
def test_nested_limits_are_opt_in(limits: ResultLimits | None, seen: int) -> None:
    """Only a caller that asks for larger results gets them: a subagent's go to a model."""

    tools = _Tools(lookup=_Tool("lookup", reply="r" * 5_000_000))
    executor = _executor(tools, ToolApprovalPolicy.approve_all())

    async def run() -> NestedCallResult:
        call = ToolCall(call_id="nested-1", name="lookup", arguments={"table": "a"})
        return await executor.run_nested(call, result_limits=limits)

    result = anyio.run(run)

    assert result.incomplete and not result.is_error
    assert seen - 64 <= len(result.text.encode()) <= seen


def test_larger_context_budget_kept() -> None:
    """An embedder whose context allows more than the script limit keeps its budget."""

    tools = _Tools(lookup=_Tool("lookup", reply="r" * 900_000))
    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path("."), max_output_bytes=2_000_000),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )

    async def run() -> NestedCallResult:
        call = ToolCall(call_id="nested-1", name="lookup", arguments={"table": "a"})
        return await executor.run_nested(call, result_limits=SCRIPT_RESULT_LIMITS)

    result = anyio.run(run)

    assert (len(result.text), result.incomplete) == (900_000, False)


def test_offered_in_plan_mode(tmp_path: Path) -> None:
    _, provider = _run(tmp_path, _Tools(), [], mode="plan")

    assert CODEMODE in [tool.name for tool in provider.calls[0].tools]


# --- The tool itself, with a fake caller -----------------------------------------


def _tool(
    call: object = None, *, limits: ScriptLimits | None = None, tools: Sequence[_Tool] = ()
) -> CodemodeTool:
    async def echo(name: str, arguments: object) -> NestedCallResult:
        return NestedCallResult(text=f"{name}:{arguments}")

    return CodemodeTool(tools or [_Tool("query")], call or echo, limits=limits)  # type: ignore[arg-type]


def _run_tool(tool: CodemodeTool, code: str) -> ToolResult:
    async def run() -> ToolResult:
        return await tool.run({"code": code}, ToolContext(cwd=Path(".")))

    return anyio.run(run)


async def _locked_b(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
    """Answer every table but `b`, which fails."""

    table = arguments["table"]
    if table == "b":
        return NestedCallResult(text="table b is locked", is_error=True)
    return NestedCallResult(text=f"rows of {table}")


_THREE_TABLES = "(*[query(table=t) for t in ('a', 'b', 'c')])"


def test_settled_fan_out_keeps_results() -> None:
    """One failed call costs a fan-out only its own result, and the error says so."""

    result = _run_tool(_tool(_locked_b), f"await gather_settled{_THREE_TABLES}")

    outcomes, footer = result.text.rsplit("\n", 1)
    assert json.loads(outcomes) == [
        "rows of a",
        "RuntimeError: query failed: table b is locked",
        "rows of c",
    ]
    # The failure stays visible to the model even when the script ignores it.
    assert footer == "[3 tool calls (1 failed): query ×3]"
    assert result.data == {"calls": 3, "failed_calls": 1}
    with pytest.raises(ToolError, match=r"Script failed \(runtime\): RuntimeError"):
        _run_tool(_tool(_locked_b), f"import asyncio\nawait asyncio.gather{_THREE_TABLES}")


def test_settled_errors_are_runtime_errors() -> None:
    """A script tells a failed call from a result by the error's type, whatever a result is."""

    code = (
        f"outcomes = await gather_settled{_THREE_TABLES}\n"
        "[str(o) if isinstance(o, RuntimeError) else o.upper() for o in outcomes]"
    )

    result = _run_tool(_tool(_locked_b), code)

    assert json.loads(result.text.rsplit("\n", 1)[0]) == [
        "ROWS OF A",
        "query failed: table b is locked",
        "ROWS OF C",
    ]


def test_settled_calls_overlap() -> None:
    """The helper wraps each call without making the calls take turns."""

    running = most_running = 0

    async def slow(name: str, arguments: object) -> NestedCallResult:
        nonlocal running, most_running
        running += 1
        most_running = max(most_running, running)
        await anyio.sleep(0.05)
        running -= 1
        return NestedCallResult(text="done")

    _run_tool(_tool(slow), f"await gather_settled{_THREE_TABLES}")

    assert most_running == 3


@pytest.mark.parametrize(
    ("bug", "error"),
    [
        ("return str(1 / 0)", "ZeroDivisionError"),
        # The sandbox raises these as RuntimeError or a subclass of it, like a
        # failed tool call, and they are still the script's own mistakes.
        ("return await bug()", "RecursionError"),
        ("raise NotImplementedError('todo')", "NotImplementedError"),
        ("raise RuntimeError('gave up')", "RuntimeError: gave up"),
        # Worded like a failed call, but no tool of this run is named `parse`.
        ("raise RuntimeError('parse failed: bad row')", "RuntimeError: parse failed: bad row"),
        (
            "seen = {1: 1}\n    for key in seen:\n        seen[key + 1] = 1\n    return 'x'",
            "RuntimeError: dictionary changed size",
        ),
    ],
    ids=[
        "other-error",
        "recursion",
        "subclass",
        "own-runtime-error",
        "not-a-tool",
        "sandbox-runtime-error",
    ],
)
def test_settled_keeps_script_bugs(bug: str, error: str) -> None:
    """Only a failed tool call comes back as a value: a bug in the script still ends it."""

    code = f"async def bug() -> str:\n    {bug}\nawait gather_settled(bug(), query(table='a'))"

    with pytest.raises(ToolError, match=rf"Script failed \(runtime\): {error}"):
        _run_tool(_tool(), code)


@pytest.mark.parametrize("count", [1, 300])
def test_settled_knows_every_tool(count: int) -> None:
    """The helper loads for any number of tools and hands back each one's failures."""

    names = [f"tool_{index}" for index in range(count)]

    async def fail(name: str, arguments: object) -> NestedCallResult:
        return NestedCallResult(text="locked", is_error=True)

    tool = _tool(fail, tools=[_Tool(name) for name in names])
    result = _run_tool(tool, f"[str(e) for e in await gather_settled({names[-1]}(table='a'))]")

    assert f"{names[-1]} failed: locked" in result.text


def test_helper_keeps_script_line_numbers() -> None:
    """The helper runs before the script, apart from it, so line 1 is the script's own."""

    with pytest.raises(ToolError, match=r"main\.py:1:") as failure:
        _run_tool(_tool(), "await gather_settled(query(table=1))")

    assert "Script failed (type)" in str(failure.value)


def test_broken_helpers_are_not_the_scripts_fault(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If Wisp's own helper code stops loading, no script runs and none is blamed."""

    calls: list[str] = []

    async def record(name: str, arguments: object) -> NestedCallResult:
        calls.append(name)
        return NestedCallResult(text="rows")

    tool = _tool(record)
    monkeypatch.setattr("wisp.tools.codemode.tool._HELPERS", "def gather_settled(:\n")

    with pytest.raises(ToolError) as failure:
        _run_tool(tool, "await query(table='a')")

    assert calls == []
    assert failure.value.failure_code == "internal_error"
    assert "Script failed" not in str(failure.value)
    assert "codemode's helpers did not load" in caplog.text


def test_description_names_the_helper() -> None:
    assert "`await gather_settled(...)`" in _tool().description


def test_refused_call_is_a_settled_error(tmp_path: Path) -> None:
    """A call a script may not make comes back as an error value, recorded and never run."""

    tools = _Tools()
    code = (
        "outcomes = await gather_settled(\n"
        "    mcp__db__query(table='a'),\n"
        "    mcp_call(name='mcp__db__drop', arguments={'table': 'users'}),\n"
        ")\n"
        "[isinstance(outcome, RuntimeError) for outcome in outcomes]"
    )

    events, _ = _run(tmp_path, tools, [_script(code)], exposure="on_demand")

    result = _result(events)
    assert not result.is_error and result.output.startswith("[false, true]")
    assert (tools.query.calls, tools.drop.calls) == ([{"table": "a"}], [])
    refused = [call for call in _ended(events).nested_calls if call.is_error]
    assert [(call.target, call.failure_code) for call in refused] == [
        ("mcp__db__drop", "policy_denied")
    ]


@dataclass
class _LockedMcpTool(_McpTool):
    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        raise ToolError("table is locked")


def test_failed_catalog_call_is_settled(tmp_path: Path) -> None:
    """A catalog tool called by name, found on demand, is one of the run's tools too."""

    tools = _Tools(query=_LockedMcpTool("mcp__db__query"))
    code = "[str(outcome) for outcome in await gather_settled(mcp__db__query(table='a'))]"

    events, _ = _run(tmp_path, tools, [_script(code)], exposure="on_demand")

    result = _result(events)
    assert not result.is_error
    assert "mcp__db__query failed: table is locked" in result.output


def test_failed_call_raises_in_script() -> None:
    async def fail(name: str, arguments: object) -> NestedCallResult:
        return NestedCallResult(text="table is locked", is_error=True)

    code = (
        "try:\n    await query(table='a')\n    outcome = 'ran'\n"
        "except RuntimeError as error:\n    outcome = str(error)\noutcome"
    )
    result = _run_tool(_tool(fail), code)

    assert result.text.splitlines()[0] == "query failed: table is locked"
    assert result.data == {"calls": 1, "failed_calls": 1}


_GATHER = "import asyncio\nawait asyncio.gather(*[query(table='a') for _ in range({})])"


@pytest.mark.parametrize(
    ("code", "limits", "message"),
    [
        (
            "while True:\n    pass",
            ScriptLimits(compute_seconds=0.2),
            r"\(limit\): The script computed for more than 0.2 seconds",
        ),
        (
            "for i in range(5):\n    await query(table='a')",
            ScriptLimits(max_calls=3),
            r"\(limit\): The script made more than 3 tool calls",
        ),
        (_GATHER.format(5000), ScriptLimits(max_calls=3), r"\(limit\): .* more than 3 tool calls"),
        # The call limit is not an error a script can catch, so the helper that
        # hands failed calls back as values does not hand this one back.
        (
            "async def one() -> str:\n    return await query(table='a')\n"
            "await gather_settled(*[one() for _ in range(5000)])",
            ScriptLimits(max_calls=3),
            r"\(limit\): .* more than 3 tool calls",
        ),
        (
            "x = []\nwhile True:\n    x.append('a' * 100000)",
            ScriptLimits(memory_bytes=8 * 1024 * 1024),
            r"\(limit\): The script used more than 8 MiB of memory",
        ),
        (
            "for _ in range(100):\n    print('y' * 100)",
            ScriptLimits(max_printed_bytes=1000),
            r"\(limit\): The script printed more than 1000 bytes",
        ),
        ("def f(n):\n    return f(n + 1)\nf(0)", None, r"\(runtime\): RecursionError"),
        ("await query(table='a')", ScriptLimits(wall_seconds=0.2), r"\(timeout\)"),
        # Type-checked first, so a positional call or bad syntax never runs.
        ("await query('a')", None, r"\(type\)"),
        ("x = (", None, r"\(type\)"),
    ],
    ids=[
        "compute",
        "calls",
        "gather",
        "settled",
        "memory",
        "printed",
        "recursion",
        "wall-clock",
        "positional",
        "syntax",
    ],
)
def test_limits_end_script(code: str, limits: ScriptLimits | None, message: str) -> None:
    async def slow(name: str, arguments: object) -> NestedCallResult:
        await anyio.sleep(1)
        return NestedCallResult(text="late")

    caller = slow if "timeout" in message else None
    with pytest.raises(ToolError, match=rf"Script failed {message}"):
        _run_tool(_tool(caller, limits=limits), code)


@pytest.mark.parametrize(
    "code",
    [
        "import socket",
        "import subprocess",
        "import os\nos.environ['HOME']",
        "import os\nos.getenv('HOME')",
        "import os\nos.system('true')",
        "__import__('os')",
        "eval(\"__import__('os')\")",
        "compile('1', 'f', 'eval')",
        "globals()",
        "vars()",
        "__builtins__",
        "getattr(query, '__globals__')",
        # eval and exec skip the type check, so the same reach is tried at run time.
        "eval('globals()')",
        "exec('vars()')",
        'eval(\'compile("1", "f", "eval")\')',
        "eval(\"getattr(query, '__globals__')\")",
        "eval('type(()).__subclasses__()')",
        "eval('().__class__.__mro__')",
        "query.__code__",
        "type(()).__subclasses__()",
        "().__class__.__mro__",
        "import sys\nsys.modules",
        "import sys\nsys._getframe()",
    ],
)
def test_no_sandbox_escape(code: str) -> None:
    with pytest.raises(ToolError, match=r"Script failed \((type|runtime)\)"):
        _run_tool(_tool(), code)


@pytest.mark.parametrize(
    "code",
    [
        "open({path!r}).read()",
        'eval("open({path!r}).read()")',
        'exec("open({path!r}).read()")',
        "from pathlib import Path\nPath({path!r}).read_text()",
        "from pathlib import Path\nPath({path!r}).exists()",
        "from pathlib import Path\nlist(Path({folder!r}).iterdir())",
        "import os\nos.listdir({folder!r})",
    ],
    ids=["open", "eval", "exec", "read_text", "exists", "iterdir", "listdir"],
)
def test_host_files_unreachable(tmp_path: Path, code: str) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret", encoding="utf-8")

    with pytest.raises(ToolError, match=r"PermissionError") as raised:
        _run_tool(_tool(), code.format(path=str(secret), folder=str(tmp_path)))

    assert "host secret" not in str(raised.value)


def test_failure_keeps_call_list() -> None:
    """A long error or printout still leaves room for the calls the script made."""

    code = (
        "await query(table='a')\nprint('START' + 'p' * 50000 + 'END')\n"
        "raise ValueError('CAUSE' + 'e' * 50000)"
    )

    with pytest.raises(ToolError) as raised:
        _run_tool(_tool(), code)

    message = str(raised.value)
    assert message.endswith("Calls made before it failed: 1 tool call: query")
    assert "ValueError: CAUSE" in message and "more characters]" in message
    assert "END" in message and "START" not in message and "earlier characters]" in message
    assert len(message) < 2_000


def test_many_tools_keep_error() -> None:
    """A script that called many distinct tools still shows why it failed."""

    names = [f"mcp__production_database__query_table_{index}" for index in range(60)]
    code = "\n".join(f"await {name}(table='a')" for name in names)
    code += "\nraise ValueError('CAUSE' + 'e' * 5000)"

    with pytest.raises(ToolError) as raised:
        _run_tool(_tool(tools=[_Tool(name) for name in names]), code)

    message = str(raised.value)
    assert "ValueError: CAUSE" in message and names[0] in message
    assert message.endswith("more tools") and "60 tool calls" in message
    assert len(message) < 2_000


def test_type_errors_keep_room() -> None:
    """Without a printout, the error has all the room: several type errors show."""

    code = "\n".join(f"await query(tabel_{index}='a')" for index in range(6))

    with pytest.raises(ToolError) as raised:
        _run_tool(_tool(), code)

    message = str(raised.value)
    assert message.count("error[") >= 3
    assert message.endswith("Calls made before it failed: No tool calls.")
    assert len(message) < 2_000


def test_host_error_reaches_script_as_text() -> None:
    """An exception a tool call raises reaches the script as its type name and text."""

    async def broken(name: str, arguments: object) -> NestedCallResult:
        raise LookupError(object())

    code = (
        "try:\n    await query(table='a')\nexcept Exception as error:\n"
        "    caught = [type(error).__name__, [type(arg).__name__ for arg in error.args]]\n"
        "caught"
    )
    result = _run_tool(_tool(broken), code)

    assert result.text.splitlines()[0] == '["LookupError", ["str"]]'


# --- Nested calls through the executor --------------------------------------------


def _executor(tools: _Tools, approval: ToolApprovalPolicy) -> ConfiguredToolExecutor:
    return ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path(".")),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=approval,
    )


def _nested(executor: ConfiguredToolExecutor, *calls: tuple[str, dict[str, object]]):  # noqa: ANN202
    async def run() -> list[NestedCallResult]:
        results: list[NestedCallResult] = [NestedCallResult("")] * len(calls)

        async def one(index: int, name: str, arguments: dict[str, object]) -> None:
            call = ToolCall(call_id=f"nested-{index}", name=name, arguments=arguments)
            results[index] = await executor.run_nested(call)

        async with anyio.create_task_group() as group:
            for index, (name, arguments) in enumerate(calls):
                group.start_soon(one, index, name, arguments)
        return results

    return anyio.run(run)


@pytest.mark.parametrize(("tool_name", "concurrent"), [("mcp__db__query", 1), ("lookup", 2)])
def test_nested_scheduling(tool_name: str, concurrent: int) -> None:
    tools = _Tools()
    tool = tools.query if tool_name == "mcp__db__query" else tools.lookup
    tool.delay = 0.05

    _nested(
        _executor(tools, ToolApprovalPolicy.require_approval()), *[(tool_name, {"table": "t"})] * 2
    )

    # A tool that is not parallel-safe waits for the other, as in a batch.
    assert tool.most_running == concurrent


@pytest.mark.parametrize(
    ("name", "approval", "message"),
    [
        # Read-only is checked on its own, so even approving everything grants no more.
        ("mcp__db__drop", ToolApprovalPolicy.approve_all(), "not read-only"),
        (CODEMODE, ToolApprovalPolicy.approve_all(), "cannot run a script"),
        ("missing", ToolApprovalPolicy.approve_all(), "missing"),
    ],
    ids=["not-read-only", "recursion", "unknown"],
)
def test_nested_refusals(name: str, approval: ToolApprovalPolicy, message: str) -> None:
    tools = _Tools()

    (result,) = _nested(_executor(tools, approval), (name, {"table": "t"}))

    assert result.is_error and message in result.text
    assert tools.drop.calls == []


def test_confirmed_tool_never_runs() -> None:
    """A tool that needs confirmation every time cannot be granted to a script."""

    tools = _Tools()
    tools.query.confirm_every_call = True

    (result,) = _nested(
        _executor(tools, ToolApprovalPolicy.approve_all()), ("mcp__db__query", {"table": "t"})
    )

    assert result.is_error and "needs approval" in result.text
    assert tools.query.calls == []


def test_proxy_cannot_reach_command_tool() -> None:
    """`mcp_call` naming a command tool is refused, as the tool that runs is checked."""

    tools = _Tools()
    executor = ConfiguredToolExecutor(
        registry=_proxied_registry(tools),
        context=ToolContext(cwd=Path(".")),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        mcp_catalog=McpCatalog.from_tools([tools.query, tools.drop], connected=lambda _: True),
    )

    (result,) = _nested(
        executor, (MCP_CALL, {"name": "mcp__db__drop", "arguments": {"table": "users"}})
    )

    assert result.is_error and "not read-only" in result.text
    assert tools.drop.calls == []


def _proxied_registry(tools: _Tools) -> ToolRegistry:
    registry = tools.registry()
    for tool in discovery_tools(
        McpCatalog.from_tools([tools.query, tools.drop], connected=lambda _: True)
    ):
        registry.register(tool, execution=ToolExecutionMetadata(parallel_safe=False))
    return registry


def test_policy_limits_scripts() -> None:
    """A tool the run's policy leaves out is refused, as a direct call would be."""

    tools = _Tools()
    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path(".")),
        policy=ToolPolicy.allow_tool_names({"lookup"}),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )

    blocked, allowed = _nested(
        executor, ("mcp__db__query", {"table": "t"}), ("lookup", {"table": "t"})
    )

    assert blocked.is_error and tools.query.calls == []
    assert (allowed.is_error, allowed.text) == (False, "lookup:t")


@pytest.mark.parametrize(
    ("ending", "fan_out"),
    [
        ("timeout", "import asyncio\nawait asyncio.gather"),
        ("caller-cancels", "import asyncio\nawait asyncio.gather"),
        ("timeout", "await gather_settled"),
        ("caller-cancels", "await gather_settled"),
    ],
    ids=["timeout", "caller-cancels", "timeout-settled", "caller-cancels-settled"],
)
def test_script_end_cancels_calls(ending: str, fan_out: str) -> None:
    """Calls still running when a script ends are cancelled, not left running."""

    finished: list[str] = []
    cancelled: list[str] = []

    async def slow(name: str, arguments: object) -> NestedCallResult:
        try:
            await anyio.sleep(2)
        except anyio.get_cancelled_exc_class():
            cancelled.append(name)
            raise
        finished.append(name)
        return NestedCallResult(text="late")

    code = f"{fan_out}(*[query(table='a') for _ in range(4)])"

    async def run() -> None:
        if ending == "timeout":
            tool = _tool(slow, limits=ScriptLimits(wall_seconds=0.3))
            with pytest.raises(ToolError, match=r"Script failed \(timeout\)"):
                await tool.run({"code": code}, ToolContext(cwd=Path(".")))
        else:
            with anyio.move_on_after(0.3):
                await _tool(slow).run({"code": code}, ToolContext(cwd=Path(".")))
        # Long enough for any call left running to have finished.
        await anyio.sleep(2.5)

    anyio.run(run)

    assert (finished, len(cancelled)) == ([], 4)


def test_unscriptable_names_left_out() -> None:
    """A tool whose name is not a Python identifier stays reachable only through mcp_call."""

    tool = CodemodeTool(
        [_Tool("lookup")],
        _echo,
        discoverable=[_McpTool("mcp__docs-site__search"), _McpTool("mcp__db__query")],
    )

    assert tool.tool_names == ("lookup", "mcp__db__query")
    assert "mcp_call(name=..., arguments=...)" in tool.description


async def _echo(name: str, arguments: object) -> NestedCallResult:
    return NestedCallResult(text=f"{name}:{arguments}")


# --- What a script's calls record ---------------------------------------------------


def _ended(events: Sequence[WispEvent]) -> ToolExecutionEnded:
    (ended,) = [
        event
        for event in events
        if isinstance(event, ToolExecutionEnded) and event.name == CODEMODE
    ]
    return ended


_AUDITED = (
    "import asyncio\n"
    "await asyncio.gather(mcp__db__query(table='a'), mcp__db__query(table='b'))\n"
    "try:\n    await mcp_call(name='mcp__db__drop', arguments={'table': 'c'})\n"
    "except RuntimeError:\n    pass"
)


def test_script_calls_recorded(tmp_path: Path) -> None:
    """Every call a script made is on its result and in the session, refusals too."""

    tools = _Tools()

    events, provider = _run(tmp_path, tools, [_script(_AUDITED)], exposure="on_demand")

    calls = _ended(events).nested_calls
    assert [(c.name, c.target, c.arguments, c.is_error, c.failure_code) for c in calls] == [
        ("mcp__db__query", None, {"table": "a"}, False, None),
        ("mcp__db__query", None, {"table": "b"}, False, None),
        (
            "mcp_call",
            "mcp__db__drop",
            {"name": "mcp__db__drop", "arguments": {"table": "c"}},
            True,
            "policy_denied",
        ),
    ]
    assert calls[2].error is not None and "not read-only" in calls[2].error
    assert _result(events).nested_calls == calls
    saved = [
        entry.tool_result.nested_calls
        for path in (tmp_path / "sessions").rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if isinstance(entry := session_entry_from_json(line), MessageSessionEntry)
        and entry.tool_result is not None
        and entry.tool_result.nested_calls
    ]
    assert saved == [calls]
    # The model reads only what the script returned, never the records.
    assert "policy_denied" not in repr(provider.calls[-1])


def test_live_events_bracket_calls(tmp_path: Path) -> None:
    """Each call starts and ends live, inside its script's own start and end."""

    timeline: list[WispEvent] = []

    events, _ = _run(
        tmp_path, _Tools(), [_script(_AUDITED)], exposure="on_demand", timeline=timeline
    )

    position = {id(event): index for index, event in enumerate(timeline)}
    (script_start,) = [
        position[id(e)]
        for e in timeline
        if isinstance(e, ToolExecutionStarted) and e.name == CODEMODE
    ]
    script_end = position[id(_ended(events))]
    started = [e for e in timeline if isinstance(e, ToolNestedCallStarted)]
    ended = [e for e in timeline if isinstance(e, ToolNestedCallEnded)]
    assert (len(started), len(ended)) == (3, 3)
    assert {e.parent_call_id for e in [*started, *ended]} == {"call-script"}
    by_id = {e.call.call_id: e.call for e in ended}
    assert [by_id[call.call_id] for call in _ended(events).nested_calls] == list(
        _ended(events).nested_calls
    )
    for start in started:
        (end,) = [e for e in ended if e.call.call_id == start.call_id]
        assert script_start < position[id(start)] < position[id(end)] < script_end


def _script_executor(
    tools: _Tools,
    *,
    limits: ScriptLimits | None = None,
    live: list[WispEvent] | None = None,
    sink: bool = True,
    failing_sink: bool = False,
) -> ConfiguredToolExecutor:
    """An executor offering codemode whose scripts call back into it, as a session's do."""

    registry = tools.registry()
    executors: list[ConfiguredToolExecutor] = []
    ids = itertools.count(1)

    async def call(name: str, arguments: Mapping[str, object]) -> NestedCallResult:
        nested = ToolCall(call_id=f"nested-{next(ids)}", name=name, arguments=dict(arguments))
        return await executors[0].run_nested(nested)

    async def publish(event: WispEvent) -> None:
        if failing_sink:
            raise RuntimeError("frontend went away")
        if live is not None:
            live.append(event)

    codemode = CodemodeTool([tools.query, tools.lookup], call, limits=limits)
    registry.register(codemode, execution=ToolExecutionMetadata(parallel_safe=False))
    executors.append(
        ConfiguredToolExecutor(
            registry=registry,
            context=ToolContext(cwd=Path(".")),
            policy=ToolPolicy.allow_all_tools(),
            approval_policy=ToolApprovalPolicy.require_approval(),
            progress_sink=publish if sink else None,
        )
    )
    return executors[0]


async def _execute(executor: ConfiguredToolExecutor, call_id: str, code: str) -> ToolExecutionEnded:
    call = ToolCall(call_id=call_id, name=CODEMODE, arguments={"code": code})
    (ended,) = [e async for e in executor.execute(call) if isinstance(e, ToolExecutionEnded)]
    return ended


def test_concurrent_scripts_attributed() -> None:
    """Two scripts running at once each record only their own calls."""

    tools = _Tools(lookup=_Tool("lookup", delay=0.05))
    live: list[WispEvent] = []
    executor = _script_executor(tools, live=live)
    gather = "import asyncio\nawait asyncio.gather(*[lookup(table=t) for t in {}])"
    ended: dict[str, ToolExecutionEnded] = {}

    async def run() -> None:
        async def one(call_id: str, tables: list[str]) -> None:
            ended[call_id] = await _execute(executor, call_id, gather.format(tables))

        async with anyio.create_task_group() as group:
            group.start_soon(one, "left", ["a", "b", "c"])
            group.start_soon(one, "right", ["x", "y"])

    anyio.run(run)

    tables = {
        call_id: [call.arguments["table"] for call in event.nested_calls if call.arguments]
        for call_id, event in ended.items()
    }
    assert tables == {"left": ["a", "b", "c"], "right": ["x", "y"]}
    for event in live:
        assert isinstance(event, ToolNestedCallStarted | ToolNestedCallEnded)
        mine = {c.call_id for c in ended[event.parent_call_id].nested_calls}
        assert (
            event.call_id if isinstance(event, ToolNestedCallStarted) else event.call.call_id
        ) in mine


@pytest.mark.parametrize("sink", [True, False], ids=["live", "no-sink"])
def test_cancelled_calls_recorded(sink: bool) -> None:
    """Calls cut off by a script's limit are recorded as unfinished and never end live."""

    tools = _Tools(lookup=_Tool("lookup", delay=5))
    live: list[WispEvent] = []
    executor = _script_executor(tools, limits=ScriptLimits(wall_seconds=1), live=live, sink=sink)
    code = "import asyncio\nawait asyncio.gather(lookup(table='a'), lookup(table='b'))"

    async def run() -> ToolExecutionEnded:
        ended = await _execute(executor, "slow", code)
        await anyio.sleep(0.2)  # Lets the cancelled calls run their handlers.
        return ended

    ended = anyio.run(run)

    assert ended.is_error and "Script failed (timeout)" in ended.output
    assert [(c.is_error, c.error) for c in ended.nested_calls] == [
        (True, "The script ended before this call finished")
    ] * 2
    assert [type(e) for e in live] == ([ToolNestedCallStarted] * 2 if sink else [])


def test_slow_cancel_publishes_nothing_late() -> None:
    """A tool that finishes after its script ended neither changes the record nor publishes."""

    @dataclass
    class _Stubborn(_Tool):
        async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.6)
            return ToolResult(text="late")

    live: list[WispEvent] = []
    executor = _script_executor(
        _Tools(lookup=_Stubborn("lookup")), limits=ScriptLimits(wall_seconds=0.3), live=live
    )

    async def run() -> ToolExecutionEnded:
        ended = await _execute(executor, "stubborn", "await lookup(table='a')")
        await anyio.sleep(1)  # Past the tool's own end.
        return ended

    ended = anyio.run(run)

    (call,) = ended.nested_calls
    assert call.error == "The script ended before this call finished"
    assert [type(e) for e in live] == [ToolNestedCallStarted]


def test_failing_sink_ignored() -> None:
    """A frontend that fails to take an event changes nothing the script sees."""

    executor = _script_executor(_Tools(), failing_sink=True)

    ended = anyio.run(_execute, executor, "plain", "await lookup(table='a')")

    assert not ended.is_error and ended.output.startswith("lookup:a")
    assert [(c.name, c.is_error) for c in ended.nested_calls] == [("lookup", False)]


def test_records_stay_bounded() -> None:
    """Huge arguments are dropped from a record, and calls past the cap only counted."""

    executor = _script_executor(_Tools(), limits=ScriptLimits(max_calls=300))
    code = "await lookup(table='x' * 5000)\nfor i in range(209):\n    await lookup(table='a')"

    ended = anyio.run(_execute, executor, "many", code)

    assert len(ended.nested_calls) == 200 and ended.nested_calls_omitted == 10
    assert ended.nested_calls[0].arguments is None
    assert ended.nested_calls[1].arguments == {"table": "a"}


_ORDER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "lines": {
            "type": "array",
            "items": {"type": "object", "properties": {"sku": {"type": "string"}}},
        },
        "note": {"type": ["string", "null"]},
    },
    "required": ["id", "lines"],
}


def _structured_tools(structured: object) -> _Tools:
    return _Tools(
        query=_McpTool(
            "mcp__db__query", reply="order 7", output_schema=_ORDER_SCHEMA, structured=structured
        )
    )


def test_script_reads_structured_fields(tmp_path: Path) -> None:
    """A tool with an output schema returns its structured result to a script, typed."""

    tools = _structured_tools({"id": 7, "lines": [{"sku": "a-1"}]})
    code = (
        "order = await mcp__db__query(table='orders')\n(order['id'] + 1, order['lines'][0]['sku'])"
    )

    events, provider = _run(tmp_path, tools, [_script(code)])

    assert _result(events).output.startswith('[8, "a-1"]')
    description = next(t for t in provider.calls[0].tools if t.name == CODEMODE).description
    assert "- mcp__db__query: a dict: id, lines, note" in description


@pytest.mark.parametrize(
    "code",
    [
        "(await mcp__db__query(table='o'))['total']",
        "(await mcp__db__query(table='o'))['lines'][0]['qty']",
    ],
    ids=["top-level", "nested"],
)
def test_unknown_field_fails_type_check(tmp_path: Path, code: str) -> None:
    tools = _structured_tools({"id": 7, "lines": []})

    events, _ = _run(tmp_path, tools, [_script(code)])

    result = _result(events)
    assert result.is_error and "Script failed (type)" in result.output
    assert "invalid-key" in result.output
    assert tools.query.calls == []


def test_direct_call_still_reads_text(tmp_path: Path) -> None:
    """The model's own call gets the text; the structured result reaches no event."""

    tools = _structured_tools({"id": 7, "lines": [], "note": "only-in-structured"})
    call = ToolCall(call_id="call-1", name="mcp__db__query", arguments={"table": "o"})

    events, provider = _run(tmp_path, tools, [tool_call_stream(call)], codemode=False)

    assert _result(events).output == "order 7"
    assert not any("only-in-structured" in event.model_dump_json() for event in events)
    # Nor the model's next request, which carries the call's result.
    assert "only-in-structured" not in repr(provider.calls[-1])


@pytest.mark.parametrize(
    ("structured", "problem"),
    [
        (None, "it returned no structured result"),
        (["not", "a", "dict"], "it returned no structured result"),
        ({"blob": "x" * (600 * 1024)}, "its structured result is larger than 512 KiB"),
        ({"bad": float("nan")}, "its structured result is not JSON"),
        ({"bad": "\ud800"}, "its structured result is not JSON"),
    ],
    ids=["missing", "not-a-dict", "too-large", "not-json", "lone-surrogate"],
)
def test_unusable_structured_result_fails_the_call(
    tmp_path: Path, structured: object, problem: str
) -> None:
    """The stub promises a value, so a call without a usable one fails, never text."""

    tools = _structured_tools(structured)
    code = "(outcome,) = await gather_settled(mcp__db__query(table='o'))\nstr(outcome)"

    events, _ = _run(tmp_path, tools, [_script(code)])

    result = _result(events)
    assert not result.is_error
    assert f"mcp__db__query failed: {problem}" in result.output
    assert _ended(events).nested_calls[0].is_error is False
    assert "1 failed" in result.output


def test_wrapped_result_is_unwrapped(tmp_path: Path) -> None:
    """A schema that only wraps `result`, as the MCP Python SDK writes, returns its value."""

    schema = {
        "type": "object",
        "properties": {"result": {"type": "array", "items": {"type": "integer"}}},
        "required": ["result"],
    }
    tools = _Tools(
        query=_McpTool("mcp__db__query", output_schema=schema, structured={"result": [1, 2]})
    )

    events, _ = _run(tmp_path, tools, [_script("sum(await mcp__db__query(table='o'))")])

    assert _result(events).output.startswith("3")


def test_wrapped_string_stays_text(tmp_path: Path) -> None:
    """The SDK gives every tool returning a string this schema; such tools keep text."""

    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    tools = _Tools(
        query=_McpTool(
            "mcp__db__query", reply="rows", output_schema=schema, structured={"result": "rows"}
        )
    )

    events, provider = _run(tmp_path, tools, [_script("(await mcp__db__query(table='o')).upper()")])

    assert _result(events).output.startswith("ROWS\n")
    description = next(t for t in provider.calls[0].tools if t.name == CODEMODE).description
    assert "structured result instead of text" not in description


@pytest.mark.parametrize(
    ("limits", "value"),
    [(None, None), (SCRIPT_RESULT_LIMITS, {"id": 7})],
    ids=["subagent", "script"],
)
def test_structured_result_only_for_scripts(limits: ResultLimits | None, value: object) -> None:
    """A subagent's calls go to a model, which reads text; only a script's carry the value."""

    tools = _Tools(lookup=_Tool("lookup", structured={"id": 7}))
    executor = _executor(tools, ToolApprovalPolicy.approve_all())

    async def run() -> NestedCallResult:
        call = ToolCall(call_id="nested-1", name="lookup", arguments={"table": "a"})
        return await executor.run_nested(call, result_limits=limits)

    assert anyio.run(run).value == value


def test_hostile_output_schema_leaves_other_tools(tmp_path: Path) -> None:
    """Keys that look like code, deep nesting and many fields still give valid stubs."""

    deep: dict[str, object] = {"type": "object", "properties": {}}
    node = deep
    for _ in range(40):
        child: dict[str, object] = {"type": "object", "properties": {}}
        node["properties"] = {"n": child}
        node = child
    schema = {
        "type": "object",
        "properties": {
            "a'] = 1\nimport os\n#": {"type": "string"},
            "deep": deep,
            **{
                f"f{index}": {"type": "object", "properties": {"x": {"type": "integer"}}}
                for index in range(100)
            },
        },
    }
    tools = _Tools(query=_McpTool("mcp__db__query", output_schema=schema, structured={}))

    events, _ = _run(tmp_path, tools, [_script("await lookup(table='a')")])

    assert _result(events).output.startswith("lookup:a\n")


def test_found_on_demand_returns_structured(tmp_path: Path) -> None:
    """A catalog tool called by name, found on demand, returns its typed value too."""

    tools = _structured_tools({"id": 7, "lines": []})

    events, _ = _run(
        tmp_path,
        tools,
        [_script("(await mcp__db__query(table='o'))['id']")],
        exposure="on_demand",
    )

    assert _result(events).output.startswith("7")


def test_unwrapped_without_result_fails(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"result": {"type": "integer"}},
        "required": ["result"],
    }
    tools = _Tools(query=_McpTool("mcp__db__query", output_schema=schema, structured={"x": 1}))
    code = "(outcome,) = await gather_settled(mcp__db__query(table='o'))\nstr(outcome)"

    events, _ = _run(tmp_path, tools, [_script(code)])

    assert "its structured result has no `result` field" in _result(events).output
