from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import anyio
import pytest
from anyio.abc import Process
from pydantic import ValidationError
from pytest import MonkeyPatch

from wisp.events import (
    EVENT_SCHEMA_VERSION,
    ProjectConfigApplied,
    ProviderRetrying,
    QueueItemsRemoved,
    RpcCommandArgument,
    RpcCommandDescriptor,
    RpcCommandFinished,
    RpcCommandsReported,
    RpcCommandStarted,
    RpcMcpServerSnapshot,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
    RpcMessageSnapshot,
    RpcMessagesReported,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionNameChanged,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSessionSummary,
    RpcSessionTreeNavigated,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
    RpcSessionTreeUnreverted,
    RpcSkillCatalogEntry,
    RpcSkillCatalogSnapshot,
    RpcSkillDiagnostic,
    RpcSkillsReported,
    RpcStateReported,
    RpcStateSnapshot,
    SkillCatalogUpdated,
    ToolExecutionEnded,
    ToolResultReady,
    TrustRequested,
    TrustResolved,
    wisp_event_from_json,
)
from wisp.rpc import (
    ClearQueueCommand,
    CloneSessionCommand,
    CompactCommand,
    ConfigureCommand,
    FollowUpCommand,
    ForkSessionCommand,
    GetCommandsCommand,
    GetMcpStatusCommand,
    GetMessagesCommand,
    GetQueueStateCommand,
    GetSessionsCommand,
    GetSessionStatsCommand,
    GetSessionTreeCommand,
    GetSkillsCommand,
    GetStateCommand,
    InitCommand,
    JsonlSubprocessRpcTransport,
    NavigateSessionTreeCommand,
    NewSessionCommand,
    PopQueueCommand,
    RpcController,
    RpcHandshakeError,
    RpcProtocolError,
    SelectSessionCommand,
    SetQueueModeCommand,
    SetSessionNameCommand,
    SteerCommand,
    UnrevertSessionTreeCommand,
)
from wisp.rpc import client as rpc_client_module
from wisp.rpc.commands import (
    MAX_RPC_COMMAND_ID_CHARS,
    ApprovalCommand,
    CancelCommand,
    PromptCommand,
    RpcCommand,
    ShutdownCommand,
    TrustCommand,
    rpc_command_from_json,
)
from wisp.rpc.protocol import LIVE_RPC_PROTOCOL_VERSION, RpcHandshakeRequest


class RecordingTransport:
    def __init__(self, events: list[object] | None = None) -> None:
        self.commands: list[RpcCommand] = []
        self._events = events or []
        self.closed = False

    async def send(self, command: RpcCommand) -> None:
        self.commands.append(command)

    async def close(self) -> None:
        self.closed = True

    def events(self) -> AsyncIterator[object]:
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event


def test_changelog_documents_current_event_schema() -> None:
    changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## Schema v{EVENT_SCHEMA_VERSION} — current" in changelog
    assert f"Events at schema v5 through v{EVENT_SCHEMA_VERSION} remain readable." in changelog


def test_rpc_commands_serialize_as_jsonl_and_parse() -> None:
    command = ApprovalCommand(
        id="approval-1",
        call_id="call-1",
        approved=False,
        reason="not safe",
    )

    line = command.to_json_line()

    assert line.endswith("\n")
    assert json.loads(line) == {
        "id": "approval-1",
        "type": "approval",
        "call_id": "call-1",
        "approved": False,
        "reason": "not safe",
    }
    assert rpc_command_from_json(line) == command


def test_rpc_commands_bound_ids_that_are_echoed_in_server_events() -> None:
    with pytest.raises(ValidationError, match="String should have at most 256 characters"):
        PromptCommand(id="x" * (MAX_RPC_COMMAND_ID_CHARS + 1), prompt="hello")


@pytest.mark.parametrize("instructions", [None, "Keep exact paths"])
def test_compact_command_serializes_as_jsonl_and_parses(
    instructions: str | None,
) -> None:
    command = CompactCommand(id="compact-1", instructions=instructions)

    line = command.to_json_line()

    expected: dict[str, object] = {"id": "compact-1", "type": "compact"}
    if instructions is not None:
        expected["instructions"] = instructions
    assert json.loads(line) == expected
    assert rpc_command_from_json(line) == command


def test_get_session_stats_command_serializes_as_jsonl_and_parses() -> None:
    command = GetSessionStatsCommand(id="stats-1")

    assert json.loads(command.to_json_line()) == {
        "id": "stats-1",
        "type": "get_session_stats",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_state_command_serializes_as_jsonl_and_parses() -> None:
    command = GetStateCommand(id="state-1")

    assert json.loads(command.to_json_line()) == {
        "id": "state-1",
        "type": "get_state",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_commands_command_serializes_as_jsonl_and_parses() -> None:
    command = GetCommandsCommand(id="commands-1")

    assert json.loads(command.to_json_line()) == {
        "id": "commands-1",
        "type": "get_commands",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_skills_command_serializes_as_jsonl_and_parses() -> None:
    command = GetSkillsCommand(id="skills-1")

    assert json.loads(command.to_json_line()) == {
        "id": "skills-1",
        "type": "get_skills",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_mcp_status_command_serializes_as_jsonl_and_parses() -> None:
    command = GetMcpStatusCommand(id="mcp-1")

    assert json.loads(command.to_json_line()) == {
        "id": "mcp-1",
        "type": "get_mcp_status",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_mcp_status_event_round_trips_over_json_transport() -> None:
    event = RpcMcpStatusReported(
        command_id="mcp-1",
        status=RpcMcpStatusSnapshot(
            servers=(
                RpcMcpServerSnapshot(
                    name="docs",
                    status="connected",
                    tool_names=("mcp__docs__search",),
                ),
            )
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 30"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 29}).model_dump_json())


def test_skill_catalog_events_round_trip_over_json_transport() -> None:
    catalog = RpcSkillCatalogSnapshot(
        entries=(
            RpcSkillCatalogEntry(
                name="review",
                description="Review [b]literal[/b] output",
                source="user:wisp",
            ),
            RpcSkillCatalogEntry(
                name="wisp-development",
                description="Develop Wisp",
                source="package:wisp",
            ),
        ),
        diagnostics=(
            RpcSkillDiagnostic(
                code="invalid-yaml",
                severity="warning",
                message="broken [literal] metadata",
                source="project:wisp",
                path=Path("/project/.agents/skills/bad/SKILL.md"),
            ),
        ),
        project_trusted=True,
    )
    events = (
        RpcSkillsReported(command_id="skills-1", catalog=catalog),
        SkillCatalogUpdated(catalog=catalog),
    )

    assert tuple(wisp_event_from_json(event.model_dump_json()) for event in events) == events
    for event_type in (RpcSkillsReported, SkillCatalogUpdated):
        kwargs: dict[str, object] = {"catalog": catalog, "schema_version": 30}
        if event_type is RpcSkillsReported:
            kwargs["command_id"] = "skills-1"
        with pytest.raises(
            ValidationError, match="Package skill sources require schema_version 31"
        ):
            event_type(**kwargs)

    for event in events:
        legacy_payload = json.loads(event.model_dump_json())
        legacy_payload["schema_version"] = 30
        with pytest.raises(ValueError, match="Package skill sources require schema_version 31"):
            wisp_event_from_json(json.dumps(legacy_payload))


def test_get_messages_command_serializes_as_jsonl_and_parses() -> None:
    command = GetMessagesCommand(
        id="messages-1",
        session_id="session-1",
        limit=25,
        before_entry_id="entry-1",
    )

    assert json.loads(command.to_json_line()) == {
        "id": "messages-1",
        "type": "get_messages",
        "session_id": "session-1",
        "limit": 25,
        "before_entry_id": "entry-1",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_messages_command_serializes_forward_cursor() -> None:
    command = GetMessagesCommand(id="messages-2", after_entry_id="entry-1")

    assert json.loads(command.to_json_line()) == {
        "id": "messages-2",
        "type": "get_messages",
        "limit": 200,
        "after_entry_id": "entry-1",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_messages_command_serializes_active_prompt_read_opt_in() -> None:
    command = GetMessagesCommand(id="messages-live", allow_during_prompt=True)

    assert json.loads(command.to_json_line()) == {
        "id": "messages-live",
        "type": "get_messages",
        "limit": 200,
        "allow_during_prompt": True,
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_messages_command_serializes_exact_full_content_lookup() -> None:
    command = GetMessagesCommand(
        id="messages-detail",
        session_id="session-1",
        limit=1,
        entry_ids=("tool-result-1",),
        complete_structure=True,
        full_content=True,
        allow_during_prompt=True,
    )

    assert json.loads(command.to_json_line()) == {
        "id": "messages-detail",
        "type": "get_messages",
        "session_id": "session-1",
        "limit": 1,
        "entry_ids": ["tool-result-1"],
        "complete_structure": True,
        "full_content": True,
        "allow_during_prompt": True,
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_messages_command_rejects_invalid_bounds() -> None:
    with pytest.raises(ValidationError):
        GetMessagesCommand(limit=True)
    with pytest.raises(ValidationError):
        GetMessagesCommand(limit="2")
    with pytest.raises(ValidationError):
        GetMessagesCommand(limit=0)
    with pytest.raises(ValidationError):
        GetMessagesCommand(limit=501)
    with pytest.raises(ValidationError):
        GetMessagesCommand(session_id="")
    with pytest.raises(ValidationError):
        GetMessagesCommand(before_entry_id="")
    with pytest.raises(ValidationError):
        GetMessagesCommand(after_entry_id="")
    with pytest.raises(ValidationError, match="mutually exclusive"):
        GetMessagesCommand(before_entry_id="before", after_entry_id="after")
    with pytest.raises(ValidationError, match="cannot be combined"):
        GetMessagesCommand(entry_ids=("entry",), before_entry_id="before")
    with pytest.raises(ValidationError, match="requires exact entry IDs"):
        GetMessagesCommand(full_content=True)
    with pytest.raises(ValidationError, match="exactly one entry ID"):
        GetMessagesCommand(entry_ids=("one", "two"), full_content=True)
    with pytest.raises(ValidationError, match="must be unique"):
        GetMessagesCommand(entry_ids=("entry", "entry"))


def test_get_sessions_command_serializes_as_jsonl_and_parses() -> None:
    command = GetSessionsCommand(id="sessions-1", limit=25)

    assert json.loads(command.to_json_line()) == {
        "id": "sessions-1",
        "type": "get_sessions",
        "limit": 25,
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_get_sessions_command_rejects_invalid_bounds() -> None:
    with pytest.raises(ValidationError):
        GetSessionsCommand(limit=True)
    with pytest.raises(ValidationError):
        GetSessionsCommand(limit="2")
    with pytest.raises(ValidationError):
        GetSessionsCommand(limit=-1)
    with pytest.raises(ValidationError):
        GetSessionsCommand(limit=201)


def test_select_session_command_serializes_as_jsonl_and_parses() -> None:
    command = SelectSessionCommand(id="select-1", session_id="session-1")

    assert json.loads(command.to_json_line()) == {
        "id": "select-1",
        "type": "select_session",
        "session_id": "session-1",
    }
    assert rpc_command_from_json(command.to_json_line()) == command


def test_select_session_command_rejects_invalid_id() -> None:
    with pytest.raises(ValidationError):
        SelectSessionCommand(session_id="")


def test_session_derivation_commands_serialize_as_jsonl_and_parse() -> None:
    clone = CloneSessionCommand(id="clone-1")
    fork = ForkSessionCommand(id="fork-1", entry_id="entry-1")

    assert json.loads(clone.to_json_line()) == {
        "id": "clone-1",
        "type": "clone_session",
    }
    assert json.loads(fork.to_json_line()) == {
        "id": "fork-1",
        "type": "fork_session",
        "entry_id": "entry-1",
    }
    assert rpc_command_from_json(clone.to_json_line()) == clone
    assert rpc_command_from_json(fork.to_json_line()) == fork


def test_fork_session_command_rejects_empty_entry_id() -> None:
    with pytest.raises(ValidationError):
        ForkSessionCommand(entry_id="")


def test_session_tree_commands_serialize_as_jsonl_and_parse() -> None:
    get_tree = GetSessionTreeCommand(
        id="tree-1",
        limit=25,
        after_entry_id="entry-1",
    )
    navigate = NavigateSessionTreeCommand(id="navigate-1", entry_id="entry-2")
    unrevert = UnrevertSessionTreeCommand(id="unrevert-1")

    assert json.loads(get_tree.to_json_line()) == {
        "id": "tree-1",
        "type": "get_session_tree",
        "limit": 25,
        "after_entry_id": "entry-1",
    }
    assert json.loads(navigate.to_json_line()) == {
        "id": "navigate-1",
        "type": "navigate_session_tree",
        "entry_id": "entry-2",
    }
    assert json.loads(unrevert.to_json_line()) == {
        "id": "unrevert-1",
        "type": "unrevert_session_tree",
    }
    assert rpc_command_from_json(get_tree.to_json_line()) == get_tree
    assert rpc_command_from_json(navigate.to_json_line()) == navigate
    assert rpc_command_from_json(unrevert.to_json_line()) == unrevert


def test_session_tree_commands_reject_invalid_fields() -> None:
    for limit in (True, "2", 0, 501):
        with pytest.raises(ValidationError):
            GetSessionTreeCommand(limit=limit)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        GetSessionTreeCommand(after_entry_id="")
    with pytest.raises(ValidationError):
        NavigateSessionTreeCommand(entry_id="")


def test_set_session_name_command_serializes_as_jsonl_and_parses() -> None:
    command = SetSessionNameCommand(
        id="name-1",
        name="  first\r\nname  ",
        session_id="session-1",
    )

    assert json.loads(command.to_json_line()) == {
        "id": "name-1",
        "type": "set_session_name",
        "name": "  first\r\nname  ",
        "session_id": "session-1",
    }
    assert rpc_command_from_json(command.to_json_line()) == command
    assert json.loads(SetSessionNameCommand(id="clear-1", name="").to_json_line()) == {
        "id": "clear-1",
        "type": "set_session_name",
        "name": "",
    }
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        SetSessionNameCommand(name="ok", session_id="")


def test_rpc_state_report_round_trips_only_at_schema_v16() -> None:
    event = RpcStateReported(
        command_id="state-1",
        state=RpcStateSnapshot(
            provider="fake",
            model="fake-model",
            effort=None,
            auto_compaction_enabled=True,
            steering_mode="one_at_a_time",
            follow_up_mode="one_at_a_time",
            pending_steering_count=0,
            pending_follow_up_count=0,
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 16"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 15}).model_dump_json())


def test_rpc_state_agent_mode_is_backward_compatible_before_schema_v27() -> None:
    event = RpcStateReported(
        command_id="state-1",
        state=RpcStateSnapshot(
            provider="fake",
            mode="plan",
            auto_compaction_enabled=True,
            steering_mode="one_at_a_time",
            follow_up_mode="one_at_a_time",
            pending_steering_count=0,
            pending_follow_up_count=0,
        ),
    )
    legacy = event.model_copy(update={"schema_version": 26})
    payload = json.loads(legacy.model_dump_json())

    assert "mode" not in payload["state"]
    restored = wisp_event_from_json(json.dumps(payload))
    assert isinstance(restored, RpcStateReported)
    assert restored.state.mode == "build"


def test_rpc_commands_report_round_trips_only_at_schema_v23() -> None:
    event = RpcCommandsReported(
        command_id="commands-1",
        commands=(
            RpcCommandDescriptor(
                name="compact",
                title="Compact",
                description="Compact the session context",
                category="session",
                aliases=("cx",),
                slash_command="/compact",
                slash_aliases=("/cx",),
                arguments=(
                    RpcCommandArgument(
                        name="instructions",
                        description="Optional compaction guidance",
                    ),
                ),
                accepts_arguments=True,
                order=20,
            ),
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 23"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 22}).model_dump_json())


def test_rpc_messages_report_round_trips_only_at_schema_v17() -> None:
    event = RpcMessagesReported(
        command_id="messages-1",
        session_id="session-1",
        session_path=Path("/tmp/session.jsonl"),
        active_leaf_id="entry-2",
        messages=(
            RpcMessageSnapshot(
                entry_id="entry-1",
                created_at=datetime(2026, 7, 23, tzinfo=UTC),
                role="assistant",
                content="running",
                content_original_bytes=7,
                tool_calls=(
                    RpcMessageToolCallSnapshot(
                        call_id="call-1",
                        name="bash",
                        arguments={"command": "pwd"},
                        arguments_original_bytes=17,
                    ),
                ),
            ),
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 17"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 16}).model_dump_json())


def test_rpc_messages_tool_result_metadata_requires_schema_v22() -> None:
    event = RpcMessagesReported(
        command_id="messages-1",
        messages=(
            RpcMessageSnapshot(
                entry_id="entry-1",
                created_at=datetime(2026, 7, 28, tzinfo=UTC),
                role="tool",
                content="boom",
                content_original_bytes=4,
                tool_call_id="call-1",
                tool_name="bash",
                is_error=False,
                tool_result=RpcMessageToolResultSnapshot(
                    exit_code=1,
                    output_has_exit_status=True,
                    summary="command failed",
                    truncated=True,
                ),
            ),
        ),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    payload = json.loads(event.model_dump_json())
    payload["schema_version"] = 21
    with pytest.raises(ValueError, match="tool-result metadata requires schema_version 22"):
        wisp_event_from_json(json.dumps(payload))
    with pytest.raises(ValueError, match="valid only on tool messages"):
        RpcMessageSnapshot(
            entry_id="entry-2",
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
            role="assistant",
            content="not a tool",
            content_original_bytes=10,
            tool_result=RpcMessageToolResultSnapshot(summary="invalid"),
        )


def test_rpc_messages_tool_result_metadata_strips_from_legacy_serialization() -> None:
    event = RpcMessagesReported(
        command_id="messages-1",
        messages=(
            RpcMessageSnapshot(
                entry_id="entry-1",
                created_at=datetime(2026, 7, 28, tzinfo=UTC),
                role="tool",
                content="denied",
                content_original_bytes=6,
                tool_call_id="call-1",
                tool_name="write",
                is_error=True,
                tool_result=RpcMessageToolResultSnapshot(status="denied"),
            ),
        ),
    )
    legacy = event.model_copy(update={"schema_version": 21})

    payload = json.loads(legacy.model_dump_json())

    assert "tool_result" not in payload["messages"][0]
    assert wisp_event_from_json(json.dumps(payload)).schema_version == 21


def test_rpc_sessions_report_round_trips_only_at_schema_v18() -> None:
    event = RpcSessionsReported(
        command_id="sessions-1",
        sessions=(
            RpcSessionSummary(
                session_id="session-1",
                session_path=Path("/tmp/session-1.jsonl"),
                updated_at=datetime(2026, 7, 24, tzinfo=UTC),
                entry_count=3,
                active_leaf_id="entry-3",
            ),
        ),
        selected_session_id="session-1",
        selected_session_path=Path("/tmp/session-1.jsonl"),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 18"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 17}).model_dump_json())


def test_rpc_session_selected_round_trips_only_at_schema_v18() -> None:
    event = RpcSessionSelected(
        command_id="select-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        active_leaf_id="entry-3",
        entry_count=3,
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 18"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 17}).model_dump_json())


def test_rpc_session_derivation_events_round_trip_only_at_schema_v19() -> None:
    clone = RpcSessionCloned(
        command_id="clone-1",
        source_session_id="source",
        source_session_path=Path("/tmp/source.jsonl"),
        source_active_leaf_id="entry-2",
        session_id="clone",
        session_path=Path("/tmp/clone.jsonl"),
        active_leaf_id="entry-2",
        entry_count=2,
    )
    fork = RpcSessionForked(
        command_id="fork-1",
        source_session_id="source",
        source_session_path=Path("/tmp/source.jsonl"),
        source_active_leaf_id="entry-4",
        session_id="fork",
        session_path=Path("/tmp/fork.jsonl"),
        active_leaf_id="entry-2",
        entry_count=2,
        selected_entry_id="entry-3",
        selected_prompt="edit me",
    )

    assert wisp_event_from_json(clone.model_dump_json()) == clone
    assert wisp_event_from_json(fork.model_dump_json()) == fork
    for event in (clone, fork):
        with pytest.raises(ValueError, match="require schema_version 19"):
            wisp_event_from_json(event.model_copy(update={"schema_version": 18}).model_dump_json())


def test_rpc_session_derivation_events_reject_invalid_targets() -> None:
    fields = {
        "command_id": "clone-1",
        "source_session_id": "same",
        "source_session_path": Path("/tmp/source.jsonl"),
        "source_active_leaf_id": "entry-1",
        "session_id": "same",
        "session_path": Path("/tmp/clone.jsonl"),
        "active_leaf_id": "entry-1",
        "entry_count": 1,
    }
    with pytest.raises(ValidationError, match="must create a new session id"):
        RpcSessionCloned(**fields)
    with pytest.raises(ValidationError, match="require an active leaf"):
        RpcSessionCloned(**{**fields, "session_id": "clone", "active_leaf_id": None})


def test_rpc_session_tree_events_round_trip_only_at_schema_v20() -> None:
    node = RpcSessionTreeNode(
        entry_id="entry-1",
        parent_id=None,
        operation_id="prompt-1",
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        kind="message",
        role="user",
        preview="edit me",
    )
    report = RpcSessionTreeReported(
        command_id="tree-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        active_leaf_id="entry-1",
        total_node_count=2,
        nodes=(node,),
        truncated=True,
        next_after_entry_id="entry-1",
    )
    navigated = RpcSessionTreeNavigated(
        command_id="navigate-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        selected_entry_id="entry-1",
        previous_active_leaf_id="entry-2",
        active_leaf_id=None,
        editor_text="edit me",
        changed=True,
        entry_count=3,
    )

    assert wisp_event_from_json(report.model_dump_json()) == report
    assert wisp_event_from_json(navigated.model_dump_json()) == navigated
    for event in (report, navigated):
        with pytest.raises(ValueError, match="require schema_version 20"):
            wisp_event_from_json(event.model_copy(update={"schema_version": 19}).model_dump_json())


def test_rpc_session_tree_unrevert_round_trips_only_at_schema_v24() -> None:
    event = RpcSessionTreeUnreverted(
        command_id="unrevert-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        source_transition_id="navigation-1",
        previous_active_leaf_id="entry-1",
        active_leaf_id="entry-2",
        entry_count=5,
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 24"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 23}).model_dump_json())


def test_rpc_message_forward_cursor_round_trips_only_at_schema_v34() -> None:
    report = RpcMessagesReported(
        command_id="messages-1",
        truncated=True,
        next_after_entry_id="entry-1",
    )

    assert report.schema_version == 34
    assert wisp_event_from_json(report.model_dump_json()) == report

    legacy_without_cursor = RpcMessagesReported(
        command_id="messages-legacy",
        schema_version=33,
    )
    legacy_payload = json.loads(legacy_without_cursor.model_dump_json())
    assert "next_after_entry_id" not in legacy_payload
    assert wisp_event_from_json(json.dumps(legacy_payload)) == legacy_without_cursor

    legacy_payload["next_after_entry_id"] = "entry-1"
    legacy_payload["truncated"] = True
    with pytest.raises(ValueError, match="forward cursors require schema_version 34"):
        wisp_event_from_json(json.dumps(legacy_payload))

    with pytest.raises(ValidationError, match="forward cursors require schema_version 34"):
        RpcMessagesReported(
            command_id="messages-invalid",
            schema_version=33,
            truncated=True,
            next_after_entry_id="entry-1",
        )


def test_tool_failure_metadata_round_trips_only_at_schema_v33() -> None:
    event = ToolResultReady(
        call_id="call-1",
        name="grep",
        output="Invalid grep pattern\nRecovery: Retry with literal=true.",
        is_error=True,
        failure_code="invalid_pattern",
        retryable=True,
        recovery_hint="Retry with literal=true.",
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    legacy = event.model_copy(update={"schema_version": 32})
    payload = json.loads(legacy.model_dump_json())
    assert "failure_code" not in payload
    assert "retryable" not in payload
    assert "recovery_hint" not in payload

    payload["failure_code"] = "invalid_pattern"
    with pytest.raises(ValueError, match="Tool failure metadata requires schema_version 33"):
        wisp_event_from_json(json.dumps(payload))


def test_tool_failure_metadata_requires_an_error_and_failure_code() -> None:
    with pytest.raises(ValidationError, match="requires is_error=true"):
        ToolResultReady(
            call_id="call-1",
            name="grep",
            output="bad pattern",
            is_error=False,
            failure_code="invalid_pattern",
        )
    with pytest.raises(ValidationError, match="requires a tool failure code"):
        ToolResultReady(
            call_id="call-1",
            name="grep",
            output="bad pattern",
            is_error=True,
            retryable=True,
        )


@pytest.mark.parametrize("event_type", ["tool.result", "tool.execution.ended"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("process_id", "p123"),
        ("process_state", "running"),
        ("process_error", "failed"),
        ("stdout", "out"),
        ("stderr", "err"),
        ("stdout_truncated", True),
        ("stderr_truncated", True),
        ("stdout_dropped_bytes", 1),
        ("stderr_dropped_bytes", 1),
    ],
)
def test_bash_process_metadata_requires_schema_v25(
    event_type: str,
    field: str,
    value: object,
) -> None:
    payload = {
        "schema_version": 24,
        "type": event_type,
        "call_id": "call-1",
        "name": "bash",
        "output": "Process p123 is still running",
        "is_error": False,
        field: value,
    }

    with pytest.raises(ValueError, match="Bash process metadata requires schema_version 25"):
        wisp_event_from_json(json.dumps(payload))

    payload["schema_version"] = 25
    assert wisp_event_from_json(json.dumps(payload)).schema_version == 25


@pytest.mark.parametrize(
    "event",
    [
        ToolResultReady(
            schema_version=24,
            call_id="call-1",
            name="bash",
            output="Process p123 is still running",
            is_error=False,
            process_id="p123",
            process_state="running",
            stdout="out",
            stderr="err",
            stdout_truncated=True,
            stderr_truncated=True,
            stdout_dropped_bytes=1,
            stderr_dropped_bytes=2,
        ),
        ToolExecutionEnded(
            schema_version=24,
            call_id="call-1",
            name="bash",
            output="Process p123 is still running",
            is_error=False,
            process_id="p123",
            process_state="running",
            stdout="out",
            stderr="err",
            stdout_truncated=True,
            stderr_truncated=True,
            stdout_dropped_bytes=1,
            stderr_dropped_bytes=2,
        ),
    ],
)
def test_bash_process_metadata_is_stripped_for_legacy_serialized_events(
    event: ToolResultReady | ToolExecutionEnded,
) -> None:
    payload = json.loads(event.model_dump_json())

    for field in (
        "process_id",
        "process_state",
        "process_error",
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
        "stdout_dropped_bytes",
        "stderr_dropped_bytes",
    ):
        assert field not in payload
    assert wisp_event_from_json(json.dumps(payload)).schema_version == 24


def test_rpc_session_name_changed_round_trips_only_at_schema_v21() -> None:
    event = RpcSessionNameChanged(
        command_id="name-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        previous_name="Old",
        name=None,
        entry_count=4,
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 21"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 20}).model_dump_json())


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 20,
            "type": "rpc.state",
            "command_id": "state-1",
            "state": {
                "provider": "fake",
                "model": "fake-model",
                "effort": None,
                "auto_compaction_enabled": True,
                "steering_mode": "one_at_a_time",
                "follow_up_mode": "one_at_a_time",
                "pending_steering_count": 0,
                "pending_follow_up_count": 0,
                "session_name": "Named",
                "active_command_id": None,
                "active_command_type": None,
                "cancel_requested": False,
            },
        },
        {
            "schema_version": 20,
            "type": "rpc.sessions",
            "command_id": "sessions-1",
            "sessions": [],
            "selected_session_id": "session-1",
            "selected_session_path": "/tmp/session-1.jsonl",
            "selected_session_name": "Named",
        },
        {
            "schema_version": 20,
            "type": "rpc.sessions",
            "command_id": "sessions-1",
            "sessions": [
                {
                    "session_id": "session-1",
                    "session_path": "/tmp/session-1.jsonl",
                    "updated_at": "2026-07-24T00:00:00Z",
                    "entry_count": 1,
                    "active_leaf_id": "entry-1",
                    "name": "Named",
                }
            ],
        },
        {
            "schema_version": 20,
            "type": "rpc.session.selected",
            "command_id": "select-1",
            "session_id": "session-1",
            "session_path": "/tmp/session-1.jsonl",
            "active_leaf_id": "entry-1",
            "entry_count": 1,
            "session_name": "Named",
        },
        {
            "schema_version": 20,
            "type": "rpc.session.cloned",
            "command_id": "clone-1",
            "source_session_id": "source",
            "source_session_path": "/tmp/source.jsonl",
            "source_active_leaf_id": "entry-1",
            "source_session_name": "Source",
            "session_id": "clone",
            "session_path": "/tmp/clone.jsonl",
            "active_leaf_id": "entry-1",
            "session_name": "Clone",
            "entry_count": 1,
        },
        {
            "schema_version": 20,
            "type": "rpc.session.forked",
            "command_id": "fork-1",
            "source_session_id": "source",
            "source_session_path": "/tmp/source.jsonl",
            "source_active_leaf_id": "entry-2",
            "source_session_name": "Source",
            "session_id": "fork",
            "session_path": "/tmp/fork.jsonl",
            "active_leaf_id": "entry-1",
            "session_name": "Fork",
            "entry_count": 1,
            "selected_entry_id": "entry-2",
            "selected_prompt": "edit me",
        },
    ],
)
def test_rpc_session_name_fields_require_schema_v21(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="session name fields require schema_version 21"):
        wisp_event_from_json(json.dumps(payload))


def test_rpc_session_name_fields_are_stripped_for_legacy_serialized_events() -> None:
    event = RpcSessionsReported(
        schema_version=20,
        command_id="sessions-1",
        sessions=(
            RpcSessionSummary(
                session_id="session-1",
                session_path=Path("/tmp/session-1.jsonl"),
                updated_at=datetime(2026, 7, 24, tzinfo=UTC),
                entry_count=1,
                active_leaf_id="entry-1",
                name="Named",
            ),
        ),
        selected_session_id="session-1",
        selected_session_path=Path("/tmp/session-1.jsonl"),
        selected_session_name="Named",
    )

    payload = json.loads(event.model_dump_json())

    assert "selected_session_name" not in payload
    assert "name" not in payload["sessions"][0]
    assert wisp_event_from_json(json.dumps(payload)).schema_version == 20


def test_rpc_session_tree_events_reject_inconsistent_payloads() -> None:
    with pytest.raises(ValidationError, match="session_id and session_path together"):
        RpcSessionTreeReported(
            command_id="tree-1",
            session_id="session-1",
            total_node_count=0,
        )
    with pytest.raises(ValidationError, match="next_after_entry_id exactly when truncated"):
        RpcSessionTreeReported(
            command_id="tree-1",
            total_node_count=0,
            truncated=True,
        )
    node = RpcSessionTreeNode(
        entry_id="entry-1",
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
        kind="message",
        role="user",
        preview="one",
    )
    with pytest.raises(ValidationError, match="final returned node"):
        RpcSessionTreeReported(
            command_id="tree-1",
            session_id="session-1",
            session_path=Path("/tmp/session-1.jsonl"),
            total_node_count=2,
            nodes=(node,),
            truncated=True,
            next_after_entry_id="entry-2",
        )
    with pytest.raises(ValidationError, match="without a session must be empty"):
        RpcSessionTreeReported(
            command_id="tree-1",
            active_leaf_id="entry-1",
            total_node_count=1,
            nodes=(node,),
        )
    with pytest.raises(ValidationError, match="include role only for messages"):
        RpcSessionTreeNode(
            entry_id="entry-1",
            created_at=datetime(2026, 7, 24, tzinfo=UTC),
            kind="event",
            role="user",
            preview="error",
        )
    with pytest.raises(ValidationError, match="must match the active-leaf transition"):
        RpcSessionTreeNavigated(
            command_id="navigate-1",
            session_id="session-1",
            session_path=Path("/tmp/session-1.jsonl"),
            selected_entry_id="entry-1",
            previous_active_leaf_id="entry-2",
            active_leaf_id="entry-2",
            editor_text="edit",
            changed=True,
            entry_count=3,
        )


def test_rpc_session_events_reject_incomplete_or_empty_identity() -> None:
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        RpcSessionSummary(
            session_id="",
            session_path=Path("/tmp/session-1.jsonl"),
            updated_at=datetime(2026, 7, 24, tzinfo=UTC),
            entry_count=0,
        )
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        RpcSessionSummary(
            session_id="session-1",
            session_path=Path("/tmp/session-1.jsonl"),
            updated_at=datetime(2026, 7, 24, tzinfo=UTC),
            entry_count=0,
            active_leaf_id="",
        )
    with pytest.raises(ValidationError, match="selected_session_id and selected_session_path"):
        RpcSessionsReported(command_id="sessions-1", selected_session_id="session-1")
    with pytest.raises(ValidationError, match="selected_session_id and selected_session_path"):
        RpcSessionsReported(
            command_id="sessions-1",
            selected_session_path=Path("/tmp/session-1.jsonl"),
        )
    with pytest.raises(ValidationError, match="String should have at least 1 character"):
        RpcSessionSelected(
            command_id="select-1",
            session_id="",
            session_path=Path("/tmp/session-1.jsonl"),
            entry_count=0,
        )


def test_rpc_state_snapshot_is_frozen_and_forbids_extra_fields() -> None:
    state = RpcStateSnapshot(
        provider="fake",
        model="fake-model",
        effort=None,
        auto_compaction_enabled=True,
        steering_mode="one_at_a_time",
        follow_up_mode="one_at_a_time",
        pending_steering_count=0,
        pending_follow_up_count=0,
    )

    with pytest.raises(ValidationError, match="frozen"):
        state.provider = "other"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RpcStateSnapshot.model_validate(
            {
                "provider": "fake",
                "model": "fake-model",
                "effort": None,
                "auto_compaction_enabled": True,
                "steering_mode": "one_at_a_time",
                "follow_up_mode": "one_at_a_time",
                "pending_steering_count": 0,
                "pending_follow_up_count": 0,
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SteerCommand(id="steer-1", content="redirect"), {"content": "redirect"}),
        (FollowUpCommand(id="follow-1", content="continue"), {"content": "continue"}),
        (GetQueueStateCommand(id="state-1"), {}),
        (
            SetQueueModeCommand(id="mode-1", kind="steering", mode="all"),
            {"kind": "steering", "mode": "all"},
        ),
        (PopQueueCommand(id="pop-1", kind="follow_up"), {"kind": "follow_up"}),
        (ClearQueueCommand(id="clear-1"), {}),
        (ClearQueueCommand(id="clear-2", kind="steering"), {"kind": "steering"}),
    ],
)
def test_queue_commands_serialize_as_jsonl_and_parse(
    command: RpcCommand,
    expected: dict[str, object],
) -> None:
    payload = json.loads(command.to_json_line())

    assert payload == {"id": command.id, "type": command.type, **expected}
    assert rpc_command_from_json(command.to_json_line()) == command


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"set_queue_mode","kind":"unknown","mode":"all"}',
        '{"type":"set_queue_mode","kind":"steering","mode":"invalid"}',
        '{"type":"pop_queue","kind":"unknown"}',
        '{"type":"clear_queue","kind":"unknown"}',
    ],
)
def test_typed_queue_commands_reject_invalid_kinds_and_modes(line: str) -> None:
    with pytest.raises(ValueError):
        rpc_command_from_json(line)


def test_queue_items_removed_round_trips_through_json() -> None:
    event = QueueItemsRemoved(
        command_id="clear-1",
        operation="clear",
        steering=("first", "second"),
        follow_up=("later",),
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 15"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 14}).model_dump_json())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"operation": "pop", "kind": None},
            "queue pop results require a queue kind",
        ),
        (
            {"operation": "pop", "kind": "steering", "steering": ("one", "two")},
            "at most one removed item",
        ),
        (
            {"operation": "clear", "kind": "steering", "follow_up": ("wrong",)},
            "cannot contain follow-up items",
        ),
        (
            {"operation": "clear", "kind": "follow_up", "steering": ("wrong",)},
            "cannot contain steering items",
        ),
    ],
)
def test_queue_items_removed_rejects_impossible_payloads(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        QueueItemsRemoved(command_id="queue-1", **kwargs)


def test_approval_scope_serializes_only_when_selected() -> None:
    scoped = ApprovalCommand(
        id="approval-1",
        call_id="call-1",
        approved=True,
        scope="tool_session",
    )

    assert json.loads(scoped.to_json_line()) == {
        "id": "approval-1",
        "type": "approval",
        "call_id": "call-1",
        "approved": True,
        "scope": "tool_session",
    }
    assert "scope" not in json.loads(
        ApprovalCommand(call_id="call-1", approved=True).to_json_line()
    )


def test_trust_command_serializes_as_jsonl_and_parses() -> None:
    command = TrustCommand(id="trust-1", request_id="req-1", trusted=True)

    line = command.to_json_line()

    assert json.loads(line) == {
        "id": "trust-1",
        "type": "trust",
        "request_id": "req-1",
        "trusted": True,
    }
    assert rpc_command_from_json(line) == command


def test_transient_trust_command_serializes_as_jsonl_and_parses() -> None:
    command = TrustCommand(
        id="trust-1",
        request_id="req-1",
        trusted=False,
        reason="Trust prompt closed",
        transient=True,
    )

    line = command.to_json_line()

    assert json.loads(line) == {
        "id": "trust-1",
        "type": "trust",
        "request_id": "req-1",
        "trusted": False,
        "reason": "Trust prompt closed",
        "transient": True,
    }
    assert rpc_command_from_json(line) == command


def test_trust_events_round_trip_through_json() -> None:
    requested = TrustRequested(request_id="req-1", project_path=Path("/repo"))
    resolved = TrustResolved(request_id="req-1", project_path=Path("/repo"), trusted=True)

    assert wisp_event_from_json(requested.model_dump_json()) == requested
    assert wisp_event_from_json(resolved.model_dump_json()) == resolved


def test_project_config_applied_round_trips_through_json() -> None:
    applied = ProjectConfigApplied(
        provider="openai", model="gpt-5.5", auth_path=Path("/home/u/.wisp/auth.json")
    )

    assert wisp_event_from_json(applied.model_dump_json()) == applied
    # model is optional (provider default).
    minimal = ProjectConfigApplied(provider="fake", auth_path=Path("/tmp/auth.json"))
    assert wisp_event_from_json(minimal.model_dump_json()) == minimal

    legacy = applied.model_copy(update={"schema_version": 25})
    legacy_payload = json.loads(legacy.model_dump_json())
    assert "auto_compaction_enabled" not in legacy_payload
    legacy_event = wisp_event_from_json(json.dumps(legacy_payload))
    assert legacy_event.schema_version == 25
    assert isinstance(legacy_event, ProjectConfigApplied)
    assert legacy_event.auto_compaction_enabled is None


def test_compaction_policy_fields_require_schema_v26() -> None:
    project_payload = json.loads(
        ProjectConfigApplied(
            provider="openai",
            auto_compaction_enabled=False,
            auth_path=Path("/home/u/.wisp/auth.json"),
        ).model_dump_json()
    )
    project_payload["schema_version"] = 25

    with pytest.raises(ValueError, match="Project compaction policy requires schema_version 26"):
        wisp_event_from_json(json.dumps(project_payload))


def test_rpc_commands_allow_protocol_optional_id() -> None:
    command = PromptCommand(prompt="hello")

    line = command.to_json_line()
    parsed = rpc_command_from_json('{"type":"prompt","prompt":"hello"}')

    assert json.loads(line) == {"type": "prompt", "prompt": "hello"}
    assert command.id is None
    assert parsed == command


def test_rpc_init_command_round_trips_without_arguments() -> None:
    command = InitCommand()

    assert json.loads(command.to_json_line()) == {"type": "init"}
    assert rpc_command_from_json('{"type":"init"}') == command


def test_wisp_event_from_json_returns_typed_event() -> None:
    event = wisp_event_from_json(
        '{"schema_version":6,"type":"rpc.command.finished","command_id":"cmd-1",'
        '"command_type":"prompt","ok":true}'
    )

    assert isinstance(event, RpcCommandFinished)
    assert event.command_id == "cmd-1"
    assert event.ok is True


def test_wisp_event_from_json_parses_provider_retry_progress() -> None:
    retry = ProviderRetrying(
        turn=1,
        provider="openai",
        attempt=2,
        max_attempts=3,
        delay_seconds=0.5,
        reason="rate_limit",
        status_code=429,
    )

    assert wisp_event_from_json(retry.model_dump_json()) == retry


@pytest.mark.parametrize("schema_version", [5, 17, 18, 19, 20])
def test_wisp_event_from_json_accepts_legacy_schema_versions(schema_version: int) -> None:
    payload: dict[str, object] = {
        "type": "rpc.command.finished",
        "schema_version": schema_version,
        "command_id": "cmd-1",
        "command_type": "prompt",
        "ok": True,
    }

    assert wisp_event_from_json(json.dumps(payload)).schema_version == schema_version


@pytest.mark.parametrize("schema_version", [None, 1, 2, 3, 4])
def test_wisp_event_from_json_rejects_unsupported_schema_version(
    schema_version: int | None,
) -> None:
    payload: dict[str, object] = {
        "type": "rpc.command.finished",
        "command_id": "cmd-1",
        "command_type": "prompt",
        "ok": True,
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version

    with pytest.raises(ValueError, match="Unsupported Wisp event schema_version"):
        wisp_event_from_json(json.dumps(payload))


def test_rpc_controller_sends_typed_commands_and_closes_transport() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        prompt_id = await controller.prompt("hello")
        init_id = await controller.init()
        compact_id = await controller.compact("Keep paths")
        stats_id = await controller.get_session_stats()
        state_id = await controller.get_state()
        commands_id = await controller.get_commands()
        skills_id = await controller.get_skills()
        mcp_id = await controller.get_mcp_status()
        messages_id = await controller.get_messages(
            session_id="session-1",
            limit=25,
            before_entry_id="entry-1",
        )
        sessions_id = await controller.get_sessions(limit=25)
        new_session_id = await controller.new_session()
        select_id = await controller.select_session("session-1")
        clone_id = await controller.clone_session()
        fork_id = await controller.fork_session("entry-1")
        tree_id = await controller.get_session_tree(limit=25, after_entry_id="entry-1")
        navigate_id = await controller.navigate_session_tree("entry-2")
        unrevert_id = await controller.unrevert_session_tree()
        name_id = await controller.set_session_name("Display", session_id="session-1")
        steer_id = await controller.steer("redirect")
        follow_up_id = await controller.follow_up("continue")
        queue_state_id = await controller.get_queue_state()
        queue_mode_id = await controller.set_queue_mode("steering", "all")
        queue_pop_id = await controller.pop_queue("steering")
        queue_clear_id = await controller.clear_queue("follow_up")
        cancel_id = await controller.cancel(prompt_id)
        approval_id = await controller.approve(
            "call-1",
            approved=True,
            scope="tool_session",
        )
        configure_id = await controller.configure(provider="openai-codex", model="gpt-5.5")
        shutdown_id = await controller.shutdown()
        await controller.close()

        assert [
            prompt_id,
            init_id,
            compact_id,
            stats_id,
            state_id,
            commands_id,
            skills_id,
            mcp_id,
            messages_id,
            sessions_id,
            new_session_id,
            select_id,
            clone_id,
            fork_id,
            tree_id,
            navigate_id,
            unrevert_id,
            name_id,
            steer_id,
            follow_up_id,
            queue_state_id,
            queue_mode_id,
            queue_pop_id,
            queue_clear_id,
            cancel_id,
            approval_id,
            configure_id,
            shutdown_id,
        ] == [
            "prompt-id",
            "init-id",
            "compact-id",
            "stats-id",
            "state-id",
            "commands-id",
            "skills-id",
            "mcp-id",
            "messages-id",
            "sessions-id",
            "new-session-id",
            "select-session-id",
            "clone-session-id",
            "fork-session-id",
            "session-tree-id",
            "navigate-session-tree-id",
            "unrevert-session-tree-id",
            "set-session-name-id",
            "steer-id",
            "follow-up-id",
            "queue-state-id",
            "queue-mode-id",
            "queue-pop-id",
            "queue-clear-id",
            "cancel-id",
            "approval-id",
            "configure-id",
            "shutdown-id",
        ]
        assert transport.commands == [
            PromptCommand(id="prompt-id", prompt="hello"),
            InitCommand(id="init-id"),
            CompactCommand(id="compact-id", instructions="Keep paths"),
            GetSessionStatsCommand(id="stats-id"),
            GetStateCommand(id="state-id"),
            GetCommandsCommand(id="commands-id"),
            GetSkillsCommand(id="skills-id"),
            GetMcpStatusCommand(id="mcp-id"),
            GetMessagesCommand(
                id="messages-id",
                session_id="session-1",
                limit=25,
                before_entry_id="entry-1",
            ),
            GetSessionsCommand(id="sessions-id", limit=25),
            NewSessionCommand(id="new-session-id"),
            SelectSessionCommand(id="select-session-id", session_id="session-1"),
            CloneSessionCommand(id="clone-session-id"),
            ForkSessionCommand(id="fork-session-id", entry_id="entry-1"),
            GetSessionTreeCommand(
                id="session-tree-id",
                limit=25,
                after_entry_id="entry-1",
            ),
            NavigateSessionTreeCommand(
                id="navigate-session-tree-id",
                entry_id="entry-2",
            ),
            UnrevertSessionTreeCommand(id="unrevert-session-tree-id"),
            SetSessionNameCommand(
                id="set-session-name-id",
                name="Display",
                session_id="session-1",
            ),
            SteerCommand(id="steer-id", content="redirect"),
            FollowUpCommand(id="follow-up-id", content="continue"),
            GetQueueStateCommand(id="queue-state-id"),
            SetQueueModeCommand(id="queue-mode-id", kind="steering", mode="all"),
            PopQueueCommand(id="queue-pop-id", kind="steering"),
            ClearQueueCommand(id="queue-clear-id", kind="follow_up"),
            CancelCommand(id="cancel-id", target_id="prompt-id"),
            ApprovalCommand(
                id="approval-id",
                call_id="call-1",
                approved=True,
                scope="tool_session",
            ),
            ConfigureCommand(id="configure-id", provider="openai-codex", model="gpt-5.5"),
            ShutdownCommand(id="shutdown-id"),
        ]
        assert transport.closed is True

    anyio.run(run)


def test_rpc_controller_configure_sends_effort() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(effort="high")

        assert transport.commands == [
            ConfigureCommand(id="configure-id", effort="high"),
        ]

    anyio.run(run)


def test_rpc_controller_configure_sends_agent_mode() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda _prefix: "configure-id",
        )

        await controller.configure(mode="plan")

        assert transport.commands == [
            ConfigureCommand(id="configure-id", mode="plan"),
        ]

    anyio.run(run)


def test_rpc_controller_configure_sends_auto_compaction_setting() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(auto_compaction_enabled=False)

        assert transport.commands == [
            ConfigureCommand(id="configure-id", auto_compaction_enabled=False),
        ]

    anyio.run(run)


def test_configure_command_serializes_effort_and_omits_when_unset() -> None:
    with_effort = ConfigureCommand(id="configure-1", effort="medium")

    line = with_effort.to_json_line()

    assert json.loads(line) == {
        "id": "configure-1",
        "type": "configure",
        "effort": "medium",
        "clear_effort": False,
    }
    assert rpc_command_from_json(line) == with_effort

    without_effort = ConfigureCommand(id="configure-2", model="gpt-5.5")

    assert "effort" not in json.loads(without_effort.to_json_line())


def test_rpc_controller_configure_clear_effort() -> None:
    # Regression test: effort=None is indistinguishable on the wire from
    # never having set effort at all (exclude_none drops it), so a client
    # that previously set an effort tier has no way to reset it back to the
    # provider's own default without a distinct, always-serialized signal.
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(clear_effort=True)

        assert transport.commands == [
            ConfigureCommand(id="configure-id", clear_effort=True),
        ]

    anyio.run(run)


def test_configure_command_always_serializes_clear_effort() -> None:
    # clear_effort's default (False) must never be silently dropped the way
    # effort=None is -- it is the only signal the server has to distinguish
    # "leave effort untouched" from "explicitly reset effort."
    command = ConfigureCommand(id="configure-1", model="gpt-5.5")

    assert json.loads(command.to_json_line())["clear_effort"] is False


def test_rpc_controller_exposes_transport_events() -> None:
    async def run() -> None:
        expected_events = [
            RpcCommandStarted(command_id="cmd-1", command_type="prompt"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=True),
        ]
        controller = RpcController(RecordingTransport(events=expected_events))

        events = [event async for event in controller.events()]

        assert events == expected_events

    anyio.run(run)


def test_jsonl_subprocess_rpc_transport_times_out_while_writing_handshake(
    monkeypatch: MonkeyPatch,
) -> None:
    class BlockingSendStream:
        async def send(self, _item: bytes) -> None:
            await anyio.sleep_forever()

    class BlockingProcess:
        stdin = BlockingSendStream()
        stdout = object()

    async def run() -> None:
        request = RpcHandshakeRequest(
            frontend_name="fixture",
            frontend_version="0.1.0",
            min_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
            max_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
            min_event_schema_version=EVENT_SCHEMA_VERSION,
            max_event_schema_version=EVENT_SCHEMA_VERSION,
            supported_capabilities=(),
            required_capabilities=(),
        )
        transport = JsonlSubprocessRpcTransport(
            cast(Process, BlockingProcess()),
            request,
        )
        monkeypatch.setattr(rpc_client_module, "_SUBPROCESS_HANDSHAKE_TIMEOUT_SECONDS", 0.01)

        with anyio.fail_after(1):
            with pytest.raises(RpcHandshakeError, match="did not complete handshake in time"):
                await transport._perform_handshake()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_round_trips_events(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": "0.1.0",
    "protocol_version": 2,
    "event_schema_version": 34,
    "min_protocol_version": 2,
    "max_protocol_version": 2,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
command = json.loads(sys.stdin.readline())
started = {
    "schema_version": 34,
    "type": "rpc.command.started",
    "command_id": command["id"],
    "command_type": command["type"],
}
finished = {
    "schema_version": 34,
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}
print(json.dumps(started), flush=True)
print(json.dumps(finished), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == [
            "rpc.command.started",
            "rpc.command.finished",
        ]
        assert isinstance(events[0], RpcCommandStarted)
        assert isinstance(events[1], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[1].ok is True

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_rejects_wrong_event_schema_version(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": "0.1.0",
    "protocol_version": 2,
    "event_schema_version": 34,
    "min_protocol_version": 2,
    "max_protocol_version": 2,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
print(json.dumps({
    "schema_version": 6,
    "type": "rpc.command.started",
    "command_id": "command-1",
    "command_type": "prompt",
}), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        with pytest.raises(RpcProtocolError, match="negotiated version"):
            await anext(transport.events())
        await transport.close()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_rejects_empty_event_frame(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": "0.1.0",
    "protocol_version": 2,
    "event_schema_version": 34,
    "min_protocol_version": 2,
    "max_protocol_version": 2,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
print("", flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        with pytest.raises(RpcProtocolError, match="empty RPC event frame"):
            await anext(transport.events())
        await transport.close()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_surfaces_handshake_rejection(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.rejected",
    "code": "protocol_version_mismatch",
    "message": "No compatible live RPC protocol version.",
    "backend_package_version": "0.1.0",
    "min_protocol_version": 3,
    "max_protocol_version": 3,
    "event_schema_version": 34,
}), flush=True)
"""
        with pytest.raises(RpcHandshakeError, match="No compatible") as error:
            await JsonlSubprocessRpcTransport.start(
                [sys.executable, "-c", script],
                cwd=tmp_path,
            )
        assert error.value.code == "protocol_version_mismatch"

    anyio.run(run)


@pytest.mark.process
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal behavior")
def test_jsonl_subprocess_rpc_transport_kills_sigterm_resistant_child(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run() -> None:
        script = """
import json
import signal
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": "0.1.0",
    "protocol_version": 2,
    "event_schema_version": 34,
    "min_protocol_version": 2,
    "max_protocol_version": 2,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
sys.stdin.read()
time.sleep(60)
"""
        monkeypatch.setattr(rpc_client_module, "_SUBPROCESS_CLOSE_TIMEOUT_SECONDS", 0.05)
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        with anyio.fail_after(1):
            await transport.close()
        assert transport._process.returncode is not None
        await transport.close()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_does_not_block_on_stderr(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
sys.stderr.write("x" * 200000)
sys.stderr.flush()
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": "0.1.0",
    "protocol_version": 2,
    "event_schema_version": 34,
    "min_protocol_version": 2,
    "max_protocol_version": 2,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
command = json.loads(sys.stdin.readline())
print(json.dumps({
    "schema_version": 34,
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", script],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        with anyio.fail_after(5):
            events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == ["rpc.command.finished"]
        assert isinstance(events[0], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[0].ok is True

    anyio.run(run)
