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
    reply: str | None = None
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
        return ToolResult(text=self.reply or f"{self.name}:{arguments.get('table')}")


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
    codemode: bool = True,
    mode: AgentMode = "build",
    exposure: McpToolExposure = "direct",
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


def test_huge_tool_output_bounded(tmp_path: Path) -> None:
    """A script reads a tool's result as the model would: cut to the output limit."""

    tools = _Tools(lookup=_Tool("lookup", reply="r" * 5_000_000))

    events, _ = _run(tmp_path, tools, [_script("len(await lookup(table='a'))")])

    seen = int(_result(events).output.splitlines()[0])
    assert 0 < seen <= ToolContext(cwd=tmp_path).max_output_bytes


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


@pytest.mark.parametrize("ending", ["timeout", "caller-cancels"])
def test_script_end_cancels_calls(ending: str) -> None:
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

    code = "import asyncio\nawait asyncio.gather(*[query(table='a') for _ in range(4)])"

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
