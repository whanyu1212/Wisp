"""Whole-message transport pagination must never turn history into empty previews."""

from pathlib import Path

import anyio
import pytest

from wisp.agent.messages import Message
from wisp.events import RpcMessagesReported
from wisp.rpc.framing import RpcFrameError, encode_rpc_frame
from wisp.rpc.session.read import _write_messages_page
from wisp.sessions.jsonl import JsonlSessionStore


@pytest.mark.parametrize("forward", [False, True])
def test_history_transfer_defers_whole_messages_without_losing_content(
    tmp_path: Path, forward: bool
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        for index in range(7):
            await session.append_message(Message(role="user", content=f"{index}:" + "界" * 30_000))

    anyio.run(write)
    original = session.read_message_page(complete_structure=True)
    first = original.messages[0]
    one = RpcMessagesReported(command_id="history", messages=(first,), truncated=True)
    frame_limit = len(encode_rpc_frame(one, max_frame_bytes=1_000_000)) + 1_000
    cursor = None
    received = []
    while True:
        page = session.read_message_page(
            complete_structure=True,
            after_entry_id=cursor if forward else None,
            before_entry_id=None if forward else cursor,
        )
        report = RpcMessagesReported(
            command_id="history",
            messages=page.messages,
            truncated=page.truncated,
            next_before_entry_id=page.next_before_entry_id,
            next_after_entry_id=page.next_after_entry_id,
        )
        published = []
        _write_messages_page(
            report,
            write_event=published.append,
            forward=forward,
            exact=False,
            max_frame_bytes=frame_limit,
        )
        assert len(published) == 1
        accepted = published[0]
        assert isinstance(accepted, RpcMessagesReported)
        encode_rpc_frame(accepted, max_frame_bytes=frame_limit)
        assert all(not message.content_truncated for message in accepted.messages)
        if forward:
            received.extend(accepted.messages)
        else:
            received[0:0] = accepted.messages
        cursor = accepted.next_after_entry_id if forward else accepted.next_before_entry_id
        if cursor is None:
            break
    assert tuple(received) == original.messages


@pytest.mark.parametrize("exact", [False, True])
def test_oversized_single_message_fails_before_publishing(tmp_path: Path, exact: bool) -> None:
    session = JsonlSessionStore(tmp_path).create()
    anyio.run(session.append_message, Message(role="user", content="x" * 2_000))
    report = RpcMessagesReported(
        command_id="history", messages=session.read_message_page(complete_structure=True).messages
    )
    published = []
    with pytest.raises(RpcFrameError):
        _write_messages_page(
            report, write_event=published.append, forward=False, exact=exact, max_frame_bytes=1_000
        )
    assert published == []
