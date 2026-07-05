# ruff: noqa: F403,F405

from __future__ import annotations

from pytest import MonkeyPatch

from tests.tui_support import *
from wisp.auth.storage import OAuthCredential
from wisp.tui import shell as tui_shell_module


def test_tui_shell_records_submitted_prompt_for_fullscreen_renderer() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    AssistantMessage(content="answer"),
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
                    AssistantMessage(content="fullscreen response"),
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


def test_tui_shell_runs_prompt_then_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    TokenDelta(delta="hello"),
                    AssistantMessage(content="hello"),
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
        assert "Provider set to openai-codex" in rendered
        assert "Model set to gpt-5.5" in rendered
        assert "Current provider: openai-codex" in rendered
        assert "Current model: gpt-5.5" in rendered

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
