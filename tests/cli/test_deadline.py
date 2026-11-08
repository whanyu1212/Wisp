"""`--timeout` stops a one-shot run at a wall-clock deadline (`wisp.cli.deadline`)."""

# ruff: noqa: F403,F405

from __future__ import annotations

import os
import time

import pytest

from tests.support.cli import *
from wisp.cli.deadline import DEADLINE_EXIT_CODE, RunDeadline
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
)


class _Run:
    """A run that refuses `refusals` stop requests, then stops, or never stops."""

    def __init__(self, refusals: int | None) -> None:
        self.refusals = refusals
        self.requests = 0
        self.stopped = anyio.Event()

    def cancel_run(self) -> bool:
        self.requests += 1
        if self.refusals is None or self.requests <= self.refusals:
            return False
        self.stopped.set()
        return True


def _deadline(reports: list[str], *, grace_seconds: float = 5.0) -> RunDeadline:
    return RunDeadline(0.01, report=reports.append, grace_seconds=grace_seconds)


def test_stop_retried_until_accepted() -> None:
    reports: list[str] = []
    deadline = _deadline(reports)
    run = _Run(refusals=2)

    async def work() -> None:
        deadline.attach(run)
        await run.stopped.wait()
        raise RuntimeError("the run ended as cancelled")

    anyio.run(deadline.run, work)

    assert (deadline.stopped, deadline.forced) == (True, False)
    assert run.requests == 3
    assert reports == ["Run stopped: it reached its 0.01 s time limit (--timeout)."]


def test_unstopped_run_is_forced() -> None:
    reports: list[str] = []
    deadline = _deadline(reports, grace_seconds=0.05)
    run = _Run(refusals=None)
    cancelled = False

    async def work() -> None:
        nonlocal cancelled
        deadline.attach(run)
        try:
            await anyio.sleep_forever()
        finally:
            cancelled = True

    anyio.run(deadline.run, work)

    assert (deadline.stopped, deadline.forced, cancelled) == (True, True, True)
    assert run.requests >= 1


def test_finishing_run_not_stopped() -> None:
    reports: list[str] = []
    deadline = _deadline(reports)
    run = _Run(refusals=None)

    async def work() -> None:
        # Past its last turn, the run refuses the stop and finishes by itself.
        deadline.attach(run)
        while run.requests < 2:
            await anyio.sleep(0.01)

    anyio.run(deadline.run, work)

    assert (deadline.stopped, deadline.forced, reports) == (False, False, [])


def test_early_finish_keeps_errors() -> None:
    reports: list[str] = []
    deadline = RunDeadline(30.0, report=reports.append)

    async def work() -> None:
        raise ValueError("provider failed")

    with pytest.raises(ValueError, match="provider failed"):
        anyio.run(deadline.run, work)
    assert (deadline.stopped, reports) == (False, [])


def _timed_run(args: list[str], provider: str, tmp_path: Path) -> tuple[int, list[dict]]:
    result = CliRunner().invoke(
        app,
        ["--mode", "json", "--session-dir", str(tmp_path), *args],
        env={"WISP_PROVIDER": provider, "WISP_MODEL": ""},
    )
    return result.exit_code, _jsonl_records(result.stdout)


def _assert_stopped(records: list[dict]) -> None:
    types = [record["type"] for record in records]
    deadline_error = types.index("error")
    assert "time limit" in str(records[deadline_error]["message"])
    assert deadline_error < types.index("agent.completed")
    assert records[-1]["type"] == "agent.completed"
    assert records[-1]["outcome"] == "cancelled"


def test_stalled_provider_stops(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_cancellable_runtime)

    exit_code, records = _timed_run(
        ["-p", "slow", "--timeout", "0.2"], "cancellable-test", tmp_path
    )

    assert exit_code == DEADLINE_EXIT_CODE
    _assert_stopped(records)


def test_hung_tool_settles(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    async def hang(self: DangerTool, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    monkeypatch.setattr(DangerTool, "run", hang)
    monkeypatch.setattr(rpc_host_module, "build_runtime", build_tool_runtime)

    exit_code, records = _timed_run(
        ["-p", "use tool", "--allow-tool", "danger", "--yes", "--timeout", "0.2"],
        "tool-test",
        tmp_path,
    )

    assert exit_code == DEADLINE_EXIT_CODE
    _assert_stopped(records)
    types = [record["type"] for record in records]
    assert types.index("tool.execution.ended") < types.index("agent.completed")


class _SleepingBashProvider:
    """Run one `bash` command that records its PID and then sleeps."""

    name = "bash-test"
    default_model: str | None = "bash-test"

    def __init__(self, pid_path: Path) -> None:
        self.pid_path = pid_path

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderResponseStarted(model=self.name)
        tool_call = ToolCall(
            call_id="call-1",
            name="bash",
            arguments={"command": f"echo $$ > {self.pid_path}; exec sleep 30"},
            response_id="response-1",
        )
        yield ProviderToolCallCompleted(tool_call=tool_call)
        yield ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-1",
            finish_reason="tool_calls",
        )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.process
def test_command_process_ends(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    pid_path = tmp_path / "sleep.pid"
    build_runtime = rpc_host_module.build_runtime

    async def build_with_bash_provider(**kwargs: object) -> WispRuntime:
        runtime = await build_runtime(**kwargs)
        runtime.providers.register(_SleepingBashProvider(pid_path))
        return runtime

    monkeypatch.setattr(rpc_host_module, "build_runtime", build_with_bash_provider)

    exit_code, records = _timed_run(
        ["-p", "sleep", "--allow-tool", "bash", "--yes", "--timeout", "1.5"],
        "bash-test",
        tmp_path / "sessions",
    )

    assert exit_code == DEADLINE_EXIT_CODE
    _assert_stopped(records)
    pid = int(pid_path.read_text())
    give_up = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < give_up:
        time.sleep(0.02)
    assert not _alive(pid)


def test_finished_run_ignores_deadline(tmp_path: Path) -> None:
    exit_code, records = _timed_run(["-p", "hello", "--timeout", "30"], "fake", tmp_path)

    assert exit_code == 0
    assert records[-1]["outcome"] == "completed"
    assert "error" not in [record["type"] for record in records]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--mode", "tui", "--timeout", "5"], "--timeout applies only to --prompt runs"),
        (["-p", "hello", "--timeout", "0"], "--timeout must be positive"),
    ],
)
def test_bad_timeout_rejected(args: list[str], message: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [*args, "--session-dir", str(tmp_path)],
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1
    assert message in result.output
