"""Complete scripts use real file/shell tools and preserve side-effect boundaries."""

from pathlib import Path

import anyio
import pytest

from tests.coding.test_codemode import _done, _script, _Tools
from wisp.coding.session import CodingSession
from wisp.events import (
    AgentCompleted,
    ToolApprovalRequested,
    ToolExecutionEnded,
    ToolNestedCallEnded,
    ToolResultReady,
    WispEvent,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.runtime.registry import ToolRegistry
from wisp.sessions import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import EditTool, ReadTool, WriteTool
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult
from wisp.tools.shell.tool import BashTool

pytest.importorskip("pydantic_monty")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (ReadTool(), WriteTool(), EditTool(), BashTool()):
        registry.register(tool)
    return registry


@pytest.mark.process
@pytest.mark.parametrize("fails", [False, True], ids=["success", "partial-failure"])
def test_file_workflow_commits_state(tmp_path: Path, fails: bool) -> None:
    (tmp_path / "count.txt").write_text("2\n")
    code = (
        'source = await read(path="count.txt")\n'
        "total = int(source.strip()) * 3\n"
        'await edit(path="count.txt", edits=[{"oldText": "2", "newText": str(total)}])\n'
        'await write(path="receipt.txt", content=f"count={total}\\n", overwrite=False)\n'
        'await store("total", total)\n'
        'print(await bash(operation="run", '
        'command="test $(cat count.txt) = 6 && cat receipt.txt"))\n'
    )
    if fails:
        code += 'raise ValueError("after effects")\n'
    store = JsonlSessionStore(tmp_path / "sessions")
    persisted = store.create()
    session = CodingSession(
        provider=ScriptedProvider([_script(code), _done()]),
        sessions=store,
        tool_registry=_registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_approval_policy=ToolApprovalPolicy.approve_all(),
        codemode=True,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("update and test", session=persisted)]

    events = anyio.run(run)
    (result,) = [event for event in events if isinstance(event, ToolResultReady)]
    assert result.is_error is fails, result.output
    assert "count=6" in result.output
    assert (tmp_path / "count.txt").read_text() == "6\n"
    assert (tmp_path / "receipt.txt").read_text() == "count=6\n"
    assert persisted.read_context().codemode_state == ({} if fails else {"total": 6})
    assert [call.name for call in result.nested_calls or ()] == ["read", "edit", "write", "bash"]
    assert all(not call.is_error for call in result.nested_calls or ())
    if fails:
        assert "External effects are not rolled back" in result.output
        assert not result.retryable


@pytest.mark.parametrize("approved", [False, True], ids=["deny", "approve"])
@pytest.mark.parametrize("proxy", [False, True], ids=["direct", "proxy"])
def test_read_decides_approved_action(tmp_path: Path, approved: bool, proxy: bool) -> None:
    tools = _Tools()
    tools.query.reply = "proceed"
    action = (
        'await mcp_call(name="mcp__db__drop", arguments={"table": "staging"})'
        if proxy
        else 'await mcp__db__drop(table="staging")'
    )
    code = (
        'decision = await mcp__db__query(table="policy")\n'
        'if decision == "proceed":\n'
        "    try:\n"
        f"        {action}\n"
        '        await store("acted", True)\n'
        "    except RuntimeError:\n"
        '        await store("acted", False)\n'
        'print(await load("acted"))'
    )
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.approve_tool_names({"codemode"}))
    approvals: list[ToolApprovalRequested] = []
    store = JsonlSessionStore(tmp_path / "sessions")
    persisted = store.create()
    session = CodingSession(
        provider=ScriptedProvider([_script(code), _done()]),
        sessions=store,
        tool_registry=tools.registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=policy,
        mcp_tool_exposure="on_demand" if proxy else "direct",
        codemode=True,
    )

    async def live(event: WispEvent) -> None:
        if isinstance(event, ToolApprovalRequested):
            approvals.append(event)
            assert tools.query.calls == [{"table": "policy"}]
            assert tools.drop.calls == []
            assert policy.resolve_approval(call_id=event.call_id, approved=approved)

    async def run() -> list[WispEvent]:
        return [
            event async for event in session.run("decide", session=persisted, live_event_sink=live)
        ]

    events = anyio.run(run)
    (result,) = [event for event in events if isinstance(event, ToolResultReady)]
    assert not result.is_error, result.output
    assert len(approvals) == 1
    assert approvals[0].safety == "command"
    assert approvals[0].target == (tools.drop.name if proxy else None)
    assert tools.drop.calls == ([{"table": "staging"}] if approved else [])
    assert persisted.read_context().codemode_state == {"acted": approved}
    assert [call.failure_code for call in result.nested_calls or ()] == [
        None,
        None if approved else "approval_denied",
    ]


@pytest.mark.parametrize("mode", ["plan", "build"])
def test_policy_limits_discovery(tmp_path: Path, mode: str) -> None:
    session = CodingSession(
        provider=ScriptedProvider([_script('await tool_search(query="write")'), _done()]),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_registry(),
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_tool_names({"read", "write"}),
        codemode=True,
    )
    session.mode = "plan" if mode == "plan" else "build"

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("list")]

    (result,) = [event for event in anyio.run(run) if isinstance(event, ToolResultReady)]
    assert not result.is_error
    assert ('"name": "write"' in result.output) is (mode == "build")
    assert '"name": "bash"' not in result.output


@pytest.mark.parametrize("external", [False, True], ids=["user-cancel", "caller-cancel"])
def test_cancel_keeps_mutation_audit(tmp_path: Path, external: bool) -> None:
    """Completed effects survive cancellation with their audit, while staged state does not."""

    async def run() -> None:
        blocked = anyio.Event()
        stopped = anyio.Event()
        write_calls = 0
        block_calls = 0

        class CountingWrite(WriteTool):
            async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
                nonlocal write_calls
                write_calls += 1
                return await super().run(arguments, context)

        class BlockingTool:
            name = "block"
            safety = "mutating"
            description = "Wait until cancelled after the completed write."
            input_schema = {"type": "object", "properties": {}}

            async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
                nonlocal block_calls
                block_calls += 1
                blocked.set()
                try:
                    await anyio.sleep_forever()
                finally:
                    stopped.set()

        registry = ToolRegistry()
        registry.register(CountingWrite())
        registry.register(BlockingTool())
        store = JsonlSessionStore(tmp_path / "sessions")
        persisted = store.create()
        provider = ScriptedProvider(
            [
                _script(
                    'await store("staged", True)\n'
                    'await write(path="effect.txt", content="once", overwrite=False)\n'
                    "await block()\n"
                    'await write(path="unreached.txt", content="unexpected")'
                ),
                _done(),
            ]
        )
        session = CodingSession(
            provider=provider,
            sessions=store,
            tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path),
            tool_approval_policy=ToolApprovalPolicy.approve_all(),
            codemode=True,
        )
        events: list[WispEvent] = []
        live: list[WispEvent] = []

        async def sink(event: WispEvent) -> None:
            live.append(event)

        with anyio.fail_after(5), anyio.CancelScope() as caller:

            async def cancel() -> None:
                await blocked.wait()
                if external:
                    caller.cancel()
                else:
                    assert session.cancel_run()

            async with anyio.create_task_group() as group:
                group.start_soon(cancel)
                async for event in session.run(
                    "write then wait", session=persisted, live_event_sink=sink
                ):
                    events.append(event)

        assert stopped.is_set()
        assert write_calls == block_calls == 1
        assert len(provider.calls) == 1
        assert (tmp_path / "effect.txt").read_text() == "once"
        assert not (tmp_path / "unreached.txt").exists()
        assert persisted.read_context().codemode_state == {}
        (saved,) = [
            entry.tool_result
            for entry in persisted.read_entries()
            if isinstance(entry, MessageSessionEntry) and entry.tool_result is not None
        ]
        assert saved.status == "cancelled"
        calls = saved.nested_calls or ()
        assert [(call.name, call.is_error) for call in calls] == [
            ("write", False),
            ("block", True),
        ]
        assert calls[0].arguments == {
            "path": "effect.txt",
            "content": "once",
            "overwrite": False,
        }
        assert calls[1].error == "The script ended before this call finished"
        assert [event.call for event in live if isinstance(event, ToolNestedCallEnded)] == [
            calls[0]
        ]
        if not external:
            (ended,) = [event for event in events if isinstance(event, ToolExecutionEnded)]
            (result,) = [event for event in events if isinstance(event, ToolResultReady)]
            assert ended.nested_calls == result.nested_calls == calls
            assert ended.is_error and result.is_error
            assert not ended.retryable and not result.retryable
            assert [event.outcome for event in events if isinstance(event, AgentCompleted)] == [
                "cancelled"
            ]

    anyio.run(run)
