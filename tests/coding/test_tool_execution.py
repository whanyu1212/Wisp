"""Tests for the coding-layer tool executor's result promotion (issue #74).

The executor promotes a narrow ``exit_code`` scalar from a tool's structured
``ToolResult.data`` onto the event, gated to shell-like tools. Gating agent-side
(rather than in the renderer) is what keeps a custom tool that stashes an
unrelated ``exit_code`` from being styled as a failure.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest

import wisp.coding.tool_execution as tool_execution
from wisp.agent.tool_contracts import PreparedToolExecution
from wisp.coding.tool_execution import (
    ConfiguredToolExecutor,
    ToolResultProcessingError,
    _promote_before_text,
    _promote_created,
    _promote_exit_code,
)
from wisp.events import ToolExecutionEnded, wisp_event_from_json
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolExecutionMetadata
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult

_PROCESS_START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        pytest.param("bash", {"exit_code": 2}, 2, id="failure"),
        pytest.param("bash", {"exit_code": 0}, 0, id="success"),
        # A custom tool may legitimately carry an integer "exit_code" that has no
        # process-exit meaning; it must not reach the event or drive failure styling.
        pytest.param("custom", {"exit_code": 7}, None, id="custom-tool"),
        pytest.param("search", {"exit_code": 1}, None, id="search-tool"),
        pytest.param("bash", {}, None, id="absent"),
        pytest.param("bash", {"exit_code": "boom"}, None, id="string"),
        pytest.param("bash", {"exit_code": None}, None, id="none"),
    ],
)
def test_promote_exit_code_only_for_shell_ints(
    name: str, data: Mapping[str, object], expected: int | None
) -> None:
    assert _promote_exit_code(name, data) == expected


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        pytest.param("write", {"before_text": "old\n"}, "old\n", id="snapshot"),
        pytest.param("write", {"before_text": ""}, "", id="empty-snapshot"),
        # A custom tool that stashes a "before_text" must not feed the diff renderer;
        # only the write tool's snapshot is promoted onto the event.
        pytest.param("edit", {"before_text": "old\n"}, None, id="edit-tool"),
        pytest.param("custom", {"before_text": "old\n"}, None, id="custom-tool"),
        pytest.param("write", {}, None, id="absent"),
        pytest.param("write", {"before_text": 123}, None, id="int"),
        pytest.param("write", {"before_text": None}, None, id="none"),
    ],
)
def test_promote_before_text_only_for_write_strings(
    name: str, data: Mapping[str, object], expected: str | None
) -> None:
    assert _promote_before_text(name, data) == expected


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        pytest.param("write", {"created": True}, True, id="created"),
        pytest.param("write", {"created": False}, False, id="overwrote"),
        # Only write-like tools report a create; anything else is treated as "not a
        # create" so a stray "created" key can't drive create-style rendering.
        pytest.param("edit", {"created": True}, False, id="edit-tool"),
        pytest.param("custom", {"created": True}, False, id="custom-tool"),
        # Conservative default: a missing or odd value reads as "overwrote", which never
        # fabricates a create-style pure-addition diff.
        pytest.param("write", {}, False, id="absent"),
        pytest.param("write", {"created": "yes"}, False, id="string"),
        pytest.param("write", {"created": None}, False, id="none"),
    ],
)
def test_promote_created_only_for_write_bools(
    name: str, data: Mapping[str, object], expected: bool
) -> None:
    assert _promote_created(name, data) is expected


class _TruncatingTool:
    name = "capped"
    safety = "read"
    description = "Returns truncated output."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, *, truncated: object) -> None:
        self._truncated = truncated

    async def run(self, arguments: object, context: object) -> ToolResult:
        result = ToolResult(text="partial output", data={})
        # ToolResult is a plain frozen dataclass with no validation, so a custom or
        # malformed extension tool can end up with a non-bool truncated. Bypass the
        # frozen guard to plant exactly that (a normal tool can't, but the executor
        # must still cope) without the constructor's type hint getting in the way.
        object.__setattr__(result, "truncated", self._truncated)
        return result


class _RaisingTool:
    name = "raising"
    safety = "read"
    description = "Raises an unexpected exception."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def run(self, arguments: object, context: object) -> ToolResult:
        raise self._exc


class _MalformedTextResult:
    @property
    def text(self) -> str:
        raise ValueError("secret result detail")


class _MalformedResultTool:
    name = "malformed"
    safety = "read"
    description = "Returns a malformed result."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    async def run(self, arguments: object, context: object) -> object:
        return _MalformedTextResult()


class _ResultTool:
    safety = "read"
    description = "Returns a configurable result."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, *, name: str, result: ToolResult) -> None:
        self.name = name
        self._result = result
        self.calls = 0

    async def run(self, arguments: object, context: object) -> ToolResult:
        self.calls += 1
        return self._result


class _PendingApprovalPolicy(ToolApprovalPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.pending_call_id: str | None = None
        self.cancel_reason: str | None = None

    def prepare_approval(
        self,
        tool: object,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> None:
        del tool, arguments
        self.pending_call_id = call_id

    def cancel_approval(self, *, call_id: str, reason: str) -> bool:
        if self.pending_call_id != call_id:
            return False
        self.pending_call_id = None
        self.cancel_reason = reason
        return True


class _NoIterMapping(Mapping[str, object]):
    def __init__(self, values: Mapping[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("result metadata must not be iterated")

    def __len__(self) -> int:
        raise AssertionError("result metadata size must not be requested")


class _RaisingList(list[object]):
    def __len__(self) -> int:
        raise AssertionError("extension list subclass must not be inspected")


def _run_executor(
    tool: object,
    *,
    context: ToolContext | None = None,
) -> ToolExecutionEnded:
    registry = ToolRegistry()
    registry.register(tool)  # type: ignore[arg-type]
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=context or ToolContext(cwd=Path.cwd(), protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )

    call = ToolCall(call_id="c1", name=tool.name, arguments={})  # type: ignore[attr-defined]

    async def run() -> ToolExecutionEnded:
        events = [event async for event in executor.execute(call)]
        return next(e for e in events if isinstance(e, ToolExecutionEnded))

    return anyio.run(run)


def test_closing_preparation_cancels_pending_approval() -> None:
    tool = _ResultTool(name="mutating", result=ToolResult(text="done"))
    tool.safety = "mutating"  # type: ignore[assignment]
    registry = ToolRegistry()
    registry.register(tool)  # type: ignore[arg-type]
    approval_policy = _PendingApprovalPolicy()
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=Path.cwd(), protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=approval_policy,
    )
    call = ToolCall(call_id="c1", name=tool.name, arguments={})

    async def run() -> None:
        preparation = executor.prepare(call)
        requested = await anext(preparation)
        assert requested.type == "tool.approval.requested"
        assert approval_policy.pending_call_id == call.call_id
        await preparation.aclose()

    anyio.run(run)

    assert approval_policy.pending_call_id is None
    assert approval_policy.cancel_reason == "Agent run cancelled"
    assert tool.calls == 0


def test_executor_prepares_parallel_tool_without_starting_it() -> None:
    tool = _ResultTool(name="parallel", result=ToolResult(text="done"))
    registry = ToolRegistry()
    registry.register(tool, execution=ToolExecutionMetadata(parallel_safe=True))  # type: ignore[arg-type]
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=Path.cwd(), protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )
    call = ToolCall(call_id="c1", name=tool.name, arguments={})

    async def run() -> tuple[PreparedToolExecution, ToolExecutionEnded]:
        preparation = [event async for event in executor.prepare(call)]
        prepared = next(event for event in preparation if isinstance(event, PreparedToolExecution))
        assert prepared.call_id == call.call_id
        assert prepared.name == call.name
        assert prepared.parallel_safe
        assert tool.calls == 0
        return prepared, await prepared.run()

    _, ended = anyio.run(run)

    assert tool.calls == 1
    assert ended.call_id == call.call_id
    assert ended.output == "done"
    assert not ended.is_error


def test_executor_promotes_truncated_from_tool_result() -> None:
    # The tool's own truncated flag rides onto the event so the card can honestly
    # mark output the tool itself capped.
    assert _run_executor(_TruncatingTool(truncated=True)).truncated is True
    assert _run_executor(_TruncatingTool(truncated=False)).truncated is False


def test_executor_tolerates_non_bool_truncated_flag() -> None:
    # A malformed extension tool handing back a non-bool truncated must not raise a
    # Pydantic ValidationError when ToolExecutionEnded is built (outside _run_tool's
    # try/except) — that would abort the tool stream. It degrades to truncated=False,
    # and the tool otherwise succeeds (the odd flag is not itself an error).
    for bad in (None, "weird", 3, [1]):
        ended = _run_executor(_TruncatingTool(truncated=bad))
        assert ended.truncated is False
        assert ended.is_error is False


def test_executor_shows_tool_errors_to_model_within_bounds() -> None:
    detail = "x" * 2_100

    ended = _run_executor(_RaisingTool(ToolError(detail)))

    assert ended.is_error is True
    assert ended.failure_code is None
    assert ended.output.endswith("...")
    assert len(ended.output) == 2_000


def test_executor_projects_actionable_tool_failure_metadata() -> None:
    ended = _run_executor(
        _RaisingTool(
            ToolError(
                "Invalid grep pattern",
                failure_code="invalid_pattern",
                retryable=True,
                recovery_hint="Retry with literal=true.",
            )
        )
    )

    assert ended.is_error is True
    assert ended.failure_code == "invalid_pattern"
    assert ended.retryable is True
    assert ended.recovery_hint == "Retry with literal=true."
    assert ended.output == "Invalid grep pattern\nRecovery: Retry with literal=true."


def test_executor_reserves_output_space_for_recovery_hint() -> None:
    hint = "Retry with literal=true."
    ended = _run_executor(
        _RaisingTool(
            ToolError(
                "x" * 2_100,
                failure_code="invalid_pattern",
                retryable=True,
                recovery_hint=hint,
            )
        )
    )

    assert len(ended.output) == 2_000
    assert ended.output.endswith(f"\nRecovery: {hint}")
    assert "...\nRecovery:" in ended.output


def test_executor_classifies_tool_argument_errors() -> None:
    ended = _run_executor(_RaisingTool(ToolArgumentError("limit must be an integer")))

    assert ended.failure_code == "invalid_arguments"
    assert ended.retryable is True
    assert ended.recovery_hint == "Retry with arguments that match the tool's input schema."
    assert ended.output.endswith(f"\nRecovery: {ended.recovery_hint}")


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            ToolError("retry me", retryable=True, recovery_hint="secret hint"),
            id="inconsistent-metadata",
        ),
        pytest.param(RuntimeError("api-key=secret"), id="unexpected-exception"),
        pytest.param(ToolError("\ud800"), id="unencodable-message"),
    ],
)
def test_executor_hides_untrusted_error_detail(exc: Exception) -> None:
    ended = _run_executor(_RaisingTool(exc))

    assert ended.is_error is True
    assert ended.failure_code == "internal_error"
    assert ended.retryable is False
    assert ended.recovery_hint is None
    assert ended.output == "Tool execution failed"


def test_executor_hides_malformed_result_detail_from_model() -> None:
    ended = _run_executor(_MalformedResultTool())

    assert ended.is_error is True
    assert ended.output == "Tool returned an invalid result"
    assert "secret" not in ended.output


def test_executor_treats_unencodable_result_text_as_malformed() -> None:
    ended = _run_executor(_ResultTool(name="custom", result=ToolResult(text="\ud800")))

    assert ended.is_error is True
    assert ended.output == "Tool returned an invalid result"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("write", {"before_text": "\ud800"}),
        ("read", {"line_count": 1, "path": "\ud800"}),
    ],
)
def test_executor_treats_unencodable_metadata_as_malformed(
    name: str, data: Mapping[str, object]
) -> None:
    ended = _run_executor(_ResultTool(name=name, result=ToolResult(text="result", data=data)))

    assert ended.is_error is True
    assert ended.output == "Tool returned an invalid result"


def test_executor_bounds_successful_extension_output() -> None:
    ended = _run_executor(_ResultTool(name="custom", result=ToolResult(text="x" * 60_000)))

    assert ended.is_error is False
    assert ended.truncated is True
    assert len(ended.output.encode()) <= 50_000
    assert ended.output.endswith("[truncated]")


def test_executor_reads_only_recognized_result_metadata() -> None:
    data = _NoIterMapping({"line_count": 2, "selected_count": 1, "path": "file.py"})

    ended = _run_executor(_ResultTool(name="read", result=ToolResult(text="line", data=data)))

    assert ended.is_error is False
    assert ended.summary == "read 1 line of 2 from file.py"


def test_executor_preserves_exact_ls_entry_count_for_summary() -> None:
    ended = _run_executor(
        _ResultTool(
            name="ls",
            result=ToolResult(
                text="a\nb\n[truncated]",
                data={"entries": ["a", "b"], "entry_count": 100, "path": "big/"},
                truncated=True,
            ),
        )
    )

    assert ended.is_error is False
    assert ended.summary == "ls: 100 entries in big/ (+ more)"


def test_executor_rejects_hostile_metadata_subclasses() -> None:
    ended = _run_executor(
        _ResultTool(
            name="ls",
            result=ToolResult(text="entry", data={"entries": _RaisingList(["secret"])}),
        )
    )

    assert ended.is_error is False
    assert ended.summary is None


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("read", {"line_count": 10**5_000}),
        ("grep", {"count": 10**5_000}),
        ("find", {"count": -1}),
    ],
)
def test_executor_omits_out_of_range_summary_counts(name: str, data: Mapping[str, object]) -> None:
    ended = _run_executor(_ResultTool(name=name, result=ToolResult(text="result", data=data)))

    assert ended.is_error is False
    assert ended.summary is None


def test_executor_omits_out_of_range_exit_code() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(text="result", data={"exit_code": 10**5_000}),
        )
    )

    assert ended.is_error is False
    assert ended.exit_code is None


def test_executor_promotes_bash_process_metadata_and_round_trips() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Process p1 completed with exit code 0\nstdout:\nready\n",
                data={
                    "process_id": "p1",
                    "process_state": "completed",
                    "process_error": "",
                    "stdout": "ready\n",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "stdout_dropped_bytes": 0,
                    "stderr_dropped_bytes": 0,
                    "exit_code": 0,
                    "output_has_exit_status": False,
                    "process_started_at": _PROCESS_START,
                    "process_ended_at": _PROCESS_START + timedelta(minutes=4, seconds=12),
                },
            ),
        )
    )

    assert ended.is_error is False
    assert ended.process_id == "p1"
    assert ended.process_state == "completed"
    assert ended.process_error == ""
    assert ended.stdout == "ready\n"
    assert ended.stderr == ""
    assert ended.stdout_truncated is False
    assert ended.stderr_truncated is False
    assert ended.stdout_dropped_bytes == 0
    assert ended.stderr_dropped_bytes == 0
    assert ended.exit_code == 0
    assert ended.output_has_exit_status is False
    round_tripped = wisp_event_from_json(ended.model_dump_json())
    assert isinstance(round_tripped, ToolExecutionEnded)
    assert round_tripped.process_id == "p1"
    assert round_tripped.process_state == "completed"
    assert round_tripped.stdout == "ready\n"
    assert round_tripped.process_started_at == _PROCESS_START
    assert round_tripped.process_ended_at == _PROCESS_START + timedelta(minutes=4, seconds=12)


def test_executor_bounds_bash_process_output_metadata() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Process running",
                data={
                    "process_id": "bad\nid",
                    "process_state": "running",
                    "stdout": "alpha\nbeta\n",
                    "stderr": "e" * 100,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "stdout_dropped_bytes": 7,
                    "stderr_dropped_bytes": 11,
                },
            ),
        ),
        context=ToolContext(
            cwd=Path.cwd(),
            max_output_bytes=32,
            max_output_lines=1,
            protected_paths=(),
        ),
    )

    assert ended.is_error is False
    assert ended.process_id is None
    assert ended.process_state == "running"
    assert ended.stdout == "alpha\n[truncated]"
    assert ended.stderr == "eeeeeeeeeeeeeeeeeeee\n[truncated]"
    assert ended.stdout_truncated is True
    assert ended.stderr_truncated is True
    assert ended.stdout_dropped_bytes == 7
    assert ended.stderr_dropped_bytes == 11


def test_executor_promotes_one_shot_bash_truncation() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Command exited with code 0: x\n[truncated]",
                data={
                    "exit_code": 0,
                    "output_has_exit_status": True,
                    "stdout": "x\n[truncated]",
                    "stderr": "",
                    "stdout_truncated": True,
                    "stderr_truncated": False,
                    "stdout_dropped_bytes": 128,
                    "stderr_dropped_bytes": 0,
                },
                truncated=True,
            ),
        )
    )

    assert ended.process_id is None
    assert ended.process_state is None
    assert ended.stdout == "x\n[truncated]"
    assert ended.stderr == ""
    assert ended.stdout_truncated is True
    assert ended.stderr_truncated is False
    assert ended.stdout_dropped_bytes == 128
    assert ended.stderr_dropped_bytes == 0


def test_executor_ignores_malformed_bash_process_metadata() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Process running",
                data={
                    "process_id": "p1",
                    "process_state": ["running"],
                    "stdout_dropped_bytes": -1,
                    "stderr_dropped_bytes": "many",
                    # A naive time cannot be placed on the frontend's clock.
                    "process_started_at": datetime(2026, 1, 2, 3, 4, 5),
                    "process_ended_at": "2026-01-02T03:08:17Z",
                },
            ),
        )
    )

    assert ended.is_error is False
    assert ended.process_id == "p1"
    assert ended.process_state is None
    assert ended.stdout_dropped_bytes == 0
    assert ended.stderr_dropped_bytes == 0
    assert ended.process_started_at is None
    assert ended.process_ended_at is None


def test_executor_keeps_bash_exit_line_beyond_tiny_budget() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Command exited with code 2",
                data={
                    "exit_code": 2,
                    "output_has_exit_status": True,
                },
            ),
        ),
        context=ToolContext(
            cwd=Path.cwd(),
            max_output_bytes=1,
            max_output_lines=0,
            protected_paths=(),
        ),
    )

    assert ended.output == "Command exited with code 2"
    assert ended.exit_code == 2
    assert ended.output_has_exit_status is True


def test_executor_keeps_managed_header_beyond_tiny_budget() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Process p123 is still running\nstdout:\nchunk\n",
                data={
                    "process_id": "p123",
                    "process_state": "running",
                    "stdout": "chunk\n",
                    "stderr": "",
                    "output_has_exit_status": False,
                },
            ),
        ),
        context=ToolContext(
            cwd=Path.cwd(),
            max_output_bytes=1,
            max_output_lines=0,
            protected_paths=(),
        ),
    )

    assert ended.output.startswith("Process p123 is still running")
    assert "p123" in ended.output
    assert ended.truncated is True
    assert ended.process_id == "p123"
    assert ended.process_state == "running"


def test_executor_preserves_bash_managed_output_after_labels() -> None:
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text="Process p123 is still running\nstdout:\ntail\n",
                data={
                    "process_id": "p123",
                    "process_state": "running",
                    "stdout": "tail\n",
                    "stderr": "",
                    "output_has_exit_status": False,
                },
            ),
        ),
        context=ToolContext(
            cwd=Path.cwd(),
            max_output_bytes=5,
            max_output_lines=1,
            protected_paths=(),
        ),
    )

    assert ended.output == "Process p123 is still running\nstdout:\ntail\n"
    assert ended.process_id == "p123"
    assert ended.stdout == "tail\n"
    assert ended.truncated is False


def test_executor_keeps_managed_failure_beyond_tiny_budget() -> None:
    process_error = "Failed to terminate process tree"
    ended = _run_executor(
        _ResultTool(
            name="bash",
            result=ToolResult(
                text=f"Process p123 failed: {process_error}",
                data={
                    "process_id": "p123",
                    "process_state": "failed",
                    "process_error": process_error,
                    "stdout": "",
                    "stderr": "",
                    "output_has_exit_status": False,
                },
            ),
        ),
        context=ToolContext(
            cwd=Path.cwd(),
            max_output_bytes=1,
            max_output_lines=0,
            protected_paths=(),
        ),
    )

    assert ended.output == f"Process p123 failed: {process_error}"
    assert ended.process_error == process_error
    assert ended.truncated is False


def test_executor_propagates_wisp_result_processing_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_summary(
        name: str,
        data: Mapping[str, object],
        *,
        truncated: bool = False,
    ) -> str | None:
        del name, data, truncated
        raise RuntimeError("internal secret detail")

    monkeypatch.setattr(tool_execution, "summarize_tool_result", fail_summary)

    with pytest.raises(ToolResultProcessingError) as raised:
        _run_executor(_TruncatingTool(truncated=False))

    assert raised.value.call_id == "c1"
    assert raised.value.tool_name == "capped"
    assert str(raised.value) == "Internal error while processing a tool result"
    assert "secret" not in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_executor_propagates_wisp_output_normalization_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_truncation(text: str, *, max_bytes: int, max_lines: int) -> object:
        del text, max_bytes, max_lines
        raise RuntimeError("internal truncation failure")

    monkeypatch.setattr(tool_execution, "truncate_text", fail_truncation)

    with pytest.raises(ToolResultProcessingError) as raised:
        _run_executor(_TruncatingTool(truncated=False))

    assert str(raised.value) == "Internal error while processing a tool result"
    assert isinstance(raised.value.__cause__, RuntimeError)
