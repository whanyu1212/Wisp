"""Layer 2: does an agent finish a multi-turn task after its history is compacted?

A task is a fixture repository and a list of user turns. Each turn is one
`wisp -p` call; later turns continue the same session. With the `forced` setup
the reserve is raised so the session crosses Wisp's auto-compaction threshold
at a fixed, small size and the final turns run on a checkpoint. The `default`
setup leaves the model's real window, so nothing is compacted. Comparing the two
with the same model separates what compaction loses from what the task costs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import TypeAliasType

from evals.tool_use.credentials import Credentials
from evals.tool_use.metrics import measure, parse_events
from evals.tool_use.runner import (
    Build,
    ModelSpec,
    command_environment,
    run_environment,
    run_in_own_group,
    run_task_command,
)
from evals.tool_use.task_definitions import ToolName
from wisp.events import CompactionCompleted, CompactionStarted, KnownWispEvent, ToolExecutionStarted
from wisp.providers.catalog import ModelRegistry, builtin_catalog

TASKS_DIR = Path(__file__).parent / "tasks"

Setup = TypeAliasType("Setup", Literal["forced", "default"])
SETUPS: tuple[Setup, ...] = ("forced", "default")
ContinuationStatus = TypeAliasType(
    "ContinuationStatus", Literal["passed", "failed", "compaction_failed", "errored"]
)

_KILL_MARGIN_SECONDS = 60
_GIT = ("git", "-c", "user.name=wisp-eval", "-c", "user.email=wisp-eval@example.invalid")


class ContinuationTask(BaseModel):
    """The contents of one `task.toml`.

    Attributes:
        id (str): Task name; it must match the folder name.
        title (str): One line saying what the task measures.
        turns (tuple[str, ...]): The user's messages, one `wisp -p` call each.
        verify (str): Runs after the last turn with `bash -c`; exit 0 passes.
            `{python}` and `{task}` are replaced as in the tool-use evals.
        tools (tuple[ToolName, ...]): The only tools Wisp exposes.
        timeout_seconds (float): Wisp's `--timeout` for each turn.
        max_tool_iterations (int): Wisp's `--max-tool-iterations` for each turn.
        trigger_tokens (int): With the `forced` setup, the context size above
            which Wisp compacts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    turns: tuple[str, ...] = Field(min_length=2)
    verify: str = Field(min_length=1)
    tools: tuple[ToolName, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=300, gt=0)
    max_tool_iterations: int = Field(default=30, gt=0)
    trigger_tokens: int = Field(default=20_000, gt=0)


@dataclass(frozen=True, slots=True)
class LoadedTask:
    """A task definition and the folder its fixture and verifier live in."""

    definition: ContinuationTask
    folder: Path

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def fixture(self) -> Path:
        return self.folder / "repo"


def load_tasks(root: Path = TASKS_DIR, ids: Iterable[str] = ()) -> tuple[LoadedTask, ...]:
    """Load every task under ``root``, or only those named by ``ids``.

    Raises:
        ValueError: When a task is malformed or an id names no task.
    """

    tasks: list[LoadedTask] = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        path = folder / "task.toml"
        try:
            with path.open("rb") as handle:
                definition = ContinuationTask.model_validate(tomllib.load(handle))
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as err:
            raise ValueError(f"task {folder.name}: invalid {path}: {err}") from err
        if definition.id != folder.name:
            raise ValueError(f"task {folder.name}: id {definition.id!r} must match its folder")
        if not (folder / "repo").is_dir():
            raise ValueError(f"task {folder.name}: missing fixture folder {folder / 'repo'}")
        tasks.append(LoadedTask(definition=definition, folder=folder))
    wanted = set(ids)
    unknown = wanted - {task.id for task in tasks}
    if unknown:
        raise ValueError(f"no task named {', '.join(sorted(unknown))}")
    return tuple(task for task in tasks if not wanted or task.id in wanted)


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    """One compaction Wisp ran during a turn.

    Attributes:
        turn (int): The 1-based turn it ran in.
        reason (str): Why it ran: `threshold`, `overflow` or `manual`.
        outcome (str): How it ended.
        trigger_tokens (int | None): Wisp's context estimate when it started.
        replaced_entries (int): Session entries the checkpoint replaced.
        error (str | None): Wisp's error when it failed.
        before_tools (bool): Whether it ended before the turn's first tool call.
    """

    turn: int
    reason: str
    outcome: str
    trigger_tokens: int | None
    replaced_entries: int
    error: str | None
    before_tools: bool = False


@dataclass(slots=True)
class ContinuationRow:
    """One task run's result, written as a line of `continuation.jsonl`.

    `status` is decided by `classify`: `compaction_failed` when Wisp could not
    finish a compaction the run needed, `errored` when the run says nothing
    about compaction (a crash, a provider error, or a `forced` run whose
    checkpoint came too late or never). `repeat_reads` counts reads in the last turn of
    files already read in an earlier one, the work a lost checkpoint makes the
    agent redo.
    """

    task: str
    provider: str
    model: str
    setup: Setup
    rep: int
    status: ContinuationStatus
    error: str | None
    verify_exit: int | None
    turns_completed: int
    reserve_tokens: int | None
    compactions: list[CompactionRecord]
    repeat_reads: int
    last_turn_tool_calls: int
    tool_calls: int
    tool_errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    known_usd: str
    unpriced_responses: int
    seconds: float
    credential_source: str | None
    wisp_version: str
    git_commit: str
    run_dir: str
    verify_output: str = ""
    turn_outcomes: list[str | None] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ContinuationContext:
    """What every run in a suite shares."""

    build: Build
    credentials: Credentials
    results_dir: Path
    models: ModelRegistry
    python: str = sys.executable


def forced_reserve(models: ModelRegistry, model: ModelSpec, trigger_tokens: int) -> int:
    """Return the reserve that makes Wisp compact above ``trigger_tokens``.

    Wisp compacts when context exceeds ``window - reserve`` and uses the larger
    of this reserve and the provider's own compaction limit, so the threshold
    lands at ``trigger_tokens`` for every provider.

    Raises:
        ValueError: When the catalog has no window for the model, or the
            window is not larger than ``trigger_tokens``.
    """

    window = models.context_window(model.provider, model.model)
    if window is None:
        raise ValueError(f"no context window for {model.label} in the catalog")
    if window <= trigger_tokens:
        raise ValueError(f"{model.label} window {window} is not above {trigger_tokens}")
    return window - trigger_tokens


def compaction_records(turn: int, events: Iterable[KnownWispEvent]) -> list[CompactionRecord]:
    """Pair each turn's compaction start and end events into records."""

    records: list[CompactionRecord] = []
    started: CompactionStarted | None = None
    tools_started = False
    for event in events:
        if isinstance(event, ToolExecutionStarted):
            tools_started = True
        elif isinstance(event, CompactionStarted):
            started = event
        elif isinstance(event, CompactionCompleted):
            budget = started.trigger_budget if started is not None else None
            records.append(
                CompactionRecord(
                    turn=turn,
                    reason=event.reason,
                    outcome=event.outcome,
                    trigger_tokens=budget.effective_tokens if budget is not None else None,
                    replaced_entries=event.replaced_entry_count,
                    error=event.error,
                    before_tools=not tools_started,
                )
            )
            started = None
    return records


def read_paths(events: Iterable[KnownWispEvent]) -> list[str]:
    """Return the `path` of every `read` call, normalized, in order."""

    paths: list[str] = []
    for event in events:
        if isinstance(event, ToolExecutionStarted) and event.name == "read":
            path = event.arguments.get("path")
            if isinstance(path, str):
                paths.append(os.path.normpath(path.removeprefix("./")))
    return paths


def repeat_reads(turn_reads: Sequence[Sequence[str]]) -> int:
    """Count reads in the last turn of files read in an earlier turn."""

    if not turn_reads:
        return 0
    earlier = {path for reads in turn_reads[:-1] for path in reads}
    return sum(path in earlier for path in turn_reads[-1])


def classify(
    *,
    setup: Setup,
    runner_error: str | None,
    turn_outcomes: Sequence[str | None],
    turns: int,
    compactions: Sequence[CompactionRecord],
    verify_exit: int | None,
    turn_error: str | None = None,
) -> tuple[ContinuationStatus, str | None]:
    """Decide a run's status, and say why when it is not `passed`.

    `compaction_failed` is the failure this eval looks for: Wisp started a
    compaction and could not finish it, and the run either stopped or reached
    its last turn without a checkpoint. A `forced` run whose compaction ran
    only after the last turn's first tool call is `errored`: the work it is
    scored on did not depend on the checkpoint.
    """

    failed_compactions = [record for record in compactions if record.outcome != "completed"]
    if runner_error is not None:
        return "errored", runner_error
    if len(turn_outcomes) < turns or any(outcome != "completed" for outcome in turn_outcomes):
        if failed_compactions:
            return "compaction_failed", failed_compactions[-1].error or turn_error
        return "errored", turn_error or f"turn outcomes {list(turn_outcomes)}"
    if setup == "forced" and not any(
        record.outcome == "completed" and (record.turn < turns or record.before_tools)
        for record in compactions
    ):
        if failed_compactions:
            return "compaction_failed", failed_compactions[-1].error
        if compactions:
            return "errored", "compaction ran only after the last turn's work began"
        return "errored", "forced setup never compacted"
    if verify_exit != 0:
        return "failed", f"verify exited {verify_exit}"
    return "passed", None


def run_one(
    task: LoadedTask, model: ModelSpec, setup: Setup, rep: int, context: ContinuationContext
) -> ContinuationRow:
    """Run every turn of one task once, then verify the folder."""

    definition = task.definition
    run_dir = (
        context.results_dir / task.id / f"{model.provider}__{model.model}" / setup / f"rep{rep}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    reserve = (
        forced_reserve(context.models, model, definition.trigger_tokens)
        if setup == "forced"
        else None
    )
    turn_outcomes: list[str | None] = []
    compactions: list[CompactionRecord] = []
    turn_reads: list[list[str]] = []
    totals: Counter[str] = Counter()
    usd = Decimal(0)
    last_turn_calls = 0
    runner_error: str | None = None
    turn_error: str | None = None
    verify_exit: int | None = None
    with tempfile.TemporaryDirectory(prefix="wisp-compaction-eval-") as scratch:
        work = Path(scratch) / "work"
        home = Path(scratch) / "home"
        (home / ".wisp").mkdir(parents=True)
        (home / ".wisp" / "settings.json").write_text(json.dumps({"codemode": False}))
        shutil.copytree(task.fixture, work)
        commands = command_environment(home, os.environ)
        runner_error = _commit_fixture(work, commands)
        environment = run_environment(home, context.credentials, os.environ)
        environment["WISP_AUTO_COMPACTION"] = "1"
        if reserve is not None:
            environment["WISP_CONTEXT_RESERVE_TOKENS"] = str(reserve)
        for turn, prompt in enumerate(definition.turns, start=1):
            if runner_error is not None:
                break
            events_path = run_dir / f"events-turn{turn}.jsonl"
            exit_code = _run_turn(
                definition, model, prompt, turn, work, run_dir, events_path, environment, context
            )
            if exit_code is None:
                runner_error = f"turn {turn}: wisp did not exit after its --timeout"
                break
            try:
                events = parse_events(events_path.read_text(encoding="utf-8").splitlines())
            except ValueError as err:
                runner_error = f"turn {turn}: unreadable events: {err}"
                break
            metrics = measure(events)
            turn_outcomes.append(metrics.outcome)
            compactions += compaction_records(turn, events)
            turn_reads.append(read_paths(events))
            last_turn_calls = sum(metrics.tool_calls.values())
            totals["tool_calls"] += last_turn_calls
            totals["tool_errors"] += sum(metrics.tool_errors.values())
            totals["input"] += metrics.input_tokens
            totals["output"] += metrics.output_tokens
            totals["cache_read"] += metrics.cache_read_tokens
            totals["unpriced"] += metrics.unpriced_responses
            usd += metrics.known_usd
            if metrics.outcome != "completed":
                turn_error = f"turn {turn}: {metrics.last_error or metrics.outcome}"
                break
        verify_log = run_dir / "verify.txt"
        if runner_error is None and turn_error is None:
            verify_exit = run_task_command(
                definition.verify, work, commands, context.python, task.folder, verify_log
            )
    status, error = classify(
        setup=setup,
        runner_error=runner_error,
        turn_outcomes=turn_outcomes,
        turns=len(definition.turns),
        compactions=compactions,
        verify_exit=verify_exit,
        turn_error=turn_error,
    )
    return ContinuationRow(
        task=task.id,
        provider=model.provider,
        model=model.model,
        setup=setup,
        rep=rep,
        status=status,
        error=error,
        verify_exit=verify_exit,
        turns_completed=sum(outcome == "completed" for outcome in turn_outcomes),
        reserve_tokens=reserve,
        compactions=compactions,
        repeat_reads=repeat_reads(turn_reads) if len(turn_reads) == len(definition.turns) else 0,
        last_turn_tool_calls=last_turn_calls,
        tool_calls=totals["tool_calls"],
        tool_errors=totals["tool_errors"],
        input_tokens=totals["input"],
        output_tokens=totals["output"],
        cache_read_tokens=totals["cache_read"],
        known_usd=str(usd),
        unpriced_responses=totals["unpriced"],
        seconds=round(time.monotonic() - started, 3),
        credential_source=context.credentials.sources.get(model.provider),
        wisp_version=context.build.version,
        git_commit=context.build.git_commit,
        run_dir=str(run_dir.relative_to(context.results_dir)),
        verify_output=_tail(run_dir / "verify.txt"),
        turn_outcomes=turn_outcomes,
    )


def run_continuation_suite(
    tasks: Sequence[LoadedTask],
    models: Sequence[ModelSpec],
    setups: Sequence[Setup],
    reps: int,
    context: ContinuationContext,
    *,
    workers: int = 2,
    max_usd: Decimal | None = None,
    execute: Callable[
        [LoadedTask, ModelSpec, Setup, int, ContinuationContext], ContinuationRow
    ] = run_one,
    on_row: Callable[[ContinuationRow], None] = lambda _row: None,
) -> list[ContinuationRow]:
    """Run every task, model and setup ``reps`` times, round by round.

    The budget is checked before each round, so a stopped suite has whole rounds.
    """

    rows: list[ContinuationRow] = []
    combos = [(task, model, setup) for task in tasks for model in models for setup in setups]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rep in range(1, reps + 1):
            if (
                max_usd is not None
                and sum((Decimal(r.known_usd) for r in rows), Decimal(0)) >= max_usd
            ):
                break
            futures = [pool.submit(execute, *combo, rep, context) for combo in combos]
            for future, (task, model, setup) in zip(futures, combos, strict=True):
                try:
                    row = future.result()
                except Exception as exc:  # noqa: BLE001 - one broken run must not end the suite
                    row = _crashed_row(task, model, setup, rep, context, exc)
                rows.append(row)
                on_row(row)
    return rows


def write_continuation_row(path: Path, row: ContinuationRow) -> None:
    """Append one row to a `continuation.jsonl` file."""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(row), sort_keys=True) + "\n")


def default_models() -> ModelRegistry:
    """The built-in catalog, which is what Wisp reads with a run's private `HOME`."""

    return ModelRegistry(builtin_catalog())


def _crashed_row(
    task: LoadedTask,
    model: ModelSpec,
    setup: Setup,
    rep: int,
    context: ContinuationContext,
    exc: Exception,
) -> ContinuationRow:
    return ContinuationRow(
        task=task.id,
        provider=model.provider,
        model=model.model,
        setup=setup,
        rep=rep,
        status="errored",
        error=f"runner failed: {type(exc).__name__}: {exc}",
        verify_exit=None,
        turns_completed=0,
        reserve_tokens=None,
        compactions=[],
        repeat_reads=0,
        last_turn_tool_calls=0,
        tool_calls=0,
        tool_errors=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        known_usd="0",
        unpriced_responses=0,
        seconds=0.0,
        credential_source=context.credentials.sources.get(model.provider),
        wisp_version=context.build.version,
        git_commit=context.build.git_commit,
        run_dir="",
    )


def _commit_fixture(work: Path, environment: Mapping[str, str]) -> str | None:
    for command in (
        ["git", "init", "-q"],
        [*_GIT, "add", "-A"],
        [*_GIT, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"],
    ):
        completed = subprocess.run(
            command, cwd=work, env=dict(environment), capture_output=True, check=False
        )
        if completed.returncode != 0:
            return (
                f"fixture {' '.join(command)} failed: {completed.stderr.decode(errors='replace')}"
            )
    return None


def _run_turn(
    definition: ContinuationTask,
    model: ModelSpec,
    prompt: str,
    turn: int,
    work: Path,
    run_dir: Path,
    events_path: Path,
    environment: Mapping[str, str],
    context: ContinuationContext,
) -> int | None:
    command = [
        str(context.build.wisp),
        "-p",
        prompt,
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
        str((run_dir / "sessions").resolve()),
    ]
    if turn > 1:
        command.append("--continue")
    for tool in definition.tools:
        command += ["--allow-tool", tool]
    with (
        events_path.open("w", encoding="utf-8") as events,
        (run_dir / f"stderr-turn{turn}.txt").open("w", encoding="utf-8") as stderr,
    ):
        return run_in_own_group(
            command,
            cwd=work,
            environment=environment,
            stdout=events,
            stderr=stderr,
            timeout=definition.timeout_seconds + _KILL_MARGIN_SECONDS,
        )


def _tail(path: Path, chars: int = 600) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-chars:] if path.is_file() else ""
