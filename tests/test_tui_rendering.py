# ruff: noqa: F403,F405

from __future__ import annotations

from tests.tui_support import *
from wisp.events import ProviderRetrying


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


def test_line_tui_renderer_prints_retry_progress() -> None:
    console, output = _console()
    renderer = LineTuiRenderer(console)

    renderer.event(
        ProviderRetrying(
            turn=1,
            provider="openai",
            attempt=2,
            max_attempts=3,
            delay_seconds=0.5,
            reason="rate_limit",
            status_code=429,
        )
    )

    rendered = output.getvalue()
    assert "retrying openai: rate_limit (429)" in rendered
    assert "attempt 2/3 in 0.5s" in rendered


def test_fullscreen_tui_renderer_keeps_retry_progress_out_of_transcript() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.event(
        ProviderRetrying(
            turn=1,
            provider="openai",
            attempt=2,
            max_attempts=3,
            delay_seconds=0.5,
            reason="rate_limit",
        )
    )

    assert renderer.state.transcript == []


def test_tui_trust_on_closed_input_sends_transient_denial() -> None:
    # Regression: when input has already closed and a TrustRequested arrives, the shell
    # must answer trusted=False as a transient denial. The RPC gate persists explicit
    # "no" answers, including those with explanatory reasons.
    async def run() -> None:
        controller = ScriptedController()
        renderer = LineTuiRenderer(_console()[0])
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from([]),
        )
        shell.state.input_closed = True

        await shell._handle_rpc_event(
            TrustRequested(request_id="req-1", project_path="/some/project")
        )

        assert controller.trusts == [("req-1", False, "Trust prompt: input closed", True)]
        request_id, trusted, reason, transient = controller.trusts[0]
        assert trusted is False
        assert reason is not None
        assert transient is True

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
            cwd=str(tmp_path),
            provider="openai",
            model="gpt-test",
        )
    )
    renderer.event(SessionSaved(session_id="session", path=tmp_path / "session.jsonl"))

    rendered = output.getvalue()
    assert "Transcript" in rendered
    assert "Editor" in rendered
    assert "openai/gpt-test" in rendered
    assert "session: session.jsonl" in rendered
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
            cwd="/tmp/project",
            provider="openai",
            model="gpt-test",
        )
    )

    assert renderer.state.status == "waiting for approval"
    assert renderer.state.input_hint == "approve? [y/N] "
    assert renderer.state.input_mode == "idle"
    assert renderer.state.queued_follow_ups == 2
    assert renderer.state.last_session == "session.jsonl"
    assert renderer.state.cwd == "/tmp/project"
    assert renderer.state.provider == "openai"
    assert renderer.state.model == "gpt-test"


def test_tui_footer_formatter_compacts_and_truncates() -> None:
    lines = format_tui_footer_lines(
        TuiViewSnapshot(
            status="running",
            input_hint="wisp(running)> ",
            queued_follow_ups=12,
            last_session="session.jsonl",
            cwd="/very/long/project/path/that/will/not/fit",
            provider="openai",
            model="gpt-4.1",
        ),
        width=32,
    )

    assert len(lines) == 2
    assert all(len(line) <= 32 for line in lines)
    assert "…" in lines[0]
    assert lines[1].endswith("openai/gpt-4.1")


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


def test_fullscreen_tui_renderer_transcript_view_defaults_to_latest() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False, transcript_view_entries=3)
    for index in range(5):
        renderer.event(completed_message(content=f"message {index}"))

    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 2",
        "message 3",
        "message 4",
    ]
    assert renderer.state.transcript_scroll_offset == 0
    assert "message 0" not in renderer._transcript_text().plain
    assert renderer._transcript_title() == "Transcript (latest)"


def test_fullscreen_tui_renderer_scrolls_transcript_and_clamps() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False, transcript_view_entries=3)
    for index in range(5):
        renderer.event(completed_message(content=f"message {index}"))

    renderer.scroll_transcript_up(1)

    assert renderer.state.transcript_scroll_offset == 1
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 1",
        "message 2",
        "message 3",
    ]

    renderer.scroll_transcript_top()

    assert renderer.state.transcript_scroll_offset == 2
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 0",
        "message 1",
        "message 2",
    ]

    renderer.scroll_transcript_down(10)

    assert renderer.state.transcript_scroll_offset == 0
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 2",
        "message 3",
        "message 4",
    ]


def test_fullscreen_tui_renderer_preserves_scrolled_view_during_new_output() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False, transcript_view_entries=3)
    for index in range(5):
        renderer.event(completed_message(content=f"message {index}"))
    renderer.scroll_transcript_up(1)

    renderer.event(completed_message(content="message 5"))

    assert renderer.state.transcript_scroll_offset == 2
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 1",
        "message 2",
        "message 3",
    ]

    renderer.token_delta("streaming")

    assert renderer.state.transcript_scroll_offset == 3
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 1",
        "message 2",
        "message 3",
    ]

    renderer.end_token_stream()

    assert renderer.state.transcript_scroll_offset == 3
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 1",
        "message 2",
        "message 3",
    ]

    renderer.scroll_transcript_bottom()

    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 4",
        "message 5",
        "streaming",
    ]


def test_fullscreen_tui_renderer_preserves_scrolled_view_when_pruning_cap() -> None:
    renderer = FullscreenTuiRenderer(
        _console()[0],
        clear_screen=False,
        max_transcript_entries=5,
        transcript_view_entries=3,
    )
    for index in range(5):
        renderer.event(completed_message(content=f"message {index}"))
    renderer.scroll_transcript_up(1)

    renderer.event(completed_message(content="message 5"))

    assert [entry.content for entry in renderer.state.transcript] == [
        "message 1",
        "message 2",
        "message 3",
        "message 4",
        "message 5",
    ]
    assert renderer.state.transcript_scroll_offset == 2
    assert [entry.content for entry in renderer._visible_transcript_entries()] == [
        "message 1",
        "message 2",
        "message 3",
    ]


def test_fullscreen_tui_renderer_keeps_footer_visible_while_scrolled() -> None:
    console, output = _console()
    renderer = FullscreenTuiRenderer(console, clear_screen=False, transcript_view_entries=2)
    renderer.view_updated(
        TuiViewSnapshot(
            status="running",
            input_hint="wisp(running)> ",
            queued_follow_ups=1,
            provider="openai",
            model="gpt-test",
        )
    )
    for index in range(4):
        renderer.event(completed_message(content=f"message {index}"))

    renderer.scroll_transcript_up(1)

    rendered = output.getvalue()
    assert "Transcript" in rendered
    assert "Editor" in rendered
    assert "running • queued 1" in rendered
    assert "openai/gpt-test" in rendered
    assert "wisp(running)> " in rendered


def test_create_tui_renderer_selects_fullscreen_renderer() -> None:
    renderer = create_tui_renderer(TuiRendererKind.fullscreen, _console()[0])

    assert isinstance(renderer, FullscreenTuiRenderer)
