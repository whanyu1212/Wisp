"""`uv run python -m evals.tool_use run|report ...`; see `README.md` here."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from evals.tool_use.credentials import (
    DEFAULT_AUTH_FILE,
    DEFAULT_ENV_FILE,
    REPO_ROOT,
    CredentialError,
    resolve_credentials,
)
from evals.tool_use.report import build_report, load_rows, render_markdown
from evals.tool_use.runner import (
    Budget,
    ModelSpec,
    RunContext,
    RunRow,
    detect_build,
    run_suite,
    write_row,
)
from evals.tool_use.task_definitions import TaskError, select_tasks

RESULTS_DIR = Path(__file__).parent / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.tool_use")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run tasks against models and write runs.jsonl")
    run.add_argument("--models", required=True, help="comma-separated provider::model list")
    run.add_argument("--reps", type=int, default=3, help="repetitions of each task and model")
    run.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    run.add_argument("--tags", default="", help="comma-separated tags; any of them matches")
    run.add_argument("--workers", type=int, default=2, help="runs in flight at once")
    run.add_argument("--budget-usd", type=Decimal, help="stop after priced runs cost this much")
    run.add_argument(
        "--budget-tokens", type=int, help="stop after unpriced runs used this many tokens"
    )
    run.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    run.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
    run.add_argument("--results", type=Path, help="output folder (default: a new timestamped one)")

    report = commands.add_parser("report", help="summarize a runs.jsonl file")
    report.add_argument("runs", type=Path, help="a runs.jsonl file or the folder holding it")
    report.add_argument("--k", type=int, default=3, help="the k in pass^k")

    args = parser.parse_args(argv)
    if args.command == "report":
        path = args.runs / "runs.jsonl" if args.runs.is_dir() else args.runs
        print(render_markdown(build_report(load_rows(path), k=args.k)))
        return 0
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    if args.reps < 1 or args.workers < 1:
        print("error: --reps and --workers must be at least 1", file=sys.stderr)
        return 2
    try:
        models = [ModelSpec.parse(text) for text in _split(args.models)]
        tasks = select_tasks(ids=_split(args.tasks), tags=_split(args.tags))
        credentials = resolve_credentials(
            {model.provider for model in models},
            env_file=args.env_file,
            auth_file=args.auth_file,
            environ=os.environ,
        )
    except (ValueError, TaskError, CredentialError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    results_dir = args.results or RESULTS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results_dir.mkdir(parents=True, exist_ok=True)
    context = RunContext(
        build=detect_build(REPO_ROOT), credentials=credentials, results_dir=results_dir
    )
    runs_path = results_dir / "runs.jsonl"
    print(
        f"{len(tasks)} tasks × {len(models)} models × {args.reps} reps → {results_dir}",
        file=sys.stderr,
    )

    def show(row: RunRow) -> None:
        write_row(runs_path, row)
        print(_progress_line(row), file=sys.stderr)

    result = run_suite(
        tasks,
        models,
        args.reps,
        context,
        workers=args.workers,
        budget=Budget(max_usd=args.budget_usd, max_unpriced_tokens=args.budget_tokens),
        on_row=show,
    )
    suite = {
        "models": [model.label for model in models],
        "tasks": [task.id for task in tasks],
        "reps": args.reps,
        "wisp_version": context.build.version,
        "git_commit": context.build.git_commit,
        "credential_sources": dict(credentials.sources),
        "stopped_for_budget": result.stopped_for_budget,
        "skipped_runs": result.skipped_runs,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    (results_dir / "suite.json").write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    if result.stopped_for_budget:
        print(f"stopped for budget; {result.skipped_runs} runs skipped", file=sys.stderr)
    print(render_markdown(build_report([asdict(row) for row in result.rows], k=args.reps)))
    return 0


def _progress_line(row: RunRow) -> str:
    return (
        f"{row.status:>7}  {row.task}  {row.provider}::{row.model}  rep {row.rep}"
        f"  {row.seconds:.0f}s"
    )


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
