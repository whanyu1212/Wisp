"""A paginated release audit whose records exceed the old script result cap.

Long synthetic titles stand in for verbose audit evidence. The decisions at their
ends and the matching records late in each page must survive result transport.
Nothing here may import Wisp or the MCP SDK.
"""

from __future__ import annotations

from evals.tool_use.servers import devhub_results as results
from evals.tool_use.servers.model import Arguments, Reply

OWNER = "acme"
REPO = "release-audit"
BLOCKED_ISSUES = (3003, 3017, 3033)
_EVIDENCE = "Dependency checks, artifact signatures and rollback rehearsals recorded. " * 34


def _issue(number: int) -> results.Issue:
    blocked = number in (*BLOCKED_ISSUES, 3032, 3034, 3035)
    region = "us-east" if number == 3034 else "eu-west"
    return results.Issue(
        number=number,
        state="closed" if number == 3032 else "open",
        title=f"Release audit {number}: {_EVIDENCE}decision={'block' if blocked else 'pass'}",
        labels=["release-gate-candidate" if number == 3035 else "release-gate", f"region:{region}"],
        comments=number % 17,
    )


def list_issues(arguments: Arguments) -> Reply | None:
    """Return one audit page, or None for a repository handled elsewhere."""

    if (arguments.get("owner"), arguments.get("repo")) != (OWNER, REPO):
        return None
    state = arguments.get("state") or "open"
    raw_labels = arguments.get("labels")
    labels = set(raw_labels) if isinstance(raw_labels, list) else set()
    records = [_issue(number) for number in range(3060, 3000, -1)]
    issues = [
        issue
        for issue in records
        if (state == "all" or issue["state"] == state) and labels <= set(issue["labels"])
    ]
    issues.sort(
        key=lambda issue: (
            issue["comments"] if arguments.get("sort") == "comments" else issue["number"]
        ),
        reverse=arguments.get("direction") != "asc",
    )
    raw_size, raw_page = arguments.get("per_page"), arguments.get("page")
    size = max(1, min(100, raw_size if isinstance(raw_size, int) else 30))
    page = max(1, raw_page if isinstance(raw_page, int) else 1)
    selected = issues[(page - 1) * size : page * size]
    text = "\n".join(
        f"#{issue['number']} [{issue['state']}] {issue['title']} — "
        f"labels: {', '.join(issue['labels'])}; {issue['comments']} comments"
        for issue in selected
    )
    return Reply(text or "No issues match.", results.IssueList(issues=selected))
