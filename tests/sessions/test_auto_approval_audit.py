"""Automatic decisions survive reload without leaking across branches or reused IDs."""

from __future__ import annotations

from pathlib import Path

import anyio

from wisp.events import ToolApprovalResolved
from wisp.messages import Message
from wisp.sessions.jsonl import JsonlSessionStore


def test_auto_audit_is_attached_to_the_matching_tool_result(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="edit"), operation_id="first")
        await session.append_event(
            ToolApprovalResolved(
                call_id="reused",
                name="write",
                approved=True,
                source="auto",
                rule_id="workspace_edit_v1",
            ),
            operation_id="first",
        )
        await session.append_message(
            Message(role="tool", content="written", tool_call_id="reused", tool_name="write"),
            operation_id="first",
        )
        await session.append_message(
            Message(
                role="tool",
                content="not automatically approved",
                tool_call_id="reused",
                tool_name="write",
            ),
            operation_id="second",
        )

    anyio.run(write)
    reopened = store.load(session.path)
    messages = reopened.read_message_page().messages
    assert [message.auto_approved for message in messages] == [False, True, False]
    exact = reopened.read_message_page(entry_ids=(messages[1].entry_id,), full_content=True)
    assert exact.messages[0].auto_approved


def test_abandoned_branch_auto_decision_is_not_inherited(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        root = await session.append_message(Message(role="user", content="edit"))
        abandoned = await session.append_event(
            ToolApprovalResolved(
                call_id="same",
                name="write",
                approved=True,
                source="auto",
                rule_id="workspace_edit_v1",
            )
        )
        await session.select_active_leaf(root.id, expected_active_leaf_id=abandoned.id)
        await session.append_message(
            Message(role="tool", content="not auto", tool_call_id="same", tool_name="write")
        )

    anyio.run(write)
    assert not any(message.auto_approved for message in session.read_message_page().messages)
