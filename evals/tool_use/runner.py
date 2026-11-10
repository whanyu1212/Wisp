"""Run eval tasks through `wisp -p --mode json` and score each run.

One run copies a task's fixture into a fresh folder, commits it, runs Wisp on the
task's prompt with a private `HOME`, then runs the task's verifier there. Runs
happen in rounds, one repetition of every task and model per round, so stopping
for the budget leaves every pairing with the same number of runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import IO, Literal

from evals.tool_use.credentials import REPO_ROOT, Credentials
from evals.tool_use.metrics import RunMetrics, measure, parse_events
from evals.tool_use.servers.catalogs import CATALOGS
from evals.tool_use.task_definitions import Task, TaskChecks
from wisp.mcp.config import McpToolExposure
from wisp.mcp.discovery import DISCOVERY_TOOL_NAMES
from wisp.mcp.tool import mcp_tool_name

# Whether runs offer the experimental codemode tool, and in which language.
type Codemode = Literal["off", "python"]
CODEMODES: tuple[Codemode, ...] = ("off", "python")
type RunStatus = Literal["passed", "failed", "errored"]

DEADLINE_EXIT_CODE = 124
# Wisp stops a run at its own --timeout, allows 10 s to wind down and 10 s more
# before exiting outright; the runner kills it only after that and a margin.
_KILL_MARGIN_SECONDS = 60
_VERIFY_TIMEOUT_SECONDS = 120
_GIT_OPTIONS = (
    "-c",
    "user.name=wisp-eval",
    "-c",
    "user.email=wisp-eval@example.invalid",
    "-c",
    "commit.gpgsign=false",
)
# The only runner variables a run inherits. Everything else, including tokens for
# other services and settings such as OPENAI_BASE_URL, stays out of the commands
# the model runs and out of the setup under test.
_PASSTHROUGH_VARIABLES = frozenset(
    {
        "PATH",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TZ",
        "USER",
        "LOGNAME",
        "SHELL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
# Wisp reports an MCP server that failed to start as an error event like this.
_MCP_UNAVAILABLE = re.compile(r"^MCP server \S+ is unavailable")
# Wisp reports reaching --max-tool-iterations only as an error with this text.
_ITERATION_LIMIT_ERROR = "Maximum tool iterations exceeded"


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
    """What every run in a suite shares.

    Attributes:
        build (Build): The Wisp under test.
        credentials (Credentials): How runs sign in.
        results_dir (Path): Where rows and run folders go.
        python (str): The interpreter for task commands and synthetic servers.
        mcp_tool_exposure (McpToolExposure): How MCP tasks' tools reach the
            model: every definition (`direct`), found on demand (`on_demand`), or
            either by catalog size (`auto`, Wisp's default).
        codemode (Codemode): Whether runs offer the codemode tool (`python`) or
            not (`off`).
    """

    build: Build
    credentials: Credentials
    results_dir: Path
    python: str = sys.executable
    mcp_tool_exposure: McpToolExposure = "direct"
    codemode: Codemode = "off"


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
    mcp_tool_exposure: McpToolExposure = "direct"
    proxied_calls: int = 0
    proxied_errors: int = 0
    proxied_argument_errors: int = 0
    codemode: Codemode = "off"
    scripts: int = 0
    failed_scripts: int = 0
    nested_calls: int = 0


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


def command_environment(home: Path, base: Mapping[str, str]) -> dict[str, str]:
    """Build the environment for a run's setup and verify commands.

    Only allowlisted variables pass from the runner, and `HOME` is the run's
    private folder, so nothing reads the user's own configuration.
    """

    environment = {
        name: value
        for name, value in base.items()
        if name in _PASSTHROUGH_VARIABLES or name.startswith("LC_")
    }
    environment["HOME"] = str(home)
    return environment


def run_environment(
    home: Path, credentials: Credentials, base: Mapping[str, str]
) -> dict[str, str]:
    """Build Wisp's environment: the command environment plus credentials.

    Args:
        home (Path): The run's private `HOME`.
        credentials (Credentials): The auth file and provider keys to pass.
        base (Mapping[str, str]): The runner's environment, for `PATH` and the like.

    Returns:
        dict[str, str]: The environment Wisp runs with.
    """

    environment = command_environment(home, base)
    environment.update(credentials.environment)
    environment["WISP_TRUST"] = "1"
    environment["WISP_AUTH_FILE"] = str(credentials.auth_file)
    return environment


def mcp_settings(
    task: Task, python: str, exposure: McpToolExposure = "direct"
) -> dict[str, object]:
    """Return the user settings that start a task's synthetic MCP servers.

    MCP servers and their exposure are user-only settings, so they go in the run's
    private `HOME`. Every catalog tool is marked read-only, so no run waits for an
    approval.

    Args:
        task (Task): The task whose `mcp_servers` to start.
        python (str): The interpreter that runs each server; it needs the MCP SDK.
        exposure (McpToolExposure): How the servers' tools reach the model.

    Returns:
        dict[str, object]: The contents of `~/.wisp/settings.json`.
    """

    return {
        "mcp_tool_exposure": exposure,
        "mcp_servers": {
            name: {
                "command": python,
                "args": ["-m", "evals.tool_use.servers.serve", name],
                "env": {"PYTHONPATH": str(REPO_ROOT)},
                "tool_safety": dict.fromkeys(CATALOGS[name].tool_names, "read"),
            }
            for name in task.definition.mcp_servers
        },
    }


def mcp_tool_names(task: Task) -> tuple[str, ...]:
    """Return the Wisp names of every tool a task's MCP servers offer."""

    return tuple(
        mcp_tool_name(server, tool)
        for server in task.definition.mcp_servers
        for tool in CATALOGS[server].tool_names
    )


def classify(
    *,
    exit_code: int | None,
    runner_error: str | None,
    metrics: RunMetrics,
    verify_exit: int | None,
    failed_checks: Sequence[str],
) -> RunStatus:
    """Decide whether a run passed, failed the task, or says nothing about it.

    A deadline is a task failure whatever outcome the stopped run reports. A run
    that fails because the model hit its tool-iteration limit or outgrew its
    context is a task failure too. Any other failure, a crash, or a run that
    exits badly after reporting success, is `errored`.
    """

    if runner_error is not None:
        return "errored"
    if exit_code == DEADLINE_EXIT_CODE:
        return "failed"
    if metrics.outcome == "failed":
        hit_limit = metrics.context_overflowed or any(
            error.startswith(_ITERATION_LIMIT_ERROR) for error in metrics.errors
        )
        return "failed" if hit_limit else "errored"
    if metrics.outcome != "completed" or exit_code != 0:
        return "errored"
    if verify_exit != 0 or failed_checks:
        return "failed"
    return "passed"


def run_one(task: Task, model: ModelSpec, rep: int, context: RunContext) -> RunRow:
    """Run one task once and score it."""

    run_dir = _run_dir(context, task, model, rep)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wisp-eval-") as scratch:
        work = Path(scratch) / "work"
        home = Path(scratch) / "home"
        home.mkdir()
        shutil.copytree(task.fixture, work)
        commands = command_environment(home, os.environ)
        setup_error = _prepare(task, work, run_dir, commands, context.python)
        if setup_error is not None:
            return _row(
                task, model, rep, context, run_dir, started, status="errored", error=setup_error
            )
        exit_code, runner_error = _run_wisp(task, model, work, home, run_dir, context)
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
        # A server that failed to start drops its tools; that says nothing about the model.
        # Without `agent.started` Wisp failed earlier, and its own error explains why.
        if metrics.setup is not None and runner_error is None:
            runner_error = _mcp_setup_problem(task, metrics, context.mcp_tool_exposure)
        verify_exit = _run_command(
            task.definition.verify,
            work,
            commands,
            context.python,
            task.folder,
            run_dir / "verify.txt",
        )
    failed_checks = evaluate_checks(task.definition.checks, metrics)
    status = classify(
        exit_code=exit_code,
        runner_error=runner_error,
        metrics=metrics,
        verify_exit=verify_exit,
        failed_checks=failed_checks,
    )
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
        timed_out=exit_code == DEADLINE_EXIT_CODE,
        error=runner_error or metrics.last_error,
    )


def evaluate_checks(checks: TaskChecks, metrics: RunMetrics) -> list[str]:
    """Return a description of each event check the run broke."""

    failed: list[str] = []
    # Any second command containing the text counts, also one wrapped differently,
    # such as `bash report.sh | tail`: that is still a second costly run.
    if checks.no_rerun is not None and (
        sum(checks.no_rerun in command for command in metrics.commands) > 1
    ):
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
            runs: dict[Future[RunRow], tuple[Task, ModelSpec]] = {
                pool.submit(execute, task, model, rep, context): (task, model)
                for task, model in pairs
            }
            pending = set(runs)
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        row = future.result()
                    except Exception as exc:  # noqa: BLE001 - one broken run must not end the suite
                        task, model = runs[future]
                        row = _row(
                            task,
                            model,
                            rep,
                            context,
                            _run_dir(context, task, model, rep),
                            time.monotonic(),
                            status="errored",
                            error=f"runner failed: {type(exc).__name__}: {exc}",
                        )
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


def _run_dir(context: RunContext, task: Task, model: ModelSpec, rep: int) -> Path:
    return context.results_dir / task.id / f"{model.provider}__{model.model}" / f"rep{rep}"


def _prepare(
    task: Task, work: Path, run_dir: Path, environment: Mapping[str, str], python: str
) -> str | None:
    """Commit the fixture and run the task's setup; return an error, if any."""

    for command in (
        ["git", "init", "-q"],
        ["git", *_GIT_OPTIONS, "add", "-A"],
        ["git", *_GIT_OPTIONS, "commit", "-q", "-m", "fixture"],
    ):
        completed = subprocess.run(
            command, cwd=work, env=environment, capture_output=True, check=False
        )
        if completed.returncode != 0:
            return (
                f"fixture {' '.join(command)} failed: {completed.stderr.decode(errors='replace')}"
            )
    if task.definition.setup is not None:
        exit_code = _run_command(
            task.definition.setup, work, environment, python, task.folder, run_dir / "setup.txt"
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
        # Wisp runs in the task folder, so a relative path would land there and be deleted.
        str((run_dir / "sessions").resolve()),
    ]
    # MCP tools are selected the same way in both modes; on demand, Wisp offers them
    # through the discovery tools, which exist per run and are never selected by name.
    for tool in (*definition.tools, *mcp_tool_names(task)):
        command += ["--allow-tool", tool]
    settings: dict[str, object] = (
        mcp_settings(task, context.python, context.mcp_tool_exposure)
        if definition.mcp_servers
        else {}
    )
    if context.codemode != "off":
        # A user-only setting, like the MCP servers. Scripts may call the task's
        # read-only tools, which include every MCP tool, all marked read-only above.
        settings["codemode"] = True
    if settings:
        settings_path = home / ".wisp" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
    environment = run_environment(home, context.credentials, os.environ)
    with (
        (run_dir / "events.jsonl").open("w", encoding="utf-8") as events,
        (run_dir / "stderr.txt").open("w", encoding="utf-8") as stderr,
    ):
        exit_code = _run_in_own_group(
            command,
            cwd=work,
            environment=environment,
            stdout=events,
            stderr=stderr,
            timeout=definition.timeout_seconds + _KILL_MARGIN_SECONDS,
        )
    if exit_code is None:
        return None, "wisp did not exit after its --timeout; the runner killed it"
    return exit_code, None


def _mcp_setup_problem(task: Task, metrics: RunMetrics, exposure: McpToolExposure) -> str | None:
    """Say why a run did not get the MCP setup its task needs, or None.

    In direct mode every catalog tool must be offered. On demand only the discovery
    tools are offered, so a server that failed shows as Wisp's startup error. Auto
    offers one set or the other, depending on the catalog's size.
    """

    if not task.definition.mcp_servers:
        return None
    failed = [error for error in metrics.errors if _MCP_UNAVAILABLE.search(error)]
    if failed:
        return failed[0]
    offered = set(metrics.setup.tool_names) if metrics.setup is not None else set()
    match exposure:
        case "direct":
            expected = mcp_tool_names(task)
        case "on_demand":
            expected = DISCOVERY_TOOL_NAMES
        case "auto":
            expected = (
                DISCOVERY_TOOL_NAMES
                if offered.issuperset(DISCOVERY_TOOL_NAMES)
                else mcp_tool_names(task)
            )
    missing = [name for name in expected if name not in offered]
    if missing:
        return f"run was not offered {len(missing)} MCP tools, such as {missing[0]}"
    return None


def _run_command(
    command: str,
    work: Path,
    environment: Mapping[str, str],
    python: str,
    task_dir: Path,
    log: Path,
) -> int | None:
    """Run a task command with `bash -c`, logging its output.

    Returns:
        int | None: Its exit status, or None when it ran past its time limit.
    """

    script = command.replace("{python}", shlex.quote(python)).replace(
        "{task}", shlex.quote(str(task_dir))
    )
    with log.open("w", encoding="utf-8") as output:
        return _run_in_own_group(
            ["bash", "-c", script],
            cwd=work,
            environment=environment,
            stdout=output,
            stderr=output,
            timeout=_VERIFY_TIMEOUT_SECONDS,
        )


def _run_in_own_group(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdout: IO[str],
    stderr: IO[str],
    timeout: float,
) -> int | None:
    """Run a command in a new process group, and kill the group when it ends.

    Killing the group also stops whatever the command left running, such as a
    stuck build or a background process the model started, so nothing outlives
    the run. A process that started its own session escapes this.

    Returns:
        int | None: The exit status, or None when ``timeout`` passed first.
    """

    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


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
        mcp_tool_exposure=context.mcp_tool_exposure,
        proxied_calls=measured.proxied_calls,
        proxied_errors=measured.proxied_errors,
        proxied_argument_errors=measured.proxied_argument_errors,
        codemode=context.codemode,
        scripts=measured.scripts,
        failed_scripts=measured.failed_scripts,
        nested_calls=measured.nested_calls,
    )
