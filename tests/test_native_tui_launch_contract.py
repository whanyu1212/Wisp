"""Native TUI RPC handoff and CLI trust contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

import wisp.cli as cli_module
import wisp.cli.native_tui.launch as native_tui_launch
from wisp.cli import app
from wisp.cli.native_tui.launch import TuiOptions, _rpc_command
from wisp.cli.types import TuiFrontendKind
from wisp.config import WispConfig
from wisp.trust_flow import TrustDecision


def test_tui_rpc_command_forwards_tool_and_session_flags(tmp_path: Path) -> None:
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", model="model-x", session_dir=tmp_path),
            allow_read_tools=True,
            allowed_tools=("bash",),
            resume="session-123",
            approve_unsafe_tools=True,
            max_tool_iterations=3,
        )
    )

    assert command[:4] == (command[0], "-m", "wisp", "--mode")
    assert "rpc" in command
    assert ("--resume", "session-123") == (
        command[command.index("--resume")],
        command[command.index("--resume") + 1],
    )
    assert "--allow-read-tools" in command
    assert ("--allow-tool", "bash") == (
        command[command.index("--allow-tool")],
        command[command.index("--allow-tool") + 1],
    )
    assert "--yes" in command
    assert ("--max-tool-iterations", "3") == (
        command[command.index("--max-tool-iterations")],
        command[command.index("--max-tool-iterations") + 1],
    )


def test_tui_rpc_command_omits_trust_gated_config_flags(tmp_path: Path) -> None:
    # Provider/model/session-dir/auth-file are trust-gated: the parent must NOT launder
    # its untrusted-startup resolution into subprocess CLI flags (which would outrank a
    # trusted project's settings.json). With no explicit user override, they are absent.
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(
                provider="fake",
                model="model-x",
                session_dir=tmp_path,
                auth_path=tmp_path / "auth.json",
            ),
        )
    )

    assert "--provider" not in command
    assert "--model" not in command
    assert "--session-dir" not in command
    assert "--auth-file" not in command


def test_tui_rpc_command_forwards_explicit_user_overrides(tmp_path: Path) -> None:
    # An explicit provider/model/session-dir/auth-file the user passed on the command IS
    # forwarded: each is a legitimate highest-precedence override the subprocess cannot
    # otherwise know about.
    user_sessions = tmp_path / "user-sessions"
    user_auth = tmp_path / "user-auth.json"
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", session_dir=tmp_path),
            user_provider="fake",
            user_model="model-x",
            user_session_dir=user_sessions,
            user_auth_file=user_auth,
        )
    )

    assert ("--provider", "fake") == (
        command[command.index("--provider")],
        command[command.index("--provider") + 1],
    )
    assert ("--model", "model-x") == (
        command[command.index("--model")],
        command[command.index("--model") + 1],
    )
    assert ("--session-dir", str(user_sessions)) == (
        command[command.index("--session-dir")],
        command[command.index("--session-dir") + 1],
    )
    assert ("--auth-file", str(user_auth)) == (
        command[command.index("--auth-file")],
        command[command.index("--auth-file") + 1],
    )


def test_tui_rpc_command_includes_continue_latest(tmp_path: Path) -> None:
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", session_dir=tmp_path),
            continue_latest=True,
        )
    )

    assert "--continue" in command


def test_tui_rpc_command_passes_all_tools_to_the_subprocess(tmp_path: Path) -> None:
    # The TUI defaults to the full tool registry; the flag must reach the RPC
    # child so the spawned agent actually has tools (unsafe calls still prompt).
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", session_dir=tmp_path),
            all_tools=True,
        )
    )

    assert "--all-tools" in command
    # all_tools is availability, not auto-approval — unsafe calls still prompt.
    assert "--yes" not in command


def test_tui_rpc_command_omits_all_tools_when_disabled(tmp_path: Path) -> None:
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", session_dir=tmp_path),
            all_tools=False,
        )
    )

    assert "--all-tools" not in command


def test_tui_rpc_env_carries_preflight_trust_without_mutating_parent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_TRUST", "1")
    options = TuiOptions(
        config=WispConfig(provider="fake", session_dir=tmp_path),
        project_trusted=False,
    )

    child_env = native_tui_launch._rpc_env(options)

    assert child_env["WISP_TRUST"] == "0"
    assert os.environ["WISP_TRUST"] == "1"


def test_tui_rpc_env_forwards_explicit_config_effort(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Regression test (Codex review on #125): an embedder constructing
    # TuiOptions(config=WispConfig(effort=...)) directly -- bypassing
    # WISP_EFFORT/the settings file entirely -- must still reach the RPC
    # subprocess, or the frontend could display a tier the backend never
    # applies to any prompt. Unlike
    # provider/model/session_dir/auth_file, forwarding the resolved value
    # here carries no precedence-inversion risk: effort is never trust-gated,
    # so it resolves identically in both processes regardless of trust.
    monkeypatch.delenv("WISP_EFFORT", raising=False)
    options = TuiOptions(
        config=WispConfig(provider="fake", session_dir=tmp_path, effort="high"),
    )

    child_env = native_tui_launch._rpc_env(options)

    assert child_env["WISP_EFFORT"] == "high"
    assert "WISP_EFFORT" not in os.environ


def test_tui_rpc_env_forwards_embedded_openai_compatible_config(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("WISP_OPENAI_COMPATIBLE_CONFIG", raising=False)
    options = TuiOptions(
        config=WispConfig(
            provider="openai-compatible",
            session_dir=tmp_path,
            openai_compatible={
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "anthropic/claude-sonnet-4",
            },
        )
    )

    child_env = native_tui_launch._rpc_env(options)

    assert json.loads(child_env["WISP_OPENAI_COMPATIBLE_CONFIG"]) == {
        "base_url": "https://openrouter.ai/api/v1",
        "ca_bundle": None,
        "default_model": "anthropic/claude-sonnet-4",
        "provider_name": "openai-compatible",
        "requires_api_key": True,
    }
    assert "WISP_OPENAI_COMPATIBLE_CONFIG" not in os.environ


def test_tui_rpc_env_omits_effort_when_config_effort_is_unset(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("WISP_EFFORT", raising=False)
    options = TuiOptions(config=WispConfig(provider="fake", session_dir=tmp_path))

    child_env = native_tui_launch._rpc_env(options)

    assert "WISP_EFFORT" not in child_env


@pytest.mark.parametrize("enabled", [True, False])
def test_tui_rpc_env_forwards_context_policy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    enabled: bool,
) -> None:
    monkeypatch.delenv("WISP_AUTO_COMPACTION", raising=False)
    monkeypatch.delenv("WISP_CONTEXT_RESERVE_TOKENS", raising=False)
    options = TuiOptions(
        config=WispConfig(
            provider="fake",
            session_dir=tmp_path,
            auto_compaction_enabled=enabled,
            context_reserve_tokens=4096,
        )
    )

    child_env = native_tui_launch._rpc_env(options)

    assert child_env["WISP_AUTO_COMPACTION"] == ("1" if enabled else "0")
    assert child_env["WISP_CONTEXT_RESERVE_TOKENS"] == "4096"


@pytest.mark.parametrize("arguments", [["--mode", "tui"], ["tui"]])
@pytest.mark.parametrize("trusted", [False, True])
def test_cli_resolves_trust_before_native_launch(
    arguments: list[str], trusted: bool, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    order: list[str] = []

    def resolve(project_path: Path) -> TrustDecision:
        order.append("trust")
        return TrustDecision(project_path=project_path, trusted=trusted)

    def launch(**kwargs: object) -> None:
        order.append("launch")
        assert kwargs["project_trusted"] is trusted
        assert kwargs["renderer"] is TuiFrontendKind.rust
        assert isinstance(kwargs["config"], WispConfig)
        assert kwargs["config"].provider == "fake"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "_resolve_cli_trust", resolve)
    monkeypatch.setattr(cli_module, "_run_tui_from_cli_options", launch)

    result = CliRunner().invoke(app, arguments, env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""})

    assert result.exit_code == 0, result.output
    assert order == ["trust", "launch"]


def test_native_tui_loads_trusted_project_settings_from_subdirectory(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from wisp.trust import record_trust

    captured: list[dict[str, object]] = []
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (project / ".wisp").mkdir()
    (project / ".wisp" / "settings.json").write_text('{"model": "project-model"}', encoding="utf-8")
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    record_trust(project, True, trust_path=trust_file)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(
        cli_module, "_run_tui_from_cli_options", lambda **kwargs: captured.append(kwargs)
    )

    result = CliRunner().invoke(app, ["tui"], env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""})

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    config = captured[0]["config"]
    assert isinstance(config, WispConfig)
    assert config.model == "project-model"
    assert captured[0]["project_trusted"] is True


def test_native_tui_command_forwards_explicit_auth_and_session_overrides(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module, "_run_tui_from_cli_options", lambda **kwargs: captured.append(kwargs)
    )
    auth_file = tmp_path / "auth.json"
    sessions = tmp_path / "sessions"

    result = CliRunner().invoke(
        app,
        ["tui", "--auth-file", str(auth_file), "--session-dir", str(sessions)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0]["user_auth_file"] == auth_file
    assert captured[0]["user_session_dir"] == sessions
    assert captured[0]["all_tools"] is True


def test_compatibility_tui_mode_forwards_explicit_provider_model_and_session(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module, "_run_tui_from_cli_options", lambda **kwargs: captured.append(kwargs)
    )

    result = CliRunner().invoke(
        app,
        [
            "--mode",
            "tui",
            "--provider",
            "fake",
            "--model",
            "model-x",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0]["user_provider"] == "fake"
    assert captured[0]["user_model"] == "model-x"
    assert captured[0]["user_session_dir"] == tmp_path
    assert captured[0]["all_tools"] is True


@pytest.mark.parametrize(
    "arguments", [["tui", "--no-all-tools"], ["--mode", "tui", "--no-all-tools"]]
)
def test_explicit_no_all_tools_overrides_native_tui_default(
    arguments: list[str], monkeypatch: MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli_module, "_run_tui_from_cli_options", lambda **kwargs: captured.append(kwargs)
    )

    result = CliRunner().invoke(
        app, arguments, env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"}
    )

    assert result.exit_code == 0, result.output
    assert captured[0]["all_tools"] is False
