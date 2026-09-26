from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import anyio
import pytest

from wisp.cli.rpc_output import RpcEventWriter
from wisp.events import ErrorEvent, MessageDelta, WispEvent


def test_rpc_writer_coalesces_adjacent_deltas_before_ordering_boundary() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        timestamp = datetime(2026, 9, 16, tzinfo=UTC)
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(written.append, task_group, flush_seconds=60)
            writer(MessageDelta(turn=1, delta="first ", timestamp=timestamp))
            writer(MessageDelta(turn=1, delta="second"))
            writer(ErrorEvent(message="boundary"))
            writer.close()
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert len(written) == 2
    assert isinstance(written[0], MessageDelta)
    assert written[0].delta == "first second"
    assert written[0].timestamp == datetime(2026, 9, 16, tzinfo=UTC)
    assert isinstance(written[1], ErrorEvent)


def test_rpc_writer_keeps_distinct_message_streams_separate() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(written.append, task_group, flush_seconds=60)
            writer(MessageDelta(turn=1, delta="text", content_kind="text"))
            writer(MessageDelta(turn=1, delta="thought", content_kind="thinking"))
            writer(MessageDelta(turn=2, delta="next", content_kind="thinking"))
            writer(MessageDelta(turn=2, delta=" item", content_index=1, content_kind="thinking"))
            writer(
                MessageDelta(
                    turn=2,
                    role="user",
                    delta="different role",
                    content_index=1,
                    content_kind="thinking",
                )
            )
            writer.close()
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert [event.delta for event in written if isinstance(event, MessageDelta)] == [
        "text",
        "thought",
        "next",
        " item",
        "different role",
    ]


def test_rpc_writer_bounds_each_batch_by_source_parts() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(
                written.append,
                task_group,
                flush_seconds=60,
                max_parts=2,
            )
            for delta in ("a", "b", "c", "d", "e"):
                writer(MessageDelta(turn=1, delta=delta))
            writer.close()
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert [event.delta for event in written if isinstance(event, MessageDelta)] == [
        "ab",
        "cd",
        "e",
    ]


def test_rpc_writer_bounds_each_batch_by_utf8_bytes() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(
                written.append,
                task_group,
                flush_seconds=60,
                max_bytes=3,
            )
            writer(MessageDelta(turn=1, delta="é"))
            writer(MessageDelta(turn=1, delta="é"))
            writer.close()
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert [event.delta for event in written if isinstance(event, MessageDelta)] == ["é", "é"]


def test_rpc_writer_flushes_a_partial_batch_after_deadline() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        flushed = anyio.Event()

        def record(event: WispEvent) -> None:
            written.append(event)
            flushed.set()

        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(record, task_group, flush_seconds=0.01)
            writer(MessageDelta(turn=1, delta="visible"))
            with anyio.fail_after(0.2):
                await flushed.wait()
            writer.close()
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert len(written) == 1
    assert isinstance(written[0], MessageDelta)
    assert written[0].delta == "visible"


def test_rpc_writer_timer_applies_only_to_the_current_batch() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(written.append, task_group, flush_seconds=0.01)
            writer(MessageDelta(turn=1, delta="first"))
            writer(ErrorEvent(message="boundary"))
            writer(MessageDelta(turn=1, delta="second"))
            await anyio.sleep(0.03)
            writer.close()
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert [
        (type(event), event.delta if isinstance(event, MessageDelta) else event.message)
        for event in written
    ] == [
        (MessageDelta, "first"),
        (ErrorEvent, "boundary"),
        (MessageDelta, "second"),
    ]


def test_rpc_writer_reuses_one_timer_task_across_batches() -> None:
    async def scenario() -> tuple[int, list[WispEvent]]:
        written: list[WispEvent] = []
        current_task = asyncio.current_task()
        assert current_task is not None
        before = asyncio.all_tasks()
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(written.append, task_group, flush_seconds=60)
            await anyio.sleep(0)
            writer_tasks = asyncio.all_tasks() - before
            for index in range(1_000):
                writer(MessageDelta(turn=index, delta="x"))
                writer(ErrorEvent(message="boundary"))
            await anyio.sleep(0)
            active_writer_tasks = [task for task in writer_tasks if not task.done()]
            writer.close()
            task_group.cancel_scope.cancel()
        return len(active_writer_tasks), written

    active_task_count, written = anyio.run(scenario)

    assert active_task_count == 1
    assert len(written) == 2_000


def test_rpc_writer_flushes_on_close_and_rejects_later_events() -> None:
    async def scenario() -> list[WispEvent]:
        written: list[WispEvent] = []
        async with anyio.create_task_group() as task_group:
            writer = RpcEventWriter(written.append, task_group, flush_seconds=60)
            writer(MessageDelta(turn=1, delta="final"))
            writer.close()
            with pytest.raises(RuntimeError, match="closed"):
                writer(MessageDelta(turn=1, delta="late"))
            task_group.cancel_scope.cancel()
        return written

    written = anyio.run(scenario)

    assert len(written) == 1
    assert isinstance(written[0], MessageDelta)
    assert written[0].delta == "final"
