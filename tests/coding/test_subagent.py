"""The `spawn_agent` tool: a subagent reads on the run's behalf and only reports back.

These drive a real `CodingSession` with fake tools and one scripted provider. The
provider serves the run and its subagent in turn, so each test's streams read in the
order the requests are made: the run asks for a subagent, the subagent works, the
run continues.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from wisp.coding.prompt import WEB_SEARCH_GUIDANCE
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    AgentMode,
    BillableTokenUsage,
    ModelResponseUsage,
    TokenUsage,
    ToolApprovalRequested,
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
from wisp.tools.result import ToolResult
from wisp.tools.seen_files import SeenFiles
from wisp.tools.subagent import SPAWN_AGENT

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


def _spawn(task: str = "find the users table") -> list[ProviderEvent]:
    return tool_call_stream(
        ToolCall(call_id="call-spawn", name=SPAWN_AGENT, arguments={"task": task})
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
        (CODEMODE, "cannot run another script"),
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
                    arguments={"code": "await spawn_agent(task='look around')"},
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
