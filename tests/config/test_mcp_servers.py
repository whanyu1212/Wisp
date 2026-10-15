"""Tests for saving MCP servers to the user settings file."""

from __future__ import annotations

import asyncio
import json
import stat
import threading
from pathlib import Path

import pytest

from wisp.config.mcp_servers import (
    McpServerExistsError,
    McpSettingsError,
    add_user_mcp_servers,
    load_user_mcp_servers,
    parse_mcp_snippet,
    remove_user_mcp_server,
    same_launch,
    set_user_mcp_server_enabled,
    validated_mcp_server,
)
from wisp.config.settings import (
    persist_user_model_selection,
    resolve_settings,
    user_settings_path,
)
from wisp.mcp.config import McpServerConfig
from wisp.runtime.extensions import build_runtime
from wisp.tools.selection import select_tools


def _settings(home: Path) -> dict[str, object]:
    loaded = json.loads(user_settings_path(home_dir=home).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_settings(home: Path, data: object) -> Path:
    path = user_settings_path(home_dir=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _stdio(name: str = "github", **extra: object) -> McpServerConfig:
    return validated_mcp_server(name, {"command": "npx", "args": ["-y", "server"], **extra})


def test_add_keeps_other_settings_and_unknown_keys(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        {
            "provider": "openai",
            "future_setting": {"kept": True},
            "mcp_servers": {"docs": {"url": "https://example.com/mcp"}},
        },
    )

    add_user_mcp_servers((_stdio(env_from=["GITHUB_TOKEN"]),), home_dir=tmp_path)

    assert _settings(tmp_path) == {
        "provider": "openai",
        "future_setting": {"kept": True},
        "mcp_servers": {
            "docs": {"url": "https://example.com/mcp"},
            "github": {
                "command": "npx",
                "args": ["-y", "server"],
                "env_from": ["GITHUB_TOKEN"],
            },
        },
    }


def test_add_creates_private_file_and_stores_real_env_values(tmp_path: Path) -> None:
    add_user_mcp_servers((_stdio(env={"TOKEN": "secret-sentinel"}),), home_dir=tmp_path)

    path = user_settings_path(home_dir=tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    servers = _settings(tmp_path)["mcp_servers"]
    assert isinstance(servers, dict)
    assert servers["github"]["env"] == {"TOKEN": "secret-sentinel"}
    (loaded,) = load_user_mcp_servers(home_dir=tmp_path)
    assert dict((name, value.get_secret_value()) for name, value in loaded.env) == {
        "TOKEN": "secret-sentinel"
    }


def test_add_refuses_existing_name_unless_replace(tmp_path: Path) -> None:
    add_user_mcp_servers((_stdio(),), home_dir=tmp_path)

    with pytest.raises(McpServerExistsError):
        add_user_mcp_servers((_stdio(env_from=["X"]),), home_dir=tmp_path)
    (unchanged,) = load_user_mcp_servers(home_dir=tmp_path)
    assert unchanged.env_from == ()

    add_user_mcp_servers((_stdio(env_from=["X"]),), replace=True, home_dir=tmp_path)
    (replaced,) = load_user_mcp_servers(home_dir=tmp_path)
    assert replaced.env_from == ("X",)


@pytest.mark.parametrize(
    "document",
    [
        {"mcp_servers": {"bad": {"command": "npx", "headers": {"A": "b"}}}},
        {"mcp_servers": {"good": {"command": "npx"}}, "provider": 3},
    ],
    ids=["invalid-server", "invalid-other-field"],
)
def test_change_to_invalid_file_is_refused_and_leaves_it_unchanged(
    tmp_path: Path, document: dict[str, object]
) -> None:
    path = _write_settings(tmp_path, document)
    before = path.read_bytes()

    with pytest.raises(McpSettingsError) as error:
        add_user_mcp_servers((_stdio(),), home_dir=tmp_path)

    assert path.read_bytes() == before
    expected_field = "mcp_servers.bad" if "bad" in str(document) else "provider"
    assert expected_field in str(error.value)


def test_invalid_server_error_does_not_echo_values(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"mcp_servers": {"bad": {"command": "npx", "env": {"T": 123456789}}}})

    with pytest.raises(McpSettingsError) as error:
        load_user_mcp_servers(home_dir=tmp_path)

    assert "mcp_servers.bad" in str(error.value)
    assert "123456789" not in str(error.value)


def test_remove_and_enable_round_trip(tmp_path: Path) -> None:
    add_user_mcp_servers((_stdio(),), home_dir=tmp_path)
    add_user_mcp_servers((_stdio("docs"),), home_dir=tmp_path)

    set_user_mcp_server_enabled("github", False, home_dir=tmp_path)
    servers = _settings(tmp_path)["mcp_servers"]
    assert isinstance(servers, dict)
    assert servers["github"]["enabled"] is False
    assert [server.enabled for server in load_user_mcp_servers(home_dir=tmp_path)] == [
        True,
        False,
    ]

    set_user_mcp_server_enabled("github", True, home_dir=tmp_path)
    servers = _settings(tmp_path)["mcp_servers"]
    assert isinstance(servers, dict)
    assert "enabled" not in servers["github"]

    remove_user_mcp_server("github", home_dir=tmp_path)
    remove_user_mcp_server("docs", home_dir=tmp_path)
    assert "mcp_servers" not in _settings(tmp_path)


def test_changing_a_missing_server_fails(tmp_path: Path) -> None:
    with pytest.raises(McpSettingsError, match="no MCP server named 'nope'"):
        remove_user_mcp_server("nope", home_dir=tmp_path)
    with pytest.raises(McpSettingsError, match="no MCP server named 'nope'"):
        set_user_mcp_server_enabled("nope", False, home_dir=tmp_path)


def test_adding_several_servers_is_all_or_nothing(tmp_path: Path) -> None:
    add_user_mcp_servers((_stdio(),), home_dir=tmp_path)

    with pytest.raises(McpServerExistsError, match="'github'"):
        add_user_mcp_servers((_stdio("aaa"), _stdio(), _stdio("zzz")), home_dir=tmp_path)

    assert [server.name for server in load_user_mcp_servers(home_dir=tmp_path)] == ["github"]


def test_null_entry_can_be_removed(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"mcp_servers": {"broken": None, "ok": {"command": "npx"}}})

    remove_user_mcp_server("broken", home_dir=tmp_path)

    assert _settings(tmp_path) == {"mcp_servers": {"ok": {"command": "npx"}}}


def test_enable_refuses_a_server_that_changed_since_it_was_shown(tmp_path: Path) -> None:
    add_user_mcp_servers((_stdio(enabled=False),), home_dir=tmp_path)
    (shown,) = load_user_mcp_servers(home_dir=tmp_path)
    add_user_mcp_servers((_stdio(command="curl", enabled=False),), replace=True, home_dir=tmp_path)

    with pytest.raises(McpSettingsError, match="changed while you were confirming"):
        set_user_mcp_server_enabled(
            "github", True, confirmed=lambda saved: same_launch(saved, shown), home_dir=tmp_path
        )
    (current,) = load_user_mcp_servers(home_dir=tmp_path)
    assert not current.enabled

    set_user_mcp_server_enabled(
        "github", True, confirmed=lambda saved: same_launch(saved, current), home_dir=tmp_path
    )
    (enabled,) = load_user_mcp_servers(home_dir=tmp_path)
    assert enabled.enabled


def test_concurrent_writers_all_land(tmp_path: Path) -> None:
    names = [f"server-{index}" for index in range(8)]
    threads = [
        threading.Thread(
            target=add_user_mcp_servers, args=((_stdio(name),),), kwargs={"home_dir": tmp_path}
        )
        for name in names
    ]
    model_writer = threading.Thread(
        target=persist_user_model_selection,
        args=("openai", "gpt-test", None),
        kwargs={"home_dir": tmp_path},
    )
    for thread in [*threads, model_writer]:
        thread.start()
    for thread in [*threads, model_writer]:
        thread.join()

    assert [server.name for server in load_user_mcp_servers(home_dir=tmp_path)] == sorted(names)
    assert _settings(tmp_path)["provider"] == "openai"


def test_model_selection_writer_keeps_an_invalid_server_list(tmp_path: Path) -> None:
    invalid = {"bad": {"command": "npx", "headers": {"A": "b"}}}
    _write_settings(tmp_path, {"mcp_servers": invalid})

    persist_user_model_selection("openai", "gpt-test", None, home_dir=tmp_path)

    settings = _settings(tmp_path)
    assert settings["mcp_servers"] == invalid
    assert settings["provider"] == "openai"


def test_disabled_server_is_loaded_but_not_started(tmp_path: Path) -> None:
    add_user_mcp_servers((_stdio(),), home_dir=tmp_path)
    set_user_mcp_server_enabled("github", False, home_dir=tmp_path)

    settings = resolve_settings(project_dir=tmp_path / "project", home_dir=tmp_path)
    assert settings.mcp_servers is not None
    assert [(server.name, server.enabled) for server in settings.mcp_servers] == [("github", False)]

    async def start() -> None:
        runtime = await build_runtime(mcp_servers=settings.mcp_servers or ())
        try:
            assert runtime.mcp_runtime is None
            # An explicit selection of a disabled server's tool is withheld, not fatal.
            selected = select_tools(
                runtime.tools,
                allowed_tools=("mcp__github__search",),
                ignored_unknown_prefixes=runtime.unavailable_tool_prefixes,
            )
            assert "mcp__github__search" not in selected.names()
        finally:
            await runtime.aclose()

    asyncio.run(start())


def test_parse_snippet_accepts_published_shapes() -> None:
    servers = parse_mcp_snippet(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-github"],
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "<token>"},
                    },
                    "docs": {"type": "http", "url": "https://example.com/mcp"},
                }
            }
        )
    )

    assert [server.name for server in servers] == ["github", "docs"]
    assert servers[0].command == "npx"
    assert servers[1].url == "https://example.com/mcp"


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("{not json", "not valid JSON"),
        ('{"mcpServers": {}}', '"mcpServers" object'),
        ('{"servers": {"a": {"command": "x"}}}', '"mcpServers" object'),
        ('{"mcpServers": {"a": {"command": "x", "cwd": "/"}}}', "unsupported settings: cwd"),
        ('{"mcpServers": {"a": {"command": "x", "type": "http"}}}', "does not match"),
        ('{"mcpServers": {"a": {"url": "https://e.com", "type": "stdio"}}}', "does not match"),
        ('{"mcpServers": {"a": "npx"}}', "must be a JSON object"),
        ('{"mcpServers": {"Bad Name": {"command": "x"}}}', "is invalid"),
    ],
)
def test_parse_snippet_rejects_unsupported_input(snippet: str, message: str) -> None:
    with pytest.raises(McpSettingsError, match=message):
        parse_mcp_snippet(snippet)
