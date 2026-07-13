# ruff: noqa: F403,F405

from __future__ import annotations

import os

from pytest import MonkeyPatch
from textual import events
from textual.await_complete import AwaitComplete
from textual.content import Content
from textual.widgets import OptionList, Static

import wisp.cli as cli_module
from tests.tui_support import *
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    MessageStarted,
    ProviderRetrying,
    RpcCommandStarted,
    TurnStarted,
)
from wisp.trust_flow import TrustDecision
from wisp.tui.commands import parse_tui_slash_command
from wisp.tui.compact_echo import MAX_PENDING_ECHOES as _MAX_PENDING_ECHOES
from wisp.tui.textual_app import (
    TextualTui,
    TextualTuiRenderer,
    create_textual_tui,
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


def _transcript_texts(app: TextualTui) -> list[str]:
    """Plain text of every mounted transcript message (line + streamed)."""

    transcript = app.query_one("#transcript", Transcript)
    texts: list[str] = []
    for child in transcript.children:
        if isinstance(child, LineMessage | ToolCard):
            texts.append(child.render().plain)  # Textual Content
        elif isinstance(child, StreamMessage):
            texts.append(child._markdown.source)
    return texts


def _working_activity(app: TextualTui) -> str:
    """Plain transcript heartbeat text, or empty when activity is hidden."""

    indicator = app._working_indicator
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
    assert "Wisp: a Python, Pi-inspired coding agent." in result.output


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
        async with app_instance.run_test() as pilot:
            app_instance.append_stream("code has [brackets] and [/close] tags")
            app_instance.flush_stream()
            await pilot.pause()
            # Streamed assistant text renders as Markdown; bracketed text must
            # survive intact (Markdown source is not Rich-markup-interpreted).
            return "\n".join(_transcript_texts(app_instance))

    rendered = anyio.run(scenario)
    assert "[brackets]" in rendered
    assert "[/close]" in rendered


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

    assert "assistant: hello there" in rendered
    # One card for c1: done glyph + name + the bounded multiline output preview.
    assert "✓ bash" in rendered
    assert "file-a" in rendered
    assert "file-b" in rendered
    # The denied card carries the reason.
    assert "✗ write" in rendered
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

    assert rendered == "assistant: the answer"  # framing events produced no lines
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
    assert texts[0].startswith("✗ grep")
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

    assert "line-0" in rendered
    assert "line-7" in rendered
    assert "line-8" not in rendered
    assert "... 4 more lines" in rendered


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

    assert "✗ bash" in rendered
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

    assert "✗ bash" in rendered  # failure glyph despite is_error=False
    assert "exit 1" in rendered
    assert "line-39" in rendered


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
    assert "✓ edit" in text
    assert "-return 1" in text  # deletion line
    assert "+return 2" in text  # addition line
    # Diff spans carry the theme *variables* ($success/$error), not baked hex —
    # Textual resolves them per active theme at paint time, so a theme switch
    # recolors the diff. Asserting the variable names proves it's theme-linked,
    # not hardcoded, which is the whole point of using $success/$error.
    assert "$success" in styles  # additions
    assert "$error" in styles  # deletions


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
    assert "✓ write" in text
    assert "-line a" in text  # deletion line (prior content)
    assert "+line b" in text  # addition line (new content)
    assert "$success" in styles  # additions
    assert "$error" in styles  # deletions


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
    assert "✓ write" in text
    assert "+fresh line" in text


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
    assert "✓ write" in text
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
    assert "✓ read" in text
    assert "read 3 lines from foo.py" in text
    assert "import os" not in text  # the raw output is replaced by the summary


def test_textual_tool_card_grep_shows_match_summary() -> None:
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
    assert "✓ grep" in text
    assert "grep: 3 matches" in text
    assert "a.py:1:x" not in text  # raw matches replaced by the summary


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
    # LineMessage/StreamMessage carry their color as a role-styled Rich span.
    styles = _rendered_segment_styles(
        [
            completed_message(content="hi"),
            ErrorEvent(message="boom"),
        ]
    )

    assert "#5cc9a7" in styles  # assistant -> success
    assert "#d16a7c" in styles  # error -> error


def test_textual_tool_card_carries_role_class_for_left_rule_color() -> None:
    # A ToolCard's color lives in its `message--<role>` CSS class (which drives the
    # left-rule color), not in a text span — so assert the class, not a span color.
    cards = _cards_for_events([ToolCallRequested(call_id="c1", name="bash", arguments={})])
    assert cards == [("message--tool", "tool")]


def test_textual_theme_switch_rederives_transcript_styles() -> None:
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            app_instance.theme = "wisp-light"
            renderer.event(completed_message(content="after switch"))
            await pilot.pause()
            return _transcript_styles(app_instance)

    rendered = anyio.run(scenario)
    # The post-switch line uses the light theme's success color, not dark's.
    assert "#2f9d78" in rendered  # light wisp assistant/success
    assert "#5cc9a7" not in rendered  # dark wisp success must be gone


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
            await pilot.pause()
            await pilot.pause()  # let the deferred finalize settle
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


def test_textual_end_token_stream_finalizes_the_bubble() -> None:
    # end_token_stream() is the ONLY place a streamed assistant turn is finalized
    # (the shell suppresses the trailing MessageCompleted when tokens rendered).
    # After it, the buffer/live-widget refs are cleared and the text persists.
    async def scenario() -> tuple[str, object, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("final answer")
            renderer.end_token_stream()
            await pilot.pause()
            await pilot.pause()
            texts = _transcript_texts(app_instance)
            return (
                texts[0] if texts else "",
                app_instance._stream.live_widget,
                app_instance._stream.buffered_text,
            )

    text, live_widget, buffer = anyio.run(scenario)
    assert text == "final answer"
    assert live_widget is None  # finalized, no dangling live widget
    assert buffer == ""  # buffer cleared


def test_textual_single_tick_turn_keeps_its_content() -> None:
    # A turn finalized in the same tick it mounts (delta then flush, no refresh
    # between) must not lose its text. Markdown._on_mount runs `update("")` on the
    # widget's Mount event — a path separate from set_content's update — and can
    # run AFTER finalize, clobbering the content back to empty. set_content keeps
    # Markdown._initial_markdown in sync so that mount re-applies the real text.
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("first turn")
            renderer.end_token_stream()
            await pilot.pause()
            await pilot.pause()
            # Second turn finalized in a single tick: the fresh StreamMessage mounts
            # and finalizes before any refresh interleaves — the clobber window.
            renderer.token_delta("second turn")
            renderer.end_token_stream()
            await pilot.pause()
            await pilot.pause()
            return _transcript_texts(app_instance)

    texts = anyio.run(scenario)
    assert texts == ["first turn", "second turn"]  # neither turn lost to the clobber


def test_textual_streamed_and_line_messages_use_distinct_widgets() -> None:
    async def scenario() -> list[str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("streamed reply")
            renderer.end_token_stream()
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            await pilot.pause()
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
    # denied), c4 is denied at approval (→ denied). One card per call_id.
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
        "message--denied",  # c2 errored
        "message--denied",  # c4 denied at approval
        "message--error",
    ]


def test_textual_line_message_border_title_from_role_labels() -> None:
    # Stage 3: the card's role label comes ONLY from _ROLE_LABELS (fixed literals),
    # never from untrusted payload — so it's safe as border chrome.
    cards = _cards_for_events(
        [
            completed_message(content="hi"),
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ErrorEvent(message="bad"),
        ]
    )
    titles = [title for _, title in cards]
    assert titles == [_ROLE_LABELS["assistant"], _ROLE_LABELS["tool"], _ROLE_LABELS["error"]]


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
                app_instance._working_indicator is not None,
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
                app_instance._working_indicator is None,
                app_instance._working_indicator is not None,
            )

    recovered, transcript, timer_stopped, active = anyio.run(scenario)
    assert "Working" in recovered
    assert "Retrying" not in recovered
    assert "response" in transcript
    assert "Retrying" not in transcript
    assert timer_stopped
    assert not active


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
            replayed = app_instance._working_indicator is not None

            renderer.running()
            renderer.event(TurnStarted(turn=1))
            renderer.event(_provider_retry())
            await pilot.pause()
            renderer.event(AgentCompleted(session_id="s1", turns=1, outcome="completed"))
            renderer.event(_provider_retry(attempt=2))
            await pilot.pause()
            remaining = app_instance._working_indicator is not None
            return replayed, remaining, not remaining, _working_activity(app_instance)

    replayed, remaining, timer_stopped, rendered = anyio.run(scenario)
    assert not replayed
    assert not remaining
    assert timer_stopped
    assert "Retrying" not in rendered


def test_textual_retry_progress_yields_to_approval_cancellation_and_rpc_failure() -> None:
    async def approval_scenario() -> tuple[bool, bool]:
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
                app_instance._working_indicator is not None,
                app_instance.query_one("#decision-panel").display,
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
            remaining = app_instance._working_indicator is not None
            return remaining, not remaining

    async def failure_scenario() -> tuple[bool, bool]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            renderer.event(_provider_retry())
            await pilot.pause()
            renderer.rpc_event_reader_failed("closed")
            await pilot.pause()
            remaining = app_instance._working_indicator is not None
            return remaining, not remaining

    approval_row, approval_visible = anyio.run(approval_scenario)
    cancellation_row, cancellation_timer_stopped = anyio.run(cancellation_scenario)
    failure_row, failure_timer_stopped = anyio.run(failure_scenario)
    assert not approval_row
    assert approval_visible
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
                len(app_instance._tool_cards),
                len(renderer._tool_started),
            )

    texts, timers_stopped, app_registry, started_registry = anyio.run(scenario)
    assert all(t.startswith("⊘ ") and "cancelled" in t for t in texts)  # cancelled glyph + label
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
            assert len(app_instance._tool_cards) == 1
            renderer.event(
                RpcCommandFinished(command_id="cmd1", command_type="prompt", ok=False, error="boom")
            )
            await pilot.pause()
            return len(app_instance._tool_cards)

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


def test_textual_stream_message_carries_the_assistant_card() -> None:
    # The streamed turn wears the same card as a finalized assistant line, so the
    # bubble looks identical before and after finalize.
    async def scenario() -> tuple[str | None, object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.token_delta("partial answer")
            await pilot.pause()
            (card,) = _transcript_cards(app_instance)
            return card

    role, title = anyio.run(scenario)
    assert role == "message--assistant"
    assert title == _ROLE_LABELS["assistant"]


def test_textual_card_css_resolves_under_the_light_theme() -> None:
    # The app starts on the dark theme, so card CSS is only exercised in light on a
    # runtime switch. Guard that the message's left-rule color resolves (bad CSS
    # fails app startup) AND tracks the light palette, not dark's — so a future
    # theme edit that drops a variable the rules use is caught in CI, not only at
    # runtime.
    async def scenario() -> object:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 16)) as pilot:
            app_instance.theme = "wisp-light"
            renderer.event(ToolCallRequested(call_id="c1", name="bash", arguments={}))
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            (tool_card,) = transcript.children
            _kind, color = tool_card.styles.border_left
            return color

    border_color = anyio.run(scenario)
    # tool messages use a $accent left rule; light wisp accent is #2f8f8f, dark #3fb8b8.
    assert border_color.hex.lower() == "#2f8f8f"


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
    # The footer keeps cwd/session on the first line and status/model on the second.
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
    assert "session: sess.json" in status_text
    assert "running • queued 2" in status_text
    assert "openai/gpt-test" in status_text


def test_textual_footer_fits_the_status_content_region() -> None:
    # The footer is sized to the #status content region (padding-excluded), not
    # the app width. The status bar has horizontal padding, so sizing from app
    # width would over-pad each line and make the two-line footer wrap/clip. At an
    # 80-col terminal the render region is 78; no footer line may exceed it.
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
    assert region_width == 78  # app width 80 minus the status-bar's 1-cell side padding
    assert all(w <= 78 for w in line_widths)  # no line overflows the render region


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


def test_textual_working_status_disappears_on_first_stream_output() -> None:
    async def scenario() -> tuple[str, str, list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            before = _working_activity(app_instance)
            renderer.token_delta("hello")
            await pilot.pause()
            after = _working_activity(app_instance)
            return before, after, _transcript_texts(app_instance)

    before, after, transcript = anyio.run(scenario)
    assert "Working" in before
    assert "Working" not in after
    assert any("hello" in text for text in transcript)


def _fill_transcript(renderer: TextualTuiRenderer, count: int) -> None:
    # Mount enough lines to overflow the viewport so the transcript can scroll.
    for i in range(count):
        renderer.event(ToolCallRequested(call_id=f"c{i}", name=f"tool{i}", arguments={}))


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
            await pilot.pause()
            await pilot.pause()  # second pass: catch the settled max_scroll_y
            return transcript.scroll_y, transcript.max_scroll_y

    scroll_y, max_scroll_y = anyio.run(scenario)
    assert max_scroll_y > 0  # content actually overflowed
    assert scroll_y >= max_scroll_y - 3  # pinned to the tail


def test_textual_stream_message_set_content_returns_the_markdown_awaitable() -> None:
    # Contract test for the deeper race Codex flagged: Markdown.update() mounts its
    # block children asynchronously and returns an AwaitComplete whose completion is
    # the signal "all blocks mounted, max_scroll_y is final". set_content must hand
    # that awaitable back (not swallow it) so the finalize path can await it before
    # following the tail — rather than guessing a fixed number of refresh cycles.
    async def scenario() -> object:
        app_instance, _ = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            message = StreamMessage()
            transcript = app_instance.query_one("#transcript", Transcript)
            transcript.mount(message)
            await pilot.pause()
            awaitable = message.set_content("# Title\n\nsome **body** text")
            await awaitable  # awaiting it must not raise and must settle the mount
            return awaitable

    result = anyio.run(scenario)
    assert isinstance(result, AwaitComplete)


def test_textual_streaming_keeps_a_large_many_block_reply_pinned_to_the_tail() -> None:
    # A large, many-block Markdown reply (headings + lists) must still end pinned to
    # the tail. The finalize path awaits Markdown.update()'s AwaitComplete, so the
    # scroll lands on the settled extent no matter how many block children mount.
    async def scenario() -> tuple[float, float]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(60, 12)) as pilot:
            transcript = app_instance.query_one("#transcript", Transcript)
            _fill_transcript(renderer, 20)
            await pilot.pause()
            transcript.scroll_end(animate=False)
            await pilot.pause()
            blocks: list[str] = []
            for i in range(80):
                blocks.append(f"## Section {i}")
                blocks.append(f"- point a {i}\n- point b {i}")
            body = "\n\n".join(blocks)
            for chunk in (body[i : i + 80] for i in range(0, len(body), 80)):
                renderer.token_delta(chunk)
                await pilot.pause()
            renderer.end_token_stream()
            await pilot.pause()
            await pilot.pause()
            return transcript.scroll_y, transcript.max_scroll_y

    scroll_y, max_scroll_y = anyio.run(scenario)
    assert max_scroll_y > 100  # a genuinely large, overflowing reply
    assert scroll_y >= max_scroll_y - 3  # still pinned to the tail


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
            await pilot.pause()
            await pilot.pause()
            return transcript.scroll_y, transcript._follow

    scroll_y, follow = anyio.run(scenario)
    assert not follow  # scrolling away cleared the follow intent
    assert scroll_y <= 7  # stayed roughly where the user left it, not the bottom


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
            stream_count = len(app_instance._unseen_output)
            stream_label = jump.render().plain

            # A new ToolCard is a second logical output; resolving that same card
            # in place must not increase the count again.
            renderer.event(ToolCallRequested(call_id="latest", name="read", arguments={}))
            await pilot.pause()
            tool_count = len(app_instance._unseen_output)
            renderer.event(
                ToolResultReady(call_id="latest", name="read", output="ok", is_error=False)
            )
            await pilot.pause()
            resolved_count = len(app_instance._unseen_output)
            resolved_label = jump.render().plain

            await pilot.press("end")
            await pilot.pause()
            return {
                "stream_count": stream_count,
                "stream_label": stream_label,
                "tool_count": tool_count,
                "resolved_count": resolved_count,
                "resolved_label": resolved_label,
                "cleared": len(app_instance._unseen_output) == 0,
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
            working_count = len(app_instance._unseen_output)
            working_shown = jump.display is True

            # Retiring the heartbeat must drop it from the unseen set and hide the
            # badge, since it was the only unseen output.
            app_instance.hide_working_indicator()
            await pilot.pause()
            return {
                "working_count": working_count,
                "working_shown": working_shown,
                "cleared": len(app_instance._unseen_output) == 0,
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
            await pilot.pause()
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
            await pilot.pause()
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
                submitted = await app_instance._prompt_receive.receive()
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
                submitted = await app_instance._prompt_receive.receive()
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
                submitted = await app_instance._prompt_receive.receive()
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
                submitted = await app_instance._prompt_receive.receive()
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
                submitted = await app_instance._prompt_receive.receive()
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
            after_bare_interrupt = app_instance._echo_log.key_count

            # The shell's real queue-drop hook reclaims them.
            app_instance.clear_compact_echoes()
            after_queue_drop = app_instance._echo_log.key_count

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
            total = app_instance._echo_log.pending_count
            order_len = app_instance._echo_log.order_length
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
                app_instance._prompt_receive.receive_nowait()
            except anyio.WouldBlock:
                was_submitted = False
            else:
                was_submitted = True
            await pilot.press("enter")
            with anyio.fail_after(1):
                submitted = await app_instance._prompt_receive.receive()
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
    # input is never cleared/hijacked, and no modal palette exists.
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


def test_textual_command_palette_is_disabled() -> None:
    # The modal palette is gone: "/" is the only command affordance, so Textual's
    # framework ctrl+p palette must be off (it clashes with terminal history).
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


def test_textual_slash_menu_is_anchored_near_the_input() -> None:
    # The menu must render near the prompt, not float at the top of the app. Using
    # a separate `layer:` detaches it to the top (a real regression); overlay:screen
    # alone keeps its compose position just above #input. Assert it renders in the
    # lower half of the screen, adjacent to the input, with the input unmoved.
    async def scenario() -> tuple[int, int, int, int]:
        app_instance = TextualTui()
        async with app_instance.run_test(size=(80, 24)) as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()
            return (
                suggest.region.y,
                suggest.region.y + suggest.region.height,
                input_widget.region.y,
                app_instance.size.height,
            )

    menu_top, menu_bottom, input_y, height = anyio.run(scenario)
    assert menu_top >= height // 2  # menu is in the lower half, not floating at top
    assert input_y >= height - 4  # input stays pinned near the bottom (not shoved)
    assert abs(menu_bottom - input_y) <= 5  # menu sits adjacent to the input


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
            queued = await app_instance._prompt_receive.receive()
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
            queued = await app_instance._prompt_receive.receive()
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
            queued = await app_instance._prompt_receive.receive()
            return queued, suggest.is_open

    queued, menu_open = anyio.run(scenario)
    assert queued == "/quit"  # the HIGHLIGHTED command ran, not the "/" in the buffer
    assert menu_open is False  # accepting the highlight closes the menu


def test_textual_enter_on_arg_taking_highlight_fills_then_waits_for_value() -> None:
    # Enter on a highlighted arg-taking command (/model) must NOT submit a bare
    # "/model" — it fills "/model " and waits for the value, then a second Enter
    # runs the completed line. This mirrors how Tab-completion primes arg-taking
    # commands, so Enter and Tab agree.
    async def scenario() -> tuple[str, bool, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            await pilot.press("/")
            await pilot.pause()
            await _navigate_menu_to(pilot, suggest, "/model")
            await pilot.press("enter")  # accept -> fill, don't submit
            await pilot.pause()
            filled = input_widget.value
            open_after_fill = suggest.is_open
            await pilot.press(*"gpt-5")  # type the value
            await pilot.pause()
            await pilot.press("enter")  # now run it
            await pilot.pause()
            queued = await app_instance._prompt_receive.receive()
            return filled, open_after_fill, queued

    filled, open_after_fill, queued = anyio.run(scenario)
    assert filled == "/model "  # arg-taking -> filled with a trailing space
    assert open_after_fill is False  # menu closed while the user types the value
    assert queued == "/model gpt-5"  # second Enter runs the completed line


def test_textual_enter_runs_fully_typed_optional_arg_command_bare() -> None:
    # REGRESSION: "takes_args" means the command *optionally* takes an argument —
    # bare `/model`, `/provider`, `/login` are valid (show current / use defaults).
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
            queued = await app_instance._prompt_receive.receive()
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
            queued = await app_instance._prompt_receive.receive()
            return queued, input_widget.value

    queued, buffer_after = anyio.run(scenario)
    assert queued == "/model"  # ran the canonical spelling on the FIRST Enter
    assert buffer_after == ""  # not left as "/model " awaiting a second Enter


def test_textual_selecting_optional_arg_command_from_prefix_waits_for_value() -> None:
    # SAFETY: /auth and /logout take an optional [provider] (see the shell's
    # `/auth [provider]` / `/logout [provider]` handlers), so their specs are
    # takes_args=True. Selecting one from a bare "/" prefix must fill "/logout "
    # and wait for the provider — NOT submit "/logout" immediately, which would act
    # on the default provider (e.g. delete its credentials) instead of the intended
    # one. Checks both an idempotent (/auth) and a destructive (/logout) command.
    async def scenario() -> dict[str, tuple[str, str]]:
        results: dict[str, tuple[str, str]] = {}
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            suggest = app_instance.query_one("#suggest", SlashSuggest)
            for command in ("/auth", "/logout"):
                input_widget.value = ""
                suggest.hide()
                await pilot.pause()
                input_widget.focus()
                await pilot.pause()
                await pilot.press("/")  # bare prefix; nothing else typed
                await pilot.pause()
                await _navigate_menu_to(pilot, suggest, command)
                await pilot.press("enter")
                await pilot.pause()
                try:
                    with anyio.fail_after(0.2):
                        queued = await app_instance._prompt_receive.receive()
                except TimeoutError:
                    queued = "<none>"
                results[command] = (input_widget.value, queued)
        return results

    results = anyio.run(scenario)
    assert results["/auth"] == ("/auth ", "<none>")  # filled, awaiting provider
    assert results["/logout"] == ("/logout ", "<none>")  # no destructive auto-run


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
            first = await app_instance._prompt_receive.receive()
            try:
                with anyio.fail_after(0.2):
                    second = await app_instance._prompt_receive.receive()
            except TimeoutError:
                second = "<none>"
            return first, second

    first, second = anyio.run(scenario)
    assert first == "/quit"
    assert second == "<none>"  # exactly one submission, not two


def test_textual_startup_shows_a_disposable_centered_empty_state() -> None:
    # The wordmark identifies an empty session without consuming permanent
    # scrollback. Its ultra-minimal form and prompt hint must fit the compact
    # audit viewport, then disappear before the first real transcript item is
    # mounted.
    async def scenario() -> tuple[
        tuple[str, str],
        tuple[int, int, int],
        list[str],
        list[str],
    ]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(72, 20)) as pilot:
            renderer.startup()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            empty = app_instance.query_one("#transcript-empty", TranscriptEmptyState)
            wordmark = app_instance.query_one("#transcript-empty-wordmark", Static)
            hint = app_instance.query_one("#transcript-empty-hint", Static)
            centers = (
                transcript.region.x + transcript.region.width // 2,
                wordmark.region.x + wordmark.region.width // 2,
                hint.region.x + hint.region.width // 2,
            )
            initial_children = [type(child).__name__ for child in transcript.children]
            wordmark_content = wordmark.render()
            hint_content = hint.render()
            assert isinstance(wordmark_content, Content)
            assert isinstance(hint_content, Content)
            content = (wordmark_content.plain, hint_content.plain)

            renderer.prompt_submitted("hello")
            await pilot.pause()
            final_children = [type(child).__name__ for child in transcript.children]
            assert empty not in transcript.children
            return content, centers, initial_children, final_children

    content, centers, initial_children, final_children = anyio.run(scenario)
    wordmark, hint = content
    assert wordmark.strip() == "wisp"
    assert hint == "Type a prompt or / for commands."
    assert centers[0] == centers[1] == centers[2]
    assert initial_children == ["TranscriptEmptyState"]
    assert final_children == ["LineMessage"]


def test_textual_composer_focus_styles_resolve_for_both_themes() -> None:
    async def scenario() -> dict[str, tuple[str, str, str, str, float]]:
        styles: dict[str, tuple[str, str, str, str, float]] = {}
        for theme in ("wisp", "wisp-light"):
            app_instance = TextualTui()
            async with app_instance.run_test() as pilot:
                app_instance.theme = theme
                input_widget = app_instance.query_one("#input", Input)
                app_instance.screen.set_focus(None)
                await pilot.pause(0.25)
                idle_background = input_widget.styles.background.hex
                idle_border = input_widget.styles.border_bottom[1].hex

                input_widget.focus()
                await pilot.pause(0.25)
                transition = input_widget.styles.transitions["border"]
                styles[theme] = (
                    idle_background,
                    input_widget.styles.background.hex,
                    idle_border,
                    input_widget.styles.border_bottom[1].hex,
                    transition.duration,
                )
        return styles

    styles = anyio.run(scenario)
    expected = {
        "wisp": ("#0E1216", "#151B21", "#3FB8B8"),
        "wisp-light": ("#FBFCFD", "#FFFFFF", "#2F8F8F"),
    }
    for theme, (
        idle_background,
        focused_background,
        idle_border,
        focused_border,
        delay,
    ) in styles.items():
        expected_idle, expected_focused, expected_accent = expected[theme]
        assert idle_background == expected_idle
        assert focused_background == expected_focused
        assert idle_border != focused_border
        assert focused_border == expected_accent
        assert delay == 0.2


def test_textual_scrollbar_colors_resolve_for_both_themes() -> None:
    async def scenario() -> dict[str, tuple[str, str, str, int]]:
        colors: dict[str, tuple[str, str, str, int]] = {}
        for theme in ("wisp", "wisp-light"):
            app_instance = TextualTui()
            async with app_instance.run_test() as pilot:
                app_instance.theme = theme
                await pilot.pause()
                transcript = app_instance.query_one("#transcript", Transcript)
                colors[theme] = (
                    transcript.styles.scrollbar_color.hex,
                    transcript.styles.scrollbar_color_hover.hex,
                    transcript.styles.scrollbar_color_active.hex,
                    transcript.styles.scrollbar_size_vertical,
                )
        return colors

    colors = anyio.run(scenario)
    assert colors == {
        "wisp": ("#7C8B99", "#4AA3C7", "#3FB8B8", 1),
        "wisp-light": ("#55636D", "#2F8FB3", "#2F8F8F", 1),
    }


def test_textual_input_is_pinned_to_the_bottom() -> None:
    # Regression: a wrapping Container defaulted to height:1fr and floated the
    # input into the middle. The transcript should own the free space (1fr) while
    # the input hugs the bottom rows.
    async def scenario() -> tuple[int, int, int]:
        app_instance = TextualTui()
        async with app_instance.run_test(size=(74, 24)) as pilot:
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            transcript = app_instance.query_one("#transcript", Transcript)
            return app_instance.size.height, input_widget.region.y, transcript.region.height

    screen_h, input_top, transcript_h = anyio.run(scenario)
    # The input sits in the last few rows; the transcript fills most of the height.
    assert input_top >= screen_h - 4
    assert transcript_h >= screen_h // 2


def test_textual_input_placeholder_uses_the_prompt_glyph() -> None:
    # The underline-only input leads with a `❯` glyph, not the verbose `wisp>`
    # chrome. The shared semantic hint (wisp> / wisp(running)>) is mapped to a terse
    # glyph placeholder in the Textual layer, so a mode change swaps the cue.
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
    assert running_placeholder == "❯ running…"


def test_textual_input_has_no_box_border() -> None:
    # The input is underline-only — a bottom rule, no four-sided box. Asserting the
    # border is absent on the top/left/right edges (only bottom is styled) guards
    # against a regression back to the heavy `tall` box.
    async def scenario() -> object:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            input_widget = app_instance.query_one("#input", Input)
            return input_widget.styles.border

    border = anyio.run(scenario)
    # Textual's Edges exposes each side as an (edge_type, color) tuple; only the
    # bottom edge carries a rule. top/left/right have an empty ("") edge type.
    assert border.top[0] == ""
    assert border.left[0] == ""
    assert border.right[0] == ""
    assert border.bottom[0] == "heavy"


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


def test_textual_header_shows_the_wisp_wordmark() -> None:
    # The header title is the lowercase wordmark; the clock chrome is gone.
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            return app_instance.title, app_instance.sub_title

    title, sub_title = anyio.run(scenario)
    assert title == "wisp"
    assert sub_title == "tethered to you"


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


def test_textual_tui_ctrl_c_interrupts_read_prompt() -> None:
    assert _read_prompt_signal_for_key("ctrl+c") is KeyboardInterrupt


def test_textual_tui_ctrl_d_closes_read_prompt() -> None:
    # ctrl+d must reach the app binding even though the Input widget is focused.
    assert _read_prompt_signal_for_key("ctrl+d") is EOFError


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
                    try:
                        await app_instance.read_prompt("wisp> ")
                    except KeyboardInterrupt:
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

    assert anyio.run(scenario) is KeyboardInterrupt


def test_textual_ctrl_c_copies_when_editor_is_focused_with_selection() -> None:
    # Complement to the approval regression: when the editor IS visible and
    # focused with a selection, Ctrl+C keeps its "copy, don't interrupt" behavior.
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
                    except KeyboardInterrupt:
                        interrupted = True

                tg.start_soon(read)
                await pilot.pause()
                app_instance.action_interrupt()
                await pilot.pause()
                # No interrupt was queued, so the read is still pending — cancel it
                # so the task group can exit.
                tg.cancel_scope.cancel()
            return interrupted

    assert anyio.run(scenario) is False


def test_textual_ctrl_c_copy_failure_does_not_fire_interrupt() -> None:
    # Regression (verification P1): the copy-branch return must sit OUTSIDE the
    # suppress block. If copy_to_clipboard raises (e.g. broken terminal OSC52
    # write), an editor-owned ctrl+c copy gesture must NOT fall through to
    # KeyboardInterrupt — that would interrupt the agent and wipe the draft line.
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
                    except KeyboardInterrupt:
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
            await pilot.pause()
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
