"""On-demand MCP exposure: the proxy reaches nothing a direct call could not.

These tests drive a real `CodingSession` with fake MCP tools and a scripted model.
Each one pins a way `mcp_call` could otherwise reach a tool or skip an approval
that direct mode would not allow.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from wisp.coding.session import CodingSession, _HarnessLoopState
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentMode,
    AgentStarted,
    ToolApprovalRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    WispEvent,
)
from wisp.mcp.discovery import MCP_CALL, MCP_DESCRIBE, MCP_SEARCH, McpCatalog
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
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult

_SCHEMA: ToolInputSchema = {
    "type": "object",
    "properties": {"table": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["table"],
}


@dataclass
class _FakeMcpTool:
    name: str
    description: str
    safety: ToolSafety
    server_name: str = "db"
    input_schema: ToolInputSchema = field(default_factory=lambda: dict(_SCHEMA))
    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def remote_name(self) -> str:
        return self.name.rsplit("__", 1)[-1]

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.calls.append(dict(arguments))
        return ToolResult(text=f"ran {self.name}")


@dataclass
class _Builtin:
    name: str = "bash"
    description: str = "Run a command."
    input_schema: ToolInputSchema = field(default_factory=lambda: {"type": "object"})
    safety: ToolSafety = "command"
    calls: int = 0

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(text="ran bash")


@dataclass
class _Tools:
    query: _FakeMcpTool = field(
        default_factory=lambda: _FakeMcpTool(
            "mcp__db__query", "MCP server db: Query a table.", "read"
        )
    )
    drop: _FakeMcpTool = field(
        default_factory=lambda: _FakeMcpTool(
            "mcp__db__drop_table", "MCP server db: Drop a table.", "command"
        )
    )
    bash: _Builtin = field(default_factory=_Builtin)

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in (self.bash, self.query, self.drop):
            registry.register(tool)
        return registry


def _call(tool: str, **arguments: object) -> list[ProviderEvent]:
    return tool_call_stream(ToolCall(call_id=f"call-{tool}", name=tool, arguments=arguments))


def _done() -> list[ProviderEvent]:
    return [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]


def _run(
    tmp_path: Path,
    tools: _Tools,
    streams: Sequence[list[ProviderEvent]],
    *,
    approval: ToolApprovalPolicy | None = None,
    mode: AgentMode = "build",
    exposure: str = "on_demand",
    policy: ToolPolicy | None = None,
    explicit_tools: bool = False,
    operation_tool_names: frozenset[str] | None = None,
) -> tuple[list[WispEvent], ScriptedProvider]:
    provider = ScriptedProvider([*streams, _done()])
    registry = tools.registry()
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        # Explicit specs are not filtered by the session policy; the executor is.
        tools=registry.specs() if explicit_tools else None,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=policy or ToolPolicy.allow_all_tools(),
        tool_approval_policy=approval or ToolApprovalPolicy.require_approval(),
        mcp_tool_exposure=exposure,  # type: ignore[arg-type]
        mode=mode,
    )

    async def collect() -> list[WispEvent]:
        return [
            event async for event in session.run("go", operation_tool_names=operation_tool_names)
        ]

    return anyio.run(collect), provider


def _results(events: Sequence[WispEvent]) -> list[ToolResultReady]:
    return [event for event in events if isinstance(event, ToolResultReady)]


def test_offers_discovery_instead_of_mcp_tools(tmp_path: Path) -> None:
    events, provider = _run(tmp_path, _Tools(), [])

    offered = [tool.name for tool in provider.calls[0].tools]
    assert offered == ["bash", MCP_SEARCH, MCP_DESCRIBE, MCP_CALL]
    (started,) = [event for event in events if isinstance(event, AgentStarted)]
    assert started.setup is not None and list(started.setup.tool_names) == offered
    system = "\n".join(message.content for message in provider.calls[0].messages)
    assert "Drop a table" not in system and "db (2 tools)" in system


def test_direct_mode_unchanged(tmp_path: Path) -> None:
    events, provider = _run(
        tmp_path, _Tools(), [_call("mcp__db__query", table="t")], exposure="direct"
    )

    assert [tool.name for tool in provider.calls[0].tools] == [
        "bash",
        "mcp__db__query",
        "mcp__db__drop_table",
    ]
    (result,) = _results(events)
    assert (result.name, result.target, result.is_error) == ("mcp__db__query", None, False)


def test_proxied_call_runs_target(tmp_path: Path) -> None:
    tools = _Tools()

    events, _ = _run(
        tmp_path,
        tools,
        [_call(MCP_CALL, name="mcp__db__query", arguments={"table": "users", "limit": 2})],
    )

    (started,) = [event for event in events if isinstance(event, ToolExecutionStarted)]
    (result,) = _results(events)
    assert (started.name, started.target) == (MCP_CALL, "mcp__db__query")
    assert (result.name, result.target, result.output) == (
        MCP_CALL,
        "mcp__db__query",
        "ran mcp__db__query",
    )
    assert tools.query.calls == [{"table": "users", "limit": 2}]


@pytest.mark.parametrize(
    ("name", "failure_code"),
    [
        ("bash", "policy_denied"),
        (MCP_CALL, "policy_denied"),
        (MCP_SEARCH, "policy_denied"),
        ("mcp__db__missing", "unknown_tool"),
    ],
    ids=["builtin", "recursion", "discovery-tool", "unknown"],
)
def test_proxy_refuses_non_catalog_targets(tmp_path: Path, name: str, failure_code: str) -> None:
    tools = _Tools()

    events, _ = _run(tmp_path, tools, [_call(MCP_CALL, name=name, arguments={})])

    (result,) = _results(events)
    assert (result.is_error, result.failure_code, result.target) == (True, failure_code, None)
    assert tools.bash.calls == 0
    assert not [event for event in events if isinstance(event, ToolApprovalRequested)]


def test_plan_mode_hides_command_targets(tmp_path: Path) -> None:
    tools = _Tools()
    streams = [
        _call(MCP_SEARCH, query="drop table"),
        _call(MCP_CALL, name="mcp__db__drop_table", arguments={"table": "users"}),
    ]

    events, provider = _run(tmp_path, tools, streams, mode="plan")

    search, refused = _results(events)
    assert "mcp__db__drop_table" not in search.output
    assert (refused.failure_code, tools.drop.calls) == ("policy_denied", [])
    assert MCP_CALL in [tool.name for tool in provider.calls[0].tools]


def test_command_target_asks_approval(tmp_path: Path) -> None:
    tools = _Tools()

    events, _ = _run(
        tmp_path,
        tools,
        [_call(MCP_CALL, name="mcp__db__drop_table", arguments={"table": "users"})],
    )

    (request,) = [event for event in events if isinstance(event, ToolApprovalRequested)]
    assert (request.name, request.target, request.safety) == (
        MCP_CALL,
        "mcp__db__drop_table",
        "command",
    )
    assert request.arguments == {"name": "mcp__db__drop_table", "arguments": {"table": "users"}}
    (result,) = _results(events)
    assert (result.failure_code, tools.drop.calls) == ("approval_denied", [])


@pytest.mark.parametrize(
    ("granted", "ran"), [("mcp__db__drop_table", True), (MCP_CALL, False)], ids=["target", "proxy"]
)
def test_grants_follow_target(tmp_path: Path, granted: str, ran: bool) -> None:
    tools = _Tools()

    _run(
        tmp_path,
        tools,
        [_call(MCP_CALL, name="mcp__db__drop_table", arguments={"table": "users"})],
        approval=ToolApprovalPolicy.approve_tool_names({granted}),
    )

    assert bool(tools.drop.calls) is ran


@pytest.mark.parametrize("arguments", [{}, "users"], ids=["missing", "not-object"])
def test_bad_arguments_fail_before_approval(tmp_path: Path, arguments: object) -> None:
    tools = _Tools()

    events, _ = _run(
        tmp_path, tools, [_call(MCP_CALL, name="mcp__db__drop_table", arguments=arguments)]
    )

    (result,) = _results(events)
    assert (result.failure_code, result.retryable) == ("invalid_arguments", True)
    assert not [event for event in events if isinstance(event, ToolApprovalRequested)]
    assert tools.drop.calls == []


def test_existing_discovery_name_keeps_direct(tmp_path: Path) -> None:
    tools = _Tools()
    tools.bash.name = MCP_SEARCH

    _events, provider = _run(tmp_path, tools, [])

    assert [tool.name for tool in provider.calls[0].tools] == [
        MCP_SEARCH,
        "mcp__db__query",
        "mcp__db__drop_table",
    ]


@pytest.mark.parametrize("parallel_safe", [True, False])
def test_scheduling_follows_target(parallel_safe: bool, tmp_path: Path) -> None:
    tools = _Tools()
    registry = tools.registry()
    registry.register(tools.query, execution=ToolExecutionMetadata(parallel_safe=parallel_safe))
    registry.register(tools.bash, execution=ToolExecutionMetadata(parallel_safe=not parallel_safe))
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        mcp_catalog=McpCatalog.from_tools([tools.query]),
    )
    call = ToolCall(
        call_id="c",
        name=MCP_CALL,
        arguments={"name": "mcp__db__query", "arguments": {"table": "t"}},
    )

    async def prepared() -> bool:
        async for event in executor.prepare(call):
            if hasattr(event, "parallel_safe"):
                return bool(event.parallel_safe)
        raise AssertionError("no prepared execution")

    assert anyio.run(prepared) is parallel_safe


@pytest.mark.parametrize(
    ("name", "target", "unsafe"),
    [
        (MCP_CALL, "mcp__db__drop_table", True),
        (MCP_CALL, "mcp__db__query", False),
        (MCP_CALL, None, False),
        (MCP_SEARCH, None, False),
        (MCP_DESCRIBE, None, False),
    ],
    ids=["command-target", "read-target", "unresolved", "search", "describe"],
)
def test_overflow_retry_judges_target(
    tmp_path: Path, name: str, target: str | None, unsafe: bool
) -> None:

    tools = _Tools()
    session = CodingSession(
        provider=ScriptedProvider([]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
    )
    marked: list[bool] = []

    class _Compaction:
        recovered_from_overflow = False

        def unsafe_tool_ran(self) -> None:
            marked.append(True)

    state = _HarnessLoopState()
    for event in (
        ToolExecutionStarted(call_id="c", name=name, target=target, arguments={}),
        ToolExecutionEnded(call_id="c", name=name, target=target, output="", is_error=False),
    ):
        state.observe(
            event,
            compaction=_Compaction(),  # type: ignore[arg-type]
            tool_is_unsafe=session._tool_is_unsafe,  # noqa: SLF001
        )

    assert bool(marked) is unsafe


def test_policy_still_blocks_explicit_builtins(tmp_path: Path) -> None:
    tools = _Tools()

    events, _ = _run(
        tmp_path,
        tools,
        [_call("bash", command="rm -rf /")],
        policy=ToolPolicy.allow_read_tools(),
        explicit_tools=True,
    )

    (result,) = _results(events)
    assert (result.failure_code, tools.bash.calls) == ("policy_denied", 0)


def test_tool_selection_limits_catalog(tmp_path: Path) -> None:
    tools = _Tools()
    streams = [
        _call(MCP_SEARCH, query="table"),
        _call(MCP_CALL, name="mcp__db__drop_table", arguments={"table": "users"}),
    ]

    events, _ = _run(
        tmp_path,
        tools,
        streams,
        policy=ToolPolicy.allow_tool_names({"bash", "mcp__db__query"}),
        approval=ToolApprovalPolicy.approve_all(),
    )

    search, refused = _results(events)
    assert "mcp__db__query" in search.output and "drop_table" not in search.output
    assert (refused.failure_code, tools.drop.calls) == ("policy_denied", [])


def test_operation_limit_leaves_out_discovery(tmp_path: Path) -> None:
    _events, provider = _run(tmp_path, _Tools(), [], operation_tool_names=frozenset({"bash"}))

    assert [tool.name for tool in provider.calls[0].tools] == ["bash"]


@pytest.mark.parametrize(
    ("tool", "ran"), [("mcp__db__query", True), ("mcp__db__drop_table", False)]
)
def test_direct_calls_keep_gates(tmp_path: Path, tool: str, ran: bool) -> None:
    tools = _Tools()
    target = tools.query if ran else tools.drop

    events, _ = _run(tmp_path, tools, [_call(tool, table="users")])

    (result,) = _results(events)
    assert bool(target.calls) is ran and result.target is None
    assert result.failure_code == (None if ran else "approval_denied")


def test_older_draft_types_do_not_crash(tmp_path: Path) -> None:
    tools = _Tools()
    tools.query.input_schema = {
        "$schema": "http://json-schema.org/draft-03/schema#",
        "properties": {"table": {"type": [{"type": "string"}, "string"]}},
        "required": [["table"], "table"],
    }

    events, _ = _run(
        tmp_path, tools, [_call(MCP_CALL, name="mcp__db__query", arguments={"table": "t"})]
    )

    (result,) = _results(events)
    assert (result.is_error, tools.query.calls) == (False, [{"table": "t"}])


def test_registered_tool_named_like_discovery_keeps_its_safety(tmp_path: Path) -> None:
    tools = _Tools()
    tools.bash.name = MCP_CALL  # a user's own command tool, so the run stays direct
    session = CodingSession(
        provider=ScriptedProvider([]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
    )

    assert session._tool_is_unsafe(MCP_CALL) is True  # noqa: SLF001
    assert session._tool_is_unsafe(MCP_SEARCH) is False  # noqa: SLF001
