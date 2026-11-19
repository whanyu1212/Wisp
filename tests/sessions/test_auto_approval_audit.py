"""Automatic decisions survive reload without leaking across branches or reused IDs."""

from __future__ import annotations

from pathlib import Path

import anyio

from wisp.events import ToolApprovalResolved, ToolApprovalReviewed
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


def test_owned_process_control_note_survives_reload(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    reason = "Auto-approved: control of this session's sandboxed job."

    async def write() -> None:
        await session.append_message(Message(role="user", content="stop the test job"))
        await session.append_event(
            ToolApprovalResolved(
                call_id="cancel-job",
                name="bash",
                approved=True,
                source="auto",
                rule_id="sandbox_process_v1",
                reason=reason,
            )
        )
        await session.append_message(
            Message(role="tool", content="cancelled", tool_call_id="cancel-job", tool_name="bash")
        )

    anyio.run(write)
    result = store.load(session.path).read_message_page().messages[-1]
    assert result.auto_approved and result.approval_note == reason


def test_late_review_and_unused_grant_do_not_leak_to_a_reused_id_in_another_run(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="first"))
        await session.append_event(
            ToolApprovalReviewed(
                call_id="same", name="bash", verdict="allow", reason="Previous tests"
            )
        )
        await session.append_event(
            ToolApprovalResolved(
                call_id="same",
                name="bash",
                approved=True,
                source="auto",
                rule_id="command_review_v1",
                reason="Auto-approved: command review",
            )
        )
        # No operation IDs: raw CodingSession/SDK runs also need a run boundary.
        await session.append_message(Message(role="user", content="second"))
        await session.append_message(
            Message(role="tool", content="manual", tool_call_id="same", tool_name="bash")
        )

    anyio.run(write)
    result = session.read_message_page().messages[-1]
    assert not result.auto_approved and result.approval_note is None


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
