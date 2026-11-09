"""`uv run python -m evals.tool_use run|report|compare|baseline ...`; see `README.md` here."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from evals.tool_use.compare import (
    baseline_document,
    compare_runs,
    render_comparison,
    write_baseline,
)
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
from wisp.mcp.config import MCP_TOOL_EXPOSURES

RESULTS_DIR = Path(__file__).parent / "results"
BASELINES_DIR = Path(__file__).parent / "baselines"


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
    run.add_argument(
        "--mcp-tool-exposure",
        choices=MCP_TOOL_EXPOSURES,
        default="direct",
        help="how MCP tasks' tools reach the model (default: direct)",
    )

    runs_help = "a results folder, its runs.jsonl, or a baseline .json file"
    report = commands.add_parser("report", help="summarize one set of runs")
    report.add_argument("runs", type=Path, help=runs_help)
    report.add_argument("--k", type=int, default=3, help="the k in pass^k")

    compare = commands.add_parser("compare", help="compare two sets of runs task by task")
    compare.add_argument("before", type=Path, help=runs_help)
    compare.add_argument("after", type=Path, help=runs_help)

    baseline = commands.add_parser("baseline", help="save a results folder as a baseline")
    baseline.add_argument("results", type=Path, help="a results folder from `run`")
    baseline.add_argument("--label", required=True, help="what it measures, e.g. current-tools")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    try:
        if args.command == "report":
            print(render_markdown(build_report(load_rows(args.runs), k=args.k)))
            return 0
        if args.command == "compare":
            comparison = compare_runs(load_rows(args.before), load_rows(args.after))
            print(render_comparison(comparison))
            return 0
        return _baseline(args.results, args.label)
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


def _baseline(results: Path, label: str) -> int:
    # The label becomes part of a committed file name.
    if re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", label) is None:
        print("error: --label must be lowercase words joined by hyphens", file=sys.stderr)
        return 2
    # `run` writes suite.json last, so its absence means the suite did not finish.
    suite_path = results / "suite.json"
    if not suite_path.is_file():
        print(
            f"error: {results} has no suite.json; pass a finished results folder", file=sys.stderr
        )
        return 2
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    finished = suite.get("finished_at") if isinstance(suite, dict) else None
    if not isinstance(finished, str):
        print(f"error: {suite_path} has no finished_at time", file=sys.stderr)
        return 2
    date = finished[:10]
    path = BASELINES_DIR / f"{date}-{label}.json"
    document = baseline_document(load_rows(results), label=label, date=date, suite=suite)
    try:
        write_baseline(path, document)
    except FileExistsError:
        print(f"error: {path} already exists; choose another label", file=sys.stderr)
        return 2
    print(path)
    return 0


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
    if (results_dir / "runs.jsonl").exists():
        print(f"error: {results_dir} already holds runs; choose a new folder", file=sys.stderr)
        return 2
    results_dir.mkdir(parents=True, exist_ok=True)
    context = RunContext(
        build=detect_build(REPO_ROOT),
        credentials=credentials,
        results_dir=results_dir,
        mcp_tool_exposure=args.mcp_tool_exposure,
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
        "mcp_tool_exposure": args.mcp_tool_exposure,
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
