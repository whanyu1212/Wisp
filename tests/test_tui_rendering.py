# ruff: noqa: F403,F405

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from textual.theme import Theme

from tests.tui_support import *
from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import (
    ContextBudget,
    ContextEstimate,
    MessageRole,
    ProviderRetrying,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    RpcSkillInvocationSnapshot,
    SessionCostSummary,
    SkillInvoked,
)
from wisp.skills.models import SkillInvocationEvidence
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    HistoricalSkillInvocation,
    HistoricalToolCard,
    HistoricalTranscriptMessage,
    history_entries_from_rpc_messages,
    history_from_rpc_messages,
    represented_history_entry_ids,
)
from wisp.tui.input_types import PendingSubmissionView, TuiSubmission, new_submission_id
from wisp.tui.rendering import _tui_help_text, _unsent_submission_text
from wisp.tui.theme import WISP_THEMES

pytestmark = pytest.mark.tui


def test_unsent_submission_label_preserves_queue_kind() -> None:
    steering = TuiSubmission(
        id=new_submission_id(),
        content="change direction",
        display="change direction",
        input_mode="running",
        queue_kind="steering",
    )
    follow_up = TuiSubmission(
        id=new_submission_id(),
        content="do this after",
        display="do this after",
        input_mode="running",
        queue_kind="follow_up",
    )

    assert _unsent_submission_text(steering) == "unsent steering: change direction"
    assert _unsent_submission_text(follow_up) == "unsent follow-up: do this after"


def test_shared_help_scopes_runtime_queue_keys_to_fullscreen_interfaces() -> None:
    help_text = _tui_help_text()

    assert "In fullscreen interfaces while a prompt runs" in help_text


def test_line_renderer_bounds_new_pending_submission_preview() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, width=24)
    renderer = LineTuiRenderer(console)
    pending = PendingSubmissionView(
        id=new_submission_id(),
        display=(
            "a first line that is much wider than the terminal\n"
            "a second visible line\n"
            "a third line that must stay hidden"
        ),
    )
    snapshot = TuiViewSnapshot(
        status="running",
        input_hint="wisp(running)> ",
        pending_submissions=(pending,),
    )

    renderer.view_updated(snapshot)
    renderer.view_updated(snapshot)

    rendered = output.getvalue()
    assert rendered.count("Queued 1 follow-up") == 1
    assert "a third line that must stay hidden" not in rendered
    assert "a first line that is much wider than the terminal" not in rendered
    assert len(rendered.splitlines()) == 3


def test_renderers_clear_session_for_new_session() -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    interactive_output = io.StringIO()
    interactive_line = LineTuiRenderer(
        Console(file=interactive_output, force_terminal=True, color_system=None)
    )
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    fullscreen.notice("old transcript")

    line.clear_session()
    interactive_line.clear_session()
    fullscreen.clear_session()

    assert "--- new session ---" in output.getvalue()
    assert "\x1b[2J" in interactive_output.getvalue()
    assert "\x1b[H" in interactive_output.getvalue()
    assert fullscreen.state.transcript == []
    assert fullscreen.state.streaming_text == ""
    assert fullscreen.state.transcript_scroll_offset == 0


@pytest.mark.parametrize(
    ("event", "category", "style"),
    [
        (
            ToolApprovalRequested(
                call_id="read-1",
                name="inspect",
                arguments={"path": "README.md"},
                safety="read",
            ),
            "○ READ-ONLY ACCESS · inspect",
            "cyan",
        ),
        (
            ToolApprovalRequested(
                call_id="write-1",
                name="write",
                arguments={"path": "notes.txt", "content": "hello"},
                safety="mutating",
            ),
            "△ MODIFIES FILES · notes.txt",
            "yellow",
        ),
        (
            ToolApprovalRequested(
                call_id="command-1",
                name="bash",
                arguments={"command": "echo hi"},
                safety="command",
            ),
            "! COMMAND EXECUTION · bash",
            "bold red",
        ),
    ],
)
def test_line_and_fullscreen_approval_renderers_keep_category_labels(
    event: ToolApprovalRequested,
    category: str,
    style: str,
) -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    line.approval_request(event)
    fullscreen.approval_request(event)

    assert category in output.getvalue()
    assert len(fullscreen.state.transcript) == 1
    entry = fullscreen.state.transcript[0]
    assert entry.role == "approval"
    assert category in entry.content
    assert entry.style == style


def test_line_and_fullscreen_trust_renderers_explain_project_resources() -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    event = TrustRequested(request_id="trust-1", project_path=Path("/work/project"))

    line.trust_request(event)
    fullscreen.trust_request(event)

    rendered = output.getvalue()
    assert "◆ PROJECT TRUST · /work/project" in rendered
    assert "project-controlled settings, instructions, and skills" in rendered
    assert len(fullscreen.state.transcript) == 1
    entry = fullscreen.state.transcript[0]
    assert entry.role == "trust"
    assert "◆ PROJECT TRUST · /work/project" in entry.content
    assert "does not bypass tool approvals" in entry.content
    assert entry.style == "magenta"


def test_line_and_fullscreen_approval_details_are_bounded_and_literal() -> None:
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    event = ToolApprovalRequested(
        call_id="extension-1",
        name="[red]extension-tool[/red]",
        arguments={"payload": "[bold]literal[/bold]" + ("x" * 500)},
        safety="mutating",
    )

    line.approval_request(event)
    fullscreen.approval_request(event)

    rendered = output.getvalue()
    entry = fullscreen.state.transcript[0]
    for notice in (rendered, entry.content):
        assert "[red]extension-tool[/red]" in notice
        assert "[bold]literal[/bold]" in notice
        assert "... preview truncated" in notice
        assert "x" * 400 not in notice
    assert len(rendered) < 800
    assert len(entry.content) < 800


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
    skill_invocation: RpcSkillInvocationSnapshot | None = None,
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
        skill_invocation=skill_invocation,
    )


def test_history_uses_typed_skill_invocation_instead_of_expanded_content() -> None:
    invocation = RpcSkillInvocationSnapshot(
        name="review",
        original_content="/skill:review focus on safety",
        original_content_bytes=29,
        request="focus on safety",
        request_bytes=15,
        content_sha256="a" * 64,
        instructions_truncated=True,
    )
    entries = history_entries_from_rpc_messages(
        (
            _rpc_message(
                "user",
                "provider-visible expanded instructions",
                entry_id="message-1",
                skill_invocation=invocation,
            ),
        )
    )

    assert entries == (
        HistoricalSkillInvocation(
            entry_id="message-1",
            name="review",
            original_content="/skill:review focus on safety",
            original_content_truncated=False,
            request="focus on safety",
            request_truncated=False,
            instructions_truncated=True,
        ),
    )


def test_fullscreen_renderer_replaces_live_skill_invocation_echo() -> None:
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    original = "/skill:review focus on safety"
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

    assert [(entry.role, entry.content) for entry in renderer.state.transcript] == [
        ("skill", "skill /skill:review focus on safety")
    ]


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


def test_history_from_rpc_messages_hides_system_and_represents_empty_assistant_rows() -> None:
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
        HistoricalTranscriptMessage(role="assistant", content="(empty assistant message)"),
        HistoricalTranscriptMessage(role="assistant", content="[content truncated]"),
        HistoricalTranscriptMessage(role="assistant", content="long answer\n[content truncated]"),
    )


def test_history_from_rpc_messages_hides_all_system_only_history() -> None:
    messages = (
        _rpc_message("system", "system prompt", entry_id="system-1"),
        _rpc_message("system", "", entry_id="system-2"),
    )

    assert history_entries_from_rpc_messages(messages) == ()
    assert history_from_rpc_messages(messages) == ()


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
    assert represented_history_entry_ids(entries) == {
        "user-1",
        "assistant-1",
        "tool-1",
    }
    assert isinstance(entries[0], HistoricalTranscriptMessage)
    assert isinstance(entries[1], HistoricalTranscriptMessage)
    assert (entries[0].entry_id, entries[1].entry_id) == ("user-1", "assistant-1")
    assert history_from_rpc_messages(tuple()) == ()


def test_historical_tool_card_preserves_existing_positional_field_order() -> None:
    entry = HistoricalToolCard(
        "history:tool-1",
        "bash",
        {"command": "true"},
        "output",
        False,
        "done",
        0,
        True,
        "before",
        True,
        "summary",
        True,
        True,
    )

    assert entry.status == "done"
    assert entry.exit_code == 0
    assert entry.output_has_exit_status is True
    assert entry.before_text == "before"
    assert entry.created is True
    assert entry.summary == "summary"
    assert entry.truncated is True
    assert entry.missing_result is True
    assert entry.tool_call_id is None
    assert entry.call_missing is False


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
            call_missing=True,
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
    assert isinstance(entries[0], HistoricalToolCard)
    assert isinstance(entries[1], HistoricalToolCard)
    assert entries[0].call_missing is True
    assert entries[1].call_missing is False


def test_history_entries_from_rpc_messages_marks_legacy_interrupted_repairs_cancelled() -> None:
    entries = history_entries_from_rpc_messages(
        (
            _rpc_message(
                "tool",
                INTERRUPTED_TOOL_RESULT_TEXT,
                entry_id="repair-1",
                tool_call_id="call-1",
                tool_name="read",
                is_error=True,
            ),
        )
    )

    assert entries == (
        HistoricalToolCard(
            card_id="history:repair-1",
            name="read",
            arguments={},
            output=INTERRUPTED_TOOL_RESULT_TEXT,
            is_error=True,
            status="cancelled",
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


def test_historical_cancelled_tool_result_renders_stored_output() -> None:
    entries = (
        HistoricalToolCard(
            card_id="history:cancelled",
            name="bash",
            arguments={"operation": "cancel", "process_id": "p123"},
            output="Process p123 cancelled\nstdout:\ntail\n",
            is_error=False,
            status="cancelled",
        ),
        HistoricalToolCard(
            card_id="history:missing",
            name="bash",
            arguments={"operation": "start", "command": "sleep 30"},
            output="No persisted tool result.",
            is_error=True,
            status="cancelled",
            missing_result=True,
        ),
    )
    console, output = _console()
    line = LineTuiRenderer(console)
    fullscreen = FullscreenTuiRenderer(_console()[0], clear_screen=False)

    line.render_history_entries(entries)
    fullscreen.render_history_entries(entries)

    rendered = output.getvalue()
    assert "⊘ historical tool bash: Process p123 cancelled" in rendered
    assert "⊘ historical tool bash: no persisted result" in rendered
    assert [(entry.role, entry.content) for entry in fullscreen.state.transcript] == [
        ("tool", "bash: Process p123 cancelled"),
        ("tool", "bash: no persisted result"),
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


def test_line_tui_renderer_renders_failed_manual_compaction_as_error() -> None:
    # Regression: a manual /compact failure (reason="manual", the default) fell
    # through to the bare unstyled branch -- rendered in default terminal
    # color, less visually distinct than a routine successful compaction (which
    # gets Rich's automatic number highlighting). FullscreenTuiRenderer and
    # TextualTuiRenderer both already style this red; LineTuiRenderer must too.
    output = io.StringIO()
    # Verify the renderer's explicit error style independently of an ambient NO_COLOR setting.
    console = Console(file=output, force_terminal=True, no_color=False, width=120)
    renderer = LineTuiRenderer(console)

    renderer.event(
        CompactionCompleted(
            session_id="session-1",
            reason="manual",
            outcome="failed",
            replaced_entry_count=5,
            retained_entry_count=1,
            error="disk full",
        )
    )

    rendered = output.getvalue()
    assert "Compaction failed: disk full" in rendered
    assert "\x1b[31m" in rendered  # red, matching the threshold/overflow failure styling


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


def test_tui_footer_displays_plan_mode_without_changing_default_build_footer() -> None:
    build = TuiViewSnapshot(status="idle", input_hint="wisp> ", mode="build")
    plan = TuiViewSnapshot(status="idle", input_hint="wisp> ", mode="plan")

    assert format_tui_footer_lines(build, width=40)[1].strip() == "idle"
    assert format_tui_footer_lines(plan, width=40)[1].strip() == "plan • idle"


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
    assert protected == "running • later 12"


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
    assert lines[1] == "running • later 12"
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
    from wisp.tui.textual_app import TextualTui

    assert "color: $warning" in TextualTui.CSS
    assert "color: $accent" in TextualTui.CSS


@pytest.mark.parametrize("role", ["approved", "denied", "error"])
def test_semantic_surface_roles_use_contrast_adjusted_text(role: str) -> None:
    from wisp.tui.textual_app import TextualTui

    semantic_role = {"approved": "success", "denied": "warning", "error": "error"}[role]
    rule = TextualTui.CSS.split(f".message--{role}", 1)[1].split("}", 1)[0]

    assert f"background: ${semantic_role}-muted;" in rule
    assert f"color: $text-{semantic_role};" in rule


def test_denied_and_error_tool_cards_keep_distinct_semantic_roles() -> None:
    from wisp.tui.widgets import ToolCard

    denied_role = ToolCard._STATUS_ROLE["denied"]
    error_role = ToolCard._STATUS_ROLE["error"]

    assert denied_role != error_role


def test_tool_card_role_rules_preserve_muted_content_color() -> None:
    from wisp.tui.textual_app import TextualTui

    role_rule = TextualTui.CSS.split("ToolCard.message--tool,", 1)[1].split("}", 1)[0]
    assert "color: $text-muted;" in role_rule


def test_cancelled_tool_card_uses_explicit_cancelled_action() -> None:
    from wisp.tui.widgets import ToolCard

    card = ToolCard("write", {"path": "x.py"})
    card.set_state("cancelled", detail="cancelled")

    assert card.render().plain.startswith("• Cancelled writing  x.py")
    assert "Denied" not in card.render().plain


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


@pytest.mark.parametrize(
    "color_attr", ["primary", "secondary", "success", "accent", "warning", "error"]
)
def test_dark_theme_semantic_colors_meet_normal_text_contrast_target(color_attr: str) -> None:
    from wisp.tui.theme import WISP_THEME_DARK, contrast_ratio

    assert contrast_ratio(getattr(WISP_THEME_DARK, color_attr), WISP_THEME_DARK.background) >= 4.5


@pytest.mark.parametrize("theme_name", [theme.name for theme in WISP_THEMES])
@pytest.mark.parametrize("role", ["success", "warning", "error"])
def test_derived_semantic_muted_pairs_meet_text_contrast_target(theme_name: str, role: str) -> None:
    # Textual auto-derives the muted surfaces used by approved, denied, and error
    # transcript rows. Resolve every registered theme through a live app so these
    # assertions cover the actual text/background pairs handed to the painter.
    from wisp.tui.textual_app import TextualTui
    from wisp.tui.theme import contrast_ratio

    async def scenario() -> tuple[str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.theme = theme_name
            await pilot.pause()
            variables = app.get_css_variables()
            return variables[f"text-{role}"], variables[f"{role}-muted"]

    text, background = anyio.run(scenario)

    assert contrast_ratio(text, background) >= 4.5


@pytest.mark.parametrize("theme_name", [theme.name for theme in WISP_THEMES])
@pytest.mark.parametrize(
    ("foreground", "background"),
    [
        ("diff-add-fg", "diff-add-bg"),
        ("diff-add-fg", "diff-add-token-bg"),
        ("diff-line-number-fg", "diff-add-gutter-bg"),
        ("diff-add-sign-fg", "diff-add-gutter-bg"),
        ("diff-del-fg", "diff-del-bg"),
        ("diff-del-fg", "diff-del-token-bg"),
        ("diff-line-number-fg", "diff-del-gutter-bg"),
        ("diff-del-sign-fg", "diff-del-gutter-bg"),
        ("diff-context-fg", "background"),
        ("diff-hunk-fg", "background"),
        ("diff-add-count-fg", "panel"),
        ("diff-del-count-fg", "panel"),
    ],
)
def test_diff_theme_colors_clear_contrast_thresholds(
    theme_name: str, foreground: str, background: str
) -> None:
    # Diff source text must stay legible on BOTH its row band and the stronger
    # token band layered on top. The token band is the tighter pairing, so it
    # governs: tuning against the row band alone once shipped a light theme
    # below AA. Resolved through a live app so the assertion covers the values
    # Textual actually hands the painter, not just the literals in theme.py.
    from wisp.tui.textual_app import TextualTui
    from wisp.tui.theme import contrast_ratio

    async def scenario() -> dict[str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            app.theme = theme_name
            await pilot.pause()
            return dict(app.get_css_variables())

    variables = anyio.run(scenario)

    assert contrast_ratio(variables[foreground], variables[background]) >= 4.5


def test_refined_vapor_and_paper_palette_values_are_stable() -> None:
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
        "#81a2be",
        "#a7adb3",
        "#8abeb7",
        "#f0c674",
        "#d97979",
        "#c5cd78",
        "#d4d4d4",
        "#18181e",
        "#1e1e24",
        "#2d2d38",
    )
    assert (
        WISP_THEME_LIGHT.secondary,
        WISP_THEME_LIGHT.foreground,
        WISP_THEME_LIGHT.background,
        WISP_THEME_LIGHT.surface,
        WISP_THEME_LIGHT.panel,
    ) == ("#5e409d", "#100f0f", "#fffcf0", "#f2f0e5", "#e6e4d9")


def test_muted_text_role_meets_contrast_target() -> None:
    # Issue #76: dim/session previously stacked Rich's undefined "dim"
    # attribute on top of the already-muted secondary color (raw ANSI SGR-2,
    # not a deterministic blend — see theme.py's MUTED_DARK/MUTED_LIGHT
    # comment). The baked replacement must clear 4.5:1 against both the main
    # background and the panel background, in both themes.
    from wisp.tui.textual_app import TextualTui
    from wisp.tui.theme import WISP_THEMES, contrast_ratio

    async def scenario() -> dict[str, str]:
        app = TextualTui()
        async with app.run_test() as pilot:
            resolved: dict[str, str] = {}
            for theme in WISP_THEMES:
                app.theme = theme.name
                await pilot.pause()
                resolved[theme.name] = app.get_css_variables()["transcript-muted"]
            return resolved

    muted_by_theme = anyio.run(scenario)
    for theme in WISP_THEMES:
        muted = muted_by_theme[theme.name]
        assert contrast_ratio(muted, theme.background) >= 4.5
        assert contrast_ratio(muted, theme.panel) >= 4.5


def test_primary_on_panel_meets_contrast_target_in_every_theme() -> None:
    # JumpToLatest renders `color: $primary` on `background: $panel` for the
    # jump-to-latest badge. A theme whose primary only clears 4.5:1 against
    # its background (not against panel too) still ships with an illegible
    # badge, so every registered theme must clear the pairing directly.
    from wisp.tui.theme import WISP_THEMES, contrast_ratio

    for theme in WISP_THEMES:
        assert contrast_ratio(theme.primary, theme.panel) >= 4.5, theme.name


@pytest.mark.parametrize("theme", WISP_THEMES, ids=lambda theme: theme.name)
@pytest.mark.parametrize(
    ("color_attr", "background_attr"),
    [
        ("foreground", "background"),
        ("primary", "background"),
        ("primary", "panel"),
        ("secondary", "background"),
        ("accent", "background"),
        ("accent", "surface"),
        ("accent", "panel"),
        ("warning", "background"),
        ("warning", "surface"),
        ("error", "background"),
        ("success", "background"),
    ],
)
def test_every_theme_semantic_text_pair_meets_contrast_target(
    theme: Theme, color_attr: str, background_attr: str
) -> None:
    from wisp.tui.theme import contrast_ratio

    color = getattr(theme, color_attr)
    background = getattr(theme, background_attr)

    assert contrast_ratio(color, background) >= 4.5, (
        f"{theme.name}: {color_attr} on {background_attr}"
    )


def test_line_messages_keep_literal_content_without_baked_rich_styles() -> None:
    from wisp.tui.widgets import LineMessage

    line = LineMessage("[dim]literal[/dim]", role="dim")

    rendered = line.render()
    assert rendered.plain == "[dim]literal[/dim]"
    assert rendered.spans == []


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


def test_monochrome_operational_role_collisions_still_have_distinct_non_color_cues() -> None:
    # Issue #76: NO_COLOR runs every rendered color through Textual's built-in
    # Monochrome filter (a fixed Rec. 709 luma conversion). Operational roles
    # that collide once color is gone must retain distinct border-title labels;
    # ToolCard also adds a glyph. Conversation roles are deliberately label-free
    # and are distinguished by their rail color only, so they are excluded along
    # with quiet, borderless metadata roles.
    from wisp.tui.theme import WISP_THEME_DARK, WISP_THEME_LIGHT
    from wisp.tui.widgets import _ROLE_LABELS

    role_color_attr = {
        "notice": "warning",
        "error": "error",
        "tool": "accent",
        "approved": "success",
        "denied": "warning",
    }
    comparable_roles = sorted(role_color_attr)
    collision_threshold = 5  # gray levels; "near-collision" per the issue's audit

    for theme in (WISP_THEME_DARK, WISP_THEME_LIGHT):
        grays = {
            role: _monochrome_gray(getattr(theme, role_color_attr[role]))
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


def test_fullscreen_tui_renderer_token_delta_matches_full_rewrap_baseline_under_wrapping() -> None:
    # Regression: token_delta() computes the appended-line count incrementally
    # from just the streaming entry (see its docstring) instead of re-wrapping
    # the entire transcript on every token. That must produce byte-identical
    # transcript_scroll_offset to re-wrapping everything from scratch,
    # including when word-wrapping is active and a delta lands exactly on a
    # wrap boundary -- the case a naive delta computation could get wrong.
    def build_renderer() -> FullscreenTuiRenderer:
        renderer = FullscreenTuiRenderer(
            _console()[0], clear_screen=False, transcript_view_entries=3
        )
        renderer._transcript_wrap_width = lambda: 10  # type: ignore[method-assign]
        for index in range(5):
            renderer.event(completed_message(content=f"message {index} has a fairly long line"))
        renderer.scroll_transcript_up(1)
        return renderer

    baseline = build_renderer()
    under_test = build_renderer()
    assert baseline.state.transcript_scroll_offset == under_test.state.transcript_scroll_offset

    for delta in ["stream", "ing ", "a lo", "ng w", "ord ", "boun", "dary", " end"]:
        previous_lines = len(baseline._rendered_transcript_lines())
        baseline.state.streaming_text += delta
        appended = len(baseline._rendered_transcript_lines()) - previous_lines
        baseline._preserve_scroll_after_appended_lines(appended)

        under_test.token_delta(delta)

        assert under_test.state.streaming_text == baseline.state.streaming_text
        assert under_test.state.transcript_scroll_offset == baseline.state.transcript_scroll_offset


def test_fullscreen_tui_renderer_token_delta_does_not_double_rewrap_transcript() -> None:
    # Regression: the old token_delta() called _rendered_transcript_lines()
    # (a full re-wrap of the whole transcript plus the whole streaming_text)
    # twice per token -- once directly, once again inside
    # _preserve_scroll_after_line_count_change. token_delta() itself must call
    # it at most once now (via _clamp_transcript_scroll's ceiling check, which
    # is unavoidable and unrelated to this fix); the appended-line count comes
    # from _rendered_streaming_entry_line_count(), which only re-wraps the
    # single streaming entry, not the rest of the transcript.
    renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
    for index in range(20):
        renderer.event(completed_message(content=f"message {index}"))

    call_count = 0
    original = FullscreenTuiRenderer._rendered_transcript_lines

    def counting_rewrap(self: FullscreenTuiRenderer) -> list[object]:
        nonlocal call_count
        call_count += 1
        return original(self)

    renderer._rendered_transcript_lines = counting_rewrap.__get__(renderer)  # type: ignore[method-assign]

    renderer.token_delta("one token")

    assert call_count <= 1


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
    assert "running • later 1" in rendered
    assert "openai/gpt-test" in rendered
    assert "wisp(running)> " in rendered


def test_create_tui_renderer_selects_fullscreen_renderer() -> None:
    renderer = create_tui_renderer(TuiRendererKind.fullscreen, _console()[0])

    assert isinstance(renderer, FullscreenTuiRenderer)
