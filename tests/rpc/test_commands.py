from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from wisp.rpc import (
    BeginDeviceCodeCommand,
    ClearQueueCommand,
    CloneSessionCommand,
    CompactCommand,
    DisconnectProviderCommand,
    FollowUpCommand,
    ForkSessionCommand,
    GetCommandsCommand,
    GetConnectionCatalogCommand,
    GetMcpStatusCommand,
    GetMessagesCommand,
    GetModelCatalogCommand,
    GetQueueStateCommand,
    GetSessionsCommand,
    GetSessionStatsCommand,
    GetSessionTreeCommand,
    GetSkillsCommand,
    GetStateCommand,
    InitCommand,
    NavigateSessionTreeCommand,
    PopQueueCommand,
    SelectSessionCommand,
    SetQueueModeCommand,
    SetSessionNameCommand,
    SteerCommand,
    StoreApiKeyCommand,
    UnrevertSessionTreeCommand,
)
from wisp.rpc.commands import (
    MAX_RPC_API_KEY_CHARS,
    MAX_RPC_COMMAND_ID_CHARS,
    ApprovalCommand,
    PromptCommand,
    RpcCommand,
    TrustCommand,
    detach_secret_fields,
    rpc_command_from_json,
    rpc_command_payload_size,
    take_store_api_key,
)


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


def test_get_model_catalog_command_serializes_as_canonical_jsonl() -> None:
    command = GetModelCatalogCommand(id="models-1")

    assert command.to_json_line() == '{"id":"models-1","type":"get_model_catalog"}\n'
    assert rpc_command_from_json(command.to_json_line()) == command


def test_connection_catalog_command_serializes_as_canonical_jsonl() -> None:
    command = GetConnectionCatalogCommand(id="connections-1")

    assert command.to_json_line() == '{"id":"connections-1","type":"get_connection_catalog"}\n'
    assert rpc_command_from_json(command.to_json_line()) == command


def test_store_api_key_command_serializes_but_redacts_repr() -> None:
    command = StoreApiKeyCommand(id="store-1", provider="anthropic", api_key="sentinel-secret")

    assert json.loads(command.to_json_line()) == {
        "id": "store-1",
        "type": "store_api_key",
        "provider": "anthropic",
        "api_key": "sentinel-secret",
    }
    assert "sentinel-secret" not in repr(command)
    assert "sentinel-secret" not in str(command)
    assert rpc_command_from_json(command.to_json_line()) == command

    detached = detach_secret_fields(command.model_dump(exclude_none=True))
    assert "sentinel-secret" not in repr(detached)
    assert rpc_command_payload_size(detached) > len("sentinel-secret")
    assert take_store_api_key(detached) == "sentinel-secret"
    assert "_api_key" not in detached


def test_store_api_key_validation_errors_redact_the_input() -> None:
    secret = "sentinel-secret-" + "s" * MAX_RPC_API_KEY_CHARS
    payload = json.dumps({"type": "store_api_key", "provider": "anthropic", "api_key": secret})

    with pytest.raises(ValidationError) as direct:
        StoreApiKeyCommand(provider="anthropic", api_key=secret)
    with pytest.raises(ValidationError) as parsed:
        rpc_command_from_json(payload)

    for error in (direct.value, parsed.value):
        assert secret not in str(error)
        assert secret not in error.json()


def test_disconnect_and_device_code_commands_serialize() -> None:
    disconnect = DisconnectProviderCommand(id="disconnect-1", provider="anthropic")
    begin = BeginDeviceCodeCommand(id="device-1", provider="openai-codex")

    assert json.loads(disconnect.to_json_line()) == {
        "id": "disconnect-1",
        "type": "disconnect_provider",
        "provider": "anthropic",
    }
    assert json.loads(begin.to_json_line()) == {
        "id": "device-1",
        "type": "begin_device_code",
        "provider": "openai-codex",
    }


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
