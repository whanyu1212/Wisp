# ruff: noqa: F403,F405

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from tempfile import TemporaryDirectory

from pytest import MonkeyPatch

from tests.tui_support import *
from wisp.auth.storage import OAuthCredential
from wisp.events import (
    BillableTokenUsage,
    ContextBudget,
    ContextEstimate,
    ContextEstimated,
    MessageCompleted,
    MessageRole,
    MessageStarted,
    ProviderRetrying,
    RpcMessageSnapshot,
    RpcMessagesReported,
    SessionStatsReported,
    TokenUsage,
    UsageCost,
    UsageCostRates,
)
from wisp.tui import auth_commands as tui_auth_commands_module
from wisp.tui.history import HistoricalTranscriptMessage
from wisp.tui.state import TuiViewState


def _context_budget(
    *,
    estimated: int,
    observed: int | None = None,
    current: bool = False,
    window: int | None = 128_000,
) -> ContextBudget:
    return ContextBudget.model_construct(
        estimate=ContextEstimate.model_construct(total_tokens=estimated),
        observed_tokens=observed,
        observed_is_current=current,
        context_window=window,
        reserve_tokens=8_000,
        remaining_tokens=None,
        estimated_percent=estimated / window * 100 if window else None,
        over_budget=False,
    )


def _rpc_message(
    role: MessageRole,
    content: str,
    *,
    entry_id: str,
    content_truncated: bool = False,
) -> RpcMessageSnapshot:
    return RpcMessageSnapshot(
        entry_id=entry_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        role=role,
        content=content,
        content_original_bytes=len(content.encode("utf-8")),
        content_truncated=content_truncated,
    )


def test_tui_view_state_updates_context_from_estimate_stats_and_usage() -> None:
    state = TuiViewState()
    estimate = _context_budget(estimated=10_000)
    stats_context = _context_budget(estimated=11_000, observed=10_500)

    assert state.update_context_from_event(
        ContextEstimated(turn=1, provider="test", budget=estimate)
    )
    assert state.context is estimate

    stats_event = SessionStatsReported.model_construct(
        command_id="stats-1",
        stats=type("Stats", (), {"context": stats_context})(),
    )
    assert state.update_context_from_event(stats_event)
    assert state.context is stats_context

    assert state.update_context_from_event(
        MessageCompleted(
            turn=1,
            content="answer",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=11_500, output_tokens=500, total_tokens=12_000),
        )
    )
    assert state.context is not None
    assert state.context.observed_tokens == 12_000
    assert state.context.observed_is_current is True
    assert state.context.estimated_percent == 9.375
    assert state.snapshot().context is state.context


def test_tui_view_state_accumulates_complete_and_unpriced_costs() -> None:
    state = TuiViewState()
    priced = UsageCost(
        provider="openai",
        model="model",
        billable=BillableTokenUsage(
            input_tokens=1,
            cache_read_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=1,
        ),
        rates=UsageCostRates(
            input_usd_per_million=Decimal("1"),
            output_usd_per_million=Decimal("1"),
        ),
        estimated_usd=Decimal("0.042"),
    )
    unpriced = UsageCost(
        provider="openai-codex",
        model="model",
        unavailable_reason="pricing_unavailable",
    )

    assert state.update_context_from_event(
        MessageCompleted(turn=1, content="one", finish_reason="stop", cost=priced)
    )
    assert state.cost is not None
    assert state.cost.known_usd == Decimal("0.042")
    assert state.cost.complete is True
    assert state.update_context_from_event(
        MessageCompleted(turn=2, content="two", finish_reason="stop", cost=unpriced)
    )
    assert state.cost.complete is False
    assert state.cost.unpriced_record_count == 1
    assert state.snapshot().cost is state.cost


def test_tui_view_state_invalidates_context_after_automatic_compaction() -> None:
    state = TuiViewState(context=_context_budget(estimated=81))
    trigger = _context_budget(estimated=90)

    assert state.update_context_from_event(
        CompactionStarted(
            session_id="session",
            reason="threshold",
            source_entry_count=4,
            trigger_budget=trigger,
        )
    )
    assert state.context is trigger
    assert state.update_context_from_event(
        CompactionCompleted(
            session_id="session",
            reason="threshold",
            outcome="completed",
            replaced_entry_count=2,
            retained_entry_count=2,
        )
    )
    assert state.context is None


def test_tui_view_state_tracks_overflow_compaction_budget() -> None:
    state = TuiViewState(context=_context_budget(estimated=81))
    trigger = _context_budget(estimated=90)

    assert state.update_context_from_event(
        CompactionStarted(
            session_id="session",
            reason="overflow",
            source_entry_count=4,
            trigger_budget=trigger,
        )
    )
    assert state.context is trigger
    assert state.update_context_from_event(
        CompactionCompleted(
            session_id="session",
            reason="overflow",
            outcome="completed",
            replaced_entry_count=2,
            retained_entry_count=2,
            will_retry=True,
        )
    )
    assert state.context is None


def test_tui_view_state_ignores_zero_or_failed_message_usage() -> None:
    state = TuiViewState(context=_context_budget(estimated=10_000))
    original = state.context

    messages = (
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
        ),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="error",
            usage=TokenUsage(input_tokens=12_000, output_tokens=0, total_tokens=12_000),
        ),
        MessageCompleted(
            turn=1,
            content="",
            finish_reason="cancelled",
            usage=TokenUsage(input_tokens=12_000, output_tokens=0, total_tokens=12_000),
        ),
    )
    for message in messages:
        assert not state.update_context_from_event(message)
        assert state.context is original


def test_tui_shell_updates_context_before_suppressing_streamed_completion() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.snapshots: list[TuiViewSnapshot] = []

        def view_updated(self, snapshot: TuiViewSnapshot) -> None:
            self.snapshots.append(snapshot)

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        estimate = _context_budget(estimated=10_000)

        await shell._handle_rpc_event(ContextEstimated(turn=1, provider="test", budget=estimate))
        shell.state.rendered_tokens = True
        await shell._handle_rpc_event(
            MessageCompleted(
                turn=1,
                content="streamed answer",
                finish_reason="stop",
                usage=TokenUsage(input_tokens=11_500, output_tokens=500, total_tokens=12_000),
            )
        )

        assert renderer.snapshots[-1].context is not None
        assert renderer.snapshots[-1].context.observed_tokens == 12_000
        assert renderer.snapshots[-1].context.observed_is_current is True

    anyio.run(run)


def test_tui_shell_hydrates_resume_history_before_reading_prompt() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.calls: list[str] = []
            self.histories: list[tuple[HistoricalTranscriptMessage, ...]] = []

        def render_history(self, messages: tuple[HistoricalTranscriptMessage, ...]) -> None:
            self.calls.append("history")
            self.histories.append(messages)

        def running(self) -> None:
            self.calls.append("running")

    async def run() -> None:
        renderer = RecordingRenderer()
        controller = ScriptedController(
            [
                [
                    completed_message(content="live answer"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ],
            messages_events=[
                [
                    RpcMessagesReported(
                        command_id="messages-1",
                        messages=(
                            _rpc_message("user", "old prompt", entry_id="user-1"),
                            _rpc_message("assistant", "old answer", entry_id="assistant-1"),
                        ),
                    ),
                    RpcCommandFinished(
                        command_id="messages-1",
                        command_type="get_messages",
                        ok=True,
                    ),
                ]
            ],
        )
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["new prompt"]),
        )

        await shell.run()

        assert controller.messages_requests == [("messages-1", None, 500, None)]
        assert controller.prompts == ["new prompt"]
        assert renderer.calls[:2] == ["history", "running"]
        assert [(message.role, message.content) for message in renderer.histories[0]] == [
            ("user", "old prompt"),
            ("assistant", "old answer"),
        ]

    anyio.run(run)


def test_tui_shell_ignores_wrong_and_duplicate_history_reports() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.histories: list[tuple[HistoricalTranscriptMessage, ...]] = []

        def render_history(self, messages: tuple[HistoricalTranscriptMessage, ...]) -> None:
            self.histories.append(messages)

    async def run() -> None:
        renderer = RecordingRenderer()
        controller = ScriptedController(
            messages_events=[
                [
                    RpcMessagesReported(
                        command_id="other",
                        messages=(_rpc_message("user", "wrong", entry_id="wrong"),),
                    ),
                    RpcMessagesReported(
                        command_id="messages-1",
                        messages=(_rpc_message("user", "right", entry_id="right"),),
                    ),
                    RpcMessagesReported(
                        command_id="messages-1",
                        messages=(_rpc_message("user", "duplicate", entry_id="duplicate"),),
                    ),
                    RpcCommandFinished(
                        command_id="messages-1",
                        command_type="get_messages",
                        ok=True,
                    ),
                ]
            ]
        )
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from([]),
        )

        await shell.run()

        assert len(renderer.histories) == 1
        assert [(message.role, message.content) for message in renderer.histories[0]] == [
            ("user", "right")
        ]

    anyio.run(run)


def test_tui_shell_history_failure_does_not_block_input() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    completed_message(content="answer"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ],
            messages_events=[
                [
                    ErrorEvent(message="history failed"),
                    RpcCommandFinished(
                        command_id="messages-1",
                        command_type="get_messages",
                        ok=False,
                        error="history failed",
                    ),
                ]
            ],
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        rendered = output.getvalue()
        assert controller.prompts == ["hello"]
        assert "error: history failed" in rendered
        assert "command failed: history failed" in rendered
        assert "answer" in rendered

    anyio.run(run)


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
        assert "gpt-5.5 (legacy) (current)" in rendered
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
        assert "gpt-5.5 (legacy) (current)" in rendered
        assert "gpt-5.5 (legacy)" in rendered

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
        assert "  openai: gpt-5.6-sol (current)" in rendered
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


def test_tui_shell_typed_model_command_rejects_effort_the_model_does_not_support() -> None:
    # Regression test (Codex review on #125): the picker only ever offers
    # tiers a model's catalog entry lists (see ModelPicker.show's seeding
    # filter), but a typed "/model <id> <effort>" bypasses the picker
    # entirely -- claude-haiku-4-5 is deliberately absent from anthropic's
    # effort_levels, so an unvalidated typed effort would otherwise reach the
    # RPC agent (and eventually the provider's API) unsupported.
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model claude-haiku-4-5 high", "/quit"]),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [(None, "claude-haiku-4-5", None, False)]
        assert shell.current_model == "claude-haiku-4-5"
        assert shell.current_effort is None
        rendered = output.getvalue()
        assert "not supported by claude-haiku-4-5 on anthropic" in rendered

    anyio.run(run)


def test_tui_shell_typed_model_command_keeps_a_supported_effort() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model claude-opus-4-8 high", "/quit"]),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [(None, "claude-opus-4-8", "high", False)]
        assert shell.current_effort == "high"

    anyio.run(run)


def test_tui_shell_typed_model_command_keeps_new_default_model_efforts() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(
                [
                    "/model claude-fable-5 xhigh",
                    "/model google::gemini-3.5-flash MINIMAL",
                    "/quit",
                ]
            ),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [
            (None, "claude-fable-5", "xhigh", False),
            ("google", "gemini-3.5-flash", "MINIMAL", False),
        ]
        assert shell.current_effort == "MINIMAL"

    anyio.run(run)


def test_tui_shell_typed_model_command_effort_validation_is_permissive_for_unknown_model() -> None:
    # A brand-new model ahead of a catalog update must still work -- effort
    # validation must not hard-block the command just because the model
    # itself can't be resolved (mirrors /model's existing permissive
    # fallthrough for unrecognized model ids).
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model brand-new-model high", "/quit"]),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [(None, "brand-new-model", "high", False)]
        assert shell.current_effort == "high"

    anyio.run(run)


def test_tui_shell_typed_model_command_effort_permissive_for_qualified_unknown_model() -> None:
    # Regression test (Codex review on #125): the picker-qualified
    # "provider::model" form must be just as permissive for a brand-new,
    # not-yet-cataloged model as the bare (unqualified) form already is --
    # supports_effort() alone can't distinguish "model known, tier not
    # listed" from "model unknown to this provider" (both return False), so
    # _validated_effort must check knows_model() before treating a
    # provider-qualified unknown model as an unsupported-tier rejection.
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model openai::gpt-6 high", "/quit"]),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [("openai", "gpt-6", "high", False)]
        assert shell.current_effort == "high"

    anyio.run(run)


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


def test_tui_shell_accepts_documented_codex_effort_for_its_default_model() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, _output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/model openai-codex::gpt-5.6-sol high", "/quit"]),
            provider="anthropic",
        )

        await shell.run()

        assert controller.configurations == [("openai-codex", "gpt-5.6-sol", "high", False)]
        assert shell.current_effort == "high"

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


def test_tui_shell_compact_calls_controller_without_prompting_and_returns_idle() -> None:
    async def run() -> None:
        controller = ScriptedController(
            compact_events=[
                [
                    CompactionStarted(session_id="session-1", source_entry_count=7),
                    CompactionCompleted(
                        session_id="session-1",
                        outcome="completed",
                        replaced_entry_count=5,
                        retained_entry_count=2,
                    ),
                    RpcCommandFinished(command_id="compact-1", command_type="compact", ok=True),
                ]
            ]
        )
        renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["/compact preserve tool decisions"]),
        )

        await shell.run()

        assert controller.compactions == ["preserve tool decisions"]
        assert controller.session_stats_requests == ["session-stats-1", "session-stats-2"]
        assert controller.prompts == []
        assert shell.state.current_command_id is None
        assert shell.state.current_command_type is None
        assert shell.state.status is TuiStatus.exiting
        assert not any(entry.role == "user" for entry in renderer.state.transcript)
        assert any(
            entry.content == "Compacted 5 context entries." for entry in renderer.state.transcript
        )

    anyio.run(run)


def test_tui_shell_keeps_threshold_compaction_inside_prompt_state() -> None:
    async def run() -> None:
        renderer = FullscreenTuiRenderer(_console()[0], clear_screen=False)
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"
        shell.state.status = TuiStatus.running
        shell._sync_view()

        await shell._handle_rpc_event(
            CompactionStarted(
                session_id="session-1",
                reason="threshold",
                source_entry_count=6,
                trigger_budget=_context_budget(estimated=81),
            )
        )
        await shell._handle_rpc_event(
            CompactionCompleted(
                session_id="session-1",
                reason="threshold",
                outcome="failed",
                replaced_entry_count=5,
                retained_entry_count=1,
                error="summary failed",
            )
        )

        assert shell.state.current_command_type == "prompt"
        assert shell.state.status is TuiStatus.running
        assert shell.view.status == "running"
        assert renderer.state.transcript[-1].role == "system"
        assert renderer.state.transcript[-1].style == "yellow"

        await shell._handle_rpc_event(
            RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True)
        )
        assert shell.state.current_command_type is None
        assert shell.state.status is TuiStatus.idle
        assert shell.view.status == "idle"

    anyio.run(run)


def test_tui_shell_bare_compact_passes_no_instructions_and_shows_help() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/help", "/compact"]),
        )

        await shell.run()

        assert controller.compactions == [None]
        assert controller.prompts == []
        assert "/compact [instructions]" in output.getvalue()

    anyio.run(run)


def test_tui_shell_queues_prompt_during_compaction_and_runs_it_after_success() -> None:
    async def run() -> None:
        controller = ScriptedController(
            compact_events=[
                (
                    0.05,
                    [
                        CompactionStarted(session_id="session-1", source_entry_count=4),
                        CompactionCompleted(
                            session_id="session-1",
                            outcome="completed",
                            replaced_entry_count=3,
                            retained_entry_count=1,
                        ),
                        RpcCommandFinished(command_id="compact-1", command_type="compact", ok=True),
                    ],
                )
            ]
        )
        inputs = deque(["/compact", "use the compacted context"])

        async def read(_prompt: str) -> str:
            if inputs:
                return inputs.popleft()
            await anyio.sleep(0.15)
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.compactions == [None]
        assert controller.prompts == ["use the compacted context"]
        assert "queued follow-up #1" in output.getvalue()
        assert "Compacted 3 context entries." in output.getvalue()

    anyio.run(run)


def test_tui_shell_failed_compaction_runs_queued_prompt_without_duplicate_error() -> None:
    async def run() -> None:
        controller = ScriptedController(
            compact_events=[
                (
                    0.05,
                    [
                        ErrorEvent(message="summary failed"),
                        CompactionCompleted(
                            session_id="session-1",
                            outcome="failed",
                            replaced_entry_count=3,
                            retained_entry_count=1,
                            error="summary failed",
                        ),
                        RpcCommandFinished(
                            command_id="compact-1",
                            command_type="compact",
                            ok=False,
                            error="summary failed",
                        ),
                    ],
                )
            ]
        )
        inputs = deque(["/compact", "run with unchanged context"])

        async def read(_prompt: str) -> str:
            if inputs:
                return inputs.popleft()
            await anyio.sleep(0.1)
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.prompts == ["run with unchanged context"]
        assert list(shell.state.queued_prompts) == []
        rendered = output.getvalue()
        assert rendered.count("summary failed") == 1

    anyio.run(run)


def test_tui_shell_compact_send_failure_remains_usable() -> None:
    class FailingCompactController(ScriptedController):
        async def compact(
            self,
            instructions: str | None = None,
            *,
            command_id: str | None = None,
        ) -> str:
            self.compactions.append(instructions)
            raise RuntimeError("pipe closed")

    async def run() -> None:
        controller = FailingCompactController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/compact keep this", "/help", "/quit"]),
        )

        await shell.run()

        assert controller.compactions == ["keep this"]
        assert shell.state.current_command_id is None
        assert shell.state.current_command_type is None
        assert "failed to send compact: pipe closed" in output.getvalue()
        assert "Commands:" in output.getvalue()

    anyio.run(run)
