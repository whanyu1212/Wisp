"""Tests for the `wisp mcp` commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner, Result

from wisp.cli import app
from wisp.config.settings import user_settings_path

SECRET = "secret-sentinel"


@pytest.fixture
def home() -> Path:
    # The autouse conftest fixture already points the home directory at a temp dir.
    return Path.home()


@pytest.fixture
def terminal(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("wisp.cli.mcp._interactive", lambda: True)


def _run(args: list[str], *, input: str | None = None) -> Result:
    return CliRunner().invoke(app, ["mcp", *args], input=input)


def _servers(home: Path) -> dict[str, object]:
    path = user_settings_path(home_dir=home)
    if not path.exists():
        return {}
    servers = json.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {})
    assert isinstance(servers, dict)
    return servers


def test_add_with_yes_saves_command_and_shows_it(home: Path) -> None:
    result = _run(
        ["add", "github", "--env-from", "GITHUB_TOKEN", "--yes", "--", "npx", "-y", "pkg"]
    )

    assert result.exit_code == 0, result.output
    assert "runs: npx -y pkg" in result.output
    assert "forwards: GITHUB_TOKEN" in result.output
    assert "Restart Wisp" in result.output
    assert _servers(home) == {
        "github": {"command": "npx", "args": ["-y", "pkg"], "env_from": ["GITHUB_TOKEN"]}
    }


def test_add_url_server(home: Path) -> None:
    result = _run(["add", "docs", "--url", "https://example.com/mcp", "--yes"])

    assert result.exit_code == 0, result.output
    assert _servers(home) == {"docs": {"url": "https://example.com/mcp"}}


def test_add_needs_exactly_one_transport(home: Path) -> None:
    neither = _run(["add", "x", "--yes"])
    both = _run(["add", "x", "--url", "https://example.com", "--yes", "--", "npx"])

    assert neither.exit_code == both.exit_code == 1
    assert _servers(home) == {}


def test_add_without_terminal_or_yes_saves_nothing(home: Path) -> None:
    result = _run(["add", "github", "--", "npx"])

    assert result.exit_code == 1
    assert "pass --yes" in result.output
    assert _servers(home) == {}


def test_add_asks_before_saving(home: Path, terminal: None) -> None:
    declined = _run(["add", "github", "--", "npx", "pkg"], input="n\n")
    assert declined.exit_code == 1
    assert "Wisp runs this command every time it starts." in declined.output
    assert _servers(home) == {}

    accepted = _run(["add", "github", "--", "npx", "pkg"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "github" in _servers(home)


def test_add_env_value_is_masked_and_never_printed(home: Path, terminal: None) -> None:
    result = _run(["add", "github", "--env", "TOKEN", "--", "npx"], input=f"{SECRET}\ny\n")

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "sets: TOKEN (values hidden)" in result.output
    assert _servers(home)["github"] == {"command": "npx", "env": {"TOKEN": SECRET}}

    listed = _run(["list"])
    assert listed.exit_code == 0
    assert SECRET not in listed.output
    assert "TOKEN (values hidden)" in listed.output


def test_add_env_requires_terminal(home: Path) -> None:
    result = _run(["add", "github", "--env", "TOKEN", "--yes", "--", "npx"], input=f"{SECRET}\n")

    assert result.exit_code == 1
    assert "--env-from" in result.output
    assert _servers(home) == {}


def test_add_existing_name_suggests_replace(home: Path) -> None:
    assert _run(["add", "github", "--yes", "--", "npx"]).exit_code == 0

    duplicate = _run(["add", "github", "--yes", "--", "uvx"])
    assert duplicate.exit_code == 1
    assert "pass --replace to overwrite" in duplicate.output

    replaced = _run(["add", "github", "--replace", "--yes", "--", "uvx"])
    assert replaced.exit_code == 0, replaced.output
    assert _servers(home) == {"github": {"command": "uvx"}}


def test_add_without_double_dash_does_not_read_server_flags_as_options(home: Path) -> None:
    # npx's own `-y` must not become Wisp's `--yes` and skip the confirmation.
    result = _run(["add", "github", "npx", "-y", "pkg"])

    assert result.exit_code != 0
    assert _servers(home) == {}


def test_server_flags_after_double_dash_reach_the_server(home: Path) -> None:
    result = _run(["add", "x", "--yes", "--", "node", "server.js", "--env-from", "FOO", "-y"])

    assert result.exit_code == 0, result.output
    assert _servers(home)["x"] == {
        "command": "node",
        "args": ["server.js", "--env-from", "FOO", "-y"],
    }


def test_add_shows_command_before_asking_for_secrets(home: Path, terminal: None) -> None:
    result = _run(["add", "github", "--env", "TOKEN", "--", "npx", "pkg"], input=f"{SECRET}\nn\n")

    assert result.exit_code == 1
    assert result.output.index("runs: npx pkg") < result.output.index("Value for TOKEN")
    assert _servers(home) == {}


def test_add_rejects_invalid_server(home: Path) -> None:
    result = _run(["add", "Bad Name", "--yes", "--", "npx"])

    assert result.exit_code == 1
    assert "is invalid" in result.output
    assert _servers(home) == {}


def test_control_characters_are_neutralized_in_output(home: Path) -> None:
    result = _run(["add", "x", "--yes", "--", "npx", "\x1b[31mpkg", "a‮b"])

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output
    assert "‮" not in result.output
    assert "\N{REPLACEMENT CHARACTER}[31mpkg" in result.output
    # The saved command is the real one; only the display is neutralized.
    assert _servers(home)["x"] == {"command": "npx", "args": ["\x1b[31mpkg", "a‮b"]}


def test_list_shows_state_and_empty_message(home: Path) -> None:
    empty = _run(["list"])
    assert empty.exit_code == 0
    assert "No MCP servers configured" in empty.output

    _run(["add", "github", "--yes", "--", "npx"])
    _run(["add", "docs", "--url", "https://example.com/mcp", "--yes"])
    _run(["disable", "docs"])

    listed = _run(["list"])
    assert listed.exit_code == 0
    assert "docs  (disabled)" in listed.output
    assert "github  (enabled)" in listed.output
    assert listed.output.index("docs") < listed.output.index("github")


def test_disable_enable_and_remove(home: Path, terminal: None) -> None:
    _run(["add", "github", "--yes", "--", "npx", "pkg"])

    disabled = _run(["disable", "github"])
    assert disabled.exit_code == 0, disabled.output
    assert _servers(home)["github"] == {"command": "npx", "args": ["pkg"], "enabled": False}

    declined = _run(["enable", "github"], input="n\n")
    assert declined.exit_code == 1
    assert "runs: npx pkg" in declined.output
    assert _servers(home)["github"] == {"command": "npx", "args": ["pkg"], "enabled": False}

    enabled = _run(["enable", "github"], input="y\n")
    assert enabled.exit_code == 0, enabled.output
    assert _servers(home)["github"] == {"command": "npx", "args": ["pkg"]}

    kept = _run(["remove", "github"], input="n\n")
    assert kept.exit_code == 1
    assert "github" in _servers(home)

    removed = _run(["remove", "github"], input="y\n")
    assert removed.exit_code == 0, removed.output
    assert _servers(home) == {}


def test_enable_without_terminal_or_yes_changes_nothing(home: Path) -> None:
    _run(["add", "github", "--yes", "--", "npx"])
    _run(["disable", "github"])

    result = _run(["enable", "github"])

    assert result.exit_code == 1
    assert _servers(home)["github"] == {"command": "npx", "enabled": False}


def test_missing_server_errors(home: Path) -> None:
    for args in (["remove", "nope", "--yes"], ["enable", "nope", "--yes"], ["disable", "nope"]):
        result = _run(args)
        assert result.exit_code == 1, args
        assert "no MCP server named 'nope'" in result.output


def _snippet(tmp_path: Path, env: dict[str, str]) -> Path:
    path = tmp_path / "snippet.json"
    path.write_text(
        json.dumps({"mcpServers": {"github": {"command": "npx", "args": ["pkg"], "env": env}}}),
        encoding="utf-8",
    )
    return path


def test_import_from_stdin_requires_yes(home: Path) -> None:
    snippet = json.dumps({"mcpServers": {"github": {"command": "npx"}}})

    refused = _run(["import", "-"], input=snippet)
    assert refused.exit_code == 1
    assert _servers(home) == {}

    accepted = _run(["import", "-", "--yes"], input=snippet)
    assert accepted.exit_code == 0, accepted.output
    assert _servers(home) == {"github": {"command": "npx"}}


def test_import_asks_for_each_env_value(home: Path, terminal: None) -> None:
    snippet = _snippet(home, {"TOKEN": "<your token>", "ORG": "<org>"})

    # Prompts follow name order: ORG is left empty and forwarded instead, and
    # TOKEN gets a real value.
    result = _run(["import", str(snippet)], input=f"\n{SECRET}\ny\n")

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "<your token>" not in result.output
    assert _servers(home)["github"] == {
        "command": "npx",
        "args": ["pkg"],
        "env": {"TOKEN": SECRET},
        "env_from": ["ORG"],
    }


def test_import_with_yes_keeps_snippet_env_without_printing_it(home: Path) -> None:
    snippet = _snippet(home, {"TOKEN": SECRET})

    result = _run(["import", str(snippet), "--yes"])

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert _servers(home)["github"] == {"command": "npx", "args": ["pkg"], "env": {"TOKEN": SECRET}}


def _write_snippet(home: Path, names: list[str]) -> Path:
    path = home / "many.json"
    path.write_text(
        json.dumps({"mcpServers": {name: {"command": name} for name in names}}), encoding="utf-8"
    )
    return path


def test_import_with_a_name_conflict_saves_nothing(home: Path) -> None:
    _run(["add", "github", "--yes", "--", "npx"])
    snippet = _write_snippet(home, ["aaa", "github", "zzz"])

    refused = _run(["import", str(snippet), "--yes"])
    assert refused.exit_code == 1
    assert "pass --replace to overwrite" in refused.output
    assert _servers(home) == {"github": {"command": "npx"}}

    replaced = _run(["import", str(snippet), "--yes", "--replace"])
    assert replaced.exit_code == 0, replaced.output
    assert _servers(home) == {
        "aaa": {"command": "aaa"},
        "github": {"command": "github"},
        "zzz": {"command": "zzz"},
    }


def test_import_shows_every_server_then_asks_once(home: Path, terminal: None) -> None:
    snippet = _write_snippet(home, ["aaa", "zzz"])

    declined = _run(["import", str(snippet)], input="n\n")
    assert declined.exit_code == 1
    assert "runs: aaa" in declined.output
    assert "runs: zzz" in declined.output
    assert "Save all 2?" in declined.output
    assert _servers(home) == {}

    accepted = _run(["import", str(snippet)], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert set(_servers(home)) == {"aaa", "zzz"}


def test_import_rejects_unsupported_snippet(home: Path) -> None:
    path = home / "snippet.json"
    path.write_text(
        json.dumps({"mcpServers": {"x": {"command": "npx", "headers": {"A": SECRET}}}}),
        encoding="utf-8",
    )

    result = _run(["import", str(path), "--yes"])

    assert result.exit_code == 1
    assert "do not accept headers" in result.output
    assert SECRET not in result.output
    assert _servers(home) == {}


def test_import_of_a_deeply_nested_snippet_fails_cleanly(home: Path) -> None:
    path = home / "deep.json"
    path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")

    result = _run(["import", str(path), "--yes"])

    assert result.exit_code == 1
    assert "nested too deeply" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    ("option", "template"),
    [
        ("Authorization=Bearer LINEAR_API_KEY", "Bearer ${LINEAR_API_KEY}"),
        ("Authorization=Bearer ${LINEAR_API_KEY}", "Bearer ${LINEAR_API_KEY}"),
        ("Authorization=LINEAR_API_KEY", "${LINEAR_API_KEY}"),
    ],
)
def test_add_header_from_names_the_variable_and_where_it_goes(
    home: Path, option: str, template: str
) -> None:
    result = _run(
        ["add", "linear", "--url", "https://mcp.linear.app/mcp", "--header-from", option, "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "sends $LINEAR_API_KEY to mcp.linear.app in header Authorization" in result.output
    assert _servers(home) == {
        "linear": {
            "url": "https://mcp.linear.app/mcp",
            "headers_from": {"Authorization": template},
        }
    }


def test_add_header_value_is_masked_and_never_printed(home: Path, terminal: None) -> None:
    result = _run(
        ["add", "linear", "--url", "https://mcp.linear.app/mcp", "--header", "Authorization"],
        input=f"Bearer {SECRET}\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "sends headers: Authorization (values hidden)" in result.output
    assert _servers(home)["linear"]["headers"] == {"Authorization": f"Bearer {SECRET}"}
    listed = _run(["list"])
    assert SECRET not in listed.output


@pytest.mark.parametrize(
    "option", ["Authorization", "Authorization=Bearer sk-live-1234 TOKEN", "Authorization="]
)
def test_add_rejects_a_header_from_that_is_not_a_variable(home: Path, option: str) -> None:
    result = _run(
        ["add", "linear", "--url", "https://mcp.linear.app/mcp", "--header-from", option, "--yes"]
    )

    assert result.exit_code != 0
    assert "sk-live-1234" not in result.output
    assert _servers(home) == {}


def test_add_header_on_a_command_server_is_rejected(home: Path) -> None:
    result = _run(["add", "x", "--header-from", "Authorization=TOKEN", "--yes", "--", "npx"])

    assert result.exit_code != 0
    assert _servers(home) == {}


def test_login_opens_the_sign_in_page_and_logout_forgets_it(
    home: Path, monkeypatch: MonkeyPatch
) -> None:
    from urllib.parse import parse_qs, urlsplit

    import httpx

    from tests.mcp.test_http_transport import _mock_http
    from tests.mcp.test_oauth import ISSUER, MCP_URL, _AuthorizationServer
    from wisp.mcp.credentials import McpTokenStore

    _mock_http(monkeypatch, _AuthorizationServer().handle)

    def approve(url: str) -> bool:
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        httpx.get(query["redirect_uri"], params={"code": "code-1", "state": query["state"]})
        return True

    monkeypatch.setattr("wisp.cli.mcp.webbrowser.open", approve)
    assert _run(["add", "linear", "--url", MCP_URL, "--yes"]).exit_code == 0

    login = _run(["login", "linear"])

    assert login.exit_code == 0, login.output
    assert f"{ISSUER}/authorize?" in login.output
    assert "Signed in to linear." in login.output
    assert "access-1" not in login.output
    assert McpTokenStore().load("linear", MCP_URL) is not None

    logout = _run(["logout", "linear"])
    assert logout.exit_code == 0, logout.output
    assert McpTokenStore().load("linear", MCP_URL) is None
    assert _run(["logout", "linear"]).exit_code != 0


def test_login_refuses_a_command_server(home: Path) -> None:
    assert _run(["add", "x", "--yes", "--", "npx"]).exit_code == 0

    result = _run(["login", "x"])

    assert result.exit_code != 0
    assert "only HTTP servers sign in" in result.output


def test_import_asks_for_each_header_value_masked(home: Path, terminal: None) -> None:
    path = home / "snippet.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "linear": {
                        "type": "http",
                        "url": "https://mcp.linear.app/mcp",
                        "headers": {"Authorization": "Bearer <token>"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(["import", str(path)], input=f"Bearer {SECRET}\ny\n")

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "sends headers: Authorization (values hidden)" in result.output
    assert _servers(home)["linear"]["headers"] == {"Authorization": f"Bearer {SECRET}"}


def _write_header_snippet(home: Path, headers: dict[str, str]) -> Path:
    path = home / "snippet.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "linear": {
                        "type": "http",
                        "url": "https://mcp.linear.app/mcp",
                        "headers": headers,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_import_accepts_empty_header_placeholders_and_asks_for_them(
    home: Path, terminal: None
) -> None:
    path = _write_header_snippet(home, {"X-Api-Key": "", "Authorization": "Bearer "})

    # Values are asked for in header-name order, and surrounding spaces dropped.
    result = _run(["import", str(path)], input=f"Bearer {SECRET}\n  key-{SECRET} \ny\n")

    assert result.exit_code == 0, result.output
    assert _servers(home)["linear"]["headers"] == {
        "Authorization": f"Bearer {SECRET}",
        "X-Api-Key": f"key-{SECRET}",
    }


def test_import_with_yes_refuses_a_header_placeholder(home: Path) -> None:
    path = _write_header_snippet(home, {"X-Api-Key": ""})

    result = _run(["import", str(path), "--yes"])

    assert result.exit_code == 1
    assert "header without a usable value" in result.output
    assert _servers(home) == {}
