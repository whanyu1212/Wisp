"""Repositories behind the devhub catalog's `orchestration` tasks.

`devhub.py` serves acme/web for `mcp-triage`. The repositories here are larger, so each
task needs many calls, or calls that depend on earlier answers: work a script could do
in one tool call and a model otherwise does over many turns. Each `respond` function
returns None for a repository it does not hold, and devhub then answers as it always
has, so acme/web and the catalog's definitions stay unchanged.

Every value comes from `hashlib`, not `hash()`: the server runs in its own process, and
the answers below must be what it serves. Nothing here may import Wisp or the MCP SDK.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from evals.tool_use.servers import devhub_large_results
from evals.tool_use.servers import devhub_results as results
from evals.tool_use.servers.model import Arguments, Reply

T = TypeVar("T")

API_OWNER = "acme"
API_REPO = "api"


def _digest(*parts: object) -> bytes:
    return hashlib.sha256("/".join(str(part) for part in parts).encode()).digest()


def _pick(options: Sequence[T], *parts: object) -> T:
    return options[_digest(*parts)[0] % len(options)]


def _repo(arguments: Arguments) -> tuple[str, str]:
    return (
        str(arguments.get("owner", "")).strip().lower(),
        str(arguments.get("repo", "")).strip().lower(),
    )


def _is_api(arguments: Arguments) -> bool:
    return _repo(arguments) == (API_OWNER, API_REPO)


def _int(value: object) -> int | None:
    # The server's schema has already turned numeric strings into integers.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _page(items: Sequence[T], arguments: Arguments) -> Sequence[T]:
    """The page of ``items`` that `page` and `per_page` select, as devhub pages."""

    per_page = max(min(_int(arguments.get("per_page")) or 30, 100), 1)
    page = max(_int(arguments.get("page")) or 1, 1)
    start = (page - 1) * per_page
    return items[start : start + per_page]


def _filters(arguments: Arguments) -> dict[str, str]:
    raw = arguments.get("filters")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value).strip() for key, value in raw.items() if value is not None}


_PEOPLE = ("dana-k", "jun-w", "lea-m", "omar-b", "priya-s", "rafael-o", "sam-t", "yuki-n")
_TOPICS = (
    "rate limiter",
    "refund retries",
    "webhook signatures",
    "invoice export",
    "token refresh",
    "audit log",
    "pagination cursors",
    "currency rounding",
    "request tracing",
    "health checks",
    "cache headers",
    "error codes",
)

# --- mcp-failing-lint: which open pull requests fail their `lint` check -------------

OPEN_PULLS = tuple(range(534, 500, -1))
CLOSED_PULLS = tuple(range(500, 469, -1))
# The answer. #503 sits on the second page of open pull requests at the default size.
LINT_FAILURES = (503, 509, 516, 522, 531)
# Traps, each a different wrong reading of the question.
LINT_FAILURE_CLOSED = 488
LINT_DOCS_FAILURE = 507
LINT_CANCELLED = 527
LINT_SKIPPED = 512


@dataclass(frozen=True, slots=True)
class _Pull:
    number: int
    state: str
    title: str
    body: str


def _pull(number: int) -> _Pull:
    state = "open" if number in OPEN_PULLS else "closed"
    topic = _pick(_TOPICS, "pull-topic", number)
    verb = _pick(("Fix", "Tidy", "Speed up", "Document", "Harden"), "pull-verb", number)
    return _Pull(number, state, f"{verb} {topic}", f"Touches the {topic} code. No API changes.")


def _checks(number: int) -> tuple[tuple[str, str], ...]:
    lint = "success"
    if number in LINT_FAILURES or number == LINT_FAILURE_CLOSED:
        lint = "failure"
    elif number == LINT_CANCELLED:
        lint = "cancelled"
    elif number == LINT_SKIPPED:
        lint = "skipped"
    checks = [
        ("build", "success"),
        ("unit-tests", "success"),
        ("lint", lint),
        ("e2e", "failure" if number in (505, 516, 530) else "success"),
    ]
    if number % 4 == 3 or number == LINT_DOCS_FAILURE:
        checks.append(("lint-docs", "failure" if number == LINT_DOCS_FAILURE else "success"))
    return tuple(checks)


def list_pull_requests(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    state = str(arguments.get("state") or "open").lower()
    numbers = {"open": OPEN_PULLS, "closed": CLOSED_PULLS}.get(state, OPEN_PULLS + CLOSED_PULLS)
    ordered = sorted(numbers, reverse=arguments.get("direction") != "asc")
    pulls = [_pull(number) for number in _page(ordered, arguments)]
    rows = [f"#{pull.number} [{pull.state}] {pull.title}" for pull in pulls]
    return Reply(
        "\n".join(rows) or "No pull requests match.",
        results.PullRequestList(
            pull_requests=[
                results.PullRequest(number=pull.number, state=pull.state, title=pull.title)
                for pull in pulls
            ]
        ),
    )


def get_pull_request(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    number = _int(arguments.get("pull_number"))
    if number not in OPEN_PULLS + CLOSED_PULLS:
        return Reply(f"Pull request #{number} not found.", None)
    pull = _pull(number)
    return Reply(
        f"#{pull.number} {pull.title}\nstate: {pull.state}\nbase: main\n\n{pull.body}",
        results.PullRequestDetail(
            number=pull.number, state=pull.state, title=pull.title, base="main", body=pull.body
        ),
    )


def list_check_runs(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    number = _int(arguments.get("pull_number"))
    if number not in OPEN_PULLS + CLOSED_PULLS:
        return Reply(f"Pull request #{number} not found.", None)
    return check_runs_reply(_checks(number))


def check_runs_reply(checks: Sequence[tuple[str, str]]) -> Reply:
    """A pull request's checks, as both devhub repositories list them."""

    return Reply(
        "\n".join(f"{name}: {conclusion}" for name, conclusion in checks),
        results.CheckRunList(
            check_runs=[
                results.CheckRun(name=name, conclusion=conclusion) for name, conclusion in checks
            ]
        ),
    )


# --- mcp-flaky-comments: total comments on open issues labelled `flaky` -------------


@dataclass(frozen=True, slots=True)
class IssueRecord:
    number: int
    state: str
    labels: tuple[str, ...]
    comments: int
    title: str


def _issue(number: int) -> IssueRecord:
    digest = _digest("issue", number)
    labels: list[str] = []
    if digest[1] % 5 < 3:
        labels.append("flaky")
    elif digest[2] % 4 == 0:
        # A label that contains `flaky` without being it.
        labels.append("flaky-infra")
    labels.append(_pick(("api", "billing", "auth", "ci"), "issue-area", number))
    return IssueRecord(
        number=number,
        state="closed" if digest[0] % 5 == 0 else "open",
        labels=tuple(labels),
        comments=digest[4] % 37,
        title=f"{_pick(_TOPICS, 'issue-topic', number).capitalize()} test fails intermittently",
    )


_ISSUES = tuple(_issue(number) for number in range(1001, 1261))


def _flaky_total(*, state: str = "open", labels: tuple[str, ...] = ("flaky",)) -> int:
    return sum(
        issue.comments
        for issue in _ISSUES
        if issue.state == state and any(label in issue.labels for label in labels)
    )


# The answer, and what common mistakes give instead.
FLAKY_COMMENTS = _flaky_total()
FLAKY_COMMENTS_WITH_CLOSED = FLAKY_COMMENTS + _flaky_total(state="closed")
FLAKY_COMMENTS_WITH_INFRA = _flaky_total(labels=("flaky", "flaky-infra"))
OPEN_FLAKY_ISSUES = tuple(
    issue.number for issue in _ISSUES if issue.state == "open" and "flaky" in issue.labels
)


def list_issues(arguments: Arguments) -> Reply | None:
    audit = devhub_large_results.list_issues(arguments)
    if audit is not None:
        return audit
    if not _is_api(arguments):
        return None
    state = str(arguments.get("state") or "open").lower()
    raw_labels = arguments.get("labels")
    wanted = {str(label).lower() for label in raw_labels} if isinstance(raw_labels, list) else set()
    matches = [
        issue
        for issue in _ISSUES
        if (state == "all" or issue.state == state) and wanted <= set(issue.labels)
    ]
    by: Callable[[IssueRecord], int] = (
        (lambda issue: issue.comments)
        if arguments.get("sort") == "comments"
        else (lambda issue: issue.number)
    )
    matches.sort(key=by, reverse=arguments.get("direction") != "asc")
    return issues_reply(_page(matches, arguments))


def issues_reply(issues: Sequence[IssueRecord]) -> Reply:
    """A page of issues, as both devhub repositories list them."""

    rows = [
        f"#{issue.number} [{issue.state}] {issue.title} — labels: {', '.join(issue.labels)}; "
        f"{issue.comments} comments"
        for issue in issues
    ]
    return Reply(
        "\n".join(rows) or "No issues match.",
        results.IssueList(issues=[_issue_value(issue) for issue in issues]),
    )


def issue_reply(issue: IssueRecord, opened_by: str) -> Reply:
    """One issue, as both devhub repositories show it."""

    return Reply(
        f"#{issue.number} {issue.title}\nstate: {issue.state}\n"
        f"labels: {', '.join(issue.labels)}\ncomments: {issue.comments}\nopened by: "
        f"{opened_by}",
        results.IssueDetail(**_issue_value(issue), opened_by=opened_by),
    )


def _issue_value(issue: IssueRecord) -> results.Issue:
    return results.Issue(
        number=issue.number,
        state=issue.state,
        title=issue.title,
        labels=list(issue.labels),
        comments=issue.comments,
    )


def get_issue(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    number = _int(arguments.get("issue_number"))
    issue = next((issue for issue in _ISSUES if issue.number == number), None)
    if issue is None:
        return Reply(f"Issue #{number} not found.", None)
    return issue_reply(issue, _pick(_PEOPLE, "issue-author", issue.number))


# --- mcp-deploy-chain: run, then commit, then pull request, then its approval -------

DEPLOY_WORKFLOW = "deploy"
DEPLOY_BRANCH = "main"
# The most recent failed deploy of main: the 42nd run, on the second page at the
# default size. It ran on the merge commit of #493, which rafael-o approved.
DEPLOY_RUN_INDEX = 41
DEPLOY_COMMIT = "9f3c2a1"
DEPLOY_PULL = 493
DEPLOY_APPROVER = "rafael-o"
# Newer runs that each look like the answer but are not: another branch, another
# workflow, a run that was cancelled rather than failed, a release branch.
_TRAP_RUNS = {
    3: ("deploy", "feature/rate-limits", "failure", 489),
    9: ("deploy-docs", "main", "failure", 491),
    17: ("deploy", "main", "cancelled", 490),
    26: ("deploy", "release/2.x", "failure", 486),
}
# Who reviewed the pull requests the runs merged. #493 also has a request for
# changes and a comment, and #490 an approval, so only reading #493 gives the answer.
_REVIEWS = {
    DEPLOY_PULL: (
        ("priya-s", "changes_requested"),
        (DEPLOY_APPROVER, "approved"),
        ("jun-w", "commented"),
    ),
    490: (("dana-k", "approved"),),
    491: (("lea-m", "approved"),),
    489: (("omar-b", "approved"),),
    486: (("sam-t", "approved"),),
}
_RUN_COUNT = 150
_NEWEST_RUN = 88150
_NEWEST_RUN_START = datetime(2026, 11, 9, 23, 40, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Run:
    id: int
    workflow: str
    branch: str
    conclusion: str
    commit: str
    started: str
    merged_pull: int | None


def _run(index: int) -> _Run:
    started = (_NEWEST_RUN_START - timedelta(minutes=37 * index)).strftime("%Y-%m-%dT%H:%MZ")
    run_id = _NEWEST_RUN - index
    if index == DEPLOY_RUN_INDEX:
        return _Run(
            run_id, DEPLOY_WORKFLOW, DEPLOY_BRANCH, "failure", DEPLOY_COMMIT, started, DEPLOY_PULL
        )
    commit = _digest("commit", index).hex()[:7]
    if index in _TRAP_RUNS:
        workflow, branch, conclusion, pull = _TRAP_RUNS[index]
        return _Run(run_id, workflow, branch, conclusion, commit, started, pull)
    workflow = _pick(("ci", "ci", "ci", "deploy", "deploy-docs", "nightly"), "workflow", index)
    branch = _pick(("main", "main", "release/2.x", "feature/audit-log"), "branch", index)
    conclusion = _pick(("success", "success", "success", "failure", "cancelled"), "result", index)
    if index < DEPLOY_RUN_INDEX and (workflow, branch, conclusion) == ("deploy", "main", "failure"):
        # Only the planted run may be the answer among the newer runs.
        conclusion = "success"
    merged = 470 + _digest("merged", index)[0] % 31 if branch == "main" else None
    return _Run(run_id, workflow, branch, conclusion, commit, started, merged)


_RUNS = tuple(_run(index) for index in range(_RUN_COUNT))


def _commit_author(run: _Run) -> str:
    if run.commit == DEPLOY_COMMIT:
        return "mira-t"
    return _pick(_PEOPLE, "commit-author", run.commit)


def list_workflow_runs(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    filters = _filters(arguments)
    workflow = str(arguments.get("workflow_id") or "").strip().lower().removesuffix(".yml")
    branch = str(arguments.get("branch") or "").strip()
    matches = [
        run
        for run in _RUNS
        if (not workflow or run.workflow == workflow)
        and (not branch or run.branch == branch)
        and run.started >= filters.get("since", "")
        and (not filters.get("until") or run.started < filters["until"])
        and (not filters.get("author") or _commit_author(run) == filters["author"])
    ]
    runs = _page(matches, arguments)
    rows = [
        f"run {run.id} · {run.workflow} · {run.branch} · {run.conclusion} · "
        f"commit {run.commit} · started {run.started}"
        for run in runs
    ]
    return Reply(
        "\n".join(rows) or "No workflow runs match.",
        results.WorkflowRunList(
            workflow_runs=[
                results.WorkflowRun(
                    id=run.id,
                    workflow=run.workflow,
                    branch=run.branch,
                    conclusion=run.conclusion,
                    commit=run.commit,
                    started=run.started,
                )
                for run in runs
            ]
        ),
    )


def get_commit(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    wanted = str(arguments.get("id", "")).strip().lower()
    run = next(
        (run for run in _RUNS if len(wanted) >= 7 and run.commit.startswith(wanted[:7])), None
    )
    if run is None:
        return Reply(f"Commit {wanted} not found in {API_OWNER}/{API_REPO}.", None)
    if run.merged_pull is not None:
        topic = _pull(run.merged_pull).title.split(" ", 1)[1].replace(" ", "-")
        message = f"Merge pull request #{run.merged_pull} from acme/{topic}"
    else:
        message = f"Update the {_pick(_TOPICS, 'commit-topic', run.commit)} code"
    author = _commit_author(run)
    return Reply(
        f"commit {run.commit}\nauthor: {author}\nbranch: {run.branch}\n\n{message}",
        results.Commit(sha=run.commit, author=author, branch=run.branch, message=message),
    )


def list_reviews(arguments: Arguments) -> Reply | None:
    if not _is_api(arguments):
        return None
    number = _int(arguments.get("pull_number"))
    if number not in OPEN_PULLS + CLOSED_PULLS:
        return Reply(f"Pull request #{number} not found.", None)
    reviews = _REVIEWS.get(number, ((_pick(_PEOPLE, "reviewer", number), "approved"),))
    page = _page(
        [
            results.Review(id=number * 10 + index, user=login, state=state)
            for index, (login, state) in enumerate(reviews, start=1)
        ],
        arguments,
    )
    return Reply(
        "\n".join(
            f"review {review['id']} by {review['user']}: {review['state']}" for review in page
        ),
        results.ReviewList(reviews=list(page)),
    )


# --- mcp-service-alerts: services from a local file, alerts from each repository ----

# The services the task's `services.txt` lists, one acme repository each.
SERVICES = (
    "auth-proxy",
    "billing-gateway",
    "catalog-api",
    "checkout-web",
    "email-relay",
    "feature-flags",
    "image-resizer",
    "inventory-sync",
    "ledger-sync",
    "metrics-agent",
    "notify-hub",
    "orders-api",
    "payments-core",
    "profile-store",
    "rates-cache",
    "search-indexer",
    "session-store",
    "shipping-quotes",
    "tax-engine",
    "user-directory",
)
# The answer: services with an open alert of high or critical severity.
ALERTED_SERVICES = ("billing-gateway", "ledger-sync", "notify-hub", "search-indexer")
# (state, severity, package, ecosystem, summary) per repository with fixed alerts.
_PLANTED_ALERTS = {
    "billing-gateway": (("open", "high", "requests", "pip", "Proxy credentials leak"),),
    "ledger-sync": (
        ("closed", "low", "tqdm", "pip", "Option injection"),
        ("open", "critical", "pyyaml", "pip", "Arbitrary code execution on load"),
    ),
    "notify-hub": (("open", "high", "axios", "npm", "Server-side request forgery"),),
    "search-indexer": (("open", "critical", "log4j-core", "maven", "Remote code execution"),),
    # Traps: an open medium alert, a fixed critical one, an open low one, and a
    # critical alert in a repository that is not in the team's list.
    "auth-proxy": (("open", "medium", "jsonwebtoken", "npm", "Weak key accepted"),),
    "catalog-api": (("closed", "critical", "lodash", "npm", "Prototype pollution"),),
    "image-resizer": (("open", "low", "pillow", "pip", "Denial of service on large files"),),
    "legacy-billing": (("open", "critical", "struts", "maven", "Remote code execution"),),
}
_PACKAGES = (
    ("urllib3", "pip"),
    ("express", "npm"),
    ("jackson-databind", "maven"),
    ("serde_yaml", "cargo"),
)


def _alerts(service: str) -> tuple[tuple[str, str, str, str, str], ...]:
    if service in _PLANTED_ALERTS:
        return _PLANTED_ALERTS[service]
    count = _digest("alerts", service)[0] % 3
    alerts = []
    for index in range(count):
        package, ecosystem = _pick(_PACKAGES, "alert-package", service, index)
        state = _pick(("open", "closed"), "alert-state", service, index)
        # Only the planted services may have an open high or critical alert.
        severity = _pick(
            ("low", "medium") if state == "open" else ("low", "high", "critical"),
            "alert-severity",
            service,
            index,
        )
        alerts.append((state, severity, package, ecosystem, "Outdated dependency"))
    return tuple(alerts)


def list_dependabot_alerts(arguments: Arguments) -> Reply | None:
    owner, repo = _repo(arguments)
    if owner != API_OWNER or (repo not in SERVICES and repo not in _PLANTED_ALERTS):
        return None
    state = str(arguments.get("state") or "open").lower()
    ecosystem = str(arguments.get("ecosystem") or "").strip().lower()
    alerts = _page(
        [
            results.DependabotAlert(
                number=index,
                state=alert_state,
                severity=severity,
                package=package,
                ecosystem=package_ecosystem,
                summary=summary,
            )
            for index, (alert_state, severity, package, package_ecosystem, summary) in enumerate(
                _alerts(repo), start=1
            )
            if (state == "all" or alert_state == state)
            and (not ecosystem or package_ecosystem == ecosystem)
        ],
        arguments,
    )
    rows = [
        f"alert {alert['number']} [{alert['state']}] {alert['severity']} · {alert['package']} "
        f"({alert['ecosystem']}) · {alert['summary']}"
        for alert in alerts
    ]
    return Reply(
        "\n".join(rows) or f"No dependency alerts for {owner}/{repo}.",
        results.DependabotAlertList(alerts=list(alerts)),
    )


# The tools these repositories answer, by devhub tool name, with the type of their
# structured result.
RESPONDERS: dict[str, tuple[Callable[[Arguments], Reply | None], type]] = {
    "list_pull_requests": (list_pull_requests, results.PullRequestList),
    "get_pull_request": (get_pull_request, results.PullRequestDetail),
    "list_check_runs_for_pull_request": (list_check_runs, results.CheckRunList),
    "list_issues": (list_issues, results.IssueList),
    "get_issue": (get_issue, results.IssueDetail),
    "list_workflow_runs": (list_workflow_runs, results.WorkflowRunList),
    "get_commits": (get_commit, results.Commit),
    "list_reviews": (list_reviews, results.ReviewList),
    "list_dependabot_alerts": (list_dependabot_alerts, results.DependabotAlertList),
}
