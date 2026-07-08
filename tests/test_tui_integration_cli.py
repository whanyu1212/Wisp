# ruff: noqa: F403,F405

from __future__ import annotations

from textual.await_complete import AwaitComplete
from textual.command import CommandPalette
from textual.widgets import Input, Static

from tests.tui_support import *
from wisp.events import AgentStarted, RpcCommandStarted
from wisp.tui.commands import parse_tui_slash_command
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui
from wisp.tui.widgets import (
    _ROLE_LABELS,
    LineMessage,
    StreamMessage,
    ToolCard,
    Transcript,
    WorkingMessage,
)


def _transcript_texts(app: TextualTui) -> list[str]:
    """Plain text of every mounted transcript message (line + streamed)."""

    transcript = app.query_one("#transcript", Transcript)
    texts: list[str] = []
    for child in transcript.children:
        if isinstance(child, LineMessage | WorkingMessage | ToolCard):
            texts.append(child.render().plain)  # Textual Content
        elif isinstance(child, StreamMessage):
            texts.append(child._markdown.source)
    return texts


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
    return [(_transcript_role_class(c), c.border_title) for c in transcript.children]


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
    # `wisp tui` gives the agent the full toolset by default — otherwise it's a
    # toolless chatbot that can't read files or run commands.
    assert captured[0].all_tools is True


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
            AssistantMessage(content="hello there"),
            ToolCallRequested(call_id="c1", name="bash", arguments={"cmd": "ls"}),
            ToolResultReady(call_id="c1", name="bash", output="file-a\nfile-b", is_error=False),
            ToolCallRequested(call_id="c3", name="write", arguments={"path": "x"}),
            ToolApprovalResolved(call_id="c3", name="write", approved=False, reason="too risky"),
            ErrorEvent(message="boom"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=False, error="nope"),
        ]
    )

    assert "assistant: hello there" in rendered
    # One card for c1: done glyph + name + first output line (not two lines).
    assert "✓ bash" in rendered
    assert "file-a" in rendered
    assert "file-b" not in rendered  # only the first non-empty output line shows
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
            AssistantMessage(content="the answer"),
        ]
    )

    assert rendered == "assistant: the answer"  # framing events produced no lines
    assert "RpcCommand" not in rendered
    assert "AgentStarted" not in rendered
    assert "command_id" not in rendered


def test_textual_renderer_collapses_call_and_result_into_one_card() -> None:
    # Request then result for one call_id mutate a single card in place (one line,
    # not two). An errored result flips the glyph to ✗ and shows the output line.
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
    assert text.endswith("· 2.5s"), text  # true delta, not the virtual-clock tick count


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
    # TokenDelta is streaming plumbing (assistant text arrives via the streaming
    # path, not event()); showing it in the transcript was the noise bug.
    rendered = _render_events_to_transcript([TokenDelta(delta="raw")])

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
            AssistantMessage(content="hi"),
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
            renderer.event(AssistantMessage(content="after switch"))
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
    # (the shell suppresses the trailing AssistantMessage when tokens rendered).
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
                app_instance._stream_widget,
                app_instance._streaming_text,
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
            AssistantMessage(content="hi"),
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
            AssistantMessage(content="hi"),
            ToolCallRequested(call_id="c1", name="bash", arguments={}),
            ErrorEvent(message="bad"),
        ]
    )
    titles = [title for _, title in cards]
    assert titles == [_ROLE_LABELS["assistant"], _ROLE_LABELS["tool"], _ROLE_LABELS["error"]]


def test_textual_running_mounts_transient_working_row() -> None:
    # Running state should surface inline in the transcript as a dim, untitled
    # working row while we're waiting for the first visible output.
    async def scenario() -> list[tuple[str | None, object]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            return _transcript_cards(app_instance)

    cards = anyio.run(scenario)
    assert cards == [("message--dim", None)]


def test_textual_working_row_animates_spinner_and_counts_elapsed() -> None:
    # The heartbeat is a smooth braille spinner + a live elapsed-seconds counter,
    # both driven off one monotonic tick counter (frame = ticks % len, seconds =
    # ticks × interval). Advance ticks directly and assert the spinner rotates
    # through all its frames and the counter reaches whole seconds.
    async def scenario() -> tuple[int, str, str]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            row = next(c for c in transcript.children if isinstance(c, WorkingMessage))

            start = row.render().plain
            glyphs: set[str] = {start[0]}
            # One full spinner cycle plus enough ticks to cross 1s (interval 0.08s).
            for _ in range(len(WorkingMessage._FRAMES) + 3):
                row._tick()
                glyphs.add(row.render().plain[0])
            return len(glyphs), start, row.render().plain

    distinct_glyphs, start, later = anyio.run(scenario)
    assert distinct_glyphs == len(WorkingMessage._FRAMES)  # every frame shown → smooth
    assert start.endswith("0s")
    assert later.endswith("1s")  # counter advanced with elapsed time
    assert start[0] in WorkingMessage._FRAMES  # a braille frame, not the old dot


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
    async def scenario() -> list[object]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.event(SessionSaved(session_id="sess-1", path=Path("/tmp/sess.json")))
            await pilot.pause()
            return _transcript_cards(app_instance)

    cards = anyio.run(scenario)
    assert cards == []


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


def test_textual_working_row_disappears_on_first_stream_output() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test() as pilot:
            renderer.running()
            await pilot.pause()
            before = _transcript_texts(app_instance)
            renderer.token_delta("hello")
            await pilot.pause()
            after = _transcript_texts(app_instance)
            return before, after

    before, after = anyio.run(scenario)
    assert any("Working" in text for text in before)
    assert all("Working" not in text for text in after)
    assert any("hello" in text for text in after)


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


def test_textual_command_palette_exposes_wisp_commands() -> None:
    # The command palette surfaces the TUI's slash commands alongside Textual's
    # built-ins. Our graceful /quit replaces Textual's raw Quit (which would
    # bypass the shell's shutdown), so there is exactly one Quit entry.
    async def scenario() -> list[str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            return [
                command.title for command in app_instance.get_system_commands(app_instance.screen)
            ]

    titles = anyio.run(scenario)
    for expected in (
        "Help",
        "Quit",
        "Auth status",
        "Provider: show current",
        "Provider: switch…",
        "Model: show current",
        "Model: switch…",
        "Login…",
        "Logout",
    ):
        assert expected in titles
    assert "Theme" in titles  # a Textual built-in survived (yield-from super)
    assert titles.count("Quit") == 1  # Textual's raw Quit was filtered out


def test_textual_command_palette_entries_route_through_the_typed_path() -> None:
    # A palette selection must reach read_prompt exactly as typing the command
    # would, so the shell stays the single source of command semantics.
    async def scenario() -> str:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            async with anyio.create_task_group() as tg:
                results: list[str] = []

                async def read() -> None:
                    results.append(await app_instance.read_prompt("wisp> "))

                tg.start_soon(read)
                await pilot.pause()
                app_instance.submit_command_line("/help")
            return results[0]

    assert anyio.run(scenario) == "/help"


def test_textual_prefill_command_sets_input_without_submitting() -> None:
    # An arg-bearing palette entry (Model: switch…) prefills the Input for the
    # user to complete — it must NOT submit, so a pending read stays pending.
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


def test_textual_palette_command_strings_are_valid_slash_commands() -> None:
    # Guard against drift: every command the palette submits (not the prefill
    # stubs) must parse as a real slash command.
    for text in ("/help", "/quit", "/auth", "/provider", "/model", "/logout"):
        assert parse_tui_slash_command(text) is not None
    # The prefill stubs are valid command prefixes (parse once a value is added).
    assert parse_tui_slash_command("/model gpt-5.5") is not None
    assert parse_tui_slash_command("/provider fake") is not None


def test_textual_slash_on_empty_input_opens_the_palette() -> None:
    # Typing "/" as the whole input opens the command palette (Claude-Code style)
    # and clears the stray slash from the input.
    async def scenario() -> tuple[bool, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.focus()
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()
            return CommandPalette.is_open(app_instance), input_widget.value

    palette_open, value = anyio.run(scenario)
    assert palette_open
    assert value == ""  # the "/" was consumed, not left in the input


def test_textual_slash_mid_text_does_not_open_the_palette() -> None:
    # A "/" that isn't the entire input (e.g. a URL, or a path) must not hijack.
    async def scenario() -> bool:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            input_widget = app_instance.query_one("#input", Input)
            input_widget.value = "http:/"  # value is not exactly "/"
            await pilot.pause()
            return CommandPalette.is_open(app_instance)

    assert anyio.run(scenario) is False


def test_textual_startup_shows_the_wordmark_banner() -> None:
    # startup() renders the wordmark as an accent-colored, borderless banner plus
    # the tagline — the greeting, distinct from a normal message card.
    async def scenario() -> tuple[list[str | None], list[str]]:
        app_instance, renderer = create_textual_tui()
        async with app_instance.run_test(size=(72, 20)) as pilot:
            renderer.startup()
            await pilot.pause()
            transcript = app_instance.query_one("#transcript", Transcript)
            roles = [_transcript_role_class(c) for c in transcript.children]
            texts = _transcript_texts(app_instance)
            return roles, texts

    roles, texts = anyio.run(scenario)
    assert "message--banner" in roles  # the wordmark is a banner, not a card
    # The banner carries the block-drawing wordmark; a single tightened greeting
    # line follows — tagline + the `/` command door + how to quit, all in one row.
    assert any("▄" in t for t in texts)
    greeting = [t for t in texts if "a quiet coding agent" in t]
    assert len(greeting) == 1
    assert "press / for commands" in greeting[0]
    assert "/quit to exit" in greeting[0]


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


def test_textual_run_shell_disables_mouse_for_native_copy() -> None:
    # Copy is delegated to the terminal: the shell must start with mouse reporting
    # off so the emulator keeps click-drag selection and the OS copy shortcut
    # (Cmd+C / right-click-copy) works natively. Assert run_async is invoked with
    # mouse=False rather than driving a real terminal.
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
    assert captured["mouse"] is False


def test_textual_header_shows_the_wisp_wordmark() -> None:
    # The header title is the lowercase wordmark; the clock chrome is gone.
    async def scenario() -> tuple[str, str]:
        app_instance = TextualTui()
        async with app_instance.run_test() as pilot:
            await pilot.pause()
            return app_instance.title, app_instance.sub_title

    title, sub_title = anyio.run(scenario)
    assert title == "wisp"
    assert sub_title == "a quiet coding agent"


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
