# ruff: noqa: F403,F405

from __future__ import annotations

from tests.tui_support import *


def test_tui_shell_uses_injected_renderer() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.calls: list[str] = []

        def __bool__(self) -> bool:
            return False

        def startup(self) -> None:
            self.calls.append("startup")

        def running(self) -> None:
            self.calls.append("running")

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert renderer.calls == ["startup", "running"]
        assert controller.prompts == ["hello"]

    anyio.run(run)


def test_fullscreen_tui_renderer_renders_layout_regions(tmp_path: Path) -> None:
    console, output = _console()
    renderer = FullscreenTuiRenderer(console, clear_screen=False)

    renderer.startup()
    renderer.running()
    renderer.token_delta("hello")
    renderer.end_token_stream()
    renderer.view_updated(
        TuiViewSnapshot(
            status="idle",
            input_hint="wisp> ",
            last_session="session.jsonl",
        )
    )
    renderer.event(SessionSaved(session_id="session", path=tmp_path / "session.jsonl"))

    rendered = output.getvalue()
    assert "Transcript" in rendered
    assert "Status" in rendered
    assert "Input" in rendered
    assert "hello" in rendered
    assert "session saved: session.jsonl" in rendered
    assert renderer.state.last_session == "session.jsonl"


def test_fullscreen_tui_renderer_does_not_clear_terminal_by_default() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=80)
    renderer = FullscreenTuiRenderer(console)

    renderer.startup()

    assert renderer.clear_screen is False
    assert "\x1b[2J" not in output.getvalue()


def test_fullscreen_tui_renderer_coalesces_streaming_token_redraws() -> None:
    console, output = _console()
    renderer = FullscreenTuiRenderer(console, clear_screen=False)

    renderer.startup()
    renderer.running()
    before_tokens = output.getvalue()

    renderer.token_delta("hel")
    renderer.token_delta("lo")

    assert renderer.state.streaming_text == "hello"
    assert output.getvalue() == before_tokens

    renderer.end_token_stream()

    assert renderer.state.streaming_text == ""
    assert "hello" in output.getvalue()


def test_fullscreen_tui_renderer_applies_view_snapshot() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.view_updated(
        TuiViewSnapshot(
            status="waiting for approval",
            input_hint="approve? [y/N] ",
            queued_follow_ups=2,
            last_session="session.jsonl",
        )
    )

    assert renderer.state.status == "waiting for approval"
    assert renderer.state.input_hint == "approve? [y/N] "
    assert renderer.state.input_mode == "idle"
    assert renderer.state.queued_follow_ups == 2
    assert renderer.state.last_session == "session.jsonl"


def test_fullscreen_tui_renderer_messages_do_not_infer_footer_state() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    renderer.view_updated(
        TuiViewSnapshot(
            status="running",
            input_hint="wisp(running)> ",
            queued_follow_ups=1,
        )
    )

    renderer.cancelled()
    renderer.input_closed_finishing_prompt()
    renderer.event(RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True))

    assert renderer.state.status == "running"
    assert renderer.state.input_hint == "wisp(running)> "
    assert renderer.state.queued_follow_ups == 1


def test_create_tui_renderer_selects_fullscreen_renderer() -> None:
    renderer = create_tui_renderer(TuiRendererKind.fullscreen, _console()[0])

    assert isinstance(renderer, FullscreenTuiRenderer)
