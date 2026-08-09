"""Tests for MCP server configuration contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from wisp.config import WispConfig
from wisp.mcp.config import McpServerConfig


def test_mcp_server_config_normalizes_nested_mappings() -> None:
    server = McpServerConfig.model_validate(
        {
            "name": "github",
            "command": "docker",
            "args": ["run", "--rm"],
            "env": {"Z_TOKEN": "z", "A_TOKEN": "a"},
            "env_from": ["USER_TOKEN", "GITHUB_TOKEN"],
            "tool_safety": {"write-file": "mutating", "read-file": "read"},
        }
    )

    assert server.args == ("run", "--rm")
    assert [(name, value.get_secret_value()) for name, value in server.env] == [
        ("A_TOKEN", "a"),
        ("Z_TOKEN", "z"),
    ]
    assert server.env_from == ("GITHUB_TOKEN", "USER_TOKEN")
    assert server.tool_safety == (("read-file", "read"), ("write-file", "mutating"))


def test_mcp_server_config_is_frozen_and_hides_literal_environment() -> None:
    server = McpServerConfig(
        name="github",
        command="server",
        env={"TOKEN": "super-secret"},
    )

    with pytest.raises(ValidationError, match="frozen"):
        server.command = "other"  # type: ignore[misc]

    assert "super-secret" not in repr(server)
    assert "super-secret" not in repr(WispConfig(mcp_servers=(server,)))
    assert "super-secret" not in server.model_dump_json()


def test_mcp_server_config_json_round_trip_uses_mapping_shapes() -> None:
    server = McpServerConfig(
        name="github",
        command="server",
        tool_safety={"write-file": "mutating", "read-file": "read"},
    )

    serialized = server.model_dump_json()
    data = json.loads(serialized)

    assert data["env"] == {}
    assert data["tool_safety"] == {"read-file": "read", "write-file": "mutating"}
    assert McpServerConfig.model_validate_json(serialized) == server
    schema = McpServerConfig.model_json_schema()
    assert schema["properties"]["env"]["type"] == "object"
    assert schema["properties"]["tool_safety"]["type"] == "object"

    config = WispConfig(mcp_servers=(server,))
    assert WispConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Uppercase"),
        ("command", "   "),
        ("command", "bad\x00command"),
        ("args", ["bad\x00argument"]),
        ("args", ["argument"] * 65),
        ("env", {"INVALID-NAME": "value"}),
        ("env", [["TOKEN", "value"]]),
        ("env_from", ["TOKEN", "TOKEN"]),
        ("tool_safety", {"tool": "safe"}),
        ("unknown", True),
    ],
)
def test_mcp_server_config_rejects_invalid_values(field: str, value: object) -> None:
    data: dict[str, object] = {"name": "server", "command": "command", field: value}

    with pytest.raises(ValidationError):
        McpServerConfig.model_validate(data)


def test_mcp_server_config_rejects_overlapping_environment_sources() -> None:
    with pytest.raises(ValidationError, match="env and env_from"):
        McpServerConfig(
            name="server",
            command="command",
            env={"TOKEN": "literal"},
            env_from=("TOKEN",),
        )


def test_mcp_validation_error_hides_environment_value() -> None:
    secret = "super-secret\x00"

    with pytest.raises(ValidationError) as captured:
        McpServerConfig(
            name="server",
            command="command",
            env={"TOKEN": secret},
        )

    assert secret not in str(captured.value)
    assert secret not in captured.value.json()
    assert all(error.get("input") == "<redacted>" for error in captured.value.errors())


def test_mcp_length_validation_error_hides_environment_value() -> None:
    secret = "s" * 16_385

    with pytest.raises(ValidationError) as captured:
        McpServerConfig(
            name="server",
            command="command",
            env={"TOKEN": secret},
        )

    assert secret not in captured.value.json()
    assert all(error.get("input") == "<redacted>" for error in captured.value.errors())


def test_wisp_config_sorts_and_rejects_duplicate_server_names() -> None:
    alpha = McpServerConfig(name="alpha", command="alpha-server")
    beta = McpServerConfig(name="beta", command="beta-server")

    config = WispConfig(mcp_servers=(beta, alpha))

    assert config.mcp_servers == (alpha, beta)
    with pytest.raises(ValidationError, match="names must be unique"):
        WispConfig(mcp_servers=(alpha, alpha))


def test_duplicate_server_error_hides_nested_environment_values() -> None:
    secret = "super-secret"

    with pytest.raises(ValidationError) as captured:
        WispConfig.model_validate(
            {
                "mcp_servers": [
                    {
                        "name": "duplicate",
                        "command": "first",
                        "env": {"TOKEN": secret},
                    },
                    {
                        "name": "duplicate",
                        "command": "second",
                        "env": {"TOKEN": secret},
                    },
                ]
            }
        )

    assert secret not in captured.value.json()
    assert all(error.get("input") == "<redacted>" for error in captured.value.errors())


def test_wisp_config_limits_server_count() -> None:
    servers = tuple(
        McpServerConfig(name=f"server-{index}", command="command") for index in range(17)
    )

    with pytest.raises(ValidationError):
        WispConfig(mcp_servers=servers)
