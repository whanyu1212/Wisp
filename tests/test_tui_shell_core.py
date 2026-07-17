# ruff: noqa: F403,F405

from __future__ import annotations

import json
from tempfile import TemporaryDirectory

from pytest import MonkeyPatch

from tests.tui_support import *
from wisp.auth.storage import OAuthCredential
from wisp.events import MessageStarted, ProviderRetrying
from wisp.tui import auth_commands as tui_auth_commands_module


def test_tui_shell_records_submitted_prompt_for_fullscreen_renderer() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    completed_message(content="answer"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["what <now>?"]),
        )

        await shell.run()

        assert any(
            entry.role == "user" and entry.content == "what <now>?"
            for entry in renderer.state.transcript
        )

    anyio.run(run)


def test_tui_shell_runs_with_fullscreen_renderer() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    completed_message(content="fullscreen response"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            renderer=FullscreenTuiRenderer(console, clear_screen=False),
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert "Transcript" in output.getvalue()
        assert "fullscreen response" in output.getvalue()

    anyio.run(run)


def test_tui_shell_shows_retry_status_until_response_starts() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.snapshots: list[TuiViewSnapshot] = []

        def view_updated(self, snapshot: TuiViewSnapshot) -> None:
            self.snapshots.append(snapshot)

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.status = TuiStatus.running

        await shell._handle_rpc_event(
            ProviderRetrying(
                turn=1,
                provider="openai",
                attempt=2,
                max_attempts=3,
                delay_seconds=0.5,
                reason="rate_limit",
            )
        )
        assert renderer.snapshots[-1].status == "retrying 2/3 in 0.5s"

        await shell._handle_rpc_event(MessageStarted(turn=1))
        assert renderer.snapshots[-1].status == "running"

    anyio.run(run)


def test_tui_shell_runs_prompt_then_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    message_delta(delta="hello"),
                    completed_message(content="hello"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert controller.shutdown_count == 1
        assert "Wisp TUI MVP" in output.getvalue()
        assert "hello" in output.getvalue()

    anyio.run(run)


def test_tui_shell_sends_slash_prefixed_prose_to_the_model() -> None:
    # A leading slash that isn't a known command (a path, or slash-prose) must
    # reach the model as a normal prompt, not be rejected as "Unknown command".
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    completed_message(content="looking into it"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, _ = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/etc/hosts is broken"]),
        )

        await shell.run()

        # It reached the model verbatim rather than raising a command error.
        assert controller.prompts == ["/etc/hosts is broken"]

    anyio.run(run)


def test_tui_shell_sends_known_slash_command_multiline_input_to_the_model() -> None:
    async def run() -> None:
        prompt = "/help\nplease explain this"
        controller = ScriptedController(
            [
                [
                    completed_message(content="explanation"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        shell = TuiShell(
            controller,
            console=_console()[0],
            prompt_reader=await _reader_from([prompt]),
        )

        await shell.run()

        assert controller.prompts == [prompt]

    anyio.run(run)


def test_tui_shell_preserves_trailing_newline_before_slash_command_parsing() -> None:
    async def run() -> None:
        prompt = "/quit\n"
        controller = ScriptedController(
            [
                [
                    completed_message(content="not quitting"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        shell = TuiShell(
            controller,
            console=_console()[0],
            prompt_reader=await _reader_from([prompt]),
        )

        await shell.run()

        assert controller.prompts == [prompt]
        assert controller.shutdown_count == 1

    anyio.run(run)


def test_tui_shell_help_renders_approval_hint_literally() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/help", "/quit"]),
        )

        await shell.run()

        assert "approve? [y/N]" in output.getvalue()

    anyio.run(run)


def test_tui_shell_adopts_trusted_project_config(tmp_path: Path) -> None:
    # A ProjectConfigApplied event (first-run trust approval applied the project's
    # settings.json on the RPC side) must update the TUI's provider/model/auth so the
    # header and /provider,/model,/auth,/login stop showing the untrusted-startup ones.
    async def run() -> None:
        controller = ScriptedController()
        startup_auth = tmp_path / "startup-auth.json"
        trusted_auth = tmp_path / "trusted-auth.json"
        shell = TuiShell(
            controller,
            renderer=LineTuiRenderer(_console()[0]),
            prompt_reader=await _reader_from([]),
            provider="startup-provider",
            model=None,
            auth_path=startup_auth,
        )

        await shell._handle_rpc_event(
            ProjectConfigApplied(
                provider="trusted-provider", model="trusted-model", auth_path=trusted_auth
            )
        )

        assert shell.current_provider == "trusted-provider"
        assert shell.current_model == "trusted-model"
        assert shell.auth_store.path == trusted_auth
        assert shell.view.provider == "trusted-provider"  # header resynced

    anyio.run(run)


def test_tui_shell_init_drops_effort_invalid_for_the_startup_provider() -> None:
    # Regression test (Codex review on #125): TuiShell resolves its own
    # config.effort independently, via its own WispConfig.from_env() call in
    # the same process launch as the separate RPC subprocess -- so it must
    # apply the same provider/model effort-scoping wisp.cli.rpc's
    # startup_effort() call performs on the CodingSession side, or the picker
    # would seed a stale/incompatible tier into its "current" row (see
    # ModelPicker.show) even after the RPC side had already filtered it out.
    controller = ScriptedController()
    shell = TuiShell(
        controller,
        renderer=LineTuiRenderer(_console()[0]),
        provider="openai",
        model="gpt-5.5",
        effort="HIGH",  # Google-style, not one of gpt-5.5's real catalog tiers
    )

    assert shell.current_effort is None


def test_tui_shell_init_keeps_effort_valid_for_the_startup_provider() -> None:
    controller = ScriptedController()
    shell = TuiShell(
        controller,
        renderer=LineTuiRenderer(_console()[0]),
        provider="anthropic",
        model="claude-opus-4-8",
        effort="high",
    )

    assert shell.current_effort == "high"


def test_tui_shell_project_config_applied_adopts_the_events_own_effort(
    tmp_path: Path,
) -> None:
    # Regression test (Codex review on #125): ProjectConfigApplied.effort
    # carries the RPC agent's already-filtered, authoritative post-rebuild
    # value -- the TUI must adopt it directly rather than re-deriving effort
    # from its own local current_effort. That local copy was itself already
    # filtered once, against the untrusted-startup provider/model, in
    # __init__; a tier invalid there but valid for the trusted project's
    # provider/model would already be gone from it and unrecoverable, so
    # re-deriving from it (instead of trusting the event) can never recover a
    # tier that's only valid on the trusted side. Here the startup tier
    # ("HIGH", invalid for anthropic/claude-opus-4-8's lowercase vocabulary)
    # is correctly dropped at __init__, and the *event* carries a different,
    # freshly-valid tier the RPC side determined for the trusted provider --
    # proving the TUI takes the event's value, not its own stale local one.
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(
            controller,
            renderer=LineTuiRenderer(_console()[0]),
            provider="anthropic",
            model="claude-opus-4-8",
            effort="HIGH",
            auth_path=tmp_path / "startup-auth.json",
        )
        assert shell.current_effort is None

        await shell._handle_rpc_event(
            ProjectConfigApplied(
                provider="google",
                model="gemini-flash-latest",
                effort="HIGH",
                auth_path=tmp_path / "trusted-auth.json",
            )
        )

        assert shell.current_provider == "google"
        assert shell.current_model == "gemini-flash-latest"
        assert shell.current_effort == "HIGH"

    anyio.run(run)


def test_tui_shell_project_config_applied_drops_effort_invalid_for_new_provider(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(
            controller,
            renderer=LineTuiRenderer(_console()[0]),
            provider="anthropic",
            model="claude-opus-4-8",
            effort="high",
            auth_path=tmp_path / "startup-auth.json",
        )
        assert shell.current_effort == "high"

        await shell._handle_rpc_event(
            ProjectConfigApplied(
                provider="google",
                model="gemini-flash-latest",
                auth_path=tmp_path / "trusted-auth.json",
            )
        )

        assert shell.current_provider == "google"
        assert shell.current_model == "gemini-flash-latest"
        assert shell.current_effort is None

    anyio.run(run)


def test_tui_shell_auth_status_uses_current_provider(tmp_path: Path) -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/auth", "/quit"]),
            provider="openai-codex",
            auth_path=tmp_path / "auth.json",
        )

        await shell.run()

        assert "openai-codex: not logged in" in output.getvalue()
        assert controller.prompts == []

    anyio.run(run)


def test_tui_shell_auth_status_reports_storage_errors(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{not json", encoding="utf-8")

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/auth openai-codex", "/quit"]),
            provider="openai-codex",
            auth_path=auth_path,
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Auth storage error: Invalid auth file JSON:" in rendered
        assert "openai-codex: not logged in" not in rendered
        assert controller.prompts == []

    anyio.run(run)


def test_tui_shell_logout_reports_storage_errors(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{not json", encoding="utf-8")

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/logout openai-codex", "/quit"]),
            provider="openai-codex",
            auth_path=auth_path,
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Auth storage error: Invalid auth file JSON:" in rendered
        assert "Logged out: openai-codex" not in rendered
        assert "Not logged in: openai-codex" not in rendered
        assert controller.prompts == []

    anyio.run(run)


def test_tui_shell_login_reports_storage_errors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_login(*_args: object, **_kwargs: object) -> OAuthCredential:
        return OAuthCredential(
            access="access-token",
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        )

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex", fake_login)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{not json", encoding="utf-8")

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/login openai-codex", "/quit"]),
            provider="openai-codex",
            auth_path=auth_path,
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Starting openai-codex device-code login..." in rendered
        assert "Auth storage error: Invalid auth file JSON:" in rendered
        assert "Logged in: openai-codex" not in rendered
        assert "access-token" not in rendered

    anyio.run(run)


def test_tui_shell_login_and_logout_openai_codex(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_login(*_args: object, **_kwargs: object) -> OAuthCredential:
        return OAuthCredential(
            access="access-token",
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        )

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex", fake_login)

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(
                ["/login openai-codex", "/auth openai-codex", "/logout openai-codex", "/quit"]
            ),
            provider="openai-codex",
            auth_path=tmp_path / "auth.json",
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Logged in: openai-codex" in rendered
        assert "openai-codex: oauth configured" in rendered
        assert "Logged out: openai-codex" in rendered
        assert "access-token" not in rendered

    anyio.run(run)


def test_tui_shell_login_defaults_to_pending_provider(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_login(*_args: object, **_kwargs: object) -> OAuthCredential:
        return OAuthCredential(
            access="access-token",
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        )

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex", fake_login)

    async def run() -> None:
        controller = ScriptedController(
            configure_events=[
                (
                    0.2,
                    [
                        RpcCommandFinished(
                            command_id="configure-1",
                            command_type="configure",
                            ok=True,
                        )
                    ],
                )
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(
                ["/provider openai-codex", "/login", "/auth", "/quit"]
            ),
            provider="fake",
            auth_path=tmp_path / "auth.json",
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Configuring provider: openai-codex" in rendered
        assert "Logged in: openai-codex" in rendered
        assert "openai-codex: oauth configured" in rendered
        assert "TUI login currently supports only openai-codex" not in rendered
        assert "fake: oauth configured" not in rendered

    anyio.run(run)


def test_tui_shell_provider_and_model_commands_configure_future_prompts() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(
                ["/provider openai-codex", "/model gpt-5.5", "/provider", "/model", "/quit"]
            ),
        )

        await shell.run()

        assert controller.configurations == [
            ("openai-codex", None, None, False),
            (None, "gpt-5.5", None, False),
        ]
        rendered = output.getvalue()
        assert "Configuring provider: openai-codex" in rendered
        assert "Provider set to openai-codex" in rendered
        assert "Configuring model: gpt-5.5" in rendered
        assert "Model set to gpt-5.5" in rendered

    anyio.run(run)


def test_tui_shell_bare_model_command_lists_catalog_models_grouped_by_provider() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model", "/quit"]),
            provider="openai",
            model="gpt-5.5",
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Available models:" in rendered
        assert "openai:" in rendered
        assert "openai-codex:" in rendered
        assert "fake:" in rendered
        assert "gpt-5.5 (current)" in rendered
        assert "Current model: gpt-5.5" in rendered
        assert "Current provider: openai" in rendered
        # No configure command should be sent for a bare, argument-less /model.
        assert controller.configurations == []

    anyio.run(run)


def test_tui_shell_model_listing_marks_current_only_on_the_active_provider() -> None:
    # "gpt-5.5" is claimed by both openai and openai-codex in the built-in
    # catalog (see ModelRegistry.resolve()'s ambiguity handling). The listing
    # must mark (current) only on the entry under the active provider, not on
    # every provider's copy of the shared model id.
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model", "/quit"]),
            provider="openai",
            model="gpt-5.5",
        )

        await shell.run()

        rendered = output.getvalue()
        assert rendered.count("(current)") == 1
        assert "  openai: gpt-5.5 (current)" in rendered
        assert "  openai-codex: gpt-5.5," in rendered or "  openai-codex: gpt-5.5\n" in rendered

    anyio.run(run)


def test_tui_shell_model_listing_marks_provider_default_as_current_when_unset() -> None:
    # Regression test: at startup, no /model has been run yet, so
    # self.current_model is None -- but the provider's own default_model is
    # what will actually be used. The listing must mark that entry current
    # instead of leaving the whole listing unmarked (the "Current model:
    # provider default" line below already communicates this fallback; the
    # listing itself must be consistent with it).
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model", "/quit"]),
            provider="openai",
            model=None,
        )

        await shell.run()

        rendered = output.getvalue()
        assert rendered.count("(current)") == 1
        assert "  openai: gpt-5.5 (current)" in rendered
        assert "Current model: provider default" in rendered

    anyio.run(run)


def test_tui_shell_bare_model_command_lists_current_effort() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model", "/quit"]),
            provider="openai",
            model="gpt-5.5",
            effort="high",
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Current effort: high" in rendered

    anyio.run(run)


def test_tui_shell_model_command_with_effort_configures_and_persists() -> None:
    async def run(tmp_path: Path) -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model claude-opus-4-8 high", "/quit"]),
            provider="anthropic",
            settings_home_dir=tmp_path,
        )

        await shell.run()

        assert controller.configurations == [(None, "claude-opus-4-8", "high", False)]
        assert shell.current_model == "claude-opus-4-8"
        assert shell.current_effort == "high"
        rendered = output.getvalue()
        assert "Configuring model: claude-opus-4-8, effort high" in rendered
        assert "Model set to claude-opus-4-8" in rendered
        settings_path = tmp_path / ".wisp" / "settings.json"
        assert json.loads(settings_path.read_text(encoding="utf-8"))["effort"] == "high"

    with TemporaryDirectory() as tmp_dir:
        anyio.run(run, Path(tmp_dir))


def test_tui_shell_model_command_without_effort_arg_also_clears_stale_effort() -> None:
    # Regression test (Codex review on #125): _handle_rpc_configure_command
    # unconditionally resets agent.effort to None whenever a configure carries
    # `model` (or `provider`) and no explicit `effort` -- via an explicit
    # provider switch, a model-triggered auto-switch, or a same-provider model
    # change (the old tier may not be valid for the new model; see
    # wisp.cli.rpc's has_model branch). Before this fix, the shell only
    # cleared current_effort/the persisted setting when the picker's explicit
    # clear-token was sent, leaving both stale (and the picker seeding a tier
    # the backend no longer uses) after a plain "/model <id>" with no effort
    # argument at all.
    async def run(tmp_path: Path) -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model claude-haiku-4-5", "/quit"]),
            provider="anthropic",
            effort="high",
            settings_home_dir=tmp_path,
        )

        await shell.run()

        assert controller.configurations == [(None, "claude-haiku-4-5", None, False)]
        assert shell.current_model == "claude-haiku-4-5"
        assert shell.current_effort is None
        settings_path = tmp_path / ".wisp" / "settings.json"
        if settings_path.exists():
            assert "effort" not in json.loads(settings_path.read_text(encoding="utf-8"))

    with TemporaryDirectory() as tmp_dir:
        anyio.run(run, Path(tmp_dir))


def test_tui_shell_provider_command_clears_stale_effort() -> None:
    # Same server-side unconditional-reset rule as the /model regression above
    # (wisp.cli.rpc's has_provider branch), exercised via /provider instead.
    async def run(tmp_path: Path) -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/provider openai", "/quit"]),
            provider="anthropic",
            effort="high",
            settings_home_dir=tmp_path,
        )

        await shell.run()

        assert shell.current_provider == "openai"
        assert shell.current_effort is None
        settings_path = tmp_path / ".wisp" / "settings.json"
        if settings_path.exists():
            assert "effort" not in json.loads(settings_path.read_text(encoding="utf-8"))

    with TemporaryDirectory() as tmp_dir:
        anyio.run(run, Path(tmp_dir))


def test_tui_shell_model_command_too_many_args_rejected() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model a b c", "/quit"]),
            provider="openai",
        )

        await shell.run()

        assert controller.configurations == []
        assert "Usage: /model [model] [effort]" in output.getvalue()

    anyio.run(run)


def test_tui_shell_model_command_parses_provider_qualified_selection() -> None:
    # Regression test: ModelPicker.submit_current_selection sends
    # "provider::model" (see widgets.ModelPicker), not a bare model id, so a
    # model shared by multiple providers (e.g. "gpt-5.5" under both openai and
    # openai-codex) always switches to the exact row picked rather than
    # depending on ModelRegistry.resolve's ambiguity handling server-side.
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model openai-codex::gpt-5.5", "/quit"]),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [("openai-codex", "gpt-5.5", None, False)]
        assert shell.current_provider == "openai-codex"
        assert shell.current_model == "gpt-5.5"
        rendered = output.getvalue()
        assert "Provider set to openai-codex" in rendered
        assert "Model set to gpt-5.5" in rendered

    anyio.run(run)


def test_tui_shell_model_command_clear_effort_token_clears_persisted_effort() -> None:
    # Regression test: ModelPicker sends MODEL_COMMAND_CLEAR_EFFORT_TOKEN ("-")
    # when the user explicitly cycles effort back to "(default)". This test
    # only exercises that explicit path; see
    # test_tui_shell_model_command_without_effort_arg_also_clears_stale_effort
    # for confirmation that a bare "/model <id>" (no effort arg at all)
    # produces the exact same clearing outcome, since the RPC side resets
    # agent.effort unconditionally whenever a configure carries `model` and no
    # explicit `effort` -- there is no client-only "leave it untouched" case.
    async def run(tmp_path: Path) -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model claude-opus-4-8 -", "/quit"]),
            provider="anthropic",
            effort="high",
            settings_home_dir=tmp_path,
        )

        await shell.run()

        assert controller.configurations == [(None, "claude-opus-4-8", None, True)]
        assert shell.current_model == "claude-opus-4-8"
        assert shell.current_effort is None
        settings_path = tmp_path / ".wisp" / "settings.json"
        if settings_path.exists():
            assert "effort" not in json.loads(settings_path.read_text(encoding="utf-8"))

    with TemporaryDirectory() as tmp_dir:
        anyio.run(run, Path(tmp_dir))


def test_tui_shell_adopts_server_side_auto_switched_provider() -> None:
    # Regression test: a model-only /model <id> can resolve server-side to a
    # different provider than the one the TUI thinks is active (see
    # _auto_switch_provider_for_model in wisp.cli.rpc). Without handling
    # ModelProviderAutoSwitched, the shell would only update current_model and
    # leave current_provider stale, so /provider, /auth, and the header would
    # keep showing the old provider even though the RPC agent had moved on.
    async def run() -> None:
        controller = ScriptedController(
            configure_events=[
                [
                    ModelProviderAutoSwitched(
                        command_id="configure-1", provider="openai", model="gpt-5.5-pro"
                    ),
                    RpcCommandFinished(command_id="configure-1", command_type="configure", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model gpt-5.5-pro", "/quit"]),
            provider="fake",
        )

        await shell.run()

        assert shell.current_provider == "openai"
        assert shell.current_model == "gpt-5.5-pro"
        rendered = output.getvalue()
        assert "Provider set to openai" in rendered
        assert "Model set to gpt-5.5-pro" in rendered
        # The model was not "reset" by the auto-switch -- it was explicitly
        # requested, so the reset-to-default wording must not appear.
        assert "reset to provider default" not in rendered

    anyio.run(run)


def test_tui_shell_provider_and_model_updates_footer_snapshots() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.snapshots: list[TuiViewSnapshot] = []

        def view_updated(self, snapshot: TuiViewSnapshot) -> None:
            self.snapshots.append(snapshot)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["/provider openai", "/model gpt-test", "/quit"]),
            provider="fake",
        )

        await shell.run()

        assert any(
            snapshot.provider == "openai" and snapshot.model is None
            for snapshot in renderer.snapshots
        )
        assert renderer.snapshots[-1].provider == "openai"
        assert renderer.snapshots[-1].model == "gpt-test"

    anyio.run(run)


def test_tui_shell_provider_command_waits_for_configure_success() -> None:
    async def run() -> None:
        controller = ScriptedController(
            configure_events=[
                [
                    ErrorEvent(message="Unknown provider: missing"),
                    RpcCommandFinished(
                        command_id="configure-1",
                        command_type="configure",
                        ok=False,
                        error="Unknown provider: missing",
                    ),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/provider missing", "/provider", "/quit"]),
            provider="fake",
        )

        await shell.run()

        assert controller.configurations == [("missing", None, None, False)]
        assert shell.current_provider == "fake"
        rendered = output.getvalue()
        assert "Configuring provider: missing" in rendered
        assert "Provider unchanged (fake): Unknown provider: missing" in rendered
        assert "Provider set to missing" not in rendered

    anyio.run(run)


def test_tui_shell_rejects_slash_commands_while_running() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                )
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["first", "/model gpt-5.5"]),
        )

        await shell.run()

        assert controller.configurations == []
        assert "Cannot run slash commands while a prompt is running." in output.getvalue()

    anyio.run(run)


def test_default_prompt_reader_hides_prompts_for_non_tty(monkeypatch: object) -> None:
    prompts: list[str] = []

    class NonTtyStdin:
        def isatty(self) -> bool:
            return False

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "hello"

    monkeypatch.setattr(sys, "stdin", NonTtyStdin())
    monkeypatch.setattr(builtins, "input", fake_input)

    result = anyio.run(_default_prompt_reader, "wisp> ")

    assert result == "hello"
    assert prompts == [""]


def test_tui_shell_quit_then_eof_sends_one_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(
            controller,
            console=_console()[0],
            prompt_reader=await _reader_from(["/quit"]),
        )

        await shell.run()

        assert controller.shutdown_count == 1

    anyio.run(run)


def test_tui_shell_queues_follow_up_while_running() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                ),
                [RpcCommandFinished(command_id="prompt-2", command_type="prompt", ok=True)],
            ]
        )
        inputs = deque(["first", "second"])

        async def read(_prompt: str) -> str:
            if inputs:
                return inputs.popleft()
            await anyio.sleep(0.1)
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.prompts == ["first", "second"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "queued follow-up #1" in rendered
        assert "running queued follow-up" in rendered

    anyio.run(run)


def test_tui_shell_preserves_remaining_fullscreen_follow_up_count() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                ),
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-2", command_type="prompt", ok=True)],
                ),
                [RpcCommandFinished(command_id="prompt-3", command_type="prompt", ok=True)],
            ]
        )
        inputs = deque(["first", "second", "third"])

        async def read(_prompt: str) -> str:
            if inputs:
                return inputs.popleft()
            await anyio.sleep(0.2)
            raise EOFError

        class RecordingFullscreenRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.snapshots: list[TuiViewSnapshot] = []

            def view_updated(self, snapshot: TuiViewSnapshot) -> None:
                self.snapshots.append(snapshot)
                super().view_updated(snapshot)

        renderer = RecordingFullscreenRenderer()
        shell = TuiShell(controller, renderer=renderer, prompt_reader=read)

        await shell.run()

        assert controller.prompts == ["first", "second", "third"]
        assert ("running queued follow-up", 1) in {
            (snapshot.status, snapshot.queued_follow_ups) for snapshot in renderer.snapshots
        }
        assert ("running queued follow-up", 0) in {
            (snapshot.status, snapshot.queued_follow_ups) for snapshot in renderer.snapshots
        }

    anyio.run(run)


def test_tui_shell_discards_queued_follow_ups_after_input_eof() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)],
                )
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["first", "second"]),
        )

        await shell.run()

        assert controller.prompts == ["first"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "queued follow-up #1" in rendered
        assert "running queued follow-up" not in rendered
        assert "input closed; finishing current prompt" in rendered
        assert "waiting for current prompt" not in rendered

    anyio.run(run)


def test_tui_shell_clears_queued_follow_ups_after_failed_prompt() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [
                        RpcCommandFinished(
                            command_id="prompt-1",
                            command_type="prompt",
                            ok=False,
                            error="failed",
                        )
                    ],
                )
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["first", "second"]),
        )

        await shell.run()

        assert controller.prompts == ["first"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "queued follow-up #1" in rendered
        assert "running queued follow-up" not in rendered

    anyio.run(run)
