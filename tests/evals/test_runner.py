"""Running eval tasks (`evals.tool_use.runner`)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest

from evals.tool_use.credentials import Credentials
from evals.tool_use.metrics import RunMetrics
from evals.tool_use.runner import (
    Budget,
    Build,
    ModelSpec,
    RunContext,
    RunRow,
    _mcp_setup_problem,
    classify,
    command_environment,
    detect_build,
    evaluate_checks,
    mcp_settings,
    run_environment,
    run_one,
    run_suite,
)
from evals.tool_use.task_definitions import Task, TaskChecks, load_task
from wisp.events import RunOutcome, RunSetup

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_model_spec_parses() -> None:
    assert ModelSpec.parse("openai::gpt-6.1-sol") == ModelSpec("openai", "gpt-6.1-sol")
    with pytest.raises(ValueError, match="provider::model"):
        ModelSpec.parse("gpt-6.1-sol")


def test_environment_passes_only_allowlist(tmp_path: Path) -> None:
    credentials = Credentials(
        auth_file=tmp_path / "auth.json", environment={"OPENAI_API_KEY": "sk-run"}
    )
    base = {
        "PATH": "/bin",
        "LC_ALL": "C",
        "HOME": "/u",
        "WISP_MODEL": "other",
        "OPENAI_API_KEY": "sk-shell",
        "OPENAI_BASE_URL": "https://elsewhere",
        "GITHUB_TOKEN": "ghp",
        "XDG_CONFIG_HOME": "/u/.config",
    }

    environment = run_environment(tmp_path / "home", credentials, base)

    assert environment == {
        "PATH": "/bin",
        "LC_ALL": "C",
        "OPENAI_API_KEY": "sk-run",
        "HOME": str(tmp_path / "home"),
        "WISP_TRUST": "1",
        "WISP_AUTH_FILE": str(tmp_path / "auth.json"),
    }
    assert command_environment(tmp_path / "home", base) == {
        "PATH": "/bin",
        "LC_ALL": "C",
        "HOME": str(tmp_path / "home"),
    }


def _metrics(outcome: RunOutcome | None, *errors: str, overflow: bool = False) -> RunMetrics:
    return RunMetrics(outcome=outcome, errors=list(errors), context_overflowed=overflow)


@pytest.mark.parametrize(
    ("exit_code", "metrics", "verify_exit", "failed_checks", "status"),
    [
        (0, _metrics("completed"), 0, [], "passed"),
        (0, _metrics("completed"), 1, [], "failed"),
        (0, _metrics("completed"), 0, ["never called read_output"], "failed"),
        (124, _metrics("cancelled", "Agent run cancelled"), 0, [], "failed"),
        (124, _metrics("failed", "Provider error"), 0, [], "failed"),
        (124, _metrics(None), 0, [], "failed"),
        (1, _metrics("failed", "Maximum tool iterations exceeded: 30"), 0, [], "failed"),
        (1, _metrics("failed", "Context window exceeded", overflow=True), 0, [], "failed"),
        (1, _metrics("failed", "429 Too Many Requests"), 0, [], "errored"),
        (1, _metrics("completed"), 0, [], "errored"),
        (1, _metrics(None, "auth failed"), 0, [], "errored"),
    ],
    ids=[
        "passed",
        "verify-failed",
        "check-failed",
        "deadline",
        "deadline-after-error",
        "deadline-forced",
        "iteration-limit",
        "context-overflow",
        "provider-error",
        "crash-after-success",
        "crash",
    ],
)
def test_classifies_runs(
    exit_code: int,
    metrics: RunMetrics,
    verify_exit: int,
    failed_checks: list[str],
    status: str,
) -> None:
    assert (
        classify(
            exit_code=exit_code,
            runner_error=None,
            metrics=metrics,
            verify_exit=verify_exit,
            failed_checks=failed_checks,
        )
        == status
    )


def test_checks_report_breaks() -> None:
    metrics = RunMetrics(commands=["make build", "make build"])
    metrics.tool_errors["timeout"] = 3
    checks = TaskChecks(no_rerun="make", uses_tool="read_output", max_tool_errors=2)

    assert evaluate_checks(checks, metrics) == [
        "reran a command containing 'make'",
        "never called read_output",
        "3 tool errors, more than 2",
    ]
    assert evaluate_checks(TaskChecks(), metrics) == []


@pytest.mark.parametrize(
    ("commands", "broken"),
    [
        (["bash report.sh", "bash report.sh 2>&1 | tail -n 5"], True),
        (["bash report.sh", "cat report.sh", "bash check.sh"], False),
    ],
    ids=["wrapped-rerun", "single-run"],
)
def test_no_rerun_counts_wrapped(commands: list[str], broken: bool) -> None:
    failed = evaluate_checks(TaskChecks(no_rerun="bash report.sh"), RunMetrics(commands=commands))

    assert bool(failed) is broken


def _context(tmp_path: Path, *, build: Build | None = None) -> RunContext:
    return RunContext(
        build=build or Build(wisp=Path("wisp"), version="wisp 0", git_commit="abc"),
        credentials=Credentials(auth_file=tmp_path / "auth.json", sources={"fake": "none"}),
        results_dir=tmp_path / "results",
    )


def _task(tmp_path: Path, verify: str = "true", *, extra: str = "") -> Task:
    folder = tmp_path / "tasks" / "demo"
    (folder / "repo").mkdir(parents=True)
    (folder / "repo" / "notes.txt").write_text("hello\n", encoding="utf-8")
    (folder / "task.toml").write_text(
        f'id = "demo"\ntitle = "Demo"\ntags = ["general"]\nprompt = "hi"\n'
        f'verify = "{verify}"\ntools = ["read"]\ntimeout_seconds = 60\n{extra}',
        encoding="utf-8",
    )
    return load_task(folder)


def test_budget_stops_between_rounds(tmp_path: Path) -> None:
    started: list[int] = []

    def execute(task: Task, model: ModelSpec, rep: int, context: RunContext) -> RunRow:
        started.append(rep)
        return _row(task.id, model, rep, usd="4")

    tasks = [_task(tmp_path)]
    models = [ModelSpec("openai", "a"), ModelSpec("openai", "b")]

    result = run_suite(
        tasks,
        models,
        5,
        _context(tmp_path),
        budget=Budget(max_usd=Decimal("10")),
        execute=execute,
    )

    # Round 1 spends 8 of 10, so round 2 starts; it spends 16, so round 3 does not.
    assert sorted(started) == [1, 1, 2, 2]
    assert (result.stopped_for_budget, result.skipped_runs) == (True, 6)


def test_broken_run_recorded(tmp_path: Path) -> None:
    def execute(task: Task, model: ModelSpec, rep: int, context: RunContext) -> RunRow:
        if model.model == "a":
            raise OSError("disk full")
        return _row(task.id, model, rep, usd="0")

    rows: list[RunRow] = []
    models = [ModelSpec("openai", "a"), ModelSpec("openai", "b")]

    result = run_suite(
        [_task(tmp_path)], models, 1, _context(tmp_path), execute=execute, on_row=rows.append
    )

    assert sorted((row.model, row.status) for row in result.rows) == [
        ("a", "errored"),
        ("b", "passed"),
    ]
    assert rows == result.rows
    broken = next(row for row in rows if row.model == "a")
    assert broken.error == "runner failed: OSError: disk full"


def _row(task: str, model: ModelSpec, rep: int, *, usd: str) -> RunRow:
    return RunRow(
        task=task,
        provider=model.provider,
        model=model.model,
        rep=rep,
        status="passed",
        timed_out=False,
        exit_code=0,
        verify_exit=0,
        failed_checks=[],
        seconds=1.0,
        outcome="completed",
        turns=1,
        tool_calls={},
        tool_errors={},
        reruns=0,
        used_read_output=False,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        known_usd=usd,
        unpriced_responses=0,
        retries=0,
        error=None,
        tools_sha256=None,
        instructions_sha256=None,
        credential_source="environment",
        wisp_version="wisp 0",
        git_commit="abc",
        run_dir="x",
    )


@pytest.mark.process
@pytest.mark.parametrize(
    ("verify", "status"),
    [("grep -q hello notes.txt", "passed"), ("grep -q missing notes.txt", "failed")],
)
def test_fake_run_scored(tmp_path: Path, verify: str, status: str) -> None:
    context = _context(tmp_path, build=detect_build(_REPO_ROOT))

    row = run_one(_task(tmp_path, verify), ModelSpec("fake", "fake"), 1, context)

    assert (row.status, row.exit_code, row.outcome, row.verify_exit == 0) == (
        status,
        0,
        "completed",
        status == "passed",
    )
    assert row.tools_sha256 is not None and len(row.tools_sha256) == 64
    assert row.credential_source == "none"
    assert (context.results_dir / row.run_dir / "events.jsonl").stat().st_size > 0
    assert (context.results_dir / row.run_dir / "verify.txt").exists()


@pytest.mark.process
def test_setup_failure_errors(tmp_path: Path) -> None:
    task = _task(tmp_path)
    toml = task.folder / "task.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + 'setup = "exit 3"\n', encoding="utf-8")

    row = run_one(load_task(task.folder), ModelSpec("fake", "fake"), 1, _context(tmp_path))

    assert (row.status, row.error) == ("errored", "setup exited with 3")


@pytest.mark.process
def test_relative_results_keep_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    context = RunContext(
        build=detect_build(_REPO_ROOT),
        credentials=Credentials(auth_file=tmp_path / "auth.json", sources={"fake": "none"}),
        results_dir=Path("results"),
    )

    row = run_one(_task(tmp_path), ModelSpec("fake", "fake"), 1, context)

    assert list((tmp_path / "results" / row.run_dir / "sessions").glob("*.jsonl"))


@pytest.mark.process
def test_mcp_tools_offered(tmp_path: Path) -> None:
    task = _task(tmp_path, extra='mcp_servers = ["billing"]\n')
    context = _context(tmp_path, build=detect_build(_REPO_ROOT))

    row = run_one(task, ModelSpec("fake", "fake"), 1, context)

    assert (row.status, row.error) == ("passed", None)


@pytest.mark.process
def test_missing_mcp_tools_error(tmp_path: Path) -> None:
    task = _task(tmp_path, extra='mcp_servers = ["billing"]\n')
    context = dataclasses.replace(
        _context(tmp_path, build=detect_build(_REPO_ROOT)), python=str(tmp_path / "no-python")
    )

    row = run_one(task, ModelSpec("fake", "fake"), 1, context)

    assert row.status == "errored"
    # Wisp's own startup error names the server, in either exposure mode.
    assert row.error is not None and row.error.startswith("MCP server billing is unavailable")


@pytest.mark.process
def test_on_demand_run_offered_discovery(tmp_path: Path) -> None:
    task = _task(tmp_path, extra='mcp_servers = ["billing"]\n')
    context = dataclasses.replace(
        _context(tmp_path, build=detect_build(_REPO_ROOT)), mcp_tool_exposure="on_demand"
    )

    row = run_one(task, ModelSpec("fake", "fake"), 1, context)

    assert (row.status, row.error, row.mcp_tool_exposure) == ("passed", None, "on_demand")


def test_mcp_settings_carry_exposure(tmp_path: Path) -> None:
    task = _task(tmp_path, extra='mcp_servers = ["billing"]\n')

    settings = mcp_settings(task, "python", "on_demand")

    assert settings["mcp_tool_exposure"] == "on_demand"
    assert list(settings["mcp_servers"]) == ["billing"]  # type: ignore[call-overload]


_BILLING_TOOLS = ("mcp__billing__get_invoice", "mcp__billing__get_invoice_customer")
_DISCOVERY = ("mcp_search", "mcp_describe", "mcp_call")
_UNAVAILABLE = "MCP server billing is unavailable: startup or tool discovery timed out"


@pytest.mark.parametrize(
    ("exposure", "offered", "errors", "problem"),
    [
        ("direct", ("read", *_BILLING_TOOLS), (), None),
        ("direct", ("read", _BILLING_TOOLS[0]), (), "not offered 1 MCP tools"),
        ("on_demand", ("read", *_DISCOVERY), (), None),
        ("on_demand", ("read",), (), "not offered 3 MCP tools, such as mcp_search"),
        ("on_demand", ("read", *_DISCOVERY), (_UNAVAILABLE,), "MCP server billing is unavailable"),
    ],
    ids=["direct-ok", "direct-missing", "on-demand-ok", "on-demand-missing", "server-failed"],
)  # fmt: skip
def test_mcp_setup_problem(
    tmp_path: Path,
    exposure: str,
    offered: tuple[str, ...],
    errors: tuple[str, ...],
    problem: str | None,
) -> None:
    task = _task(tmp_path, extra='mcp_servers = ["billing"]\n')
    metrics = RunMetrics(
        setup=RunSetup(tool_names=offered, tools_sha256="0" * 64, instructions_sha256="1" * 64),
        errors=list(errors),
    )

    found = _mcp_setup_problem(task, metrics, exposure)  # type: ignore[arg-type]

    assert found is None if problem is None else problem in (found or "")
