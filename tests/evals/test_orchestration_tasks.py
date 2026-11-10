"""The `orchestration` tasks' data in devhub: answers, traps and an unchanged catalog."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from evals.tool_use.servers import devhub, devhub_repos
from evals.tool_use.servers.catalogs import CATALOGS
from evals.tool_use.task_definitions import Task, select_tasks

_TASKS = {task.id: task for task in select_tasks(tags=["orchestration"])}
_TOOLS = {tool.name: tool for tool in CATALOGS["devhub"].tools}
_API = {"owner": "acme", "repo": "api"}


def _answer(task_id: str) -> str:
    return (_TASKS[task_id].folder / "solution" / "ANSWER.txt").read_text(encoding="utf-8")


def _verifies(task: Task, folder: Path, answer: str) -> bool:
    (folder / "ANSWER.txt").write_text(answer, encoding="utf-8")
    for fixture in (task.fixture).iterdir():
        (folder / fixture.name).write_bytes(fixture.read_bytes())
    command = [sys.executable, str(task.folder / "verify.py")]
    return subprocess.run(command, cwd=folder, check=False).returncode == 0


def test_tasks_are_opt_in() -> None:
    assert set(_TASKS) == {
        "mcp-failing-lint",
        "mcp-flaky-comments",
        "mcp-deploy-chain",
        "mcp-service-alerts",
    }
    assert all(task.definition.opt_in for task in _TASKS.values())


def test_catalog_definitions_unchanged() -> None:
    """Only answers change: names, descriptions and schemas are the baselines' prompt."""

    raw = (*devhub._TASK_TOOLS, *devhub._decoy_tools())
    served = CATALOGS["devhub"].tools
    assert [(tool.name, tool.description, tool.parameters) for tool in served] == [
        (tool.name, tool.description, tool.parameters) for tool in raw
    ]
    web = {"owner": "acme", "repo": "web", "pull_number": 418, "issue_number": 412, "id": "1"}
    assert [tool.respond(web) for tool in served] == [tool.respond(web) for tool in raw]


def test_solutions_match_data() -> None:
    assert [int(line) for line in _answer("mcp-failing-lint").split()] == list(
        devhub_repos.LINT_FAILURES
    )
    assert int(_answer("mcp-flaky-comments")) == devhub_repos.FLAKY_COMMENTS
    assert _answer("mcp-deploy-chain").splitlines() == [
        f"pr: {devhub_repos.DEPLOY_PULL}",
        f"approved by: {devhub_repos.DEPLOY_APPROVER}",
    ]
    assert _answer("mcp-service-alerts").split() == list(devhub_repos.ALERTED_SERVICES)
    services = _TASKS["mcp-service-alerts"].fixture / "services.txt"
    assert services.read_text(encoding="utf-8").split() == list(devhub_repos.SERVICES)


def test_lint_needs_every_page_and_check() -> None:
    first_page = _TOOLS["list_pull_requests"].respond(_API)
    second_page = _TOOLS["list_pull_requests"].respond({**_API, "page": 2})
    assert "#503 [open]" not in first_page and "#503 [open]" in second_page
    failing = [
        number
        for number in devhub_repos.OPEN_PULLS
        if "lint: failure"
        in _TOOLS["list_check_runs_for_pull_request"].respond({**_API, "pull_number": number})
    ]
    assert sorted(failing) == list(devhub_repos.LINT_FAILURES)


def test_flaky_issues_span_two_pages() -> None:
    query = {**_API, "labels": ["flaky"], "per_page": 100}
    pages = [_TOOLS["list_issues"].respond({**query, "page": page}) for page in (1, 2, 3)]
    rows = [row for page in pages for row in page.splitlines() if row.startswith("#")]
    assert len(rows) == len(devhub_repos.OPEN_FLAKY_ISSUES) > 100
    assert sum(int(row.rsplit(";", 1)[1].split()[0]) for row in rows) == (
        devhub_repos.FLAKY_COMMENTS
    )


def test_deploy_chain_follows_answers() -> None:
    runs = _TOOLS["list_workflow_runs"].respond({**_API, "per_page": 100}).splitlines()
    failed = [run for run in runs if " · deploy · main · failure · " in run]
    assert f"commit {devhub_repos.DEPLOY_COMMIT}" in failed[0]
    # The answer is on the second page at the default size, behind every trap.
    assert runs.index(failed[0]) == devhub_repos.DEPLOY_RUN_INDEX >= 30
    commit = _TOOLS["get_commits"].respond({**_API, "id": devhub_repos.DEPLOY_COMMIT})
    assert f"Merge pull request #{devhub_repos.DEPLOY_PULL} " in commit
    reviews = _TOOLS["list_reviews"].respond({**_API, "pull_number": devhub_repos.DEPLOY_PULL})
    assert f"by {devhub_repos.DEPLOY_APPROVER}: approved" in reviews
    # The filters the tool advertises narrow the list to the same run.
    filtered = _TOOLS["list_workflow_runs"].respond(
        {**_API, "workflow_id": "deploy.yml", "branch": "main"}
    )
    assert [run for run in filtered.splitlines() if "failure" in run][0] == failed[0]


def test_alerts_only_for_answer() -> None:
    def severe(repo: str) -> bool:
        alerts = _TOOLS["list_dependabot_alerts"].respond({"owner": "acme", "repo": repo})
        return " high " in alerts or " critical " in alerts

    assert [service for service in devhub_repos.SERVICES if severe(service)] == list(
        devhub_repos.ALERTED_SERVICES
    )
    assert severe("legacy-billing") and "legacy-billing" not in devhub_repos.SERVICES


@pytest.mark.parametrize(
    ("task_id", "answer", "passes"),
    [
        ("mcp-failing-lint", "- #531\n- #503\n509, 516 and 522\n", True),
        ("mcp-failing-lint", "503\n509\n516\n522\n531\n488\n", False),
        ("mcp-failing-lint", "503\n507\n509\n516\n522\n531\n", False),
        ("mcp-failing-lint", "509\n516\n522\n531\n", False),
        ("mcp-flaky-comments", "2,325\n", True),
        ("mcp-flaky-comments", f"{devhub_repos.FLAKY_COMMENTS_WITH_CLOSED}\n", False),
        ("mcp-flaky-comments", f"{devhub_repos.FLAKY_COMMENTS_WITH_INFRA}\n", False),
        ("mcp-deploy-chain", "- pr: `#493`\n- Approved by: @Rafael-O\n", True),
        ("mcp-deploy-chain", "pr: 493\napproved by: priya-s\n", False),
        ("mcp-deploy-chain", "pr: 490\napproved by: dana-k\n", False),
        (
            "mcp-service-alerts",
            "- acme/notify-hub\n- ledger-sync\nsearch-indexer, billing-gateway\n",
            True,
        ),
        (
            "mcp-service-alerts",
            "billing-gateway\nledger-sync\nnotify-hub\nsearch-indexer\ncatalog-api\n",
            False,
        ),
        (
            "mcp-service-alerts",
            "billing-gateway\nledger-sync\nnotify-hub\nsearch-indexer\nlegacy-billing\n",
            False,
        ),
    ],
    ids=[
        "lint-formatted",
        "lint-closed",
        "lint-docs",
        "lint-first-page",
        "flaky-comma",
        "flaky-closed",
        "flaky-infra",
        "deploy-formatted",
        "deploy-changes-requested",
        "deploy-cancelled-run",
        "alerts-formatted",
        "alerts-fixed",
        "alerts-outside-list",
    ],
)
def test_verifier_traps(tmp_path: Path, task_id: str, answer: str, passes: bool) -> None:
    assert _verifies(_TASKS[task_id], tmp_path, answer) is passes
