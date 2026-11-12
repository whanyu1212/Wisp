"""The `spawn_agent` tool: a subagent reads on the run's behalf and only reports back.

These drive a real `CodingSession` with fake tools and one scripted provider. The
provider serves the run and its subagent in turn, so each test's streams read in the
order the requests are made: the run asks for a subagent, the subagent works, the
run continues.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from wisp.coding.prompt import WEB_SEARCH_GUIDANCE
from wisp.coding.session import CodingSession
from wisp.coding.subagent import fork_prompt, fork_transcript
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    AgentMode,
    BillableTokenUsage,
    ModelResponseUsage,
    TokenUsage,
    ToolApprovalRequested,
    ToolCallSnapshot,
    ToolNestedCallEnded,
    ToolNestedCallStarted,
    ToolResultReady,
    UsageCost,
    UsageCostRates,
    WispEvent,
)
from wisp.mcp.discovery import MCP_CALL
from wisp.messages import Message
from wisp.providers.base import ContextOverflowError, ProviderError
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions import MessageSessionEntry
from wisp.sessions.entries import ToolResultPresentationSnapshot, session_entry_from_json
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tool_types import ToolSafety
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolArguments, ToolExecutionMetadata, ToolInputSchema
from wisp.tools.codemode.calls import CODEMODE
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.seen_files import SeenFiles
from wisp.tools.subagent import SPAWN_AGENT, SpawnAgentTool

_SCHEMA: ToolInputSchema = {
    "type": "object",
    "properties": {"table": {"type": "string"}},
    "required": ["table"],
}
_USAGE = ProviderUsage(input_tokens=100, output_tokens=10, total_tokens=110)


@dataclass
class _Tool:
    name: str
    safety: ToolSafety = "read"
    description: str = "Query a table."
    input_schema: ToolInputSchema = field(default_factory=lambda: dict(_SCHEMA))
    delay: float = 0.0
    confirm_every_call: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)
    seen_files: list[SeenFiles | None] = field(default_factory=list)
    running: int = 0
    most_running: int = 0

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        self.calls.append(dict(arguments))
        self.seen_files.append(context.seen_files)
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            await anyio.sleep(self.delay)
        finally:
            self.running -= 1
        return ToolResult(text=f"{self.name}:{arguments.get('table')}")


@dataclass
class _Tools:
    lookup: _Tool = field(default_factory=lambda: _Tool("lookup"))
    drop: _Tool = field(default_factory=lambda: _Tool("drop", safety="command"))
    audit: _Tool = field(default_factory=lambda: _Tool("audit", confirm_every_call=True))

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in (self.lookup, self.drop, self.audit):
            registry.register(tool)
        return registry


def _spawn(*tasks: str) -> list[ProviderEvent]:
    """One `spawn_agent` call handing out the tasks; one default task when none is given."""

    return tool_call_stream(
        ToolCall(
            call_id="call-spawn",
            name=SPAWN_AGENT,
            arguments={"tasks": list(tasks or ("find the users table",))},
        )
    )


def _calls(*calls: tuple[str, dict[str, object]], content: str = "") -> list[ProviderEvent]:
    """A response that calls tools, as a subagent's model would."""

    tool_calls = tuple(
        ToolCall(call_id=f"sub-{index}", name=name, arguments=arguments)
        for index, (name, arguments) in enumerate(calls, start=1)
    )
    return [
        ProviderResponseStarted(model="test"),
        *(ProviderToolCallCompleted(tool_call=call) for call in tool_calls),
        ProviderResponseCompleted(
            content=content, tool_calls=tool_calls, finish_reason="tool_calls", usage=_USAGE
        ),
    ]


def _says(content: str) -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="test"),
        ProviderResponseCompleted(content=content, usage=_USAGE),
    ]


@dataclass
class _Run:
    events: list[WispEvent]
    live: list[WispEvent]
    provider: ScriptedProvider
    session: CodingSession

    @property
    def result(self) -> ToolResultReady:
        (result,) = [
            event
            for event in self.events
            if isinstance(event, ToolResultReady) and event.call_id == "call-spawn"
        ]
        return result


def _session(
    tmp_path: Path,
    tools: _Tools,
    streams: Sequence[list[ProviderEvent]],
    *,
    subagents: bool = True,
    mode: AgentMode = "build",
    max_tool_iterations: int | None = None,
) -> tuple[CodingSession, ScriptedProvider]:
    provider = ScriptedProvider(streams)
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path, seen_files=SeenFiles()),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.approve_all(),
        subagents=subagents,
        mode=mode,
        max_tool_iterations=max_tool_iterations,
    )
    return session, provider


def _run(
    tmp_path: Path,
    tools: _Tools,
    streams: Sequence[list[ProviderEvent]],
    *,
    subagents: bool = True,
    mode: AgentMode = "build",
    max_tool_iterations: int | None = None,
) -> _Run:
    session, provider = _session(
        tmp_path,
        tools,
        streams,
        subagents=subagents,
        mode=mode,
        max_tool_iterations=max_tool_iterations,
    )
    live: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live.append(event)

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go", live_event_sink=sink)]

    return _Run(anyio.run(collect), live, provider, session)


@pytest.mark.parametrize(
    ("subagents", "mode", "offered"),
    [(True, "build", True), (True, "plan", True), (False, "build", False)],
    ids=["on", "plan-mode", "off"],
)
def test_offered_only_when_on(
    tmp_path: Path, subagents: bool, mode: AgentMode, offered: bool
) -> None:
    run = _run(tmp_path, _Tools(), [_says("done")], subagents=subagents, mode=mode)

    assert (SPAWN_AGENT in {tool.name for tool in run.provider.calls[0].tools}) is offered


def test_registered_name_wins(tmp_path: Path) -> None:
    """A tool the session already registered as `spawn_agent` is never replaced."""

    tools = _Tools(lookup=_Tool(SPAWN_AGENT, description="Mine."))

    run = _run(tmp_path, tools, [_says("done")])

    offered = {tool.name: tool.description for tool in run.provider.calls[0].tools}
    assert offered[SPAWN_AGENT] == "Mine."


def test_replay_treats_spawn_as_read(tmp_path: Path) -> None:
    """Recovery may replay a turn that spawned a subagent: it changed nothing."""

    session, _ = _session(tmp_path, _Tools(), [])

    assert session._tool_is_unsafe(SPAWN_AGENT) is False  # noqa: SLF001
    assert session._tool_is_unsafe("drop") is True  # noqa: SLF001


def test_usage_outside_call_dropped() -> None:
    """Usage reported with no subagent call running is ignored, not an error."""

    ConfiguredToolExecutor.record_model_usage(
        TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2), None
    )


def test_operations_get_no_subagent(tmp_path: Path) -> None:
    """A run an operation limits to named tools is not offered a subagent."""

    session, provider = _session(tmp_path, _Tools(), [_says("done")])

    async def collect() -> list[WispEvent]:
        events = session.run("go", operation_tool_names=frozenset({"lookup"}))
        return [event async for event in events]

    anyio.run(collect)

    assert [tool.name for tool in provider.calls[0].tools] == ["lookup"]


def test_returns_final_report(tmp_path: Path) -> None:
    tools = _Tools()

    run = _run(
        tmp_path,
        tools,
        [
            _spawn(),
            _calls(("lookup", {"table": "users"})),
            _says("users lives in db/users.sql"),
            _says("done"),
        ],
    )

    assert run.result.output == "users lives in db/users.sql"
    assert not run.result.is_error
    assert tools.lookup.calls == [{"table": "users"}]
    # The run's next request carries only the report, nothing the subagent read.
    assert [result.output for result in run.provider.calls[3].tool_results] == [
        "users lives in db/users.sql"
    ]


def test_starts_without_conversation(tmp_path: Path) -> None:
    """A subagent is sent the task alone, and only tools that read without asking."""

    run = _run(tmp_path, _Tools(), [_spawn("find the users table"), _says("found"), _says("done")])

    request = run.provider.calls[1]
    assert [message.content for message in request.messages if message.role == "user"] == [
        "find the users table"
    ]
    assert [tool.name for tool in request.tools] == ["lookup"]


def test_calls_recorded_as_nested(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        _Tools(),
        [_spawn(), _calls(("lookup", {"table": "users"})), _says("found"), _says("done")],
    )

    (call,) = run.result.nested_calls
    assert (call.name, call.arguments, call.is_error) == ("lookup", {"table": "users"}, False)
    nested = [
        (event.type, event.parent_call_id)
        for event in run.live
        if isinstance(event, ToolNestedCallStarted | ToolNestedCallEnded)
    ]
    assert nested == [("tool.nested.started", "call-spawn"), ("tool.nested.ended", "call-spawn")]


@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        ("drop", "not read-only"),
        ("audit", "needs approval"),
        (SPAWN_AGENT, "cannot spawn a subagent"),
        (CODEMODE, "cannot run a script"),
    ],
)
def test_refuses_unoffered_calls(tmp_path: Path, name: str, refusal: str) -> None:
    """A subagent's model that calls a tool it was never offered gets a refusal.

    The run itself may use every one of these; approval is granted for all of them.
    """

    tools = _Tools()

    run = _run(
        tmp_path,
        tools,
        [
            _spawn(),
            _calls((name, {"table": "users", "task": "again"})),
            _says("gave up"),
            _says("done"),
        ],
    )

    (call,) = run.result.nested_calls
    assert call.is_error
    assert refusal in (call.error or "")
    assert tools.drop.calls == tools.audit.calls == []
    # Nothing was asked of the user, and no second subagent ran.
    assert not [event for event in run.events if isinstance(event, ToolApprovalRequested)]
    assert len(run.provider.calls) == 4


@dataclass
class _McpTool(_Tool):
    """A tool from MCP server `db`, which on-demand exposure finds in the catalog."""

    server_name: str = "db"

    @property
    def remote_name(self) -> str:
        return self.name.rsplit("__", 1)[-1]


def test_proxy_reaches_only_read_tools(tmp_path: Path) -> None:
    """Through `mcp_call`, a subagent runs a read-only MCP tool and no other."""

    query = _McpTool("mcp__db__query")
    drop = _McpTool("mcp__db__drop", safety="command")
    registry = ToolRegistry()
    registry.register(query)
    registry.register(drop)
    provider = ScriptedProvider(
        [
            _spawn(),
            _calls(
                (MCP_CALL, {"name": "mcp__db__query", "arguments": {"table": "a"}}),
                (MCP_CALL, {"name": "mcp__db__drop", "arguments": {"table": "a"}}),
            ),
            _says("found"),
            _says("done"),
        ]
    )
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_approval_policy=ToolApprovalPolicy.approve_all(),
        mcp_tool_exposure="on_demand",
        subagents=True,
    )

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    (result,) = [event for event in anyio.run(collect) if isinstance(event, ToolResultReady)]

    assert [(call.target, call.is_error) for call in result.nested_calls] == [
        ("mcp__db__query", False),
        ("mcp__db__drop", True),
    ]
    assert query.calls == [{"table": "a"}]
    assert drop.calls == []


def test_script_cannot_spawn(tmp_path: Path) -> None:
    """A codemode script is given no way to start a subagent."""

    pytest.importorskip("pydantic_monty")
    provider = ScriptedProvider(
        [
            tool_call_stream(
                ToolCall(
                    call_id="call-script",
                    name=CODEMODE,
                    arguments={"code": "await spawn_agent(tasks=['look around'])"},
                )
            ),
            _says("done"),
        ]
    )
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        codemode=True,
        subagents=True,
    )

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    (result,) = [event for event in anyio.run(collect) if isinstance(event, ToolResultReady)]

    offered = {tool.name: tool for tool in provider.calls[0].tools}
    assert SPAWN_AGENT in offered
    assert SPAWN_AGENT not in offered[CODEMODE].description
    assert result.is_error
    # Only the run's two requests were made: no subagent ever started.
    assert len(provider.calls) == 2


class _SearchingProvider(ScriptedProvider):
    """A scripted provider with built-in web search, recording when it is asked for."""

    supports_web_search: Literal[True] = True

    def __init__(self, streams: Sequence[list[ProviderEvent]]) -> None:
        super().__init__(streams)
        self.web_search: list[bool] = []

    async def stream(  # type: ignore[override]
        self, messages: Sequence[Message], *, web_search: bool = False, **options: object
    ) -> AsyncIterator[ProviderEvent]:
        self.web_search.append(web_search)
        async for event in super().stream(messages, **options):  # type: ignore[arg-type]
            yield event


def test_web_search_matches_prompt(tmp_path: Path) -> None:
    """A subagent told it can search the web is given the search."""

    provider = _SearchingProvider([_spawn(), _says("found"), _says("done")])
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        web_search=True,
        subagents=True,
    )

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    anyio.run(collect)

    subagent_prompt = "\n".join(
        message.content for message in provider.calls[1].messages if message.role == "system"
    )
    assert WEB_SEARCH_GUIDANCE in subagent_prompt
    assert provider.web_search == [True, True, True]


@pytest.mark.parametrize(("parallel_safe", "together"), [(True, 2), (False, 1)])
def test_calls_keep_their_scheduling(tmp_path: Path, parallel_safe: bool, together: int) -> None:
    """A subagent's calls in one response overlap only when their tool allows it."""

    lookup = _Tool("lookup", delay=0.05)
    registry = ToolRegistry()
    registry.register(lookup, execution=ToolExecutionMetadata(parallel_safe=parallel_safe))
    provider = ScriptedProvider(
        [
            _spawn(),
            _calls(("lookup", {"table": "a"}), ("lookup", {"table": "b"})),
            _says("found"),
            _says("done"),
        ]
    )
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        subagents=True,
    )

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    anyio.run(collect)

    assert len(lookup.calls) == 2
    assert lookup.most_running == together


@dataclass
class _ImageTool(_Tool):
    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text="PNG image, 1x1", images=(b"image bytes",))


def test_dropped_image_is_reported(tmp_path: Path) -> None:
    """A subagent gets a tool result's text only, and is told its image was left out."""

    run = _run(
        tmp_path,
        _Tools(lookup=_ImageTool("lookup")),
        [_spawn(), _calls(("lookup", {"table": "a"})), _says("found"), _says("done")],
    )

    assert [result.output for result in run.provider.calls[2].tool_results] == [
        "PNG image, 1x1\n[The image could not be attached to this result.]"
    ]
    assert not run.provider.calls[2].tool_results[0].images


class _RoutedProvider(ScriptedProvider):
    """Serve each conversation its own streams, whatever order requests arrive in.

    A conversation is known by its latest user message: the run's prompt, or a
    subagent's task. Subagents running at once interleave their requests, so one shared queue
    of streams would hand a subagent another's response.
    """

    def __init__(self, streams: dict[str, list[list[ProviderEvent]]]) -> None:
        super().__init__([])
        self._conversations = {user: list(queue) for user, queue in streams.items()}

    async def stream(  # type: ignore[override]
        self, messages: Sequence[Message], **options: object
    ) -> AsyncIterator[ProviderEvent]:
        user = [message.content for message in messages if message.role == "user"][-1]
        self.calls.append(user)  # type: ignore[arg-type]
        for item in self._conversations[user].pop(0):
            await anyio.sleep(0)
            yield item


def _spawn_each(*tasks: str) -> list[ProviderEvent]:
    """One response that hands every task to its own subagent."""

    return tool_call_stream(
        *(
            ToolCall(call_id=f"call-{task}", name=SPAWN_AGENT, arguments={"tasks": [task]})
            for task in tasks
        )
    )


def _fan_out(
    tmp_path: Path, tasks: Sequence[str], lookup: _Tool, *, parallel_safe: bool = True
) -> tuple[CodingSession, _RoutedProvider]:
    """A session whose run spawns one subagent per task, each calling ``lookup`` once."""

    registry = ToolRegistry()
    registry.register(lookup, execution=ToolExecutionMetadata(parallel_safe=parallel_safe))
    provider = _RoutedProvider(
        {
            "go": [_spawn_each(*tasks), _says("done")],
            **{
                task: [_calls(("lookup", {"table": task})), _says(f"report on {task}")]
                for task in tasks
            },
        }
    )
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        subagents=True,
    )
    return session, provider


def _collect(session: CodingSession) -> list[WispEvent]:
    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    return anyio.run(collect)


@pytest.mark.parametrize(
    ("tasks", "parallel_safe", "together"),
    [
        (("a", "b"), True, 2),
        # A tool that is not parallel-safe runs for one subagent at a time.
        (("a", "b"), False, 1),
        # Six subagents are asked for; four work at once.
        (("a", "b", "c", "d", "e", "f"), True, 4),
    ],
    ids=["together", "unsafe-tool-takes-turns", "limited"],
)
def test_subagents_run_together(
    tmp_path: Path, tasks: tuple[str, ...], parallel_safe: bool, together: int
) -> None:
    lookup = _Tool("lookup", delay=0.05)
    session, _ = _fan_out(tmp_path, tasks, lookup, parallel_safe=parallel_safe)

    events = _collect(session)

    assert lookup.most_running == together
    results = [event for event in events if isinstance(event, ToolResultReady)]
    # Each call gets its own subagent's report, in the order the calls were made.
    assert [(result.call_id, result.output) for result in results] == [
        (f"call-{task}", f"report on {task}") for task in tasks
    ]


def test_parallel_records_stay_apart(tmp_path: Path) -> None:
    """Subagents running at once each account for their own calls and responses."""

    session, _ = _fan_out(tmp_path, ("a", "b"), _Tool("lookup", delay=0.05))
    live: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live.append(event)

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go", live_event_sink=sink)]

    results = [event for event in anyio.run(collect) if isinstance(event, ToolResultReady)]

    assert [[call.arguments for call in result.nested_calls] for result in results] == [
        [{"table": "a"}],
        [{"table": "b"}],
    ]
    assert [len(result.model_usage) for result in results] == [2, 2]
    started = {
        event.parent_call_id: event.arguments
        for event in live
        if isinstance(event, ToolNestedCallStarted)
    }
    assert started == {"call-a": {"table": "a"}, "call-b": {"table": "b"}}


def test_cancel_stops_every_subagent(tmp_path: Path) -> None:
    lookup = _Tool("lookup", delay=60)
    session, provider = _fan_out(tmp_path, ("a", "b"), lookup)
    started: list[str] = []

    async def sink(event: WispEvent) -> None:
        if isinstance(event, ToolNestedCallStarted):
            started.append(event.parent_call_id)
            if len(started) == 2:
                assert session.cancel_run()

    async def collect() -> list[WispEvent]:
        with anyio.fail_after(5):
            return [event async for event in session.run("go", live_event_sink=sink)]

    events = anyio.run(collect)

    assert [event.outcome for event in events if isinstance(event, AgentCompleted)] == ["cancelled"]
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert sorted((result.call_id, result.is_error) for result in results) == [
        ("call-a", True),
        ("call-b", True),
    ]
    assert lookup.running == 0
    # The run and both subagents asked once each; nothing was asked after the cancel.
    assert sorted(provider.calls) == ["a", "b", "go"]  # type: ignore[comparison-overlap]


def _task_list(
    tmp_path: Path,
    conversations: dict[str, list[list[ProviderEvent]]],
    registry: ToolRegistry,
) -> tuple[CodingSession, _RoutedProvider]:
    provider = _RoutedProvider(conversations)
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_approval_policy=ToolApprovalPolicy.approve_all(),
        subagents=True,
    )
    return session, provider


def test_one_call_runs_tasks_together(tmp_path: Path) -> None:
    """A call's tasks overlap even beside a call that makes the response run in order."""

    lookup = _Tool("lookup", delay=0.05)
    registry = ToolRegistry()
    registry.register(lookup, execution=ToolExecutionMetadata(parallel_safe=True))
    registry.register(_Tool("drop", safety="command"))
    session, _ = _task_list(
        tmp_path,
        {
            "go": [
                tool_call_stream(
                    ToolCall(
                        call_id="call-spawn", name=SPAWN_AGENT, arguments={"tasks": ["a", "b"]}
                    ),
                    ToolCall(call_id="call-drop", name="drop", arguments={"table": "x"}),
                ),
                _says("done"),
            ],
            "a": [_calls(("lookup", {"table": "a"})), _says("report on a")],
            "b": [_calls(("lookup", {"table": "b"})), _says("report on b")],
        },
        registry,
    )

    events = _collect(session)

    assert lookup.most_running == 2
    spawn, drop = [event for event in events if isinstance(event, ToolResultReady)]
    assert spawn.output == "## Task 1\nreport on a\n\n## Task 2\nreport on b"
    assert drop.output == "drop:x"
    # One result accounts for both subagents, under ids that never repeat.
    assert sorted(call.call_id for call in spawn.nested_calls) == ["subagent-1-1", "subagent-2-1"]
    assert len(spawn.model_usage) == 4


def test_failed_task_leaves_the_rest(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_Tool("lookup"))
    failed = [ProviderResponseStarted(model="test"), ProviderResponseFailed(message="overloaded")]
    session, _ = _task_list(
        tmp_path,
        {"go": [_spawn("a", "b"), _says("done")], "a": [failed], "b": [_says("report on b")]},
        registry,
    )

    (result,) = [event for event in _collect(session) if isinstance(event, ToolResultReady)]

    assert not result.is_error
    assert result.output == ("## Task 1\nThe subagent failed: overloaded\n\n## Task 2\nreport on b")


def test_all_tasks_failing_is_an_error(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_Tool("lookup"))
    failed = [ProviderResponseStarted(model="test"), ProviderResponseFailed(message="overloaded")]
    session, _ = _task_list(
        tmp_path,
        {"go": [_spawn("a", "b"), _says("done")], "a": [failed], "b": [failed]},
        registry,
    )

    (result,) = [event for event in _collect(session) if isinstance(event, ToolResultReady)]

    assert result.is_error
    assert result.output.count("The subagent failed: overloaded") == 2


@pytest.mark.parametrize(
    ("tasks", "problem"),
    [
        ([], "at least one task"),
        ("find it", "at least one task"),
        (["a"] * 9, "at most 8 tasks"),
        (["a", " "], "non-empty string"),
        (["a", 3], "non-empty string"),
    ],
    ids=["empty", "not-a-list", "too-many", "blank", "not-text"],
)
def test_malformed_tasks_rejected(tmp_path: Path, tasks: object, problem: str) -> None:
    """A malformed task list starts no subagent and tells the model what to fix."""

    call = ToolCall(call_id="call-spawn", name=SPAWN_AGENT, arguments={"tasks": tasks})

    run = _run(tmp_path, _Tools(), [tool_call_stream(call), _says("done")])

    assert run.result.is_error
    assert problem in run.result.output
    assert run.result.failure_code == "invalid_arguments"
    assert len(run.provider.calls) == 2


def test_most_tasks_one_call_takes(tmp_path: Path) -> None:
    """Eight tasks, the most a call may hold, each get a subagent and a report."""

    tasks = [str(number) for number in range(8)]
    registry = ToolRegistry()
    registry.register(_Tool("lookup"))
    session, _ = _task_list(
        tmp_path,
        {
            "go": [_spawn(*tasks), _says("done")],
            **{task: [_says(f"report on {task}")] for task in tasks},
        },
        registry,
    )

    (result,) = [event for event in _collect(session) if isinstance(event, ToolResultReady)]

    assert not result.is_error
    assert result.output.count("report on") == 8
    assert result.output.endswith("## Task 8\nreport on 7")


def test_unexpected_failure_spares_the_rest(tmp_path: Path) -> None:
    """A subagent that crashes costs neither the others' reports nor their run."""

    async def runner(task: str, start_from: str) -> str:
        if task == "crash":
            raise RuntimeError("secret detail")
        await anyio.sleep(0.01)
        return f"report on {task}"

    tool = SpawnAgentTool(runner)

    async def run() -> ToolResult:
        return await tool.run({"tasks": ["crash", "b"]}, ToolContext(cwd=tmp_path))

    result = anyio.run(run)

    assert result.text == ("## Task 1\nThe subagent failed unexpectedly.\n\n## Task 2\nreport on b")


def test_long_report_leaves_room(tmp_path: Path) -> None:
    """A report over its share of the result is cut; the reports after it stay whole."""

    async def runner(task: str, start_from: str) -> str:
        return "long line\n" * 200 if task == "long" else f"report on {task}"

    tool = SpawnAgentTool(runner)

    async def run() -> ToolResult:
        context = ToolContext(cwd=tmp_path, max_output_bytes=600)
        return await tool.run({"tasks": ["long", "b", "c"]}, context)

    result = anyio.run(run)

    first, second, third = result.text.split("\n\n")
    assert first.startswith("## Task 1\nlong line\n") and first.endswith("[truncated]")
    assert (second, third) == ("## Task 2\nreport on b", "## Task 3\nreport on c")
    assert result.truncated
    assert len(result.text.encode()) <= 600


@pytest.mark.parametrize(
    ("limits", "measure"),
    [
        ({"max_output_bytes": 600}, lambda text: len(text.encode())),
        ({"max_output_lines": 30}, lambda text: len(text.splitlines())),
    ],
    ids=["bytes", "lines"],
)
def test_long_reports_fit_together(
    tmp_path: Path, limits: dict[str, int], measure: Callable[[str], int]
) -> None:
    """Reports that are all too long still fit the result's limit between them."""

    async def runner(task: str, start_from: str) -> str:
        return f"{task} line\n" * 200

    tool = SpawnAgentTool(runner)

    async def run() -> ToolResult:
        return await tool.run(
            {"tasks": ["a", "b", "c"]},
            ToolContext(cwd=tmp_path, **limits),  # type: ignore[arg-type]
        )

    result = anyio.run(run)

    assert measure(result.text) <= next(iter(limits.values()))
    for number, task in enumerate(("a", "b", "c"), start=1):
        assert f"## Task {number}\n{task} line\n" in result.text


def _fork(*tasks: str) -> list[ProviderEvent]:
    """One `spawn_agent` call that forks the conversation for each task."""

    return tool_call_stream(
        ToolCall(
            call_id="call-spawn",
            name=SPAWN_AGENT,
            arguments={"tasks": list(tasks), "context": "fork"},
        )
    )


def test_fork_continues_the_conversation(tmp_path: Path) -> None:
    """A fork is sent the run's own request, then what happened since, then its task."""

    run = _run(
        tmp_path,
        _Tools(),
        [
            tool_call_stream(ToolCall(call_id="own", name="lookup", arguments={"table": "a"})),
            _fork("say what a holds"),
            _says("a holds users"),
            _says("done"),
        ],
    )

    first, _, fork, _ = run.provider.calls
    # The run's first request is the fork's prefix, message for message, with the
    # same tools in the same order and the same cache key: what a provider cached
    # for the conversation still applies.
    assert fork.messages[: len(first.messages)] == first.messages
    assert fork.tools == first.tools
    assert fork.prompt_cache_key == first.prompt_cache_key
    assert fork.previous_response_id is None
    since = [message.content for message in fork.messages[len(first.messages) :]]
    # What the run had found, the note that this reader is the fork, then the task.
    assert "lookup:a" in since[0]
    assert "You are a subagent this call started" in since[-2]
    assert since[-1] == fork_prompt("say what a holds")
    assert fork.messages[-1].role == "user"
    assert run.result.output == "a holds users"


def _call(call_id: str, name: str) -> ToolCallSnapshot:
    return ToolCallSnapshot(call_id=call_id, name=name, arguments={})


@pytest.mark.parametrize(
    ("answered", "added"),
    [
        ((), ["read-1", "spawn-1", "grep-1"]),
        (("read-1",), ["spawn-1", "grep-1"]),
        (("read-1", "grep-1"), ["spawn-1"]),
    ],
    ids=["none-answered", "earlier-call-answered", "siblings-answered"],
)
def test_fork_answers_waiting_calls(answered: tuple[str, ...], added: list[str]) -> None:
    """Every call still waiting when the conversation forks gets a result in the copy."""

    conversation = [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            content="",
            tool_calls=(
                _call("read-1", "read"),
                _call("spawn-1", SPAWN_AGENT),
                _call("grep-1", "grep"),
            ),
        ),
        *(Message(role="tool", content="result", tool_call_id=call_id) for call_id in answered),
    ]

    forked = fork_transcript(conversation, started_by="spawn-1")

    assert list(forked[: len(conversation)]) == conversation
    placeholders = forked[len(conversation) :]
    assert [message.tool_call_id for message in placeholders] == added
    assert {
        message.tool_call_id: "You are a subagent" in message.content for message in placeholders
    } == {call_id: call_id == "spawn-1" for call_id in added}


@pytest.mark.parametrize(
    "conversation",
    [
        [],
        [Message(role="user", content="go"), Message(role="assistant", content="done")],
    ],
    ids=["empty", "settled"],
)
def test_fork_without_waiting_calls_unchanged(conversation: list[Message]) -> None:
    assert list(fork_transcript(conversation, started_by="spawn-1")) == conversation


def test_fork_ignores_earlier_rounds_ids() -> None:
    """A call waiting now is answered though an earlier round used the same call id.

    Models that issue no call ids repeat Wisp's synthetic ones from round to round.
    """

    conversation = [
        Message(role="assistant", content="", tool_calls=(_call("call-read-0", "read"),)),
        Message(role="tool", content="earlier result", tool_call_id="call-read-0"),
        Message(role="assistant", content="", tool_calls=(_call("call-read-0", "read"),)),
    ]

    forked = fork_transcript(conversation, started_by=None)

    assert [message.tool_call_id for message in forked[len(conversation) :]] == ["call-read-0"]


def test_fork_marks_only_its_own_call() -> None:
    """Of several `spawn_agent` calls waiting, a fork is told which one started it."""

    conversation = [
        Message(
            role="assistant",
            content="",
            tool_calls=(_call("spawn-1", SPAWN_AGENT), _call("spawn-2", SPAWN_AGENT)),
        ),
    ]

    first, second = fork_transcript(conversation, started_by="spawn-2")[1:]

    assert "belongs to the main agent" in first.content
    assert "You are a subagent this call started" in second.content


def test_fork_needs_a_running_conversation(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, _Tools(), [])

    with pytest.raises(ToolError, match="no conversation to fork"):
        session._fork_point()  # noqa: SLF001


def test_fork_overflow_says_to_start_fresh(tmp_path: Path) -> None:
    """A fork too long for the model is told to start fresh, not to narrow its task."""

    run = _run(
        tmp_path,
        _Tools(),
        [_fork("look"), [ContextOverflowError("too long")], _says("done")],  # type: ignore[list-item]
    )

    assert run.result.is_error
    assert "too long to fork: use context fresh" in run.result.output
    assert "narrower task" not in run.result.output


def test_fork_is_read_only(tmp_path: Path) -> None:
    """A fork is shown every tool the run has, and still may only read."""

    tools = _Tools()

    run = _run(
        tmp_path,
        tools,
        [
            _fork("clean up"),
            _calls(
                ("drop", {"table": "users"}),
                (SPAWN_AGENT, {"tasks": ["again"]}),
                ("lookup", {"table": "users"}),
            ),
            _says("looked only"),
            _says("done"),
        ],
    )

    assert {"drop", SPAWN_AGENT} <= {tool.name for tool in run.provider.calls[1].tools}
    assert [(call.name, call.is_error) for call in run.result.nested_calls] == [
        ("drop", True),
        (SPAWN_AGENT, True),
        ("lookup", False),
    ]
    assert "a script or a subagent can call only read-only tools" in (
        run.result.nested_calls[0].error or ""
    )
    assert tools.drop.calls == []
    assert len(run.provider.calls) == 4


def test_forks_share_one_conversation(tmp_path: Path) -> None:
    """Each task of a forking call gets the same conversation and its own task."""

    registry = ToolRegistry()
    registry.register(_Tool("lookup"))
    session, provider = _task_list(
        tmp_path,
        {
            "go": [_fork("a", "b"), _says("done")],
            fork_prompt("a"): [_says("report on a")],
            fork_prompt("b"): [_says("report on b")],
        },
        registry,
    )

    (result,) = [event for event in _collect(session) if isinstance(event, ToolResultReady)]

    assert result.output == "## Task 1\nreport on a\n\n## Task 2\nreport on b"
    assert sorted(provider.calls) == sorted(["go", fork_prompt("a"), fork_prompt("b"), "go"])  # type: ignore[type-var]


def test_unknown_context_rejected(tmp_path: Path) -> None:
    call = ToolCall(
        call_id="call-spawn", name=SPAWN_AGENT, arguments={"tasks": ["a"], "context": "sideways"}
    )

    run = _run(tmp_path, _Tools(), [tool_call_stream(call), _says("done")])

    assert run.result.is_error
    assert "context must be fresh or fork" in run.result.output
    assert len(run.provider.calls) == 2


def test_cancel_reaches_waiting_subagents(tmp_path: Path) -> None:
    """A subagent still waiting for its turn when the run is cancelled never starts."""

    tasks = ("a", "b", "c", "d", "e", "f")
    lookup = _Tool("lookup", delay=60)
    session, provider = _fan_out(tmp_path, tasks, lookup)
    started: list[str] = []

    async def sink(event: WispEvent) -> None:
        if isinstance(event, ToolNestedCallStarted):
            started.append(event.parent_call_id)
            if len(started) == 4:
                session.cancel_run()

    async def collect() -> list[WispEvent]:
        with anyio.fail_after(5):
            return [event async for event in session.run("go", live_event_sink=sink)]

    events = anyio.run(collect)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert sorted(result.call_id for result in results) == [f"call-{task}" for task in tasks]
    assert all(result.is_error for result in results)
    assert lookup.running == 0
    # The run and the four subagents that had a turn; the two waiting asked nothing.
    assert len(provider.calls) == 5


def test_reads_stay_unseen(tmp_path: Path) -> None:
    """What a subagent reads is not recorded as seen by the run that spawned it."""

    tools = _Tools()

    _run(
        tmp_path,
        tools,
        [
            tool_call_stream(ToolCall(call_id="own", name="lookup", arguments={"table": "a"})),
            _spawn(),
            _calls(("lookup", {"table": "b"})),
            _says("found"),
            _says("done"),
        ],
    )

    own, subagent = tools.lookup.seen_files
    assert own is not None
    assert subagent is not None
    assert subagent is not own


def test_spend_counts_toward_session(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        _Tools(),
        [_spawn(), _calls(("lookup", {"table": "users"})), _says("found"), _says("done")],
    )

    assert [response.usage.total_tokens for response in run.result.model_usage] == [110, 110]

    async def read() -> tuple[int, int, list[int]]:
        session = run.session.sessions.latest()
        stats = await run.session.get_session_stats(session)
        saved = [
            len(entry.tool_result.model_usage or ())
            for entry in session.read_entries()
            if isinstance(entry, MessageSessionEntry) and entry.tool_result is not None
        ]
        return stats.usage.total_tokens, stats.cost.unpriced_record_count, saved

    total_tokens, cost_records, saved = anyio.run(read)
    # The subagent's two responses, and the run's own last one. The run's first
    # response reports no usage, but every assistant message has a cost record.
    assert total_tokens == 330
    assert cost_records == 4
    assert saved == [2]


def test_priced_spend_survives_reload() -> None:
    """A subagent response's exact price reads back from a saved session unchanged."""

    priced = ModelResponseUsage(
        usage=TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
        cost=UsageCost(
            provider="scripted",
            model="test",
            billable=BillableTokenUsage(
                input_tokens=100,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=10,
            ),
            rates=UsageCostRates(
                input_usd_per_million=Decimal("3"), output_usd_per_million=Decimal("15")
            ),
            estimated_usd=Decimal("0.00045"),
        ),
    )
    entry = MessageSessionEntry(
        session_id="session",
        message=Message(role="tool", content="found", tool_call_id="call-spawn"),
        tool_result=ToolResultPresentationSnapshot(model_usage=(priced,)),
    )

    reloaded = session_entry_from_json(entry.model_dump_json())

    assert isinstance(reloaded, MessageSessionEntry)
    assert reloaded.tool_result is not None
    assert reloaded.tool_result.model_usage == (priced,)


def test_default_limit_fits_record(tmp_path: Path) -> None:
    """At its own limit, every response a subagent made is still accounted for."""

    rounds = [_calls(("lookup", {"table": str(index)})) for index in range(64)]

    run = _run(tmp_path, _Tools(), [_spawn(), *rounds, _says("done")])

    assert "stopped at its limit of 63 tool rounds" in run.result.output
    assert len(run.result.model_usage) == 64
    assert len(run.result.nested_calls) == 63


def test_limit_returns_partial(tmp_path: Path) -> None:
    """A subagent that runs out of tool rounds reports that, not an error."""

    run = _run(
        tmp_path,
        _Tools(),
        [
            _spawn(),
            _calls(("lookup", {"table": "a"}), content="a is empty so far"),
            _calls(("lookup", {"table": "b"})),
            _says("done"),
        ],
        max_tool_iterations=1,
    )

    assert not run.result.is_error
    # It keeps the last thing the subagent wrote, though its last response wrote nothing.
    assert run.result.output == (
        "a is empty so far\n\n"
        "[The subagent stopped at its limit of 1 tool rounds before finishing.]"
    )
    assert len(run.result.model_usage) == 2


def test_failure_keeps_spend(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        _Tools(),
        [
            _spawn(),
            _calls(("lookup", {"table": "a"})),
            [ProviderResponseStarted(model="test"), ProviderResponseFailed(message="overloaded")],
            _says("done"),
        ],
    )

    assert run.result.is_error
    assert run.result.output == "The subagent failed: overloaded"
    assert len(run.result.model_usage) == 1


@pytest.mark.parametrize(
    ("stream", "output"),
    [
        ([ProviderError("connection reset")], "The subagent failed: connection reset"),
        (
            [ContextOverflowError("too long")],
            "The subagent ran out of context before it could report. Give it a narrower task.",
        ),
        (
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="")],
            "The subagent finished without a report.",
        ),
    ],
    ids=["provider-raises", "context-overflow", "empty-report"],
)
def test_failure_reaches_model(
    tmp_path: Path, stream: list[ProviderEvent | BaseException], output: str
) -> None:
    """However a subagent fails, the run goes on with a result that says why."""

    provider = ScriptedProvider([_spawn(), stream, _says("done")])
    session = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_Tools().registry(),
        tool_context=ToolContext(cwd=tmp_path),
        subagents=True,
    )

    async def collect() -> list[WispEvent]:
        return [event async for event in session.run("go")]

    events = anyio.run(collect)

    (result,) = [event for event in events if isinstance(event, ToolResultReady)]
    assert (result.is_error, result.output) == (True, output)
    assert [event.outcome for event in events if isinstance(event, AgentCompleted)] == ["completed"]


def test_cancel_stops_subagent(tmp_path: Path) -> None:
    """Cancelling the run stops the subagent's tool and settles the call once."""

    tools = _Tools(lookup=_Tool("lookup", delay=60))
    session, provider = _session(
        tmp_path, tools, [_spawn(), _calls(("lookup", {"table": "a"})), _says("unreached")]
    )

    async def sink(event: WispEvent) -> None:
        if isinstance(event, ToolNestedCallStarted):
            assert session.cancel_run()

    async def collect() -> list[WispEvent]:
        with anyio.fail_after(5):
            return [event async for event in session.run("go", live_event_sink=sink)]

    events = anyio.run(collect)

    assert [event.outcome for event in events if isinstance(event, AgentCompleted)] == ["cancelled"]
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [(result.call_id, result.is_error) for result in results] == [("call-spawn", True)]
    assert tools.lookup.running == 0
    assert len(provider.calls) == 2
