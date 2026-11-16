"""Scoring runs (`evals.tool_use.report`)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.tool_use.report import (
    TokenSplit,
    build_report,
    estimated_usd,
    pass_hat_k,
    render_markdown,
    token_split,
)
from wisp.providers.catalog import ModelCatalog, ModelRegistry

_BASELINES = Path(__file__).parents[2] / "evals" / "tool_use" / "baselines"
# Providers that count cached tokens within their input, and one Wisp cannot sort.
_INSIDE = ("openai", "openai-codex", "google", "acme")


@pytest.mark.parametrize(
    ("passed", "scored", "k", "expected"),
    [(3, 3, 3, 1.0), (2, 3, 3, 0.0), (3, 4, 2, 0.5), (1, 2, 3, None), (0, 0, 1, None)],
)
def test_pass_hat_k(passed: int, scored: int, k: int, expected: float | None) -> None:
    assert pass_hat_k(passed, scored, k) == expected


def _row(
    task: str,
    status: str,
    *,
    rep: int,
    tools: str = "t",
    timed_out: bool = False,
    calls: int = 4,
    tool_errors: int = 0,
) -> dict:
    return {
        "task": task,
        "provider": "openai",
        "model": "m",
        "rep": rep,
        "status": status,
        "timed_out": timed_out,
        "input_tokens": 100,
        "output_tokens": 20,
        "known_usd": "0.5",
        "tool_calls": {"read": calls - 1, "edit": 1},
        "tool_errors": {"not_found": tool_errors} if tool_errors else {},
        "tools_sha256": tools,
        "instructions_sha256": f"i{rep}",
    }


def test_errored_runs_not_scored() -> None:
    rows = [
        _row("a", "passed", rep=1),
        _row("a", "failed", rep=2, timed_out=True, calls=8, tool_errors=1),
        _row("a", "errored", rep=3, calls=40, tool_errors=9),
        _row("b", "passed", rep=1, calls=2),
    ]

    report = build_report(rows, k=2)

    first, second = report.task_scores
    assert (first.task, first.scored, first.passed, first.errored, first.timed_out) == (
        "a",
        2,
        1,
        1,
        1,
    )
    assert (first.pass_1, first.pass_k, str(first.known_usd)) == (0.5, 0.0, "1.5")
    assert (first.mean_calls, first.mean_tool_errors) == (6.0, 0.5)
    assert (second.pass_1, second.pass_k, second.mean_calls) == (1.0, None, 2.0)
    model = report.model_scores[0]
    assert (model.pass_1, model.mean_calls, model.mean_tokens) == (0.75, 4.0, 120.0)
    assert report.warnings == []
    table = render_markdown(report)
    assert (
        "| a | openai::m | 6.0 | 0.5 | 100 | 0 | 20 | 1/2 | 50% | 0% | 1 | 1 | 1.5 | — |" in table
    )
    assert "Est. USD prices" not in table
    assert "| openai::m | 2 | 4.0 | 120 | 75% |" in table


def test_differing_tools_warned() -> None:
    rows = [_row("a", "passed", rep=1), _row("a", "passed", rep=2, tools="u")]

    assert build_report(rows).warnings == ["a with openai::m: runs offered 2 different tool sets"]


def test_reports_proxied_calls() -> None:
    rows = [_row("a", "passed", rep=1), _row("a", "passed", rep=2)]
    rows[0].update(proxied_calls=4, proxied_errors=1)

    table = render_markdown(build_report(rows))

    assert "| openai::m | 4 | 1 (25%) |" in table
    assert "Proxied" not in render_markdown(build_report([_row("a", "passed", rep=1)]))


def _tokens(provider: str, **counts: int) -> dict:
    return _row("a", "passed", rep=1) | {"provider": provider, "output_tokens": 50} | counts


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        # OpenAI's input already includes what it read from the cache.
        (_tokens("openai", input_tokens=1000, cache_read_tokens=600), (1000, 600, 50)),
        # Anthropic's leaves out both cache reads and cache writes.
        (
            _tokens("anthropic", input_tokens=100, cache_read_tokens=600, cache_write_tokens=300),
            (1000, 600, 50),
        ),
        (_tokens("anthropic", input_tokens=100, cache_read_tokens=600), (700, 600, 50)),
        (_tokens("acme", input_tokens=100, cache_read_tokens=600), (100, 600, 50)),
        # More cached than input cannot be sorted into parts, so it stays as reported.
        (_tokens("openai", input_tokens=100, cache_read_tokens=600), (100, 600, 50)),
    ],
    ids=["openai", "anthropic", "anthropic-no-writes", "unknown-provider", "inconsistent"],
)
def test_providers_counted_alike(row: dict, expected: tuple[int, int, int]) -> None:
    assert token_split(row) == TokenSplit(*expected)


def test_committed_baseline_totals() -> None:
    """Rows already committed keep their OpenAI totals; Anthropic ones gain cached input."""
    document = json.loads((_BASELINES / "2026-11-11-core-codemode-python.json").read_text())
    rows = [row for row in document["runs"] if row["status"] != "errored"]

    for row in rows:
        reported = row["input_tokens"] + row["output_tokens"]
        cached = row["cache_read_tokens"] if row["provider"] == "anthropic" else 0
        assert token_split(row).total == reported + cached
    assert {row["provider"] for row in rows} == {"anthropic", "openai-codex"}


def _models() -> ModelRegistry:
    def provider(name: str, pricing: dict) -> dict:
        return {
            "name": name,
            "display_name": name,
            "default_model": "m",
            "docs_url": "https://example.com/docs",
            "models": ["m", "unpriced"],
            "pricing": pricing,
        }

    base = {
        "input_usd_per_million": "2",
        "output_usd_per_million": "10",
        "cache_read_usd_per_million": "0.2",
    }
    long_context = base | {"input_token_threshold": 1000, "input_usd_per_million": "4"}
    providers = [provider("openai", {"m": [base, long_context]}), provider("openai-codex", {})]
    return ModelRegistry(ModelCatalog.model_validate({"schema_version": 2, "providers": providers}))


def _unpriced(**changes: object) -> dict:
    row = _tokens("openai-codex", input_tokens=1_000_000, cache_read_tokens=500_000)
    return row | {"output_tokens": 100_000, "known_usd": "0"} | changes


def test_unpriced_run_estimated() -> None:
    """A subscription run is priced at the API's base band, whatever its summed input."""
    # 500k uncached at $2, 500k cached at $0.20, 100k output at $10, per million.
    assert estimated_usd(_unpriced(), _models()) == Decimal("2.1")


@pytest.mark.parametrize(
    "changes",
    [
        {"known_usd": "0.5"},
        {"model": "unpriced"},
        {"provider": "acme"},
        {"input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0},
        # The catalog has no cache-write price, so these tokens cannot be priced.
        {"provider": "openai", "cache_write_tokens": 10},
    ],
    ids=["already-priced", "no-price", "unknown-provider", "no-tokens", "unpriced-part"],
)
def test_no_estimate(changes: dict[str, object]) -> None:
    assert estimated_usd(_unpriced(**changes), _models()) is None


def test_estimate_column_rendered() -> None:
    rows = [_unpriced(rep=1), _unpriced(rep=2, status="errored")]

    report = build_report(rows, models=_models())

    (score,) = report.task_scores
    assert (score.known_usd, score.estimated_usd) == (Decimal(0), Decimal("4.2"))
    table = render_markdown(report)
    assert "| 1,000,000 | 500,000 | 100,000 | 1/1 | 100% | — | 1 | 0 | 0 | ~4.2000 |" in table
    assert "(openai-codex at openai's). It is an estimate" in table


def test_missing_cache_writes_warned() -> None:
    """Only a provider that counts cached tokens outside its input can be missing writes."""
    older = _tokens("anthropic", input_tokens=100, cache_read_tokens=600)
    # A run can write to the cache without reading from it, so reads are no signal.
    uncached = _tokens("anthropic", input_tokens=100, cache_read_tokens=0) | {"rep": 2}
    current = older | {"rep": 3, "cache_write_tokens": 300}
    others = [_tokens(provider, input_tokens=1000, cache_read_tokens=600) for provider in _INSIDE]

    assert build_report([older, uncached, current, *others]).warnings == [
        "runs recorded before cache writes were counted, whose input tokens leave them out: 2"
    ]


def test_script_failures_listed() -> None:
    rows = [_row("a", "passed", rep=rep) for rep in (1, 2, 3)]
    rows[0].update(
        scripts=3,
        failed_scripts=2,
        script_failures={"type: unresolved-reference": 2},
        nested_calls=6,
        failed_script_calls=0,
    )
    rows[1].update(
        scripts=2,
        failed_scripts=1,
        script_failures={"runtime: ValueError": 1},
        nested_calls=8,
        failed_script_calls=6,
    )
    # Recorded before failures were named.
    rows[2].update(scripts=1, failed_scripts=1)

    table = render_markdown(build_report(rows))

    assert (
        "| openai::m | 6 | 4 | type: unresolved-reference (2), runtime: ValueError (1), "
        "not recorded (1) | 6 of 20 (30%) |"
    ) in table
    assert "Scripts" not in render_markdown(build_report([_row("a", "passed", rep=1)]))


def test_failed_calls_not_recorded() -> None:
    row = _row("a", "passed", rep=1)
    row.update(scripts=1, failed_scripts=1, nested_calls=3)

    assert "| not recorded |" in render_markdown(build_report([row]))
