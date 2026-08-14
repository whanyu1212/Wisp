# ruff: noqa: F403,F405

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from pytest import MonkeyPatch
from rich.cells import cell_len
from textual import events
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Header, Label, OptionList, Static

import wisp.cli as cli_module
from tests.tui_support import *
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    MessageStarted,
    ProviderRetrying,
    RpcCommandStarted,
    RpcMessageSnapshot,
    RpcMessagesReported,
    SkillInvoked,
    TrustResolved,
    TurnStarted,
    wisp_event_from_json,
)
from wisp.skills.models import SkillInvocationEvidence
from wisp.trust_flow import TrustDecision
from wisp.tui.commands import parse_tui_slash_command
from wisp.tui.compact_echo import MAX_PENDING_ECHOES as _MAX_PENDING_ECHOES
from wisp.tui.history import (
    HistoricalToolCard,
    HistoricalTranscriptMessage,
    history_entries_from_rpc_messages,
)
from wisp.tui.overlay import TranscriptViewportState
from wisp.tui.state import TuiCancelRequested, TuiQuitRequested
from wisp.tui.textual_app import (
    _EMPTY_TRANSCRIPT_TAGLINE,
    TextualTui,
    TextualTuiRenderer,
    create_textual_tui,
)
from wisp.tui.transcript_window import (
    TUI_TRANSCRIPT_WINDOW_SHIFT,
    TUI_TRANSCRIPT_WINDOW_SIZE,
)
from wisp.tui.widgets import (
    _ROLE_LABELS,
    JumpToLatest,
    LineMessage,
    SlashSuggest,
    StatusBar,
    StreamMessage,
    ToolCard,
    Transcript,
    TranscriptEmptyState,
    WorkingIndicator,
)
from wisp.tui.widgets import (
    PromptEditor as Input,
)

pytestmark = pytest.mark.tui


def _transcript_texts(app: TextualTui) -> list[str]:
    """Plain text of every mounted transcript message (line + streamed)."""

    transcript = app.query_one("#transcript", Transcript)
    texts: list[str] = []
    for child in transcript.children:
        if isinstance(child, LineMessage | ToolCard):
            texts.append(child.render().plain)  # Textual Content
        elif isinstance(child, StreamMessage):
            texts.append(child.source)
    return texts


def _working_activity(app: TextualTui) -> str:
    """Plain transcript heartbeat text, or empty when activity is hidden."""

    indicator = app._transcript_controller.working_indicator
    return indicator.render().plain if isinstance(indicator, WorkingIndicator) else ""


def _provider_retry(
    *,
    turn: int = 1,
    attempt: int = 1,
    max_attempts: int = 3,
    provider: str = "openai",
    delay_seconds: float = 0.5,
    reason: str = "rate_limit",
    status_code: int | None = None,
) -> ProviderRetrying:
    return ProviderRetrying(
        turn=turn,
        provider=provider,
        attempt=attempt,
        max_attempts=max_attempts,
        delay_seconds=delay_seconds,
        reason=reason,
        status_code=status_code,
    )


def _transcript_styles(app: TextualTui) -> str:
    """Style strings applied to every LineMessage/ToolCard span (e.g. 'bold #5cc9a7')."""

    transcript = app.query_one("#transcript", Transcript)
    styles: list[str] = []
    for child in transcript.children:
        if isinstance(child, LineMessage | ToolCard):
            styles.extend(str(span.style) for span in child.render().spans)
    return "\n".join(styles)


def _transcript_role_class(child: object) -> str | None:
    """The message--<role> class on a transcript child, or None if absent."""

    if not hasattr(child, "classes"):
        return None
    return next((c for c in child.classes if c.startswith("message--")), None)


def _transcript_cards(app: TextualTui) -> list[tuple[str | None, object]]:
    """(role class, border_title) for every mounted transcript card."""

    transcript = app.query_one("#transcript", Transcript)
    return [
        (role, child.border_title)
        for child in transcript.children
        if (role := _transcript_role_class(child)) is not None
    ]


def _cards_for_events(events: list[object]) -> list[tuple[str | None, object]]:
    # Drive events through a live app and return each card's role class + title.
    async def scenario() -> list[tuple[str | None, object]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            for event in events:
                renderer.event(event)
            await pilot.pause()
            return _transcript_cards(app_instance)

    return anyio.run(scenario)


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


def test_textual_tui_renderer_can_be_constructed() -> None:
    app_instance, renderer = create_textual_tui()

    assert isinstance(app_instance, TextualTui)
    assert isinstance(renderer, TextualTuiRenderer)
    renderer.view_updated(TuiViewSnapshot(status="idle", input_hint="wisp> "))
    renderer.notice("hello")


def test_textual_tui_preserves_brackets_in_streamed_output() -> None:
    async def scenario() -> str:
        app_instance = TextualTui()
        async with app_instance.run_test():
            app_instance.append_stream("code has [brackets] and [/close] tags")
            app_instance.flush_stream()
            await app_instance.wait_for_stream_idle()
            # Streamed assistant text renders as Markdown; bracketed text must
            # survive intact (Markdown source is not Rich-markup-interpreted).
            return "\n".join(_transcript_texts(app_instance))

    rendered = anyio.run(scenario)
    assert "[brackets]" in rendered
    assert "[/close]" in rendered


def test_textual_tui_renderer_renders_hydrated_history_in_order_and_markdown() -> None:
    restored_markdown = "# Restored answer\n\n- first\n- second\n\n```python\nprint('ok')\n```"

    async def scenario() -> tuple[list[str], int, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.render_history(
                (
                    HistoricalTranscriptMessage(role="user", content="old [red]prompt[/red]"),
                    HistoricalTranscriptMessage(role="assistant", content=restored_markdown),
                )
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            assistants = [
                child for child in transcript.children if isinstance(child, StreamMessage)
            ]
            child_count = sum(len(assistant.children) for assistant in assistants)
            return _transcript_texts(app_instance), child_count, len(assistants)

    rendered, child_count, assistant_count = anyio.run(scenario)
    assert rendered == ["old [red]prompt[/red]", restored_markdown]
    assert assistant_count == 1
    assert child_count == 0


def test_textual_tui_renders_resumed_markdown_after_rpc_json_round_trip() -> None:
    restored_markdown = "### Transported answer\n\n1. first\n2. second"
    report = RpcMessagesReported(
        command_id="messages-1",
        session_id="session-1",
        messages=(
            RpcMessageSnapshot(
                entry_id="entry-1",
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                role="assistant",
                content=restored_markdown,
                content_original_bytes=len(restored_markdown.encode()),
            ),
        ),
    )
    restored = wisp_event_from_json(report.model_dump_json())
    assert isinstance(restored, RpcMessagesReported)

    async def scenario() -> tuple[str, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.replace_history_entries(
                history_entries_from_rpc_messages(restored.messages),
                session_label="Restored session",
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            assistant = next(
                child for child in transcript.children if isinstance(child, StreamMessage)
            )
            return assistant.source, len(assistant.children)

    source, child_count = anyio.run(scenario)
    assert source == restored_markdown
    assert child_count == 0


def test_textual_completed_message_without_deltas_renders_markdown() -> None:
    async def scenario() -> tuple[str, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(completed_message(content="## Settled answer\n\n`inline`"))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            (assistant,) = transcript.children
            assert isinstance(assistant, StreamMessage)
            return assistant.source, len(assistant.children)

    source, child_count = anyio.run(scenario)
    assert source == "## Settled answer\n\n`inline`"
    assert child_count == 0


def test_textual_tui_renderer_renders_historical_tool_cards() -> None:
    async def scenario() -> tuple[str, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.render_history_entries(
                (
                    HistoricalTranscriptMessage(role="user", content="old prompt"),
                    HistoricalToolCard(
                        card_id="history:tool-1",
                        name="bash",
                        arguments={"command": "false"},
                        output="[red]boom[/red]",
                        is_error=False,
                        exit_code=1,
                    ),
                )
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            cards = [child for child in transcript.children if isinstance(child, ToolCard)]
            return "\n".join(_transcript_texts(app_instance)), len(cards)

    rendered, card_count = anyio.run(scenario)
    assert card_count == 1
    assert "old prompt" in rendered
    assert "• Failed to run  false" in rendered
    assert "  └ exit 1" in rendered
    assert "[red]boom[/red]" in rendered


def test_textual_tui_renderer_renders_historical_edit_with_structured_diff() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.render_history_entries(
                (
                    HistoricalToolCard(
                        card_id="history:edit-1",
                        name="edit",
                        arguments={
                            "path": "src/restored.py",
                            "edits": [{"oldText": "old\n", "newText": "new\n"}],
                        },
                        output="Applied 1 edit(s) to src/restored.py",
                        is_error=False,
                    ),
                )
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    rendered = anyio.run(scenario)
    assert "M src/restored.py  +1 -1" in rendered
    assert "- │ old" in rendered
    assert "+ │ new" in rendered
    assert "Applied 1 edit" not in rendered


def test_textual_tui_renderer_enriches_result_at_a_history_page_boundary() -> None:
    async def scenario() -> tuple[str, int, str, str]:
        app_instance, renderer = create_textual_tui()
        result = HistoricalToolCard(
            card_id="history:result",
            name="bash",
            arguments={},
            output="done",
            is_error=False,
            tool_call_id="call-1",
            call_missing=True,
        )
        paged_call = HistoricalToolCard(
            card_id="history:missing:call-1",
            name="bash",
            arguments={"command": "printf done"},
            output="No persisted tool result.",
            is_error=True,
            tool_call_id="call-1",
            status="cancelled",
            missing_result=True,
        )
        async with app_instance.run_test() as pilot:
            renderer.replace_history_entries((result,), session_label="Paged session")
            await pilot.pause()
            initial_arguments = _first_tool_card(app_instance)._call_arguments.plain
            renderer.prepend_history_entries((paged_call,))
            await pilot.pause()
            await pilot.pause()
            cards = [
                child
                for child in app_instance.query_one("#transcript", Transcript).children
                if isinstance(child, ToolCard)
            ]
            assert len(cards) == 1
            return (
                initial_arguments,
                len(cards),
                cards[0]._tool_name,
                cards[0]._call_arguments.plain,
            )

    initial_arguments, card_count, tool_name, call_arguments = anyio.run(scenario)
    assert initial_arguments == ""  # missing calls must not fabricate default arguments
    assert card_count == 1
    assert tool_name == "bash"
    assert call_arguments == "printf done"


def test_textual_tui_renderer_remounts_boundary_result_without_visible_call() -> None:
    async def scenario() -> tuple[int, str, int, str, int, str, str, str, str]:
        app_instance, renderer = create_textual_tui()
        result = HistoricalToolCard(
            card_id="history:result",
            name="bash",
            arguments={},
            output="done",
            is_error=False,
            tool_call_id="call-1",
            call_missing=True,
        )
        paged_call = HistoricalToolCard(
            card_id="history:missing:call-1",
            name="bash",
            arguments={"command": "printf done"},
            output="No persisted tool result.",
            is_error=True,
            tool_call_id="call-1",
            status="cancelled",
            missing_result=True,
        )
        newer = tuple(
            HistoricalTranscriptMessage(role="assistant", content=f"newer {index}")
            for index in range(TUI_TRANSCRIPT_WINDOW_SIZE - 1)
        )
        older = tuple(
            HistoricalTranscriptMessage(role="user", content=f"older {index}")
            for index in range(TUI_TRANSCRIPT_WINDOW_SIZE)
        )
        async with app_instance.run_test() as pilot:
            renderer.replace_history_entries((result, *newer), session_label="Paged session")
            await pilot.pause()
            renderer.prepend_history_entries((paged_call,))
            await pilot.pause()
            renderer.prepend_history_entries(older)
            for _ in range(4):
                app_instance.action_scroll_transcript_home()
            await pilot.pause()

            app_instance.action_scroll_transcript_end()
            await pilot.pause()
            result_only_cards = _all_tool_cards(app_instance)

            app_instance.action_scroll_transcript_home()
            await pilot.pause()
            paired_cards = _all_tool_cards(app_instance)

            for _ in range(4):
                app_instance.action_scroll_transcript_home()
            await pilot.pause()
            renderer.running()
            await pilot.pause()
            activity_before_remount = _working_activity(app_instance)
            app_instance.action_scroll_transcript_end()
            await pilot.pause()
            activity_after_remount = _working_activity(app_instance)
            remounted_result_cards = _all_tool_cards(app_instance)
            remounted_detail = remounted_result_cards[0]._detail
            return (
                len(result_only_cards),
                (
                    result_only_cards[0]._detail.plain
                    if isinstance(result_only_cards[0]._detail, Content)
                    else result_only_cards[0]._detail
                ),
                len(paired_cards),
                paired_cards[0]._call_arguments.plain,
                len(remounted_result_cards),
                remounted_detail.plain
                if isinstance(remounted_detail, Content)
                else remounted_detail,
                remounted_result_cards[0]._call_arguments.plain,
                activity_before_remount,
                activity_after_remount,
            )

    (
        result_count,
        detail,
        paired_count,
        call_arguments,
        remounted_count,
        remounted_detail,
        remounted_call_arguments,
        activity_before_remount,
        activity_after_remount,
    ) = anyio.run(scenario)
    assert result_count == 1
    assert detail == "done"
    assert paired_count == 1
    assert call_arguments == "printf done"
    assert remounted_count == 1
    assert remounted_detail == "done"
    assert remounted_call_arguments == "printf done"
    assert "Working" in activity_before_remount
    assert "Working" in activity_after_remount


def test_textual_history_result_without_call_id_reports_unavailable_arguments() -> None:
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        entry = HistoricalToolCard(
            card_id="history:orphan",
            name="grep",
            arguments={},
            output="No matches found",
            is_error=False,
            summary="grep: no matches",
            call_missing=True,
        )
        async with app_instance.run_test() as pilot:
            renderer.replace_history_entries((entry,), session_label="Legacy session")
            await pilot.pause()
            card = _first_tool_card(app_instance)
            return card._call_arguments.plain, card.render().plain

    arguments, rendered = anyio.run(scenario)
    assert arguments == ""  # no fabricated ``/ / in .`` argument snapshot
    assert rendered.startswith("• Searched  (arguments unavailable)")
    assert "// in ." not in rendered


def test_textual_tui_renderer_remounted_boundary_pair_is_not_pending() -> None:
    async def scenario() -> tuple[int, tuple[str, str, str], tuple[str, str, str]]:
        app_instance, renderer = create_textual_tui()
        result = HistoricalToolCard(
            card_id="history:result",
            name="bash",
            arguments={},
            output="done",
            is_error=False,
            tool_call_id="call-1",
            call_missing=True,
        )
        paged_call = HistoricalToolCard(
            card_id="history:missing:call-1",
            name="bash",
            arguments={"command": "printf done"},
            output="No persisted tool result.",
            is_error=True,
            tool_call_id="call-1",
            status="cancelled",
            missing_result=True,
        )
        older = tuple(
            HistoricalTranscriptMessage(role="user", content=f"older {index}")
            for index in range(600)
        )
        async with app_instance.run_test() as pilot:
            renderer.replace_history_entries((result,), session_label="Paged session")
            await pilot.pause()
            renderer.prepend_history_entries((paged_call,))
            await pilot.pause()
            renderer.prepend_history_entries(older)
            for _ in range(8):
                app_instance.action_scroll_transcript_home()
            await pilot.pause()

            app_instance.action_scroll_transcript_end()
            await pilot.pause()
            card = _first_tool_card(app_instance)
            pending_count = app_instance._transcript_controller.pending_tool_count
            detail = card._detail.plain if isinstance(card._detail, Content) else card._detail
            before_cancel = (card._status, str(card.border_title), detail)
            renderer.cancelled()
            await pilot.pause()
            detail = card._detail.plain if isinstance(card._detail, Content) else card._detail
            after_cancel = (card._status, str(card.border_title), detail)
            return pending_count, before_cancel, after_cancel

    pending_count, before_cancel, after_cancel = anyio.run(scenario)
    assert pending_count == 0
    assert before_cancel == ("done", "", "done")
    assert after_cancel == before_cancel


def test_textual_tui_renderer_matches_reused_history_tool_call_ids_by_occurrence() -> None:
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        newer_second_result = HistoricalToolCard(
            card_id="history:second-result",
            name="bash",
            arguments={},
            output="second output",
            is_error=False,
            tool_call_id="reused",
            call_missing=True,
        )
        older_first_result = HistoricalToolCard(
            card_id="history:first-result",
            name="bash",
            arguments={"command": "first"},
            output="first output",
            is_error=False,
            tool_call_id="reused",
        )
        boundary_second_call = HistoricalToolCard(
            card_id="history:missing:reused",
            name="bash",
            arguments={"command": "second"},
            output="No persisted tool result.",
            is_error=True,
            tool_call_id="reused",
            status="cancelled",
            missing_result=True,
        )
        async with app_instance.run_test() as pilot:
            renderer.replace_history_entries((newer_second_result,), session_label="Paged session")
            await pilot.pause()
            renderer.prepend_history_entries((older_first_result, boundary_second_call))
            await pilot.pause()
            await pilot.pause()
            return [
                card._call_arguments.plain
                for card in app_instance.query_one("#transcript", Transcript).children
                if isinstance(card, ToolCard)
            ]

    assert anyio.run(scenario) == ["first", "second"]


def test_textual_tui_renderer_normalizes_historical_bash_full_output() -> None:
    async def scenario() -> tuple[str, str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.render_history_entries(
                (
                    HistoricalToolCard(
                        card_id="history:tool-1",
                        name="bash",
                        arguments={"command": "echo ok"},
                        output="Command exited with code 0: ok",
                        is_error=False,
                        exit_code=0,
                        output_has_exit_status=True,
                    ),
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            detail = card._detail
            assert isinstance(detail, str)
            return detail, card._full_output, card._can_expand()

    detail, full_output, can_expand = anyio.run(scenario)

    assert detail == "ok"
    assert full_output == "ok"
    assert can_expand is False


def test_textual_tui_renderer_preserves_matching_legacy_bash_output() -> None:
    legacy_output = "Command exited with code 0: important"

    async def scenario() -> tuple[str, str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.render_history_entries(
                (
                    HistoricalToolCard(
                        card_id="history:tool-1",
                        name="bash",
                        arguments={"command": "legacy-command"},
                        output=legacy_output,
                        is_error=False,
                        exit_code=0,
                        output_has_exit_status=False,
                    ),
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            detail = card._detail
            assert isinstance(detail, str)
            return detail, card._full_output, card._can_expand()

    detail, full_output, can_expand = anyio.run(scenario)

    assert detail == legacy_output
    assert full_output == legacy_output
    assert can_expand is False


def test_textual_tui_renderer_preserves_historical_denied_tool_cards() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.render_history_entries(
                (
                    HistoricalToolCard(
                        card_id="history:tool-1",
                        name="write",
                        arguments={"path": "x.py"},
                        output="too risky",
                        is_error=True,
                        status="denied",
                    ),
                )
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    rendered = anyio.run(scenario)
    assert "• Denied writing  x.py" in rendered
    assert "too risky" in rendered
    assert "Failed to write" not in rendered


def _render_events_to_transcript(events: list[object]) -> str:
    # Drive TextualTuiRenderer.event() through a live app and return the plain
    # text of every mounted transcript message.
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            for event in events:
                renderer.event(event)
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    return anyio.run(scenario)


def test_textual_skill_invocation_keeps_active_working_indicator() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        original = "/skill:review focus on safety"
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.prompt_submitted(original)
            renderer.skill_invoked(
                SkillInvoked(
                    session_id="session-1",
                    message_entry_id="message-1",
                    invocation=SkillInvocationEvidence(
                        name="review",
                        original_content=original,
                        request="focus on safety",
                        content_sha256="a" * 64,
                        instructions_truncated=False,
                    ),
                    provider_content="expanded instructions",
                )
            )
            await pilot.pause()
            return _working_activity(app_instance)

    assert "Working" in anyio.run(scenario)


def test_textual_renderer_matches_skill_invocation_to_retried_prompt() -> None:
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        assert isinstance(renderer, TextualTuiRenderer)
        original = "/skill:review focus on safety"
        async with app_instance.run_test() as pilot:
            renderer.prompt_submitted(original)
            renderer.discard_live_prompt(original)
            renderer.prompt_submitted(original)
            renderer.skill_invoked(
                SkillInvoked(
                    session_id="session-1",
                    message_entry_id="message-1",
                    invocation=SkillInvocationEvidence(
                        name="review",
                        original_content=original,
                        request="focus on safety",
                        content_sha256="a" * 64,
                        instructions_truncated=False,
                    ),
                    provider_content="expanded instructions",
                )
            )
            await pilot.pause()
            return _transcript_texts(app_instance)

    assert anyio.run(scenario) == [
        "/skill:review focus on safety",
        "skill /skill:review focus on safety",
    ]


def test_textual_renderer_dispatches_events_by_type() -> None:
    # Each event type renders distinctly. A tool call is ONE evolving card keyed by
    # call_id: the request mounts it, the result mutates it in place (request +
    # result do not stack two lines). A stand-alone denial (safety-gated tool that
    # had a prior request) flips its card to denied with the reason.
    rendered = _render_events_to_transcript(
        [
            completed_message(content="hello there"),
            ToolCallRequested(call_id="c1", name="bash", arguments={"cmd": "ls"}),
            ToolResultReady(call_id="c1", name="bash", output="file-a\nfile-b", is_error=False),
            ToolCallRequested(call_id="c3", name="write", arguments={"path": "x"}),
            ToolApprovalResolved(call_id="c3", name="write", approved=False, reason="too risky"),
            ErrorEvent(message="boom"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=False, error="nope"),
        ]
    )

    assert "hello there" in rendered
    # One card for c1: done glyph + name + the bounded multiline output preview.
    assert "• Ran" in rendered
    assert "  └ file-a\n    file-b" in rendered
    assert "file-a" in rendered
    assert "file-b" in rendered
    # The denied card carries an explicit action and the reason without relying
    # on a status glyph or color.
    assert "• Denied writing  x" in rendered
    assert "too risky" in rendered
    assert "error: boom" in rendered
    assert "command failed: nope" in rendered


def test_textual_renderer_suppresses_rpc_framing_events() -> None:
    # Framing/plumbing events are session/RPC audit, not conversation — they must
    # NOT leak their repr into the transcript (regression: a catch-all else once
    # dumped str(event) for every unhandled type). Only the assistant line shows.
    rendered = _render_events_to_transcript(
        [
            RpcCommandStarted(command_id="cmd-1", command_type="prompt"),
            AgentStarted(session_id="s1"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=True),
            completed_message(content="the answer"),
        ]
    )

    assert rendered == "the answer"  # framing events produced no lines
    assert "RpcCommand" not in rendered
    assert "AgentStarted" not in rendered
    assert "command_id" not in rendered


def test_textual_renderer_collapses_call_and_result_into_one_card() -> None:
    # Request then result for one call_id mutate a single card in place rather than
    # mounting a second card. An errored result flips the glyph to ✗ and shows its
    # bounded output preview.
    async def scenario() -> tuple[list[str], int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="grep", arguments={}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="grep", output="match", is_error=True)
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            cards = [c for c in transcript.children if isinstance(c, ToolCard)]
            return [c.render().plain for c in cards], len(cards)

    texts, count = anyio.run(scenario)
    assert count == 1  # one card carried the whole lifecycle
    assert texts[0].startswith("• Failed to search")
    assert "match" in texts[0]


def test_textual_tool_card_shows_true_elapsed_from_event_timestamps() -> None:
    # A resolved card freezes at the wall-clock duration between the request and
    # result event timestamps (not the live tick count), so the resting number is
    # honest. Construct the events a known 2.5s apart and assert the formatted
    # duration lands on the card.
    from datetime import timedelta

    from wisp.events import utc_now

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            start = utc_now()
            renderer.event(
                ToolCallRequested(call_id="c1", name="grep", arguments={}, timestamp=start)
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="grep",
                    output="match",
                    is_error=False,
                    timestamp=start + timedelta(seconds=2.5),
                )
            )
            await pilot.pause()
            card = next(
                c
                for c in app_instance.query_one("#transcript", Transcript).children
                if isinstance(c, ToolCard)
            )
            return card.render().plain

    text = anyio.run(scenario)
    assert text.splitlines()[0].endswith("· 2.5s"), text


def test_textual_tool_output_preview_reports_hidden_lines_and_bytes() -> None:
    from wisp.tui.widgets import _preview_tool_output

    preview = _preview_tool_output("one\ntwo\nthree", max_lines=2, max_bytes=100)

    assert preview == "one\ntwo\n... 1 more line, 6 bytes hidden"


def test_textual_tool_output_preview_bounds_long_unicode_line() -> None:
    from wisp.tui.widgets import _preview_tool_output

    preview = _preview_tool_output("é" * 10, max_lines=8, max_bytes=5)

    assert preview == "éé\n... 16 bytes hidden"


def test_textual_tool_card_bounds_large_multiline_output() -> None:
    output = "\n".join(f"line-{index}" for index in range(12))
    rendered = _render_events_to_transcript(
        [
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ToolResultReady(call_id="c1", name="bash", output=output, is_error=False),
        ]
    )

    assert "line-0" not in rendered
    assert "line-3" not in rendered
    assert "line-4" in rendered
    assert "line-11" in rendered
    assert "... 4 earlier lines" in rendered


def test_textual_tool_card_error_shows_tail_and_exit_code() -> None:
    # Issue #74 PR A: a failed tool now renders the exit status and the TAIL of
    # its output (where the error is), not the head. Before this, the card showed
    # the first lines and dropped the actual failure at the end.
    output = "\n".join(f"line-{index}" for index in range(40))
    rendered = _render_events_to_transcript(
        [
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ToolResultReady(
                call_id="c1",
                name="bash",
                output=output,
                is_error=True,
                exit_code=2,
            ),
        ]
    )

    assert "• Failed to run" in rendered
    assert "exit 2" in rendered
    assert "line-39" in rendered  # the tail (the error) is shown
    assert "line-0" not in rendered  # the head is dropped


def test_textual_tool_card_nonzero_exit_renders_as_failure() -> None:
    # The realistic bash failure: the tool RAN (is_error=False, a normal
    # model-visible result) but the command exited nonzero. The card must still
    # show as a failure with the exit status and tail — driven by exit_code,
    # not by is_error (which stays honest on the wire).
    output = "\n".join(f"line-{index}" for index in range(40))
    rendered = _render_events_to_transcript(
        [
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ToolResultReady(
                call_id="c1",
                name="bash",
                output=output,
                is_error=False,
                exit_code=1,
            ),
        ]
    )

    assert "• Failed to run" in rendered  # failure words despite is_error=False
    assert "exit 1" in rendered
    assert "line-39" in rendered


def test_textual_tool_card_timed_out_process_state_renders_as_failure() -> None:
    rendered = _render_events_to_transcript(
        [
            ToolCallRequested(
                call_id="c1",
                name="bash",
                arguments={"operation": "poll", "process_id": "proc-1"},
            ),
            ToolResultReady(
                call_id="c1",
                name="bash",
                output="Process proc-1 timed out",
                is_error=False,
                process_state="timed_out",
            ),
        ]
    )

    assert "• Failed to run  poll proc-1" in rendered
    assert "Process proc-1 timed out" in rendered


def test_textual_tool_card_edit_renders_colored_diff() -> None:
    # Issue #74 PR B1: a successful `edit` renders a colored unified diff built
    # from the request's oldText/newText hunks — which reach the renderer on
    # ToolCallRequested and are retained until the result arrives. Drives the real
    # request → result pipeline (not the pure function) and asserts both the diff
    # text and the resolved theme colors reach the transcript.
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/foo.py",
                        "edits": [{"oldText": "return 1", "newText": "return 2"}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="edit",
                    output="Applied 1 edit(s) to src/foo.py",
                    is_error=False,
                )
            )
            await pilot.pause()
            text = "\n".join(_transcript_texts(app_instance))
            styles = _transcript_styles(app_instance)
            return text, styles

    text, styles = anyio.run(scenario)
    assert "• Edited  src/foo.py · 1 edit" in text
    assert "M src/foo.py  +1 -1" in text
    assert "- │ return 1" in text  # deletion gutter + literal source
    assert "+ │ return 2" in text  # addition gutter + literal source
    # Diff spans carry semantic theme variables, not baked hex. Textual resolves
    # them per active theme at paint time, so a theme switch recolors the diff.
    # Asserted over the real RPC round-trip: a style that only exists in-process
    # would render as an unstyled diff in the actual TUI.
    assert "$diff-add-fg on $diff-add-bg" in styles  # addition row band
    assert "$diff-del-fg on $diff-del-bg" in styles  # deletion row band


@pytest.mark.parametrize("size", [(28, 20), (80, 24), (120, 40)])
def test_textual_tool_card_edit_diff_rows_stay_unambiguous_at_supported_widths(
    size: tuple[int, int],
) -> None:
    # Structured rows crop source text to the available width instead of soft
    # wrapping, so every visible row retains its +/- gutter at compact widths.
    long_old = "def f(): return " + "x" * 120
    long_new = "def f(): return " + "y" * 120

    async def scenario() -> tuple[str, tuple[int, ...], int, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=size) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/foo.py",
                        "edits": [{"oldText": long_old, "newText": long_new}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="edit",
                    output="Applied 1 edit(s) to src/foo.py",
                    is_error=False,
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            rows = tuple(cell_len(line) for line in card.render().plain.splitlines() if "│" in line)
            return card.render().plain, rows, card.content_size.width, card.region.width

    text, row_widths, content_width, card_width = anyio.run(scenario)
    assert "• Edited  src/foo.py" in text  # rendered without error at every supported width
    assert "- │" in text  # deletion row retained its gutter
    assert "+ │" in text  # addition row retained its gutter
    assert len(row_widths) == 2  # no soft-wrapped source continuation rows
    assert row_widths == (content_width, content_width)
    assert card_width <= size[0]  # no horizontal overflow past the viewport


@pytest.mark.parametrize("size", [(28, 20), (84, 24), (168, 40)])
def test_textual_transcript_hides_scrollbar_without_reflowing_tool_diff(
    size: tuple[int, int],
) -> None:
    """Vertical overflow stays scrollable without changing the visible width."""

    async def scenario() -> tuple[
        int,
        int,
        bool,
        bool,
        tuple[int, ...],
        int,
        str,
        tuple[int, int],
    ]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=size) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/wisp/coding/session.py",
                        "edits": [
                            {
                                "oldText": "expected_active_leaf_id=snapshot.active_leaf_id,",
                                "newText": "expected_active_leaf_id=replay.active_leaf_id,",
                            }
                        ],
                    },
                )
            )
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            await pilot.pause()
            card = _first_tool_card(app_instance)
            width_before = transcript.scrollable_content_region.width
            scrollbar_before = transcript.scrollbars_enabled[0]

            # Cross the vertical-overflow boundary in one event-loop turn. The
            # logical scrollbar should activate for scrolling, but hidden zero-width
            # chrome must not narrow or repaint the manually formatted ToolCard.
            for index in range(size[1] + 8):
                renderer.notice(f"filler row {index}")
            await pilot.pause(0)

            row_widths = tuple(cell_len(line) for line in card.render().plain.splitlines())
            return (
                width_before,
                transcript.scrollable_content_region.width,
                scrollbar_before,
                transcript.scrollbars_enabled[0],
                row_widths,
                transcript.max_scroll_x,
                transcript.styles.scrollbar_visibility,
                transcript.scrollbars_space,
            )

    (
        width_before,
        width_after,
        scrollbar_before,
        scrollbar_after,
        row_widths,
        max_x,
        visibility,
        scrollbar_space,
    ) = anyio.run(scenario)
    assert not scrollbar_before
    assert scrollbar_after  # logical overflow state remains available to scrolling
    assert visibility == "hidden"
    assert scrollbar_space == (0, 0)
    assert width_after == width_before
    assert all(width <= width_after for width in row_widths)
    assert max_x == 0


@pytest.mark.parametrize("resized_width", [28, 84, 120])
def test_textual_transcript_content_stays_inside_viewport_after_resize(
    resized_width: int,
) -> None:
    """Every transcript row family uses the full width without horizontal overflow."""

    async def scenario() -> tuple[bool, int, bool, bool, tuple[int, int], bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(120, 24)) as pilot:
            renderer.prompt_submitted("user " + "u" * 180)
            renderer.notice("notice " + "n" * 180)
            renderer.command_error("error " + "e" * 180)
            renderer.event(
                ToolCallRequested(
                    call_id="pending",
                    name="bash",
                    arguments={"command": "x" * 180},
                )
            )
            renderer.event(
                ToolCallRequested(
                    call_id="output",
                    name="bash",
                    arguments={"command": "run"},
                )
            )
            renderer.event(
                ToolResultReady(
                    call_id="output",
                    name="bash",
                    output="\n".join(f"output {index} " + "o" * 80 for index in range(20)),
                    is_error=False,
                )
            )
            renderer.event(
                ToolCallRequested(
                    call_id="diff",
                    name="edit",
                    arguments={
                        "path": "src/long.py",
                        "edits": [
                            {
                                "oldText": "before " + "x" * 180,
                                "newText": "after " + "y" * 180,
                            }
                        ],
                    },
                )
            )
            renderer.event(
                ToolResultReady(call_id="diff", name="edit", output="Applied", is_error=False)
            )
            renderer.token_delta("## Markdown\n\n" + "wrapped prose " * 80)
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            await pilot.resize_terminal(resized_width, 24)
            await pilot.pause(0)

            transcript = app_instance.query_one("#transcript", Transcript)
            content_right = transcript.scrollable_content_region.right
            child_regions_fit = all(
                not child.display or child.region.right <= content_right
                for child in transcript.children
            )
            card_rows_fit = all(
                cell_len(line) <= card.content_size.width
                for card in transcript.query(ToolCard)
                for line in card.render().plain.splitlines()
            )
            stream_fits = all(
                stream.content_size.width <= transcript.scrollable_content_region.width
                for stream in transcript.query(StreamMessage)
            )
            return (
                transcript.scrollbars_enabled[0],
                transcript.max_scroll_x,
                child_regions_fit and card_rows_fit,
                stream_fits,
                transcript.scrollbars_space,
                transcript.scrollable_content_region.width == transcript.content_region.width,
            )

    (
        scrollbar_enabled,
        max_x,
        non_markdown_fits,
        markdown_fits,
        scrollbar_space,
        full_content_width,
    ) = anyio.run(scenario)
    assert scrollbar_enabled
    assert max_x == 0
    assert non_markdown_fits
    assert markdown_fits
    assert scrollbar_space == (0, 0)
    assert full_content_width


def test_textual_tool_card_narrow_diff_keeps_tail_changed_tokens_visible() -> None:
    """Width cropping must not hide the only changed evidence at a line's tail."""

    prefix = "unchanged-" * 8

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/tail.py",
                        "edits": [{"oldText": f"{prefix}OLD", "newText": f"{prefix}NEW"}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            return _first_tool_card(app_instance).render().plain

    text = anyio.run(scenario)
    assert "OLD" in text
    assert "NEW" in text
    assert "…" in text


def test_textual_tool_card_narrow_multiline_diff_keeps_tail_tokens_visible() -> None:
    """Structured cards retain tail-only changes beyond the legacy eight-row preview."""

    prefix = "unchanged-" * 8
    old = "".join(f"{prefix}OLD-{index}\n" for index in range(8))
    new = "".join(f"{prefix}NEW-{index}\n" for index in range(8))

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/multiline-tail.py",
                        "edits": [{"oldText": old, "newText": new}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            return _first_tool_card(app_instance).render().plain

    text = anyio.run(scenario)
    assert "OLD-0" in text
    assert "NEW-0" in text


def test_textual_tool_card_narrow_unequal_diff_keeps_tail_tokens_visible() -> None:
    """Changed rows without intra-line ranges use a suffix review window."""

    prefix = "unchanged-" * 8

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/unequal-tail.py",
                        "edits": [
                            {
                                "oldText": f"{prefix}OLD\n",
                                "newText": f"{prefix}NEW\nextra\n",
                            }
                        ],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            return _first_tool_card(app_instance).render().plain

    text = anyio.run(scenario)
    assert "OLD" in text
    assert "NEW" in text
    assert "…" in text


def test_textual_tool_card_narrow_unequal_diff_keeps_prefix_tokens_visible() -> None:
    """Unequal replacements crop around a linear prefix/middle focus anchor."""

    suffix = "unchanged-" * 8

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/unequal-prefix.py",
                        "edits": [
                            {
                                "oldText": f"OLD-{suffix}\n",
                                "newText": f"NEW-{suffix}\nextra\n",
                            }
                        ],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            return _first_tool_card(app_instance).render().plain

    text = anyio.run(scenario)
    assert "OLD-" in text
    assert "NEW-" in text


def test_textual_tool_card_narrow_write_keeps_source_before_newline_note() -> None:
    """Terminator annotations never displace changed source at compact widths."""

    old = "x" * 12_000 + "OLD"
    new = "x" * 12_000 + "NEW"

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="write",
                    arguments={"path": "src/no-newline.py", "content": new},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="write",
                    output="Wrote replacement",
                    is_error=False,
                    before_text=old,
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return card.render().plain

    text = anyio.run(scenario)
    assert "OLD" in text
    assert "NEW" in text
    assert "no newline" not in text


def test_textual_tool_card_marks_omitted_newline_note_on_exact_source_width() -> None:
    """A hidden no-newline annotation remains visible as horizontal omission."""

    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="write",
                    arguments={"path": "src/newline.py", "content": "abcdefgh\n"},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="write",
                    output="Wrote replacement",
                    is_error=False,
                    before_text="abcdefgh",
                )
            )
            await pilot.pause()
            rows = [
                line.rstrip()
                for line in _first_tool_card(app_instance).render().plain.splitlines()
                if "│" in line
            ]
            return rows[0], rows[1]

    deletion, addition = anyio.run(scenario)
    assert "…" in deletion
    assert deletion != addition


def test_textual_tool_card_narrow_full_line_replace_marks_hidden_tail() -> None:
    """A cropped full-line replacement must explicitly mark hidden changed text."""

    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(28, 20)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/minified.py",
                        "edits": [{"oldText": "x" * 120, "newText": "y" * 120}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            rows = [
                line.rstrip()
                for line in _first_tool_card(app_instance).render().plain.splitlines()
                if "│" in line
            ]
            return rows[0], rows[1]

    deletion, addition = anyio.run(scenario)
    assert "…" in deletion
    assert "…" in addition


def test_textual_tool_card_write_renders_colored_diff() -> None:
    # Issue #74 PR B2: a successful `write` renders a colored unified diff. Unlike
    # edit, the "before" text is NOT in the request args (which carry only the new
    # content); it rides the result event as before_text (captured by the tool
    # before it overwrote the file) and must survive to the renderer. Drives the
    # real request → result pipeline and asserts diff text + resolved theme colors.
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="write",
                    arguments={"path": "src/foo.py", "content": "line b\n"},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="write",
                    output="Wrote 7 bytes to src/foo.py",
                    is_error=False,
                    before_text="line a\n",
                )
            )
            await pilot.pause()
            text = "\n".join(_transcript_texts(app_instance))
            styles = _transcript_styles(app_instance)
            return text, styles

    text, styles = anyio.run(scenario)
    assert "• Wrote  src/foo.py" in text
    assert "M src/foo.py  +1 -1" in text
    assert "- │ line a" in text  # deletion line (prior content)
    assert "+ │ line b" in text  # addition line (new content)
    assert "$diff-add-fg on $diff-add-bg" in styles  # addition row band
    assert "$diff-del-fg on $diff-del-bg" in styles  # deletion row band


def test_textual_tool_card_write_create_renders_pure_addition() -> None:
    # A newly created file has no before_text but created=True; the write renders a
    # diff as an all-additions preview so the transcript shows what was written.
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="write",
                    arguments={"path": "new.py", "content": "fresh line\n"},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="write",
                    output="Wrote 11 bytes to new.py",
                    is_error=False,
                    before_text=None,
                    created=True,
                )
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    text = anyio.run(scenario)
    assert "• Wrote  new.py" in text
    assert "A new.py  +1 -0" in text
    assert "+ │ fresh line" in text


def test_textual_tool_card_expands_structured_diff_without_showing_acknowledgement() -> None:
    """Diff expansion reveals more review rows, never the raw tool acknowledgement."""

    old = "".join(f"old {index}\n" for index in range(20))
    new = "".join(f"new {index}\n" for index in range(20))

    async def scenario() -> tuple[str, str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "src/large.py",
                        "edits": [{"oldText": old, "newText": new}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="edit",
                    output="Applied 1 edit(s) to src/large.py",
                    is_error=False,
                )
            )
            await pilot.pause()
            collapsed = "\n".join(_transcript_texts(app_instance))

            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            expanded = "\n".join(_transcript_texts(app_instance))

            await pilot.press("enter")
            await pilot.pause()
            recollapsed = "\n".join(_transcript_texts(app_instance))
            return collapsed, expanded, recollapsed

    collapsed, expanded, recollapsed = anyio.run(scenario)
    assert "- │ old 0" in collapsed and "+ │ new 0" in collapsed
    assert "old 19" not in collapsed and "new 19" not in collapsed
    assert "… 16 lines hidden" in collapsed
    assert "- │ old 19" in expanded and "+ │ new 19" in expanded
    assert "Applied 1 edit" not in expanded
    assert "old 19" not in recollapsed and "new 19" not in recollapsed


def test_textual_tool_card_write_overwrite_without_snapshot_shows_summary() -> None:
    # Overwriting a file whose prior text couldn't be captured (before_text=None,
    # created=False) must show the plain summary — never a pure-addition diff that
    # would falsely read as a create.
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="write",
                    arguments={"path": "data.bin", "content": "replacement\n"},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="write",
                    output="Wrote 12 bytes to data.bin",
                    is_error=False,
                    before_text=None,
                    created=False,
                )
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    text = anyio.run(scenario)
    assert "• Wrote  data.bin" in text
    assert "Wrote 12 bytes to data.bin" in text
    assert "+replacement" not in text  # not rendered as a create-style diff


def test_textual_tool_card_read_shows_summary_not_raw_output() -> None:
    # Issue #74 PR C: a successful read renders its one-line summary in place of the
    # raw file dump. The summary rides the result event (promoted from the tool's
    # structured data); the card shows it instead of the output lines.
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(call_id="c1", name="read", arguments={"path": "foo.py"})
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output="import os\nimport sys\nprint('hi')\n",
                    is_error=False,
                    summary="read 3 lines from foo.py",
                )
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    text = anyio.run(scenario)
    assert "• Read  foo.py" in text
    assert "read 3 lines from foo.py" in text
    assert "import os" not in text  # the raw output is replaced by the summary


def test_textual_tool_card_grep_shows_summary_and_match_preview() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="grep", arguments={"pattern": "x"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="grep",
                    output="a.py:1:x\nb.py:2:x\nc.py:3:x\n",
                    is_error=False,
                    summary="grep: 3 matches",
                )
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    text = anyio.run(scenario)
    assert "• Searched  /x/ in ." in text  # semantic call arguments survive resolution
    assert "grep: 3 matches" in text
    assert "a.py:1:x" in text  # bounded evidence accompanies the structured count
    assert "c.py:3:x" in text


def _first_tool_card(app: TextualTui) -> ToolCard:
    transcript = app.query_one("#transcript", Transcript)
    return next(child for child in transcript.children if isinstance(child, ToolCard))


def _all_tool_cards(app: TextualTui) -> list[ToolCard]:
    transcript = app.query_one("#transcript", Transcript)
    return [child for child in transcript.children if isinstance(child, ToolCard)]


def test_textual_parallel_tool_cards_resolve_by_call_id_out_of_order() -> None:
    # Parallel tool calls each own a stable card keyed by call_id: two requests mount
    # two pending cards (in request order), and resolving them OUT OF ORDER routes each
    # result to its own card — the second-requested tool finishing first must not land
    # on the first card. Covers the acceptance-criteria "parallel tools" case.
    async def scenario() -> tuple[list[str], list[tuple[str, str, str]]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={"path": "a"}))
            await pilot.pause()
            renderer.event(ToolCallRequested(call_id="c2", name="bash", arguments={"command": "b"}))
            await pilot.pause()
            pending = [card._tool_name for card in _all_tool_cards(app_instance)]

            # Resolve c2 (the second request) BEFORE c1 — the interleaved-finish case.
            renderer.event(
                ToolResultReady(call_id="c2", name="bash", output="OUTPUT_B", is_error=False)
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output="OUTPUT_A",
                    is_error=False,
                    summary="read A",
                )
            )
            await pilot.pause()
            resolved = [
                (card._tool_name, card._status, card._full_output)
                for card in _all_tool_cards(app_instance)
            ]
            return pending, resolved

    pending, resolved = anyio.run(scenario)
    assert pending == ["read", "bash"]  # two cards, in request order
    # Each result routed to its own card despite the reversed finish order; mount order
    # is preserved (the earlier request stays first).
    assert resolved == [
        ("read", "done", "OUTPUT_A"),
        ("bash", "done", "OUTPUT_B"),
    ]


def test_textual_tool_card_expands_to_full_output_on_enter() -> None:
    # Issue #74 PR D: a resolved read card shows its one-line summary collapsed, and
    # expands to the full (tool-bounded) output when focused and Enter is pressed —
    # restoring what the summary replaced. Enter again collapses it back.
    full = "".join(f"line {i}\n" for i in range(30))

    async def scenario() -> tuple[str, str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={"path": "f.py"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output=full,
                    is_error=False,
                    summary="read 30 lines from f.py",
                )
            )
            await pilot.pause()
            collapsed = "\n".join(_transcript_texts(app_instance))

            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            expanded = "\n".join(_transcript_texts(app_instance))

            await pilot.press("enter")
            await pilot.pause()
            recollapsed = "\n".join(_transcript_texts(app_instance))
            return collapsed, expanded, recollapsed

    collapsed, expanded, recollapsed = anyio.run(scenario)
    # Collapsed: summary shown, full output hidden.
    assert "read 30 lines from f.py" in collapsed
    assert "line 29" not in collapsed
    # Expanded: full output revealed.
    assert "line 29" in expanded
    # Re-collapsed: back to the summary.
    assert "line 29" not in recollapsed
    assert "read 30 lines from f.py" in recollapsed


def test_textual_bash_card_normalizes_collapsed_and_full_output() -> None:
    async def scenario() -> tuple[str, str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="bash",
                    arguments={"command": "echo ok"},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="bash",
                    output="Command exited with code 0: ok",
                    is_error=False,
                    exit_code=0,
                    output_has_exit_status=True,
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            detail = card._detail
            assert isinstance(detail, str)
            return detail, card._full_output, card._can_expand()

    detail, full_output, can_expand = anyio.run(scenario)

    assert detail == "ok"
    assert full_output == "ok"
    assert can_expand is False


def test_textual_failed_bash_card_normalizes_collapsed_and_full_output() -> None:
    async def scenario() -> tuple[Content, str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="bash",
                    arguments={"command": "failing-command"},
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="bash",
                    output="Command exited with code 2: diagnostic",
                    is_error=False,
                    exit_code=2,
                    output_has_exit_status=True,
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            detail = card._detail
            assert isinstance(detail, Content)
            return detail, card._full_output, card._can_expand()

    detail, full_output, can_expand = anyio.run(scenario)

    assert detail.plain == "exit 2\ndiagnostic"
    assert full_output == detail.plain
    assert next(str(span.style) for span in detail.spans if span.start == 0) == "$error"
    assert can_expand is False


def test_textual_tool_card_expanded_shows_tool_truncation_marker() -> None:
    # When the tool itself capped its output (truncated=True), the expanded card says
    # so — even the full view isn't the whole story.
    full = "".join(f"row {i}\n" for i in range(30))

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={"path": "big"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output=full,
                    is_error=False,
                    summary="read 30 lines from big",
                    truncated=True,
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    text = anyio.run(scenario)
    assert "truncated at the tool's limit" in text


def test_textual_tool_card_small_capped_output_shows_truncation_marker_collapsed() -> None:
    # A tool that capped its output but returned a buffer that fits the preview budget
    # has nothing extra to expand, so the card stays collapsed with no affordance. The
    # truncation marker must still show — otherwise the capped output reads as complete.
    # (Codex-flagged: small max_output_bytes/lines on bash/custom tools.)
    short = "line 1\nline 2\nline 3\n"

    async def scenario() -> tuple[str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={"command": "x"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="bash",
                    output=short,
                    is_error=False,
                    truncated=True,
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            return card.render().plain, card._can_expand()

    rendered, can_expand = anyio.run(scenario)
    assert can_expand is False  # nothing more to expand than the collapsed preview
    assert "▸" not in rendered  # so no affordance is offered
    assert "truncated at the tool's limit" in rendered  # but truncation is still surfaced


def test_textual_tool_card_escape_returns_focus_to_input() -> None:
    # Escape on a focused card hands focus back to the prompt input, so the reader
    # isn't stranded on a card.
    async def scenario() -> bool:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={"path": "f"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output="a\nb\nc\n",
                    is_error=False,
                    summary="read 3 lines from f",
                )
            )
            await pilot.pause()
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return app_instance._input is app_instance.focused

    assert anyio.run(scenario) is True


def test_textual_tool_card_expand_keeps_tail_follow() -> None:
    # Expanding the newest card while following must keep the transcript pinned to the
    # tail — content growth alone should not break follow-tail (acceptance criterion).
    full = "".join(f"line {i}\n" for i in range(40))

    async def scenario() -> bool:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={"path": "f"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output=full,
                    is_error=False,
                    summary="read 40 lines from f",
                )
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            assert transcript.is_following  # resting at the tail
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return transcript.is_following

    assert anyio.run(scenario) is True


def test_textual_tool_card_expand_repins_tail_when_focus_scrolled_a_tall_card() -> None:
    # A card taller than the viewport is center-scrolled by Textual when it takes
    # focus, which settles the transcript off the bottom and flips follow off before
    # the expand runs. A followed reader who focuses the newest card and expands it
    # must still end up pinned to the tail (acceptance criterion). The collapsed
    # preview here (a bash result with no one-line summary) is wide enough to wrap
    # past the default viewport, so focusing it genuinely triggers the center-scroll
    # — the plain short-summary case doesn't exercise this path.
    preview = "".join(("X" * 280 + "\n") for _ in range(8))
    full = preview + "".join(f"tail line {i}\n" for i in range(30))

    async def scenario() -> tuple[bool, bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={"command": "x"}))
            await pilot.pause()
            renderer.event(ToolResultReady(call_id="c1", name="bash", output=full, is_error=False))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            following_before = transcript.is_following  # resting at the tail
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            # Focusing a card taller than the viewport center-scrolls it on some
            # Textual/platform combinations. If the driver keeps the tail pinned,
            # force the same precondition directly so this test verifies the
            # acceptance criterion, not Textual's focus-scroll implementation.
            if transcript.is_following:
                transcript.restore_viewport_state(
                    TranscriptViewportState(scroll_y=0, following=False)
                )
                await pilot.pause()
            following_after_focus = transcript.is_following
            await pilot.press("enter")
            await pilot.pause()
            return following_before, following_after_focus, transcript.is_following

    following_before, following_after_focus, following_after_expand = anyio.run(scenario)
    assert following_before is True
    assert following_after_focus is False  # the focus scroll dropped follow-tail
    assert following_after_expand is True  # the explicit expand re-pinned it


def test_textual_tool_card_expand_of_older_card_does_not_yank_viewport() -> None:
    # Expanding an *older* card while the transcript is following must NOT scroll to
    # the tail: the reader asked to see that card's output, so the freshly revealed
    # content (its top) must stay in view rather than being yanked toward later output.
    # Only the newest card's expansion re-pins the tail.
    def _output(tag: str) -> str:
        return "".join(f"{tag} line {i}\n" for i in range(30))

    async def scenario() -> tuple[float, float, float, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            for idx in (1, 2):
                renderer.event(
                    ToolCallRequested(call_id=f"c{idx}", name="read", arguments={"path": f"f{idx}"})
                )
                await pilot.pause()
                renderer.event(
                    ToolResultReady(
                        call_id=f"c{idx}",
                        name="read",
                        output=_output(f"c{idx}"),
                        is_error=False,
                        summary=f"read 30 lines from f{idx}",
                    )
                )
                await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            renderer.token_delta("live output keeps the stream layout anchored")
            await pilot.pause()
            assert transcript.is_anchored
            assert transcript.is_following  # resting at the tail, both cards fit
            cards = [c for c in transcript.children if isinstance(c, ToolCard)]
            older = cards[0]
            # Keep the viewport at the followed tail while delivering the same
            # focus event a visible older card receives. This isolates the race
            # Codex identified from Textual's optional deferred center-scroll.
            app_instance.screen.set_focus(older, scroll_visible=False)
            await pilot.pause()
            assert not transcript.is_anchored  # focus disarmed the active stream
            assert transcript.is_following
            renderer.token_delta(" while the reader examines an older card")
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            assert transcript.is_anchored  # a later provider delta can re-arm it
            top_before = older.region.y
            await pilot.press("enter")
            await pilot.pause()
            result = (
                top_before,
                older.region.y,
                transcript.scroll_y,
                transcript.is_anchored,
            )
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            return result

    top_before, top_after, scroll_y, anchored_after = anyio.run(scenario)
    # The older card's top was visible before expanding and must remain in view — a
    # re-pin would push it off the top (its region.y going sharply negative).
    assert top_before >= 0
    assert top_after >= 0  # not yanked: the older card's top is still on-screen
    assert scroll_y == 0  # the viewport did not jump to the tail
    assert not anchored_after  # toggle disarmed the delta that arrived after focus


def test_textual_tool_card_expand_does_not_repin_after_user_scrolls_away() -> None:
    # The follow intent captured when a card takes focus is stale once the reader
    # deliberately scrolls up (PageUp/wheel) before expanding. Expanding then must NOT
    # yank the viewport back to the tail — the user has left tail-follow on purpose.
    # The card here is tall (wide bash preview) so focusing it does drop follow via the
    # center-scroll; the user's PageUp is a *further*, deliberate move away.
    preview = "".join(("X" * 280 + "\n") for _ in range(8))
    full = preview + "".join(f"tail line {i}\n" for i in range(30))

    async def scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={"command": "x"}))
            await pilot.pause()
            renderer.event(ToolResultReady(call_id="c1", name="bash", output=full, is_error=False))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            card = _first_tool_card(app_instance)
            card.focus()  # tall card: center-scroll drops follow, intent captured
            await pilot.pause()
            await pilot.press("pageup")  # the reader deliberately scrolls away
            await pilot.pause()
            following_after_scroll = transcript.is_following
            await pilot.press("enter")  # expand must NOT re-pin the tail now
            await pilot.pause()
            return following_after_scroll, transcript.is_vertical_scroll_end

    following_after_scroll, at_tail_after_expand = anyio.run(scenario)
    assert following_after_scroll is False  # the user's scroll left tail-follow
    assert at_tail_after_expand is False  # expanding did not yank them back to the tail


def test_textual_tool_card_escape_returns_to_decision_panel_when_input_hidden() -> None:
    # With a decision panel open the input is hidden; Escape on a focused card must
    # hand focus to the panel's choice list (not strand the reader on the card with
    # no way back), mirroring the jump-to-latest fallback.
    async def scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={"path": "f"}))
            await pilot.pause()
            renderer.event(
                ToolResultReady(
                    call_id="c1",
                    name="read",
                    output="a\nb\nc\n",
                    is_error=False,
                    summary="read 3 lines from f",
                )
            )
            await pilot.pause()
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="c2",
                    name="bash",
                    arguments={"command": "echo ok"},
                    safety="command",
                )
            )
            await pilot.pause()
            input_hidden = not app_instance._input.display
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            options = app_instance.query_one("#decision-options", OptionList)
            return input_hidden, app_instance.focused is options

    input_hidden, options_focused = anyio.run(scenario)
    assert input_hidden is True  # the panel hid the input
    assert options_focused is True  # Escape landed on the panel, not stranded on the card


def test_textual_tool_card_edit_content_is_not_markup_injectable() -> None:
    # End-to-end injection guard: edit content containing markup metacharacters
    # must render literally in the transcript, never parsed as color markup.
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(
                    call_id="c1",
                    name="edit",
                    arguments={
                        "path": "x",
                        "edits": [{"oldText": "old", "newText": "[red]INJECT[/red]"}],
                    },
                )
            )
            await pilot.pause()
            renderer.event(
                ToolResultReady(call_id="c1", name="edit", output="Applied", is_error=False)
            )
            await pilot.pause()
            return "\n".join(_transcript_texts(app_instance))

    text = anyio.run(scenario)
    assert "[red]INJECT[/red]" in text  # literal, not parsed


def test_textual_tool_card_without_a_request_shows_no_duration() -> None:
    # A result arriving with no prior request (e.g. a resumed session) can't
    # compute a duration; the card is simply never mounted, so nothing is shown
    # rather than a bogus 0s. Assert no ToolCard appears.
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolResultReady(call_id="orphan", name="grep", output="x", is_error=False)
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            return sum(1 for c in transcript.children if isinstance(c, ToolCard))

    assert anyio.run(scenario) == 0


def test_textual_renderer_escapes_untrusted_event_payloads() -> None:
    # Tool-controlled fields (name, arguments, output) must not inject Rich markup.
    # The pending card shows the escaped name + arg summary; after the result the
    # same card shows the escaped output line.
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                ToolCallRequested(call_id="c1", name="evil[/blue]", arguments={"k": "[red]x[/red]"})
            )
            await pilot.pause()
            card = next(
                c
                for c in app_instance.query_one("#transcript", Transcript).children
                if isinstance(c, ToolCard)
            )
            pending = card.render().plain
            renderer.event(
                ToolResultReady(call_id="c1", name="t", output="[bold]out[/bold]", is_error=False)
            )
            await pilot.pause()
            return pending, card.render().plain

    pending, done = anyio.run(scenario)
    # Rich markup control chars survive verbatim as literal text (rendered), which
    # means they were escaped at the boundary, not interpreted as style tags.
    assert "evil[/blue]" in pending
    assert "[red]x[/red]" in pending
    assert "[bold]out[/bold]" in done


def test_textual_renderer_ignores_unhandled_framing_events() -> None:
    # An event type with no dedicated branch is dropped, not dumped as its repr.
    # MessageDelta is streaming plumbing (assistant text arrives via the streaming
    # path, not event()); showing it in the transcript was the noise bug.
    rendered = _render_events_to_transcript([message_delta(delta="raw")])

    assert rendered == ""  # nothing rendered


def _rendered_segment_styles(events: list[object]) -> str:
    # Return the applied styles of every LineMessage segment (as Rich style
    # strings, e.g. "bold #5cc9a7") so tests can assert theme colors are applied.
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            for event in events:
                renderer.event(event)
            await pilot.pause()
            return _transcript_styles(app_instance)

    return anyio.run(scenario)


def test_textual_tui_registers_and_activates_wisp_theme() -> None:
    async def scenario() -> tuple[str, list[str]]:
        app_instance = TextualTui()
        async with app_instance.run_test():
            wisp_themes = [
                name for name in app_instance.available_themes if name.startswith("wisp")
            ]
            return app_instance.theme, wisp_themes

    active, registered = anyio.run(scenario)
    assert active == "wisp"
    assert "wisp" in registered
    assert "wisp-light" in registered


def test_textual_transcript_uses_theme_colors() -> None:
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(completed_message(content="hi"))
            renderer.event(ErrorEvent(message="boom"))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            assistant, error = transcript.children
            return (
                assistant.styles.border_left[1].hex.lower(),
                error.styles.border_left[1].hex.lower(),
            )

    # Textual's CSS color conversion rounds the configured #d16a7c error rail
    # to #d06a7c; assert the resolved widget colors rather than Rich span colors.
    assert anyio.run(scenario) == ("#5cc9a7", "#d06a7c")


def test_textual_tool_card_carries_role_class_for_lifecycle_styling() -> None:
    # A ToolCard's rail lives in its `message--<role>` CSS class; glyph color is
    # a separate Content span. Assert the class here rather than a span color.
    cards = _cards_for_events([ToolCallRequested(call_id="c1", name="bash", arguments={})])
    assert cards == [("message--tool", "")]


def test_textual_theme_switch_rederives_transcript_styles() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            app_instance.theme = "wisp-light"
            renderer.event(completed_message(content="after switch"))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            (assistant,) = transcript.children
            return assistant.styles.border_left[1].hex.lower()

    # The post-switch rail uses the light theme's success color, not dark's.
    assert anyio.run(scenario) == "#2b8164"


def test_textual_theme_switch_rederives_muted_text_color() -> None:
    # Issue #76's baked MUTED_DARK/MUTED_LIGHT constants (theme.py) are new
    # since the general rederive test above was written — a "dim"-styled line
    # written after a theme switch must resolve to the *new* theme's muted
    # color, not linger on the old one. watch_theme() only re-derives
    # _role_styles for lines written after the switch; already-mounted lines
    # keep their baked-in markup, which this test isn't exercising.
    from wisp.tui.theme import MUTED_DARK, MUTED_LIGHT

    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            app_instance.theme = "wisp-light"
            renderer.input_cleared()  # -> write_dim("input cleared")
            await pilot.pause()
            return _transcript_styles(app_instance)

    rendered = anyio.run(scenario)
    assert MUTED_LIGHT in rendered
    assert MUTED_DARK not in rendered


def test_textual_themed_transcript_still_escapes_untrusted_payloads() -> None:
    # Routing colors through the theme must not weaken the escape invariant.
    rendered = _render_events_to_transcript(
        [ToolCallRequested(call_id="c1", name="evil[/blue]", arguments={"k": "[red]x[/red]"})]
    )
    assert "evil[/blue]" in rendered
    assert "[red]x[/red]" in rendered


def _stream_deltas(deltas: list[str], *, pause_between: bool) -> tuple[list[str], int]:
    # Stream deltas through the renderer, optionally pausing between each (spaced
    # arrival) or not (a burst — the mount-race stress case). Returns the final
    # transcript texts and the number of StreamMessage widgets mounted.
    async def scenario() -> tuple[list[str], int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            for delta in deltas:
                renderer.token_delta(delta)
                if pause_between:
                    await pilot.pause()
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            transcript = app_instance.query_one("#transcript", Transcript)
            streams = sum(1 for c in transcript.children if isinstance(c, StreamMessage))
            return _transcript_texts(app_instance), streams

    return anyio.run(scenario)


def test_textual_streaming_accumulates_into_one_markdown_widget() -> None:
    texts, streams = _stream_deltas(
        ["# Plan\n", "Use ", "`bash`", " to **list**."], pause_between=True
    )
    # Exactly one streaming widget holds the full accumulated markdown.
    assert streams == 1
    assert texts == ["# Plan\nUse `bash` to **list**."]


def test_textual_streaming_survives_a_burst_without_dropping_text() -> None:
    # No pauses between deltas: reconcile must not hit the mount race and drop
    # content (update() on a not-yet-mounted widget silently drops).
    texts, streams = _stream_deltas(list("The quick brown fox"), pause_between=False)
    assert streams == 1
    assert texts == ["The quick brown fox"]


def test_textual_streaming_coalesces_one_pending_drain_per_turn() -> None:
    async def scenario() -> tuple[int, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("first ")
            await pilot.pause()
            for delta in ("batched ", "stream ", "output"):
                renderer.token_delta(delta)
            turn = app_instance._stream._turn
            assert turn is not None
            pending_callbacks = app_instance._stream._pending_callbacks
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            return pending_callbacks, _transcript_texts(app_instance)[0]

    pending_callbacks, text = anyio.run(scenario)
    assert pending_callbacks == 1
    assert text == "first batched stream output"


def test_textual_stream_completion_reconciles_authoritative_content() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("partial response")
            renderer.end_token_stream_with_content("complete authoritative response")
            await app_instance.wait_for_stream_idle()
            stream = app_instance.query_one(StreamMessage)
            return stream.source

    assert anyio.run(scenario) == "complete authoritative response"


def test_textual_stream_completion_does_not_erase_deltas_with_empty_content() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("response that was streamed")
            await pilot.pause()
            renderer.end_token_stream_with_content("")
            await app_instance.wait_for_stream_idle()
            stream = app_instance.query_one(StreamMessage)
            return stream.source

    assert anyio.run(scenario) == "response that was streamed"


def test_textual_stream_uses_one_widget_without_a_markdown_mount_barrier() -> None:
    authoritative = "content written through one static widget"

    async def scenario() -> tuple[str, int]:
        app_instance, _renderer = create_textual_tui()
        async with app_instance.run_test():
            transcript = app_instance.query_one("#transcript", Transcript)
            stream = StreamMessage()
            await transcript.mount_message(stream)
            await stream.replace_markdown(authoritative)
            return stream.source, len(stream.children)

    source, child_count = anyio.run(scenario)
    assert source == authoritative
    assert child_count == 0


def test_textual_stream_completion_falls_back_when_final_markdown_update_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    authoritative = "[red]complete authoritative response[/red]"
    original_build = StreamMessage._build_markdown

    def fail_authoritative_update(stream: StreamMessage, content: str) -> object:
        if content == authoritative:
            raise RuntimeError("simulated final Markdown failure")
        return original_build(stream, content)

    monkeypatch.setattr(StreamMessage, "_build_markdown", fail_authoritative_update)

    async def scenario() -> tuple[Content, str, int, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("partial response")
            renderer.end_token_stream_with_content(authoritative)
            renderer.record_streamed_message_completed(completed_message(content=authoritative))
            await app_instance.wait_for_stream_idle()
            stream = app_instance.query_one(StreamMessage)
            assert isinstance(stream.content, Content)
            return (
                stream.content,
                stream.source,
                len(app_instance.query(StreamMessage)),
                renderer._history._live_entries[-1].widget is stream,
            )

    fallback, source, stream_count, history_retained = anyio.run(scenario)
    assert fallback.plain == authoritative
    assert not fallback.spans  # fallback content is literal, not parsed as markup
    assert source == authoritative
    assert stream_count == 1
    assert history_retained is True


def test_textual_stream_failed_callback_scheduling_does_not_wedge_idle(
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> tuple[int, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            monkeypatch.setattr(app_instance, "call_after_refresh", lambda *_args: False)
            renderer.token_delta("response while the app is closing")
            renderer.end_token_stream_with_content("authoritative response")
            with anyio.fail_after(2):
                await app_instance.wait_for_stream_idle()
            return app_instance._stream._pending_callbacks, bool(
                app_instance._stream._finalizing_turns
            )

    pending_callbacks, has_finalizers = anyio.run(scenario)
    assert pending_callbacks == 0
    assert has_finalizers is False


def test_textual_stream_finalizers_preserve_flush_order(
    monkeypatch: MonkeyPatch,
) -> None:
    first_append_started = asyncio.Event()
    release_first_append = asyncio.Event()
    original_append = StreamMessage.append_markdown

    async def delayed_first_append(stream: StreamMessage, fragment: str) -> None:
        if fragment == "first partial":
            first_append_started.set()
            await release_first_append.wait()
        await original_append(stream, fragment)

    monkeypatch.setattr(StreamMessage, "append_markdown", delayed_first_append)

    async def scenario() -> tuple[list[str], list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            try:
                renderer.token_delta("first partial")
                with anyio.fail_after(2):
                    await first_append_started.wait()
                    renderer.end_token_stream_with_content("first final")
                    renderer.token_delta("second partial")
                    renderer.end_token_stream_with_content("second final")
                    release_first_append.set()
                    await app_instance.wait_for_stream_idle()
                transcript = app_instance.query_one("#transcript", Transcript)
                mounted = [
                    child.source
                    for child in transcript.children
                    if isinstance(child, StreamMessage)
                ]
                settled = [
                    widget.source
                    for widget, _entry_count in app_instance._transcript_controller._settled_widgets
                    if isinstance(widget, StreamMessage)
                ]
                return mounted, settled
            finally:
                release_first_append.set()

    mounted, settled = anyio.run(scenario)
    assert mounted == ["first final", "second final"]
    assert settled == mounted


def test_textual_transcript_replacement_invalidates_a_flushed_stream_finalizer(
    monkeypatch: MonkeyPatch,
) -> None:
    authoritative = "response from the replaced session"
    final_update_started = asyncio.Event()
    release_final_update = asyncio.Event()
    original_replace = StreamMessage.replace_markdown

    async def delayed_final_update(stream: StreamMessage, content: str) -> None:
        if content == authoritative:
            final_update_started.set()
            await release_final_update.wait()
        await original_replace(stream, content)

    monkeypatch.setattr(StreamMessage, "replace_markdown", delayed_final_update)

    async def scenario() -> tuple[int, bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            try:
                renderer.token_delta("partial response")
                renderer.end_token_stream_with_content(authoritative)
                with anyio.fail_after(2):
                    await final_update_started.wait()
                    app_instance.replace_transcript()
                    release_final_update.set()
                    await app_instance.wait_for_stream_idle()
                return (
                    app_instance._transcript_controller.settled_widget_count,
                    app_instance.stream_widget_for_completed_message() is None,
                    not app_instance._stream._finalizing_turns,
                )
            finally:
                release_final_update.set()

    settled_count, completed_cleared, finalizers_cleared = anyio.run(scenario)
    assert settled_count == 0
    assert completed_cleared is True
    assert finalizers_cleared is True


def test_textual_stream_completion_repairs_an_incremental_render_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            original_append = StreamMessage.append_markdown
            failed_once = False

            async def fail_first_append(widget: StreamMessage, fragment: str) -> None:
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise RuntimeError("simulated incremental Markdown failure")
                await original_append(widget, fragment)

            monkeypatch.setattr(StreamMessage, "append_markdown", fail_first_append)
            renderer.token_delta("first half ")
            await pilot.pause()
            renderer.token_delta("second half")
            renderer.end_token_stream_with_content("first half second half")
            await app_instance.wait_for_stream_idle()
            stream = app_instance.query_one(StreamMessage)
            return stream.source

    assert anyio.run(scenario) == "first half second half"


def test_textual_stream_completion_retries_a_failed_incremental_markdown_build(
    monkeypatch: MonkeyPatch,
) -> None:
    original_build = StreamMessage._build_markdown
    failed_once = False

    def fail_first_build(stream: StreamMessage, content: str) -> object:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated incremental Markdown build failure")
        return original_build(stream, content)

    monkeypatch.setattr(StreamMessage, "_build_markdown", fail_first_build)

    async def scenario() -> tuple[str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("same final content")
            await pilot.pause()
            renderer.end_token_stream_with_content("same final content")
            await app_instance.wait_for_stream_idle()
            stream = app_instance.query_one(StreamMessage)
            return stream.source, stream.needs_reconciliation(stream.source)

    source, needs_reconciliation = anyio.run(scenario)
    assert source == "same final content"
    assert needs_reconciliation is False


def test_textual_end_token_stream_finalizes_the_bubble() -> None:
    # end_token_stream() is the ONLY place a streamed assistant turn is finalized
    # (the shell suppresses the trailing MessageCompleted when tokens rendered).
    # After it, the native stream is drained and the text persists.
    async def scenario() -> tuple[str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("final answer")
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            texts = _transcript_texts(app_instance)
            return texts[0] if texts else "", app_instance._is_streaming()

    text, is_streaming = anyio.run(scenario)
    assert text == "final answer"
    assert not is_streaming


def test_textual_stream_completion_skips_identical_final_rerender(
    monkeypatch: MonkeyPatch,
) -> None:
    replacements: list[str] = []
    original_replace = StreamMessage.replace_markdown

    async def track_replace(stream: StreamMessage, content: str) -> None:
        replacements.append(content)
        await original_replace(stream, content)

    monkeypatch.setattr(StreamMessage, "replace_markdown", track_replace)

    async def scenario() -> tuple[str, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("same final content")
            await pilot.pause()
            renderer.end_token_stream_with_content("same final content")
            await app_instance.wait_for_stream_idle()
            stream = app_instance.query_one(StreamMessage)
            return stream.source, app_instance.last_stream_write_count

    source, write_count = anyio.run(scenario)
    assert source == "same final content"
    assert write_count == 1
    assert replacements == []


def test_textual_stream_widget_is_available_before_async_finalization() -> None:
    async def scenario() -> bool:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("streamed response")
            renderer.end_token_stream()
            renderer.record_streamed_message_completed(
                completed_message(content="streamed response")
            )
            return (
                renderer._history._live_entries[-1].widget
                is app_instance.stream_widget_for_completed_message()
            )

    assert anyio.run(scenario)


def test_textual_live_eviction_defers_history_reload_while_reader_is_browsing() -> None:
    async def scenario() -> tuple[int, int]:
        app_instance, _renderer = create_textual_tui()
        requests = 0

        async def request_latest() -> None:
            nonlocal requests
            requests += 1

        async with app_instance.run_test() as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            app_instance.set_history_latest_request_hook(request_latest)
            transcript._follow = False
            app_instance.live_transcript_widget_evicted(Widget())
            await pilot.pause()
            deferred_requests = requests
            transcript.return_to_latest()
            await pilot.pause()
            return deferred_requests, requests

    deferred_requests, resumed_requests = anyio.run(scenario)
    assert deferred_requests == 0
    assert resumed_requests == 1


def test_textual_stream_shutdown_drains_pending_output() -> None:
    async def scenario() -> tuple[list[str], bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("pending output")
            await app_instance._stream.shutdown()
            return _transcript_texts(app_instance), app_instance._is_streaming()

    texts, is_streaming = anyio.run(scenario)
    assert texts == ["pending output"]
    assert not is_streaming


def test_textual_single_tick_turn_keeps_its_content() -> None:
    # A turn finalized in the same tick it mounts must not lose its text. The
    # controller schedules native writes after mount refresh, before stopping the
    # stream, so Markdown's mount initialization cannot clobber the first fragment.
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("first turn")
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            # Second turn finalized in a single tick: the fresh StreamMessage mounts
            # and finalizes before any refresh interleaves — the clobber window.
            renderer.token_delta("second turn")
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            return _transcript_texts(app_instance)

    texts = anyio.run(scenario)
    assert texts == ["first turn", "second turn"]  # neither turn lost to the clobber


def test_textual_stream_completion_survives_an_immediate_tool_event() -> None:
    authoritative = "assistant conclusion before the tool card"

    async def scenario() -> tuple[list[str], list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            renderer.token_delta("partial conclusion")
            renderer.end_token_stream_with_content(authoritative)
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            renderer.event(ToolResultReady(call_id="c1", name="bash", output="ok", is_error=False))
            await app_instance.wait_for_stream_idle()
            transcript = app_instance.query_one("#transcript", Transcript)
            kinds = [type(child).__name__ for child in transcript.children]
            stream = transcript.query_one(StreamMessage)
            return kinds, [stream.source]

    kinds, rendered = anyio.run(scenario)
    assert kinds == ["StreamMessage", "ToolCard"]
    assert authoritative in rendered


def test_textual_streamed_and_line_messages_use_distinct_widgets() -> None:
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("streamed reply")
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            return [type(c).__name__ for c in transcript.children]

    kinds = anyio.run(scenario)
    # The assistant stream is a StreamMessage (Markdown); the tool call is a
    # ToolCard (stateful styled Static) — they are different widget types.
    assert "StreamMessage" in kinds
    assert "ToolCard" in kinds


def test_textual_line_messages_carry_role_classes() -> None:
    # Every event type maps to a message--<role> class so the card CSS can style
    # it. Tool cards evolve in place, so drive each through its full lifecycle and
    # assert the terminal role class. c1 succeeds (→ approved), c2 errors (→
    # error), c4 is denied at approval (→ denied). One card per call_id. Issue
    # #76: a tool-execution error and a user denial previously both landed on
    # "denied" (glyph AND color AND label collision); a genuine error now gets
    # its own "error" role class, distinct from a user's "denied" decision.
    cards = _cards_for_events(
        [
            completed_message(content="hi"),
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ToolResultReady(call_id="c1", name="bash", output="ok", is_error=False),
            ToolCallRequested(call_id="c2", name="bash", arguments={}),
            ToolResultReady(call_id="c2", name="bash", output="boom", is_error=True),
            ToolCallRequested(call_id="c4", name="write", arguments={}),
            ToolApprovalResolved(call_id="c4", name="write", approved=False, reason="no"),
            ErrorEvent(message="bad"),
        ]
    )
    role_classes = [role for role, _ in cards]
    assert role_classes == [
        "message--assistant",
        "message--approved",  # c1 succeeded
        "message--error",  # c2 errored
        "message--denied",  # c4 denied at approval
        "message--error",
    ]


def test_textual_turn_rails_distinguish_conversation_roles_without_color() -> None:
    async def scenario() -> list[tuple[str, str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.prompt_submitted("hello")
            renderer.event(completed_message(content="hi"))
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={}))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            return [
                (role, child.styles.border_left[0])
                for child in transcript.children
                if (role := _transcript_role_class(child)) is not None
            ]

    assert anyio.run(scenario) == [
        ("message--user", "heavy"),
        ("message--assistant", "outer"),
        ("message--tool", ""),
    ]


@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_textual_transcript_keeps_tool_cards_minimal_and_semantic(theme: str) -> None:
    async def scenario() -> tuple[dict[str, tuple[str, int, int, str]], str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            app_instance.theme = theme
            renderer.prompt_submitted("hello")
            renderer.event(completed_message(content="hi"))
            renderer.event(ToolCallRequested(call_id="pending", name="read", arguments={}))
            renderer.event(ToolCallRequested(call_id="done", name="read", arguments={}))
            renderer.event(
                ToolResultReady(call_id="done", name="read", output="ok", is_error=False)
            )
            renderer.event(ToolCallRequested(call_id="error", name="read", arguments={}))
            renderer.event(
                ToolResultReady(call_id="error", name="read", output="boom", is_error=True)
            )
            renderer.event(ToolCallRequested(call_id="denied", name="write", arguments={}))
            renderer.event(
                ToolApprovalResolved(call_id="denied", name="write", approved=False, reason="no")
            )
            await pilot.pause()

            states: dict[str, tuple[str, int, int, str]] = {}
            for card in _all_tool_cards(app_instance):
                role = _transcript_role_class(card)
                assert role is not None
                content = card.render()
                assert content.plain.startswith("• ")
                assert all("dim" not in str(span.style).split() for span in content.spans)
                states[role] = (
                    card.styles.background.hex.lower(),
                    card.styles.padding.left,
                    card.styles.padding.right,
                    content.plain,
                )
            transcript = app_instance.query_one("#transcript", Transcript)
            user = next(child for child in transcript.children if child.has_class("message--user"))
            return states, user.styles.background.hex.lower()

    states, user_background = anyio.run(scenario)
    assert user_background != "#00000000"
    expected_actions = {
        "message--tool": "• Reading",
        "message--approved": "• Read",
        "message--error": "• Failed to read",
        "message--denied": "• Denied writing",
    }
    assert set(states) == set(expected_actions)
    for role, action in expected_actions.items():
        background, padding_left, padding_right, rendered = states[role]
        assert background == "#00000000"
        assert padding_left == 0
        assert padding_right == 0
        assert rendered.startswith(action)


@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_textual_bash_header_resolves_semantic_colors_in_each_theme(theme: str) -> None:
    async def scenario() -> tuple[dict[str, str], dict[str, str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            app_instance.theme = theme
            renderer.event(
                ToolCallRequested(
                    call_id="bash",
                    name="bash",
                    arguments={"command": "pytest -q tests/x.py"},
                )
            )
            renderer.event(
                ToolResultReady(
                    call_id="bash",
                    name="bash",
                    output="1 passed",
                    is_error=False,
                )
            )
            await pilot.pause()

            card = _first_tool_card(app_instance)
            resolved: dict[str, str] = {}
            for text, style in card.render().render(parse_style=card._get_style):
                color = style.rich_style.color
                if color is None:
                    continue
                for token in ("Ran", "pytest", "-q", "tests/x.py"):
                    if token in text:
                        resolved[token] = color.get_truecolor().hex.lower()
            variables = app_instance.get_css_variables()
            expected = {
                "Ran": variables["success"].lower(),
                "pytest": variables["accent"].lower(),
                "-q": variables["secondary"].lower(),
                "tests/x.py": variables["primary"].lower(),
            }
            return resolved, expected

    resolved, expected = anyio.run(scenario)
    assert resolved == expected


@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_textual_standalone_error_retains_its_surface(theme: str) -> None:
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            app_instance.theme = theme
            renderer.event(ErrorEvent(message="boom"))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            (error,) = transcript.children
            return error.styles.background.hex.lower(), app_instance.get_css_variables()[
                "error-muted"
            ].lower()

    background, expected = anyio.run(scenario)
    assert background == expected


def test_textual_focused_tool_card_uses_only_a_left_outline() -> None:
    async def scenario() -> tuple[str, tuple[str, str, str, str], str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={}))
            await pilot.pause()
            card = _first_tool_card(app_instance)
            card.focus()
            await pilot.pause()
            outlines = (
                card.styles.outline_top[0],
                card.styles.outline_right[0],
                card.styles.outline_bottom[0],
                card.styles.outline_left[0],
            )
            return (
                card.styles.background.hex.lower(),
                outlines,
                card.styles.outline_left[1].hex.lower(),
                card.has_class("message--tool"),
            )

    background, outlines, outline_color, keeps_role = anyio.run(scenario)
    assert background == "#00000000"
    assert outlines == ("", "", "", "heavy")
    assert outline_color == "#3fb8b8"
    assert keeps_role


def test_textual_denied_and_error_tool_cards_render_distinct_actions() -> None:
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="denied", name="write", arguments={}))
            renderer.event(
                ToolApprovalResolved(call_id="denied", name="write", approved=False, reason="no")
            )
            renderer.event(ToolCallRequested(call_id="errored", name="bash", arguments={}))
            renderer.event(
                ToolResultReady(call_id="errored", name="bash", output="boom", is_error=True)
            )
            await pilot.pause()
            cards = _all_tool_cards(app_instance)
            return [card.render().plain for card in cards]

    denied_card, error_card = anyio.run(scenario)
    assert denied_card.startswith("• Denied writing")
    assert error_card.startswith("• Failed to run")


def test_textual_cancelled_tool_card_action_is_not_denied() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="a", name="read_file", arguments={}))
            await pilot.pause()
            renderer.cancelled()
            await pilot.pause()
            card = _first_tool_card(app_instance)
            return card.render().plain

    rendered = anyio.run(scenario)
    assert rendered.startswith("• Cancelled calling read_file")
    assert "Denied" not in rendered


def test_textual_no_color_env_var_keeps_transcript_legible(monkeypatch: MonkeyPatch) -> None:
    # Issue #76: NO_COLOR isn't Wisp-implemented — Textual's App.__init__ reads
    # it from the environment once, at construction, and appends its own
    # Monochrome filter. That's free coverage Wisp never explicitly verified.
    # Set the env var BEFORE constructing the app (create_textual_tui() builds
    # a fresh TextualTui()); setting it after construction would be a no-op,
    # since App.__init__ already popped it from os.environ by then.
    monkeypatch.setenv("NO_COLOR", "1")

    async def scenario() -> tuple[bool, list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            renderer.event(ToolResultReady(call_id="c1", name="bash", output="ok", is_error=False))
            renderer.event(ToolCallRequested(call_id="c2", name="write", arguments={}))
            renderer.event(
                ToolApprovalResolved(call_id="c2", name="write", approved=False, reason="no")
            )
            renderer.event(ToolCallRequested(call_id="c3", name="bash", arguments={}))
            renderer.event(ToolResultReady(call_id="c3", name="bash", output="boom", is_error=True))
            renderer.event(
                ToolCallRequested(
                    call_id="c4",
                    name="edit",
                    arguments={
                        "path": "plain.py",
                        "edits": [{"oldText": "old\n", "newText": "new\n"}],
                    },
                )
            )
            renderer.event(
                ToolResultReady(call_id="c4", name="edit", output="Applied", is_error=False)
            )
            renderer.notice("heads up")
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            texts = [
                child.render().plain
                for child in transcript.children
                if isinstance(child, LineMessage | ToolCard)
            ]
            return app_instance.no_color, texts

    no_color, texts = anyio.run(scenario)
    rendered = "\n".join(texts)

    assert no_color is True  # the env var was actually observed
    # Explicit action words survive color removal; status never depends on hue.
    assert "• Ran" in rendered
    assert "• Denied writing" in rendered
    assert "• Failed to run" in rendered
    assert "M plain.py  +1 -1" in rendered
    assert "- │ old" in rendered
    assert "+ │ new" in rendered
    assert "heads up" in rendered


def test_textual_conversation_messages_have_no_role_title() -> None:
    # Conversation roles remain attached as CSS classes. Flat tool trees also
    # avoid border titles; standalone operational errors retain theirs.
    cards = _cards_for_events(
        [
            completed_message(content="hi"),
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ErrorEvent(message="bad"),
        ]
    )
    titles = [title for _, title in cards]
    assert titles == [None, "", _ROLE_LABELS["error"]]


def test_textual_running_uses_transcript_heartbeat_and_stable_status_bar() -> None:
    async def scenario() -> tuple[str, str, list[tuple[str | None, object]]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            status = app_instance.query_one("#status", StatusBar)
            return (
                status.render().plain,
                _working_activity(app_instance),
                _transcript_cards(app_instance),
            )

    status, activity, cards = anyio.run(scenario)
    assert "Working" not in status
    assert "Working" in activity
    assert cards == [("message--dim", None)]


def test_textual_compaction_notices_and_rpc_completion_stop_progress() -> None:
    async def scenario() -> tuple[list[str], list[tuple[str | None, object]], bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(CompactionStarted(session_id="session-1", source_entry_count=6))
            renderer.event(
                CompactionCompleted(
                    session_id="session-1",
                    outcome="completed",
                    replaced_entry_count=5,
                    retained_entry_count=1,
                )
            )
            await pilot.pause()
            assert app_instance._transcript_controller.working_indicator is not None

            renderer.event(
                RpcCommandFinished(command_id="compact-1", command_type="compact", ok=True)
            )
            await pilot.pause()
            return (
                _transcript_texts(app_instance),
                _transcript_cards(app_instance),
                app_instance._transcript_controller.working_indicator is None,
            )

    texts, cards, progress_stopped = anyio.run(scenario)
    assert "Compacting session..." in texts
    assert "Compacted 5 context entries." in texts
    assert all(role != "message--assistant" for role, _title in cards)
    assert progress_stopped


def test_textual_threshold_compaction_failure_is_a_notice() -> None:
    async def scenario() -> tuple[list[str], list[tuple[str | None, object]]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(
                CompactionStarted(
                    session_id="session-1",
                    reason="threshold",
                    source_entry_count=6,
                    trigger_budget=threshold_budget(),
                )
            )
            renderer.event(
                CompactionCompleted(
                    session_id="session-1",
                    reason="threshold",
                    outcome="completed",
                    replaced_entry_count=5,
                    retained_entry_count=1,
                )
            )
            renderer.event(
                CompactionCompleted(
                    session_id="session-1",
                    reason="threshold",
                    outcome="failed",
                    replaced_entry_count=5,
                    retained_entry_count=1,
                    error="summary failed",
                )
            )
            await pilot.pause()
            return _transcript_texts(app_instance), _transcript_cards(app_instance)

    texts, cards = anyio.run(scenario)
    assert "Context threshold reached; compacting automatically..." in texts
    assert "Automatically compacted 5 context entries." in texts
    assert "Automatic compaction failed: summary failed" in texts
    assert [role for role, _title in cards] == [
        "message--notice",
        "message--notice",
        "message--notice",
    ]


def test_textual_overflow_compaction_keeps_progress_for_retry() -> None:
    async def scenario() -> tuple[list[str], bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(
                CompactionStarted(
                    session_id="session-1",
                    reason="overflow",
                    source_entry_count=6,
                    trigger_budget=threshold_budget(),
                )
            )
            await pilot.pause()
            started = app_instance._transcript_controller.working_indicator is not None
            renderer.event(
                CompactionCompleted(
                    session_id="session-1",
                    reason="overflow",
                    outcome="completed",
                    replaced_entry_count=5,
                    retained_entry_count=1,
                    will_retry=True,
                )
            )
            await pilot.pause()
            remained = app_instance._transcript_controller.working_indicator is not None
            renderer.event(TurnStarted(turn=2))
            await pilot.pause()
            continued = app_instance._transcript_controller.working_indicator is not None
            return _transcript_texts(app_instance), started and remained, continued

    texts, remained_after_compaction, continued_for_retry = anyio.run(scenario)
    assert "Context overflow detected; compacting before one retry..." in texts
    assert "Compacted 5 context entries; retrying request..." in texts
    assert remained_after_compaction
    assert continued_for_retry


def test_textual_status_activity_animates_spinner_and_counts_elapsed() -> None:
    # The heartbeat is a smooth braille spinner + a live elapsed-seconds counter,
    # both driven off one monotonic tick counter (frame = ticks % len, seconds =
    # ticks × interval). Advance ticks directly and assert the spinner rotates
    # through all its frames and the counter reaches whole seconds.
    async def scenario() -> tuple[int, str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            indicator = app_instance.query_one(WorkingIndicator)

            start = indicator.render().plain
            glyphs: set[str] = {start[0]}
            # One full spinner cycle plus enough ticks to cross 1s (interval 0.08s).
            for _ in range(len(WorkingIndicator._FRAMES) + 3):
                indicator._tick()
                glyphs.add(indicator.render().plain[0])
            return len(glyphs), start, indicator.render().plain

    distinct_glyphs, start, later = anyio.run(scenario)
    assert distinct_glyphs == len(WorkingIndicator._FRAMES)  # every frame shown → smooth
    assert start.endswith("0s")
    assert later.endswith("1s")  # counter advanced with elapsed time
    assert start[0] in WorkingIndicator._FRAMES  # a braille frame, not the old dot


def test_textual_working_indicator_ticks_without_relayout(monkeypatch: MonkeyPatch) -> None:
    layouts: list[bool] = []
    original_update = WorkingIndicator.update

    def record_update(self: WorkingIndicator, content: object = "", *, layout: bool = True) -> None:
        layouts.append(layout)
        original_update(self, content, layout=layout)

    monkeypatch.setattr(WorkingIndicator, "update", record_update)

    async def scenario() -> list[bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            indicator = app_instance.query_one(WorkingIndicator)
            layouts.clear()
            indicator._tick()
            indicator._tick()
            return layouts

    layouts = anyio.run(scenario)
    assert len(layouts) >= 2
    assert not any(layouts)


def test_textual_retry_progress_mutates_status_and_rejects_older_attempts() -> None:
    async def scenario() -> tuple[bool, str, str, list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(TurnStarted(turn=1))
            renderer.event(_provider_retry(attempt=2, delay_seconds=1.25))
            await pilot.pause()
            first = _working_activity(app_instance)

            renderer.event(_provider_retry(attempt=1, delay_seconds=9.0))
            renderer.event(_provider_retry(attempt=2, delay_seconds=9.0))
            renderer.event(_provider_retry(attempt=3, status_code=429))
            await pilot.pause()
            return (
                app_instance._transcript_controller.working_indicator is not None,
                first,
                _working_activity(app_instance),
                _transcript_texts(app_instance),
            )

    active, first, latest, transcript = anyio.run(scenario)
    assert active
    assert "Retrying openai · 2/3 in 1.2s · rate limited" in first
    assert "Retrying openai · 3/3 in 0.5s · rate limited (429)" in latest
    assert "9.0s" not in latest
    assert transcript == []


def test_textual_retry_progress_recovers_and_ignores_post_start_retry() -> None:
    async def scenario() -> tuple[str, str, bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(TurnStarted(turn=1))
            renderer.event(_provider_retry(attempt=2))
            renderer.event(MessageStarted(turn=1))
            renderer.event(_provider_retry(attempt=3))
            await pilot.pause()
            recovered = _working_activity(app_instance)

            renderer.token_delta("response")
            await pilot.pause()
            texts = _transcript_texts(app_instance)
            return (
                recovered,
                "\n".join(texts),
                app_instance._transcript_controller.working_indicator is None,
                app_instance._transcript_controller.working_indicator is not None,
            )

    recovered, transcript, timer_stopped, active = anyio.run(scenario)
    assert "Working" in recovered
    assert "Retrying" not in recovered
    assert "response" in transcript
    assert "Retrying" not in transcript
    assert not timer_stopped
    assert active


def test_textual_retry_progress_resumes_for_a_later_tool_turn() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(TurnStarted(turn=1))
            renderer.event(MessageStarted(turn=1))
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={}))
            renderer.event(ToolResultReady(call_id="c1", name="read", output="ok", is_error=False))
            renderer.event(TurnStarted(turn=2))
            renderer.event(_provider_retry(turn=2, attempt=1, reason="server_error"))
            await pilot.pause()
            return _working_activity(app_instance)

    rendered = anyio.run(scenario)
    assert "Retrying openai · 1/3 in 0.5s · server error" in rendered


def test_textual_retry_progress_does_not_replay_or_survive_terminal_states() -> None:
    async def scenario() -> tuple[bool, bool, bool, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(_provider_retry())
            await pilot.pause()
            replayed = app_instance._transcript_controller.working_indicator is not None

            renderer.running()
            renderer.event(TurnStarted(turn=1))
            renderer.event(_provider_retry())
            await pilot.pause()
            renderer.event(AgentCompleted(session_id="s1", turns=1, outcome="completed"))
            renderer.event(_provider_retry(attempt=2))
            await pilot.pause()
            remaining = app_instance._transcript_controller.working_indicator is not None
            return replayed, remaining, not remaining, _working_activity(app_instance)

    replayed, remaining, timer_stopped, rendered = anyio.run(scenario)
    assert not replayed
    assert not remaining
    assert timer_stopped
    assert "Retrying" not in rendered


def test_textual_trust_resolution_restores_working_activity() -> None:
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.trust_request(
                TrustRequested(request_id="trust-1", project_path=Path("/tmp/project"))
            )
            await pilot.pause()
            waiting = _working_activity(app_instance)

            renderer.event(
                TrustResolved(
                    request_id="trust-1",
                    project_path=Path("/tmp/project"),
                    trusted=True,
                )
            )
            await pilot.pause()
            return waiting, _working_activity(app_instance)

    waiting, resolved = anyio.run(scenario)
    assert "Waiting for trust" in waiting
    assert "Working" in resolved
    assert "Waiting for trust" not in resolved


def test_textual_skills_catalog_keeps_active_working_indicator() -> None:
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            before = _working_activity(app_instance)

            renderer.skills_catalog(RpcSkillCatalogSnapshot())
            await pilot.pause()
            return before, _working_activity(app_instance)

    before, after = anyio.run(scenario)
    assert "Working" in before
    assert "Working" in after


def test_textual_retry_progress_yields_to_approval_cancellation_and_rpc_failure() -> None:
    async def approval_scenario() -> tuple[bool, bool, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(_provider_retry())
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="c1",
                    name="bash",
                    arguments={"command": "echo ok"},
                    safety="command",
                )
            )
            await pilot.pause()
            return (
                app_instance._transcript_controller.working_indicator is not None,
                app_instance.query_one("#decision-panel").display,
                _working_activity(app_instance),
            )

    async def cancellation_scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(_provider_retry())
            await pilot.pause()
            renderer.cancelling("Cancelling current prompt...")
            renderer.event(_provider_retry(attempt=2))
            await pilot.pause()
            remaining = app_instance._transcript_controller.working_indicator is not None
            return remaining, not remaining

    async def failure_scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(_provider_retry())
            await pilot.pause()
            renderer.rpc_event_reader_failed("closed")
            await pilot.pause()
            remaining = app_instance._transcript_controller.working_indicator is not None
            return remaining, not remaining

    approval_row, approval_visible, approval_activity = anyio.run(approval_scenario)
    cancellation_row, cancellation_timer_stopped = anyio.run(cancellation_scenario)
    failure_row, failure_timer_stopped = anyio.run(failure_scenario)
    assert approval_row
    assert approval_visible
    assert "Waiting for approval" in approval_activity
    assert not cancellation_row
    assert cancellation_timer_stopped
    assert not failure_row
    assert failure_timer_stopped


def test_textual_retry_progress_preserves_compact_prompt_and_footer() -> None:
    async def scenario() -> tuple[str, str, bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(72, 20)) as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="retrying 2/3 in 0.5s",
                    input_hint="wisp(running)> ",
                    input_mode="running",
                    cwd="/tmp/project",
                    provider="openai-codex",
                    model="gpt-5-codex",
                )
            )
            renderer.running()
            renderer.event(TurnStarted(turn=1))
            renderer.event(
                _provider_retry(
                    attempt=2,
                    provider="custom-provider-name-that-is-too-long",
                    reason="transient_http",
                    status_code=503,
                )
            )
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            footer = app_instance.query_one("#status", StatusBar)
            return (
                footer.render().plain,
                _working_activity(app_instance),
                input_widget.region.y < footer.region.y,
                input_widget.display,
            )

    footer, activity, footer_below_prompt, prompt_visible = anyio.run(scenario)
    assert "custom-provider-nam…" in activity
    assert "2/3" in activity
    assert "openai-codex/gpt-5-codex" in footer
    assert footer_below_prompt
    assert prompt_visible


def test_format_duration_scales_units() -> None:
    from wisp.tui.widgets import _format_duration

    assert _format_duration(0.34) == "0.3s"  # sub-10s keeps a decimal
    assert _format_duration(9.9) == "9.9s"
    assert _format_duration(10.4) == "10s"  # past 10s the decimal is noise
    assert _format_duration(63.2) == "1m03s"  # rolls to Nm SSs past a minute
    assert _format_duration(-1.0) == "0.0s"  # clock skew clamps to 0


def test_textual_pending_tool_card_ticks_a_live_counter() -> None:
    # A running card shows a live whole-second counter (per-card timer) until it
    # resolves. Advance ticks directly and assert the counter climbs.
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="grep", arguments={}))
            await pilot.pause()
            card = next(
                c
                for c in app_instance.query_one("#transcript", Transcript).children
                if isinstance(c, ToolCard)
            )
            start = card.render().plain
            for _ in range(3):
                card._tick()
            return start, card.render().plain

    start, ticked = anyio.run(scenario)
    assert start.endswith("· 0.0s")  # counter starts at zero on mount
    assert ticked.endswith("· 3.0s")  # three 1s ticks


def test_textual_cancel_drains_pending_tool_cards() -> None:
    # A prompt that ends without results (cancel/failure/stream death) must not
    # leave tool cards spinning forever. cancelled() marks every pending card
    # cancelled, stops its timer, and clears both the app and renderer registries.
    async def scenario() -> tuple[list[str], list[bool], int, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="a", name="read_file", arguments={"p": "x"}))
            renderer.event(ToolCallRequested(call_id="b", name="grep", arguments={}))
            await pilot.pause()
            cards = [
                c
                for c in app_instance.query_one("#transcript", Transcript).children
                if isinstance(c, ToolCard)
            ]
            renderer.cancelled()
            await pilot.pause()
            return (
                [c.render().plain for c in cards],
                [c._timer is None for c in cards],
                app_instance._transcript_controller.pending_tool_count,
                len(renderer._tool_started),
            )

    texts, timers_stopped, app_registry, started_registry = anyio.run(scenario)
    assert all(t.startswith("• Cancelled ") for t in texts)
    assert all(timers_stopped)  # no card keeps ticking
    assert app_registry == 0  # app _tool_cards drained
    assert started_registry == 0  # renderer _tool_started drained


def test_textual_rpc_command_failure_drains_pending_tool_cards() -> None:
    # A non-ok RpcCommandFinished after a request but before a result must also
    # drain the pending card rather than leave it spinning.
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            await pilot.pause()
            assert app_instance._transcript_controller.pending_tool_count == 1
            renderer.event(
                RpcCommandFinished(command_id="cmd1", command_type="prompt", ok=False, error="boom")
            )
            await pilot.pause()
            return app_instance._transcript_controller.pending_tool_count

    assert anyio.run(scenario) == 0


def test_textual_session_saved_is_not_rendered() -> None:
    # SessionSaved is session/RPC audit, not conversation — the active session id
    # already lives in the status bar, so a per-turn "session saved:" line is pure
    # redundancy. The Textual renderer drops it, matching the line renderer.
    async def scenario() -> tuple[list[tuple[str | None, object]], bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(SessionSaved(session_id="sess-1", path=Path("/tmp/sess.json")))
            await pilot.pause()
            has_empty_state = bool(list(app_instance.query(TranscriptEmptyState)))
            return _transcript_cards(app_instance), has_empty_state

    cards, has_empty_state = anyio.run(scenario)
    assert cards == []
    assert has_empty_state is True


def test_textual_stream_message_carries_the_label_free_assistant_role() -> None:
    # The streamed turn retains assistant role styling without a visible title,
    # matching a settled assistant line.
    async def scenario() -> tuple[str | None, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("partial answer")
            await pilot.pause()
            (card,) = _transcript_cards(app_instance)
            return card

    role, title = anyio.run(scenario)
    assert role == "message--assistant"
    assert title is None


def test_textual_tool_tree_has_no_resting_rail_under_the_light_theme() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 16)) as pilot:
            app_instance.theme = "wisp-light"
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            (tool_card,) = transcript.children
            kind, _color = tool_card.styles.border_left
            return kind

    assert anyio.run(scenario) == ""


def _status_after_snapshots(snapshots: list[TuiViewSnapshot]) -> tuple[str, bool]:
    # Apply each snapshot in order and return the final footer text plus whether
    # the Input kept focus.
    async def scenario() -> tuple[str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            status = app_instance.query_one("#status", Static)
            for snapshot in snapshots:
                renderer.view_updated(snapshot)
                await pilot.pause()
            focus_ok = app_instance.focused is app_instance.query_one("#input", Input)
            return status.render().plain, focus_ok

    return anyio.run(scenario)


def test_textual_footer_updates_without_stealing_input_focus() -> None:
    status_text, focus_ok = _status_after_snapshots(
        [TuiViewSnapshot(status="running", input_hint="wisp(running)> ", input_mode="running")]
    )
    assert "running" in status_text
    assert focus_ok


def test_textual_status_bar_renders_compact_footer_summary() -> None:
    # The footer keeps cwd/session on the first line and status/model on the
    # second. Tests run with cwd chdir'd into a long pytest tmp path (see
    # conftest._isolate_settings), so under issue #72's cwd-outranks-session
    # priority, session correctly drops here rather than surviving as before.
    status_text, _ = _status_after_snapshots(
        [
            TuiViewSnapshot(
                status="running",
                input_hint="wisp> ",
                input_mode="running",
                queued_follow_ups=2,
                last_session="sess.json",
                provider="openai",
                model="gpt-test",
            )
        ]
    )
    assert "\n" in status_text
    assert "running • queued 2" in status_text
    assert "openai/gpt-test" in status_text


def test_textual_status_bar_shows_session_when_cwd_is_short() -> None:
    # With a short cwd both fields fit, so session id still shows up whole —
    # proving priority=left only drops session under genuine width pressure.
    status_text, _ = _status_after_snapshots(
        [
            TuiViewSnapshot(
                status="running",
                input_hint="wisp> ",
                input_mode="running",
                cwd="/tmp",
                last_session="sess.json",
                provider="openai",
                model="gpt-test",
            )
        ]
    )
    assert "session: sess.json" in status_text


def test_textual_footer_fits_the_status_content_region() -> None:
    # The footer is sized to the #status content region (padding-excluded), not
    # the app width. The status bar has horizontal padding, and #status-bar now
    # also sits inside the #composer panel's round border (1 column each side),
    # so sizing from app width would over-pad each line and make the two-line
    # footer wrap/clip. At an 80-col terminal the render region is 76 (80 minus
    # 2 for #composer's border minus 2 for #status-bar's padding); no footer
    # line may exceed it.
    from rich.cells import cell_len

    async def scenario() -> tuple[int | None, list[int]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            renderer.view_updated(
                TuiViewSnapshot(
                    status="running",
                    input_hint="wisp> ",
                    input_mode="running",
                    cwd="/Users/hanyuwu/Wisp",
                    last_session="dac1357f",
                    provider="openai-codex",
                    model="gpt-5.5",
                )
            )
            await pilot.pause()
            status = app_instance.query_one("#status", Static)
            lines = status.render().plain.split("\n")
            return app_instance.status_width(), [cell_len(ln) for ln in lines]

    region_width, line_widths = anyio.run(scenario)
    assert region_width == 76  # app width 80 minus #composer's 2-col border minus 2-col padding
    assert all(w <= 76 for w in line_widths)  # no line overflows the render region


def test_textual_footer_renders_markup_in_cwd_and_model_literally() -> None:
    # The footer is plain data (cwd, session, provider/model), but Static renders
    # markup by default — so a cwd or model name containing bracket syntax would be
    # interpreted as style tags (restyle/hide/raise). The #status widget is built
    # with markup=False, so such content must render verbatim with no style spans.
    async def scenario() -> tuple[str, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            renderer.view_updated(
                TuiViewSnapshot(
                    status="running",
                    input_hint="wisp> ",
                    input_mode="running",
                    cwd="/tmp/[/red]evil[bold]",
                    last_session="s1",
                    provider="openai",
                    model="gpt[/]x",
                )
            )
            await pilot.pause()
            rendered = app_instance.query_one("#status", Static).render()
            return rendered.plain, len(rendered.spans)

    plain, span_count = anyio.run(scenario)
    assert "[/red]" in plain  # cwd markup survives as literal text
    assert "[bold]" in plain
    assert "[/]" in plain  # model markup survives as literal text
    assert span_count == 0  # nothing interpreted as a style span


def test_textual_footer_stays_below_input_without_stealing_focus() -> None:
    async def scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.view_updated(TuiViewSnapshot(status="idle", input_hint="wisp> "))
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            footer = app_instance.query_one("#status", Static)
            return input_widget.region.y < footer.region.y, app_instance.focused is input_widget

    below_input, focus_ok = anyio.run(scenario)
    assert below_input
    assert focus_ok


def test_textual_working_status_persists_at_tail_during_stream_output() -> None:
    async def scenario() -> tuple[str, str, list[str], bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            before = _working_activity(app_instance)
            renderer.token_delta("hello")
            await pilot.pause()
            indicator = app_instance._transcript_controller.working_indicator
            transcript = app_instance.query_one("#transcript", Transcript)
            return (
                before,
                _working_activity(app_instance),
                _transcript_texts(app_instance),
                indicator is not None and transcript.children[-1] is indicator,
            )

    before, after, transcript, indicator_is_tail = anyio.run(scenario)
    assert "Working" in before
    assert "Working" in after
    assert any("hello" in text for text in transcript)
    assert indicator_is_tail


def test_textual_working_status_persists_after_tool_card_mount() -> None:
    async def scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(ToolCallRequested(call_id="c1", name="read", arguments={}))
            await pilot.pause()
            indicator = app_instance._transcript_controller.working_indicator
            transcript = app_instance.query_one("#transcript", Transcript)
            remained_at_tail = indicator is not None and transcript.children[-1] is indicator

            renderer.event(AgentCompleted(session_id="s1", turns=1, outcome="completed"))
            await pilot.pause()
            return remained_at_tail, app_instance._transcript_controller.working_indicator is None

    remained_at_tail, removed_on_completion = anyio.run(scenario)
    assert remained_at_tail
    assert removed_on_completion


def _fill_transcript(renderer: TextualTuiRenderer, count: int) -> None:
    # Mount enough lines to overflow the viewport so the transcript can scroll.
    for i in range(count):
        renderer.event(ToolCallRequested(call_id=f"c{i}", name=f"tool{i}", arguments={}))


def test_textual_transcript_requests_one_history_page_at_a_time_and_rearms() -> None:
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        requests = 0

        async def request_history_page() -> None:
            nonlocal requests
            requests += 1

        async with app_instance.run_test(size=(60, 12)) as pilot:
            renderer.render_history_entries(
                tuple(
                    HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                    for index in range(30)
                )
            )
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            renderer.set_history_page_request_hook(request_history_page)
            renderer.history_page_loaded(has_more=True)
            transcript.scroll_end(animate=False)
            await pilot.pause()
            transcript.scroll_home(animate=False)
            await pilot.pause()
            transcript.scroll_home(animate=False)
            await pilot.pause()
            assert requests == 1

            renderer.history_page_loaded(has_more=True)
            transcript.scroll_to(y=5, animate=False)
            await pilot.pause()
            transcript.scroll_home(animate=False)
            await pilot.pause()
            return requests

    assert anyio.run(scenario) == 2


def test_textual_transcript_loads_history_when_the_initial_page_fits() -> None:
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        requests = 0

        async def request_history_page() -> None:
            nonlocal requests
            requests += 1

        async with app_instance.run_test(size=(80, 24)) as pilot:
            renderer.render_history_entries(
                (HistoricalTranscriptMessage(role="assistant", content="current"),)
            )
            await pilot.pause()
            renderer.set_history_page_request_hook(request_history_page)
            renderer.history_page_loaded(has_more=True)
            await pilot.pause()
            await pilot.pause()
            return requests

    assert anyio.run(scenario) == 1


def test_textual_transcript_waits_for_layout_before_requesting_history() -> None:
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        requests = 0

        async def request_history_page() -> None:
            nonlocal requests
            requests += 1

        async with app_instance.run_test(size=(60, 12)) as pilot:
            renderer.render_history_entries(
                tuple(
                    HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                    for index in range(30)
                )
            )
            renderer.set_history_page_request_hook(request_history_page)
            renderer.history_page_loaded(has_more=True)
            await pilot.pause()
            await pilot.pause()
            return requests

    assert anyio.run(scenario) == 0


def test_textual_home_retries_failed_history_request_at_the_top() -> None:
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        requests = 0

        async def request_history_page() -> None:
            nonlocal requests
            requests += 1

        async with app_instance.run_test(size=(80, 24)) as pilot:
            renderer.render_history_entries(
                (HistoricalTranscriptMessage(role="assistant", content="current"),)
            )
            await pilot.pause()
            renderer.set_history_page_request_hook(request_history_page)
            renderer.history_page_loaded(has_more=True)
            await pilot.pause()
            await pilot.pause()
            assert requests == 1

            renderer.history_page_request_failed()
            app_instance.action_scroll_transcript_home()
            await pilot.pause()
            return requests

    assert anyio.run(scenario) == 2


def test_textual_history_page_prepend_preserves_viewport_and_session_marker() -> None:
    async def scenario() -> tuple[list[str], float, float, float, float, float, float, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            current = tuple(
                HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                for index in range(30)
            )
            older = tuple(
                HistoricalTranscriptMessage(role="user", content=f"older {index}")
                for index in range(12)
            )
            renderer.replace_history_entries(current, session_label="Paged session")
            await pilot.pause()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            transcript.scroll_to(y=8, animate=False)
            await pilot.pause()
            anchor = next(
                child
                for child in transcript.children
                if (isinstance(child, LineMessage) and "current 8" in child.render().plain)
                or (isinstance(child, StreamMessage) and "current 8" in child.source)
            )
            anchor_y_before = anchor.region.y
            scroll_y_before = transcript.scroll_y
            max_scroll_y_before = transcript.max_scroll_y

            renderer.prepend_history_entries(older)
            app_instance.write_assistant("concurrent tail output")
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()

            return (
                _transcript_texts(app_instance),
                scroll_y_before,
                transcript.scroll_y,
                max_scroll_y_before,
                transcript.max_scroll_y,
                anchor_y_before,
                anchor.region.y,
                transcript.is_following,
            )

    (
        texts,
        scroll_y_before,
        scroll_y_after,
        max_scroll_y_before,
        max_scroll_y_after,
        anchor_y_before,
        anchor_y_after,
        following,
    ) = anyio.run(scenario)
    assert "resumed session: Paged session" in texts[0]
    assert texts[1:13] == [f"older {index}" for index in range(12)]
    assert texts[13] == "current 0"
    assert texts[-1] == "concurrent tail output"
    assert scroll_y_after > scroll_y_before, (
        scroll_y_before,
        scroll_y_after,
        max_scroll_y_before,
        max_scroll_y_after,
        anchor_y_before,
        anchor_y_after,
    )
    assert abs(anchor_y_after - anchor_y_before) <= 1
    assert following is False


def test_textual_history_prepend_does_not_override_return_to_latest() -> None:
    async def scenario() -> tuple[bool, float, float]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            current = tuple(
                HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                for index in range(30)
            )
            older = tuple(
                HistoricalTranscriptMessage(role="user", content=f"older {index}")
                for index in range(12)
            )
            renderer.replace_history_entries(current, session_label="Paged session")
            await pilot.pause()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            transcript.scroll_to(y=8, animate=False)
            await pilot.pause()

            renderer.prepend_history_entries(older)
            transcript.return_to_latest()
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            return transcript.is_following, transcript.scroll_y, transcript.max_scroll_y

    following, scroll_y, max_scroll_y = anyio.run(scenario)
    assert following is True
    assert scroll_y >= max_scroll_y - 1


def test_textual_history_prepend_does_not_override_home_at_top() -> None:
    async def scenario() -> tuple[bool, float]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            current = tuple(
                HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                for index in range(30)
            )
            older = tuple(
                HistoricalTranscriptMessage(role="user", content=f"older {index}")
                for index in range(12)
            )
            renderer.replace_history_entries(current, session_label="Paged session")
            await pilot.pause()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            transcript.scroll_home(animate=False)
            await pilot.pause()

            renderer.prepend_history_entries(older)
            app_instance.action_scroll_transcript_home()
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            return transcript.is_following, transcript.scroll_y

    following, scroll_y = anyio.run(scenario)
    assert following is False
    assert scroll_y == 0


def test_textual_history_window_shifts_without_evicting_live_output() -> None:
    async def scenario() -> tuple[list[str], list[str], int, int, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            current = tuple(
                HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                for index in range(TUI_TRANSCRIPT_WINDOW_SIZE)
            )
            older = tuple(
                HistoricalTranscriptMessage(role="user", content=f"older {index}")
                for index in range(TUI_TRANSCRIPT_WINDOW_SHIFT)
            )
            renderer.replace_history_entries(current, session_label="Windowed session")
            await pilot.pause()
            await pilot.pause()
            renderer.prepend_history_entries(older)
            app_instance.write_assistant("live output")
            await pilot.pause()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            initial_count = sum(
                isinstance(child, LineMessage | StreamMessage) for child in transcript.children
            )

            # Normal wheel/PageUp edge navigation still shifts retained entries
            # after the durable page cursor has been exhausted.
            transcript.scroll_home(animate=False)
            await pilot.pause()
            await pilot.pause()
            older_count = sum(
                isinstance(child, LineMessage | StreamMessage) for child in transcript.children
            )
            older_texts = _transcript_texts(app_instance)

            app_instance.action_scroll_transcript_end()
            await pilot.pause()
            await pilot.pause()
            newest_count = sum(
                isinstance(child, LineMessage | StreamMessage) for child in transcript.children
            )
            return (
                _transcript_texts(app_instance),
                older_texts,
                initial_count,
                older_count,
                newest_count,
            )

    newest_texts, older_texts, initial_count, older_count, newest_count = anyio.run(scenario)
    assert "older 0" in older_texts
    assert "older 0" not in newest_texts
    assert f"current {TUI_TRANSCRIPT_WINDOW_SIZE - 1}" in newest_texts
    assert "live output" in newest_texts
    # Marker and one live line sit outside the bounded persisted-history window.
    assert initial_count == older_count == newest_count == TUI_TRANSCRIPT_WINDOW_SIZE + 2


def test_textual_history_window_shift_rearms_durable_paging() -> None:
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        requests = 0

        async def request_history_page() -> None:
            nonlocal requests
            requests += 1

        async with app_instance.run_test(size=(60, 12)) as pilot:
            renderer.replace_history_entries(
                tuple(
                    HistoricalTranscriptMessage(role="assistant", content=f"current {index}")
                    for index in range(TUI_TRANSCRIPT_WINDOW_SIZE)
                ),
                session_label="Windowed session",
            )
            renderer.prepend_history_entries(
                tuple(
                    HistoricalTranscriptMessage(role="user", content=f"older {index}")
                    for index in range(TUI_TRANSCRIPT_WINDOW_SHIFT)
                )
            )
            renderer.set_history_page_request_hook(request_history_page)
            renderer.history_page_loaded(has_more=True)
            await pilot.pause()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)

            transcript.scroll_home(animate=False)
            await pilot.pause()
            await pilot.pause()
            transcript.scroll_to(y=1, animate=False)
            await pilot.pause()
            transcript.scroll_home(animate=False)
            await pilot.pause()
            return requests

    assert anyio.run(scenario) == 1


def test_textual_streaming_keeps_the_growing_tail_visible() -> None:
    # Regression: an expanding streamed Markdown widget must stay pinned to the
    # bottom. The bug was measuring "near the bottom?" as the content grew — the
    # growth itself pushed the bottom away, so the check read False and stopped
    # following. The transcript must end scrolled to the newest output.
    async def scenario() -> tuple[float, float]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 20)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            body = "\n\n".join(f"Line {i} of the streamed answer." for i in range(15))
            for chunk in (body[i : i + 40] for i in range(0, len(body), 40)):
                renderer.token_delta(chunk)
                await pilot.pause()
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            return transcript.scroll_y, transcript.max_scroll_y

    scroll_y, max_scroll_y = anyio.run(scenario)
    assert max_scroll_y > 0  # content actually overflowed
    assert scroll_y == max_scroll_y  # pinned to the exact tail


def test_textual_streaming_keeps_wrapped_final_row_above_scrollbar() -> None:
    source = """## Files inspected

- `src/wisp/coding/session.py`
- `src/wisp/sessions/jsonl.py`
- `src/wisp/sessions/errors.py`
- `src/wisp/sessions/entries.py`
- `src/wisp/sessions/__init__.py`
- `src/wisp/sessions/replay.py`
- `src/wisp/rpc/execution.py`
- `src/wisp/rpc/coordinator.py`
- `src/wisp/cli/__init__.py`
- `tests/test_sessions.py`
- `tests/test_session_replay.py`
- `tests/test_session_branching.py`
- `tests/test_coding_session.py`
- `tests/test_rpc_execution.py`
- `tests/test_cli_rpc.py`
- `tests/test_sdk.py`

No files were changed and no tests were run in plan mode. GitHub freshness was not checked.
"""

    async def scenario() -> tuple[float, float, int, str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(84, 40)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.return_to_latest()
            await pilot.pause()
            renderer.token_delta(source[:-13])
            await pilot.pause()
            renderer.token_delta(source[-13:])
            renderer.end_token_stream_with_content(source)
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            stream = transcript.query_one(StreamMessage)
            return (
                transcript.scroll_y,
                transcript.max_scroll_y,
                transcript.max_scroll_x,
                stream.source,
                transcript.is_anchored,
            )

    scroll_y, max_scroll_y, max_scroll_x, rendered, anchored = anyio.run(scenario)
    assert scroll_y == max_scroll_y
    assert max_scroll_x == 0
    assert rendered.endswith("GitHub freshness was not checked.\n")
    assert not anchored  # native anchoring is scoped to active stream layout


def test_textual_streaming_keeps_a_large_many_block_reply_pinned_to_the_tail() -> None:
    # A large, many-block Markdown reply (headings + lists) must still end pinned to
    # the tail when a burst of provider deltas is coalesced into one native write.
    blocks: list[str] = []
    for i in range(80):
        blocks.append(f"## Section {i}")
        blocks.append(f"- point a {i}\n- point b {i}")
    body = "\n\n".join(blocks)

    async def scenario() -> tuple[float, float, str, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 20)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            for chunk in (body[i : i + 80] for i in range(0, len(body), 80)):
                renderer.token_delta(chunk)
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            with anyio.fail_after(2):
                while transcript.scroll_y < transcript.max_scroll_y:
                    await pilot.pause()
            stream = transcript.query_one(StreamMessage)
            return (
                transcript.scroll_y,
                transcript.max_scroll_y,
                stream.source,
                app_instance.last_stream_write_count,
            )

    scroll_y, max_scroll_y, rendered, write_count = anyio.run(scenario)
    assert rendered == body
    assert write_count == 1
    assert max_scroll_y > 100  # a genuinely large, overflowing reply
    assert scroll_y == max_scroll_y  # still pinned to the exact tail


def test_textual_streaming_does_not_yank_a_reader_who_scrolled_up() -> None:
    # The flip side of tail-follow: if the user scrolled up to read history, new
    # streamed output must NOT drag them back to the bottom.
    async def scenario() -> tuple[float, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            transcript.scroll_to(y=6, animate=False)  # user reads back
            await pilot.pause()
            for i in range(10):
                renderer.token_delta(f"new line {i}\n\n")
                await pilot.pause()
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            return transcript.scroll_y, transcript._follow

    scroll_y, follow = anyio.run(scenario)
    assert not follow  # scrolling away cleared the follow intent
    assert scroll_y <= 7  # stayed roughly where the user left it, not the bottom


def test_textual_streaming_releases_active_anchor_when_reader_scrolls_up() -> None:
    """Reader navigation wins even when final reconciliation grows the stream."""

    async def scenario() -> tuple[bool, bool, float, float, float]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.return_to_latest()
            await pilot.pause()

            renderer.token_delta("visible streamed prefix")
            await pilot.pause()
            anchored_before = transcript.is_anchored
            transcript.scroll_to(y=6, animate=False)
            await pilot.pause()
            anchored_after = transcript.is_anchored
            reader_y = transcript.scroll_y

            # The authoritative completion is much taller than the displayed
            # prefix. A stale native anchor would move the viewport to the new
            # tail during this replacement even though follow intent is off.
            final = "visible streamed prefix\n\n" + "final reconciliation row\n\n" * 40
            renderer.end_token_stream_with_content(final)
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            return (
                anchored_before,
                anchored_after,
                reader_y,
                transcript.scroll_y,
                transcript.max_scroll_y,
            )

    anchored_before, anchored_after, reader_y, final_y, max_y = anyio.run(scenario)
    assert anchored_before
    assert not anchored_after
    assert final_y == reader_y
    assert final_y < max_y


def test_textual_streaming_reconciles_deferred_output_after_returning_to_tail() -> None:
    async def scenario() -> tuple[str, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()

            renderer.token_delta("visible")
            await pilot.pause()
            transcript.scroll_to(y=6, animate=False)
            await pilot.pause()
            renderer.token_delta(" deferred")
            await pilot.pause()

            stream = next(
                child for child in transcript.children if isinstance(child, StreamMessage)
            )
            assert stream.source == "visible"
            transcript.return_to_latest()
            # Two distinct waits, in this order. The pause delivers the
            # FollowChanged message, whose handler is what re-queues the
            # deferred fragments; only then is there streaming work to wait on.
            # Waiting on idle first would return immediately, because nothing is
            # queued until that handler runs. The idle wait then replaces a
            # fixed pump count, so this test does not silently depend on
            # StreamBuffer's drain interval, which is a pacing choice rather
            # than part of the behavior under test.
            await pilot.pause()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            return stream.source, transcript.is_following

    content, following = anyio.run(scenario)
    assert content == "visible deferred"
    assert following is True


def test_textual_scrollback_counts_distinct_unseen_output_and_end_clears_it() -> None:
    async def scenario() -> dict[str, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            jump = app_instance.query_one("#jump-latest", JumpToLatest)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()

            # Multiple deltas reconcile the same StreamMessage, so this is one
            # unseen logical output rather than a token-counting notification.
            renderer.token_delta("first ")
            await pilot.pause()
            renderer.token_delta("second")
            await pilot.pause()
            stream_count = app_instance._transcript_controller.unseen_output_count
            stream_label = jump.render().plain

            # A new ToolCard is a second logical output; resolving that same card
            # in place must not increase the count again.
            renderer.event(ToolCallRequested(call_id="latest", name="read", arguments={}))
            await pilot.pause()
            tool_count = app_instance._transcript_controller.unseen_output_count
            renderer.event(
                ToolResultReady(call_id="latest", name="read", output="ok", is_error=False)
            )
            await pilot.pause()
            resolved_count = app_instance._transcript_controller.unseen_output_count
            resolved_label = jump.render().plain

            await pilot.press("end")
            await pilot.pause()
            return {
                "stream_count": stream_count,
                "stream_label": stream_label,
                "tool_count": tool_count,
                "resolved_count": resolved_count,
                "resolved_label": resolved_label,
                "cleared": app_instance._transcript_controller.unseen_output_count == 0,
                "hidden": jump.display is False,
                "following": transcript.is_following,
            }

    result = anyio.run(scenario)
    assert result["stream_count"] == 1
    assert result["stream_label"] == "↓ 1 new"
    assert result["tool_count"] == 2
    assert result["resolved_count"] == 2
    assert result["resolved_label"] == "↓ 2 new"
    assert result["cleared"]
    assert result["hidden"]
    assert result["following"]


def test_textual_heartbeat_notifies_scrolled_back_reader_and_clears_on_removal() -> None:
    # Regression (Codex P2): activity no longer lives in the footer, so the
    # jump-to-latest badge is the only cue that working/retry state began. A
    # scrolled-back reader must see the heartbeat register as unseen output, and
    # its transient removal must reconcile the badge (no phantom "1 new").
    async def scenario() -> dict[str, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            jump = app_instance.query_one("#jump-latest", JumpToLatest)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()
            assert transcript.is_following is False

            app_instance.show_working_indicator()
            await pilot.pause()
            working_count = app_instance._transcript_controller.unseen_output_count
            working_shown = jump.display is True

            # Retiring the heartbeat must drop it from the unseen set and hide the
            # badge, since it was the only unseen output.
            app_instance.hide_working_indicator()
            await pilot.pause()
            return {
                "working_count": working_count,
                "working_shown": working_shown,
                "cleared": app_instance._transcript_controller.unseen_output_count == 0,
                "hidden": jump.display is False,
            }

    result = anyio.run(scenario)
    assert result["working_count"] == 1
    assert result["working_shown"]
    assert result["cleared"]
    assert result["hidden"]


def test_textual_jump_to_latest_click_restores_follow_and_input_focus() -> None:
    async def scenario() -> dict[str, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            jump = app_instance.query_one("#jump-latest", JumpToLatest)
            input_widget = app_instance.query_one("#input", Input)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            await pilot._post_mouse_events(
                [events.MouseScrollUp],
                widget=transcript,
                times=3,
            )
            await pilot.pause()

            renderer.notice("new output")
            await pilot.pause()
            visible_before_click = jump.display
            overlay_row = app_instance.query_one("#jump-latest-row")
            before_overlay_wheel = transcript.scroll_y
            await pilot._post_mouse_events(
                [events.MouseScrollUp],
                widget=overlay_row,
                times=1,
            )
            await pilot.pause()
            overlay_wheel_forwarded = transcript.scroll_y < before_overlay_wheel
            clicked = await pilot.click("#jump-latest")
            await pilot.pause()
            return {
                "visible_before_click": visible_before_click,
                "overlay_wheel_forwarded": overlay_wheel_forwarded,
                "clicked": clicked,
                "hidden": jump.display is False,
                "following": transcript.is_following,
                "at_bottom": transcript.scroll_y >= transcript.max_scroll_y - 3,
                "focus_kept": app_instance.focused is input_widget,
            }

    result = anyio.run(scenario)
    assert result["visible_before_click"]
    assert result["overlay_wheel_forwarded"]
    assert result["clicked"]
    assert result["hidden"]
    assert result["following"]
    assert result["at_bottom"]
    assert result["focus_kept"]


def test_textual_jump_to_latest_preserves_approval_and_trust_focus() -> None:
    async def scenario(mode: str) -> tuple[bool, bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()
            renderer.notice("new output")
            await pilot.pause()

            if mode == "approval":
                renderer.approval_request(
                    ToolApprovalRequested(
                        call_id="latest",
                        name="bash",
                        arguments={"command": "echo ok"},
                        safety="command",
                    )
                )
            else:
                renderer.trust_request(
                    TrustRequested(request_id="trust-latest", project_path=Path("/tmp/project"))
                )
            await pilot.pause()

            input_widget = app_instance.query_one("#input", Input)
            options = app_instance.query_one("#decision-options", OptionList)
            focused_before = app_instance.focused is options
            clicked = await pilot.click("#jump-latest")
            await pilot.pause()
            return (
                clicked,
                focused_before,
                app_instance.focused is options and not input_widget.display,
            )

    approval = anyio.run(scenario, "approval")
    trust = anyio.run(scenario, "trust")
    assert approval == (True, True, True)
    assert trust == (True, True, True)


def test_textual_jump_to_latest_scrolls_transcript_not_decision_highlight() -> None:
    # Regression: action_scroll_transcript_end() was taught to move the
    # decision panel's highlight instead of scrolling the transcript while a
    # panel is open (so a real End keypress moves the highlight to "Deny"
    # rather than scrolling past it). But the jump-to-latest overlay is a
    # mouse affordance meaning "scroll the transcript to the bottom" — it is
    # never a decision-panel-navigation gesture, so clicking it while a panel
    # happens to be open must still scroll the transcript and clear the
    # unseen-output badge, and must NOT move the panel's highlighted option.
    async def scenario() -> tuple[bool, int | None]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()
            renderer.notice("new output")
            await pilot.pause()

            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="latest",
                    name="bash",
                    arguments={"command": "echo ok"},
                    safety="command",
                )
            )
            await pilot.pause()

            options = app_instance.query_one("#decision-options", OptionList)

            await pilot.click("#jump-latest")
            await pilot.pause()

            scrolled_to_bottom = transcript.scroll_y >= transcript.max_scroll_y - 1
            return scrolled_to_bottom, options.highlighted

    scrolled_to_bottom, highlighted_after = anyio.run(scenario)
    assert scrolled_to_bottom
    assert highlighted_after == 0  # unchanged from "Approve once" default


def test_textual_returning_to_the_bottom_resumes_following() -> None:
    # After scrolling up and back down, the reader is following again: the next
    # streamed output should pin to the tail once more.
    async def scenario() -> tuple[float, float]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            transcript.scroll_to(y=6, animate=False)  # scroll away...
            await pilot.pause()
            transcript.scroll_end(animate=False)  # ...then back to the bottom
            await pilot.pause()
            renderer.token_delta("resumed answer\n\n")
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()
            return transcript.scroll_y, transcript.max_scroll_y

    scroll_y, max_scroll_y = anyio.run(scenario)
    assert scroll_y >= max_scroll_y - 3  # following resumed


def test_textual_scrollback_keys_reach_transcript_and_compose_with_follow() -> None:
    # Stage 5 load-bearing test: with the Input focused (default), scrollback keys
    # must reach the transcript AND keep the follow flag correct. PageUp scrolls up
    # and clears follow; End returns to the bottom and restores it; a subsequent
    # stream then re-pins to the tail. Focus never leaves the Input.
    async def scenario() -> dict[str, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            input_widget = app_instance.query_one("#input", Input)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            start_y = transcript.scroll_y

            await pilot.press("pageup")
            await pilot.pause()
            after_pageup_y = transcript.scroll_y
            after_pageup_follow = transcript._follow
            focus_after_pageup = app_instance.focused is input_widget

            await pilot.press("end")
            await pilot.pause()
            after_end_y = transcript.scroll_y
            after_end_follow = transcript._follow

            renderer.token_delta("tail line\n\n")
            renderer.end_token_stream()
            await app_instance.wait_for_stream_idle()
            await pilot.pause()

            return {
                "scrolled_up": after_pageup_y < start_y,
                "follow_cleared": after_pageup_follow is False,
                "focus_kept": focus_after_pageup,
                "end_at_bottom": after_end_y >= transcript.max_scroll_y - 3,
                "follow_restored": after_end_follow is True,
                "stream_repinned": transcript.scroll_y >= transcript.max_scroll_y - 3,
            }

    r = anyio.run(scenario)
    assert r["scrolled_up"], "PageUp did not scroll the transcript"
    assert r["follow_cleared"], "scrolling up should clear the follow flag"
    assert r["focus_kept"], "scrollback must not steal focus from the Input"
    assert r["end_at_bottom"], "End did not return to the bottom"
    assert r["follow_restored"], "returning to the bottom should restore follow"
    assert r["stream_repinned"], "a stream after End should re-pin to the tail"


def test_textual_mouse_wheel_scrolls_transcript_and_updates_follow() -> None:
    # run_test bypasses terminal mouse-mode negotiation, so post the same wheel
    # events Textual's driver emits. This proves the Transcript consumes them,
    # preserves editor focus, and composes with its sticky follow-tail state.
    async def scenario() -> dict[str, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            input_widget = app_instance.query_one("#input", Input)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            start_y = transcript.scroll_y

            scrolled_up = await pilot._post_mouse_events(
                [events.MouseScrollUp],
                widget=transcript,
                times=3,
            )
            await pilot.pause()
            after_up_y = transcript.scroll_y
            after_up_follow = transcript._follow
            focus_after_up = app_instance.focused is input_widget

            scrolled_down = await pilot._post_mouse_events(
                [events.MouseScrollDown],
                widget=transcript,
                times=30,
            )
            await pilot.pause()
            return {
                "events_delivered": scrolled_up and scrolled_down,
                "scrolled_up": after_up_y < start_y,
                "follow_cleared": after_up_follow is False,
                "focus_kept": focus_after_up,
                "returned_to_bottom": transcript.scroll_y >= transcript.max_scroll_y - 3,
                "follow_restored": transcript._follow is True,
            }

    result = anyio.run(scenario)
    assert result["events_delivered"]
    assert result["scrolled_up"]
    assert result["follow_cleared"]
    assert result["focus_kept"]
    assert result["returned_to_bottom"]
    assert result["follow_restored"]


def test_textual_wheel_outside_transcript_keeps_following_tail() -> None:
    async def scenario() -> bool:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            input_widget = app_instance.query_one("#input", Input)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()

            await pilot._post_mouse_events(
                [events.MouseScrollUp],
                widget=input_widget,
                times=1,
            )
            await pilot.pause()
            return transcript.is_following

    assert anyio.run(scenario)


def test_textual_home_key_scrolls_transcript_over_input_cursor() -> None:
    # home is priority-bound to the transcript, so it jumps the transcript to the
    # top even while the Input has typed text — it does not move the input cursor.
    async def scenario() -> tuple[float, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            input_widget = app_instance.query_one("#input", Input)
            _fill_transcript(renderer, 30)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            await pilot.press(*"hello")  # type into the Input
            await pilot.press("home")
            await pilot.pause()
            return transcript.scroll_y, input_widget.value

    scroll_y, value = anyio.run(scenario)
    assert scroll_y == 0  # transcript jumped to the top
    assert value == "hello"  # input text untouched


def test_textual_plan_mode_hotkey_routes_through_input_queue() -> None:
    async def scenario() -> tuple[str, str]:
        app = TextualTui()
        app.action_toggle_agent_mode()
        plan = await app.read_prompt("wisp> ")
        app.set_status(TuiViewSnapshot(status="idle", input_hint="wisp> ", mode="plan"))
        app.action_toggle_agent_mode()
        build = await app.read_prompt("wisp> ")
        return plan, build

    assert anyio.run(scenario) == ("/plan", "/build")


def test_textual_scroll_actions_are_safe_before_mount() -> None:
    # The scroll actions are None-guarded, so invoking them before on_mount wires
    # the transcript is a no-op, not a crash.
    app = TextualTui()
    app.action_scroll_transcript_page_up()
    app.action_scroll_transcript_page_down()
    app.action_scroll_transcript_home()
    app.action_scroll_transcript_end()


def test_textual_tui_read_prompt_returns_submitted_input() -> None:
    async def scenario() -> str:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            async with anyio.create_task_group() as tg:
                results: list[str] = []

                async def read() -> None:
                    results.append(await app_instance.read_prompt("wisp> "))

                tg.start_soon(read)
                await pilot.pause()
                await pilot.click("#input")
                await pilot.press(*"hello", "enter")
            return results[0]

    assert anyio.run(scenario) == "hello"


def test_textual_multiline_paste_is_submitted_without_truncation() -> None:
    pasted = (
        "Use the bash tool to run exactly: printf 'approval panel dogfood\\n'. "
        "Do not use any\nother tools."
    )

    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            app_instance.post_message(events.Paste(pasted))
            await pilot.pause()
            editor_text = input_widget.value
            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._input_controller.receive_stream.receive()
            assert isinstance(submitted, str)
            return editor_text, submitted

    editor_text, submitted = anyio.run(scenario)
    assert editor_text == pasted
    assert submitted == pasted


def test_textual_large_paste_replaces_selection_and_expands_on_submit() -> None:
    pasted = "replacement\n" * 250

    async def scenario() -> tuple[str, str, int]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.value = "keep replace me tail"
            input_widget.selection = type(input_widget.selection)((0, 5), (0, 15))
            input_widget.focus()
            app_instance.post_message(events.Paste(pasted))
            await pilot.pause()
            editor_text = input_widget.value
            cursor_position = input_widget.cursor_position
            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._input_controller.receive_stream.receive()
            assert isinstance(submitted, str)
            return editor_text, submitted, cursor_position

    editor_text, submitted, cursor_position = anyio.run(scenario)
    assert editor_text.startswith("keep [Pasted content #1:")
    assert editor_text.endswith(" tail")
    assert "replace me" not in editor_text
    assert submitted == f"keep {pasted} tail"
    assert cursor_position == len(editor_text) - len(" tail")


def test_textual_large_paste_survives_placeholder_delete_then_restore() -> None:
    # Regression (Codex P2): a placeholder can vanish from an intermediate editor
    # state (delete) and return (undo / paste-back). The backing content must be
    # retained across that round-trip so Enter submits the real paste, not the
    # literal "[Pasted content #N: ...]" marker.
    pasted = "restore me\n" * 250

    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            app_instance.post_message(events.Paste(pasted))
            await pilot.pause()
            marker = input_widget.value
            assert marker.startswith("[Pasted content #1:")
            # Delete the marker (drops it from the current text), then restore the
            # same visible text — each assignment fires TextArea.Changed, the event
            # that used to prune the record mid-edit.
            input_widget.value = "typing something else"
            await pilot.pause()
            input_widget.value = marker
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._input_controller.receive_stream.receive()
            assert isinstance(submitted, str)
            return marker, submitted

    marker, submitted = anyio.run(scenario)
    assert submitted == pasted
    assert marker not in submitted


def test_textual_large_paste_survives_cut_and_move() -> None:
    # Regression (Codex P2): cutting the marker to move it elsewhere removes it
    # from the text for a beat before it's pasted back. The record must persist so
    # the relocated marker still expands to the full paste on submit.
    pasted = "move me\n" * 300

    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            app_instance.post_message(events.Paste(pasted))
            await pilot.pause()
            marker = input_widget.value
            assert marker.startswith("[Pasted content #1:")
            # Simulate cut (marker gone) then paste-back at a new position (marker
            # now trails after appended text) via successive Changed events.
            input_widget.value = "head "
            await pilot.pause()
            input_widget.value = f"head tail {marker}"
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._input_controller.receive_stream.receive()
            assert isinstance(submitted, str)
            return marker, submitted

    marker, submitted = anyio.run(scenario)
    assert submitted == f"head tail {pasted}"
    assert marker not in submitted


def test_textual_large_paste_echoes_compact_line_while_model_gets_full_text() -> None:
    # Regression (Codex P2): submitting a >2 KB paste must not mount the whole
    # blob into the transcript. The model still receives the full expanded text
    # via controller.prompt(prompt); the transcript echo (renderer.prompt_submitted)
    # keeps the compact "[Pasted content #N: ...]" marker.
    pasted = "echo me\n" * 400

    async def scenario() -> tuple[str, list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            app_instance.post_message(events.Paste(pasted))
            await pilot.pause()
            marker = input_widget.value
            assert marker.startswith("[Pasted content #1:")

            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._input_controller.receive_stream.receive()
            assert isinstance(submitted, str)
            # Drive the echo seam the shell uses: prompt_submitted(full_text).
            renderer.prompt_submitted(submitted)
            await pilot.pause()
            return submitted, _transcript_texts(app_instance)

    submitted, transcript = anyio.run(scenario)
    # Model side: the full expanded blob.
    assert submitted == pasted
    # Transcript side: exactly the compact marker (role label prefixes the line),
    # never the blob.
    user_lines = [line for line in transcript if "[Pasted content #1:" in line]
    assert len(user_lines) == 1
    assert not any("echo me\necho me" in line for line in transcript)


def test_textual_duplicate_large_paste_submissions_each_echo_compactly() -> None:
    # Regression (Codex P3): the compact-echo map is keyed by the expanded prompt
    # text. Two identical large pastes submitted before either is echoed (e.g.
    # duplicate queued follow-ups) must NOT collide — each keeps a compact echo,
    # consumed in submission order, or the second echoes the whole blob.
    async def scenario() -> tuple[str, str, str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            marker = "[Pasted content #1: 5,000 characters, 5.0 KB]"
            full = "duplicate blob " * 400
            # Two identical submissions register two echoes under the same key.
            app_instance.post_message(input_widget.Submitted(full, marker))
            app_instance.post_message(input_widget.Submitted(full, marker))
            await pilot.pause()
            # The shell echoes each in submission order via prompt_submitted(full).
            first = app_instance.compact_echo_for(full)
            second = app_instance.compact_echo_for(full)
            # A third identical prompt with no fresh paste echoes verbatim.
            third = app_instance.compact_echo_for(full)
            return marker, first, second, third

    marker, first, second, third = anyio.run(scenario)
    assert first == marker  # first echo is compact
    assert second == marker  # second (duplicate) is ALSO compact, not the blob
    assert third == "duplicate blob " * 400  # exhausted → falls back to full text


def test_textual_queue_drop_clears_pending_paste_echoes_but_bare_interrupt_does_not() -> None:
    # Regression (Codex P2 on the round-4 fix): an echo is registered on Enter but
    # consumed only when the prompt is echoed. Echoes must be reclaimed when the
    # shell actually DROPS its queued follow-ups (cancel/quit/input-closed/error)
    # — via clear_compact_echoes() / the queued_prompts_cleared renderer hook — so
    # an orphan can't mis-echo a later identical paste. But a bare Ctrl+C during an
    # approval only DENIES that decision; the queued follow-ups (and their echoes)
    # survive, so action_interrupt alone must NOT clear the echoes.
    async def scenario() -> tuple[int, int, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            full = "orphan blob " * 400
            marker = "[Pasted content #1: 4,800 characters, 4.7 KB]"
            app_instance.post_message(input_widget.Submitted(full, marker))
            await pilot.pause()

            # A bare interrupt (approval-deny shape) must PRESERVE queued echoes.
            app_instance.action_interrupt()
            await pilot.pause()
            after_bare_interrupt = app_instance._input_controller.compact_echo_key_count

            # The shell's real queue-drop hook reclaims them.
            app_instance.clear_compact_echoes()
            after_queue_drop = app_instance._input_controller.compact_echo_key_count

            # A later identical paste registers and echoes its OWN marker.
            fresh_marker = "[Pasted content #2: 4,800 characters, 4.7 KB]"
            app_instance.post_message(input_widget.Submitted(full, fresh_marker))
            await pilot.pause()
            echoed = app_instance.compact_echo_for(full)
            return after_bare_interrupt, after_queue_drop, echoed

    after_bare_interrupt, after_queue_drop, echoed = anyio.run(scenario)
    assert after_bare_interrupt == 1  # bare interrupt preserves queued echoes
    assert after_queue_drop == 0  # a real queue-drop reclaims them
    assert echoed == "[Pasted content #2: 4,800 characters, 4.7 KB]"  # not stale #1


def test_textual_pending_paste_echoes_are_bounded() -> None:
    # Regression (verification P2): echoes for prompts that are never echoed
    # (abandoned before consumption) must not accumulate without bound. Registering
    # far more than the cap evicts the oldest so the map stays bounded, and the
    # most-recent echoes are the ones that survive.
    async def scenario() -> tuple[int, int, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            overflow = _MAX_PENDING_ECHOES + 10
            for i in range(overflow):
                full = f"blob-{i} " * 400  # distinct >2 KB prompt each
                marker = f"[Pasted content #{i}: ...]"
                app_instance.post_message(input_widget.Submitted(full, marker))
            await pilot.pause()
            total = app_instance._input_controller.pending_compact_echo_count
            order_len = app_instance._input_controller.compact_echo_order_length
            # The oldest were evicted; the newest survives and still echoes compact.
            newest = app_instance.compact_echo_for(f"blob-{overflow - 1} " * 400)
            return total, order_len, newest

    total, order_len, newest = anyio.run(scenario)
    assert total <= _MAX_PENDING_ECHOES  # bounded, never grows past the cap
    assert order_len <= _MAX_PENDING_ECHOES
    assert newest == f"[Pasted content #{_MAX_PENDING_ECHOES + 9}: ...]"  # newest kept


def test_textual_newline_keys_edit_without_submitting() -> None:
    async def scenario() -> tuple[str, bool, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.press(*"first", "shift+enter", *"second", "ctrl+j", *"third")
            await pilot.pause()
            editor_text = input_widget.value
            try:
                app_instance._input_controller.receive_stream.receive_nowait()
            except anyio.WouldBlock:
                was_submitted = False
            else:
                was_submitted = True
            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._input_controller.receive_stream.receive()
            assert isinstance(submitted, str)
            return editor_text, was_submitted, submitted

    editor_text, submitted_early, submitted = anyio.run(scenario)
    assert editor_text == "first\nsecond\nthird"
    assert submitted_early is False
    assert submitted == editor_text


def test_textual_multiline_editor_grows_to_a_bounded_height() -> None:
    async def scenario() -> tuple[int, int, int, int]:
        app_instance = TextualTui()
        async with app_instance.run_test(size=(60, 20)) as pilot:
            input_widget = app_instance.query_one("#input", Input)
            transcript = app_instance.query_one("#transcript", Transcript)
            footer = app_instance.query_one("#status-bar")
            input_widget.value = "\n".join(f"line {index}" for index in range(12))
            await pilot.pause()
            return (
                input_widget.region.height,
                transcript.region.height,
                input_widget.region.y,
                footer.region.y,
            )

    editor_height, transcript_height, editor_y, footer_y = anyio.run(scenario)
    assert 1 < editor_height <= 8
    assert transcript_height > 0
    assert editor_y < footer_y


def test_textual_prefill_command_sets_input_without_submitting() -> None:
    # Tab-completion prefills the Input for the user to complete — it must NOT
    # submit, so a pending read stays pending.
    async def scenario() -> tuple[str, int, bool]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            async with anyio.create_task_group() as tg:
                results: list[str] = []

                async def read() -> None:
                    results.append(await app_instance.read_prompt("wisp> "))

                tg.start_soon(read)
                await pilot.pause()
                app_instance.prefill_command("/model ")
                await pilot.pause()
                input_widget = app_instance.query_one("#input", Input)
                value = input_widget.value
                cursor = input_widget.cursor_position
                submitted = bool(results)  # nothing should have been sent yet
                app_instance.submit_command_line("/quit")  # unblock the reader
            return value, cursor, submitted

    value, cursor, submitted = anyio.run(scenario)
    assert value == "/model "
    assert cursor == len("/model ")
    assert submitted is False


def test_slash_command_specs_are_valid_slash_commands() -> None:
    # Guard against drift: every command the inline menu offers must parse as a
    # real slash command. Arg-taking specs parse once a value is added.
    from wisp.tui.commands import SLASH_COMMAND_SPECS

    for spec in SLASH_COMMAND_SPECS:
        assert parse_tui_slash_command(spec.command) is not None, spec.command
    assert parse_tui_slash_command("/model gpt-5.5") is not None
    assert parse_tui_slash_command("/provider fake") is not None


def test_textual_leading_slash_is_typable_as_text() -> None:
    # THE BUG FIX: a message starting with "/" must be typable literally — the
    # input is never cleared or hijacked by either command surface.
    async def scenario() -> str:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            await pilot.press(*"/etc/hosts")
            await pilot.pause()
            return input_widget.value

    assert anyio.run(scenario) == "/etc/hosts"


def test_textual_framework_command_palette_is_disabled() -> None:
    # Wisp uses inline slash commands, so Textual's separate Ctrl+P palette remains off.
    assert TextualTui.ENABLE_COMMAND_PALETTE is False


def test_textual_slash_shows_inline_menu_and_filters() -> None:
    # "/" shows the full command list inline; typing filters it; a non-matching
    # token hides it. The input is never mutated.
    async def scenario() -> tuple[int, str | None, bool, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()
            all_count = suggest.option_count
            await pilot.press(*"mo")
            await pilot.pause()
            highlighted = suggest.highlighted_spec()
            await pilot.press(*"zzz")  # "/mozzz" matches nothing
            await pilot.pause()
            return (
                all_count,
                (highlighted.command if highlighted else None),
                suggest.is_open,
                input_widget.value,
            )

    all_count, highlighted, open_after_nomatch, value = anyio.run(scenario)
    assert all_count >= 5  # the full list showed on a bare "/"
    assert highlighted == "/model"  # "/mo" filtered to /model
    assert open_after_nomatch is False  # no match -> menu hidden
    assert value == "/mozzz"  # input untouched throughout


@pytest.mark.parametrize("size", [(120, 40), (100, 30), (80, 24), (72, 20), (40, 16)])
def test_textual_slash_suggest_stays_above_the_input_at_every_breakpoint(
    size: tuple[int, int],
) -> None:
    """Slash suggestions are constrained, non-overlapping, and non-reflowing."""

    async def scenario() -> tuple[
        int,
        int,
        int,
        tuple[int, int, int, int],
        tuple[int, int, int, int],
        str | None,
    ]:
        app_instance = TextualTui()
        async with app_instance.run_test(size=size) as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            before = (
                input_widget.region.x,
                input_widget.region.y,
                input_widget.region.width,
                input_widget.region.height,
            )
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()
            after = (
                input_widget.region.x,
                input_widget.region.y,
                input_widget.region.width,
                input_widget.region.height,
            )
            return (
                suggest.region.y,
                suggest.region.bottom,
                suggest.region.right,
                before,
                after,
                app_instance.focused.id if app_instance.focused else None,
            )

    menu_top, menu_bottom, menu_right, before, after, focused_id = anyio.run(scenario)
    assert menu_top >= 0
    assert menu_bottom <= after[1]  # #193: the menu must never cover the editor
    assert after == before  # opening suggestions must not reflow the composer
    assert menu_right <= size[0]
    assert focused_id == "input"


def test_textual_slash_suggest_does_not_cover_typing_or_backspace_at_compact_size() -> None:
    """The composer stays visible as a live slash query grows and shrinks."""

    async def scenario() -> tuple[
        tuple[int, int, int, int],
        list[tuple[str, int, int, int, tuple[int, int, int, int], str | None]],
    ]:
        app_instance = TextualTui()
        states: list[tuple[str, int, int, int, tuple[int, int, int, int], str | None]] = []
        async with app_instance.run_test(size=(40, 16)) as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            before = (
                input_widget.region.x,
                input_widget.region.y,
                input_widget.region.width,
                input_widget.region.height,
            )
            suggest = app_instance.query_one("#suggest", SlashSuggest)

            for key in ("/", "m", "backspace"):
                await pilot.press(key)
                await pilot.pause()
                states.append(
                    (
                        input_widget.value,
                        suggest.option_count,
                        suggest.region.bottom,
                        input_widget.region.y,
                        (
                            input_widget.region.x,
                            input_widget.region.y,
                            input_widget.region.width,
                            input_widget.region.height,
                        ),
                        app_instance.focused.id if app_instance.focused else None,
                    )
                )
        return before, states

    before, states = anyio.run(scenario)
    assert [value for value, *_ in states] == ["/", "/m", "/"]
    assert states[1][1] < states[0][1]  # typing filters the suggestions
    assert states[2][1] == states[0][1]  # Backspace restores them
    for _, _, menu_bottom, input_y, region, focused_id in states:
        assert menu_bottom <= input_y
        assert region == before
        assert focused_id == "input"


@pytest.mark.parametrize("size", [(120, 40), (100, 30), (80, 24), (72, 20)])
def test_textual_slash_suggest_fits_within_the_terminal_width(size: tuple[int, int]) -> None:
    # SlashSuggest.on_resize clamps max-width to the screen, so the menu
    # (border + padding included) never overflows the viewport even at the
    # 72-column breakpoint, where the unclamped 60-column ceiling would be
    # uncomfortably close to the full width.
    async def scenario() -> int:
        app_instance = TextualTui()
        async with app_instance.run_test(size=size) as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()
            return suggest.region.right

    menu_right = anyio.run(scenario)
    assert menu_right <= size[0]


def test_textual_slash_suggest_aligns_command_and_description_columns() -> None:
    # Descriptions of different-length commands (e.g. /help vs /provider)
    # must start at the same column, not a literal two-space join that drifts
    # per command length.
    from wisp.tui.commands import SLASH_COMMAND_SPECS

    async def scenario() -> list[str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()
            return [str(suggest.get_option_at_index(i).prompt) for i in range(suggest.option_count)]

    prompts = anyio.run(scenario)
    assert len(prompts) == len(SLASH_COMMAND_SPECS)
    name_width = max(len(spec.command) for spec in SLASH_COMMAND_SPECS)

    for spec, prompt in zip(SLASH_COMMAND_SPECS, prompts, strict=True):
        assert prompt == f"{spec.command:<{name_width}}  {spec.description}"


def test_textual_tab_completes_highlighted_command() -> None:
    # Tab fills the highlighted command: a trailing space for arg-taking commands
    # (so the user types the value), none for arg-less ones.
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            await pilot.press(*"/mo")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            model_value = input_widget.value
            input_widget.value = ""
            await pilot.pause()
            await pilot.press(*"/he")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            return model_value, input_widget.value

    model_value, help_value = anyio.run(scenario)
    assert model_value == "/model "  # arg-taking -> trailing space
    assert help_value == "/help"  # arg-less -> no space


def test_textual_menu_completes_compact_with_instruction_space() -> None:
    async def scenario() -> tuple[str | None, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.press(*"/co")
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            highlighted = suggest.highlighted_spec()
            await pilot.press("tab")
            await pilot.pause()
            return highlighted.command if highlighted else None, input_widget.value

    highlighted, value = anyio.run(scenario)
    assert highlighted == "/compact"
    assert value == "/compact "


def test_textual_menu_dismisses_on_escape_space_and_backspace() -> None:
    # Each dismissal path hides the menu and leaves the typed text intact.
    async def scenario() -> list[tuple[bool, str]]:
        app_instance = TextualTui()
        results: list[tuple[bool, str]] = []
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)

            async def dismiss(setup: str, *keys: str) -> None:
                input_widget.value = ""
                suggest.hide()
                await pilot.pause()
                await pilot.press(*setup)
                await pilot.pause()
                assert suggest.is_open, f"menu should be open after {setup!r}"
                await pilot.press(*keys)
                await pilot.pause()
                results.append((suggest.is_open, input_widget.value))

            await dismiss("/mo", "escape")  # Escape keeps "/mo"
            await dismiss("/model", "space")  # space -> "/model "
            await dismiss("/h", "backspace", "backspace")  # backspace past "/"
        return results

    (esc_open, esc_val), (space_open, space_val), (bs_open, bs_val) = anyio.run(scenario)
    assert esc_open is False and esc_val == "/mo"
    assert space_open is False and space_val == "/model "
    assert bs_open is False and bs_val == ""


def test_textual_enter_runs_completed_command_through_typed_path() -> None:
    # Enter runs the current line as-is through submit_command_line — the same path
    # as typing it by hand — and closes the menu.
    async def scenario() -> tuple[str, bool]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press(*"/he")
            await pilot.pause()
            await pilot.press("tab")  # -> /help
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return queued, suggest.is_open

    queued, menu_open = anyio.run(scenario)
    assert queued == "/help"
    assert menu_open is False


def test_textual_full_command_typed_letter_by_letter_submits_whole_line() -> None:
    # REGRESSION: the slash menu is a passive hint layer and must never take
    # keyboard focus from the editor. SlashSuggest inherits OptionList.can_focus,
    # which defaults to True; before it was forced off, opening the menu on "/"
    # could move focus onto the OptionList, so the rest of the command was dropped
    # and only "/" ever submitted. Type a full command letter-by-letter (no Tab
    # completion) with the menu open and assert the whole line submits and focus
    # never leaves #input.
    async def scenario() -> tuple[str, bool, bool, str | None]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press(*"/quit")  # menu is open the whole time
            await pilot.pause()
            menu_open = suggest.is_open
            focused_id = app_instance.focused.id if app_instance.focused else None
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return queued, suggest.can_focus, menu_open, focused_id

    queued, suggest_focusable, menu_open, focused_id = anyio.run(scenario)
    assert queued == "/quit"  # the WHOLE command, not a lone "/"
    assert suggest_focusable is False  # the menu can never be a keyboard target
    assert menu_open is True  # menu stayed open while typing (it's a live hint)
    assert focused_id == "input"  # focus never left the editor


async def _navigate_menu_to(pilot, suggest, command: str) -> None:
    """Press ``down`` until the menu highlights ``command`` (wraps a full cycle)."""
    for _ in range(suggest.option_count):
        spec = suggest.highlighted_spec()
        if spec is not None and spec.command == command:
            return
        await pilot.press("down")
        await pilot.pause()
    raise AssertionError(f"never highlighted {command!r}")


def test_textual_enter_runs_highlighted_argless_command_without_typing_it() -> None:
    # Type only "/", arrow-navigate to an arg-less command, press Enter -> that
    # command runs, even though the buffer only ever held "/". This is the
    # Claude-Code/Codex/Pi model: Enter accepts the highlight, it isn't a raw-line
    # submit. Before the fix, Enter submitted the literal "/".
    async def scenario() -> tuple[str, bool]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")  # open menu, nothing else typed
            await pilot.pause()
            await _navigate_menu_to(pilot, suggest, "/quit")
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return queued, suggest.is_open

    queued, menu_open = anyio.run(scenario)
    assert queued == "/quit"  # the HIGHLIGHTED command ran, not the "/" in the buffer
    assert menu_open is False  # accepting the highlight closes the menu


def test_textual_enter_on_partial_model_command_opens_model_picker() -> None:
    # REGRESSION: Enter executes the highlighted command, while Tab is reserved for
    # completing an argument-taking command with a trailing space. In particular,
    # `/mo` + Enter must submit bare `/model` so the shell opens the model picker.
    async def scenario() -> tuple[str, str, bool]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press(*"/mo")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return queued, input_widget.value, suggest.is_open

    queued, buffer_after, menu_open = anyio.run(scenario)
    assert queued == "/model"
    assert buffer_after == ""
    assert menu_open is False


def test_textual_enter_runs_fully_typed_optional_arg_command_bare() -> None:
    # REGRESSION: "takes_args" means the command *optionally* takes an argument —
    # bare `/model`, `/provider`, `/connect` are valid (show current / open pickers).
    # When the command name is already fully typed, Enter must run it as-is on the
    # first press, NOT prefill "/model " and demand a second Enter. Only a strict
    # prefix (still-typing suggestion) gets the fill-and-wait treatment.
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            await pilot.press(*"/model")  # fully typed; menu still open, highlighted
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return queued, input_widget.value

    queued, buffer_after = anyio.run(scenario)
    assert queued == "/model"  # ran bare on the FIRST Enter
    assert buffer_after == ""  # submitted and cleared, not left as "/model "


def test_textual_enter_accepts_fully_typed_command_case_insensitively() -> None:
    # The menu matches case-insensitively (query_from_value lowercases), so a
    # fully-typed "/MODEL" keeps "/model" highlighted. The accept check must match
    # that: "/MODEL" is a completed command, not a prefix still being typed, so the
    # first Enter runs it as the canonical "/model" instead of filling "/model ".
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            await pilot.press(*"/MODEL")  # uppercase, arg-taking
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return queued, input_widget.value

    queued, buffer_after = anyio.run(scenario)
    assert queued == "/model"  # ran the canonical spelling on the FIRST Enter
    assert buffer_after == ""  # not left as "/model " awaiting a second Enter


def test_textual_enter_executes_highlighted_optional_arg_command() -> None:
    # Enter consistently executes the highlighted command. Users who want to add
    # an optional argument use Tab, which preserves the trailing-space behavior.
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            input_widget.focus()
            await pilot.pause()
            await pilot.press("/")  # bare prefix; nothing else typed
            await pilot.pause()
            await _navigate_menu_to(pilot, suggest, "/auth")
            await pilot.press("enter")
            await pilot.pause()
            queued = await app_instance._input_controller.receive_stream.receive()
            return input_widget.value, queued

    assert anyio.run(scenario) == ("", "/auth")


def test_textual_disconnect_selection_opens_picker_command() -> None:
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            input_widget.focus()
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            await _navigate_menu_to(pilot, suggest, "/disconnect")
            await pilot.press("enter")
            await pilot.pause()
            submitted = await app_instance._input_controller.receive_stream.receive()
            return input_widget.value, submitted

    assert anyio.run(scenario) == ("", "/disconnect")


def test_textual_enter_on_fully_typed_command_runs_it_once() -> None:
    # A fully typed command with the menu still open (its own name highlighted)
    # runs exactly once on Enter — accepting the highlight yields the same command
    # as the buffer, so there's no double-submit.
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            await pilot.press(*"/quit")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            first = await app_instance._input_controller.receive_stream.receive()
            try:
                with anyio.fail_after(0.2):
                    second = await app_instance._input_controller.receive_stream.receive()
            except TimeoutError:
                second = "<none>"
            return first, second

    first, second = anyio.run(scenario)
    assert first == "/quit"
    assert second == "<none>"  # exactly one submission, not two


def test_textual_startup_shows_a_disposable_centered_empty_state() -> None:
    # The wordmark identifies an empty session without consuming permanent
    # scrollback, and disappears before the first real transcript item is
    # mounted.
    async def scenario() -> tuple[
        tuple[str, str, str, str],
        tuple[int, int, int, int, int],
        list[str],
        list[str],
    ]:
        app_instance, renderer = create_textual_tui()
        # Deliberately roomy. This test asserts the full panel — every tier
        # visible and the drawn mark at its full height — so it must sit well
        # clear of the boundary where tiers start being shed. At height 20 the
        # panel had only two spare rows, and CI's composer/footer took just
        # enough of them to hide the actions row and fail the centre check.
        async with app_instance.run_test(size=(72, 30)) as pilot:
            renderer.startup()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            empty = app_instance.query_one("#transcript-empty", TranscriptEmptyState)
            # Static, not Label: the wordmark is multi-line drawn lettering and
            # is swapped for a one-row badge on short terminals.
            wordmark = app_instance.query_one("#transcript-empty-wordmark", Static)
            tagline = app_instance.query_one("#transcript-empty-tagline", Label)
            hint = app_instance.query_one("#transcript-empty-hint", Label)
            actions = app_instance.query_one("#transcript-empty-actions", Static)
            # Only VISIBLE children: the panel hides its lower tiers when the
            # viewport is too short, and a hidden child reports zero width, so
            # including one would assert a centre of 0 against everything else.
            # The exact height at which that happens depends on how many rows
            # the composer and footer take, which is not identical across
            # environments — this test must not silently depend on it.
            centers = (transcript.region.x + transcript.region.width // 2,) + tuple(
                child.region.x + child.region.width // 2
                for child in (wordmark, tagline, hint, actions)
                if child.display
            )
            initial_children = [type(child).__name__ for child in transcript.children]
            wordmark_content = wordmark.render()
            tagline_content = tagline.render()
            hint_content = hint.render()
            actions_content = actions.render()
            assert isinstance(wordmark_content, Content)
            assert isinstance(tagline_content, Content)
            assert isinstance(hint_content, Content)
            assert isinstance(actions_content, Content)
            # Drawn letterforms are the mark: no frame, and every row the same
            # width so `text-align: center` cannot shear them apart.
            assert wordmark.region.height == 5
            assert not wordmark.styles.border_top[0]
            assert wordmark.styles.background.a == 0
            content = (
                wordmark_content.plain,
                tagline_content.plain,
                hint_content.plain,
                actions_content.plain,
            )

            renderer.prompt_submitted("hello")
            await pilot.pause()
            final_children = [type(child).__name__ for child in transcript.children]
            assert empty not in transcript.children
            return content, centers, initial_children, final_children

    content, centers, initial_children, final_children = anyio.run(scenario)
    wordmark, tagline, hint, actions = content
    wordmark_rows = wordmark.splitlines()
    assert len(wordmark_rows) == 5
    # A true rectangle. Ragged rows centre independently and the glyphs shear.
    assert len({len(row) for row in wordmark_rows}) == 1
    assert set("".join(wordmark_rows)) == {"█", " "}
    assert tagline == "A coding agent that stays in sync"
    assert hint == "Type a prompt or / for commands."
    assert actions == "/ commands  ·  /resume session"
    assert len(set(centers)) == 1
    assert initial_children == ["TranscriptEmptyState"]
    assert final_children == ["LineMessage"]


def test_textual_startup_empty_state_wordmark_centers_match_hint() -> None:
    # Regression: Textual centers these siblings as a block, not independently,
    # so every child is given the same explicit width. That width has to fit the
    # drawn wordmark, which is wider than any of the text lines.
    async def scenario() -> tuple[int, int]:
        app_instance, renderer = create_textual_tui()
        # Roomy for the same reason as the test above: the hint is hidden in the
        # sparsest tier, and a hidden child reports zero width.
        async with app_instance.run_test(size=(90, 30)) as pilot:
            renderer.startup()
            await pilot.pause()
            wordmark = app_instance.query_one("#transcript-empty-wordmark", Static)
            hint = app_instance.query_one("#transcript-empty-hint", Label)
            wordmark_center = wordmark.region.x + wordmark.region.width // 2
            hint_center = hint.region.x + hint.region.width // 2
            return wordmark_center, hint_center

    wordmark_center, hint_center = anyio.run(scenario)
    assert wordmark_center == hint_center


@pytest.mark.parametrize("height", [24, 18, 16, 14, 12, 10, 8, 6])
def test_textual_startup_empty_state_never_overflows_its_viewport(height: int) -> None:
    # Regression: the panel used to carry `min-height`, which pinned its
    # reported height above the real viewport on a short terminal. Its resize
    # breakpoints could therefore never observe the small sizes they exist to
    # handle, and the oversized panel overflowed the transcript — clipping the
    # wordmark mid-glyph instead of falling back to the one-row badge.
    async def scenario() -> tuple[bool, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(90, height)) as pilot:
            renderer.startup()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            wordmark = app_instance.query_one("#transcript-empty-wordmark", Static)
            # Measured against the TRANSCRIPT, not the panel: a floored panel
            # reports bounds larger than the viewport it sits in, so comparing
            # the mark against its own parent would compare it to the same
            # inflated number and never see the overflow.
            overflows = wordmark.region.bottom > transcript.region.bottom
            return overflows, wordmark.region.height

    overflows, wordmark_rows = anyio.run(scenario)

    assert not overflows
    # Either the full drawn mark or the single-row badge — never a partial one.
    assert wordmark_rows in {1, 5}


@pytest.mark.parametrize("width", [90, 40, 30, 26, 24, 20, 16, 12])
def test_textual_startup_wordmark_never_wraps_in_a_narrow_viewport(width: int) -> None:
    # The drawn mark needs its full cell width. Textual wraps the rows rather
    # than clipping them when the viewport is narrower, which both shears the
    # letterforms and doubles the rendered height — invalidating the five-row
    # assumption the height breakpoints are derived from. Sweeping only height
    # (as the test above does) cannot surface this.
    async def scenario() -> int:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(width, 30)) as pilot:
            renderer.startup()
            await pilot.pause()
            wordmark = app_instance.query_one("#transcript-empty-wordmark", Static)
            return wordmark.region.height

    # A wrapped five-row mark renders taller than five rows; the badge is one.
    assert anyio.run(scenario) in {1, 5}


@pytest.mark.parametrize("width", [12, 16, 20, 24, 30, 40, 60, 90])
@pytest.mark.parametrize("height", [10, 14, 16, 18, 20, 24, 30])
def test_textual_startup_empty_state_fits_every_viewport(width: int, height: int) -> None:
    # The visibility tiers are chosen by measuring each one's wrapped footprint,
    # not by comparing height against fixed thresholds. Constants cannot work
    # here: once the text rows wrap, their row count depends on the text, the
    # available width AND the tier, so a value correct at one width overflows at
    # another. Sweeping both axes together is the only way to see it — earlier
    # single-axis sweeps missed thirteen overflowing combinations.
    async def scenario() -> tuple[int, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(width, height)) as pilot:
            renderer.startup()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            empty = app_instance.query_one("#transcript-empty", TranscriptEmptyState)
            visible = [child for child in empty.children if child.display]
            content_bottom = max((child.region.bottom for child in visible), default=0)
            return content_bottom, transcript.region.bottom

    content_bottom, transcript_bottom = anyio.run(scenario)

    assert content_bottom <= transcript_bottom


@pytest.mark.parametrize("width", [90, 40, 30, 24, 20, 16])
def test_textual_startup_text_wraps_instead_of_truncating_when_narrow(width: int) -> None:
    # The children share one explicit width so Textual's block-centering keeps
    # them aligned. That width used to be fixed at construction, so on a viewport
    # narrower than the block the tagline and hint were clipped mid-word rather
    # than wrapping. Both the shared width and the rows' `height: auto` are
    # needed: clamping alone still truncates while the rows are pinned to one.
    async def scenario() -> tuple[int, int, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(width, 30)) as pilot:
            renderer.startup()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            tagline = app_instance.query_one("#transcript-empty-tagline", Label)
            return transcript.size.width, tagline.region.width, tagline.region.height

    transcript_width, tagline_width, tagline_rows = anyio.run(scenario)

    # Never wider than the space available, and never clipped to a single row
    # when the text cannot fit one.
    assert tagline_width <= transcript_width
    expected_rows = -(-len(_EMPTY_TRANSCRIPT_TAGLINE) // max(1, tagline_width))
    assert tagline_rows >= min(expected_rows, 1)
    assert tagline_rows * tagline_width >= len(_EMPTY_TRANSCRIPT_TAGLINE)


def test_textual_composer_focus_styles_resolve_for_both_themes() -> None:
    # The editor+status frame now lives on the shared #composer panel, not on
    # #input directly (see test_textual_composer_frames_input_and_status_as_
    # one_panel) — #input itself has no border or background of its own, so
    # the idle-vs-focused visual state this test guards moved to #composer's
    # `border`/`background` (round border, not the old underline-only
    # `border_bottom`), driven by the same `:focus-within` a child gaining
    # focus triggers.
    async def scenario() -> dict[str, tuple[str, str, str, str, float]]:
        styles: dict[str, tuple[str, str, str, str, float]] = {}
        for theme in ("wisp", "wisp-light"):
            app_instance = TextualTui()
            async with app_instance.run_test() as pilot:
                app_instance.theme = theme
                input_widget = app_instance.query_one("#input", Input)
                composer = app_instance.query_one("#composer")
                app_instance.screen.set_focus(None)
                await pilot.pause(0.25)
                idle_background = composer.styles.background.hex
                idle_border = composer.styles.border_top[1].hex

                input_widget.focus()
                await pilot.pause(0.25)
                transition = composer.styles.transitions["border"]
                styles[theme] = (
                    idle_background,
                    composer.styles.background.hex,
                    idle_border,
                    composer.styles.border_top[1].hex,
                    transition.duration,
                )
        return styles

    styles = anyio.run(scenario)
    expected = {
        "wisp": ("#0E1216", "#3FB8B8"),
        "wisp-light": ("#FBFCFD", "#2E7676"),
    }
    for theme, (
        idle_background,
        focused_background,
        idle_border,
        focused_border,
        delay,
    ) in styles.items():
        expected_background, expected_accent = expected[theme]
        assert idle_background == expected_background
        assert focused_background == expected_background
        assert idle_border != focused_border
        assert focused_border == expected_accent
        assert delay == 0.2


def test_textual_transcript_hides_scrollbar_chrome_without_disabling_scrolling() -> None:
    async def scenario() -> tuple[str, int, tuple[int, int], bool, float, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 20)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 40)
            await pilot.pause()
            return (
                transcript.styles.scrollbar_visibility,
                transcript.styles.scrollbar_size_vertical,
                transcript.scrollbars_space,
                transcript.scrollbars_enabled[0],
                transcript.max_scroll_y,
                transcript.scrollable_content_region.width == transcript.content_region.width,
            )

    visibility, width, space, enabled, max_y, full_content_width = anyio.run(scenario)
    assert visibility == "hidden"
    assert width == 0
    assert space == (0, 0)
    assert enabled
    assert max_y > 0
    assert full_content_width


def test_textual_input_is_pinned_to_the_bottom() -> None:
    # Regression: a wrapping Container defaulted to height:1fr and floated the
    # input into the middle. The transcript should own the free space (1fr) while
    # the composer (bordered editor + status panel) plus the keybinding hint row
    # below it hug the bottom rows. The composer is taller than the old
    # borderless input (border top/bottom rules plus an internal status divider
    # add rows) and the hint row adds one more below it, so the margin from the
    # bottom is wider than before — the input itself must still sit in the
    # composer's top row, not floated into the middle of the screen.
    async def scenario() -> tuple[int, int, int, int]:
        app_instance = TextualTui()
        async with app_instance.run_test(size=(74, 24)) as pilot:
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            transcript = app_instance.query_one("#transcript", Transcript)
            hint = app_instance.query_one("#keybinding-hint")
            return (
                app_instance.size.height,
                input_widget.region.y,
                transcript.region.height,
                hint.region.bottom,
            )

    screen_h, input_top, transcript_h, hint_bottom = anyio.run(scenario)
    # The composer (and the input inside it) sits in the last several rows; the
    # transcript fills most of the height above it; the hint row is the very
    # last thing on screen.
    assert input_top >= screen_h - 7
    assert hint_bottom == screen_h
    assert transcript_h >= screen_h // 2


def test_textual_keybinding_hint_lists_only_real_bindings() -> None:
    # Persistent, low-contrast reminder below the composer — must only name
    # affordances that actually exist (all currently show=False, i.e. hidden
    # from Textual's own footer/help): `/` for the slash-command menu, Enter/
    # Space to expand a focused ToolCard, Escape to leave a card back to the
    # input. Not folded into format_tui_footer_lines (shared with line/
    # fullscreen renderers) — this is Textual-only chrome.
    async def scenario() -> tuple[str, bool]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            hint = app_instance.query_one("#keybinding-hint", Static)
            content = hint.render()
            assert isinstance(content, Content)
            return content.plain, hint.display

    text, displayed = anyio.run(scenario)
    assert "/ commands" in text
    assert "shift+enter/ctrl+j newline" in text
    assert "esc cancel" in text
    assert "ctrl+c×2 quit" in text
    assert displayed


def test_textual_input_placeholder_uses_the_prompt_glyph() -> None:
    # The underline-only input leads with a `❯` glyph, not the verbose `wisp>`
    # chrome. The shared semantic hint (wisp> / wisp(running)>) maps to the same
    # quiet glyph because command activity remains visible in the transcript.
    async def scenario() -> tuple[str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            idle_placeholder = input_widget.placeholder
            renderer.view_updated(TuiViewSnapshot(status="running", input_hint="wisp(running)> "))
            await pilot.pause()
            return idle_placeholder, input_widget.placeholder

    idle_placeholder, running_placeholder = anyio.run(scenario)
    assert idle_placeholder == "❯ "
    assert running_placeholder == "❯ "


def test_textual_composer_frames_input_and_status_as_one_panel() -> None:
    # The editor and status line are framed together as one bordered #composer
    # panel (input above, status below, no divider between them save the
    # status bar's own top rule) rather than a borderless underline input
    # floating over a separately-backgrounded status bar. #input itself carries
    # no border of its own — the frame belongs to the shared parent, not
    # duplicated on the child — so this asserts that split explicitly, guarding
    # against a regression back to either the old per-widget underline or an
    # accidental double border.
    async def scenario() -> tuple[object, object]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            composer = app_instance.query_one("#composer")
            return input_widget.styles.border, composer.styles.border

    input_border, composer_border = anyio.run(scenario)
    # Textual's Edges exposes each side as an (edge_type, color) tuple; "" means
    # no rule on that side.
    assert input_border.top[0] == ""
    assert input_border.left[0] == ""
    assert input_border.right[0] == ""
    assert input_border.bottom[0] == ""
    assert composer_border.top[0] == "round"
    assert composer_border.bottom[0] == "round"
    assert composer_border.left[0] == "round"
    assert composer_border.right[0] == "round"


def test_textual_composer_frame_clicks_focus_prompt_editor() -> None:
    async def scenario() -> list[bool]:
        app_instance = TextualTui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            composer = app_instance.query_one("#composer")
            width = composer.region.width
            height = composer.region.height
            focused: list[bool] = []
            for offset in (
                (width // 2, 0),
                (width // 2, height - 1),
                (0, height // 2),
                (width - 1, height // 2),
            ):
                app_instance.screen.set_focus(None)
                assert await pilot.click("#composer", offset=offset)
                focused.append(input_widget.has_focus)
            return focused

    assert anyio.run(scenario) == [True, True, True, True]


def test_textual_wheel_uses_one_row_sensitivity() -> None:
    transcript = Transcript()

    assert transcript.scroll_sensitivity_y == 1.0


def test_textual_run_shell_enables_mouse_for_wheel_scrolling() -> None:
    # The real terminal must enter mouse-reporting mode; widget-level wheel tests
    # pass headlessly even when run_shell accidentally disables terminal events.
    captured: dict[str, object] = {}

    async def scenario() -> None:
        app_instance = TextualTui()

        async def fake_run_async(*args: object, **kwargs: object) -> None:
            captured["mouse"] = kwargs.get("mouse")

        app_instance.run_async = fake_run_async  # type: ignore[method-assign]

        async def runner() -> None:
            return None

        await app_instance.run_shell(runner)

    anyio.run(scenario)
    assert captured["mouse"] is True


def test_textual_header_is_removed_without_losing_application_metadata() -> None:
    async def scenario() -> tuple[str, str, int]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            return app_instance.title, app_instance.sub_title, len(list(app_instance.query(Header)))

    title, sub_title, header_count = anyio.run(scenario)
    assert title == "wisp"
    assert sub_title == ""
    assert header_count == 0


@pytest.mark.parametrize("size", [(120, 40), (100, 30), (80, 24), (72, 20)])
@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_textual_no_duplicate_identity_at_any_breakpoint(size: tuple[int, int], theme: str) -> None:
    # Issue #72: the disposable welcome treatment is gone once the transcript
    # has content, and no permanent Header duplicates it at any breakpoint.
    async def scenario() -> tuple[str, str, bool, bool, int]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=size) as pilot:
            app_instance.theme = theme
            await pilot.pause()
            empty_state_present_initially = bool(list(app_instance.query(TranscriptEmptyState)))

            renderer.prompt_submitted("hello")
            await pilot.pause()
            empty_state_present_after_prompt = bool(list(app_instance.query(TranscriptEmptyState)))

            return (
                app_instance.title,
                app_instance.sub_title,
                empty_state_present_initially,
                empty_state_present_after_prompt,
                len(list(app_instance.query(Header))),
            )

    title, sub_title, empty_before, empty_after, header_count = anyio.run(scenario)
    assert title == "wisp"
    assert sub_title == ""
    assert empty_before is True
    assert empty_after is False
    assert header_count == 0


def _read_prompt_signal_for_key(key: str) -> type[BaseException] | None:
    # Press a real key (through the focused Input) and report what read_prompt
    # raises. Guards the priority bindings: without priority=True the Input
    # swallows ctrl+d and this hangs.
    async def scenario() -> type[BaseException] | None:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            captured: list[BaseException] = []

            async with anyio.create_task_group() as tg:

                async def read() -> None:
                    try:
                        await app_instance.read_prompt("wisp> ")
                    except BaseException as exc:  # noqa: BLE001 - assert on type below
                        captured.append(exc)

                tg.start_soon(read)
                await pilot.pause()
                await pilot.press(key)
                await pilot.pause()
            return type(captured[0]) if captured else None

    return anyio.run(scenario)


def test_textual_tui_ctrl_c_emits_quit_press() -> None:
    from wisp.tui.state import TuiQuitRequested

    assert _read_prompt_signal_for_key("ctrl+c") is TuiQuitRequested


def test_textual_tui_ctrl_d_closes_read_prompt() -> None:
    # ctrl+d must reach the app binding even though the Input widget is focused.
    assert _read_prompt_signal_for_key("ctrl+d") is EOFError


def test_textual_tui_ctrl_d_deletes_right_without_closing_nonempty_draft() -> None:
    async def scenario() -> tuple[str, bool]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            editor = app_instance.query_one("#input", Input)
            editor.value = "ab"
            editor.cursor_position = 1
            read_task = asyncio.create_task(app_instance.read_prompt("wisp> "))
            await pilot.pause()

            await pilot.press("ctrl+d")
            await pilot.pause()
            result = (editor.value, read_task.done())
            read_task.cancel()
            with suppress(asyncio.CancelledError):
                await read_task
            return result

    assert anyio.run(scenario) == ("a", False)


def test_textual_tui_escape_emits_cancel_without_clearing_draft() -> None:
    async def scenario() -> tuple[type[BaseException] | None, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            editor = app_instance.query_one("#input", Input)
            editor.value = "keep draft"
            captured: list[BaseException] = []

            async def read() -> None:
                try:
                    await app_instance.read_prompt("wisp> ")
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    captured.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(read)
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
            return (type(captured[0]) if captured else None, editor.value)

    assert anyio.run(scenario) == (TuiCancelRequested, "keep draft")


def test_textual_renderer_captures_mode_at_submit_time() -> None:
    # An approval that arrives after read_prompt() begins waiting must be the
    # mode the shell reconciles against; otherwise the user's "y" is tagged as
    # a running follow-up and queued instead of resolving the approval.
    _, renderer = create_textual_tui()
    renderer.view_updated(
        TuiViewSnapshot(
            status="waiting for approval",
            input_hint="approve? [y/N] ",
            input_mode="approval",
        )
    )

    # Submit fires while approval mode is visible; the shell then advances the
    # view to running as it processes the answer.
    renderer._capture_submitted_input_mode()
    renderer.view_updated(
        TuiViewSnapshot(status="running", input_hint="wisp(running)> ", input_mode="running")
    )

    assert renderer.consume_submitted_input_mode("running") == "approval"
    # The captured mode is single-use; a later read with no fresh submit falls
    # back to the shell-provided mode.
    assert renderer.consume_submitted_input_mode("running") == "running"


def test_textual_tui_submit_captures_visible_mode_via_hook() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve? [y/N] ",
                    input_mode="approval",
                )
            )
            async with anyio.create_task_group() as tg:

                async def read() -> None:
                    await app_instance.read_prompt("approve? [y/N] ")

                tg.start_soon(read)
                await pilot.pause()
                await pilot.click("#input")
                await pilot.press("y", "enter")
        return renderer.consume_submitted_input_mode("running")

    assert anyio.run(scenario) == "approval"


def test_textual_tui_ctrl_c_clears_partial_input() -> None:
    # A partially typed line must not survive an interrupt; otherwise it would be
    # resubmitted on the next Enter after the shell has already handled Ctrl-C.
    async def scenario() -> str:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            async with anyio.create_task_group() as tg:

                async def read() -> None:
                    from wisp.tui.state import TuiQuitRequested

                    try:
                        await app_instance.read_prompt("wisp> ")
                    except TuiQuitRequested:
                        pass

                tg.start_soon(read)
                await pilot.pause()
                await pilot.click("#input")
                await pilot.press(*"cancel this")
                await pilot.press("ctrl+c")
                await pilot.pause()
            return input_widget.value

    assert anyio.run(scenario) == ""


def test_textual_ctrl_c_interrupts_approval_despite_stale_editor_selection() -> None:
    # Regression (Codex P2): a decision panel hides the composer, but a draft
    # selection can linger in the hidden editor. Ctrl+C must still interrupt/deny
    # the active approval — the editor only owns the copy while it's visible and
    # focused, so a stale selection behind the panel can't swallow the interrupt.
    async def scenario() -> type[BaseException] | None:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.value = "stale draft selection"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 21))
            await pilot.pause()
            assert input_widget.selected_text  # a selection really is present

            captured: list[BaseException] = []
            async with anyio.create_task_group() as tg:

                async def read() -> None:
                    try:
                        await app_instance.read_prompt("approve? [y/N] ")
                    except BaseException as exc:  # noqa: BLE001 - assert on type below
                        captured.append(exc)

                tg.start_soon(read)
                await pilot.pause()
                # Open the approval panel: this hides the composer (display=False)
                # while the stale selection remains on the editor.
                renderer.approval_request(
                    ToolApprovalRequested(
                        call_id="latest",
                        name="bash",
                        arguments={"command": "echo ok"},
                        safety="command",
                    )
                )
                await pilot.pause()
                assert input_widget.display is False
                app_instance.action_interrupt()
                await pilot.pause()
            return type(captured[0]) if captured else None

    assert anyio.run(scenario) is TuiQuitRequested


def test_textual_ctrl_c_copies_when_editor_is_focused_with_selection() -> None:
    # Complement to the approval regression: when the editor IS visible and
    # focused with a selection, Ctrl+C keeps its "copy, don't quit" behavior.
    async def scenario() -> bool:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.value = "copy me"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 7))
            input_widget.focus()
            await pilot.pause()

            interrupted = False
            async with anyio.create_task_group() as tg:

                async def read() -> None:
                    nonlocal interrupted
                    try:
                        await app_instance.read_prompt("wisp> ")
                    except TuiQuitRequested:
                        interrupted = True

                tg.start_soon(read)
                await pilot.pause()
                app_instance.action_interrupt()
                await pilot.pause()
                # No quit press was queued, so the read is still pending — cancel it
                # so the task group can exit.
                tg.cancel_scope.cancel()
            return interrupted

    assert anyio.run(scenario) is False


def test_textual_ctrl_c_copy_failure_does_not_fire_quit() -> None:
    # Regression (verification P1): the copy-branch return must sit OUTSIDE the
    # suppress block. If copy_to_clipboard raises (e.g. broken terminal OSC52
    # write), an editor-owned ctrl+c copy gesture must NOT fall through to a
    # quit press — that would arm quit and wipe the draft line.
    async def scenario() -> tuple[bool, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.value = "keep this draft"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 4))
            input_widget.focus()
            await pilot.pause()

            def boom(_text: str) -> None:
                raise RuntimeError("clipboard write failed")

            app_instance.copy_to_clipboard = boom  # type: ignore[method-assign]

            interrupted = False
            async with anyio.create_task_group() as tg:

                async def read() -> None:
                    nonlocal interrupted
                    try:
                        await app_instance.read_prompt("wisp> ")
                    except TuiQuitRequested:
                        interrupted = True

                tg.start_soon(read)
                await pilot.pause()
                app_instance.action_interrupt()
                await pilot.pause()
                draft = input_widget.value
                tg.cancel_scope.cancel()
            return interrupted, draft

    interrupted, draft = anyio.run(scenario)
    assert interrupted is False  # a failed copy never becomes an interrupt
    assert draft == "keep this draft"  # and never wipes the draft line


def test_textual_transcript_autocopy_ignores_hidden_editor_selection() -> None:
    # Regression (Codex P3): dragging visible transcript/decision text must
    # auto-copy even when a stale draft selection lingers in a HIDDEN editor
    # (e.g. behind an approval panel). Ownership is gated on the editor being
    # visible and focused, so a hidden selection no longer blocks the copy.
    async def scenario() -> tuple[list[str], list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.value = "stale hidden selection"
            input_widget.selection = type(input_widget.selection)((0, 0), (0, 22))
            await pilot.pause()

            copied: list[str] = []
            app_instance.copy_to_clipboard = copied.append  # type: ignore[method-assign]
            app_instance.screen.get_selected_text = lambda: "transcript text"  # type: ignore[method-assign]

            # Editor visible + focused: it owns the selection, so transcript
            # auto-copy defers to the editor and copies nothing.
            input_widget.focus()
            await pilot.pause()
            app_instance.on_text_selected()
            focused_copies = list(copied)

            # Open an approval panel: the composer is hidden (display=False) while
            # the stale selection remains. Auto-copy must now fire for the drag.
            copied.clear()
            renderer.approval_request(
                ToolApprovalRequested(
                    call_id="latest",
                    name="bash",
                    arguments={"command": "echo ok"},
                    safety="command",
                )
            )
            await pilot.pause()
            assert input_widget.display is False
            app_instance.on_text_selected()
            hidden_copies = list(copied)
            return focused_copies, hidden_copies

    focused_copies, hidden_copies = anyio.run(scenario)
    assert focused_copies == []  # focused editor owns the selection
    assert hidden_copies == ["transcript text"]  # hidden editor doesn't block


def test_textual_transcript_autocopy_skips_while_streaming() -> None:
    # Regression (Codex P3): on_text_selected promised (in its own comment) not to
    # auto-copy while the agent streams — the transcript mutates and Textual's
    # selection bounds go stale — but never enforced it. A selection during a live
    # response must NOT overwrite the clipboard; it may only copy once the stream
    # has flushed.
    async def scenario() -> tuple[list[str], list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            copied: list[str] = []
            app_instance.copy_to_clipboard = copied.append  # type: ignore[method-assign]
            app_instance.screen.get_selected_text = lambda: "streamed line"  # type: ignore[method-assign]

            # Start a stream: append_stream mounts the stream widget (is_streaming).
            app_instance.append_stream("partial response ")
            await pilot.pause()
            assert app_instance._is_streaming()
            app_instance.on_text_selected()
            during_stream = list(copied)

            # Flush the stream: the guard lifts and a later selection copies again.
            app_instance.flush_stream()
            await app_instance.wait_for_stream_idle()
            assert not app_instance._is_streaming()
            app_instance.on_text_selected()
            after_flush = list(copied)
            return during_stream, after_flush

    during_stream, after_flush = anyio.run(scenario)
    assert during_stream == []  # streaming suppresses the stale-bounds auto-copy
    assert after_flush == ["streamed line"]  # after flush, auto-copy resumes


def test_cli_tui_mode_invokes_tui_runner(tmp_path: Path, monkeypatch: object) -> None:
    captured: list[TuiOptions] = []

    async def fake_run_tui(options: TuiOptions) -> None:
        captured.append(options)

    monkeypatch.setattr(tui_module, "run_tui", fake_run_tui)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--mode",
            "tui",
            "--provider",
            "fake",
            "--session-dir",
            str(tmp_path),
            "--continue",
            "--tui-renderer",
            "fullscreen",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].config.provider == "fake"
    assert captured[0].config.session_dir == tmp_path
    assert captured[0].continue_latest is True
    assert captured[0].renderer is TuiRendererKind.fullscreen
