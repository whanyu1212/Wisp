"""Codemode child approvals stay separate from the read-only subagent policy."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from tests.coding.test_codemode import _done, _proxied_registry, _script, _Tools
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolNestedCallEnded,
    ToolNestedCallStarted,
    ToolResultReady,
    WispEvent,
)
from wisp.mcp.discovery import MCP_CALL, McpCatalog
from wisp.providers.events import ToolCall
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import ReadTool
from wisp.tools.policy import ToolPolicy


class _ReadApprovalPolicy(RpcToolApprovalPolicy):
    """Exercise interactive approval for eligible read tools without side effects."""

    def requires_approval(self, tool: Tool) -> bool:
        return tool.name in {"lookup", "mcp__db__query"} or super().requires_approval(tool)


def _policy() -> _ReadApprovalPolicy:
    return _ReadApprovalPolicy(ToolApprovalPolicy.approve_tool_names({"codemode"}))


@pytest.mark.parametrize("approved", [True, False], ids=["approve", "deny"])
@pytest.mark.parametrize("proxy", [False, True], ids=["direct", "proxy"])
def test_child_has_own_approval(tmp_path: Path, approved: bool, proxy: bool) -> None:
    tools = _Tools()
    policy = _policy()
    timeline: list[WispEvent] = []
    code = (
        'await mcp_call(name="mcp__db__query", arguments={"table": "a"})'
        if proxy
        else 'await lookup(table="a")'
    )
    session = CodingSession(
        provider=ScriptedProvider([_script(code), _done()]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=policy,
        mcp_tool_exposure="on_demand" if proxy else "direct",
        codemode=True,
    )

    async def live(event: WispEvent) -> None:
        timeline.append(event)
        if isinstance(event, ToolApprovalRequested):
            assert policy.has_pending_approval(call_id=event.call_id)
            assert not tools.lookup.calls and not tools.query.calls
            assert policy.resolve_approval(call_id=event.call_id, approved=approved)

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("go", live_event_sink=live)]

    events = anyio.run(run)
    started, requested, resolved, ended = timeline
    assert isinstance(started, ToolNestedCallStarted)
    assert isinstance(requested, ToolApprovalRequested)
    assert isinstance(resolved, ToolApprovalResolved)
    assert isinstance(ended, ToolNestedCallEnded)
    assert started.parent_call_id == "call-script"
    assert requested.call_id == resolved.call_id == ended.call.call_id == started.call_id
    assert requested.call_id != started.parent_call_id
    assert requested.name == (MCP_CALL if proxy else "lookup")
    assert requested.target == ("mcp__db__query" if proxy else None)
    assert requested.arguments == (
        {"name": "mcp__db__query", "arguments": {"table": "a"}} if proxy else {"table": "a"}
    )
    assert resolved.approved is approved
    assert ended.call.is_error is not approved
    assert (tools.query if proxy else tools.lookup).calls == ([{"table": "a"}] if approved else [])
    assert not policy.has_pending_approval(call_id=requested.call_id)
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.nested_calls[0].call_id == requested.call_id
    assert result.nested_calls[0].failure_code == (None if approved else "approval_denied")


@pytest.mark.parametrize("origin", ["codemode", "subagent"])
def test_missing_channel_denies(origin: str) -> None:
    tools = _Tools()
    policy = _policy()
    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
    )

    async def run() -> None:
        caller = executor.run_codemode if origin == "codemode" else executor.run_nested
        result = await caller(ToolCall(call_id="child", name="lookup", arguments={"table": "a"}))
        assert result.is_error and "approval" in result.text
        assert not policy.has_pending_approval(call_id="child")

    anyio.run(run)
    assert tools.lookup.calls == []


@pytest.mark.parametrize("origin", ["codemode", "subagent"])
@pytest.mark.parametrize("proxy", [True, False])
def test_mutations_still_denied(origin: str, proxy: bool) -> None:
    tools = _Tools()
    executor = ConfiguredToolExecutor(
        registry=_proxied_registry(tools),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        mcp_catalog=McpCatalog.from_tools([tools.query, tools.drop], connected=lambda _: True),
    )

    async def run() -> None:
        caller = executor.run_codemode if origin == "codemode" else executor.run_nested
        result = await caller(
            ToolCall(
                call_id="child",
                name=MCP_CALL if proxy else tools.drop.name,
                arguments={"name": tools.drop.name, "arguments": {"table": "a"}}
                if proxy
                else {"table": "a"},
            )
        )
        assert result.is_error and "not read-only" in result.text

    anyio.run(run)
    assert tools.drop.calls == []


@pytest.mark.parametrize("stage", ["request", "waiting", "resolved"])
def test_cancel_clears_child_approval(stage: str) -> None:
    tools = _Tools()
    policy = _policy()
    events: list[WispEvent] = []

    async def run() -> None:
        waiting = anyio.Event()
        with anyio.CancelScope() as scope:

            async def live(event: WispEvent) -> None:
                events.append(event)
                if isinstance(event, ToolApprovalRequested):
                    waiting.set()
                    if stage == "request":
                        scope.cancel()
                        await anyio.lowlevel.checkpoint()
                    if stage == "resolved":
                        policy.resolve_approval(call_id=event.call_id, approved=True)
                elif isinstance(event, ToolApprovalResolved) and event.approved:
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

            executor = ConfiguredToolExecutor(
                registry=tools.registry(),
                context=ToolContext(cwd=Path.cwd()),
                policy=ToolPolicy.allow_all_tools(),
                approval_policy=policy,
                progress_sink=live,
            )

            async def cancel_waiter() -> None:
                await waiting.wait()
                scope.cancel()

            async with anyio.create_task_group() as group:
                if stage == "waiting":
                    group.start_soon(cancel_waiter)
                await executor.run_codemode(
                    ToolCall(call_id="child", name="lookup", arguments={"table": "a"})
                )
        assert not policy.has_pending_approval(call_id="child")
        assert not policy.resolve_approval(call_id="child", approved=True)

    anyio.run(run)
    assert tools.lookup.calls == []
    assert isinstance(events[-1], ToolApprovalResolved) and not events[-1].approved


@pytest.mark.parametrize("stage", ["request", "resolved"])
def test_failed_delivery_never_runs(stage: str) -> None:
    tools = _Tools()
    policy = _policy()

    async def live(event: WispEvent) -> None:
        if isinstance(event, ToolApprovalRequested):
            if stage == "request":
                raise OSError("channel closed")
            policy.resolve_approval(call_id=event.call_id, approved=True)
        elif isinstance(event, ToolApprovalResolved) and event.approved:
            raise OSError("channel closed")

    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
        progress_sink=live,
    )

    async def run() -> None:
        result = await executor.run_codemode(
            ToolCall(call_id="child", name="lookup", arguments={"table": "a"})
        )
        assert result.is_error and "was not run" in result.text
        assert not policy.has_pending_approval(call_id="child")

    anyio.run(run)
    assert not tools.lookup.calls


def test_approvals_do_not_overlap() -> None:
    tools = _Tools()
    policy = _policy()
    events: list[WispEvent] = []

    async def live(event: WispEvent) -> None:
        events.append(event)
        if isinstance(event, ToolApprovalRequested):
            # Let the other child reach the approval lock before releasing this one.
            await anyio.lowlevel.checkpoint()
            policy.resolve_approval(call_id=event.call_id, approved=True)

    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
        progress_sink=live,
    )

    async def one(call_id: str) -> None:
        result = await executor.run_codemode(
            ToolCall(call_id=call_id, name="lookup", arguments={"table": call_id})
        )
        assert not result.is_error

    async def run() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(one, "one")
            group.start_soon(one, "two")

    anyio.run(run)
    assert [event.type for event in events] == [
        "tool.approval.requested",
        "tool.approval.resolved",
    ] * 2
    assert len(tools.lookup.calls) == 2


def test_preapproved_child_needs_no_channel() -> None:
    tools = _Tools()
    policy = _policy()
    policy.approved_tools = frozenset({"lookup"})
    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
    )

    async def run() -> None:
        result = await executor.run_codemode(
            ToolCall(call_id="child", name="lookup", arguments={"table": "a"})
        )
        assert not result.is_error

    anyio.run(run)
    assert tools.lookup.calls == [{"table": "a"}]


@pytest.mark.parametrize("path", ["allowed.txt", "secret.txt"])
def test_protected_reads_stay_blocked(tmp_path: Path, path: str) -> None:
    (tmp_path / path).write_text("fixture-content")
    registry = ToolRegistry()
    registry.register(ReadTool())
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=("secret.txt",)),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )

    async def run() -> None:
        result = await executor.run_codemode(
            ToolCall(call_id="child", name="read", arguments={"path": path})
        )
        if path == "secret.txt":
            assert result.is_error and "protected" in result.text
            assert "fixture-content" not in result.text
        else:
            assert not result.is_error and "fixture-content" in result.text

    anyio.run(run)


def test_policy_denial_precedes_approval() -> None:
    tools = _Tools()
    policy = _policy()
    events: list[WispEvent] = []

    async def live(event: WispEvent) -> None:
        events.append(event)

    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_tool_names({"mcp__db__query"}),
        approval_policy=policy,
        progress_sink=live,
    )

    async def run() -> None:
        result = await executor.run_codemode(
            ToolCall(call_id="child", name="lookup", arguments={"table": "a"})
        )
        assert result.is_error
        assert not policy.has_pending_approval(call_id="child")

    anyio.run(run)
    assert not events and not tools.lookup.calls


def test_confirmations_remain_per_call() -> None:
    tools = _Tools()
    tools.lookup.confirm_every_call = True
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_all())
    policy.register_confirmation("lookup", lambda _name, _arguments: None)
    events: list[WispEvent] = []

    async def live(event: WispEvent) -> None:
        events.append(event)
        if isinstance(event, ToolApprovalRequested):
            assert not event.grants_allowed
            policy.resolve_approval(call_id=event.call_id, approved=True, scope="all_session")

    executor = ConfiguredToolExecutor(
        registry=tools.registry(),
        context=ToolContext(cwd=Path.cwd()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
        progress_sink=live,
    )

    async def run() -> None:
        for call_id in ("one", "two"):
            result = await executor.run_codemode(
                ToolCall(call_id=call_id, name="lookup", arguments={"table": "a"})
            )
            assert not result.is_error

    anyio.run(run)
    assert sum(isinstance(event, ToolApprovalRequested) for event in events) == 2
    assert len(tools.lookup.calls) == 2


def test_script_failure_clears_approval(tmp_path: Path) -> None:
    """A sibling failure clears the pending approval while the model run continues."""

    from wisp.events import ToolExecutionEnded
    from wisp.tools.result import ToolError, ToolResult

    tools = _Tools()
    policy = _policy()
    timeline: list[WispEvent] = []

    async def run() -> list[WispEvent]:
        requested = anyio.Event()
        cleanup_entered = anyio.Event()
        cleanup_released = anyio.Event()

        class FailingRead:
            name = "fail"
            safety = "read"
            description = "Fail after the other child requests approval."
            input_schema = {"type": "object", "properties": {}}

            async def run(self, arguments: object, context: ToolContext) -> ToolResult:
                await requested.wait()
                raise ToolError("sibling failed")

        registry = tools.registry()
        registry.register(FailingRead())
        session = CodingSession(
            provider=ScriptedProvider(
                [
                    _script('import asyncio\nawait asyncio.gather(lookup(table="a"), fail())'),
                    _done(),
                ]
            ),
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            tool_policy=ToolPolicy.allow_all_tools(),
            tool_approval_policy=policy,
            codemode=True,
        )

        async def live(event: WispEvent) -> None:
            if isinstance(event, ToolApprovalResolved):
                cleanup_entered.set()
                await cleanup_released.wait()
            timeline.append(event)
            if isinstance(event, ToolApprovalRequested):
                requested.set()

        async def release_cleanup() -> None:
            await cleanup_entered.wait()
            await anyio.wait_all_tasks_blocked()
            try:
                assert not any(isinstance(event, ToolExecutionEnded) for event in timeline)
            finally:
                cleanup_released.set()

        events = []
        with anyio.fail_after(10):
            async with anyio.create_task_group() as group:
                group.start_soon(release_cleanup)
                async for event in session.run("go", live_event_sink=live):
                    timeline.append(event)
                    events.append(event)
        return events

    events = anyio.run(run)
    approval = next(event for event in timeline if isinstance(event, ToolApprovalRequested))
    resolved = next(event for event in timeline if isinstance(event, ToolApprovalResolved))
    ended = next(event for event in timeline if isinstance(event, ToolExecutionEnded))
    assert not resolved.approved and resolved.call_id == approval.call_id
    assert timeline.index(resolved) < timeline.index(ended)
    assert not policy.has_pending_approval(call_id=approval.call_id)
    assert not policy.resolve_approval(call_id=approval.call_id, approved=True)
    assert not tools.lookup.calls
    assert next(event for event in events if isinstance(event, ToolResultReady)).is_error
    assert events[-1].type == "agent.completed"
