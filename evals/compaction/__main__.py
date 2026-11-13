"""`uv run python -m evals.compaction summaries|continuation|report ...`; see `README.md`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import anyio

from evals.compaction.continuation import (
    SETUPS,
    ContinuationContext,
    ContinuationRow,
    default_models,
    load_tasks,
    run_continuation_suite,
    write_continuation_row,
)
from evals.compaction.report import load_jsonl, render, render_summary_comparison
from evals.compaction.scenarios import select_scenarios
from evals.compaction.summaries import (
    SummaryRow,
    load_providers,
    rescore,
    run_summaries,
    write_summary_row,
)
from evals.tool_use.credentials import (
    DEFAULT_AUTH_FILE,
    DEFAULT_ENV_FILE,
    REPO_ROOT,
    CredentialError,
    resolve_credentials,
)
from evals.tool_use.runner import ModelSpec, detect_build

RESULTS_DIR = Path(__file__).parent / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.compaction")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("summaries", "Layer 1: score each model's checkpoint of fixed transcripts"),
        ("continuation", "Layer 2: run multi-turn tasks with compaction forced and not"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--models", required=True, help="comma-separated provider::model")
        command.add_argument("--reps", type=int, default=5)
        command.add_argument("--only", default="", help="comma-separated scenario or task ids")
        command.add_argument("--workers", type=int, default=4 if name == "summaries" else 2)
        command.add_argument("--budget-usd", type=Decimal, help="stop starting rounds past this")
        command.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
        command.add_argument("--auth-file", type=Path, default=DEFAULT_AUTH_FILE)
        command.add_argument("--results", type=Path, help="output folder (default: timestamped)")
        if name == "continuation":
            command.add_argument(
                "--setups", default=",".join(SETUPS), help="forced, default, or both"
            )
    report = commands.add_parser("report", help="print Markdown tables for a results folder")
    report.add_argument("results", type=Path)
    rescore_command = commands.add_parser(
        "rescore", help="score a Layer 1 folder again with the current fact definitions"
    )
    rescore_command.add_argument("results", type=Path)
    compare = commands.add_parser("compare", help="compare two Layer 1 results folders")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    args = parser.parse_args(argv)

    if args.command == "report":
        print(render(args.results))
        return 0
    if args.command == "rescore":
        path = args.results / "summaries.jsonl"
        rows = rescore(load_jsonl(path), args.results)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        print(render(args.results))
        return 0
    if args.command == "compare":
        print(
            render_summary_comparison(
                load_jsonl(args.before / "summaries.jsonl"),
                load_jsonl(args.after / "summaries.jsonl"),
            )
        )
        return 0

    models = [ModelSpec.parse(text) for text in args.models.split(",") if text]
    only = [item for item in args.only.split(",") if item]
    try:
        credentials = resolve_credentials(
            [model.provider for model in models],
            env_file=args.env_file,
            auth_file=args.auth_file,
            environ=os.environ,
        )
    except CredentialError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    results = args.results or RESULTS_DIR / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results.mkdir(parents=True, exist_ok=True)
    build = detect_build(REPO_ROOT)
    (results / f"{args.command}-suite.json").write_text(
        json.dumps(
            {
                "models": [model.label for model in models],
                "reps": args.reps,
                "only": only,
                "wisp_version": build.version,
                "git_commit": build.git_commit,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.command == "summaries":
        scenarios = select_scenarios(only)

        def record_summary(row: SummaryRow) -> None:
            write_summary_row(results / "summaries.jsonl", row)
            _progress(f"{row.provider}::{row.model} {row.scenario}", row.rep, row.status)

        async def summarize() -> None:
            providers, cost_estimator = await load_providers(
                sorted({model.provider for model in models}), credentials
            )
            await run_summaries(
                scenarios,
                models,
                args.reps,
                providers=providers,
                cost_estimator=cost_estimator,
                build=build,
                results_dir=results,
                workers=args.workers,
                max_usd=args.budget_usd,
                on_row=record_summary,
            )

        anyio.run(summarize)
    else:

        def record_continuation(row: ContinuationRow) -> None:
            write_continuation_row(results / "continuation.jsonl", row)
            _progress(f"{row.provider}::{row.model} {row.task} {row.setup}", row.rep, row.status)

        setups = [setup for setup in SETUPS if setup in args.setups.split(",")]
        run_continuation_suite(
            load_tasks(ids=only),
            models,
            setups,
            args.reps,
            ContinuationContext(
                build=build, credentials=credentials, results_dir=results, models=default_models()
            ),
            workers=args.workers,
            max_usd=args.budget_usd,
            on_row=record_continuation,
        )
    print(render(results))
    print(f"\nresults: {results}")
    return 0


def _progress(what: str, rep: int, status: str) -> None:
    print(f"{what} rep{rep}: {status}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
