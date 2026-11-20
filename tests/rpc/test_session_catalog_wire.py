"""Additive v9 catalog discovery wire compatibility."""

import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from wisp.events import RpcSessionsReported
from wisp.rpc.commands import GetSessionsCommand
from wisp.rpc.protocol_schema import generate_protocol_artifacts


@pytest.mark.parametrize("schema_name", ["events.schema.json", "rust-events.schema.json"])
def test_older_catalog_report_omissions_remain_valid(schema_name: str) -> None:
    schema = json.loads(generate_protocol_artifacts()[schema_name])
    report = RpcSessionsReported(command_id="catalog").model_dump(mode="json")
    for name in ("query", "next_cursor", "previous_cursor"):
        report.pop(name, None)
    Draft202012Validator(schema).validate(report)
    report.update(query="strasse", next_cursor="opaque", previous_cursor=None)
    Draft202012Validator(schema).validate(report)
    report["next_cursor"] = 17
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(report)


def test_discovery_command_preserves_legacy_wire_shape() -> None:
    assert json.loads(GetSessionsCommand(id="catalog").to_json_line()) == {
        "type": "get_sessions",
        "id": "catalog",
        "limit": 50,
    }
    command = GetSessionsCommand(id="catalog", query="Straße", cursor="opaque")
    assert GetSessionsCommand.model_validate_json(command.to_json_line()) == command


def test_readonly_catalog_command_roundtrips() -> None:
    command = GetSessionsCommand(id="catalog", read_only=True)
    encoded = command.to_json_line()
    assert json.loads(encoded)["read_only"] is True
    assert GetSessionsCommand.model_validate_json(encoded) == command


@pytest.mark.parametrize("schema_name", ["commands.schema.json", "rust-commands.schema.json"])
def test_readonly_flag_is_optional(schema_name: str) -> None:
    schema = json.loads(generate_protocol_artifacts()[schema_name])
    validator = Draft202012Validator(schema)
    command = {"type": "get_sessions", "id": "catalog", "limit": 50}
    validator.validate(command)
    validator.validate({**command, "read_only": True})
    with pytest.raises(ValidationError):
        validator.validate({**command, "read_only": "true"})
