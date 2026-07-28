# ruff: noqa: F403,F405

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.tui_support import *
from wisp.events import (
    ContextBudget,
    ContextEstimate,
    MessageRole,
    ProviderRetrying,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    SessionCostSummary,
)
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    HistoricalToolCard,
    HistoricalTranscriptMessage,
    history_entries_from_rpc_messages,
    history_from_rpc_messages,
)


def _rpc_message(
    role: MessageRole,
    content: str,
    *,
    entry_id: str,
    content_truncated: bool = False,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    tool_calls: tuple[RpcMessageToolCallSnapshot, ...] = (),
    is_error: bool | None = None,
    tool_result: RpcMessageToolResultSnapshot | None = None,
) -> RpcMessageSnapshot:
    return RpcMessageSnapshot(
        entry_id=entry_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        role=role,
        content=content,
        content_original_bytes=len(content.encode("utf-8")),
        content_truncated=content_truncated,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_calls=tool_calls,
        is_error=is_error,
        tool_result=tool_result,
    )


def _context_budget(
    *,
    estimated: int = 11_500,
    observed: int | None = 12_000,
    current: bool = True,
    window: int | None = 128_000,
    percent: float | None = 9.0,
) -> ContextBudget:
    return ContextBudget.model_construct(
        estimate=ContextEstimate.model_construct(total_tokens=estimated),
        observed_tokens=observed,
        observed_is_current=current,
        context_window=window,
        reserve_tokens=8_000,
        remaining_tokens=None,
        estimated_percent=percent,
        over_budget=False,
    )


def test_history_from_rpc_messages_filters_to_visible_user_and_assistant_text() -> None:
    history = history_from_rpc_messages(
        (
            _rpc_message("system", "system prompt", entry_id="system-1"),
            _rpc_message("user", "hello", entry_id="user-1"),
            _rpc_message("assistant", "", entry_id="assistant-tools-only"),
            _rpc_message(
                "assistant",
                "",
                entry_id="assistant-empty-truncated",
                content_truncated=True,
            ),
            _rpc_message(
                "assistant",
                "long answer",
                entry_id="assistant-1",
                content_truncated=True,
            ),
            _rpc_message("tool", "tool output", entry_id="tool-1"),
        )
    )

    assert history == (
        HistoricalTranscriptMessage(role="user", content="hello"),
        HistoricalTranscriptMessage(role="assistant", content="[content truncated]"),
        HistoricalTranscriptMessage(role="assistant", content="long answer\n[content truncated]"),
    )


def test_history_entries_from_rpc_messages_pairs_tool_calls_and_results() -> None:
    entries = history_entries_from_rpc_messages(
        (
            _rpc_message("user", "old prompt", entry_id="user-1"),
            _rpc_message(
                "assistant",
                "I'll inspect it.",
                entry_id="assistant-1",
                tool_calls=(
                    RpcMessageToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "app.py"},
                        arguments_original_bytes=17,
                    ),
                ),
            ),
            _rpc_message(
                "tool",
                "file contents",
                entry_id="tool-1",
                tool_call_id="call-1",
                tool_name="read",
                tool_result=RpcMessageToolResultSnapshot(
                    summary="read 3 lines from app.py",
                    truncated=True,
                ),
            ),
        )
    )

    assert entries == (
        HistoricalTranscriptMessage(role="user", content="old prompt"),
        HistoricalTranscriptMessage(role="assistant", content="I'll inspect it."),
        HistoricalToolCard(
            card_id="history:tool-1",
            name="read",
            arguments={"path": "app.py"},
            output="file contents",
            is_error=False,
            summary="read 3 lines from app.py",
            truncated=True,
        ),
    )
    assert history_from_rpc_messages(tuple()) == ()


def test_history_entries_from_rpc_messages_handles_orphan_and_missing_tool_results() -> None:
    entries = history_entries_from_rpc_messages(
        (
            _rpc_message(
                "tool",
                "orphan output",
                entry_id="tool-1",
                tool_call_id="orphan",
                tool_name="bash",
                is_error=True,
                content_truncated=True,
                tool_result=RpcMessageToolResultSnapshot(exit_code=2),
            ),
            _rpc_message(
                "assistant",
                "",
                entry_id="assistant-1",
                tool_calls=(
                    RpcMessageToolCallSnapshot(
                        call_id="missing",
                        name="grep",
                        arguments={"pattern": "x"},
                        arguments_original_bytes=15,
                    ),
                ),
            ),
        )
    )

    assert entries == (
        HistoricalToolCard(
            card_id="history:tool-1",
            name="bash",
            arguments={},
            output="orphan output\n[content truncated]",
            is_error=True,
            exit_code=2,
            truncated=True,
        ),
        HistoricalToolCard(
            card_id="history:missing:missing",
            name="grep",
            arguments={"pattern": "x"},
            output="No persisted tool result.",
            is_error=True,
            status="cancelled",
            missing_result=True,
        ),
    )


def test_tui_renderers_render_hydrated_history() -> None:
    messages = (
        HistoricalTranscriptMessage(role="user", content="old [red]prompt[/red]"),
        HistoricalTranscriptMessage(role="assistant", content="old answer"),
    )
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    line.render_history(messages)
    fullscreen.render_history(messages)

    rendered = output.getvalue()
    assert "you: old [red]prompt[/red]" in rendered
    assert "assistant: old answer" in rendered
    assert [(entry.role, entry.content) for entry in fullscreen.state.transcript] == [
        ("user", "old [red]prompt[/red]"),
        ("assistant", "old answer"),
    ]


def test_line_and_fullscreen_renderers_render_historical_tool_entries() -> None:
    entries = (
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
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    line.render_history_entries(entries)
    fullscreen.render_history_entries(entries)

    rendered = output.getvalue()
    assert "you: old prompt" in rendered
    assert "✗ historical tool bash: exit 1: [red]boom[/red]" in rendered
    assert [(entry.role, entry.content) for entry in fullscreen.state.transcript] == [
        ("user", "old prompt"),
        ("tool", "bash: exit 1: [red]boom[/red]"),
    ]


def test_fullscreen_renderers_retain_the_full_hydrated_page_by_default() -> None:
    messages = tuple(
        HistoricalTranscriptMessage(role="user", content=f"message {index}")
        for index in range(TUI_HISTORY_MESSAGE_LIMIT)
    )
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.render_history(messages)

    assert len(renderer.state.transcript) == TUI_HISTORY_MESSAGE_LIMIT
    assert renderer.state.transcript[0].content == "message 0"
    assert renderer.state.transcript[-1].content == f"message {TUI_HISTORY_MESSAGE_LIMIT - 1}"
    assert LiveFullscreenTui(run_application=False).max_transcript_entries == (
        TUI_HISTORY_MESSAGE_LIMIT
    )


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


def test_line_tui_renderer_renders_compaction_notices() -> None:
    console, output = _console()
    renderer = LineTuiRenderer(console)

    renderer.event(CompactionStarted(session_id="session-1", source_entry_count=6))
    renderer.event(
        CompactionCompleted(
            session_id="session-1",
            outcome="completed",
            replaced_entry_count=5,
            retained_entry_count=1,
        )
    )

    rendered = output.getvalue()
    assert "Compacting session..." in rendered
    assert "Compacted 5 context entries." in rendered


def test_line_tui_renderer_renders_threshold_compaction_as_automatic_notices() -> None:
    console, output = _console()
    renderer = LineTuiRenderer(console)

    renderer.event(
        CompactionStarted(
            session_id="session-1",
            reason="threshold",
            source_entry_count=6,
            trigger_budget=_context_budget(),
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
    renderer.event(
        CompactionCompleted(
            session_id="session-1",
            reason="threshold",
            outcome="completed",
            replaced_entry_count=5,
            retained_entry_count=1,
            error="Event publication failed: listener failed",
        )
    )

    rendered = output.getvalue()
    assert "Context threshold reached; compacting automatically..." in rendered
    assert "Automatically compacted 5 context entries." in rendered
    assert "Automatic compaction failed: summary failed" in rendered
    assert "Warning: Event publication failed: listener failed" in rendered


def test_tui_renderers_distinguish_overflow_recovery() -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    started = CompactionStarted(
        session_id="session-1",
        reason="overflow",
        source_entry_count=6,
        trigger_budget=_context_budget(),
    )
    completed = CompactionCompleted(
        session_id="session-1",
        reason="overflow",
        outcome="completed",
        replaced_entry_count=5,
        retained_entry_count=1,
        will_retry=True,
    )

    for renderer in (line, fullscreen):
        renderer.event(started)
        renderer.event(completed)

    rendered = output.getvalue()
    assert "Context overflow detected; compacting before one retry..." in rendered
    assert "Compacted 5 context entries; retrying request..." in rendered
    assert [(entry.role, entry.content, entry.style) for entry in fullscreen.state.transcript] == [
        ("system", "Context overflow detected; compacting before one retry...", "cyan"),
        ("system", "Compacted 5 context entries; retrying request...", "cyan"),
    ]


def test_tui_renderers_show_unstarted_overflow_retry_as_an_error() -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    event = CompactionCompleted(
        session_id="session-1",
        reason="overflow",
        outcome="completed",
        replaced_entry_count=5,
        retained_entry_count=1,
        error="replay reload failed",
    )

    line.event(event)
    fullscreen.event(event)

    assert "Context overflow recovery failed: replay reload failed" in output.getvalue()
    assert [(entry.role, entry.style) for entry in fullscreen.state.transcript] == [
        ("error", "red")
    ]


def test_tui_renderers_deduplicate_terminal_overflow_recovery_errors() -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    failure = CompactionCompleted(
        session_id="session-1",
        reason="overflow",
        outcome="completed",
        replaced_entry_count=5,
        retained_entry_count=1,
        error="replay reload failed",
    )
    error = ErrorEvent(message="Context overflow recovery failed: replay reload failed")
    finished = RpcCommandFinished(
        command_id="prompt-1",
        command_type="prompt",
        ok=False,
        error=error.message,
    )

    for renderer in (line, fullscreen):
        renderer.event(failure)
        renderer.event(error)
        renderer.event(finished)

    assert output.getvalue().count("Context overflow recovery failed") == 1
    assert len(fullscreen.state.transcript) == 1


def test_fullscreen_tui_renderer_keeps_history_and_adds_compaction_notices() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    renderer.event(completed_message(content="visible answer"))

    renderer.event(CompactionStarted(session_id="session-1", source_entry_count=6))
    renderer.event(
        CompactionCompleted(
            session_id="session-1",
            outcome="completed",
            replaced_entry_count=5,
            retained_entry_count=1,
        )
    )

    assert [(entry.role, entry.content) for entry in renderer.state.transcript] == [
        ("assistant", "visible answer"),
        ("system", "Compacting session..."),
        ("system", "Compacted 5 context entries."),
    ]


def test_fullscreen_tui_renderer_renders_threshold_failure_as_notice() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    renderer.event(
        CompactionStarted(
            session_id="session-1",
            reason="threshold",
            source_entry_count=6,
            trigger_budget=_context_budget(),
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

    assert [(entry.role, entry.content, entry.style) for entry in renderer.state.transcript] == [
        ("system", "Context threshold reached; compacting automatically...", "cyan"),
        ("system", "Automatically compacted 5 context entries.", "cyan"),
        ("system", "Automatic compaction failed: summary failed", "yellow"),
    ]


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
    # A short cwd here (rather than the long pytest tmp_path) so all four
    # footer fields fit without triggering issue #72's cwd-outranks-session
    # priority truncation — this test is a layout smoke test, not a
    # footer-priority test (see test_tui_footer_line_one_drops_session_before
    # _truncating_cwd for that).
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
            cwd="/tmp",
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
    context = _context_budget()

    renderer.view_updated(
        TuiViewSnapshot(
            status="waiting for approval",
            input_hint="approve? [y/N] ",
            queued_follow_ups=2,
            last_session="session.jsonl",
            cwd="/tmp/project",
            provider="openai",
            model="gpt-test",
            context=context,
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
    assert renderer.state.context is context


def test_tui_footer_formats_current_stale_and_unknown_context() -> None:
    current = format_tui_footer_lines(
        TuiViewSnapshot(
            status="idle",
            input_hint="wisp> ",
            context=_context_budget(),
        ),
        width=40,
    )[1]
    stale = format_tui_footer_lines(
        TuiViewSnapshot(
            status="idle",
            input_hint="wisp> ",
            context=_context_budget(estimated=14_000, current=False),
        ),
        width=40,
    )[1]
    unknown = format_tui_footer_lines(
        TuiViewSnapshot(
            status="idle",
            input_hint="wisp> ",
            context=_context_budget(observed=None, current=False, window=None, percent=None),
        ),
        width=40,
    )[1]

    assert "ctx 12k/128k" in current
    assert "ctx ~14k/128k" in stale
    assert "ctx ~12k" in unknown


def test_tui_footer_context_gauge_is_responsive_without_displacing_status() -> None:
    snapshot = TuiViewSnapshot(
        status="idle",
        input_hint="wisp> ",
        provider="openai",
        model="gpt",
        context=_context_budget(),
    )

    wide = format_tui_footer_lines(snapshot, width=80)[1]
    compact = format_tui_footer_lines(snapshot, width=34)[1]
    narrow = format_tui_footer_lines(snapshot, width=20)[1]

    assert "ctx 12k/128k (9%)" in wide
    assert "ctx 12k/128k" in compact
    assert "(9%)" not in compact
    assert "ctx 12k/128k" in narrow

    protected = format_tui_footer_lines(
        TuiViewSnapshot(
            status="running",
            input_hint="wisp(running)> ",
            queued_follow_ups=12,
            provider="openai",
            model="gpt",
            context=_context_budget(),
        ),
        width=24,
    )[1]
    assert protected == "running • queued 12"


def test_tui_footer_formats_complete_and_partial_costs_responsively() -> None:
    complete = SessionCostSummary(
        known_usd=Decimal("0.042"),
        priced_record_count=1,
    )
    partial = SessionCostSummary(
        known_usd=Decimal("0.042"),
        complete=False,
        priced_record_count=1,
        unpriced_record_count=1,
    )
    snapshot = TuiViewSnapshot(
        status="idle",
        input_hint="wisp> ",
        provider="openai",
        model="gpt",
        context=_context_budget(),
        cost=complete,
    )

    assert "cost $0.042" in format_tui_footer_lines(snapshot, width=100)[1]
    assert (
        "cost ≥$0.042"
        in format_tui_footer_lines(
            TuiViewSnapshot(
                status="idle",
                input_hint="wisp> ",
                context=_context_budget(),
                cost=partial,
            ),
            width=80,
        )[1]
    )
    narrow = format_tui_footer_lines(snapshot, width=20)[1]
    assert "ctx 12k/128k" in narrow
    assert "cost" not in narrow


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
    # status outranks model (issue #72): the model is dropped entirely here
    # rather than kept whole at the cost of clipping live status text.
    assert lines[1] == "running • queued 12"
    assert "openai/gpt-4.1" not in lines[1]


def test_tui_footer_line_one_drops_session_before_truncating_cwd() -> None:
    # cwd outranks session id (issue #72): when both don't fit, session drops
    # entirely rather than surviving as a truncated, unreadable fragment.
    lines = format_tui_footer_lines(
        TuiViewSnapshot(
            status="idle",
            input_hint="wisp> ",
            last_session="01JZ8K2Q8F7W9WISP4M2.jsonl",
            cwd="/very/long/project/path/that/will/not/fit/alongside/a/session/id",
        ),
        width=32,
    )

    assert "session:" not in lines[0]
    assert lines[0].startswith("/very/long/project/path")
    assert len(lines[0]) <= 32


def test_tui_footer_line_two_still_protects_status_over_model() -> None:
    # status/queued outranks provider+model (issue #72): under pressure the
    # model string truncates, or is dropped entirely, before live status text
    # is ever clipped — status must render whole whenever it fits alone.
    lines = format_tui_footer_lines(
        TuiViewSnapshot(
            status="running",
            input_hint="wisp(running)> ",
            provider="openai-codex",
            model="gpt-5.5-codex-preview-with-a-long-suffix",
        ),
        width=30,
    )

    assert lines[1] == "running"  # status renders whole; model dropped, not truncated
    assert "openai-codex" not in lines[1]

    # A long status still wins over the model even where a naive "protect the
    # model" implementation would keep it whole and clip status instead — the
    # exact regression a P2 review caught: priority="right" on this line
    # protects model_right (the lower-priority field), not status_left.
    long_status_lines = format_tui_footer_lines(
        TuiViewSnapshot(
            status="Retrying openai-codex, attempt 2 of 3, waiting 2.0s",
            input_hint="wisp(running)> ",
            provider="openai-codex",
            model="gpt-5.5-codex",
        ),
        width=40,
    )

    assert long_status_lines[1].startswith(
        "Retrying openai-codex, attempt 2 of 3, "
    )  # status rendered whole (only its trailing ellipsis is clipped)
    assert "openai-codex/gpt-5.5-codex" not in long_status_lines[1]  # model dropped
    assert len(long_status_lines[1]) <= 40


def test_tui_notice_role_uses_a_distinct_color_from_tool() -> None:
    # Issue #72: notice and tool previously shared the accent color, making
    # them visually identical. notice now uses the (previously unused)
    # warning token instead, in both themes.
    from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT, role_styles

    for theme in (WISP_THEME_DARK, WISP_THEME_LIGHT):
        styles = role_styles(theme)
        assert styles["notice"] != styles["tool"]
        assert theme.warning in styles["notice"]
        assert theme.accent in styles["tool"]


def test_denied_and_error_tool_cards_have_distinct_glyph_and_label() -> None:
    # Issue #76: a user-denied tool call and a genuine execution error
    # previously shared the same glyph ("✗"), the same "denied" CSS role
    # class, and (denied falling through to the generic "tool" label) the
    # same border title — visually indistinguishable apart from buried detail
    # text. denied now gets its own glyph and label; error gets its own role
    # class. All three signals (glyph, label, color-driving role) now differ.
    from wisp.tui.widgets import _ROLE_LABELS, ToolCard

    denied_glyph, denied_role = ToolCard._STATUS["denied"]
    error_glyph, error_role = ToolCard._STATUS["error"]

    assert denied_glyph != error_glyph
    assert denied_role != error_role
    assert _ROLE_LABELS[denied_role] != _ROLE_LABELS[error_role]
    assert _ROLE_LABELS[denied_role]
    assert _ROLE_LABELS[error_role]


def test_cancelled_tool_card_label_does_not_read_denied() -> None:
    # Regression (P2 review on #76's denied/error fix): "cancelled" shares
    # "denied"'s CSS role class intentionally (same left-rule color and glyph
    # family — both mean "stopped by a decision, not a failure"), but a
    # cancelled tool call was never actually denied approval. Its
    # border-title must come from ToolCard._STATUS_LABELS's status-keyed
    # override, not fall through to _ROLE_LABELS[role] and read "denied".
    from wisp.tui.widgets import _ROLE_LABELS, ToolCard

    _, cancelled_role = ToolCard._STATUS["cancelled"]
    resolved_title = ToolCard._STATUS_LABELS.get(
        "cancelled", _ROLE_LABELS.get(cancelled_role, "tool")
    )

    assert resolved_title == "cancelled"
    assert resolved_title != _ROLE_LABELS["denied"]


def test_contrast_ratio_helper_matches_known_wcag_examples() -> None:
    # Sanity-check the helper against textbook values before trusting it for
    # theme assertions below.
    from wisp.tui.theme import contrast_ratio

    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    # order-independent
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    # identical colors
    assert contrast_ratio("#777777", "#777777") == pytest.approx(1.0, abs=0.01)


def test_dark_theme_foreground_meets_normal_text_contrast_target() -> None:
    from wisp.tui.theme import WISP_THEME_DARK, contrast_ratio

    assert contrast_ratio(WISP_THEME_DARK.foreground, WISP_THEME_DARK.background) >= 4.5


def test_light_theme_foreground_meets_normal_text_contrast_target() -> None:
    from wisp.tui.theme import WISP_THEME_LIGHT, contrast_ratio

    assert contrast_ratio(WISP_THEME_LIGHT.foreground, WISP_THEME_LIGHT.background) >= 4.5


@pytest.mark.parametrize(
    ("color_attr", "background_attr"),
    [
        ("primary", "background"),  # user transcript text
        ("primary", "panel"),  # jump-to-latest badge text
        ("success", "background"),  # assistant transcript text
        ("accent", "background"),  # empty-state wordmark
        ("accent", "surface"),  # model-picker title
        ("warning", "background"),  # notice transcript text
        ("warning", "surface"),  # decision-panel title
        ("error", "background"),  # error transcript text
    ],
)
def test_light_theme_semantic_text_colors_meet_contrast_target(
    color_attr: str, background_attr: str
) -> None:
    from wisp.tui.theme import WISP_THEME_LIGHT, contrast_ratio

    color = getattr(WISP_THEME_LIGHT, color_attr)
    background = getattr(WISP_THEME_LIGHT, background_attr)

    assert contrast_ratio(color, background) >= 4.5


@pytest.mark.parametrize(
    ("color_attr", "background_attr"),
    [
        ("primary", "background"),
        ("success", "background"),
        ("accent", "background"),
        ("accent", "surface"),
        ("accent", "panel"),
        ("warning", "background"),
        ("warning", "surface"),
        ("error", "background"),
    ],
)
def test_light_theme_semantic_borders_meet_non_text_contrast_target(
    color_attr: str, background_attr: str
) -> None:
    from wisp.tui.theme import WISP_THEME_LIGHT, contrast_ratio

    color = getattr(WISP_THEME_LIGHT, color_attr)
    background = getattr(WISP_THEME_LIGHT, background_attr)

    assert contrast_ratio(color, background) >= 3.0


def test_light_theme_secondary_remains_legible() -> None:
    from wisp.tui.theme import WISP_THEME_LIGHT, contrast_ratio

    assert contrast_ratio(WISP_THEME_LIGHT.secondary, WISP_THEME_LIGHT.background) >= 4.5


@pytest.mark.parametrize("color_attr", ["primary", "success", "accent", "warning", "error"])
def test_dark_theme_semantic_colors_meet_normal_text_contrast_target(color_attr: str) -> None:
    from wisp.tui.theme import WISP_THEME_DARK, contrast_ratio

    assert contrast_ratio(getattr(WISP_THEME_DARK, color_attr), WISP_THEME_DARK.background) >= 4.5


def test_light_theme_derived_diff_colors_meet_normal_text_contrast_target() -> None:
    from wisp.tui.textual_app import TextualTui
    from wisp.tui.theme import contrast_ratio

    async def scenario() -> dict[str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.theme = "wisp-light"
            await pilot.pause()
            variables = app.get_css_variables()
            return {
                key: variables[key]
                for key in ("text-success", "success-muted", "text-error", "error-muted")
            }

    variables = anyio.run(scenario)

    assert contrast_ratio(variables["text-success"], variables["success-muted"]) >= 4.5
    assert contrast_ratio(variables["text-error"], variables["error-muted"]) >= 4.5


def test_issue_118_does_not_change_dark_or_neutral_palette_values() -> None:
    from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT

    assert (
        WISP_THEME_DARK.primary,
        WISP_THEME_DARK.secondary,
        WISP_THEME_DARK.accent,
        WISP_THEME_DARK.warning,
        WISP_THEME_DARK.error,
        WISP_THEME_DARK.success,
        WISP_THEME_DARK.foreground,
        WISP_THEME_DARK.background,
        WISP_THEME_DARK.surface,
        WISP_THEME_DARK.panel,
    ) == (
        "#4aa3c7",
        "#7c8b99",
        "#3fb8b8",
        "#d3a25a",
        "#d16a7c",
        "#5cc9a7",
        "#dfe6ec",
        "#0e1216",
        "#151b21",
        "#1b232b",
    )
    assert (
        WISP_THEME_LIGHT.secondary,
        WISP_THEME_LIGHT.foreground,
        WISP_THEME_LIGHT.background,
        WISP_THEME_LIGHT.surface,
        WISP_THEME_LIGHT.panel,
    ) == ("#55636d", "#12171c", "#fbfcfd", "#ffffff", "#eef3f5")


def test_muted_text_role_meets_contrast_target() -> None:
    # Issue #76: dim/session previously stacked Rich's undefined "dim"
    # attribute on top of the already-muted secondary color (raw ANSI SGR-2,
    # not a deterministic blend — see theme.py's MUTED_DARK/MUTED_LIGHT
    # comment). The baked replacement must clear 4.5:1 against both the main
    # background and the panel background, in both themes.
    from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT, contrast_ratio, role_styles

    for theme in (WISP_THEME_DARK, WISP_THEME_LIGHT):
        muted = role_styles(theme)["dim"]
        assert contrast_ratio(muted, theme.background) >= 4.5
        assert contrast_ratio(muted, theme.panel) >= 4.5


def test_role_styles_no_longer_uses_bare_dim_attribute_for_muted_roles() -> None:
    # Regression guard for the #76 fix: the literal "dim" Rich attribute must
    # never appear in role_styles()'s output — it's non-deterministic in
    # Wisp's rendering path (no DimFilter in the chain) and was the root
    # cause of the double-dimming contrast bug.
    from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT, role_styles

    for theme in (WISP_THEME_DARK, WISP_THEME_LIGHT):
        for role, style in role_styles(theme).items():
            assert "dim" not in style.split(), f"{role!r} still uses the dim attribute: {style!r}"


def _monochrome_gray(hex_color: str) -> int:
    """Textual's exact NO_COLOR conversion: Rec. 709 luma, rounded."""
    from textual.color import Color

    return Color.parse(hex_color).monochrome.r  # r == g == b once converted


def _monochrome_hex(hex_color: str) -> str:
    gray = _monochrome_gray(hex_color)
    return f"#{gray:02x}{gray:02x}{gray:02x}"


@pytest.mark.parametrize(
    ("color_attr", "background_attr"),
    [
        ("primary", "background"),
        ("primary", "panel"),
        ("success", "background"),
        ("accent", "background"),
        ("accent", "surface"),
        ("warning", "background"),
        ("warning", "surface"),
        ("error", "background"),
    ],
)
def test_light_theme_semantic_text_colors_keep_contrast_under_no_color(
    color_attr: str, background_attr: str
) -> None:
    from wisp.tui.theme import WISP_THEME_LIGHT, contrast_ratio

    color = _monochrome_hex(getattr(WISP_THEME_LIGHT, color_attr))
    background = _monochrome_hex(getattr(WISP_THEME_LIGHT, background_attr))

    assert contrast_ratio(color, background) >= 4.5


def test_monochrome_role_color_collisions_still_have_distinct_non_color_cues() -> None:
    # Issue #76: NO_COLOR runs every rendered color through Textual's built-in
    # Monochrome filter (a fixed Rec. 709 luma conversion), which Wisp gets
    # for free but never explicitly verified. Roles that collide (or land
    # within a couple of gray levels of each other) once color is gone must
    # still be told apart some other way — a border-title label is Wisp's
    # primary mechanism (ToolCard also adds a glyph on top). "dim"/"session"
    # are deliberately excluded from cross-role comparison: they're quiet,
    # borderless metadata (no label at all, by design — see _ROLE_LABELS)
    # that never appears as a competing alternative to a labeled role like
    # "error" or "denied", so a shared gray with them carries no ambiguity.
    from wisp.tui.theme import _ROLE_COLOR_ATTR, WISP_THEME_DARK, WISP_THEME_LIGHT
    from wisp.tui.widgets import _ROLE_LABELS

    comparable_roles = sorted(set(_ROLE_COLOR_ATTR) - {"dim", "session"})
    collision_threshold = 5  # gray levels; "near-collision" per the issue's audit

    for theme in (WISP_THEME_DARK, WISP_THEME_LIGHT):
        grays = {
            role: _monochrome_gray(getattr(theme, _ROLE_COLOR_ATTR[role]))
            for role in comparable_roles
        }
        for i, role_a in enumerate(comparable_roles):
            for role_b in comparable_roles[i + 1 :]:
                if abs(grays[role_a] - grays[role_b]) <= collision_threshold:
                    label_a, label_b = _ROLE_LABELS.get(role_a, ""), _ROLE_LABELS.get(role_b, "")
                    assert label_a != label_b, (
                        f"{theme.name}: {role_a!r} (gray={grays[role_a]}) and {role_b!r} "
                        f"(gray={grays[role_b]}) collide under NO_COLOR and share the same "
                        f"label {label_a!r} — no non-color cue distinguishes them"
                    )
                    assert label_a and label_b, (
                        f"{theme.name}: {role_a!r}/{role_b!r} collide under NO_COLOR but at "
                        "least one has no label to fall back on"
                    )


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
