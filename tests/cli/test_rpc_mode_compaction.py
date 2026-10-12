# ruff: noqa: F403,F405

from __future__ import annotations

import pytest

from tests.cli.rpc_mode_support import (
    VALID_COMPACTION_SUMMARY,
    BlockingOperationProvider,
    create_two_turn_session,
    is_compaction_request,
    runtime_with_provider,
)
from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization
from wisp.agent.messages import CompactionRecord
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderUsage,
)
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
)
from wisp.sessions.replay import HISTORICAL_CONTEXT_SUMMARY_LABEL


def _create_compacted_session(tmp_path: Path) -> JsonlSession:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        first = await session.append_message(Message(role="user", content="raw first"))
        first_answer = await session.append_message(
            Message(role="assistant", content="raw first answer", finish_reason="stop")
        )
        retained = await session.append_message(Message(role="user", content="retained second"))
        retained_answer = await session.append_message(
            Message(role="assistant", content="retained answer", finish_reason="stop")
        )
        await session.append_compaction_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                kind="compaction",
                compaction=CompactionRecord(
                    summary="durable summary",
                    replaced_entry_ids=(first.id, first_answer.id),
                    provider="replay-aware-test",
                ),
            ),
            expected_context_entry_ids=(
                first.id,
                first_answer.id,
                retained.id,
                retained_answer.id,
            ),
        )

    anyio.run(write)
    return session


class ReplayAwareProvider:
    name = "replay-aware-test"
    default_model: str | None = "replay-aware-test"

    def __init__(self) -> None:
        self.calls: list[tuple[Message, ...]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        call = tuple(messages)
        self.calls.append(call)
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if is_compaction_request(messages):
            yield ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY)
            return
        prompt = _last_user_prompt(messages)
        if prompt in {"third", "after repeat", "resume check"}:
            contents = [message.content for message in messages]
            replayed = any(
                content.startswith(HISTORICAL_CONTEXT_SUMMARY_LABEL) for content in contents
            ) and ("second" in contents or "retained second" in contents)
            raw_resurrected = "first" in contents or "raw first" in contents
            content = (
                "saw replay context" if replayed and not raw_resurrected else "bad raw history"
            )
        else:
            content = f"answer {prompt}"
        yield ProviderResponseCompleted(content=content)


class AutoCompactionProvider(ReplayAwareProvider):
    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del tools, tool_results, previous_response_id, effort
        self.calls.append(tuple(messages))
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if is_compaction_request(messages):
            yield ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY)
            return
        yield ProviderResponseCompleted(
            content=f"answer {_last_user_prompt(messages)}",
            usage=ProviderUsage(input_tokens=81, output_tokens=11, total_tokens=92),
        )


class OverflowRecoveryProvider(ReplayAwareProvider):
    def __init__(self) -> None:
        super().__init__()
        self._overflowed = False

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del tools, tool_results, previous_response_id, effort
        self.calls.append(tuple(messages))
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if is_compaction_request(messages):
            yield ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY)
            return
        if not self._overflowed:
            self._overflowed = True
            yield ProviderResponseFailed(message="maximum context length exceeded")
            return
        yield ProviderResponseCompleted(content="answer after recovery")


@pytest.mark.parametrize(
    "command",
    [
        {"id": "compact-1", "type": "compact"},
        {"id": "compact-1", "type": "compact", "instructions": None},
        {"id": "compact-1", "type": "compact", "instructions": ""},
        {"id": "compact-1", "type": "compact", "instructions": "   "},
        {"id": "compact-1", "type": "compact", "instructions": "Keep paths"},
    ],
)
def test_rpc_compact_accepts_optional_string_instructions_but_requires_session(
    tmp_path: Path,
    command: dict[str, object],
) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=f"{json.dumps(command)}\n",
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == ("RPC compact command requires an existing persisted session")
    assert records[2]["ok"] is False
    assert not list(tmp_path.glob("*.jsonl"))


def test_rpc_compact_rejects_non_string_instructions_and_remains_usable(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"compact-1","type":"compact","instructions":3}\n'
            '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    prompt = next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "prompt-1"
    )
    assert [record["message"] for record in records if record["type"] == "error"] == [
        "RPC command does not match the negotiated schema"
    ]
    assert prompt["ok"] is True


def test_rpc_prompt_compact_prompt_replays_summary_and_retained_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = ReplayAwareProvider()

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"first"}\n'
            '{"id":"prompt-2","type":"prompt","prompt":"second"}\n'
            '{"id":"compact-1","type":"compact","instructions":"  Keep paths  "}\n'
            '{"id":"prompt-3","type":"prompt","prompt":"third"}\n'
        ),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    compact_start = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.started" and record["command_id"] == "compact-1"
    )
    compact_finish = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.finished" and record["command_id"] == "compact-1"
    )
    assert [record["type"] for record in records[compact_start : compact_finish + 1]] == [
        "rpc.command.started",
        "compaction.started",
        "session.saved",
        "compaction.completed",
        "rpc.command.finished",
    ]
    assert any(record.get("content") == "saw replay context" for record in records)
    assert all(
        record["ok"] is True for record in records if record["type"] == "rpc.command.finished"
    )

    session = JsonlSessionStore(tmp_path).latest()
    raw_contents = [message.content for message in session.read_messages()]
    context_contents = [message.content for message in session.read_context_messages()]
    assert "first" in raw_contents
    assert "answer first" in raw_contents
    assert "first" not in context_contents
    assert "answer first" not in context_contents
    assert context_contents[0] == (
        f"{HISTORICAL_CONTEXT_SUMMARY_LABEL}\n\n{VALID_COMPACTION_SUMMARY}"
    )
    compaction = next(
        entry.compaction
        for entry in session.read_entries()
        if isinstance(entry, CompactionSessionEntry)
    )
    assert compaction.instructions == "Keep paths"


def test_rpc_prompt_contains_automatic_threshold_compaction(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def seed() -> None:
        await session.append_message(Message(role="user", content="first"))
        await session.append_message(
            Message(role="assistant", content="answer first", finish_reason="stop")
        )

    anyio.run(seed)
    provider = AutoCompactionProvider()

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider, context_window=100)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        [
            "--mode",
            "rpc",
            "--resume",
            session.path.name,
            "--session-dir",
            str(tmp_path),
        ],
        input='{"id":"prompt-1","type":"prompt","prompt":"second"}\n',
        env={
            "WISP_PROVIDER": provider.name,
            "WISP_MODEL": "",
            "WISP_CONTEXT_RESERVE_TOKENS": "20",
            "WISP_TRUST": "1",
        },
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.completed",
        "context.pressure",
        "turn.completed",
        "compaction.started",
        "session.saved",
        "compaction.completed",
        "agent.completed",
        "rpc.command.finished",
    ]
    started = next(record for record in records if record["type"] == "compaction.started")
    completed = next(record for record in records if record["type"] == "compaction.completed")
    assert started["reason"] == "threshold"
    assert completed["reason"] == "threshold"
    assert records[-1]["command_type"] == "prompt"
    assert records[-1]["ok"] is True
    assert not any(
        record.get("command_type") == "compact"
        for record in records
        if record["type"].startswith("rpc.command.")
    )


def test_rpc_prompt_recovers_one_overflow_inside_the_prompt_envelope(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = create_two_turn_session(tmp_path)
    provider = OverflowRecoveryProvider()

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider, context_window=100)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        [
            "--mode",
            "rpc",
            "--resume",
            session.path.name,
            "--session-dir",
            str(tmp_path),
        ],
        input='{"id":"prompt-1","type":"prompt","prompt":"third"}\n',
        env={
            "WISP_PROVIDER": provider.name,
            "WISP_MODEL": "",
            "WISP_CONTEXT_RESERVE_TOKENS": "20",
            "WISP_TRUST": "1",
        },
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.completed",
        "context.overflow",
        "compaction.started",
        "session.saved",
        "compaction.completed",
        "turn.completed",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
        "rpc.command.finished",
    ]
    overflow_compaction = next(
        record
        for record in records
        if record["type"] == "compaction.completed" and record["reason"] == "overflow"
    )
    assert overflow_compaction["will_retry"] is True
    assert records[-1]["ok"] is True
    assert not any(record["type"] == "error" for record in records)
    assert sum(record["type"] == "rpc.command.started" for record in records) == 1
    assert [record["turn"] for record in records if record["type"] == "turn.started"] == [1, 2]
    entries = session.read_entries()
    assert (
        sum(
            isinstance(entry, MessageSessionEntry)
            and entry.message.role == "user"
            and entry.message.content == "third"
            for entry in entries
        )
        == 1
    )
    assert (
        next(
            entry.compaction for entry in entries if isinstance(entry, CompactionSessionEntry)
        ).schema_version
        == 4
    )


def test_rpc_resume_initial_history_uses_compaction_replay(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = _create_compacted_session(tmp_path)
    provider = ReplayAwareProvider()

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        [
            "--mode",
            "rpc",
            "--resume",
            session.path.name,
            "--session-dir",
            str(tmp_path),
        ],
        input='{"id":"prompt-1","type":"prompt","prompt":"resume check"}\n',
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert any(
        record.get("content") == "saw replay context" for record in _jsonl_records(result.stdout)
    )


def test_rpc_queues_compact_behind_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = create_two_turn_session(tmp_path)
    provider = BlockingOperationProvider(block_prompt=True)

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--resume", session.path.name, "--session-dir", str(tmp_path)],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"compact-1","type":"compact"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"prompt-1"}\n'
        ),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("prompt-1", False),
        ("compact-1", True),
    ]
    assert any(entry.kind == "compaction" for entry in session.read_entries())


def test_rpc_cancels_blocked_compact_then_runs_queued_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = create_two_turn_session(tmp_path)
    provider = BlockingOperationProvider(block_compaction=True)

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--resume", session.path.name, "--session-dir", str(tmp_path)],
        input=(
            '{"id":"compact-1","type":"compact"}\n'
            '{"id":"prompt-1","type":"prompt","prompt":"after cancel"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"compact-1"}\n'
        ),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("compact-1", False),
        ("prompt-1", True),
    ]
    compact_finished = finished[1]
    assert compact_finished["error"] == "RPC command cancelled: compact-1"
    cancelled = next(record for record in records if record["type"] == "compaction.completed")
    assert cancelled["outcome"] == "cancelled"
    assert not any(entry.kind == "compaction" for entry in session.read_entries())
    assert any(record.get("content") == "done after cancel" for record in records)


def test_rpc_repeat_compaction_failure_leaves_process_usable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = create_two_turn_session(tmp_path)
    provider = ReplayAwareProvider()

    async def build_runtime(**_kwargs: object) -> WispRuntime:
        return await runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--resume", session.path.name, "--session-dir", str(tmp_path)],
        input=(
            '{"id":"compact-1","type":"compact"}\n'
            '{"id":"compact-2","type":"compact"}\n'
            '{"id":"prompt-1","type":"prompt","prompt":"after repeat"}\n'
        ),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = {
        record["command_id"]: record
        for record in records
        if record["type"] == "rpc.command.finished"
    }
    assert finished["compact-1"]["ok"] is True
    assert finished["compact-2"]["ok"] is False
    assert "No new complete turn" in str(finished["compact-2"]["error"])
    assert finished["prompt-1"]["ok"] is True
    assert any(record.get("content") == "saw replay context" for record in records)
    assert sum(entry.kind == "compaction" for entry in session.read_entries()) == 1


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
