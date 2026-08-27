# ruff: noqa: F403,F405

from __future__ import annotations

from collections.abc import Mapping

import pytest

import wisp.coding.tool_execution as tool_execution
import wisp.rpc.execution as rpc_execution_module
from tests.cli_support import *
from wisp import __version__
from wisp.agent.messages import CompactionRecord
from wisp.agent.transcript import INTERRUPTED_TOOL_RESULT_TEXT
from wisp.events import EVENT_SCHEMA_VERSION, ContextBudget, ContextEstimate, ToolCallSnapshot
from wisp.providers.base import Provider
from wisp.providers.catalog import (
    ModelCatalog,
    ModelCatalogProviderEntry,
    ModelRegistry,
    effective_catalog,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderUsage,
)
from wisp.rpc.coordinator import _RpcPromptReady
from wisp.rpc.execution import (
    handle_rpc_control_command,
    rpc_has_durable_completion,
    run_rpc_prompt_command,
)
from wisp.rpc.host import RpcToolApprovalPolicy, RpcTrustGate
from wisp.rpc.protocol import LIVE_RPC_PROTOCOL_VERSION, RpcHandshakeRequest
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    MessageSessionEntry,
)
from wisp.sessions.replay import HISTORICAL_CONTEXT_SUMMARY_LABEL

_BaseCliRunner = RawCliRunner


def _base_jsonl_records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines()]


_RPC_TEST_HANDSHAKE = (
    RpcHandshakeRequest(
        frontend_name="wisp-python-tests",
        frontend_version=__version__,
        min_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        max_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        min_event_schema_version=EVENT_SCHEMA_VERSION,
        max_event_schema_version=EVENT_SCHEMA_VERSION,
        supported_capabilities=(),
        required_capabilities=(),
    ).model_dump_json()
    + "\n"
)


def test_rpc_control_stream_buffers_at_most_one_parsed_event() -> None:
    assert cli_module.rpc._RPC_CONTROL_STREAM_BUFFER_SIZE == 1


def test_rpc_mode_requires_handshake_before_ordinary_commands(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fail_build_runtime(_config: object) -> object:
        raise AssertionError("runtime must not start before negotiation")

    monkeypatch.setattr(cli_module.rpc, "build_runtime_for_config", fail_build_runtime)
    result = _BaseCliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"shutdown-1","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.rejected"]
    assert records[0]["code"] == "invalid_handshake"


def test_rpc_mode_accepts_handshake_before_any_runtime_event(tmp_path: Path) -> None:
    result = _BaseCliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _base_jsonl_records(result.stdout)
    assert records[0]["type"] == "rpc.handshake.accepted"
    assert [record["type"] for record in records[1:]] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]


def test_rpc_mode_selected_from_environment_skips_plaintext_help(tmp_path: Path) -> None:
    result = _BaseCliRunner().invoke(
        app,
        [],
        input=_RPC_TEST_HANDSHAKE + '{"id":"bye","type":"shutdown"}\n',
        env={
            "WISP_MODE": "rpc",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_SESSION_DIR": str(tmp_path),
        },
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[-1]["command_id"] == "bye"
    assert records[-1]["ok"] is True


@pytest.mark.process
def test_rpc_mode_preserves_command_prefetched_with_handshake(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        process = await anyio.open_process(
            [
                sys.executable,
                "-m",
                "wisp",
                "--mode",
                "rpc",
                "--session-dir",
                str(tmp_path),
            ],
            env={**os.environ, "WISP_PROVIDER": "fake", "WISP_MODEL": ""},
        )
        assert process.stdin is not None
        assert process.stdout is not None
        await process.stdin.send(
            (_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n').encode()
        )
        await process.stdin.aclose()
        output = bytearray()
        with anyio.fail_after(10):
            while True:
                try:
                    output.extend(await process.stdout.receive())
                except anyio.EndOfStream:
                    break
            await process.wait()
        assert process.returncode == 0
        return _base_jsonl_records(output.decode())

    records = anyio.run(scenario)
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.command.started",
        "rpc.command.finished",
    ]


@pytest.mark.process
def test_rpc_mode_writes_utf8_frames_with_non_utf8_stdio_encoding(tmp_path: Path) -> None:
    async def scenario() -> bytes:
        process = await anyio.open_process(
            [
                sys.executable,
                "-m",
                "wisp",
                "--mode",
                "rpc",
                "--session-dir",
                str(tmp_path),
            ],
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-16",
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
            },
        )
        assert process.stdin is not None
        assert process.stdout is not None
        await process.stdin.send(
            (_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n').encode()
        )
        await process.stdin.aclose()
        output = bytearray()
        with anyio.fail_after(10):
            while True:
                try:
                    output.extend(await process.stdout.receive())
                except anyio.EndOfStream:
                    break
            await process.wait()
        assert process.returncode == 0
        return bytes(output)

    output = anyio.run(scenario)
    records = _base_jsonl_records(output.decode("utf-8"))
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.command.started",
        "rpc.command.finished",
    ]


VALID_COMPACTION_SUMMARY = """## Goal
Preserve the active coding objective.
## Constraints & Preferences
Keep changes focused.
## Progress
### Done
Reviewed the prior turn.
### In Progress
Continue implementation.
### Blocked
None.
## Already Investigated
Reviewed the prior turn's transcript.
## Key Decisions
Use append-only replay.
## Next Steps
Run the tests.
## Critical Context
The session audit remains intact."""


async def _runtime_with_provider(
    provider: Provider, *, context_window: int | None = None
) -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(provider)
    models = ModelRegistry(effective_catalog(home_dir=Path("/nonexistent-test-home")))
    if context_window is not None:
        assert provider.default_model is not None
        models = ModelRegistry(
            ModelCatalog(
                schema_version=1,
                providers=(
                    ModelCatalogProviderEntry(
                        name=provider.name,
                        display_name=provider.name,
                        default_model=provider.default_model,
                        docs_url="https://example.com",
                        models=(provider.default_model,),
                        context_windows={provider.default_model: context_window},
                    ),
                ),
            )
        )
    return WispRuntime(
        providers=providers,
        tools=tools,
        events=events,
        api=api,
        models=models,
    )


def _create_two_turn_session(tmp_path: Path) -> JsonlSession:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        for prompt, answer in (("first", "answer one"), ("second", "answer two")):
            await session.append_message(Message(role="user", content=prompt))
            await session.append_message(
                Message(role="assistant", content=answer, finish_reason="stop")
            )

    anyio.run(write)
    return session


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


def test_cancelled_prompt_leaf_restore_preserves_concurrent_compaction(tmp_path: Path) -> None:
    session = _create_two_turn_session(tmp_path)
    initial_entries = session.read_entries()
    entry_start = len(initial_entries)
    initial_leaf_id = session.read_active_leaf_id()

    async def append_prompt_and_compaction() -> None:
        await session.append_message(
            Message(role="system", content="current system prompt"),
            operation_id="prompt-1",
        )
        pending_user = await session.append_message(
            Message(role="user", content="cancel me"),
            operation_id="prompt-1",
        )
        context = session.read_context()
        await session.append_compaction_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                kind="compaction",
                compaction=CompactionRecord(
                    summary="durable summary",
                    replaced_entry_ids=(initial_entries[0].id, initial_entries[1].id),
                    provider="test",
                ),
            ),
            expected_context_entry_ids=context.context_entry_ids,
        )
        assert pending_user.id in session.read_context().context_entry_ids
        restored = await session.restore_active_leaf_for_operation(
            entry_start,
            initial_leaf_id,
            operation_id="prompt-1",
        )
        assert restored is False

    anyio.run(append_prompt_and_compaction)

    assert rpc_has_durable_completion(session, entry_start, "prompt-1") is False
    assert any(entry.kind == "compaction" for entry in session.read_entries()[entry_start:])


def test_rpc_cancellation_restores_leaf_before_unanswered_overflow_compaction(
    tmp_path: Path,
) -> None:
    session = _create_two_turn_session(tmp_path)
    entry_start = len(session.read_entries())
    initial_context_ids = session.read_context().context_entry_ids
    initial_leaf_id = session.read_active_leaf_id()

    async def append_overflow_compaction() -> None:
        context = session.read_context()
        await session.append_compaction_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                kind="compaction",
                operation_id="prompt-1",
                compaction=CompactionRecord(
                    schema_version=3,
                    summary="durable overflow summary",
                    replaced_entry_ids=context.context_entry_ids[:2],
                    provider="test",
                    reason="overflow",
                    trigger_budget=ContextBudget(
                        estimate=ContextEstimate(
                            system_tokens=1,
                            message_tokens=2,
                            tool_schema_tokens=0,
                            total_tokens=3,
                        ),
                        context_window=100,
                        reserve_tokens=20,
                        remaining_tokens=77,
                        estimated_percent=3,
                        over_budget=False,
                    ),
                ),
            ),
            expected_context_entry_ids=context.context_entry_ids,
        )

    anyio.run(append_overflow_compaction)

    assert rpc_has_durable_completion(session, entry_start, "prompt-1") is False

    async def restore() -> bool:
        return await session.restore_active_leaf_for_operation(
            entry_start,
            initial_leaf_id,
            operation_id="prompt-1",
        )

    assert anyio.run(restore) is True
    suffix = session.read_entries()[entry_start:]
    assert any(entry.kind == "compaction" for entry in suffix)
    assert isinstance(suffix[-1], ActiveLeafSessionEntry)
    assert session.read_context().context_entry_ids == initial_context_ids


def test_operation_leaf_restore_fails_cleanly_if_starting_leaf_was_removed(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def mutate() -> bool:
        await session.append_message(Message(role="user", content="first"))
        starting_leaf = await session.append_message(Message(role="assistant", content="second"))
        starting_count = len(session.read_entries())
        await session.truncate_entries(1)
        await session.append_message(
            Message(role="user", content="replacement"),
            operation_id="prompt-1",
        )
        await session.append_message(
            Message(role="assistant", content="replacement answer"),
            operation_id="prompt-1",
        )
        return await session.restore_active_leaf_for_operation(
            starting_count,
            starting_leaf.id,
            operation_id="prompt-1",
        )

    assert anyio.run(mutate) is False


def _is_compaction_request(messages: Sequence[Message]) -> bool:
    return any(
        message.role == "system"
        and "Create a concise, durable coding-session checkpoint" in message.content
        for message in messages
    )


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
        if _is_compaction_request(messages):
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


class BlockingOperationProvider:
    name = "blocking-operation-test"
    default_model: str | None = "blocking-operation-test"

    def __init__(self, *, block_compaction: bool = False, block_prompt: bool = False) -> None:
        self.block_compaction = block_compaction
        self.block_prompt = block_prompt

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
        compacting = _is_compaction_request(messages)
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if (compacting and self.block_compaction) or (not compacting and self.block_prompt):
            await anyio.sleep_forever()
        yield ProviderResponseCompleted(
            content=VALID_COMPACTION_SUMMARY
            if compacting
            else f"done {_last_user_prompt(messages)}"
        )


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
        if _is_compaction_request(messages):
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
        if _is_compaction_request(messages):
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


def test_rpc_mode_accepts_zero_tool_iteration_cap(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--max-tool-iterations", "0", "--session-dir", str(tmp_path)],
        input='{"id":"shutdown-1","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[1]["ok"] is True


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

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

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

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider, context_window=100)

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
    session = _create_two_turn_session(tmp_path)
    provider = OverflowRecoveryProvider()

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider, context_window=100)

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

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

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
    session = _create_two_turn_session(tmp_path)
    provider = BlockingOperationProvider(block_prompt=True)

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

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
    session = _create_two_turn_session(tmp_path)
    provider = BlockingOperationProvider(block_compaction=True)

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

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


def test_rpc_queue_commands_wait_for_prompt_readiness_then_bypass(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = BlockingOperationProvider()

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

    original_resolve = RpcTrustGate.resolve

    async def delayed_resolve(self: RpcTrustGate) -> bool:
        await anyio.sleep(0.05)
        return await original_resolve(self)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    monkeypatch.setattr(RpcTrustGate, "resolve", delayed_resolve)
    commands = [
        {"id": "prompt-1", "type": "prompt", "prompt": "block"},
        {"id": "rpc-state-1", "type": "get_state"},
        {"id": "messages-1", "type": "get_messages"},
        {"id": "steer-1", "type": "steer", "content": "one"},
        {"id": "steer-2", "type": "steer", "content": "two"},
        {"id": "follow-1", "type": "follow_up", "content": "later"},
        {
            "id": "mode-1",
            "type": "set_queue_mode",
            "kind": "follow_up",
            "mode": "all",
        },
        {"id": "state-1", "type": "get_queue_state"},
        {"id": "pop-1", "type": "pop_queue", "kind": "steering"},
        {"id": "clear-1", "type": "clear_queue", "kind": "follow_up"},
    ]
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input="".join(f"{json.dumps(command)}\n" for command in commands),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)

    def command_records(command_id: str) -> list[dict[str, object]]:
        start = next(
            index
            for index, record in enumerate(records)
            if record["type"] == "rpc.command.started" and record["command_id"] == command_id
        )
        finish = next(
            index
            for index, record in enumerate(records[start:], start=start)
            if record["type"] == "rpc.command.finished" and record["command_id"] == command_id
        )
        return records[start : finish + 1]

    state_records = command_records("state-1")
    assert [record["type"] for record in state_records] == [
        "rpc.command.started",
        "queue.updated",
        "rpc.command.finished",
    ]
    assert state_records[1]["steering"] == ["one", "two"]
    assert state_records[1]["follow_up"] == ["later"]
    assert state_records[1]["follow_up_mode"] == "all"

    rpc_state_records = command_records("rpc-state-1")
    assert [record["type"] for record in rpc_state_records] == [
        "rpc.command.started",
        "rpc.state",
        "rpc.command.finished",
    ]
    assert rpc_state_records[1]["state"]["active_command_id"] == "prompt-1"
    assert rpc_state_records[1]["state"]["active_command_type"] == "prompt"
    assert rpc_state_records[1]["state"]["cancel_requested"] is False
    assert rpc_state_records[1]["state"]["session_id"]
    assert rpc_state_records[1]["state"]["session_path"]

    pop_records = command_records("pop-1")
    assert [record["type"] for record in pop_records] == [
        "rpc.command.started",
        "queue.items.removed",
        "queue.updated",
        "rpc.command.finished",
    ]
    assert pop_records[1]["operation"] == "pop"
    assert pop_records[1]["steering"] == ["two"]
    assert pop_records[2]["steering"] == ["one"]

    clear_records = command_records("clear-1")
    assert [record["type"] for record in clear_records] == [
        "rpc.command.started",
        "queue.items.removed",
        "queue.updated",
        "rpc.command.finished",
    ]
    assert clear_records[1]["operation"] == "clear"
    assert clear_records[1]["follow_up"] == ["later"]
    assert clear_records[2]["follow_up"] == []

    prompt_finish_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.finished" and record["command_id"] == "prompt-1"
    )
    for command_id in (
        "rpc-state-1",
        "steer-1",
        "steer-2",
        "follow-1",
        "mode-1",
        "state-1",
        "pop-1",
        "clear-1",
    ):
        queue_finish_index = next(
            index
            for index, record in enumerate(records)
            if record["type"] == "rpc.command.finished" and record["command_id"] == command_id
        )
        assert queue_finish_index < prompt_finish_index
        assert records[queue_finish_index]["ok"] is True
    messages_finish_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.finished" and record["command_id"] == "messages-1"
    )
    assert messages_finish_index > prompt_finish_index
    messages_report = next(record for record in records if record["type"] == "rpc.messages")
    assert messages_report["command_id"] == "messages-1"
    assert messages_report["session_id"]


def test_rpc_rejects_container_queue_fields_and_remains_usable(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"kind","type":"pop_queue","kind":[]}\n'
            '{"id":"mode","type":"set_queue_mode","kind":"steering","mode":{}}\n'
            '{"id":"state","type":"get_queue_state"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("state", True),
    ]
    assert [record["message"] for record in records if record["type"] == "error"] == [
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
    ]


def test_rpc_pending_queue_is_bounded_while_prompt_is_blocked(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = BlockingOperationProvider(block_prompt=True)

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    commands = [{"id": "prompt", "type": "prompt", "prompt": "block"}]
    commands.extend(
        {"id": f"steer-{index}", "type": "steer", "content": str(index)} for index in range(101)
    )
    commands.extend(
        [
            {"id": "state", "type": "get_queue_state"},
            {"id": "cancel", "type": "cancel", "target_id": "prompt"},
        ]
    )
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input="".join(f"{json.dumps(command)}\n" for command in commands),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = {
        record["command_id"]: record
        for record in records
        if record["type"] == "rpc.command.finished"
    }
    assert finished["steer-99"]["ok"] is True
    assert finished["steer-100"]["ok"] is False
    assert finished["steer-100"]["error"] == ("Agent queue is full (maximum 100 pending messages)")
    assert finished["state"]["ok"] is True
    assert finished["cancel"]["ok"] is True
    state_start = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.started" and record["command_id"] == "state"
    )
    state = next(record for record in records[state_start:] if record["type"] == "queue.updated")
    assert len(state["steering"]) == 100
    assert state["follow_up"] == []


def test_rpc_mode_rejects_duplicate_outstanding_command_id(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = BlockingOperationProvider(block_prompt=True)

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_runtime)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"same","type":"prompt","prompt":"block"}\n'
            '{"id":"same","type":"prompt","prompt":"duplicate"}\n'
            '{"id":"cancel","type":"cancel","target_id":"same"}\n'
        ),
        env={"WISP_PROVIDER": provider.name, "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    terminals = [record for record in records if record["type"] == "rpc.command.finished"]
    original = [record for record in terminals if record["command_id"] == "same"]
    duplicate = [
        record
        for record in terminals
        if record.get("error") == "RPC command id is already outstanding: same"
    ]
    assert len(original) == 1
    assert original[0]["ok"] is False
    assert original[0]["error"] == "RPC command cancelled: same"
    assert len(duplicate) == 1
    assert duplicate[0]["ok"] is False
    assert duplicate[0]["command_id"] not in {"same", "cancel"}
    assert any(record["command_id"] == "cancel" and record["ok"] is True for record in terminals)


def test_rpc_repeat_compaction_failure_leaves_process_usable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    session = _create_two_turn_session(tmp_path)
    provider = ReplayAwareProvider()

    async def build_runtime() -> WispRuntime:
        return await _runtime_with_provider(provider)

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


def test_rpc_mode_runs_prompt_commands_with_explicit_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "agent.started",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.delta",
        "message.completed",
        "turn.completed",
        "session.saved",
        "agent.completed",
        "rpc.command.finished",
    ]
    assert all(record["schema_version"] == 34 for record in records)
    assert records[0]["type"] == "rpc.command.started"
    assert records[0]["command_id"] == "cmd-1"
    assert records[0]["command_type"] == "prompt"
    assert records[-5]["content"] == "fake response to: hello"
    assert records[-1]["command_id"] == "cmd-1"
    assert records[-1]["command_type"] == "prompt"
    assert records[-1]["ok"] is True
    assert records[-1]["error"] is None


def test_rpc_mode_init_refuses_existing_project_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    guidance = project / "AGENTS.md"
    guidance.write_text("Keep me.\n", encoding="utf-8")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path / "sessions")],
        input='{"id":"init-1","type":"init"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == f"Project guidance already exists: {guidance}"
    assert records[2]["command_type"] == "init"
    assert records[2]["ok"] is False
    assert guidance.read_text(encoding="utf-8") == "Keep me.\n"


def test_rpc_mode_reports_commands_before_prompt(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"commands-1","type":"get_commands"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.commands",
        "rpc.command.finished",
    ]
    assert all(record["schema_version"] == 34 for record in records)
    report = records[1]
    assert report["command_id"] == "commands-1"
    assert [command["name"] for command in report["commands"]] == [
        "help",
        "init",
        "compact",
        "context",
        "history",
        "update",
        "skills",
        "mcp",
        "plan",
        "build",
        "model",
        "new",
        "resume",
        "provider",
        "auth",
        "connect",
        "disconnect",
        "quit",
    ]
    compact = report["commands"][2]
    assert compact["slash_command"] == "/compact"
    assert compact["category"] == "session"
    assert compact["arguments"] == [
        {
            "name": "instructions",
            "description": "Optional compaction guidance",
            "required": False,
        }
    ]
    assert records[-1]["command_id"] == "commands-1"
    assert records[-1]["command_type"] == "get_commands"


def test_rpc_mode_reports_active_skill_catalog(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"skills-1","type":"get_skills"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.skills",
        "rpc.command.finished",
    ]
    report = records[1]
    assert report["schema_version"] == 34
    assert report["command_id"] == "skills-1"
    assert report["catalog"] == {
        "entries": [
            {
                "name": "github-pr-delivery",
                "description": (
                    "Deliver focused changes through a GitHub pull request with verified commits, "
                    "current-head CI, thread-aware review, and explicit authorization boundaries."
                ),
                "source": "package:wisp",
            },
            {
                "name": "wisp-development",
                "description": (
                    "Develop Wisp and its current extension surfaces while preserving "
                    "architecture and safety boundaries."
                ),
                "source": "package:wisp",
            },
        ],
        "diagnostics": [],
        "project_trusted": False,
    }
    assert records[-1]["command_type"] == "get_skills"
    assert records[-1]["ok"] is True


def test_rpc_mode_reports_mcp_status(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"mcp-1","type":"get_mcp_status"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.mcp",
        "rpc.command.finished",
    ]
    report = records[1]
    assert report["schema_version"] == 34
    assert report["command_id"] == "mcp-1"
    assert report["status"] == {"servers": []}
    assert records[-1]["command_type"] == "get_mcp_status"
    assert records[-1]["ok"] is True


def test_rpc_mode_reports_stats_after_queued_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
            '{"id":"stats-1","type":"get_session_stats"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    stats = next(record for record in records if record["type"] == "session.stats")
    assert stats["command_id"] == "stats-1"
    assert stats["stats"]["session_id"]
    assert stats["stats"]["entry_count"] == 5
    assert stats["stats"]["active_message_count"] == 2
    assert stats["stats"]["context"]["estimate"]["total_tokens"] > 0
    finished = [
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "stats-1"
    ]
    assert finished == [
        {
            "type": "rpc.command.finished",
            "schema_version": 34,
            "timestamp": finished[0]["timestamp"],
            "command_id": "stats-1",
            "command_type": "get_session_stats",
            "ok": True,
            "error": None,
        }
    ]


def test_rpc_mode_reports_messages_after_queued_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
            '{"id":"messages-1","type":"get_messages","limit":5}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["command_id"] == "messages-1"
    assert report["schema_version"] == 34
    assert report["session_id"]
    assert report["session_path"]
    assert report["active_leaf_id"]
    assert report["truncated"] is False
    assert report["next_before_entry_id"] is None
    turn_messages = report["messages"][-2:]
    assert [message["role"] for message in turn_messages] == ["user", "assistant"]
    assert [message["content"] for message in turn_messages] == [
        "hello",
        "fake response to: hello",
    ]
    assert turn_messages[1]["parent_id"] == turn_messages[0]["entry_id"]
    assert turn_messages[1]["entry_id"] == report["active_leaf_id"]
    assert all(message["content_truncated"] is False for message in turn_messages)
    finished = [
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "messages-1"
    ]
    assert finished == [
        {
            "type": "rpc.command.finished",
            "schema_version": 34,
            "timestamp": finished[0]["timestamp"],
            "command_id": "messages-1",
            "command_type": "get_messages",
            "ok": True,
            "error": None,
        }
    ]


def test_rpc_mode_reports_sessions_catalog(tmp_path: Path) -> None:
    session = _create_two_turn_session(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"sessions-1","type":"get_sessions","limit":10}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    report = next(record for record in records if record["type"] == "rpc.sessions")
    assert report["command_id"] == "sessions-1"
    assert report["schema_version"] == 34
    assert report["selected_session_id"] is None
    assert report["selected_session_path"] is None
    assert len(report["sessions"]) == 1
    summary = report["sessions"][0]
    assert summary["session_id"] == session.session_id
    assert summary["session_path"] == str(session.path)
    assert summary["entry_count"] == 4
    assert summary["active_leaf_id"] == session.read_entries()[-1].id


def test_rpc_mode_select_session_then_reads_messages(tmp_path: Path) -> None:
    session = _create_two_turn_session(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    selected = next(record for record in records if record["type"] == "rpc.session.selected")
    assert selected["schema_version"] == 34
    assert selected["command_id"] == "select-1"
    assert selected["session_id"] == session.session_id
    assert selected["session_path"] == str(session.path)
    assert selected["entry_count"] == 4

    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["schema_version"] == 34
    assert report["session_id"] == session.session_id
    assert [message["content"] for message in report["messages"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]
    finished = [
        (record["command_id"], record["command_type"], record["ok"])
        for record in records
        if record["type"] == "rpc.command.finished"
    ]
    assert finished == [
        ("select-1", "select_session", True),
        ("messages-1", "get_messages", True),
    ]


def test_rpc_mode_sets_selected_and_explicit_session_names(tmp_path: Path) -> None:
    selected_session = _create_two_turn_session(tmp_path)
    other_session = _create_two_turn_session(tmp_path)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": selected_session.session_id,
                }
            )
            + "\n"
            '{"id":"name-1","type":"set_session_name","name":"  Roadmap\\r\\nCleanup  "}\n'
            '{"id":"sessions-1","type":"get_sessions","limit":10}\n'
            + json.dumps(
                {
                    "id": "clear-other",
                    "type": "set_session_name",
                    "session_id": other_session.session_id,
                    "name": "",
                }
            )
            + "\n"
            '{"id":"sessions-2","type":"get_sessions","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    name_events = [record for record in records if record["type"] == "rpc.session.name_changed"]
    assert [event["schema_version"] for event in name_events] == [34, 34]
    assert name_events[0]["session_id"] == selected_session.session_id
    assert name_events[0]["previous_name"] is None
    assert name_events[0]["name"] == "Roadmap Cleanup"
    assert name_events[1]["session_id"] == other_session.session_id
    assert name_events[1]["name"] is None

    final_catalog = [record for record in records if record["type"] == "rpc.sessions"][-1]
    assert final_catalog["selected_session_id"] == selected_session.session_id
    assert final_catalog["selected_session_name"] == "Roadmap Cleanup"
    summaries = {summary["session_id"]: summary for summary in final_catalog["sessions"]}
    assert summaries[selected_session.session_id]["name"] == "Roadmap Cleanup"
    assert summaries[other_session.session_id]["name"] is None
    assert selected_session.read_name() == "Roadmap Cleanup"
    assert other_session.read_name() is None


def test_rpc_mode_clones_selected_session_then_reads_clone(tmp_path: Path) -> None:
    source = _create_two_turn_session(tmp_path)
    source_before = source.path.read_bytes()
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": source.session_id,
                }
            )
            + "\n"
            '{"id":"clone-1","type":"clone_session"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert all(record["schema_version"] == 34 for record in records)
    cloned = next(record for record in records if record["type"] == "rpc.session.cloned")
    assert cloned["command_id"] == "clone-1"
    assert cloned["source_session_id"] == source.session_id
    assert cloned["source_session_path"] == str(source.path)
    assert cloned["session_id"] != source.session_id
    assert cloned["entry_count"] == 4
    assert cloned["active_leaf_id"] == source.read_active_leaf_id()

    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["session_id"] == cloned["session_id"]
    assert report["session_path"] == cloned["session_path"]
    assert [message["content"] for message in report["messages"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]
    assert Path(cloned["session_path"]).is_file()
    assert source.path.read_bytes() == source_before


def test_rpc_mode_first_message_fork_persists_after_edited_prompt(tmp_path: Path) -> None:
    source = _create_two_turn_session(tmp_path)
    selected_entry = source.read_entries()[0]
    assert isinstance(selected_entry, MessageSessionEntry)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": source.session_id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "fork-1",
                    "type": "fork_session",
                    "entry_id": selected_entry.id,
                }
            )
            + "\n"
            '{"id":"prompt-1","type":"prompt","prompt":"edited first"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert all(record["schema_version"] == 34 for record in records)
    forked = next(record for record in records if record["type"] == "rpc.session.forked")
    assert forked["command_id"] == "fork-1"
    assert forked["source_session_id"] == source.session_id
    assert forked["selected_entry_id"] == selected_entry.id
    assert forked["selected_prompt"] == "first"
    assert forked["entry_count"] == 0
    assert forked["active_leaf_id"] is None

    report = next(record for record in records if record["type"] == "rpc.messages")
    assert report["session_id"] == forked["session_id"]
    assert [message["content"] for message in report["messages"]][-2:] == [
        "edited first",
        "fake response to: edited first",
    ]
    assert Path(forked["session_path"]).is_file()


def test_rpc_mode_pages_navigates_and_resubmits_session_tree_prompt(
    tmp_path: Path,
) -> None:
    session = _create_two_turn_session(tmp_path)
    entries = session.read_entries()
    selected = entries[2]
    assert isinstance(selected, MessageSessionEntry)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"tree-empty","type":"get_session_tree"}\n'
            + json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            '{"id":"tree-1","type":"get_session_tree","limit":2}\n'
            + json.dumps(
                {
                    "id": "tree-2",
                    "type": "get_session_tree",
                    "limit": 10,
                    "after_entry_id": entries[1].id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "navigate-1",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                }
            )
            + "\n"
            '{"id":"prompt-1","type":"prompt","prompt":"edited second"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert all(record["schema_version"] == 34 for record in records)
    tree_reports = [record for record in records if record["type"] == "rpc.session.tree"]
    assert tree_reports[0]["session_id"] is None
    assert tree_reports[0]["session_path"] is None
    assert tree_reports[0]["nodes"] == []
    assert tree_reports[1]["total_node_count"] == 4
    assert [node["entry_id"] for node in tree_reports[1]["nodes"]] == [
        entries[0].id,
        entries[1].id,
    ]
    assert tree_reports[1]["truncated"] is True
    assert tree_reports[1]["next_after_entry_id"] == entries[1].id
    assert [node["entry_id"] for node in tree_reports[2]["nodes"]] == [
        entries[2].id,
        entries[3].id,
    ]
    assert tree_reports[2]["truncated"] is False
    assert tree_reports[2]["next_after_entry_id"] is None

    navigated = next(record for record in records if record["type"] == "rpc.session.tree.navigated")
    assert navigated["selected_entry_id"] == selected.id
    assert navigated["previous_active_leaf_id"] == entries[3].id
    assert navigated["active_leaf_id"] == entries[1].id
    assert navigated["editor_text"] == "second"
    assert navigated["changed"] is True
    assert navigated["entry_count"] == 5

    messages = next(record for record in records if record["type"] == "rpc.messages")
    assert [
        message["content"] for message in messages["messages"] if message["role"] != "system"
    ] == [
        "first",
        "answer one",
        "edited second",
        "fake response to: edited second",
    ]
    assert [node["preview"] for report in tree_reports[1:] for node in report["nodes"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]

    resumed = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-2",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            '{"id":"messages-2","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert resumed.exit_code == 0, resumed.output
    resumed_records = _jsonl_records(resumed.stdout)
    resumed_messages = next(
        record for record in resumed_records if record["type"] == "rpc.messages"
    )
    assert [
        message["content"]
        for message in resumed_messages["messages"]
        if message["role"] != "system"
    ] == [
        "first",
        "answer one",
        "edited second",
        "fake response to: edited second",
    ]


def test_rpc_mode_unreverts_navigation_after_process_restart(tmp_path: Path) -> None:
    session = _create_two_turn_session(tmp_path)
    entries = session.read_entries()
    selected = entries[2]
    leaf = entries[-1]
    assert isinstance(selected, MessageSessionEntry)
    assert isinstance(leaf, MessageSessionEntry)

    first = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {"id": "select-1", "type": "select_session", "session_id": session.session_id}
            )
            + "\n"
            + json.dumps(
                {
                    "id": "navigate-1",
                    "type": "navigate_session_tree",
                    "entry_id": selected.id,
                }
            )
            + "\n"
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {"id": "select-2", "type": "select_session", "session_id": session.session_id}
            )
            + "\n"
            '{"id":"unrevert-1","type":"unrevert_session_tree"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert second.exit_code == 0, second.output
    records = _jsonl_records(second.stdout)
    unreverted = next(
        record for record in records if record["type"] == "rpc.session.tree.unreverted"
    )
    assert unreverted["schema_version"] == 34
    assert unreverted["active_leaf_id"] == leaf.id
    report = next(record for record in records if record["type"] == "rpc.messages")
    assert [message["content"] for message in report["messages"]] == [
        "first",
        "answer one",
        "second",
        "answer two",
    ]


def test_rpc_tree_navigation_repairs_incomplete_tool_boundary_before_prompt(
    tmp_path: Path,
) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> MessageSessionEntry:
        await session.append_message(Message(role="user", content="old prompt"))
        assistant = await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        await session.append_message(
            Message(
                role="tool",
                content="old result",
                tool_call_id="call-1",
                tool_name="read",
            )
        )
        return assistant

    assistant = anyio.run(write)
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            json.dumps(
                {
                    "id": "select-1",
                    "type": "select_session",
                    "session_id": session.session_id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "navigate-1",
                    "type": "navigate_session_tree",
                    "entry_id": assistant.id,
                }
            )
            + "\n"
            '{"id":"prompt-1","type":"prompt","prompt":"continue"}\n'
            '{"id":"messages-1","type":"get_messages","limit":10}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    navigated = next(record for record in records if record["type"] == "rpc.session.tree.navigated")
    assert navigated["active_leaf_id"] == assistant.id
    assert navigated["editor_text"] is None
    messages = next(record for record in records if record["type"] == "rpc.messages")
    active = [message for message in messages["messages"] if message["role"] != "system"]
    assert [message["role"] for message in active] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert active[2]["content"] == INTERRUPTED_TOOL_RESULT_TEXT
    assert active[2]["tool_call_id"] == "call-1"
    assert active[2]["is_error"] is True
    assert active[-2]["content"] == "continue"


def test_rpc_mode_reports_idle_and_reconfigured_state(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"state-1","type":"get_state"}\n'
            '{"id":"configure-1","type":"configure","model":"fake-custom-model","effort":"high"}\n'
            '{"id":"state-2","type":"get_state"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    reports = [record for record in records if record["type"] == "rpc.state"]
    assert [record["command_id"] for record in reports] == ["state-1", "state-2"]
    assert reports[0]["state"] == {
        "provider": "fake",
        "model": "fake",
        "mode": "build",
        "effort": None,
        "auto_compaction_enabled": True,
        "steering_mode": "one_at_a_time",
        "follow_up_mode": "one_at_a_time",
        "pending_steering_count": 0,
        "pending_follow_up_count": 0,
        "session_id": None,
        "session_path": None,
        "session_name": None,
        "active_command_id": None,
        "active_command_type": None,
        "cancel_requested": False,
    }
    assert reports[1]["state"] == {
        **reports[0]["state"],
        "model": "fake-custom-model",
        "effort": "high",
    }
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.state",
        "rpc.command.finished",
        "rpc.command.started",
        "rpc.command.finished",
        "rpc.command.started",
        "rpc.state",
        "rpc.command.finished",
    ]


def test_rpc_mode_new_session_is_lazy_and_reports_no_selected_session(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"new-1","type":"new_session"}\n'
            '{"id":"new-2","type":"new_session"}\n'
            '{"id":"state-1","type":"get_state"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("new-1", True),
        ("new-2", True),
        ("state-1", True),
    ]
    state = next(record["state"] for record in records if record["type"] == "rpc.state")
    assert state["session_id"] is None
    assert state["session_path"] is None
    assert list(tmp_path.glob("*.jsonl")) == []


def test_rpc_mode_configures_model_for_future_prompts(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"configure-1","type":"configure","model":"fake-custom-model"}\n'
            '{"id":"cmd-1","type":"prompt","prompt":"hello"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    configure_finished = records[1]
    assert configure_finished["type"] == "rpc.command.finished"
    assert configure_finished["command_id"] == "configure-1"
    assert configure_finished["command_type"] == "configure"
    assert configure_finished["ok"] is True
    assert configure_finished["error"] is None
    assert any(record.get("content") == "fake response to: hello" for record in records)


def test_rpc_mode_orders_provider_switch_between_prompts_in_same_session(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path / "sessions")],
        input=(
            '{"id":"prompt-1","type":"prompt","prompt":"first"}\n'
            '{"id":"configure-1","type":"configure","provider":"openai"}\n'
            '{"id":"prompt-2","type":"prompt","prompt":"second"}\n'
        ),
        env={
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_AUTH_FILE": str(tmp_path / "auth.json"),
            "OPENAI_API_KEY": "",
        },
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == [
        "prompt-1",
        "configure-1",
        "prompt-2",
    ]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("prompt-1", True),
        ("configure-1", True),
        ("prompt-2", False),
    ]
    estimates = [record for record in records if record["type"] == "context.estimated"]
    assert [record["provider"] for record in estimates] == ["fake", "openai"]
    agent_starts = [record for record in records if record["type"] == "agent.started"]
    assert len(agent_starts) == 2
    assert agent_starts[0]["session_id"] == agent_starts[1]["session_id"]
    assert any(record.get("content") == "fake response to: first" for record in records)
    assert any(
        record["type"] == "error" and "openai credentials are required" in record["message"]
        for record in records
    )


def test_rpc_mode_configure_rejects_unknown_provider(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"configure-1","type":"configure","provider":"missing"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "Unknown provider: missing"
    assert records[2]["ok"] is False


def test_rpc_mode_generates_command_id_when_missing(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    started = records[0]
    finished = records[-1]
    assert started["type"] == "rpc.command.started"
    assert finished["type"] == "rpc.command.finished"
    assert isinstance(started["command_id"], str)
    assert started["command_id"]
    assert finished["command_id"] == started["command_id"]
    assert finished["ok"] is True


def test_rpc_mode_runs_multiple_prompt_commands_in_one_session(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"first"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["fake response to: first", "fake response to: second"]
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == ["cmd-1", "cmd-2"]
    assert [record["command_id"] for record in finished] == ["cmd-1", "cmd-2"]
    assert all(record["ok"] is True for record in finished)
    session_paths = {record["path"] for record in records if record["type"] == "session.saved"}
    assert len(session_paths) == 1
    session_file = next(tmp_path.glob("*.jsonl"))
    session_records = _jsonl_records(session_file.read_text(encoding="utf-8"))
    user_messages = [
        record["message"]["content"]
        for record in session_records
        if record["kind"] == "message" and record["message"]["role"] == "user"
    ]
    assert user_messages == ["first", "second"]


def test_rpc_mode_reports_bad_commands_and_continues(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            "not json\n"
            "[]\n"
            '{"id":"bad","type":"missing"}\n'
            '{"id":"missing-prompt","type":"prompt"}\n'
            '{"id":123,"type":"prompt","prompt":"bad id"}\n'
            '{"id":"ok","type":"prompt","prompt":"ok"}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    error_messages = [record["message"] for record in records if record["type"] == "error"]
    assert error_messages[:5] == [
        "RPC frame is not valid JSON",
        "RPC frame must be a JSON object",
        "Unknown RPC command: missing",
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
    ]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert (finished[0]["command_id"], finished[0]["ok"], finished[0]["error"]) == (
        "bad",
        False,
        "Unknown RPC command: missing",
    )
    assert any(
        record["type"] == "message.completed" and record["content"] == "fake response to: ok"
        for record in records
    )
    assert finished[-1]["command_id"] == "ok"
    assert finished[-1]["ok"] is True


def test_rpc_mode_reports_internal_tool_result_failure_and_continues(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    original_summary = tool_execution.summarize_tool_result
    summary_calls = 0

    def fail_first_summary(
        name: str,
        data: Mapping[str, object],
        *,
        truncated: bool = False,
    ) -> str | None:
        nonlocal summary_calls
        summary_calls += 1
        if summary_calls == 1:
            raise RuntimeError("internal api-key=secret")
        return original_summary(name, data, truncated=truncated)

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_tool_runtime)
    monkeypatch.setattr(tool_execution, "summarize_tool_result", fail_first_summary)

    result = runner.invoke(
        app,
        [
            "--mode",
            "rpc",
            "--yes",
            "--allow-tool",
            "danger",
            "--session-dir",
            str(tmp_path),
        ],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"first"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
        ),
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert "internal api-key=secret" not in result.stdout
    records = _jsonl_records(result.stdout)
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"], record["error"]) for record in finished] == [
        ("cmd-1", False, "Internal error while processing a tool result"),
        ("cmd-2", True, None),
    ]
    assert any(
        record["type"] == "message.completed" and record["content"] == "done" for record in records
    )


def test_rpc_mode_rejects_cli_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--mode", "rpc", "--session-dir", str(tmp_path)],
        input="",
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    assert records[1]["message"] == (
        "--prompt is not used with --mode rpc; send prompt commands on stdin"
    )


@pytest.mark.parametrize(
    ("arguments", "environment", "message"),
    [
        (
            ["--resume", "missing", "--continue"],
            {"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
            "use either --resume or --continue, not both",
        ),
        (
            [],
            {"WISP_PROVIDER": "fake", "WISP_AUTO_COMPACTION": "sometimes"},
            "WISP_AUTO_COMPACTION must be one of",
        ),
    ],
)
def test_rpc_mode_negotiates_before_startup_errors(
    tmp_path: Path,
    arguments: list[str],
    environment: dict[str, str],
    message: str,
) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", *arguments, "--session-dir", str(tmp_path)],
        input="",
        env=environment,
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    assert message in records[1]["message"]


def test_rpc_mode_bounds_post_handshake_startup_errors(tmp_path: Path) -> None:
    provider = "x" * 5_000
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--provider", provider, "--session-dir", str(tmp_path)],
        input="",
        env={"WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    message = records[1]["message"]
    assert isinstance(message, str)
    assert len(message) == cli_module.rpc._MAX_RPC_STARTUP_ERROR_CHARS
    assert message.startswith("Unknown provider: ")
    assert message.endswith("...")


def test_rpc_mode_shutdown_emits_lifecycle_and_exits(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"bye","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[0]["command_id"] == "bye"
    assert records[0]["command_type"] == "shutdown"
    assert records[1]["command_id"] == "bye"
    assert records[1]["command_type"] == "shutdown"
    assert records[1]["ok"] is True


def test_rpc_mode_cancels_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert "RPC command cancelled: cmd-1" in errors
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    cancel_finished = next(record for record in finished if record["command_id"] == "cancel-1")
    prompt_finished = next(record for record in finished if record["command_id"] == "cmd-1")
    cancel_started_index = next(
        index
        for index, record in enumerate(records)
        if record["type"] == "rpc.command.started" and record["command_id"] == "cancel-1"
    )
    cancel_finished_index = records.index(cancel_finished)
    prompt_finished_index = records.index(prompt_finished)
    assert cancel_finished["ok"] is True
    assert prompt_finished["ok"] is False
    assert prompt_finished["error"] == "RPC command cancelled: cmd-1"
    assert cancel_started_index < cancel_finished_index < prompt_finished_index


class _TrustedGate:
    async def resolve(self) -> bool:
        return True


def test_rpc_prompt_cancellation_restores_active_leaf_before_completion_boundary(
    tmp_path: Path,
) -> None:
    class PreResponseCancellableProvider(CancellableProvider):
        def __init__(self, started: anyio.Event) -> None:
            self.started = started

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
            self.started.set()
            await anyio.sleep_forever()
            async for event in super().stream(
                messages,
                model=model,
                tools=tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=effort,
            ):
                yield event

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        agent = CodingSession(
            provider=PreResponseCancellableProvider(started),
            sessions=JsonlSessionStore(tmp_path),
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    (),
                    0,
                    "slow",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_module.rpc._write_json_event,
                    cli_module.rpc._render_json_events,
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        entries = session.read_entries()
        assert session.read_context_messages() == ()
        assert session.read_active_leaf_id() is None
        assert isinstance(entries[-1], ActiveLeafSessionEntry)
        assert entries[-1].active_leaf_id is None
        return completed, len(entries)

    completed, audit_entry_count = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.command_id == "cmd-1"
    assert completed.history is not None
    assert completed.history == ()
    assert completed.entry_count == audit_entry_count


def test_rpc_cancellation_during_run_snapshot_preserves_existing_context(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run_prompt() -> object:
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="existing prompt"))
        committed_history = session.read_context_messages()
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=CancellableProvider(),
            sessions=JsonlSessionStore(tmp_path),
        )
        snapshot_started = anyio.Event()
        release_snapshot = anyio.Event()
        execution = rpc_execution_module
        original_run_sync = execution.anyio.to_thread.run_sync

        async def delayed_run_sync(func: object, *args: object, **kwargs: object) -> object:
            if func is execution.rpc_session_run_start:
                snapshot_started.set()
                await release_snapshot.wait()
            return await original_run_sync(func, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(execution.anyio.to_thread, "run_sync", delayed_run_sync)
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    committed_history,
                    entry_start,
                    "slow",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_module.rpc._write_json_event,
                    cli_module.rpc._render_json_events,
                )
                await snapshot_started.wait()
                cancel_scope.cancel()
                release_snapshot.set()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        assert session.read_context_messages() == committed_history
        assert not any(
            isinstance(entry, ActiveLeafSessionEntry) and entry.active_leaf_id is None
            for entry in session.read_entries()
        )
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.history is not None
    assert [message.content for message in completed.history] == ["existing prompt"]


def test_rpc_prestart_cancellation_preserves_loaded_tool_repair(tmp_path: Path) -> None:
    class PreResponseCancellableProvider(CancellableProvider):
        def __init__(self, started: anyio.Event) -> None:
            self.started = started

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
            self.started.set()
            await anyio.sleep_forever()
            async for event in super().stream(
                messages,
                model=model,
                tools=tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=effort,
            ):
                yield event

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        await session.append_message(Message(role="user", content="old prompt"))
        await session.append_message(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    ),
                ),
                finish_reason="tool_calls",
            )
        )
        committed_history = session.read_messages()
        entry_start = len(session.read_entries())
        agent = CodingSession(
            provider=PreResponseCancellableProvider(started),
            sessions=JsonlSessionStore(tmp_path),
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    committed_history,
                    entry_start,
                    "cancelled prompt",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_module.rpc._write_json_event,
                    cli_module.rpc._render_json_events,
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        audit_messages = session.read_messages()
        assert [message.role for message in audit_messages[:3]] == ["user", "assistant", "tool"]
        assert len(audit_messages) > 3
        context_messages = session.read_context_messages()
        assert [message.role for message in context_messages] == ["user", "assistant", "tool"]
        assert context_messages[-1].content == INTERRUPTED_TOOL_RESULT_TEXT
        return completed, len(session.read_entries())

    completed, audit_entry_count = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.history is not None
    assert [message.role for message in completed.history] == ["user", "assistant", "tool"]
    assert completed.entry_count == audit_entry_count


def test_rpc_prompt_cancellation_retains_entries_after_completion_boundary(
    tmp_path: Path,
) -> None:
    class BlockingDangerTool:
        name = "danger"
        safety = "read"
        description = "Block after the provider completes its tool-call turn."
        input_schema: ToolInputSchema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

        def __init__(self, started: anyio.Event) -> None:
            self.started = started

        async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
            self.started.set()
            await anyio.sleep_forever()
            return ToolResult(text="unused")

    async def run_prompt() -> object:
        started = anyio.Event()
        session = JsonlSessionStore(tmp_path).create()
        tools = ToolRegistry()
        tools.register(BlockingDangerTool(started))
        agent = CodingSession(
            provider=ToolCallingProvider(),
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
        )
        cancel_scope = anyio.CancelScope()
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    run_rpc_prompt_command,
                    agent,
                    session,
                    (),
                    0,
                    "use tool",
                    "cmd-1",
                    "prompt",
                    cancel_scope,
                    send.clone(),
                    _TrustedGate(),
                    cli_module.rpc._write_json_event,
                    cli_module.rpc._render_json_events,
                )
                await started.wait()
                cancel_scope.cancel()
                completed = await receive.receive()
                if isinstance(completed, _RpcPromptReady):
                    completed = await receive.receive()

        retained = [
            message
            for message in session.read_messages()
            if message.role in {"user", "assistant", "tool"}
        ]
        assert [message.role for message in retained] == ["user", "assistant", "tool"]
        assert retained[0].content == "use tool"
        assert retained[1].tool_calls is not None
        assert [call.call_id for call in retained[1].tool_calls] == ["call-1"]
        assert retained[2].tool_call_id == "call-1"
        assert retained[2].content == INTERRUPTED_TOOL_RESULT_TEXT
        assert retained[2].is_error is True
        return completed

    completed = anyio.run(run_prompt)

    assert completed.ok is False
    assert completed.command_id == "cmd-1"
    assert completed.history is not None
    assert [
        message.role
        for message in completed.history
        if message.role in {"user", "assistant", "tool"}
    ] == ["user", "assistant", "tool"]
    assert completed.entry_count > 0


def test_rpc_mode_cancel_reports_unknown_target(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cancel-1","type":"cancel","target_id":"missing"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "No running or queued RPC command with id: missing"
    assert records[2]["command_id"] == "cancel-1"
    assert records[2]["ok"] is False
    assert records[2]["error"] == "No running or queued RPC command with id: missing"


def test_rpc_mode_cancel_requires_target_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cancel-1","type":"cancel"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["error"]
    assert records[0]["message"] == "RPC command does not match the negotiated schema"


def test_rpc_approval_policy_approves_waiting_tool_call(tmp_path: Path) -> None:
    provider = ToolCallingProvider()
    tools = ToolRegistry()
    tools.register(DangerTool())
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_approval_policy=approval_policy,
        )
        events: list[object] = []
        async for event in agent.run("use tool"):
            events.append(event)
            if isinstance(event, ToolApprovalRequested):
                assert approval_policy.resolve_approval(call_id=event.call_id, approved=True)
        return events

    events = anyio.run(run_agent)

    resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert resolved.approved is True
    assert resolved.reason is None
    assert result.output == "changed file.txt"
    assert result.is_error is False


def test_rpc_approval_policy_denies_waiting_tool_call(tmp_path: Path) -> None:
    provider = ToolCallingProvider()
    tools = ToolRegistry()
    tools.register(DangerTool())
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())

    async def run_agent() -> list[object]:
        agent = CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path),
            tool_registry=tools,
            tool_approval_policy=approval_policy,
        )
        events: list[object] = []
        async for event in agent.run("use tool"):
            events.append(event)
            if isinstance(event, ToolApprovalRequested):
                assert approval_policy.resolve_approval(
                    call_id=event.call_id,
                    approved=False,
                    reason="not allowed",
                )
        return events

    events = anyio.run(run_agent)

    resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert resolved.approved is False
    assert resolved.reason == "not allowed"
    assert result.output == "not allowed"
    assert result.is_error is True


def test_rpc_approval_command_resolves_pending_approval(
    monkeypatch: MonkeyPatch,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    handle_rpc_control_command(
        {
            "id": "approval-1",
            "type": "approval",
            "call_id": "call-1",
            "approved": False,
            "reason": "not safe",
        },
        running_command=None,
        approval_policy=approval_policy,
        write_event=cli_module.rpc._write_json_event,
    )

    async def wait_for_decision() -> object:
        return await approval_policy.await_approval(tool, call_id="call-1", arguments={})

    decision = anyio.run(wait_for_decision)
    records = _jsonl_records(output.getvalue())
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[1]["command_id"] == "approval-1"
    assert records[1]["ok"] is True
    assert decision == cli_module.ToolApprovalDecision(approved=False, reason="not safe")


def test_rpc_approval_policy_rejects_duplicate_decisions() -> None:
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=False,
        reason="first decision",
    )
    assert not approval_policy.resolve_approval(call_id="call-1", approved=True)

    async def wait_for_decision() -> object:
        return await approval_policy.await_approval(tool, call_id="call-1", arguments={})

    decision = anyio.run(wait_for_decision)
    assert decision == cli_module.ToolApprovalDecision(
        approved=False,
        reason="first decision",
    )


def test_rpc_approval_policy_remembers_exact_tool_for_process() -> None:
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    other_tool = BashTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=True,
        scope="tool_session",
    )
    assert approval_policy.approves(tool) is True
    assert approval_policy.requires_approval(other_tool) is True
    assert RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval()).approves(tool) is False


def test_rpc_approval_policy_remembers_all_unsafe_tools_for_process() -> None:
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    tool = DangerTool()
    approval_policy.prepare_approval(tool, call_id="call-1", arguments={})

    assert approval_policy.resolve_approval(
        call_id="call-1",
        approved=True,
        scope="all_session",
    )
    assert approval_policy.approves(tool) is True
    assert approval_policy.approves(BashTool()) is True


@pytest.mark.parametrize(
    ("scope", "approved", "message"),
    [
        ("forever", True, "field scope must be one of"),
        ([], True, "field scope must be one of"),
        ("tool_session", False, "scope is only valid for approved requests"),
    ],
)
def test_rpc_approval_command_rejects_invalid_scope(
    monkeypatch: MonkeyPatch,
    scope: object,
    approved: bool,
    message: str,
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    approval_policy = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
    approval_policy.prepare_approval(DangerTool(), call_id="call-1", arguments={})

    handle_rpc_control_command(
        {
            "id": "approval-1",
            "type": "approval",
            "call_id": "call-1",
            "approved": approved,
            "scope": scope,
        },
        running_command=None,
        approval_policy=approval_policy,
        write_event=cli_module.rpc._write_json_event,
    )

    records = _jsonl_records(output.getvalue())
    assert records[-1]["ok"] is False
    assert message in str(records[-1]["error"])


def test_rpc_mode_denies_pending_approval_when_input_closes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_tool_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--allow-tool", "danger", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"use tool"}\n',
        env={"WISP_PROVIDER": "tool-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    approval_resolved = next(
        record for record in records if record["type"] == "tool.approval.resolved"
    )
    assert approval_resolved["approved"] is False
    assert approval_resolved["reason"] == "RPC input closed before approval response"
    tool_result = next(record for record in records if record["type"] == "tool.result")
    assert tool_result["is_error"] is True
    assert tool_result["output"] == "RPC input closed before approval response"
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [("cmd-1", True)]


def test_rpc_mode_approval_reports_unknown_call_id(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"approval-1","type":"approval","call_id":"missing","approved":true}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "error",
        "rpc.command.finished",
    ]
    assert records[1]["message"] == "No pending tool approval with call_id: missing"
    assert records[2]["command_id"] == "approval-1"
    assert records[2]["ok"] is False


def test_rpc_mode_approval_requires_valid_fields(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"approval-1","type":"approval","approved":true}\n'
            '{"id":"approval-2","type":"approval","call_id":"call-1"}\n'
            '{"id":"approval-3","type":"approval","call_id":"call-1","approved":false,"reason":3}\n'
        ),
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    errors = [record["message"] for record in records if record["type"] == "error"]
    assert errors == [
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
        "RPC command does not match the negotiated schema",
    ]


def test_rpc_mode_rejects_commands_beyond_queue_cap_while_prompt_runs(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)
    monkeypatch.setattr(rpc_host_module, "_MAX_QUEUED_RPC_COMMANDS", 2)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cmd-3","type":"prompt","prompt":"third"}\n'
            '{"id":"cmd-overflow","type":"prompt","prompt":"overflow"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    overflow_error = next(
        record
        for record in records
        if record["type"] == "rpc.command.finished" and record["command_id"] == "cmd-overflow"
    )
    assert overflow_error["ok"] is False
    assert overflow_error["error"] == (
        "RPC command queue is full while another RPC command is running"
    )
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert ("cancel-1", True) in [(record["command_id"], record["ok"]) for record in finished]
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second", "done third"]


def test_rpc_mode_processes_queued_shutdown_after_running_prompt_finishes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"shutdown-1","type":"shutdown"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert "RPC prompt command requires string field: prompt" not in [
        record.get("message") for record in records
    ]
    started = [record for record in records if record["type"] == "rpc.command.started"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [record["command_id"] for record in started] == [
        "cmd-1",
        "cancel-1",
        "shutdown-1",
    ]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("shutdown-1", True),
    ]


def test_rpc_mode_finishes_command_when_session_write_fails_before_file_exists(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()

    async def fail_append_message(
        self: JsonlSession,
        message: Message,
        *,
        operation_id: str | None = None,
    ) -> object:
        del operation_id
        raise RuntimeError("session write failed")

    monkeypatch.setattr(JsonlSession, "append_message", fail_append_message)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"cmd-1","type":"prompt","prompt":"hello"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assert {record["type"] for record in records} >= {"error", "rpc.command.finished"}
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [
        (record["command_id"], record["command_type"], record["ok"], record["error"])
        for record in finished
    ] == [("cmd-1", "prompt", False, "session write failed")]


def test_rpc_mode_preserves_failed_prompt_in_next_prompt_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_failing_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"fail"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
        ),
        env={"WISP_PROVIDER": "failing-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["saw failed history"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cmd-1", False),
        ("cmd-2", True),
    ]


def test_rpc_mode_excludes_cancelled_prompt_from_next_prompt_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second"]
    session = JsonlSessionStore(tmp_path).latest()
    persisted_user_messages = [
        message.content for message in session.read_messages() if message.role == "user"
    ]
    assert persisted_user_messages == ["second"]


def test_rpc_mode_queues_prompts_while_canceling_running_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runner = CliRunner()
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=(
            '{"id":"cmd-1","type":"prompt","prompt":"slow"}\n'
            '{"id":"cmd-2","type":"prompt","prompt":"second"}\n'
            '{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
        ),
        env={"WISP_PROVIDER": "cancellable-test", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _jsonl_records(result.stdout)
    assistant_messages = [
        record["content"] for record in records if record["type"] == "message.completed"
    ]
    assert assistant_messages == ["done second"]
    finished = [record for record in records if record["type"] == "rpc.command.finished"]
    assert [(record["command_id"], record["ok"]) for record in finished] == [
        ("cancel-1", True),
        ("cmd-1", False),
        ("cmd-2", True),
    ]
