"""Run eval tasks through `wisp -p --mode json` and score each run.

One run copies a task's fixture into a fresh folder, commits it, runs Wisp on the
task's prompt with a private `HOME`, then runs the task's verifier there. Runs
happen in rounds, one repetition of every task and model per round, so stopping
for the budget leaves every pairing with the same number of runs.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from evals.tool_use.credentials import PROVIDER_KEY_VARIABLES, Credentials
from evals.tool_use.metrics import RunMetrics, measure, parse_events
from evals.tool_use.task_definitions import Task, TaskChecks

type RunStatus = Literal["passed", "failed", "errored"]

DEADLINE_EXIT_CODE = 124
# Wisp stops a run at its own --timeout, allows 10 s to wind down and 10 s more
# before exiting outright; the runner kills it only after that and a margin.
_KILL_MARGIN_SECONDS = 60
_VERIFY_TIMEOUT_SECONDS = 120
_GIT_IDENTITY = ("-c", "user.name=wisp-eval", "-c", "user.email=wisp-eval@example.invalid")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A provider and model, written `provider::model`."""

    provider: str
    model: str

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        provider, separator, model = text.partition("::")
        if not separator or not provider or not model:
            raise ValueError(f"model {text!r} must be written provider::model")
        return cls(provider=provider, model=model)

    @property
    def label(self) -> str:
        return f"{self.provider}::{self.model}"


@dataclass(frozen=True, slots=True)
class Build:
    """The Wisp under test, recorded in every row so results tie to code.

    Attributes:
        wisp (Path): The `wisp` executable runs use.
        version (str): What `wisp --version` printed.
        git_commit (str): The checkout's commit, suffixed `-dirty` when `src/`
            has uncommitted changes.
    """

    wisp: Path
    version: str
    git_commit: str


@dataclass(frozen=True, slots=True)
class Budget:
    """When to stop starting new rounds.

    Attributes:
        max_usd (Decimal | None): Stop once priced runs have cost this much.
        max_unpriced_tokens (int | None): Stop once runs without prices, such as
            Codex subscription runs, have used this many tokens.
    """

    max_usd: Decimal | None = None
    max_unpriced_tokens: int | None = None


UNLIMITED = Budget()


@dataclass(frozen=True, slots=True)
class RunContext:
    """What every run in a suite shares."""

    build: Build
    credentials: Credentials
    results_dir: Path
    python: str = sys.executable


@dataclass(slots=True)
class RunRow:
    """One run's result, written as a line of `runs.jsonl`.

    `status` is `errored` when the run says nothing about the task: Wisp
    crashed, could not sign in, failed on a provider error, or setup failed.
    Reports leave errored runs out of pass rates and count them separately.
    """

    task: str
    provider: str
    model: str
    rep: int
    status: RunStatus
    timed_out: bool
    exit_code: int | None
    verify_exit: int | None
    failed_checks: list[str]
    seconds: float
    outcome: str | None
    turns: int | None
    tool_calls: dict[str, int]
    tool_errors: dict[str, int]
    reruns: int
    used_read_output: bool
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    known_usd: str
    unpriced_responses: int
    retries: int
    error: str | None
    tools_sha256: str | None
    instructions_sha256: str | None
    credential_source: str | None
    wisp_version: str
    git_commit: str
    run_dir: str


@dataclass(slots=True)
class SuiteResult:
    """The rows a suite produced and whether the budget cut it short."""

    rows: list[RunRow] = field(default_factory=list)
    stopped_for_budget: bool = False
    skipped_runs: int = 0


def detect_build(repo_root: Path) -> Build:
    """Find the `wisp` next to the running interpreter and describe it.

    The runner is started with `uv run`, so this is the checkout's own `wisp`,
    not one installed elsewhere on `PATH`.
    """

    wisp = Path(sys.executable).parent / "wisp"
    if not wisp.is_file():
        raise RuntimeError(f"no wisp executable next to {sys.executable}; run with `uv run`")
    version = _output([str(wisp), "--version"], cwd=repo_root)
    commit = _output(["git", "rev-parse", "HEAD"], cwd=repo_root)
    dirty = _output(["git", "status", "--porcelain", "--", "src"], cwd=repo_root)
    return Build(wisp=wisp, version=version, git_commit=commit + ("-dirty" if dirty else ""))


def run_environment(
    home: Path, credentials: Credentials, base: Mapping[str, str]
) -> dict[str, str]:
    """Build a run's environment: no `WISP_*` settings and no stray provider keys.

    Args:
        home (Path): The run's private `HOME`.
        credentials (Credentials): The auth file and provider keys to pass.
        base (Mapping[str, str]): The runner's environment, for `PATH` and the like.

    Returns:
        dict[str, str]: The environment Wisp runs with.
    """

    key_names = set(PROVIDER_KEY_VARIABLES.values())
    environment = {
        name: value
        for name, value in base.items()
        if not name.startswith("WISP_") and name not in key_names
    }
    environment.update(credentials.environment)
    environment["HOME"] = str(home)
    environment["WISP_TRUST"] = "1"
    environment["WISP_AUTH_FILE"] = str(credentials.auth_file)
    return environment


def run_one(task: Task, model: ModelSpec, rep: int, context: RunContext) -> RunRow:
    """Run one task once and score it."""

    run_dir = context.results_dir / task.id / f"{model.provider}__{model.model}" / f"rep{rep}"
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wisp-eval-") as scratch:
        work = Path(scratch) / "work"
        home = Path(scratch) / "home"
        home.mkdir()
        shutil.copytree(task.fixture, work)
        setup_error = _prepare(task, work, context.python)
        if setup_error is not None:
            return _row(
                task, model, rep, context, run_dir, started, status="errored", error=setup_error
            )
        exit_code, wisp_error = _run_wisp(task, model, work, home, run_dir, context)
        events_text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        try:
            metrics = measure(parse_events(events_text.splitlines()))
        except ValueError as err:
            return _row(
                task,
                model,
                rep,
                context,
                run_dir,
                started,
                status="errored",
                exit_code=exit_code,
                error=f"unreadable events: {err}",
            )
        verify_exit = _run_command(
            task.definition.verify, work, context.python, task.folder, _VERIFY_TIMEOUT_SECONDS
        )
    failed_checks = evaluate_checks(task.definition.checks, metrics)
    timed_out = exit_code == DEADLINE_EXIT_CODE
    if (
        wisp_error is not None
        or (metrics.outcome is None and not timed_out)
        or (metrics.outcome == "failed")
    ):
        status: RunStatus = "errored"
    elif timed_out or verify_exit != 0 or failed_checks:
        status = "failed"
    else:
        status = "passed"
    return _row(
        task,
        model,
        rep,
        context,
        run_dir,
        started,
        status=status,
        exit_code=exit_code,
        verify_exit=verify_exit,
        failed_checks=failed_checks,
        metrics=metrics,
        timed_out=timed_out,
        error=wisp_error or metrics.last_error,
    )


def evaluate_checks(checks: TaskChecks, metrics: RunMetrics) -> list[str]:
    """Return a description of each event check the run broke."""

    failed: list[str] = []
    if checks.no_rerun is not None and metrics.reruns(checks.no_rerun):
        failed.append(f"reran a command containing {checks.no_rerun!r}")
    if checks.uses_tool is not None and not metrics.tool_calls[checks.uses_tool]:
        failed.append(f"never called {checks.uses_tool}")
    errors = sum(metrics.tool_errors.values())
    if checks.max_tool_errors is not None and errors > checks.max_tool_errors:
        failed.append(f"{errors} tool errors, more than {checks.max_tool_errors}")
    return failed


def run_suite(
    tasks: Sequence[Task],
    models: Sequence[ModelSpec],
    reps: int,
    context: RunContext,
    *,
    workers: int = 2,
    budget: Budget = UNLIMITED,
    execute: Callable[[Task, ModelSpec, int, RunContext], RunRow] = run_one,
    on_row: Callable[[RunRow], None] = lambda _row: None,
) -> SuiteResult:
    """Run every task with every model ``reps`` times, round by round.

    A round is one repetition of every task and model. The budget is checked
    before each round starts, so a stopped suite has whole rounds only.

    Args:
        tasks (Sequence[Task]): Tasks to run.
        models (Sequence[ModelSpec]): Models to run them with.
        reps (int): Repetitions of each task and model.
        context (RunContext): What every run shares.
        workers (int): Runs in flight at once.
        budget (Budget): When to stop starting rounds.
        execute (Callable): Runs one task once; tests replace it.
        on_row (Callable[[RunRow], None]): Called with each row as it finishes.

    Returns:
        SuiteResult: The rows, and whether and how much the budget cut short.
    """

    result = SuiteResult()
    pairs = [(task, model) for task in tasks for model in models]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rep in range(1, reps + 1):
            if _over_budget(result.rows, budget):
                result.stopped_for_budget = True
                result.skipped_runs = len(pairs) * (reps - rep + 1)
                break
            futures: set[Future[RunRow]] = {
                pool.submit(execute, task, model, rep, context) for task, model in pairs
            }
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    row = future.result()
                    result.rows.append(row)
                    on_row(row)
    return result


def write_row(path: Path, row: RunRow) -> None:
    """Append one row to a `runs.jsonl` file."""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def _over_budget(rows: Sequence[RunRow], budget: Budget) -> bool:
    usd = sum((Decimal(row.known_usd) for row in rows), Decimal(0))
    unpriced = sum(row.input_tokens + row.output_tokens for row in rows if row.unpriced_responses)
    return (budget.max_usd is not None and usd >= budget.max_usd) or (
        budget.max_unpriced_tokens is not None and unpriced >= budget.max_unpriced_tokens
    )


def _prepare(task: Task, work: Path, python: str) -> str | None:
    """Commit the fixture and run the task's setup; return an error, if any."""

    for command in (
        ["git", "init", "-q"],
        ["git", *_GIT_IDENTITY, "add", "-A"],
        ["git", *_GIT_IDENTITY, "commit", "-q", "-m", "fixture"],
    ):
        completed = subprocess.run(command, cwd=work, capture_output=True, check=False)
        if completed.returncode != 0:
            return (
                f"fixture {' '.join(command)} failed: {completed.stderr.decode(errors='replace')}"
            )
    if task.definition.setup is not None:
        exit_code = _run_command(
            task.definition.setup, work, python, task.folder, _VERIFY_TIMEOUT_SECONDS
        )
        if exit_code != 0:
            return f"setup exited with {exit_code}"
    return None


def _run_wisp(
    task: Task, model: ModelSpec, work: Path, home: Path, run_dir: Path, context: RunContext
) -> tuple[int | None, str | None]:
    """Run Wisp on the task; return its exit code and a runner-side error, if any."""

    definition = task.definition
    command = [
        str(context.build.wisp),
        "-p",
        definition.prompt,
        "--mode",
        "json",
        "--provider",
        model.provider,
        "--model",
        model.model,
        "--yes",
        "--timeout",
        f"{definition.timeout_seconds:g}",
        "--max-tool-iterations",
        str(definition.max_tool_iterations),
        "--session-dir",
        str(run_dir / "sessions"),
    ]
    for tool in definition.tools:
        command += ["--allow-tool", tool]
    environment = run_environment(home, context.credentials, os.environ)
    with (
        (run_dir / "events.jsonl").open("w", encoding="utf-8") as events,
        (run_dir / "stderr.txt").open("w", encoding="utf-8") as stderr,
    ):
        try:
            completed = subprocess.run(
                command,
                cwd=work,
                env=environment,
                stdout=events,
                stderr=stderr,
                timeout=definition.timeout_seconds + _KILL_MARGIN_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, "wisp did not exit after its --timeout; the runner killed it"
    return completed.returncode, None


def _run_command(
    command: str, work: Path, python: str, task_dir: Path, timeout: float
) -> int | None:
    """Run a task command with `bash -c`; return its exit status, or None on timeout."""

    script = command.replace("{python}", shlex.quote(python)).replace(
        "{task}", shlex.quote(str(task_dir))
    )
    try:
        completed = subprocess.run(
            ["bash", "-c", script], cwd=work, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    return completed.returncode


def _output(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _row(
    task: Task,
    model: ModelSpec,
    rep: int,
    context: RunContext,
    run_dir: Path,
    started: float,
    *,
    status: RunStatus,
    exit_code: int | None = None,
    verify_exit: int | None = None,
    failed_checks: list[str] | None = None,
    metrics: RunMetrics | None = None,
    timed_out: bool = False,
    error: str | None = None,
) -> RunRow:
    measured = metrics or RunMetrics()
    setup = measured.setup
    return RunRow(
        task=task.id,
        provider=model.provider,
        model=model.model,
        rep=rep,
        status=status,
        timed_out=timed_out,
        exit_code=exit_code,
        verify_exit=verify_exit,
        failed_checks=failed_checks or [],
        seconds=round(time.monotonic() - started, 3),
        outcome=measured.outcome,
        turns=measured.turns,
        tool_calls=dict(measured.tool_calls),
        tool_errors=dict(measured.tool_errors),
        reruns=measured.reruns(),
        used_read_output=measured.tool_calls["read_output"] > 0,
        input_tokens=measured.input_tokens,
        output_tokens=measured.output_tokens,
        cache_read_tokens=measured.cache_read_tokens,
        known_usd=str(measured.known_usd),
        unpriced_responses=measured.unpriced_responses,
        retries=measured.retries,
        error=error,
        tools_sha256=setup.tools_sha256 if setup else None,
        instructions_sha256=setup.instructions_sha256 if setup else None,
        credential_source=context.credentials.sources.get(model.provider),
        wisp_version=context.build.version,
        git_commit=context.build.git_commit,
        run_dir=str(run_dir.relative_to(context.results_dir)),
    )
