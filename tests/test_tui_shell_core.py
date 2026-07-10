# ruff: noqa: F403,F405

from __future__ import annotations

from pytest import MonkeyPatch

from tests.tui_support import *
from wisp.auth.storage import OAuthCredential
from wisp.events import MessageStarted, ProviderRetrying
from wisp.tui import shell as tui_shell_module


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

    monkeypatch.setattr(tui_shell_module, "login_openai_codex", fake_login)
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

    monkeypatch.setattr(tui_shell_module, "login_openai_codex", fake_login)

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

    monkeypatch.setattr(tui_shell_module, "login_openai_codex", fake_login)

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

        assert controller.configurations == [("openai-codex", None), (None, "gpt-5.5")]
        rendered = output.getvalue()
        assert "Configuring provider: openai-codex" in rendered
        assert "Provider set to openai-codex" in rendered
        assert "Configuring model: gpt-5.5" in rendered
        assert "Model set to gpt-5.5" in rendered

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

        assert controller.configurations == [("missing", None)]
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
