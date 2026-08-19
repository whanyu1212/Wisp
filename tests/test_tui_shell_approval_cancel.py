# ruff: noqa: F403,F405

from __future__ import annotations

from tests.tui_support import *
from wisp.tui.input_types import TuiSubmission


def test_tui_shell_preserves_error_footer_after_failed_prompt_completion() -> None:
    async def run() -> None:
        class RecordingFullscreenRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.snapshots: list[TuiViewSnapshot] = []

            def view_updated(self, snapshot: TuiViewSnapshot) -> None:
                self.snapshots.append(snapshot)
                super().view_updated(snapshot)

        renderer = RecordingFullscreenRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell.state.queued_prompts.append("queued")
        failed = RpcCommandFinished(
            command_id="prompt-1",
            command_type="prompt",
            ok=False,
            error="failed",
        )

        shell._render_event(failed)
        should_exit = await shell._finish_current_prompt(failed)

        assert should_exit is False
        assert shell.state.status is TuiStatus.idle
        assert renderer.snapshots[-1].status == "error"
        assert renderer.snapshots[-1].input_hint == "wisp> "
        assert renderer.snapshots[-1].queued_follow_ups == 0

    anyio.run(run)


def test_tui_shell_preserves_cancelled_footer_after_cancelled_prompt_completion() -> None:
    async def run() -> None:
        class RecordingFullscreenRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.snapshots: list[TuiViewSnapshot] = []

            def view_updated(self, snapshot: TuiViewSnapshot) -> None:
                self.snapshots.append(snapshot)
                super().view_updated(snapshot)

        renderer = RecordingFullscreenRenderer()
        shell = TuiShell(ScriptedController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell.state.cancel_requested = True
        cancelled = RpcCommandFinished(
            command_id="prompt-1",
            command_type="prompt",
            ok=False,
            error="RPC command cancelled: prompt-1",
        )

        shell._render_event(cancelled)
        should_exit = await shell._finish_current_prompt(cancelled)

        assert should_exit is False
        assert shell.state.status is TuiStatus.idle
        assert all(snapshot.status != "error" for snapshot in renderer.snapshots)
        assert renderer.snapshots[-1].status == "idle"
        assert any(entry.content == "cancelled" for entry in renderer.state.transcript)

    anyio.run(run)


def test_tui_shell_preserves_error_footer_when_approval_send_fails() -> None:
    class FailingApprovalController(ScriptedController):
        async def approve(
            self,
            call_id: str,
            *,
            approved: bool = True,
            reason: str | None = None,
            command_id: str | None = None,
        ) -> str:
            raise RuntimeError("approval pipe closed")

    async def run() -> None:
        class RecordingFullscreenRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.snapshots: list[TuiViewSnapshot] = []

            def view_updated(self, snapshot: TuiViewSnapshot) -> None:
                self.snapshots.append(snapshot)
                super().view_updated(snapshot)

        renderer = RecordingFullscreenRenderer()
        shell = TuiShell(FailingApprovalController(), renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="bash",
            arguments={"command": "echo hi"},
            safety="command",
        )

        should_exit = await shell._answer_pending_approval("y", exit_after_denial=False)

        assert should_exit is True
        assert shell.state.pending_approval is None
        assert renderer.snapshots[-1].status == "error"
        assert any(
            "failed to send approval" in entry.content for entry in renderer.state.transcript
        )

    anyio.run(run)


def test_tui_shell_interrupt_cancels_running_prompt() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                (
                    0.05,
                    [
                        ErrorEvent(message="RPC command cancelled: prompt-1"),
                        RpcCommandFinished(
                            command_id="prompt-1",
                            command_type="prompt",
                            ok=False,
                            error="RPC command cancelled: prompt-1",
                        ),
                    ],
                )
            ]
        )

        interrupted = False

        async def read(prompt: str) -> str:
            nonlocal interrupted
            if prompt == "wisp> ":
                return "hello"
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.cancelled == ["prompt-1"]
        assert controller.shutdown_count == 1
        rendered = output.getvalue()
        assert "Cancelling current prompt" in rendered
        assert "cancelled" in rendered
        assert "command failed" not in rendered
        assert "error: RPC command cancelled" not in rendered

    anyio.run(run)


def test_tui_shell_preserves_cancelling_footer_while_cancel_is_pending() -> None:
    async def run() -> None:
        class RecordingFullscreenRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.snapshots: list[TuiViewSnapshot] = []

            def view_updated(self, snapshot: TuiViewSnapshot) -> None:
                self.snapshots.append(snapshot)
                super().view_updated(snapshot)

        controller = ScriptedController()
        renderer = RecordingFullscreenRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running

        should_exit = await shell._cancel_current("Cancelling current prompt...")

        assert should_exit is False
        assert controller.cancelled == ["prompt-1"]
        assert shell.state.status is TuiStatus.running
        assert renderer.snapshots[-1].status == "cancelling"
        assert all(snapshot.status != "running" for snapshot in renderer.snapshots)

    anyio.run(run)


def test_tui_shell_ignores_repeated_cancel_for_same_prompt() -> None:
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(controller, console=console)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running

        first_exit = await shell._cancel_current("Cancelling current prompt...")
        second_exit = await shell._cancel_current("Cancelling current prompt...")

        assert first_exit is False
        assert second_exit is False
        assert controller.cancelled == ["prompt-1"]
        assert "cancel already requested" in output.getvalue()

    anyio.run(run)


def test_tui_shell_separates_streamed_text_from_tool_events() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    message_delta(delta="partial"),
                    completed_message(content="partial"),
                    ToolCallRequested(call_id="call-1", name="read", arguments={"path": "x"}),
                    ToolResultReady(
                        call_id="call-1",
                        name="read",
                        output="ok",
                        is_error=False,
                    ),
                    completed_message(content="final answer"),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["use tool"]),
        )

        await shell.run()

        rendered = output.getvalue()
        assert "partial\n→ tool" in rendered
        assert rendered.count("partial") == 1
        assert "final answer" in rendered

    anyio.run(run)


def test_tui_shell_denies_tool_approval() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_line(_InputLine(text="n", mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI")]
        assert shell.state.pending_approval is None

    anyio.run(run)


def test_tui_shell_allows_exact_tool_for_session() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="bash",
            arguments={"command": "echo hi"},
            safety="command",
        )

        should_exit = await shell._handle_input_line(_InputLine(text="t", mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", True, None)]
        assert controller.approval_scopes == ["tool_session"]
        assert shell.state.pending_approval is None

    anyio.run(run)


def test_tui_shell_allows_all_tools_without_second_confirmation() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="bash",
            arguments={"command": "echo hi"},
            safety="command",
        )

        should_exit = await shell._handle_input_line(_InputLine(text="a", mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", True, None)]
        assert controller.approval_scopes == ["all_session"]
        assert shell.state.pending_approval is None

    anyio.run(run)


def test_tui_shell_first_ctrl_c_only_arms_then_second_quits_pending_approval() -> None:
    from wisp.tui.state import _InputMode, _QuitPressed

    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="write",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        assert not await shell._handle_quit_pressed(
            _QuitPressed(mode=_InputMode.approval, pressed_at=10.0)
        )
        assert controller.approvals == []

        assert not await shell._handle_quit_pressed(
            _QuitPressed(mode=_InputMode.approval, pressed_at=11.0)
        )
        assert controller.approvals == [("call-1", False, "Denied from TUI: quit requested")]
        assert shell.state.exit_requested

    anyio.run(run)


def test_tui_shell_escape_denies_pending_approval_safely() -> None:
    from wisp.tui.state import _InputCancelled, _InputMode

    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="write",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_cancelled(_InputCancelled(mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI: cancelled")]

    anyio.run(run)


def test_tui_shell_interrupt_during_approval_preserves_queued_prompts() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )
        shell.state.queued_prompts.append("already-queued follow-up")

        should_exit = await shell._handle_input_interrupted(
            _InputInterrupted(mode=_InputMode.approval)
        )

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI: interrupted")]
        assert [submission.content for submission in shell._queued_submissions()] == [
            "already-queued follow-up"
        ]

    anyio.run(run)


def test_tui_shell_cancelling_running_prompt_restores_queued_prompts() -> None:
    async def run() -> None:
        controller = ScriptedController()

        class SpyRenderer(FullscreenTuiRenderer):
            def __init__(self) -> None:
                super().__init__(_console()[0], clear_screen=False)
                self.restored: tuple[TuiSubmission, ...] = ()

            def restore_submissions(self, submissions: tuple[TuiSubmission, ...]) -> bool:
                self.restored = submissions
                return True

        renderer = SpyRenderer()
        shell = TuiShell(controller, renderer=renderer)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell.state.queued_prompts.append("doomed follow-up")

        should_exit = await shell._handle_input_interrupted(
            _InputInterrupted(mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.cancelled == ["prompt-1"]
        assert list(shell.state.queued_prompts) == []
        assert [submission.content for submission in renderer.restored] == ["doomed follow-up"]

    anyio.run(run)


def test_tui_shell_does_not_approve_from_stale_running_input() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_line(
            _InputLine(text="yes", mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.approvals == []
        assert [submission.content for submission in shell._queued_submissions()] == ["yes"]

    anyio.run(run)


def test_tui_shell_answers_trust_from_stale_running_yes_no_input() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_trust
        shell.state.pending_trust = TrustRequested(
            request_id="req-1",
            project_path=Path("/some/project"),
        )

        should_exit = await shell._handle_input_line(_InputLine(text="y", mode=_InputMode.running))

        assert should_exit is False
        assert controller.trusts == [("req-1", True, None, False)]
        assert shell.state.pending_trust is None
        assert list(shell.state.queued_prompts) == []

    anyio.run(run)


def test_tui_shell_queues_non_answer_from_stale_running_trust_input() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_trust
        shell.state.pending_trust = TrustRequested(
            request_id="req-1",
            project_path=Path("/some/project"),
        )

        should_exit = await shell._handle_input_line(
            _InputLine(text="follow up", mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.trusts == []
        assert shell.state.pending_trust is not None
        assert [submission.content for submission in shell._queued_submissions()] == ["follow up"]

    anyio.run(run)


def test_tui_shell_denies_approval_on_running_tagged_interrupt_for_safety() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_interrupted(
            _InputInterrupted(mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI: interrupted")]
        assert shell.state.pending_approval is None

    anyio.run(run)


def test_tui_shell_denies_tool_approval_on_blank_answer() -> None:
    async def run() -> None:
        controller = ScriptedController()
        shell = TuiShell(controller)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.waiting_for_approval
        shell.state.pending_approval = ToolApprovalRequested(
            call_id="call-1",
            name="danger",
            arguments={"path": "file.txt"},
            safety="mutating",
        )

        should_exit = await shell._handle_input_line(_InputLine(text="", mode=_InputMode.approval))

        assert should_exit is False
        assert controller.approvals == [("call-1", False, "Denied from TUI")]
        assert list(shell.state.queued_prompts) == []

    anyio.run(run)


def test_tui_shell_denies_approval_on_input_eof_then_shutdown() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    ToolApprovalRequested(
                        call_id="call-1",
                        name="danger",
                        arguments={"path": "file.txt"},
                        safety="mutating",
                    )
                ]
            ],
            approval_events=[
                [
                    ToolApprovalResolved(
                        call_id="call-1",
                        name="danger",
                        approved=False,
                        reason="Denied from TUI: input closed",
                    ),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ],
        )

        async def read(prompt: str) -> str:
            if prompt == "wisp> ":
                return "use tool"
            raise EOFError

        console, output = _console()
        shell = TuiShell(controller, console=console, prompt_reader=read)

        await shell.run()

        assert controller.approvals == [("call-1", False, "Denied from TUI: input closed")]
        assert controller.shutdown_count == 1
        assert "Approval input closed" in output.getvalue()

    anyio.run(run)


def test_tui_shell_reports_prompt_send_failure() -> None:
    class FailingPromptController(ScriptedController):
        async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
            self.prompts.append(prompt)
            raise RuntimeError("[red]closed pipe[/red]")

    async def run() -> None:
        controller = FailingPromptController([])
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        rendered = output.getvalue()
        assert "failed to send prompt" in rendered
        assert "[red]closed pipe[/red]" in rendered

    anyio.run(run)


def test_tui_shell_reports_shutdown_failure_with_original_wording() -> None:
    class FailingShutdownController(ScriptedController):
        async def shutdown(self, *, command_id: str | None = None) -> str:
            self.shutdown_count += 1
            raise RuntimeError("closed")

    async def run() -> None:
        controller = FailingShutdownController()
        console, output = _console()
        shell = TuiShell(controller, console=console)

        should_exit = await shell._request_shutdown()

        assert should_exit is True
        assert "shutdown failed: closed" in output.getvalue()

    anyio.run(run)


def test_tui_shell_reports_rpc_eof_before_prompt_finished() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [[ErrorEvent(message="Unknown provider")]],
            close_after_prompt=True,
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["hello"]),
        )

        await shell.run()

        assert controller.prompts == ["hello"]
        assert controller.shutdown_count == 0
        rendered = output.getvalue()
        assert "Unknown provider" in rendered
        assert "RPC event stream ended before command completed: prompt-1" in rendered

    anyio.run(run)


def test_tui_shell_escapes_approval_markup() -> None:
    async def run() -> None:
        controller = ScriptedController(
            [
                [
                    ToolApprovalRequested(
                        call_id="call-1",
                        name="[red]write[/red]",
                        arguments={"path": "[black]hidden[/black]"},
                        safety="mutating",
                    ),
                    RpcCommandFinished(command_id="prompt-1", command_type="prompt", ok=True),
                ]
            ]
        )
        console, output = _console()
        shell = TuiShell(
            controller,
            console=console,
            prompt_reader=await _reader_from(["use tool", "y"]),
        )

        await shell.run()

        rendered = output.getvalue()
        assert "[red]write[/red]" in rendered
        assert "[black]hidden[/black]" in rendered

    anyio.run(run)


def test_tui_shell_compacts_session_saved_path(tmp_path: Path) -> None:
    console, output = _console()
    shell = TuiShell(ScriptedController(), console=console)

    shell._render_event(
        SessionSaved(
            session_id="session-1",
            path=tmp_path / "20260703-123456-abcdef12.jsonl",
        )
    )

    rendered = output.getvalue()
    assert "session saved: 20260703-123456-abcdef12.jsonl" in rendered
    assert str(tmp_path) not in rendered


def test_tui_interaction_state_tracks_status() -> None:
    state = TuiInteractionState()

    assert state.status is TuiStatus.idle
    state.status = TuiStatus.running
    assert state.status is TuiStatus.running


def test_tui_shell_tracks_compacting_until_compact_command_finishes() -> None:
    async def run() -> None:
        controller = ScriptedController(compact_events=[[]])
        shell = TuiShell(controller, console=_console()[0])

        should_exit = await shell._start_compaction(None)

        assert should_exit is False
        assert shell.state.status is TuiStatus.compacting
        assert shell.state.current_command_id == "compact-1"
        assert shell.state.current_command_type == "compact"
        assert shell.view.status == "compacting"
        assert shell.view.input_mode == "running"
        assert shell.view.input_hint == "wisp(compacting)> "

        await shell._handle_rpc_event(
            RpcCommandFinished(command_id="compact-1", command_type="compact", ok=True)
        )

        assert shell.state.status is TuiStatus.idle
        assert shell.state.current_command_id is None
        assert shell.state.current_command_type is None

    anyio.run(run)


def test_tui_shell_rejects_other_slash_commands_during_compaction() -> None:
    async def run() -> None:
        controller = ScriptedController(compact_events=[[]])
        console, output = _console()
        shell = TuiShell(controller, console=console)
        await shell._start_compaction(None)

        should_exit = await shell._handle_input_line(
            _InputLine(text="/model gpt-5.5", mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.configurations == []
        assert "Cannot run slash commands while compaction is running." in output.getvalue()

    anyio.run(run)


def test_tui_shell_trust_resolution_restores_compacting_status() -> None:
    async def run() -> None:
        controller = ScriptedController(compact_events=[[]])
        shell = TuiShell(controller, console=_console()[0])
        await shell._start_compaction(None)
        shell.state.pending_trust = TrustRequested(
            request_id="req-1", project_path=Path("/some/project")
        )
        shell.state.status = TuiStatus.waiting_for_trust

        should_exit = await shell._answer_pending_trust("y")

        assert should_exit is False
        assert controller.trusts == [("req-1", True, None, False)]
        assert shell.state.status is TuiStatus.compacting
        assert shell.view.status == "compacting"
        assert shell.view.input_hint == "wisp(compacting)> "

    anyio.run(run)


def test_tui_shell_interrupt_cancels_compaction_and_clears_queue() -> None:
    async def run() -> None:
        controller = ScriptedController(compact_events=[[]])
        console, output = _console()
        shell = TuiShell(controller, console=console)
        await shell._start_compaction(None)
        shell.state.queued_prompts.append("do not run")

        should_exit = await shell._handle_input_interrupted(
            _InputInterrupted(mode=_InputMode.running)
        )

        assert should_exit is False
        assert controller.cancelled == ["compact-1"]
        assert list(shell.state.queued_prompts) == []
        assert "Cancelling compaction..." in output.getvalue()

        await shell._handle_rpc_event(
            CompactionCompleted(
                session_id="session-1",
                outcome="cancelled",
                replaced_entry_count=0,
                retained_entry_count=0,
            )
        )
        await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="compact-1",
                command_type="compact",
                ok=False,
                error="RPC command cancelled: compact-1",
            )
        )

        rendered = output.getvalue()
        assert rendered.count("Compaction cancelled.") == 1
        assert "command failed" not in rendered

    anyio.run(run)


def test_tui_shell_quit_targets_active_compaction_then_shuts_down() -> None:
    async def run() -> None:
        controller = ScriptedController(compact_events=[[]])
        console, output = _console()
        shell = TuiShell(controller, console=console)
        await shell._start_compaction(None)

        should_exit = await shell._handle_quit()

        assert should_exit is False
        assert controller.cancelled == ["compact-1"]
        assert "Quit requested; cancelling compaction..." in output.getvalue()

        should_exit = await shell._handle_rpc_event(
            RpcCommandFinished(
                command_id="compact-1",
                command_type="compact",
                ok=False,
                error="RPC command cancelled: compact-1",
            )
        )

        assert should_exit is False
        assert controller.shutdown_count == 1

    anyio.run(run)


def test_tui_shell_eof_waits_for_compaction_and_drops_follow_ups() -> None:
    async def run() -> None:
        controller = ScriptedController(compact_events=[[]])
        console, output = _console()
        shell = TuiShell(controller, console=console)
        await shell._start_compaction(None)
        shell.state.queued_prompts.append("do not run")

        should_exit = await shell._handle_input_closed(_InputClosed(mode=_InputMode.running))

        assert should_exit is False
        assert list(shell.state.queued_prompts) == []
        assert controller.cancelled == []
        assert "input closed; finishing compaction" in output.getvalue()

        should_exit = await shell._handle_rpc_event(
            RpcCommandFinished(command_id="compact-1", command_type="compact", ok=True)
        )

        assert should_exit is False
        assert controller.prompts == []
        assert controller.shutdown_count == 1

    anyio.run(run)


def test_tui_shell_denies_approval_that_arrives_after_cancel_without_reopening_prompt() -> None:
    # Regression: the agent can decide to call an approval-requiring tool and
    # only then reach the checkpoint where cancellation actually takes effect.
    # If the reader's cancel is sent first, ToolApprovalRequested for that same
    # in-flight command can still arrive afterward. It must be auto-denied
    # quietly -- reopening the prompt would contradict a cancel the reader
    # already sent and could mislead them into thinking it didn't register.
    async def run() -> None:
        controller = ScriptedController()
        console, output = _console()
        shell = TuiShell(controller, console=console)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running

        should_exit = await shell._cancel_current("cancelling")
        assert should_exit is False
        assert controller.cancelled == ["prompt-1"]

        should_exit = await shell._handle_rpc_event(
            ToolApprovalRequested(
                call_id="call-1",
                name="bash",
                arguments={"command": "echo hi"},
                safety="command",
            )
        )

        assert should_exit is False
        assert shell.state.pending_approval is None
        assert shell.state.status is not TuiStatus.waiting_for_approval
        assert controller.approvals == [("call-1", False, "Denied from TUI: cancelling")]
        assert "approval required" not in output.getvalue()

    anyio.run(run)
