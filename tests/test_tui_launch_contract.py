"""Launch and CLI contracts that do not mount a Textual app."""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

import wisp.cli as cli_module
import wisp.tui.app as tui_app_module
from tests.tui_support import ScriptedController, _console, _reader_from
from wisp import tui as tui_module
from wisp.cli import app
from wisp.config import WispConfig
from wisp.trust_flow import TrustDecision
from wisp.tui import FullscreenTuiRenderer, TuiOptions, TuiRendererKind
from wisp.tui.app import _rpc_command


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

    child_env = tui_app_module._rpc_env(options)

    assert child_env["WISP_TRUST"] == "0"
    assert os.environ["WISP_TRUST"] == "1"


def test_tui_rpc_env_forwards_explicit_config_effort(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Regression test (Codex review on #125): an embedder constructing
    # TuiOptions(config=WispConfig(effort=...)) directly -- bypassing
    # WISP_EFFORT/the settings file entirely -- must still reach the RPC
    # subprocess, or the parent shell/model picker would show and persist a
    # tier the backend never applies to any prompt. Unlike
    # provider/model/session_dir/auth_file, forwarding the resolved value
    # here carries no precedence-inversion risk: effort is never trust-gated,
    # so it resolves identically in both processes regardless of trust.
    monkeypatch.delenv("WISP_EFFORT", raising=False)
    options = TuiOptions(
        config=WispConfig(provider="fake", session_dir=tmp_path, effort="high"),
    )

    child_env = tui_app_module._rpc_env(options)

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

    child_env = tui_app_module._rpc_env(options)

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

    child_env = tui_app_module._rpc_env(options)

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

    child_env = tui_app_module._rpc_env(options)

    assert child_env["WISP_AUTO_COMPACTION"] == ("1" if enabled else "0")
    assert child_env["WISP_CONTEXT_RESERVE_TOKENS"] == "4096"


def test_run_tui_closes_owned_controller_when_renderer_construction_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class OwnedController(ScriptedController):
        pass

    controller = OwnedController()

    async def start_transport(*_args: object, **_kwargs: object) -> object:
        return controller

    async def preflight(_options: object) -> None:
        return None

    monkeypatch.setattr(tui_app_module, "_preflight_tui_options", preflight)
    monkeypatch.setattr(tui_app_module.JsonlSubprocessRpcTransport, "start", start_transport)
    monkeypatch.setattr(tui_app_module, "RpcController", lambda _transport: controller)
    monkeypatch.setattr(
        tui_app_module,
        "create_tui_renderer",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("renderer failed")),
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="renderer failed"):
            await tui_app_module.run_tui(
                TuiOptions(config=WispConfig(provider="fake", session_dir=tmp_path))
            )

    anyio.run(scenario)
    assert controller.closed is True


def test_run_tui_uses_live_fullscreen_when_interactive(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    instances: list[object] = []

    class FakeLiveFullscreenTui(FullscreenTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0], clear_screen=False)
            self.prompts: list[str] = []
            self.closed = False
            instances.append(self)

        async def read_prompt(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return "/quit"
            await anyio.sleep(1)
            raise EOFError

        async def close(self) -> None:
            self.closed = True

    async def run() -> None:
        monkeypatch.setattr(tui_app_module, "_stdio_is_interactive", lambda: True)
        monkeypatch.setattr(tui_app_module, "LiveFullscreenTui", FakeLiveFullscreenTui)
        controller = ScriptedController()

        await tui_app_module.run_tui(
            TuiOptions(
                config=WispConfig(provider="fake", session_dir=tmp_path),
                renderer=TuiRendererKind.fullscreen,
            ),
            controller=controller,
        )

        assert controller.shutdown_count == 1
        assert len(instances) == 1
        live = instances[0]
        assert isinstance(live, FakeLiveFullscreenTui)
        assert live.prompts[0] == "wisp> "
        assert live.closed is True

    anyio.run(run)


def test_run_tui_uses_fullscreen_fallback_with_explicit_prompt_reader(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    class FailingLiveFullscreenTui:
        def __init__(self) -> None:
            raise AssertionError("live fullscreen should not be constructed")

    async def run() -> None:
        monkeypatch.setattr(tui_app_module, "_stdio_is_interactive", lambda: True)
        monkeypatch.setattr(tui_app_module, "LiveFullscreenTui", FailingLiveFullscreenTui)
        controller = ScriptedController()

        await tui_app_module.run_tui(
            TuiOptions(
                config=WispConfig(provider="fake", session_dir=tmp_path),
                renderer=TuiRendererKind.fullscreen,
            ),
            controller=controller,
            prompt_reader=await _reader_from(["/quit"]),
        )

        assert controller.shutdown_count == 1

    anyio.run(run)


def test_run_tui_uses_fullscreen_fallback_when_stdio_is_not_interactive(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    class FailingLiveFullscreenTui:
        def __init__(self) -> None:
            raise AssertionError("live fullscreen should not be constructed")

    prompts: list[str] = []

    async def fake_default_prompt_reader(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "/quit"
        await anyio.sleep(1)
        raise EOFError

    async def run() -> None:
        monkeypatch.setattr(tui_app_module, "_stdio_is_interactive", lambda: False)
        monkeypatch.setattr(tui_app_module, "LiveFullscreenTui", FailingLiveFullscreenTui)
        monkeypatch.setattr(tui_app_module, "_default_prompt_reader", fake_default_prompt_reader)
        controller = ScriptedController()

        await tui_app_module.run_tui(
            TuiOptions(
                config=WispConfig(provider="fake", session_dir=tmp_path),
                renderer=TuiRendererKind.fullscreen,
            ),
            controller=controller,
        )

        assert controller.shutdown_count == 1
        assert prompts[0] == "wisp> "

    anyio.run(run)


def test_run_tui_textual_respects_injected_prompt_reader(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # An injected reader means the caller drives input headlessly; the Textual
    # app must not be launched (it would seize the terminal and wait for UI
    # input), and the scripted reader must be consumed instead.
    def fail_create_textual_tui() -> object:
        raise AssertionError("textual app should not be constructed with an injected reader")

    async def run() -> None:
        monkeypatch.setattr(tui_app_module, "create_textual_tui", fail_create_textual_tui)
        controller = ScriptedController()

        await tui_app_module.run_tui(
            TuiOptions(
                config=WispConfig(provider="fake", session_dir=tmp_path),
                renderer=TuiRendererKind.textual,
            ),
            controller=controller,
            prompt_reader=await _reader_from(["/quit"]),
        )

        assert controller.shutdown_count == 1

    anyio.run(run)


def test_cli_no_args_shows_help_without_tui_env() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [],
        env={"WISP_MODE": "", "WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "Wisp: a terminal-first coding agent." in result.output


def test_cli_no_args_uses_env_tui_defaults(monkeypatch: object) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [],
        env={
            "WISP_MODE": "tui",
            "WISP_TUI_RENDERER": "fullscreen",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
        },
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].config.provider == "fake"
    assert captured[0].renderer is TuiRendererKind.fullscreen
    # The legacy WISP_MODE=tui path defaults to the full toolset too — otherwise
    # this door to the same TUI would launch a toolless agent.
    assert captured[0].all_tools is True


def test_cli_legacy_tui_resolves_trust_before_starting_ui(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    order: list[str] = []

    def fake_resolve(project_path: Path) -> TrustDecision:
        order.append("trust")
        return TrustDecision(project_path=project_path, trusted=False)

    async def fake_run_tui(options: TuiOptions) -> None:
        order.append("tui")
        assert options.config.provider == "fake"
        assert options.project_trusted is False

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "_resolve_cli_trust", fake_resolve)
    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)

    result = CliRunner().invoke(
        app,
        ["--mode", "tui"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert order == ["trust", "tui"]


def test_cli_tui_command_resolves_trust_before_starting_ui(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    order: list[str] = []

    def fake_resolve(project_path: Path) -> TrustDecision:
        order.append("trust")
        return TrustDecision(project_path=project_path, trusted=True)

    async def fake_run_tui(options: TuiOptions) -> None:
        order.append("tui")
        assert options.config.provider == "fake"
        assert options.project_trusted is True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "_resolve_cli_trust", fake_resolve)
    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)

    result = CliRunner().invoke(
        app,
        ["tui"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert order == ["trust", "tui"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["tui", "--no-synchronized-output"],
        ["--mode", "tui", "--no-synchronized-output"],
    ],
)
def test_cli_tui_synchronized_output_opt_out_reaches_textual_app(
    arguments: list[str],
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)

    result = CliRunner().invoke(
        app,
        arguments,
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].synchronized_output is False


@pytest.mark.parametrize("arguments", [["tui"], ["--mode", "tui"]])
def test_cli_tui_synchronized_output_defaults_on(
    arguments: list[str],
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)

    result = CliRunner().invoke(
        app,
        arguments,
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].synchronized_output is True


def test_cli_tui_command_rejects_invalid_auto_compaction_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "_resolve_cli_trust",
        lambda path: TrustDecision(project_path=path, trusted=True),
    )

    result = CliRunner().invoke(
        app,
        ["tui"],
        env={
            "WISP_AUTO_COMPACTION": "sometimes",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
        },
    )

    assert result.exit_code == 1
    assert "WISP_AUTO_COMPACTION must be one of" in result.output
    assert "Traceback" not in result.output


def test_cli_tui_mode_uses_env_renderer_default(monkeypatch: object) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui"],
        env={
            "WISP_MODE": "",
            "WISP_TUI_RENDERER": "fullscreen",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
        },
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].renderer is TuiRendererKind.fullscreen
    # Legacy `--mode tui` defaults the full toolset on, matching `wisp tui`.
    assert captured[0].all_tools is True


def test_cli_legacy_tui_mode_no_all_tools_flag_wins(monkeypatch: object) -> None:
    # An explicit --no-all-tools on the legacy path opts out of the TUI's
    # full-registry default, falling back to the opt-in tool filter.
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui", "--no-all-tools"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].all_tools is False


def test_cli_prompt_with_explicit_tui_mode_still_errors() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--mode", "tui"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "--prompt is not used with --mode tui" in result.output


def test_cli_rejects_invalid_env_mode_default() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [],
        env={"WISP_MODE": "missing", "WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "WISP_MODE must be one of: text, json, rpc, tui" in result.output


def test_cli_tui_mode_rejects_invalid_env_renderer_default() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui"],
        env={
            "WISP_TUI_RENDERER": "missing",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
        },
    )

    assert result.exit_code == 1
    assert "WISP_TUI_RENDERER must be one of: line, fullscreen" in result.output


def test_cli_tui_mode_validates_provider_before_prompting() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["--mode", "tui", "--provider", "missing"])

    assert result.exit_code == 1
    assert "Unknown provider: missing" in result.output
    assert "Wisp TUI MVP" not in result.output


def test_cli_tui_mode_validates_continue_before_prompting(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui", "--provider", "fake", "--session-dir", str(tmp_path), "--continue"],
    )

    assert result.exit_code == 1
    assert "No sessions found" in result.output
    assert str(tmp_path.name) in result.output
    assert "Wisp TUI MVP" not in result.output


def test_cli_tui_command_defaults_to_textual_renderer(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["tui", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].config.provider == "fake"
    assert captured[0].config.session_dir == tmp_path
    assert captured[0].renderer is TuiRendererKind.textual
    # An explicit --session-dir is carried as a user override so the RPC subprocess
    # honors it (the launcher no longer launders resolved config into flags).
    assert captured[0].user_session_dir == tmp_path
    # `wisp tui` gives the agent the full toolset by default — otherwise it's a
    # toolless chatbot that can't read files or run commands.
    assert captured[0].all_tools is True


def test_cli_tui_command_loads_trusted_root_settings_from_subdirectory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from wisp.trust import record_trust

    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (project / ".wisp").mkdir()
    (project / ".wisp" / "settings.json").write_text(
        '{"model": "project-model"}',
        encoding="utf-8",
    )
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    record_trust(project, True, trust_path=trust_file)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)

    result = CliRunner().invoke(
        app,
        ["tui"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert captured[0].config.model == "project-model"


def test_cli_tui_command_forwards_explicit_auth_file_as_user_override(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    auth_file = tmp_path / "auth.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["tui", "--auth-file", str(auth_file)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert captured[0].user_auth_file == auth_file


def test_cli_legacy_mode_tui_forwards_explicit_session_dir_override(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # Regression: the legacy `--mode tui` callback path must also forward an explicit
    # --session-dir as a user override; otherwise it is dropped now that the launcher
    # no longer serializes the resolved config into subprocess flags.
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].user_session_dir == tmp_path


def test_cli_legacy_mode_tui_forwards_explicit_provider_and_model(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # Regression: `wisp --mode tui --provider --model` must reach the RPC subprocess.
    # Otherwise the parent validates/displays them but the child re-resolves and could
    # run a DIFFERENT provider/model than the user asked for.
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "tui", "--provider", "fake", "--model", "model-x"],
        env={"WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].user_provider == "fake"
    assert captured[0].user_model == "model-x"


def test_cli_tui_command_no_all_tools_flag_disables_the_full_registry(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["tui", "--no-all-tools", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].all_tools is False


def test_cli_tui_command_line_flag_uses_line_renderer(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["tui", "--line", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].renderer is TuiRendererKind.line


def test_cli_tui_command_rejects_resume_and_continue() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["tui", "--resume", "session-123", "--continue"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "use either --resume or --continue, not both" in result.output
