from pathlib import Path
from unittest.mock import Mock

import anyio
import pytest

from tests.rpc_support import build_rpc_executor_fixture
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.tool_contracts import ToolExecutor
from wisp.events import QueueItemsRemoved, QueueUpdated, RpcCommandFinished
from wisp.rpc.commands import (
    ClearQueueCommand,
    GetQueueStateCommand,
    ParsedRpcCommand,
    PopQueueCommand,
    SetQueueModeCommand,
)
from wisp.rpc.session.queue import handle_rpc_queue_command


@pytest.mark.parametrize("operation", ["pop", "clear", "mode"])
@pytest.mark.parametrize("change", ["enqueue", "drain", "duplicate", "replacement", "mode"])
def test_guarded_queue_mutations_reject_changed_snapshots(
    tmp_path: Path, operation: str, change: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=fixture.agent.provider, tool_executor=Mock(spec=ToolExecutor)
            )
        )
        fixture.agent._active_harness = harness
        fixture.agent._accepting_queued_messages = True
        harness.steer("original")
        token = harness.queue_updated_event().token
        if change == "enqueue":
            harness.steer("newest")
        elif change == "drain":
            harness.drain_steering()
        elif change == "duplicate":
            harness.pop_latest_steering()
            harness.steer("original")
        elif change == "replacement":
            harness = AgentHarness(harness.config)
            harness.steer("original")
            fixture.agent._active_harness = harness
        else:
            harness.set_steering_mode("all")
        before = harness.queue_updated_event()
        command = (
            PopQueueCommand(id="mutation", kind="steering", expected_token=token)
            if operation == "pop"
            else ClearQueueCommand(id="mutation", expected_token=token)
            if operation == "clear"
            else SetQueueModeCommand(
                id="mutation", kind="steering", mode="all", expected_token=token
            )
        )
        await handle_rpc_queue_command(
            command, agent=fixture.agent, session=None, write_event=fixture.events.append
        )
        after = harness.queue_updated_event()
        assert (after.steering, after.follow_up, after.token) == (
            before.steering,
            before.follow_up,
            before.token,
        )
        assert not any(isinstance(event, QueueItemsRemoved) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished) and not finished.ok
        assert "Queue changed" in (finished.error or "")

    anyio.run(scenario)


def test_oversized_queue_inspection_fails_without_publishing_an_unwritable_snapshot(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=fixture.agent.provider, tool_executor=Mock(spec=ToolExecutor)
            )
        )
        fixture.agent._active_harness = harness
        fixture.agent._accepting_queued_messages = True
        harness.steer("kept" * 1000)
        before = harness.queue_updated_event()
        executor = fixture.executor(task_group=Mock(), send=Mock())
        executor.outbound_frame_limit = 512
        await executor.dispatch_parsed(
            ParsedRpcCommand.from_known(GetQueueStateCommand(id="inspect")), None
        )
        assert not any(isinstance(event, QueueUpdated) for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished) and not finished.ok
        after = harness.queue_updated_event()
        assert (after.steering, after.token) == (before.steering, before.token)

    anyio.run(scenario)


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("limit", [None, 512, 65536])
def test_queue_removal_preflights_exact_output_and_preserves_event_order(
    tmp_path: Path, clear: bool, limit: int | None
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=fixture.agent.provider, tool_executor=Mock(spec=ToolExecutor)
            )
        )
        fixture.agent._active_harness = harness
        fixture.agent._accepting_queued_messages = True
        content = '你好\n"\\\t' * 1000
        harness.steer(content)
        token = harness.queue_updated_event().token
        command = (
            ClearQueueCommand(id="remove", expected_token=token)
            if clear
            else PopQueueCommand(id="remove", kind="steering", expected_token=token)
        )
        executor = fixture.executor(task_group=Mock(), send=Mock())
        executor.outbound_frame_limit = limit
        await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), None)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        if limit == 512:
            assert not finished.ok
            assert harness.queue_updated_event().steering == (content,)
            assert harness.queue_updated_event().token == token
        else:
            assert finished.ok
            removed, snapshot = fixture.events[-3:-1]
            assert isinstance(removed, QueueItemsRemoved) and removed.steering == (content,)
            assert isinstance(snapshot, QueueUpdated) and not snapshot.steering
            assert snapshot.token != token and snapshot.command_id == "remove"

    anyio.run(scenario)
