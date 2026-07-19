# ruff: noqa: F403,F405

from __future__ import annotations

from tests.cli_support import *
from wisp.agent.messages import CompactionRecord, SessionEntry
from wisp.cli.output import _print_event_line
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ContextBudget,
    ContextEstimate,
    ErrorEvent,
    MessageCompleted,
    WispEvent,
)
from wisp.providers.base import ContextOverflowError
from wisp.sessions.replay import HISTORICAL_CONTEXT_SUMMARY_LABEL


def _trigger_budget() -> ContextBudget:
    return ContextBudget(
        estimate=ContextEstimate(
            system_tokens=10,
            message_tokens=70,
            tool_schema_tokens=1,
            total_tokens=81,
        ),
        context_window=100,
        reserve_tokens=20,
        remaining_tokens=-1,
        estimated_percent=81,
        over_budget=True,
    )


def test_print_output_renders_threshold_compaction_as_automatic_notices() -> None:
    assert (
        _print_event_line(
            CompactionStarted(
                session_id="session-1",
                reason="threshold",
                source_entry_count=6,
                trigger_budget=_trigger_budget(),
            )
        )
        == "Context threshold reached; compacting automatically..."
    )
    assert (
        _print_event_line(
            CompactionCompleted(
                session_id="session-1",
                reason="threshold",
                outcome="completed",
                replaced_entry_count=5,
                retained_entry_count=1,
            )
        )
        == "Automatically compacted 5 context entries."
    )
    assert (
        _print_event_line(
            CompactionCompleted(
                session_id="session-1",
                reason="threshold",
                outcome="completed",
                replaced_entry_count=5,
                retained_entry_count=1,
                error="Event publication failed: listener failed",
            )
        )
        == "Automatically compacted 5 context entries. Warning: Event publication failed: "
        "listener failed"
    )
    assert (
        _print_event_line(
            CompactionCompleted(
                session_id="session-1",
                reason="threshold",
                outcome="failed",
                replaced_entry_count=5,
                retained_entry_count=1,
                error="summary failed",
            )
        )
        == "Automatic compaction failed: summary failed"
    )


def test_print_output_leaves_manual_compaction_unrendered() -> None:
    assert (
        _print_event_line(CompactionStarted(session_id="session-1", source_entry_count=6)) is None
    )


def test_print_output_renders_overflow_recovery_notices() -> None:
    assert (
        _print_event_line(
            CompactionStarted(
                session_id="session-1",
                reason="overflow",
                source_entry_count=6,
                trigger_budget=_trigger_budget(),
            )
        )
        == "Context overflow detected; compacting before one retry..."
    )
    assert (
        _print_event_line(
            CompactionCompleted(
                session_id="session-1",
                reason="overflow",
                outcome="completed",
                replaced_entry_count=5,
                retained_entry_count=1,
                will_retry=True,
            )
        )
        == "Compacted 5 context entries; retrying request..."
    )
    assert (
        _print_event_line(
            CompactionCompleted(
                session_id="session-1",
                reason="overflow",
                outcome="failed",
                replaced_entry_count=5,
                retained_entry_count=1,
                error="summary failed",
            )
        )
        == "Context overflow recovery failed: summary failed"
    )
    assert (
        _print_event_line(
            CompactionCompleted(
                session_id="session-1",
                reason="overflow",
                outcome="completed",
                replaced_entry_count=5,
                retained_entry_count=1,
                error="replay reload failed",
            )
        )
        == "Context overflow recovery failed: replay reload failed"
    )


def test_print_mode_does_not_duplicate_rendered_overflow_recovery_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def failed_run(
        self: CodingSession, *args: object, **kwargs: object
    ) -> AsyncIterator[WispEvent]:
        del self, args, kwargs
        message = "Context overflow recovery failed: summary failed"
        yield CompactionCompleted(
            session_id="session-1",
            reason="overflow",
            outcome="failed",
            replaced_entry_count=1,
            retained_entry_count=2,
            error="summary failed",
        )
        yield ErrorEvent(message=message)
        raise ContextOverflowError(message)

    monkeypatch.setattr(CodingSession, "run", failed_run)
    result = CliRunner().invoke(
        app,
        ["-p", "hello", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr.count("Context overflow recovery failed: summary failed") == 1


def test_print_mode_outputs_response_and_writes_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "fake response to: hello\n"
    assert "session saved:" in result.stderr

    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1

    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert "You are Wisp" in records[0]["message"]["content"]
    assert "allowed tools: none exposed to the model" in records[1]["message"]["content"]


def test_print_mode_outputs_completion_only_response(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_completion_only_runtime)

    result = CliRunner().invoke(
        app,
        ["-p", "hello", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "completion-only-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "completion-only response\n"
    assert "session saved:" in result.stderr


def test_print_mode_drains_failed_agent_lifecycle_before_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    emitted_event_types: list[str] = []

    async def build_runtime() -> WispRuntime:
        runtime = await build_failing_runtime()
        runtime.events.on("*", lambda event: emitted_event_types.append(event.type))
        return runtime

    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_runtime)

    result = CliRunner().invoke(
        app,
        ["-p", "fail", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "failing-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr.count("error: provider failed") == 1
    assert emitted_event_types[-3:] == [
        "error",
        "turn.completed",
        "agent.completed",
    ]


def test_print_mode_loads_trusted_root_settings_from_subdirectory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from wisp.trust import record_trust

    project = tmp_path / "project"
    nested = project / "src"
    sessions = project / "project-sessions"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (project / ".wisp").mkdir()
    (project / ".wisp" / "settings.json").write_text(
        json.dumps({"session_dir": str(sessions)}),
        encoding="utf-8",
    )
    trust_file = tmp_path / "trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(trust_file))
    record_trust(project, True, trust_path=trust_file)
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(
        app,
        ["-p", "hello"],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert len(list(sessions.glob("*.jsonl"))) == 1


def test_prompt_implies_text_mode_when_env_defaults_to_tui(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--session-dir", str(tmp_path)],
        env={
            "WISP_MODE": "tui",
            "WISP_TUI_RENDERER": "fullscreen",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
        },
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "fake response to: hello\n"
    assert "session saved:" in result.stderr


def test_print_mode_continue_appends_to_latest_session(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["-p", "first", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    second = runner.invoke(
        app,
        ["-p", "second", "--continue", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1
    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["message"]["role"] for record in records] == [
        "system",
        "system",
        "user",
        "assistant",
        "system",
        "system",
        "user",
        "assistant",
    ]
    assert [
        record["message"]["content"] for record in records if record["message"]["role"] == "user"
    ] == [
        "first",
        "second",
    ]


def test_print_mode_resume_appends_to_named_session(tmp_path: Path) -> None:
    runner = CliRunner()

    first = runner.invoke(
        app,
        ["-p", "first", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert first.exit_code == 0, first.output
    session_file = next(tmp_path.glob("*.jsonl"))

    second = runner.invoke(
        app,
        ["-p", "second", "--resume", session_file.name, "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert second.exit_code == 0, second.output
    records = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines()]
    assert [
        record["message"]["content"] for record in records if record["message"]["role"] == "user"
    ] == [
        "first",
        "second",
    ]


def test_print_mode_resume_uses_compaction_replay(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write_session() -> None:
        first = await session.append_message(Message(role="user", content="raw first"))
        first_answer = await session.append_message(
            Message(role="assistant", content="raw answer", finish_reason="stop")
        )
        retained = await session.append_message(Message(role="user", content="retained"))
        retained_answer = await session.append_message(
            Message(role="assistant", content="retained answer", finish_reason="stop")
        )
        await session.append_compaction_entry(
            SessionEntry(
                session_id=session.session_id,
                kind="compaction",
                compaction=CompactionRecord(
                    summary="durable summary",
                    replaced_entry_ids=(first.id, first_answer.id),
                    provider="fake",
                ),
            ),
            expected_context_entry_ids=(
                first.id,
                first_answer.id,
                retained.id,
                retained_answer.id,
            ),
        )

    anyio.run(write_session)
    captured_history: tuple[Message, ...] = ()

    async def capture_run(
        self: CodingSession,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
    ) -> AsyncIterator[WispEvent]:
        nonlocal captured_history
        captured_history = tuple(history)
        yield MessageCompleted(turn=1, content="replayed", finish_reason="stop")

    monkeypatch.setattr(CodingSession, "run", capture_run)
    result = CliRunner().invoke(
        app,
        [
            "-p",
            "next",
            "--resume",
            session.path.name,
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "replayed\n"
    assert [message.content for message in captured_history] == [
        f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\ndurable summary",
        "retained",
        "retained answer",
    ]


def test_print_mode_rejects_resume_and_continue_together(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--resume", "missing", "--continue", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "use either --resume or --continue" in result.output


def test_print_mode_reports_missing_resume_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--resume", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Session not found: missing" in result.output


def test_print_mode_renders_denied_tool_events_to_stderr(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        ["-p", "use tool", "--allow-tool", "danger", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "done\n"
    assert '→ tool danger {"path": "file.txt"}' in result.stderr
    assert "? approval required for danger (mutating)" in result.stderr
    assert "! denied danger: Tool danger requires approval before execution" in result.stderr
    assert "✗ tool danger: Tool danger requires approval before execution" in result.stderr
    assert "session saved:" in result.stderr
    assert "changed file.txt" not in result.stderr


def test_print_mode_skips_approval_events_for_preapproved_tools(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        [
            "-p",
            "use tool",
            "--allow-tool",
            "danger",
            "--yes",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "done\n"
    assert '→ tool danger {"path": "file.txt"}' in result.stderr
    assert "approval required" not in result.stderr
    assert "approved danger" not in result.stderr
    assert "✓ tool danger: changed file.txt" in result.stderr
    assert "session saved:" in result.stderr


def test_print_mode_enforces_explicit_tool_iteration_cap(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        [
            "-p",
            "use tool",
            "--allow-tool",
            "danger",
            "--yes",
            "--max-tool-iterations",
            "0",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert "error: Maximum tool iterations exceeded: 0" in result.stderr


def test_print_mode_rejects_negative_tool_iteration_cap(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--max-tool-iterations", "-1", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert "error: --max-tool-iterations must be non-negative" in result.stderr


def test_print_mode_keeps_event_separator_out_of_stdout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(cli_module.rpc, "build_runtime", build_mixed_tool_runtime)

    result = runner.invoke(
        app,
        [
            "-p",
            "use tool",
            "--allow-tool",
            "danger",
            "--yes",
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "mixed-tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "prefixsuffix\n"
    assert "\n→ tool danger" in result.stderr
    assert "✓ tool danger: changed file.txt" in result.stderr


def test_print_mode_context_describes_allowed_read_tools(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--allow-read-tools", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    session_files = list(tmp_path.glob("*.jsonl"))
    assert len(session_files) == 1
    records = [
        json.loads(line) for line in session_files[0].read_text(encoding="utf-8").splitlines()
    ]
    context = records[1]["message"]["content"]
    assert "allowed tools:" in context
    assert "- read:" in context
    assert "- grep:" in context
    assert "- find:" in context
    assert "- ls:" in context
    assert "- write:" not in context
    assert "- edit:" not in context
    assert "- bash:" not in context


def test_print_mode_requires_approval_for_dangerous_tools_without_yes() -> None:
    approval = _print_mode_tool_approval_policy(False)

    assert approval.approves(ReadTool()) is True
    assert approval.approves(WriteTool()) is False
    assert approval.approves(EditTool()) is False
    assert approval.approves(BashTool()) is False


def test_print_mode_yes_approves_dangerous_tools() -> None:
    approval = _print_mode_tool_approval_policy(True)

    assert approval.approves(WriteTool()) is True
    assert approval.approves(EditTool()) is True
    assert approval.approves(BashTool()) is True


def test_print_mode_exposes_no_tools_by_default() -> None:
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
    ):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry)

    assert filtered.names() == ()


def test_print_mode_can_expose_sandboxed_read_tools() -> None:
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
    ):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry, allow_read_tools=True)

    assert filtered.names() == ("read", "grep", "find", "ls")


def test_print_mode_can_expose_explicit_tools() -> None:
    registry = ToolRegistry()
    for tool in (ReadTool(), WriteTool(), BashTool()):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry, allowed_tools=("bash", "write"))

    assert filtered.names() == ("write", "bash")


def test_print_mode_all_tools_exposes_the_full_registry() -> None:
    # all_tools admits every tier (read/mutating/command) — the interactive TUI's
    # default, where availability is broad and the approval policy gates unsafe use.
    registry = ToolRegistry()
    for tool in (ReadTool(), WriteTool(), EditTool(), BashTool(), GrepTool()):
        registry.register(tool)

    filtered = _print_mode_tool_registry(registry, all_tools=True)

    assert filtered.names() == ("read", "write", "edit", "bash", "grep")


def test_print_mode_reports_unknown_allowed_tool(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--allow-tool", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Unknown tool: missing" in result.output


def test_print_mode_reports_unknown_provider(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--provider", "missing", "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert "Unknown provider: missing" in result.output
