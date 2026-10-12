# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.cli.rpc_mode_support import (
    create_two_turn_session,
)
from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization
from wisp.agent.transcript_repair import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import ToolCallSnapshot
from wisp.sessions.entries import (
    MessageSessionEntry,
)


def test_rpc_mode_reports_sessions_catalog(tmp_path: Path) -> None:
    session = create_two_turn_session(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"sessions-1","type":"get_sessions","limit":10}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    report = next(record for record in records if record["type"] == "rpc.sessions")
    assert report["command_id"] == "sessions-1"
    assert "schema_version" not in report
    assert report["selected_session_id"] is None
    assert report["selected_session_path"] is None
    assert len(report["sessions"]) == 1
    summary = report["sessions"][0]
    assert summary["session_id"] == session.session_id
    assert summary["session_path"] == str(session.path)
    assert summary["entry_count"] == 4
    assert summary["active_leaf_id"] == session.read_entries()[-1].id


def test_rpc_mode_select_session_then_reads_messages(tmp_path: Path) -> None:
    session = create_two_turn_session(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    selected = next(record for record in records if record["type"] == "rpc.session.selected")
    assert "schema_version" not in selected
    assert selected["command_id"] == "select-1"
    assert selected["session_id"] == session.session_id
    assert selected["session_path"] == str(session.path)
    assert selected["entry_count"] == 4

    report = next(record for record in records if record["type"] == "rpc.messages")
    assert "schema_version" not in report
    assert report["session_id"] == session.session_id
    assert [message["content"] for message in report["messages"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]
    finished = [
        (record["command_id"], record["command_type"], record["ok"])
        for record in records
        if record["type"] == "rpc.command.finished"
    ]
    assert finished == [
        ("select-1", "select_session", True),
        ("messages-1", "get_messages", True),
    ]


def test_rpc_mode_sets_selected_and_explicit_session_names(tmp_path: Path) -> None:
    selected_session = create_two_turn_session(tmp_path)
    other_session = create_two_turn_session(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": selected_session.session_id,
                }
            )
            + "\n"
            '{"id":"name-1","type":"set_session_name","name":"  Roadmap\\r\\nCleanup  "}\n'
            '{"id":"sessions-1","type":"get_sessions","limit":10}\n'
            + json.dumps(
                {
                    "id": "clear-other",
                    "type": "set_session_name",
                    "session_id": other_session.session_id,
                    "name": "",
                }
            )
            + "\n"
            '{"id":"sessions-2","type":"get_sessions","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    name_events = [record for record in records if record["type"] == "rpc.session.name_changed"]
    assert len(name_events) == 2
    assert all("schema_version" not in event for event in name_events)
    assert name_events[0]["session_id"] == selected_session.session_id
    assert name_events[0]["previous_name"] is None
    assert name_events[0]["name"] == "Roadmap Cleanup"
    assert name_events[1]["session_id"] == other_session.session_id
    assert name_events[1]["name"] is None

    final_catalog = [record for record in records if record["type"] == "rpc.sessions"][-1]
    assert final_catalog["selected_session_id"] == selected_session.session_id
    assert final_catalog["selected_session_name"] == "Roadmap Cleanup"
    summaries = {summary["session_id"]: summary for summary in final_catalog["sessions"]}
    assert summaries[selected_session.session_id]["name"] == "Roadmap Cleanup"
    assert summaries[other_session.session_id]["name"] is None
    assert selected_session.read_name() == "Roadmap Cleanup"
    assert other_session.read_name() is None


def test_rpc_mode_clones_selected_session_then_reads_clone(tmp_path: Path) -> None:
    source = create_two_turn_session(tmp_path)
    source_before = source.path.read_bytes()
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": source.session_id,
                }
            )
            + "\n"
            '{"id":"clone-1","type":"clone_session"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert all("schema_version" not in record for record in records)
    cloned = next(record for record in records if record["type"] == "rpc.session.cloned")
    assert cloned["command_id"] == "clone-1"
    assert cloned["source_session_id"] == source.session_id
    assert cloned["source_session_path"] == str(source.path)
    assert cloned["session_id"] != source.session_id
    assert cloned["entry_count"] == 4
    assert cloned["active_leaf_id"] == source.read_active_leaf_id()

    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["session_id"] == cloned["session_id"]
    assert report["session_path"] == cloned["session_path"]
    assert [message["content"] for message in report["messages"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]
    assert Path(cloned["session_path"]).is_file()
    assert source.path.read_bytes() == source_before


def test_rpc_mode_first_message_fork_persists_after_edited_prompt(tmp_path: Path) -> None:
    source = create_two_turn_session(tmp_path)
    selected_entry = source.read_entries()[0]
    assert isinstance(selected_entry, MessageSessionEntry)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": source.session_id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "fork-1",
                    "type": "fork_session",
                    "entry_id": selected_entry.id,
                }
            )
            + "\n"
            '{"id":"prompt-1","type":"prompt","prompt":"edited first"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert all("schema_version" not in record for record in records)
    forked = next(record for record in records if record["type"] == "rpc.session.forked")
    assert forked["command_id"] == "fork-1"
    assert forked["source_session_id"] == source.session_id
    assert forked["selected_entry_id"] == selected_entry.id
    assert forked["selected_prompt"] == "first"
    assert forked["entry_count"] == 0
    assert forked["active_leaf_id"] is None

    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["session_id"] == forked["session_id"]
    assert [message["content"] for message in report["messages"]][-2:] == [
        "edited first",
        "fake response to: edited first",
    ]
    assert Path(forked["session_path"]).is_file()


def test_rpc_mode_pages_navigates_and_resubmits_session_tree_prompt(
    tmp_path: Path,
) -> None:
    session = create_two_turn_session(tmp_path)
    entries = session.read_entries()
    selected = entries[2]
    assert isinstance(selected, MessageSessionEntry)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"tree-empty","type":"get_session_tree"}\n'
            + json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            '{"id":"tree-1","type":"get_session_tree","limit":2}\n'
            + json.dumps(
                {
                    "id": "tree-2",
                    "type": "get_session_tree",
                    "limit": 10,
                    "after_entry_id": entries[1].id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "navigate-1",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                }
            )
            + "\n"
            '{"id":"prompt-1","type":"prompt","prompt":"edited second"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert all("schema_version" not in record for record in records)
    tree_reports = [record for record in records if record["type"] == "rpc.session.tree"]
    assert tree_reports[0]["session_id"] is None
    assert tree_reports[0]["session_path"] is None
    assert tree_reports[0]["nodes"] == []
    assert tree_reports[1]["total_node_count"] == 4
    assert [node["entry_id"] for node in tree_reports[1]["nodes"]] == [
        entries[0].id,
        entries[1].id,
    ]
    assert tree_reports[1]["truncated"] is True
    assert tree_reports[1]["next_after_entry_id"] == entries[1].id
    assert [node["entry_id"] for node in tree_reports[2]["nodes"]] == [
        entries[2].id,
        entries[3].id,
    ]
    assert tree_reports[2]["truncated"] is False
    assert tree_reports[2]["next_after_entry_id"] is None

    navigated = next(record for record in records if record["type"] == "rpc.session.tree.navigated")
    assert navigated["selected_entry_id"] == selected.id
    assert navigated["previous_active_leaf_id"] == entries[3].id
    assert navigated["active_leaf_id"] == entries[1].id
    assert navigated["editor_text"] == "second"
    assert navigated["changed"] is True
    assert navigated["entry_count"] == 5

    messages = next(record for record in records if record["type"] == "rpc.messages")
    assert [
        message["content"] for message in messages["messages"] if message["role"] != "system"
    ] == [
        "first",
        "answer one",
        "edited second",
        "fake response to: edited second",
    ]
    assert [node["preview"] for report in tree_reports[1:] for node in report["nodes"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]

    resumed = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-2",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            '{"id":"messages-2","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert resumed.exit_code == 0, resumed.output
    resumed_records = _jsonl_records(resumed.stdout)
    resumed_messages = next(
        record for record in resumed_records if record["type"] == "rpc.messages"
    )
    assert [
        message["content"]
        for message in resumed_messages["messages"]
        if message["role"] != "system"
    ] == [
        "first",
        "answer one",
        "edited second",
        "fake response to: edited second",
    ]


def test_rpc_mode_unreverts_navigation_after_process_restart(tmp_path: Path) -> None:
    session = create_two_turn_session(tmp_path)
    entries = session.read_entries()
    selected = entries[2]
    leaf = entries[-1]
    assert isinstance(selected, MessageSessionEntry)
    assert isinstance(leaf, MessageSessionEntry)

    first = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {"id": "select-1", "type": "select_session", "session_id": session.session_id}
            )
            + "\n"
            + json.dumps(
                {
                    "id": "navigate-1",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                }
            )
            + "\n"
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {"id": "select-2", "type": "select_session", "session_id": session.session_id}
            )
            + "\n"
            '{"id":"unrevert-1","type":"unrevert_session_tree"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert second.exit_code == 0, second.output
    records = _jsonl_records(second.stdout)
    unreverted = next(
        record for record in records if record["type"] == "rpc.session.tree.unreverted"
    )
    assert "schema_version" not in unreverted
    assert unreverted["active_leaf_id"] == leaf.id
    report = next(record for record in records if record["type"] == "rpc.messages")
    assert [message["content"] for message in report["messages"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]


def test_rpc_tree_navigation_repairs_incomplete_tool_boundary_before_prompt(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> MessageSessionEntry:
        await session.append_message(Message(role="user", content="old prompt"))
        assistant = await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        await session.append_message(
            Message(
                role="tool",
                content="old result",
                tool_call_id="call-1",
                tool_name="read",
            )
        )
        return assistant

    assistant = anyio.run(write)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "navigate-1",
                    "type": "navigate_session_tree",
                    "entry_id": assistant.id,
                }
            )
            + "\n"
            '{"id":"prompt-1","type":"prompt","prompt":"continue"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    navigated = next(record for record in records if record["type"] == "rpc.session.tree.navigated")
    assert navigated["active_leaf_id"] == assistant.id
    assert navigated["editor_text"] is None
    messages = next(record for record in records if record["type"] == "rpc.messages")
    active = [message for message in messages["messages"] if message["role"] != "system"]
    assert [message["role"] for message in active] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert active[2]["content"] == INTERRUPTED_TOOL_RESULT_TEXT
    assert active[2]["tool_call_id"] == "call-1"
    assert active[2]["is_error"] is True
    assert active[-2]["content"] == "continue"


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
