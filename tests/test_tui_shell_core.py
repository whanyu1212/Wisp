# ruff: noqa: F403,F405

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from tempfile import TemporaryDirectory

from pytest import MonkeyPatch

from tests.tui_support import *
from wisp.auth.storage import JsonAuthStore, OAuthCredential
from wisp.events import (
    BillableTokenUsage,
    CompactionPolicyStatus,
    ContextBudget,
    ContextEstimate,
    ContextEstimated,
    MessageCompleted,
    MessageDelta,
    MessageRole,
    MessageStarted,
    ProviderRetrying,
    RpcMessageSnapshot,
    RpcMessagesReported,
    RpcMessageToolCallSnapshot,
    RpcMessageToolResultSnapshot,
    RpcSessionSelected,
    RpcSessionsReported,
    RpcSessionSummary,
    SessionCostSummary,
    SessionStats,
    SessionStatsReported,
    TokenUsage,
    UsageCost,
    UsageCostRates,
)
from wisp.tui import auth_commands as tui_auth_commands_module
from wisp.tui.commands import DEFAULT_TUI_COMMAND_CATALOG, TuiCommandCatalog
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    TUI_HISTORY_PAGE_LIMIT,
    HistoricalToolCard,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
)
from wisp.tui.state import TuiCancelRequested, TuiViewState, _InputCancelled
from wisp.update_check import UpdateAvailable, UpdateStatus


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


def test_tui_shell_history_dispatches_renderer_request_and_rejects_arguments() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_requests = 0
            self.errors: list[str] = []

        def prompt_history_request(self) -> None:
            self.history_requests += 1

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)

        await shell._handle_input_line(_InputLine("/history", _InputMode.idle))
        await shell._handle_input_line(_InputLine("/history extra", _InputMode.idle))
        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"
        await shell._handle_input_line(_InputLine("/history", _InputMode.running))

        assert renderer.history_requests == 2
        assert renderer.errors == ["Usage: /history"]

    anyio.run(run)


def test_tui_update_runs_in_background_without_blocking_shell_signals() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_requests = 0
            self.notices: list[str] = []
            self.errors: list[str] = []

        def prompt_history_request(self) -> None:
            self.history_requests += 1

        def notice(self, message: str) -> None:
            self.notices.append(message)

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        started = anyio.Event()
        release = anyio.Event()

        async def check() -> UpdateStatus:
            started.set()
            await release.wait()
            return UpdateStatus("1.0.0", "1.1.0")

        renderer = RecordingRenderer()
        shell = TuiShell(
            ScriptedController(),
            renderer=renderer,
            manual_update_checker=check,
        )
        async with anyio.create_task_group() as task_group:
            shell._task_group = task_group
            await shell._handle_input_line(_InputLine("/update", _InputMode.idle))
            await started.wait()

            await shell._handle_input_line(_InputLine("/history", _InputMode.idle))
            await shell._handle_input_line(_InputLine("do work", _InputMode.idle))

            assert renderer.history_requests == 1
            assert renderer.errors == [
                "Cannot submit prompts while a Wisp update operation is in progress."
            ]
            assert shell._update_cancel_scope is not None
            await shell._handle_input_cancelled(_InputCancelled(_InputMode.idle))
            while shell._update_cancel_scope is not None:
                await anyio.sleep(0)
            task_group.cancel_scope.cancel()

        assert renderer.notices == [
            "Checking PyPI for Wisp updates...",
            "Wisp update cancelled.",
        ]

    anyio.run(run)


def test_tui_update_installation_finishes_safely_after_cancel_request() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.notices: list[str] = []

        def notice(self, message: str) -> None:
            self.notices.append(message)

    async def run() -> None:
        install_started = anyio.Event()
        release_install = anyio.Event()

        async def check() -> UpdateStatus:
            return UpdateStatus("1.0.0", "1.1.0")

        async def install(
            update: UpdateAvailable,
            *,
            on_install_started: Callable[[], None] | None = None,
        ) -> None:
            if on_install_started is not None:
                on_install_started()
            install_started.set()
            await release_install.wait()

        renderer = RecordingRenderer()
        shell = TuiShell(
            ScriptedController(),
            renderer=renderer,
            manual_update_checker=check,
            update_installer=install,
        )
        async with anyio.create_task_group() as task_group:
            shell._task_group = task_group
            await shell._handle_input_line(_InputLine("/update install", _InputMode.idle))
            await install_started.wait()

            await shell._handle_input_cancelled(_InputCancelled(_InputMode.idle))

            assert shell._update_cancel_scope is not None
            assert shell._updates.installing is True
            release_install.set()
            while shell._update_cancel_scope is not None:
                await anyio.sleep(0)
            task_group.cancel_scope.cancel()

        assert renderer.notices == [
            "Checking PyPI for Wisp updates...",
            "Wisp update installation is in progress; waiting for it to finish safely.",
            "Updated Wisp to 1.1.0. Restart Wisp to use the new version.",
        ]

    anyio.run(run)


def test_tui_update_provenance_verification_remains_cancellable() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.notices: list[str] = []

        def notice(self, message: str) -> None:
            self.notices.append(message)

    async def run() -> None:
        verification_started = anyio.Event()
        release_verification = anyio.Event()

        async def check() -> UpdateStatus:
            return UpdateStatus("1.0.0", "1.1.0")

        async def install(
            update: UpdateAvailable,
            *,
            on_install_started: Callable[[], None] | None = None,
        ) -> None:
            verification_started.set()
            await release_verification.wait()
            if on_install_started is not None:
                on_install_started()

        renderer = RecordingRenderer()
        shell = TuiShell(
            ScriptedController(),
            renderer=renderer,
            manual_update_checker=check,
            update_installer=install,
        )
        async with anyio.create_task_group() as task_group:
            shell._task_group = task_group
            await shell._handle_input_line(_InputLine("/update install", _InputMode.idle))
            await verification_started.wait()

            assert shell._updates.installing is False
            await shell._handle_input_cancelled(_InputCancelled(_InputMode.idle))
            while shell._update_cancel_scope is not None:
                await anyio.sleep(0)
            task_group.cancel_scope.cancel()

        assert renderer.notices == [
            "Checking PyPI for Wisp updates...",
            "Wisp update cancelled.",
        ]

    anyio.run(run)


def test_tui_shell_starts_project_initialization_and_rejects_arguments() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.submitted: list[str] = []
            self.errors: list[str] = []

        def prompt_submitted(self, prompt: str) -> None:
            self.submitted.append(prompt)

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)

        await shell._handle_input_line(_InputLine("/init extra", _InputMode.idle))
        assert renderer.errors == ["Usage: /init"]
        assert controller.init_requests == []

        await shell._handle_input_line(_InputLine("/init", _InputMode.idle))

        assert renderer.submitted == ["/init"]
        assert controller.init_requests == ["init-1"]
        assert shell.state.current_command_id == "init-1"
        assert shell.state.current_command_type == "init"

        await shell._handle_rpc_event(
            RpcCommandFinished(command_id="init-1", command_type="init", ok=True)
        )

        assert shell.state.current_command_id is None
        assert shell.state.current_command_type is None

    anyio.run(run)


def test_tui_shell_switches_agent_mode_after_successful_configure() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller, renderer=LineTuiRenderer(_console()[0]))

        await shell._handle_input_line(_InputLine("/plan", _InputMode.idle))
        assert controller.agent_modes == ["plan"]
        command_id = next(iter(shell.pending_configures))
        await shell._handle_rpc_event(
            RpcCommandFinished(command_id=command_id, command_type="configure", ok=True)
        )

        assert shell.current_mode == "plan"
        assert shell.view.mode == "plan"

        await shell._handle_input_line(_InputLine("/build", _InputMode.idle))
        assert controller.agent_modes == ["plan", "build"]

    anyio.run(run)


def test_tui_new_session_clears_transcript_state_only_after_rpc_success() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.clears = 0
            self.notices: list[str] = []
            self.errors: list[str] = []

        def clear_session(self) -> None:
            self.clears += 1

        def notice(self, message: str) -> None:
            self.notices.append(message)

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell.view.last_session = "old-session"
        shell.view.context = _context_budget(estimated=100)
        shell.view.cost = SessionCostSummary()

        await shell._request_session_stats()
        stale_stats_command_id = controller.session_stats_requests[-1]
        await shell._handle_input_line(_InputLine("/new extra", _InputMode.idle))
        await shell._handle_input_line(_InputLine("/new", _InputMode.idle))

        assert renderer.errors == ["Usage: /new"]
        assert renderer.clears == 0
        command_id = controller.new_session_requests[-1]
        await shell._handle_rpc_event(
            RpcCommandFinished(command_id=command_id, command_type="new_session", ok=True)
        )

        assert renderer.clears == 1
        assert renderer.notices == ["Started a new session."]
        assert shell.view.last_session is None
        assert shell.view.context is None
        assert shell.view.cost is None
        assert shell.pending_new_session_command_id is None

        await shell._handle_rpc_event(
            SessionStatsReported(
                command_id=stale_stats_command_id,
                stats=SessionStats.model_construct(
                    context=_context_budget(estimated=90_000),
                    cost=SessionCostSummary(known_usd=Decimal("1.25")),
                ),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=stale_stats_command_id,
                command_type="get_session_stats",
                ok=True,
            )
        )

        assert shell.view.context is None
        assert shell.view.cost is None
        assert stale_stats_command_id not in shell._ignored_session_stats_command_ids

    anyio.run(run)


def test_tui_new_session_failure_preserves_existing_view() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.clears = 0
            self.errors: list[str] = []

        def clear_session(self) -> None:
            self.clears += 1

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell.view.last_session = "old-session"

        await shell._handle_input_line(_InputLine("/new", _InputMode.idle))
        command_id = controller.new_session_requests[-1]
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="new_session",
                ok=False,
                error="busy",
            )
        )

        assert renderer.clears == 0
        assert renderer.errors == ["busy"]
        assert shell.view.last_session == "old-session"

    anyio.run(run)


def test_tui_context_command_renders_authoritative_compaction_status() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.notices: list[str] = []
            self.errors: list[str] = []

        def notice(self, message: str) -> None:
            self.notices.append(message)

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        stats = SessionStats(
            session_id="session-1",
            entry_count=4,
            active_message_count=4,
            compaction_count=0,
            usage_record_count=1,
            usage=TokenUsage(
                input_tokens=90_000,
                output_tokens=2_000,
                total_tokens=92_000,
                cache_read_input_tokens=24_000,
                cache_write_input_tokens=8_000,
            ),
            context=_context_budget(estimated=80_000, observed=92_000, current=True),
            compaction=CompactionPolicyStatus(
                threshold_eligible=True,
                threshold_ineligible_reason=None,
            ),
        )
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)

        await shell._handle_input_line(_InputLine("/context", _InputMode.idle))
        await shell._handle_rpc_event(
            SessionStatsReported(command_id="session-stats-1", stats=stats)
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="session-stats-1",
                command_type="get_session_stats",
                ok=True,
            )
        )

        assert controller.session_stats_requests == ["session-stats-1"]
        assert renderer.errors == []
        assert renderer.notices[-1].splitlines() == [
            "Automatic compaction: on",
            "Context: 92k / 128k",
            "Trigger: >120k",
            "Reserve: 8k",
            "Usage source: provider observation",
            "Prompt cache (reported): 24k read · 8k written",
            "Threshold eligibility: eligible",
            "Overflow recovery: on",
        ]

    anyio.run(run)


def test_tui_context_command_rejects_overlapping_status_requests() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.errors: list[str] = []

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)

        await shell._handle_input_line(_InputLine("/context", _InputMode.idle))
        await shell._handle_input_line(_InputLine("/context", _InputMode.idle))

        assert controller.session_stats_requests == ["session-stats-1"]
        assert renderer.errors == ["Context status request is already pending."]

    anyio.run(run)


def test_tui_context_command_marks_legacy_compaction_policy_unavailable() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.notices: list[str] = []

        def notice(self, message: str) -> None:
            self.notices.append(message)

    async def run() -> None:
        stats = SessionStats(
            session_id="session-1",
            entry_count=0,
            active_message_count=0,
            compaction_count=0,
            usage_record_count=0,
            usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
            context=_context_budget(estimated=1_000),
        )
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)

        await shell._handle_input_line(_InputLine("/context", _InputMode.idle))
        await shell._handle_rpc_event(
            SessionStatsReported(command_id="session-stats-1", stats=stats)
        )

        assert renderer.notices[-1].splitlines()[0] == "Automatic compaction: unavailable"
        assert renderer.notices[-1].splitlines()[-1] == "Overflow recovery: unavailable"

    anyio.run(run)


def test_tui_context_toggle_uses_typed_configure_and_rejects_busy_commands() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.notices: list[str] = []
            self.errors: list[str] = []

        def notice(self, message: str) -> None:
            self.notices.append(message)

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)

        await shell._handle_input_line(_InputLine("/context auto off", _InputMode.idle))
        assert controller.auto_compaction_settings == [False]
        await shell._handle_rpc_event(
            RpcCommandFinished(command_id="configure-1", command_type="configure", ok=True)
        )
        assert "Automatic compaction disabled." in renderer.notices
        assert controller.session_stats_requests == ["session-stats-1"]

        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"
        await shell._handle_input_line(_InputLine("/context auto on", _InputMode.running))
        assert controller.auto_compaction_settings == [False]
        assert renderer.errors[-1] == "Cannot run slash commands while a prompt is running."

    anyio.run(run)


def test_tui_shell_resume_catalog_uses_rpc_owned_order_and_selection() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.catalogs: list[tuple[tuple[RpcSessionSummary, ...], str | None]] = []
            self.catalog_lifecycle: list[str] = []

        def session_catalog_started(self) -> None:
            self.catalog_lifecycle.append("start")

        def session_catalog_finished(self) -> None:
            self.catalog_lifecycle.append("finish")

        def session_picker_request(
            self,
            sessions: tuple[RpcSessionSummary, ...],
            *,
            selected_session_id: str | None,
        ) -> None:
            self.catalogs.append((sessions, selected_session_id))

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        newer = RpcSessionSummary(
            session_id="newer",
            session_path="/tmp/newer.jsonl",
            updated_at=datetime(2026, 2, 1, tzinfo=UTC),
            entry_count=4,
            name="Newer task",
        )
        older = RpcSessionSummary(
            session_id="older",
            session_path="/tmp/older.jsonl",
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            entry_count=2,
        )

        await shell._handle_resume_command(())
        assert controller.sessions_requests == [("sessions-1", 200)]
        await shell._handle_rpc_event(
            RpcSessionsReported(
                command_id="sessions-1",
                sessions=(newer, older),
                selected_session_id="older",
                selected_session_path="/tmp/older.jsonl",
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="sessions-1",
                command_type="get_sessions",
                ok=True,
            )
        )

        assert renderer.catalogs == [((newer, older), "older")]
        assert renderer.catalog_lifecycle == ["start", "finish"]
        assert shell.pending_session_catalog is None

    anyio.run(run)


def test_tui_shell_resume_replaces_history_after_selection_and_hydration() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.replacements: list[tuple[tuple[HistoricalTranscriptEntry, ...], str]] = []
            self.switches: list[str] = []

        def session_switch_started(self, session_id: str) -> None:
            self.switches.append(f"start:{session_id}")

        def session_switch_finished(self) -> None:
            self.switches.append("finish")

        def replace_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
            *,
            session_label: str,
        ) -> None:
            self.replacements.append((entries, session_label))

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell.view.context = _context_budget(estimated=100)
        shell.view.cost = SessionCostSummary()

        await shell._handle_resume_command(("target",))
        await shell._handle_rpc_event(
            RpcSessionSelected(
                command_id="select-session-1",
                session_id="target",
                session_path="/tmp/target.jsonl",
                entry_count=2,
                session_name="Target task",
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="select-session-1",
                command_type="select_session",
                ok=True,
            )
        )
        assert controller.messages_requests[-1][1:] == (None, TUI_HISTORY_MESSAGE_LIMIT, None)
        history_id = controller.messages_requests[-1][0]
        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=history_id,
                session_id="target",
                session_path="/tmp/target.jsonl",
                messages=(
                    _rpc_message("user", "historical prompt", entry_id="message-1"),
                    _rpc_message("assistant", "historical answer", entry_id="message-2"),
                ),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=history_id,
                command_type="get_messages",
                ok=True,
            )
        )

        entries, label = renderer.replacements[-1]
        assert label == "Target task"
        assert entries == (
            HistoricalTranscriptMessage(role="user", content="historical prompt"),
            HistoricalTranscriptMessage(role="assistant", content="historical answer"),
        )
        assert renderer.switches == ["start:target", "finish"]
        assert shell.view.last_session == "Target task"
        assert shell.view.context is None
        assert shell.view.cost is None
        assert shell.pending_session_switch is None

    anyio.run(run)


def test_tui_shell_uses_small_initial_history_page_for_paginating_renderer() -> None:
    class PaginatingRenderer(LineTuiRenderer):
        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

    async def run() -> None:
        controller = ScriptedController()
        renderer = PaginatingRenderer(_console()[0])
        shell = TuiShell(controller, renderer=renderer)

        await shell._request_session_history()

        assert controller.messages_requests == [("messages-1", None, TUI_HISTORY_PAGE_LIMIT, None)]

    anyio.run(run)


def test_tui_shell_paginates_older_history_pages_with_the_reported_cursor() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.page_states: list[bool] = []
            self.prepended: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def history_page_loaded(self, *, has_more: bool) -> None:
            self.page_states.append(has_more)

        def prepend_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
            self.prepended.append(entries)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                messages=(_rpc_message("assistant", "newer", entry_id="newer"),),
                truncated=True,
                next_before_entry_id="newer",
            )
        )

        assert renderer.history_page_hook is not None
        await renderer.history_page_hook()
        page_command_id = controller.messages_requests[-1][0]
        assert controller.messages_requests[-1] == (
            page_command_id,
            "target",
            TUI_HISTORY_PAGE_LIMIT,
            "newer",
        )

        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=page_command_id,
                session_id="target",
                messages=(_rpc_message("user", "older", entry_id="older"),),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=page_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert renderer.prepended == [(HistoricalTranscriptMessage(role="user", content="older"),)]
        assert renderer.page_states == [True, False]

    anyio.run(run)


def test_tui_shell_reloads_latest_history_after_retention_eviction() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.reloaded: list[tuple[HistoricalTranscriptEntry, ...]] = []
            self.page_states: list[bool] = []

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def replace_latest_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
        ) -> None:
            self.reloaded.append(entries)

        def history_page_loaded(self, *, has_more: bool) -> None:
            self.page_states.append(has_more)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                messages=(_rpc_message("assistant", "current", entry_id="current"),),
                truncated=True,
                next_before_entry_id="current",
            )
        )

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()
        latest_command_id = controller.messages_requests[-1][0]
        assert controller.messages_requests[-1] == (
            latest_command_id,
            "target",
            TUI_HISTORY_PAGE_LIMIT,
            None,
        )

        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=latest_command_id,
                session_id="target",
                messages=(_rpc_message("assistant", "latest", entry_id="latest"),),
                truncated=True,
                next_before_entry_id="latest",
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=latest_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert renderer.reloaded == [
            (HistoricalTranscriptMessage(role="assistant", content="latest"),)
        ]
        assert renderer.page_states == [True, True]

    anyio.run(run)


def test_tui_shell_preserves_older_history_cursor_when_latest_reload_defers() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.page_states: list[bool] = []

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def replace_latest_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
        ) -> bool:
            del entries
            return False

        def history_page_loaded(self, *, has_more: bool) -> None:
            self.page_states.append(has_more)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="older-cursor",
            )
        )

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()
        latest_command_id = controller.messages_requests[-1][0]
        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=latest_command_id,
                session_id="target",
                messages=(_rpc_message("assistant", "latest", entry_id="latest"),),
                truncated=True,
                next_before_entry_id="latest-cursor",
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=latest_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert shell._history_pagination is not None
        assert shell._history_pagination.next_before_entry_id == "older-cursor"
        assert renderer.page_states == [True]

    anyio.run(run)


def test_tui_shell_releases_live_reload_guard_after_a_latest_history_failure() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.latest_history_failures = 0

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def latest_history_reload_failed(self) -> None:
            self.latest_history_failures += 1

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()
        latest_command_id = controller.messages_requests[-1][0]
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=latest_command_id,
                command_type="get_messages",
                ok=False,
                error="transport closed",
            )
        )

        assert renderer.latest_history_failures == 1

    anyio.run(run)


def test_tui_shell_recovers_history_pagination_for_a_live_reload() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.latest_history_failures = 0
            self.latest_history_captures = 0
            self.reloaded: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def latest_history_reload_failed(self) -> None:
            self.latest_history_failures += 1

        def capture_latest_history_reload(self) -> None:
            self.latest_history_captures += 1

        def replace_latest_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
        ) -> None:
            self.reloaded.append(entries)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)

        await shell._request_latest_history_page()
        command_id = controller.messages_requests[-1][0]

        assert controller.messages_requests[-1] == (
            command_id,
            None,
            TUI_HISTORY_PAGE_LIMIT,
            None,
        )
        assert renderer.latest_history_captures == 1
        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=command_id,
                session_id="target",
                messages=(_rpc_message("assistant", "recovered", entry_id="recovered"),),
                truncated=True,
                next_before_entry_id="recovered",
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert shell._history_pagination is not None
        assert shell._history_pagination.session_id == "target"
        assert renderer.latest_history_failures == 0
        assert renderer.reloaded == [
            (HistoricalTranscriptMessage(role="assistant", content="recovered"),)
        ]

    anyio.run(run)


def test_tui_shell_adopts_first_session_id_during_latest_history_reload() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.latest_history_failures = 0
            self.reloaded: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def latest_history_reload_failed(self) -> None:
            self.latest_history_failures += 1

        def replace_latest_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
        ) -> None:
            self.reloaded.append(entries)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(command_id="initial-history", session_id=None)
        )

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()
        latest_command_id = controller.messages_requests[-1][0]
        assert controller.messages_requests[-1][1] is None

        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=latest_command_id,
                session_id="first-session",
                messages=(_rpc_message("assistant", "latest", entry_id="latest"),),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=latest_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert shell._history_pagination is not None
        assert shell._history_pagination.session_id == "first-session"
        assert renderer.latest_history_failures == 0
        assert renderer.reloaded == [
            (HistoricalTranscriptMessage(role="assistant", content="latest"),)
        ]

        await renderer.latest_history_hook()
        assert controller.messages_requests[-1][1] == "first-session"

    anyio.run(run)


def test_tui_shell_reloads_latest_history_after_an_older_page_finishes() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.latest_history_hook = None
            self.latest_history_captures = 0
            self.reloaded: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def prepend_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
            del entries

        def replace_latest_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
        ) -> None:
            self.reloaded.append(entries)

        def capture_latest_history_reload(self) -> None:
            self.latest_history_captures += 1

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert callable(renderer.history_page_hook)
        assert callable(renderer.latest_history_hook)
        await renderer.history_page_hook()
        older_command_id = controller.messages_requests[-1][0]
        await renderer.latest_history_hook()
        assert len(controller.messages_requests) == 1
        assert renderer.latest_history_captures == 0

        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=older_command_id,
                session_id="target",
                messages=(_rpc_message("user", "older", entry_id="older"),),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=older_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        latest_command_id = controller.messages_requests[-1][0]
        assert renderer.latest_history_captures == 1
        assert controller.messages_requests[-1] == (
            latest_command_id,
            "target",
            TUI_HISTORY_PAGE_LIMIT,
            None,
        )
        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=latest_command_id,
                session_id="target",
                messages=(_rpc_message("assistant", "latest", entry_id="latest"),),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=latest_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert renderer.reloaded == [
            (HistoricalTranscriptMessage(role="assistant", content="latest"),)
        ]

    anyio.run(run)


def test_tui_shell_reloads_latest_history_after_an_older_page_send_failure() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.latest_history_hook = None
            self.latest_history_captures = 0

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def capture_latest_history_reload(self) -> None:
            self.latest_history_captures += 1

    class FailingOlderPageController(ScriptedController):
        def __init__(self) -> None:
            super().__init__()
            self.renderer: RecordingRenderer | None = None

        async def get_messages(
            self,
            *,
            session_id: str | None = None,
            limit: int = 200,
            before_entry_id: str | None = None,
            command_id: str | None = None,
        ) -> str:
            selected_id = command_id or "unexpected-history-page"
            self.messages_requests.append((selected_id, session_id, limit, before_entry_id))
            if before_entry_id is not None:
                assert self.renderer is not None
                assert callable(self.renderer.latest_history_hook)
                await self.renderer.latest_history_hook()
                raise RuntimeError("transport closed")
            return selected_id

    async def run() -> None:
        controller = FailingOlderPageController()
        renderer = RecordingRenderer()
        controller.renderer = renderer
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert callable(renderer.history_page_hook)
        await renderer.history_page_hook()

        assert len(controller.messages_requests) == 2
        latest_request = controller.messages_requests[-1]
        assert latest_request[1:] == ("target", TUI_HISTORY_PAGE_LIMIT, None)
        assert renderer.latest_history_captures == 1
        assert shell._history_pagination is not None
        assert not shell._history_pagination.latest_reload_pending

    anyio.run(run)


def test_tui_shell_reloads_latest_history_after_the_active_prompt_finishes() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.latest_history_captures = 0

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def capture_latest_history_reload(self) -> None:
            self.latest_history_captures += 1

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )
        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()

        assert controller.messages_requests == []
        assert shell._history_pagination is not None
        assert shell._history_pagination.latest_reload_pending

        await shell._finish_current_prompt(
            RpcCommandFinished(
                command_id="prompt-1",
                command_type="prompt",
                ok=True,
            )
        )

        assert controller.messages_requests[-1][1:] == ("target", TUI_HISTORY_PAGE_LIMIT, None)
        assert renderer.latest_history_captures == 1

    anyio.run(run)


def test_tui_shell_defers_latest_history_while_prompt_submission_is_in_flight() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )
        shell.state.current_command_type = "prompt"

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()

        assert controller.messages_requests == []
        assert shell._history_pagination is not None
        assert shell._history_pagination.latest_reload_pending

    anyio.run(run)


def test_tui_shell_handles_immediate_history_page_events() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.prepended: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def prepend_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
            self.prepended.append(entries)

    class ImmediatePageController(ScriptedController):
        def __init__(self) -> None:
            super().__init__()
            self.shell: TuiShell | None = None

        async def get_messages(
            self,
            *,
            session_id: str | None = None,
            limit: int = 200,
            before_entry_id: str | None = None,
            command_id: str | None = None,
        ) -> str:
            selected_id = command_id or "unexpected-history-page"
            self.messages_requests.append((selected_id, session_id, limit, before_entry_id))
            assert self.shell is not None
            await self.shell._handle_rpc_event(
                RpcMessagesReported(
                    command_id=selected_id,
                    session_id=session_id,
                    messages=(_rpc_message("user", "older", entry_id="older"),),
                )
            )
            await self.shell._handle_rpc_event(
                RpcCommandFinished(
                    command_id=selected_id,
                    command_type="get_messages",
                    ok=True,
                )
            )
            return selected_id

    async def run() -> None:
        controller = ImmediatePageController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        controller.shell = shell
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert callable(renderer.history_page_hook)
        await renderer.history_page_hook()

        assert renderer.prepended == [(HistoricalTranscriptMessage(role="user", content="older"),)]
        assert shell._history_pagination is not None
        assert shell._history_pagination.command_id is None

    anyio.run(run)


def test_tui_shell_retries_a_failed_history_page_request() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.failures = 0
            self.errors: list[str] = []

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def history_page_request_failed(self) -> None:
            self.failures += 1

        def command_error(self, message: str) -> None:
            self.errors.append(message)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert renderer.history_page_hook is not None
        await renderer.history_page_hook()
        first_command_id = controller.messages_requests[-1][0]
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=first_command_id,
                command_type="get_messages",
                ok=False,
                error="temporary failure",
            )
        )
        await renderer.history_page_hook()

        assert renderer.failures == 1
        assert renderer.errors == ["Failed to load older session history: temporary failure"]
        assert [request[3] for request in controller.messages_requests] == ["cursor", "cursor"]

    anyio.run(run)


def test_tui_shell_ignores_history_page_events_after_pagination_is_replaced() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.history_page_hook = None
            self.prepended: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_page_request_hook(self, hook: object) -> None:
            self.history_page_hook = hook

        def prepend_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
            self.prepended.append(entries)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert renderer.history_page_hook is not None
        await renderer.history_page_hook()
        page_command_id = controller.messages_requests[-1][0]
        shell._clear_history_pagination()

        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=page_command_id,
                session_id="target",
                messages=(_rpc_message("user", "stale", entry_id="stale"),),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=page_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert renderer.prepended == []
        assert page_command_id not in shell._ignored_history_page_commands

    anyio.run(run)


def test_tui_shell_ignores_latest_history_after_pagination_is_replaced() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.reloaded: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def replace_latest_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
        ) -> None:
            self.reloaded.append(entries)

    async def run() -> None:
        controller = ScriptedController()
        renderer = RecordingRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        assert callable(renderer.latest_history_hook)
        await renderer.latest_history_hook()
        latest_command_id = controller.messages_requests[-1][0]
        shell._clear_history_pagination()

        await shell._handle_rpc_event(
            RpcMessagesReported(
                command_id=latest_command_id,
                session_id="target",
                messages=(_rpc_message("assistant", "stale", entry_id="stale"),),
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id=latest_command_id,
                command_type="get_messages",
                ok=True,
            )
        )

        assert renderer.reloaded == []
        assert latest_command_id not in shell._ignored_history_page_commands

    anyio.run(run)


def test_tui_shell_resume_selection_failure_preserves_visible_history() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.replacement_count = 0

        def replace_history_entries(
            self,
            entries: tuple[HistoricalTranscriptEntry, ...],
            *,
            session_label: str,
        ) -> None:
            self.replacement_count += 1

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.view.last_session = "original"
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="existing-history",
                session_id="original",
                truncated=True,
                next_before_entry_id="older",
            )
        )

        await shell._handle_resume_command(("missing",))
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="select-session-1",
                command_type="select_session",
                ok=False,
                error="session not found",
            )
        )

        assert renderer.replacement_count == 0
        assert shell.view.last_session == "original"
        assert shell.pending_session_switch is None
        assert shell._history_pagination is not None
        assert shell._history_pagination.next_before_entry_id == "older"

    anyio.run(run)


def test_tui_shell_resume_send_failure_finishes_switch_ui() -> None:
    class FailingSelectionController(ScriptedController):
        async def select_session(
            self,
            session_id: str,
            *,
            command_id: str | None = None,
        ) -> str:
            raise RuntimeError("RPC transport closed")

    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.started: list[str] = []
            self.finished = 0

        def session_switch_started(self, session_id: str) -> None:
            self.started.append(session_id)

        def session_switch_finished(self) -> None:
            self.finished += 1

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(FailingSelectionController(), renderer=renderer)

        await shell._handle_resume_command(("target",))

        assert renderer.started == ["target"]
        assert renderer.finished == 1
        assert shell.pending_session_switch is None

    anyio.run(run)


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
            self.streamed_messages: list[MessageCompleted] = []

        def view_updated(self, snapshot: TuiViewSnapshot) -> None:
            self.snapshots.append(snapshot)

        def record_streamed_message_completed(self, event: MessageCompleted) -> None:
            self.streamed_messages.append(event)

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
        assert [event.content for event in renderer.streamed_messages] == ["streamed answer"]

    anyio.run(run)


def test_tui_shell_keeps_text_stream_open_across_thinking_deltas() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.deltas: list[str] = []
            self.completions: list[str | None] = []

        def token_delta(self, delta: str) -> None:
            self.deltas.append(delta)

        def end_token_stream_with_content(self, completed_content: str) -> None:
            self.completions.append(completed_content)

        def end_token_stream(self) -> None:
            self.completions.append(None)

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)

        await shell._handle_rpc_event(MessageDelta(turn=1, delta="first "))
        await shell._handle_rpc_event(
            MessageDelta(turn=1, delta="private thought", content_kind="thinking")
        )
        assert shell.state.token_stream_started
        assert renderer.completions == []

        await shell._handle_rpc_event(MessageDelta(turn=1, delta="second"))
        await shell._handle_rpc_event(
            MessageCompleted(turn=1, content="first second", finish_reason="stop")
        )

        assert renderer.deltas == ["first ", "second"]
        assert renderer.completions == ["first second"]
        assert not shell.state.token_stream_started

    anyio.run(run)


def test_tui_shell_preserves_no_argument_stream_finalizer_compatibility() -> None:
    class LegacyRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.finalized = 0

        def end_token_stream(self) -> None:
            self.finalized += 1

    async def run() -> None:
        renderer = LegacyRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)

        await shell._handle_rpc_event(MessageDelta(turn=1, delta="streamed response"))
        await shell._handle_rpc_event(
            MessageCompleted(turn=1, content="authoritative response", finish_reason="stop")
        )

        assert renderer.finalized == 1
        assert not shell.state.token_stream_started

    anyio.run(run)


def test_tui_shell_finalizes_partial_stream_before_rendering_provider_failure() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.calls: list[str] = []

        def end_token_stream(self, completed_content: str | None = None) -> None:
            self.calls.append("stream completed")

        def event(self, event: KnownWispEvent) -> None:
            if isinstance(event, ErrorEvent):
                self.calls.append("error rendered")

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"

        await shell._handle_rpc_event(MessageDelta(turn=1, delta="partial response"))
        await shell._handle_rpc_event(ErrorEvent(message="provider failed"))
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="prompt-1",
                command_type="prompt",
                ok=False,
                error="provider failed",
            )
        )

        assert renderer.calls == ["stream completed", "error rendered"]
        assert not shell.state.token_stream_started
        assert not shell.state.rendered_tokens

    anyio.run(run)


def test_tui_shell_finalizes_partial_stream_when_prompt_is_cancelled() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.completions: list[str | None] = []

        def end_token_stream(self, completed_content: str | None = None) -> None:
            self.completions.append(completed_content)

    async def run() -> None:
        renderer = RecordingRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.current_command_type = "prompt"

        await shell._handle_rpc_event(MessageDelta(turn=1, delta="partial response"))
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="prompt-1",
                command_type="prompt",
                ok=False,
                error="cancelled",
            )
        )

        assert renderer.completions == [None]
        assert not shell.state.token_stream_started
        assert not shell.state.rendered_tokens

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

        assert controller.messages_requests == [
            ("messages-1", None, TUI_HISTORY_MESSAGE_LIMIT, None)
        ]
        assert controller.prompts == ["new prompt"]
        assert renderer.calls[:2] == ["history", "running"]
        assert [(message.role, message.content) for message in renderer.histories[0]] == [
            ("user", "old prompt"),
            ("assistant", "old answer"),
        ]

    anyio.run(run)


def test_tui_shell_hydrates_rich_history_entries_before_reading_prompt() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.calls: list[str] = []
            self.entries: list[tuple[HistoricalTranscriptEntry, ...]] = []

        def render_history_entries(self, entries: tuple[HistoricalTranscriptEntry, ...]) -> None:
            self.calls.append("history_entries")
            self.entries.append(entries)

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
                            _rpc_message(
                                "assistant",
                                "",
                                entry_id="assistant-1",
                                tool_calls=(
                                    RpcMessageToolCallSnapshot(
                                        call_id="call-1",
                                        name="bash",
                                        arguments={"command": "pwd"},
                                        arguments_original_bytes=17,
                                    ),
                                ),
                            ),
                            _rpc_message(
                                "tool",
                                "/repo",
                                entry_id="tool-1",
                                tool_call_id="call-1",
                                tool_name="bash",
                                tool_result=RpcMessageToolResultSnapshot(summary="ran pwd"),
                            ),
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

        assert controller.prompts == ["new prompt"]
        assert renderer.calls[:2] == ["history_entries", "running"]
        assert renderer.entries[0] == (
            HistoricalTranscriptMessage(role="user", content="old prompt"),
            HistoricalToolCard(
                card_id="history:tool-1",
                name="bash",
                arguments={"command": "pwd"},
                output="/repo",
                is_error=False,
                summary="ran pwd",
            ),
        )

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


def test_tui_shell_history_hydration_allows_legacy_renderer_without_hook() -> None:
    async def run() -> None:
        controller = ScriptedController(
            messages_events=[
                [
                    RpcMessagesReported(
                        command_id="messages-1",
                        messages=(_rpc_message("user", "old prompt", entry_id="user-1"),),
                    ),
                    RpcCommandFinished(
                        command_id="messages-1",
                        command_type="get_messages",
                        ok=True,
                    ),
                ]
            ]
        )
        renderer = LineTuiRenderer(_console()[0])
        renderer.render_history = None  # type: ignore[method-assign]
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from([]),
        )

        await shell.run()

        assert controller.messages_requests == [
            ("messages-1", None, TUI_HISTORY_MESSAGE_LIMIT, None)
        ]

    anyio.run(run)


def test_tui_shell_hydrates_rpc_command_catalog_before_accepting_input() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.command_catalogs: list[TuiCommandCatalog] = []

        def command_catalog_updated(self, catalog: TuiCommandCatalog) -> None:
            self.command_catalogs.append(catalog)

    async def run() -> None:
        controller = ScriptedController(
            commands_events=[
                [
                    RpcCommandsReported(
                        command_id="commands-1",
                        commands=(
                            RpcCommandDescriptor(
                                name="help",
                                title="Runtime help",
                                description="Show runtime help",
                                category="general",
                                aliases=("assist",),
                                slash_command="/help",
                                slash_aliases=("/assist",),
                                order=1,
                            ),
                            RpcCommandDescriptor(
                                name="extension-action",
                                title="Extension action",
                                description="No TUI handler",
                                category="general",
                                slash_command="/extension-action",
                                order=2,
                            ),
                        ),
                    ),
                    RpcCommandFinished(
                        command_id="commands-1",
                        command_type="get_commands",
                        ok=True,
                    ),
                ]
            ]
        )
        renderer = RecordingRenderer()
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from([]),
        )

        await shell.run()

        assert controller.commands_requests == ["commands-1"]
        assert len(renderer.command_catalogs) == 1
        assert renderer.command_catalogs[0].descriptors[0].title == "Runtime help"
        assert renderer.command_catalogs[0].get("/assist").name == "help"
        assert tuple(item.name for item in shell.command_catalog.descriptors) == ("help",)

    anyio.run(run)


def test_tui_shell_hydrates_and_inspects_cached_skill_catalog() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.updated: list[RpcSkillCatalogSnapshot] = []
            self.inspected: list[RpcSkillCatalogSnapshot] = []

        def skill_catalog_updated(self, catalog: RpcSkillCatalogSnapshot) -> None:
            self.updated.append(catalog)

        def skills_catalog(self, catalog: RpcSkillCatalogSnapshot) -> None:
            self.inspected.append(catalog)

    async def run() -> None:
        catalog = RpcSkillCatalogSnapshot(
            entries=(
                RpcSkillCatalogEntry(
                    name="review",
                    description="Review changes",
                    source="user:wisp",
                ),
            )
        )
        controller = ScriptedController(
            skills_events=[
                [
                    RpcSkillsReported(command_id="skills-1", catalog=catalog),
                    RpcCommandFinished(
                        command_id="skills-1",
                        command_type="get_skills",
                        ok=True,
                    ),
                ]
            ]
        )
        renderer = RecordingRenderer()
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["/skills"]),
        )

        await shell.run()

        assert controller.skills_requests == ["skills-1"]
        assert renderer.updated == [catalog]
        assert renderer.inspected == [catalog]

    anyio.run(run)


def test_tui_shell_requests_and_renders_mcp_status() -> None:
    async def run() -> None:
        controller = ScriptedController()
        renderer = LineTuiRenderer(_console()[0])
        shell = TuiShell(
            controller,
            renderer=renderer,
            prompt_reader=await _reader_from(["/mcp"]),
        )

        await shell.run()

        assert controller.mcp_requests == ["mcp-1"]
        assert "No MCP servers configured." in renderer.console.file.getvalue()

    anyio.run(run)


def test_tui_shell_command_discovery_failure_keeps_builtin_catalog() -> None:
    async def run() -> None:
        controller = ScriptedController(
            commands_events=[
                [
                    RpcCommandFinished(
                        command_id="commands-1",
                        command_type="get_commands",
                        ok=False,
                        error="registry failed",
                    )
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from([]),
        )

        await shell.run()

        assert shell.command_catalog is DEFAULT_TUI_COMMAND_CATALOG
        assert (
            "Command discovery unavailable; using built-ins: registry failed" in output.getvalue()
        )

    anyio.run(run)


def test_tui_shell_skips_history_hydration_for_legacy_controller_without_get_messages() -> None:
    async def run() -> None:
        controller = ScriptedController()
        controller.get_messages = None  # type: ignore[method-assign]
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from([]),
        )

        await shell.run()

        assert "failed to send session history" not in output.getvalue()
        assert controller.session_stats_requests == ["session-stats-1"]

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
    # header and /provider,/model,/auth,/connect stop showing the untrusted-startup ones.
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
            settings_home_dir=tmp_path / "home",
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
        assert not (tmp_path / "home" / ".wisp" / "settings.json").exists()

    anyio.run(run)


def test_tui_shell_notifies_the_renderer_of_the_adopted_auth_path(tmp_path: Path) -> None:
    # The `@`-picker snapshots its protected-path policy at startup. Deferred trust
    # can move auth_path mid-session, so the shell must tell the renderer or the
    # picker keeps offering the new credential file that the agent's tools protect.
    class _RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.adopted: list[Path] = []

        def project_auth_path_changed(self, auth_path: Path) -> None:
            self.adopted.append(auth_path)

    async def run() -> None:
        renderer = _RecordingRenderer()
        trusted_auth = tmp_path / "trusted-auth.json"
        shell = TuiShell(
            ScriptedController(),
            renderer=renderer,
            prompt_reader=await _reader_from([]),
            provider="startup-provider",
            model=None,
            auth_path=tmp_path / "startup-auth.json",
            settings_home_dir=tmp_path / "home",
        )

        await shell._handle_rpc_event(
            ProjectConfigApplied(provider="trusted-provider", auth_path=trusted_auth)
        )

        assert renderer.adopted == [trusted_auth]

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
    auth_path.chmod(0o600)

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
    auth_path.chmod(0o600)

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
        assert "Disconnected: openai-codex" not in rendered
        assert "Not logged in: openai-codex" not in rendered
        assert controller.prompts == []

    anyio.run(run)


def test_tui_shell_connect_reports_storage_errors(
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

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex_device_code", fake_login)
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{not json", encoding="utf-8")
    auth_path.chmod(0o600)

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["/connect openai-codex", "/quit"]),
            provider="openai-codex",
            auth_path=auth_path,
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Starting openai-codex device-code login..." not in rendered
        assert "Auth storage error: Invalid auth file JSON:" in rendered
        assert "Connected: openai-codex" not in rendered
        assert "access-token" not in rendered

    anyio.run(run)


def test_tui_shell_connect_and_disconnect_openai_codex(
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

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex_device_code", fake_login)

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(
                [
                    "/connect openai-codex",
                    "/auth openai-codex",
                    "/disconnect openai-codex",
                    "/quit",
                ]
            ),
            provider="openai-codex",
            auth_path=tmp_path / "auth.json",
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Connected: openai-codex" in rendered
        assert "openai-codex: oauth configured" in rendered
        assert "Disconnected: openai-codex" in rendered
        assert "access-token" not in rendered

    anyio.run(run)


def test_tui_shell_escape_cancels_non_textual_device_authorization(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = anyio.Event()
    cancelled = anyio.Event()

    async def fake_login(*_args: object, **_kwargs: object) -> OAuthCredential:
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex_device_code", fake_login)
    auth_path = tmp_path / "auth.json"
    existing_credential = OAuthCredential(
        access="existing-access",
        refresh="existing-refresh",
        expires=4_102_444_800_000,
        account_id="existing-account",
    )
    JsonAuthStore(auth_path).set("openai-codex", existing_credential)

    async def run() -> None:
        calls = 0

        async def reader(_prompt: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "/connect openai-codex"
            if calls == 2:
                await started.wait()
                return "/disconnect openai-codex"
            if calls == 3:
                return "prompt must not race reconnect"
            if calls == 4:
                raise TuiCancelRequested
            return "/quit"

        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=reader,
            provider="openai-codex",
            auth_path=auth_path,
        )

        await shell.run()

        assert cancelled.is_set()
        assert controller.prompts == []
        assert JsonAuthStore(auth_path).get("openai-codex") == existing_credential
        rendered = output.getvalue()
        assert "Cannot disconnect while a provider connection is in progress." in rendered
        assert "Cannot submit prompts while a provider connection is in progress." in rendered
        assert "Provider connection cancelled." in rendered

    anyio.run(run)


def test_tui_shell_connects_pending_provider(
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

    monkeypatch.setattr(tui_auth_commands_module, "login_openai_codex_device_code", fake_login)

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
                ["/provider openai-codex", "/connect openai-codex", "/auth", "/quit"]
            ),
            provider="fake",
            auth_path=tmp_path / "auth.json",
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Configuring provider: openai-codex" in rendered
        assert "Connected: openai-codex" in rendered
        assert "openai-codex: oauth configured" in rendered
        assert "Unknown provider" not in rendered
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
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "effort": "high",
        }

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
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
        }

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
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {"provider": "openai"}

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
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "provider": "anthropic",
            "model": "claude-opus-4-8",
        }

    with TemporaryDirectory() as tmp_dir:
        anyio.run(run, Path(tmp_dir))


def test_tui_shell_adopts_server_side_auto_switched_provider(tmp_path: Path) -> None:
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
            settings_home_dir=tmp_path,
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
        settings_path = tmp_path / ".wisp" / "settings.json"
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "provider": "openai",
            "model": "gpt-5.5-pro",
        }

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


def test_tui_shell_failed_configure_does_not_persist_model_selection(tmp_path: Path) -> None:
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
            settings_home_dir=tmp_path,
        )

        await shell.run()

        assert controller.configurations == [("missing", None, None, False)]
        assert shell.current_provider == "fake"
        rendered = output.getvalue()
        assert "Configuring provider: missing" in rendered
        assert "Provider unchanged (fake): Unknown provider: missing" in rendered
        assert "Provider set to missing" not in rendered
        assert not (tmp_path / ".wisp" / "settings.json").exists()

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


def test_tui_shell_timestamps_ctrl_c_before_signal_queueing() -> None:
    from wisp.tui.state import TuiQuitRequested, _InputClosed, _QuitPressed

    async def run() -> None:
        reads = 0

        async def read(_prompt: str) -> str:
            nonlocal reads
            reads += 1
            if reads == 1:
                raise TuiQuitRequested(pressed_at=42.5)
            raise EOFError

        shell = TuiShell(ScriptedController(), prompt_reader=read)
        send, receive = anyio.create_memory_object_stream(2)
        await shell._read_inputs(send)

        pressed = await receive.receive()
        closed = await receive.receive()
        assert isinstance(pressed, _QuitPressed)
        assert pressed.pressed_at == 42.5
        assert isinstance(closed, _InputClosed)

    anyio.run(run)


def test_tui_shell_double_ctrl_c_quits_within_window() -> None:
    from wisp.tui.state import _InputMode, _QuitPressed

    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller, console=_console()[0])

        assert not await shell._handle_quit_pressed(
            _QuitPressed(mode=_InputMode.idle, pressed_at=10.0)
        )
        assert controller.shutdown_count == 0
        assert shell._quit_armed_at == 10.0

        assert not await shell._handle_quit_pressed(
            _QuitPressed(mode=_InputMode.idle, pressed_at=11.0)
        )
        assert controller.shutdown_count == 1
        assert shell._quit_armed_at is None

    anyio.run(run)


def test_tui_shell_ctrl_c_timeout_rearms_and_input_disarms() -> None:
    from wisp.tui.state import _InputLine, _InputMode, _QuitPressed

    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller, console=_console()[0])

        await shell._handle_quit_pressed(_QuitPressed(mode=_InputMode.idle, pressed_at=10.0))
        await shell._handle_quit_pressed(_QuitPressed(mode=_InputMode.idle, pressed_at=12.0))
        assert controller.shutdown_count == 0
        assert shell._quit_armed_at == 12.0

        await shell._handle_input_line(_InputLine(text="", mode=_InputMode.idle))
        assert shell._quit_armed_at is None

    anyio.run(run)


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
            with anyio.fail_after(2):
                while len(controller.prompts) < 3:
                    await anyio.sleep(0.01)
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
        assert controller.session_stats_requests == ["session-stats-1"]
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
        assert "/context [auto on|off]" in output.getvalue()

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


def test_tui_shell_reloads_latest_history_after_a_compaction_send_failure() -> None:
    class RecordingRenderer(LineTuiRenderer):
        def __init__(self) -> None:
            super().__init__(_console()[0])
            self.latest_history_hook = None
            self.latest_history_captures = 0

        def set_history_latest_request_hook(self, hook: object) -> None:
            self.latest_history_hook = hook

        def capture_latest_history_reload(self) -> None:
            self.latest_history_captures += 1

    class FailingCompactController(ScriptedController):
        def __init__(self) -> None:
            super().__init__()
            self.renderer: RecordingRenderer | None = None

        async def compact(
            self,
            instructions: str | None = None,
            *,
            command_id: str | None = None,
        ) -> str:
            self.compactions.append(instructions)
            assert self.renderer is not None
            assert callable(self.renderer.latest_history_hook)
            await self.renderer.latest_history_hook()
            raise RuntimeError("pipe closed")

    async def run() -> None:
        controller = FailingCompactController()
        renderer = RecordingRenderer()
        controller.renderer = renderer
        shell = TuiShell(controller, renderer=renderer)
        shell._activate_history_pagination(
            RpcMessagesReported(
                command_id="initial-history",
                session_id="target",
                truncated=True,
                next_before_entry_id="cursor",
            )
        )

        await shell._start_compaction(None)

        assert controller.messages_requests[-1][1:] == ("target", TUI_HISTORY_PAGE_LIMIT, None)
        assert renderer.latest_history_captures == 1
        assert shell._history_pagination is not None
        assert not shell._history_pagination.latest_reload_pending

    anyio.run(run)


def test_tui_shell_renders_available_update_without_blocking_input() -> None:
    async def update_checker() -> UpdateAvailable:
        return UpdateAvailable(
            current_version="0.1.0a1",
            latest_version="0.1.0a2",
            update_command='uv tool install --force "wisp-ai==0.1.0a2"',
        )

    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()

        async def reader(_prompt: str) -> str:
            await anyio.sleep(0.01)
            return "/quit"

        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=reader,
            update_checker=update_checker,
        )

        await shell.run()

        rendered = output.getvalue()
        assert "Wisp 0.1.0a2 is available (current 0.1.0a1)." in rendered
        assert 'uv tool install --force "wisp-ai==0.1.0a2"' in rendered

    anyio.run(run)
