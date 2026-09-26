from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from wisp.agent.messages import CompactionRecord, Message
from wisp.events import (
    ErrorEvent,
    ToolCallSnapshot,
)
from wisp.sessions import (
    SessionTreePage,
    pagination,
)
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
)
from wisp.sessions.errors import (
    SessionNavigationCancelledError,
    SessionUnrevertUnavailableError,
    StaleSessionTreeError,
)
from wisp.sessions.jsonl import (
    JsonlSessionStore,
    SessionError,
)


def test_tree_page_returns_empty_identified_reserved_session(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    page = session.read_tree_page()

    assert page == SessionTreePage(
        session_id=session.session_id,
        path=session.path,
        active_leaf_id=None,
        total_node_count=0,
        nodes=(),
        truncated=False,
        next_after_entry_id=None,
    )
    assert not session.path.exists()


def test_tree_page_includes_inactive_nodes_in_append_order(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    long_prompt = "🙂" * 200

    async def write() -> tuple[str, ...]:
        root = await session.append_message(Message(role="user", content=long_prompt))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        abandoned = await session.append_message(Message(role="user", content="abandoned"))
        abandoned_answer = await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(call_id="call-1", name="read", arguments={"path": "secret"}),
                ),
                finish_reason="tool_calls",
            )
        )
        await session.select_active_leaf(
            answer.id,
            expected_active_leaf_id=abandoned_answer.id,
        )
        branch = await session.append_message(Message(role="user", content="branch"))
        event = await session.append_event(ErrorEvent(message="hidden event payload"))
        branch_answer = await session.append_message(
            Message(role="assistant", content="branch answer")
        )
        return (
            root.id,
            answer.id,
            abandoned.id,
            abandoned_answer.id,
            branch.id,
            event.id,
            branch_answer.id,
        )

    node_ids = anyio.run(write)

    first = session.read_tree_page(limit=4)
    second = session.read_tree_page(limit=4, after_entry_id=first.next_after_entry_id)
    exhausted = session.read_tree_page(limit=4, after_entry_id=node_ids[-1])

    assert [node.entry_id for node in first.nodes] == list(node_ids[:4])
    assert [node.entry_id for node in second.nodes] == list(node_ids[4:])
    assert first.total_node_count == second.total_node_count == 7
    assert first.active_leaf_id == second.active_leaf_id == node_ids[-1]
    assert first.truncated is True
    assert first.next_after_entry_id == node_ids[3]
    assert second.truncated is False
    assert second.next_after_entry_id is None
    assert exhausted.total_node_count == 7
    assert exhausted.nodes == ()
    assert exhausted.truncated is False
    assert exhausted.next_after_entry_id is None
    assert first.nodes[0].preview_truncated is True
    assert len(first.nodes[0].preview.encode("utf-8")) <= 512
    assert first.nodes[0].preview.encode("utf-8").decode("utf-8") == first.nodes[0].preview
    assert first.nodes[3].preview == "[tool calls: read]"
    assert "secret" not in first.nodes[3].preview
    assert second.nodes[1].preview == "error"
    assert "hidden event payload" not in second.nodes[1].preview


def test_tree_compaction_preview_is_utf8_bounded() -> None:
    entry = CompactionSessionEntry(
        session_id="session-1",
        compaction=CompactionRecord(
            summary="漢🙂" * 300,
            replaced_entry_ids=("entry-1",),
            provider="fake",
        ),
    )

    node = pagination.session_tree_node_summary(entry)  # noqa: SLF001

    assert node.kind == "compaction"
    assert node.role is None
    assert node.preview_truncated is True
    assert len(node.preview.encode("utf-8")) <= 512
    assert node.preview.encode("utf-8").decode("utf-8") == node.preview


def test_tree_page_rejects_invalid_limits_and_unknown_cursor(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        await session.append_message(Message(role="user", content="hello"))

    anyio.run(write)

    with pytest.raises(ValueError, match="between 1 and 500"):
        session.read_tree_page(limit=0)
    with pytest.raises(ValueError, match="between 1 and 500"):
        session.read_tree_page(limit=501)
    with pytest.raises(ValueError, match="cursor must be non-empty"):
        session.read_tree_page(after_entry_id="")
    with pytest.raises(SessionError, match="Session tree cursor not found: missing"):
        session.read_tree_page(after_entry_id="missing")


def test_navigation_restores_user_prompt_and_selects_other_nodes(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()
    exact_prompt = "edit this 🙂\nwithout clipping" * 100

    async def navigate() -> tuple[str, str, str]:
        root = await session.append_message(Message(role="user", content="root"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content=exact_prompt))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))

        restored = await session.navigate_tree(
            selected.id,
            expected_active_leaf_id=leaf.id,
            operation_id="navigate-user",
        )
        assert restored.selected_entry_id == selected.id
        assert restored.previous_active_leaf_id == leaf.id
        assert restored.active_leaf_id == answer.id
        assert restored.editor_text == exact_prompt
        assert restored.changed is True
        assert restored.entry_count == 5

        current = await session.navigate_tree(
            answer.id,
            expected_active_leaf_id=answer.id,
            operation_id="navigate-current",
        )
        assert current.active_leaf_id == answer.id
        assert current.editor_text is None
        assert current.changed is False
        assert current.entry_count == 5

        reset = await session.navigate_tree(
            root.id,
            expected_active_leaf_id=answer.id,
            operation_id="navigate-root",
        )
        assert reset.active_leaf_id is None
        assert reset.editor_text == "root"
        assert reset.changed is True
        assert reset.entry_count == 6
        return root.id, answer.id, leaf.id

    root_id, answer_id, old_leaf_id = anyio.run(navigate)

    assert session.read_active_leaf_id() is None
    assert session.read_context_messages() == ()
    selections = [
        entry for entry in session.read_entries() if isinstance(entry, ActiveLeafSessionEntry)
    ]
    assert [(entry.previous_leaf_id, entry.active_leaf_id) for entry in selections] == [
        (old_leaf_id, answer_id),
        (answer_id, None),
    ]
    assert [entry.operation_id for entry in selections] == [
        "navigate-user",
        "navigate-root",
    ]
    assert root_id != answer_id


def test_navigation_rejects_missing_and_stale_entries(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def navigate() -> None:
        leaf = await session.append_message(Message(role="user", content="root"))
        with pytest.raises(SessionError, match="Session tree entry not found: missing"):
            await session.navigate_tree(
                "missing",
                expected_active_leaf_id=leaf.id,
            )
        with pytest.raises(StaleSessionTreeError, match="expected active leaf 'stale'"):
            await session.navigate_tree(
                leaf.id,
                expected_active_leaf_id="stale",
            )
        with pytest.raises(ValueError, match="entry id must be non-empty"):
            await session.navigate_tree("", expected_active_leaf_id=leaf.id)

    anyio.run(navigate)

    assert len(session.read_entries()) == 1


def test_navigation_honors_cancellation_at_commit_boundary(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def navigate() -> None:
        selected = await session.append_message(Message(role="user", content="root"))
        active = await session.append_message(Message(role="assistant", content="answer"))
        with pytest.raises(
            SessionNavigationCancelledError,
            match="Session tree navigation cancelled",
        ):
            await session.navigate_tree(
                selected.id,
                expected_active_leaf_id=active.id,
                cancel_requested=lambda: True,
            )
        assert session.read_active_leaf_id() == active.id

    anyio.run(navigate)

    assert len(session.read_entries()) == 2


def test_unrevert_survives_restart_and_ignores_name_metadata(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()

    async def navigate() -> tuple[str, str, tuple[Message, ...]]:
        await session.append_message(Message(role="user", content="first"))
        answer = await session.append_message(Message(role="assistant", content="answer"))
        selected = await session.append_message(Message(role="user", content="second"))
        leaf = await session.append_message(Message(role="assistant", content="old answer"))
        original_messages = session.read_context_messages()
        await session.navigate_tree(
            selected.id,
            expected_active_leaf_id=leaf.id,
            operation_id="navigate",
        )
        await session.set_name("renamed")
        return answer.id, leaf.id, original_messages

    answer_id, leaf_id, original_messages = anyio.run(navigate)
    reopened = store.load(session.path)

    async def unrevert() -> None:
        result = await reopened.unrevert_tree(
            expected_active_leaf_id=answer_id,
            operation_id="unrevert",
        )
        assert result.previous_active_leaf_id == answer_id
        assert result.active_leaf_id == leaf_id
        assert result.entry_count == 7

    anyio.run(unrevert)

    assert reopened.read_active_leaf_id() == leaf_id
    assert reopened.read_context_messages() == original_messages
    transitions = [
        entry for entry in reopened.read_entries() if isinstance(entry, ActiveLeafSessionEntry)
    ]
    assert [entry.reason for entry in transitions] == ["navigation", "unrevert"]
    assert transitions[0].selected_entry_id is not None
    assert transitions[1].source_transition_id == transitions[0].id


def test_unrevert_is_invalidated_by_new_history(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def mutate() -> None:
        selected = await session.append_message(Message(role="user", content="first"))
        leaf = await session.append_message(Message(role="assistant", content="answer"))
        await session.navigate_tree(selected.id, expected_active_leaf_id=leaf.id)
        active_leaf_id = session.read_active_leaf_id()
        await session.append_message(Message(role="user", content="replacement"))
        entry_count = len(session.read_entries())
        with pytest.raises(
            SessionUnrevertUnavailableError,
            match="No explicit session-tree navigation",
        ):
            await session.unrevert_tree(expected_active_leaf_id=session.read_active_leaf_id())
        assert len(session.read_entries()) == entry_count
        assert session.read_active_leaf_id() != active_leaf_id

    anyio.run(mutate)


def test_unrevert_rejects_system_transition_and_cancellation(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def exercise() -> None:
        selected = await session.append_message(Message(role="user", content="first"))
        leaf = await session.append_message(Message(role="assistant", content="answer"))
        await session.select_active_leaf(
            selected.id,
            expected_active_leaf_id=leaf.id,
            operation_id="system",
        )
        with pytest.raises(SessionUnrevertUnavailableError):
            await session.unrevert_tree(expected_active_leaf_id=selected.id)

        await session.navigate_tree(
            leaf.id,
            expected_active_leaf_id=selected.id,
            operation_id="navigate",
        )
        entry_count = len(session.read_entries())
        with pytest.raises(SessionNavigationCancelledError):
            await session.unrevert_tree(
                expected_active_leaf_id=leaf.id,
                cancel_requested=lambda: True,
            )
        assert len(session.read_entries()) == entry_count
        assert session.read_active_leaf_id() == leaf.id

    anyio.run(exercise)


def test_active_leaf_transition_metadata_is_strict() -> None:
    with pytest.raises(ValidationError, match="require selected_entry_id only"):
        ActiveLeafSessionEntry(
            session_id="session",
            previous_leaf_id="old",
            active_leaf_id="new",
            reason="navigation",
        )
    with pytest.raises(ValidationError, match="require source_transition_id only"):
        ActiveLeafSessionEntry(
            session_id="session",
            previous_leaf_id="old",
            active_leaf_id="new",
            reason="unrevert",
            selected_entry_id="message",
        )
