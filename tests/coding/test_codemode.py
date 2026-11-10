"""The experimental codemode tool: scripts reach only what a direct call could.

These drive a real `CodingSession` with fake tools and a scripted model, and the
executor's nested calls directly, with scripts running in monty.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import AgentMode, ToolExecutionEnded, ToolResultReady, WispEvent
from wisp.mcp.discovery import MCP_CALL, McpCatalog, discovery_tools
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tool_types import ToolSafety
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolArguments, ToolExecutionMetadata, ToolInputSchema
from wisp.tools.codemode.calls import CODEMODE, NestedCallResult
from wisp.tools.codemode.sandbox import ScriptLimits
from wisp.tools.codemode.tool import CodemodeTool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult

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
    confirm_every_call: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)
    running: int = 0
    most_running: int = 0
    server_name: str = "db"

    @property
    def remote_name(self) -> str:
        return self.name.rsplit("__", 1)[-1]

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.calls.append(dict(arguments))
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            await anyio.sleep(self.delay)
        finally:
            self.running -= 1
        return ToolResult(text=f"{self.name}:{arguments.get('table')}")


@dataclass
class _Tools:
    query: _Tool = field(default_factory=lambda: _Tool("mcp__db__query"))
    drop: _Tool = field(default_factory=lambda: _Tool("mcp__db__drop", safety="command"))
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
    codemode: bool = True,
    mode: AgentMode = "build",
) -> tuple[list[WispEvent], ScriptedProvider]:
    provider = ScriptedProvider([*streams, _done()])
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.require_approval(),
        mcp_tool_exposure="direct",
        codemode=codemode,
        mode=mode,
    )

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

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


def test_not_offered_without_monty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: object) -> object:
        return None if name == "pydantic_monty" else real_find_spec(name, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    _, provider = _run(tmp_path, _Tools(), [])

    assert CODEMODE not in [tool.name for tool in provider.calls[0].tools]


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


@pytest.mark.parametrize(
    ("code", "limits", "failure"),
    [
        ("while True:\n    pass", ScriptLimits(compute_seconds=0.2), "runtime"),
        ("for i in range(5):\n    await query(table='a')", ScriptLimits(max_calls=3), "runtime"),
        ("await query(table='a')", ScriptLimits(wall_seconds=0.2), "timeout"),
        # Type-checked first, so a positional call or bad syntax never runs.
        ("await query('a')", None, "type"),
        ("x = (", None, "type"),
    ],
    ids=["compute", "calls", "wall-clock", "positional", "syntax"],
)
def test_limits_end_script(code: str, limits: ScriptLimits | None, failure: str) -> None:
    async def slow(name: str, arguments: object) -> NestedCallResult:
        await anyio.sleep(1)
        return NestedCallResult(text="late")

    caller = slow if failure == "timeout" else None
    with pytest.raises(ToolError, match=rf"Script failed \({failure}\)"):
        _run_tool(_tool(caller, limits=limits), code)


def test_no_sandbox_escape() -> None:
    for code in ("open('/etc/passwd').read()", "import socket", "import os\nos.environ['HOME']"):
        with pytest.raises(ToolError, match=r"Script failed"):
            _run_tool(_tool(), code)


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
        (CODEMODE, ToolApprovalPolicy.approve_all(), "cannot run another script"),
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


def test_script_end_cancels_calls() -> None:
    """Calls still running when a script times out are cancelled, not left running."""

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

    code = "import asyncio\nawait asyncio.gather(*[query(table='a') for _ in range(4)])"

    async def run() -> None:
        tool = _tool(slow, limits=ScriptLimits(wall_seconds=0.3))
        with pytest.raises(ToolError, match=r"Script failed \(timeout\)"):
            await tool.run({"code": code}, ToolContext(cwd=Path(".")))
        # Long enough for any call left running to have finished.
        await anyio.sleep(2.5)

    anyio.run(run)

    assert (finished, len(cancelled)) == ([], 4)
