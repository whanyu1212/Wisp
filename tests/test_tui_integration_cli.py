# ruff: noqa: F403,F405

from __future__ import annotations

from textual.widgets import Input, RichLog

from tests.tui_support import *
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui


def test_tui_rpc_command_includes_runtime_flags(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(
                provider="fake", model="model-x", session_dir=tmp_path, auth_path=auth_path
            ),
            allow_read_tools=True,
            allowed_tools=("bash",),
            resume="session-123",
            approve_unsafe_tools=True,
            max_tool_iterations=3,
        )
    )

    assert command[:4] == (command[0], "-m", "wisp", "--mode")
    assert "rpc" in command
    assert ("--provider", "fake") == (
        command[command.index("--provider")],
        command[command.index("--provider") + 1],
    )
    assert ("--model", "model-x") == (
        command[command.index("--model")],
        command[command.index("--model") + 1],
    )
    assert ("--session-dir", str(tmp_path)) == (
        command[command.index("--session-dir")],
        command[command.index("--session-dir") + 1],
    )
    assert ("--auth-file", str(auth_path)) == (
        command[command.index("--auth-file")],
        command[command.index("--auth-file") + 1],
    )
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


def test_tui_rpc_command_includes_continue_latest(tmp_path: Path) -> None:
    command = _rpc_command(
        TuiOptions(
            config=WispConfig(provider="fake", session_dir=tmp_path),
            continue_latest=True,
        )
    )

    assert "--continue" in command


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


def test_textual_tui_escapes_markup_in_streamed_output() -> None:
    async def scenario() -> str:
        app_instance = TextualTui()
        async with app_instance.run_test():
            app_instance.append_stream("code has [brackets] and [/close] tags")
            app_instance.flush_stream()
            # The transcript must render literally; bracketed text must not be
            # interpreted as Rich markup (which would drop or mangle it).
            transcript = app_instance.query_one("#transcript", RichLog)
            return "".join(strip.text for strip in transcript.lines)

    rendered = anyio.run(scenario)
    assert "[brackets]" in rendered
    assert "[/close]" in rendered


def _render_events_to_transcript(events: list[object]) -> str:
    # Drive TextualTuiRenderer.event() through a live app and return the plain
    # rendered transcript text (markup already resolved by RichLog).
    async def scenario() -> str:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test():
            for event in events:
                renderer.event(event)
            transcript = app_instance.query_one("#transcript", RichLog)
            return "\n".join("".join(segment.text for segment in line) for line in transcript.lines)

    return anyio.run(scenario)


def test_textual_renderer_dispatches_events_by_type() -> None:
    # Stage 0: each event type must render as its own distinct, labeled line,
    # not a single undifferentiated str(event) repr.
    rendered = _render_events_to_transcript(
        [
            AssistantMessage(content="hello there"),
            ToolCallRequested(call_id="c1", name="bash", arguments={"cmd": "ls"}),
            ToolResultReady(call_id="c1", name="bash", output="file-a\nfile-b", is_error=False),
            ToolApprovalResolved(call_id="c2", name="edit", approved=True, reason=None),
            ToolApprovalResolved(call_id="c3", name="write", approved=False, reason="too risky"),
            ErrorEvent(message="boom"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=False, error="nope"),
        ]
    )

    assert "assistant: hello there" in rendered
    assert "→ tool bash" in rendered
    # ToolResultReady shows only the first non-empty output line.
    assert "✓ tool bash: file-a" in rendered
    assert "file-b" not in rendered
    assert "✓ approved edit" in rendered
    assert "! denied write: too risky" in rendered
    assert "error: boom" in rendered
    assert "command failed: nope" in rendered


def test_textual_renderer_distinguishes_tool_call_from_result() -> None:
    # The old duck-typed event() collapsed these into indistinguishable lines.
    rendered = _render_events_to_transcript(
        [
            ToolCallRequested(call_id="c1", name="grep", arguments={}),
            ToolResultReady(call_id="c1", name="grep", output="match", is_error=True),
        ]
    )

    assert "→ tool grep" in rendered
    assert "✗ tool grep: match" in rendered


def test_textual_renderer_escapes_untrusted_event_payloads() -> None:
    # Tool-controlled fields must not inject Rich markup into the RichLog.
    rendered = _render_events_to_transcript(
        [
            ToolCallRequested(call_id="c1", name="evil[/blue]", arguments={"k": "[red]x[/red]"}),
            ToolResultReady(call_id="c1", name="t", output="[bold]out[/bold]", is_error=False),
        ]
    )

    assert "evil[/blue]" in rendered
    assert "[red]x[/red]" in rendered
    assert "[bold]out[/bold]" in rendered


def test_textual_renderer_falls_back_for_unhandled_events() -> None:
    # An event type with no dedicated branch still renders (escaped) rather than
    # vanishing — matching the previous fallback behavior.
    rendered = _render_events_to_transcript([TokenDelta(delta="raw")])

    assert "raw" in rendered


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
