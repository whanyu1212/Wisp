from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

import wisp.rpc.protocol_schema as protocol_schema
from wisp.events import (
    EVENT_SCHEMA_VERSION,
    BillableTokenUsage,
    CompactionCompleted,
    CompactionStarted,
    ContextBudget,
    ContextEstimate,
    ContextPressure,
    MessageCompleted,
    ProviderRetrying,
    QueueItemsRemoved,
    RpcCommandFinished,
    RpcMcpServerSnapshot,
    RpcMcpStatusReported,
    RpcMcpStatusSnapshot,
    RpcMessagesReported,
    RpcSessionTreeNode,
    RpcSessionTreeReported,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
    UsageCost,
    UsageCostRates,
)
from wisp.rpc.commands import (
    CompactCommand,
    ConfigureCommand,
    GetMessagesCommand,
    PromptCommand,
    RpcCommandAdapter,
)
from wisp.rpc.protocol import (
    LIVE_RPC_PROTOCOL_VERSION,
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    RpcServerHello,
    RpcTransportLimits,
)
from wisp.rpc.protocol_schema import (
    generate_protocol_artifacts,
    invalid_protocol_history,
    modified_committed_protocol_artifacts,
    protocol_schema_directory,
    stale_protocol_artifacts,
    write_protocol_archive,
    write_protocol_artifacts,
)

_SCHEMA_FILES = (
    "client-handshake.schema.json",
    "server-handshake.schema.json",
    "commands.schema.json",
    "events.schema.json",
)
_REPOSITORY_SCHEMA_ROOT = Path(__file__).parents[1] / "schemas" / "live-rpc"
_REPOSITORY_SCHEMA_DIRECTORY = protocol_schema_directory(_REPOSITORY_SCHEMA_ROOT)


def _artifact(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads(generate_protocol_artifacts()[name]))


def _mapping(schema: dict[str, object]) -> dict[str, str]:
    discriminator = cast(dict[str, object], schema["discriminator"])
    return cast(dict[str, str], discriminator["mapping"])


def _definition(schema: dict[str, object], reference: str) -> dict[str, object]:
    definitions = cast(dict[str, object], schema["$defs"])
    return cast(dict[str, object], definitions[reference.removeprefix("#/$defs/")])


def test_protocol_artifact_generation_is_deterministic_and_hashed() -> None:
    first = generate_protocol_artifacts()
    second = generate_protocol_artifacts()

    assert first == second
    assert tuple(first) == (*_SCHEMA_FILES, "manifest.json")
    assert all(
        content.endswith("\n") and not content.endswith("\n\n") for content in first.values()
    )

    manifest = json.loads(first["manifest.json"])
    assert manifest["live_protocol_version"] == LIVE_RPC_PROTOCOL_VERSION
    assert manifest["event_schema_version"] == EVENT_SCHEMA_VERSION
    assert manifest["fixed_handshake_frame_bytes"] == MAX_HANDSHAKE_FRAME_BYTES
    assert manifest["maximum_application_frame_bytes"] == MAX_LIVE_RPC_FRAME_BYTES
    for filename in _SCHEMA_FILES:
        digest = hashlib.sha256(first[filename].encode()).hexdigest()
        assert manifest["schema_hashes"][filename] == digest


def test_committed_protocol_artifacts_match_models_and_history() -> None:
    assert stale_protocol_artifacts(_REPOSITORY_SCHEMA_DIRECTORY) == ()
    assert invalid_protocol_history(_REPOSITORY_SCHEMA_ROOT) == ()


def test_protocol_artifact_check_reports_missing_changed_and_extra_files(tmp_path: Path) -> None:
    directory = protocol_schema_directory(tmp_path)
    assert stale_protocol_artifacts(directory) == (*_SCHEMA_FILES, "manifest.json")

    write_protocol_artifacts(directory)
    assert stale_protocol_artifacts(directory) == ()
    assert invalid_protocol_history(tmp_path) == ()

    (directory / "commands.schema.json").write_text("{}\n", encoding="utf-8")
    assert stale_protocol_artifacts(directory) == ("commands.schema.json",)
    assert invalid_protocol_history(tmp_path) == (
        "protocol schema hash mismatch: v1/commands.schema.json",
        "protocol schema dialect mismatch: v1/commands.schema.json",
    )

    write_protocol_artifacts(directory)
    (directory / "obsolete.schema.json").write_text("{}\n", encoding="utf-8")
    assert stale_protocol_artifacts(directory) == ("obsolete.schema.json",)
    assert invalid_protocol_history(tmp_path) == ("unexpected protocol artifact set: v1",)


def test_historical_protocol_manifest_is_pinned_outside_its_version_directory(
    tmp_path: Path,
) -> None:
    directory = protocol_schema_directory(tmp_path)
    write_protocol_artifacts(directory)
    manifest_path = directory / "manifest.json"
    manifest_content = manifest_path.read_text(encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest_content.encode()).hexdigest()

    assert invalid_protocol_history(
        tmp_path,
        current_protocol_version=2,
        historical_manifest_hashes={1: manifest_hash},
    ) == ("missing protocol schema directory: v2",)

    manifest = json.loads(manifest_content)
    manifest["event_schema_version"] = EVENT_SCHEMA_VERSION + 1
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    assert invalid_protocol_history(
        tmp_path,
        current_protocol_version=2,
        historical_manifest_hashes={1: manifest_hash},
    ) == (
        "historical protocol manifest changed: v1",
        "missing protocol schema directory: v2",
    )


def test_protocol_history_rejects_noncanonical_directories_and_duplicate_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = protocol_schema_directory(tmp_path)
    write_protocol_artifacts(directory)
    directory.rename(tmp_path / "v01")

    assert invalid_protocol_history(tmp_path) == (
        "unexpected protocol schema directory: v01",
        "missing protocol schema directory: v1",
    )

    monkeypatch.setattr(
        protocol_schema,
        "HISTORICAL_PROTOCOL_MANIFEST_SHA256",
        ((1, "first"), (1, "second")),
    )
    assert invalid_protocol_history(tmp_path, current_protocol_version=2) == (
        "historical protocol manifest hash registry contains duplicate versions",
    )


def test_git_history_check_reports_modified_committed_version_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> object:
        assert "--diff-filter=MDRTUXB" in cast(tuple[str, ...], args[0])
        return protocol_schema.subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                "schemas/live-rpc/v1/events.schema.json\n"
                "schemas/live-rpc/v01/manifest.json\n"
                "site/reference/compatibility.md\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(protocol_schema.subprocess, "run", fake_run)

    assert modified_committed_protocol_artifacts("trusted-base") == (
        "schemas/live-rpc/v1/events.schema.json",
    )


def test_protocol_version_directories_cannot_be_cross_written(tmp_path: Path) -> None:
    assert protocol_schema_directory(tmp_path, protocol_version=2) == tmp_path / "v2"

    with pytest.raises(RuntimeError, match="refusing to write protocol v1 into v2"):
        write_protocol_artifacts(protocol_schema_directory(tmp_path, protocol_version=2))


def test_protocol_release_archive_is_deterministic_and_versioned(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    write_protocol_archive(first)
    write_protocol_archive(second)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, mode="r:gz") as archive:
        assert archive.getnames() == [
            f"wisp-live-rpc-v{LIVE_RPC_PROTOCOL_VERSION}/{filename}"
            for filename in (*_SCHEMA_FILES, "manifest.json")
        ]


def test_generated_json_schemas_are_valid_draft_2020_12() -> None:
    artifacts = generate_protocol_artifacts()
    for filename in _SCHEMA_FILES:
        schema = json.loads(artifacts[filename])
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_handshake_schemas_require_wire_critical_fields_and_safe_identifiers() -> None:
    client_schema = _artifact("client-handshake.schema.json")
    server_schema = _artifact("server-handshake.schema.json")
    client = Draft202012Validator(client_schema)
    server = Draft202012Validator(server_schema)
    client_payload = {
        "type": "rpc.client.hello",
        "frontend_name": "wisp-rust-tui",
        "frontend_version": "0.1.0",
        "min_protocol_version": 1,
        "max_protocol_version": 1,
        "min_event_schema_version": EVENT_SCHEMA_VERSION,
        "max_event_schema_version": EVENT_SCHEMA_VERSION,
        "supported_capabilities": ["streaming.text"],
        "required_capabilities": ["streaming.text"],
    }

    assert client.is_valid(client_payload)
    assert not client.is_valid(
        {key: value for key, value in client_payload.items() if key != "max_event_schema_version"}
    )
    assert not client.is_valid({**client_payload, "frontend_name": "wisp-rust-tui\n"})
    assert not client.is_valid({**client_payload, "supported_capabilities": [1]})
    assert not client.is_valid({**client_payload, "supported_capabilities": ["streaming.text\n"]})
    invariants = cast(list[dict[str, str]], client_schema["x-wisp-cross-field-invariants"])
    assert invariants == [
        {
            "kind": "ordered-range",
            "maximum_property": "max_protocol_version",
            "minimum_property": "min_protocol_version",
        },
        {
            "kind": "ordered-range",
            "maximum_property": "max_event_schema_version",
            "minimum_property": "min_event_schema_version",
        },
        {
            "kind": "array-subset",
            "subset_property": "required_capabilities",
            "superset_property": "supported_capabilities",
        },
    ]

    hello = RpcServerHello(
        backend_package_version="0.1.0",
        protocol_version=1,
        event_schema_version=EVENT_SCHEMA_VERSION,
        min_frontend_protocol_version=1,
        max_frontend_protocol_version=1,
        capabilities=(),
        limits=RpcTransportLimits(
            max_client_frame_bytes=1024,
            max_server_frame_bytes=2048,
        ),
    ).model_dump(mode="json")
    assert server.is_valid(hello)
    assert not server.is_valid(
        {key: value for key, value in hello.items() if key != "protocol_version"}
    )
    server_mapping = _mapping(server_schema)
    server_hello = _definition(server_schema, server_mapping["rpc.server.hello"])
    assert server_hello["x-wisp-cross-field-invariants"] == [
        {
            "kind": "ordered-range",
            "maximum_property": "max_frontend_protocol_version",
            "minimum_property": "min_frontend_protocol_version",
        },
        {
            "kind": "value-in-range",
            "maximum_property": "max_frontend_protocol_version",
            "minimum_property": "min_frontend_protocol_version",
            "value_property": "protocol_version",
        },
    ]
    rejection = _definition(server_schema, server_mapping["rpc.handshake.rejected"])
    assert rejection["x-wisp-cross-field-invariants"] == [
        {
            "kind": "ordered-range",
            "maximum_property": "max_protocol_version",
            "minimum_property": "min_protocol_version",
        }
    ]


def test_command_schema_contains_every_discriminator_once() -> None:
    schema = _artifact("commands.schema.json")
    mapping = _mapping(schema)
    variants = cast(list[dict[str, str]], schema["oneOf"])
    references = {variant["$ref"] for variant in variants}

    assert len(mapping) == 29
    assert len(set(mapping.values())) == len(mapping)
    assert references == set(mapping.values())


def test_typed_command_output_validates_but_none_is_never_a_wire_value() -> None:
    validator = Draft202012Validator(_artifact("commands.schema.json"))
    commands = (
        PromptCommand(prompt="hello, 世界"),
        CompactCommand(),
        GetMessagesCommand(),
        ConfigureCommand(provider="openai"),
    )

    for command in commands:
        assert validator.is_valid(json.loads(command.to_json_line()))
    assert not validator.is_valid({"type": "compact", "instructions": None})
    assert not validator.is_valid({"type": "future.command"})
    assert not validator.is_valid({"prompt": "missing discriminator"})


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "get_messages",
            "limit": 200,
            "before_entry_id": "before",
            "after_entry_id": "after",
        },
        {
            "type": "get_messages",
            "limit": 200,
            "entry_ids": ["entry", "entry"],
        },
        {"type": "get_messages", "limit": 200, "entry_ids": []},
        {
            "type": "get_messages",
            "limit": 200,
            "full_content": True,
        },
        {"type": "configure", "clear_effort": False},
        {
            "type": "configure",
            "clear_effort": True,
            "effort": "high",
        },
        {
            "type": "approval",
            "call_id": "call-1",
            "approved": False,
            "scope": "tool_session",
        },
    ],
)
def test_command_schema_rejects_semantically_invalid_typed_output(
    payload: dict[str, object],
) -> None:
    assert not Draft202012Validator(_artifact("commands.schema.json")).is_valid(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "prompt", "id": "", "prompt": "hello"},
        {"type": "cancel", "target_id": ""},
        {"type": "get_messages", "entry_ids": []},
        {"type": "configure", "clear_effort": False},
        {
            "type": "configure",
            "clear_effort": True,
            "effort": "high",
        },
        {
            "type": "approval",
            "call_id": "call-1",
            "approved": False,
            "scope": "tool_session",
        },
    ],
)
def test_typed_command_models_reject_invalid_output_states(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RpcCommandAdapter.validate_python(payload)


def test_event_schema_contains_every_current_discriminator_once() -> None:
    schema = _artifact("events.schema.json")
    mapping = _mapping(schema)
    variants = cast(list[dict[str, str]], schema["oneOf"])
    references = {variant["$ref"] for variant in variants}

    assert len(mapping) == 46
    assert len(set(mapping.values())) == len(mapping)
    assert references == set(mapping.values())
    for definition in cast(dict[str, dict[str, object]], schema["$defs"]).values():
        properties = definition.get("properties")
        if isinstance(properties, dict):
            assert set(cast(list[str], definition["required"])) == set(properties)
            assert definition["additionalProperties"] is False


def test_event_schema_matches_current_serialized_requiredness_and_exclusions() -> None:
    schema = _artifact("events.schema.json")
    validator = Draft202012Validator(schema)
    event = MessageCompleted(
        turn=1,
        content="done",
        finish_reason="stop",
        tool_calls=(
            ToolCallSnapshot(
                call_id="call-1",
                name="bash",
                arguments={"command": "pwd"},
                provider_call_id="provider-private",
            ),
        ),
    )
    payload = json.loads(event.model_dump_json())

    assert validator.is_valid(payload)
    assert "provider_call_id" not in payload["tool_calls"][0]
    assert "provider_call_id" not in json.dumps(schema)
    assert not validator.is_valid(
        {key: value for key, value in payload.items() if key != "timestamp"}
    )


def test_event_decimal_fields_are_string_only_on_the_wire() -> None:
    schema = _artifact("events.schema.json")
    definitions = cast(dict[str, dict[str, object]], schema["$defs"])

    for definition_name, field_name in (
        ("SessionCostSummary", "known_usd"),
        ("UsageCost", "estimated_usd"),
        ("UsageCostRates", "input_usd_per_million"),
    ):
        properties = cast(dict[str, dict[str, object]], definitions[definition_name]["properties"])
        assert '"type": "number"' not in json.dumps(properties[field_name])


def test_event_decimal_schema_accepts_serialized_exponent_notation() -> None:
    event = MessageCompleted(
        turn=1,
        content="done",
        finish_reason="stop",
        cost=UsageCost(
            provider="custom",
            billable=BillableTokenUsage(
                input_tokens=1,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=1,
            ),
            rates=UsageCostRates(
                input_usd_per_million=Decimal("1E+3"),
                output_usd_per_million=Decimal("2E-3"),
            ),
            estimated_usd=Decimal("1E-3"),
        ),
    )
    payload = json.loads(event.model_dump_json())

    assert payload["cost"]["rates"]["input_usd_per_million"] == "1E+3"
    assert Draft202012Validator(_artifact("events.schema.json")).is_valid(payload)


def test_event_decimal_schema_rejects_negative_financial_values() -> None:
    schema = _artifact("events.schema.json")
    definitions = cast(dict[str, dict[str, object]], schema["$defs"])

    for definition_name, field_name in (
        ("SessionCostSummary", "known_usd"),
        ("UsageCost", "estimated_usd"),
        ("UsageCostRates", "input_usd_per_million"),
        ("UsageCostRates", "output_usd_per_million"),
        ("UsageCostRates", "cache_read_usd_per_million"),
        ("UsageCostRates", "cache_write_usd_per_million"),
    ):
        properties = cast(dict[str, dict[str, object]], definitions[definition_name]["properties"])
        validator = Draft202012Validator(properties[field_name])
        assert validator.is_valid("1E+3")
        assert validator.is_valid("-0E+3")
        assert not validator.is_valid("-1")
        assert not validator.is_valid("-1E+3")


def test_event_schema_enforces_priced_and_unpriced_usage_cost_states() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    priced = MessageCompleted(
        turn=1,
        content="done",
        finish_reason="stop",
        cost=UsageCost(
            provider="custom",
            billable=BillableTokenUsage(
                input_tokens=1,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=1,
            ),
            rates=UsageCostRates(
                input_usd_per_million=Decimal("1"),
                output_usd_per_million=Decimal("1"),
            ),
            estimated_usd=Decimal("0.000002"),
        ),
    )
    priced_payload = json.loads(priced.model_dump_json())
    assert validator.is_valid(priced_payload)
    for field in ("billable", "rates"):
        malformed = json.loads(priced.model_dump_json())
        malformed["cost"][field] = None
        assert not validator.is_valid(malformed)
    malformed = json.loads(priced.model_dump_json())
    malformed["cost"]["unavailable_reason"] = "pricing_unavailable"
    assert not validator.is_valid(malformed)

    unpriced = MessageCompleted(
        turn=1,
        content="done",
        finish_reason="stop",
        cost=UsageCost(provider="custom", unavailable_reason="pricing_unavailable"),
    )
    unpriced_payload = json.loads(unpriced.model_dump_json())
    assert validator.is_valid(unpriced_payload)
    unpriced_payload["cost"]["unavailable_reason"] = None
    assert not validator.is_valid(unpriced_payload)


@pytest.mark.parametrize("event_type", [ToolExecutionEnded, ToolResultReady])
def test_event_schema_enforces_tool_failure_metadata(event_type: type[ToolExecutionEnded]) -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    success = event_type(call_id="call-1", name="bash", output="ok", is_error=False)
    success_payload = json.loads(success.model_dump_json())
    assert validator.is_valid(success_payload)

    for field, value in (
        ("failure_code", "invalid_arguments"),
        ("retryable", True),
        ("recovery_hint", "Retry with valid arguments."),
    ):
        malformed = dict(success_payload)
        malformed[field] = value
        assert not validator.is_valid(malformed)

    failure = event_type(
        call_id="call-1",
        name="bash",
        output="invalid arguments",
        is_error=True,
        failure_code="invalid_arguments",
        retryable=True,
        recovery_hint="Retry with valid arguments.",
    )
    failure_payload = json.loads(failure.model_dump_json())
    assert validator.is_valid(failure_payload)
    failure_payload["failure_code"] = None
    assert not validator.is_valid(failure_payload)


def test_event_schema_enforces_rpc_message_cursor_invariants() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    untruncated = RpcMessagesReported(command_id="command-1")
    untruncated_payload = json.loads(untruncated.model_dump_json())
    assert validator.is_valid(untruncated_payload)
    for cursor in ("next_before_entry_id", "next_after_entry_id"):
        malformed = dict(untruncated_payload)
        malformed[cursor] = "entry-1"
        assert not validator.is_valid(malformed)

    backward = RpcMessagesReported(
        command_id="command-1",
        truncated=True,
        next_before_entry_id="entry-1",
    )
    backward_payload = json.loads(backward.model_dump_json())
    assert validator.is_valid(backward_payload)
    backward_payload["next_after_entry_id"] = "entry-2"
    assert not validator.is_valid(backward_payload)

    forward = RpcMessagesReported(
        command_id="command-1",
        truncated=True,
        next_after_entry_id="entry-2",
    )
    assert validator.is_valid(json.loads(forward.model_dump_json()))


def test_event_schema_enforces_mcp_status_error_coupling() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    connected = RpcMcpStatusReported(
        command_id="command-1",
        status=RpcMcpStatusSnapshot(
            servers=(RpcMcpServerSnapshot(name="docs", status="connected"),)
        ),
    )
    connected_payload = json.loads(connected.model_dump_json())
    assert validator.is_valid(connected_payload)
    connected_payload["status"]["servers"][0]["error"] = "unexpected"
    assert not validator.is_valid(connected_payload)

    unavailable = RpcMcpStatusReported(
        command_id="command-1",
        status=RpcMcpStatusSnapshot(
            servers=(RpcMcpServerSnapshot(name="docs", status="unavailable", error="offline"),)
        ),
    )
    unavailable_payload = json.loads(unavailable.model_dump_json())
    assert validator.is_valid(unavailable_payload)
    unavailable_payload["status"]["servers"][0]["error"] = None
    assert not validator.is_valid(unavailable_payload)


def test_event_schema_enforces_session_tree_report_invariants() -> None:
    schema = _artifact("events.schema.json")
    validator = Draft202012Validator(schema)
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    nodes = (
        RpcSessionTreeNode(
            entry_id="entry-1",
            created_at=created_at,
            kind="message",
            role="user",
            preview="first",
        ),
        RpcSessionTreeNode(
            entry_id="entry-2",
            parent_id="entry-1",
            created_at=created_at,
            kind="event",
            preview="second",
        ),
    )
    report = RpcSessionTreeReported(
        command_id="command-1",
        session_id="session-1",
        session_path=Path("/tmp/session-1.jsonl"),
        active_leaf_id="entry-2",
        total_node_count=2,
        nodes=nodes,
        truncated=True,
        next_after_entry_id="entry-2",
    )
    payload = json.loads(report.model_dump_json())
    assert validator.is_valid(payload)

    for field, value in (
        ("session_path", None),
        ("truncated", False),
    ):
        malformed = dict(payload)
        malformed[field] = value
        assert not validator.is_valid(malformed)
    malformed = json.loads(report.model_dump_json())
    malformed["nodes"][0]["role"] = None
    assert not validator.is_valid(malformed)

    empty_payload = json.loads(
        RpcSessionTreeReported(command_id="command-1", total_node_count=0).model_dump_json()
    )
    assert validator.is_valid(empty_payload)
    empty_payload["total_node_count"] = 1
    assert not validator.is_valid(empty_payload)

    definitions = cast(dict[str, dict[str, object]], schema["$defs"])
    invariants = cast(
        list[dict[str, object]],
        definitions["RpcSessionTreeReported"]["x-wisp-cross-field-invariants"],
    )
    assert [invariant["kind"] for invariant in invariants] == [
        "array-length-at-most-property",
        "array-item-property-unique",
        "value-equals-last-array-item-property",
    ]


def test_event_schema_enforces_compaction_invariants() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    budget = ContextBudget(
        estimate=ContextEstimate(
            system_tokens=0,
            message_tokens=0,
            tool_schema_tokens=0,
            total_tokens=0,
        ),
        reserve_tokens=0,
    )
    manual_started = CompactionStarted(session_id="session-1", source_entry_count=1)
    manual_payload = json.loads(manual_started.model_dump_json())
    assert validator.is_valid(manual_payload)
    manual_payload["trigger_budget"] = json.loads(budget.model_dump_json())
    assert not validator.is_valid(manual_payload)

    for reason in ("threshold", "overflow"):
        started = CompactionStarted(
            session_id="session-1",
            reason=reason,
            source_entry_count=1,
            trigger_budget=budget,
        )
        payload = json.loads(started.model_dump_json())
        assert validator.is_valid(payload)
        payload["trigger_budget"] = None
        assert not validator.is_valid(payload)

    manual_completed = CompactionCompleted(
        session_id="session-1",
        outcome="completed",
        replaced_entry_count=1,
        retained_entry_count=1,
    )
    manual_completed_payload = json.loads(manual_completed.model_dump_json())
    assert validator.is_valid(manual_completed_payload)
    manual_completed_payload["will_retry"] = True
    assert not validator.is_valid(manual_completed_payload)

    overflow_retry = CompactionCompleted(
        session_id="session-1",
        reason="overflow",
        outcome="completed",
        replaced_entry_count=0,
        retained_entry_count=1,
        will_retry=True,
    )
    assert validator.is_valid(json.loads(overflow_retry.model_dump_json()))
    overflow_stopped = overflow_retry.model_copy(
        update={"will_retry": False, "error": "context still exceeds the limit"}
    )
    overflow_stopped_payload = json.loads(overflow_stopped.model_dump_json())
    assert validator.is_valid(overflow_stopped_payload)
    overflow_stopped_payload["error"] = "   "
    assert not validator.is_valid(overflow_stopped_payload)

    overflow_failed = CompactionCompleted(
        session_id="session-1",
        reason="overflow",
        outcome="failed",
        replaced_entry_count=0,
        retained_entry_count=1,
        error="provider failure",
    )
    overflow_failed_payload = json.loads(overflow_failed.model_dump_json())
    assert validator.is_valid(overflow_failed_payload)
    overflow_failed_payload["will_retry"] = True
    assert not validator.is_valid(overflow_failed_payload)


def test_event_schema_enforces_queue_removal_invariants() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    cleared = QueueItemsRemoved(command_id="command-1", operation="clear")
    assert validator.is_valid(json.loads(cleared.model_dump_json()))

    popped = QueueItemsRemoved(
        command_id="command-1",
        operation="pop",
        kind="steering",
        steering=("first",),
    )
    payload = json.loads(popped.model_dump_json())
    assert validator.is_valid(payload)
    for field, value in (
        ("kind", None),
        ("follow_up", ["second"]),
        ("steering", ["first", "second"]),
    ):
        malformed = dict(payload)
        malformed[field] = value
        assert not validator.is_valid(malformed)


def test_event_schema_rejects_historical_future_and_incomplete_live_events() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    event = RpcCommandFinished(command_id="command-1", command_type="prompt", ok=True)
    payload = json.loads(event.model_dump_json())

    assert validator.is_valid(payload)
    assert not validator.is_valid({**payload, "schema_version": EVENT_SCHEMA_VERSION - 1})
    assert not validator.is_valid({**payload, "schema_version": EVENT_SCHEMA_VERSION + 1})
    assert not validator.is_valid({key: value for key, value in payload.items() if key != "error"})


def test_provider_retry_delay_is_finite_and_schema_valid() -> None:
    validator = Draft202012Validator(_artifact("events.schema.json"))
    event = ProviderRetrying(
        turn=1,
        provider="openai",
        attempt=1,
        max_attempts=3,
        delay_seconds=0.5,
        reason="rate_limit",
    )

    assert validator.is_valid(json.loads(event.model_dump_json()))
    for invalid_delay in (-1.0, float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            ProviderRetrying(
                turn=1,
                provider="openai",
                attempt=1,
                max_attempts=3,
                delay_seconds=invalid_delay,
                reason="rate_limit",
            )


def test_all_live_event_float_fields_reject_non_finite_values() -> None:
    estimate = ContextEstimate(
        system_tokens=0,
        message_tokens=0,
        tool_schema_tokens=0,
        total_tokens=0,
    )
    for invalid_value in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            ContextBudget(
                estimate=estimate,
                reserve_tokens=0,
                estimated_percent=invalid_value,
            )
        with pytest.raises(ValidationError):
            ContextPressure(
                turn=1,
                provider="openai",
                context_window=1,
                observed_tokens=1,
                remaining_tokens=0,
                pressure_ratio=invalid_value,
            )
