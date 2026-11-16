"""The structured results devhub's task tools return when served with output schemas.

`serve.py --structured` declares each of these as its tool's output schema and returns
the value beside the tool's text, which stays exactly what the text-only server sends.
Field names follow the code-hosting API the catalog imitates. Nothing here may import
Wisp or the MCP SDK.
"""

from __future__ import annotations

from typing import TypedDict


class Issue(TypedDict):
    number: int
    state: str
    title: str
    labels: list[str]
    comments: int


class IssueList(TypedDict):
    issues: list[Issue]


class IssueDetail(Issue):
    opened_by: str


class PullRequest(TypedDict):
    number: int
    state: str
    title: str


class PullRequestList(TypedDict):
    pull_requests: list[PullRequest]


class PullRequestDetail(PullRequest):
    base: str
    body: str


class SearchResult(TypedDict):
    kind: str
    number: int
    state: str
    title: str


class SearchResults(TypedDict):
    results: list[SearchResult]


class CheckRun(TypedDict):
    name: str
    conclusion: str


class CheckRunList(TypedDict):
    check_runs: list[CheckRun]


class WorkflowRun(TypedDict):
    id: int
    workflow: str
    branch: str
    conclusion: str
    commit: str
    started: str


class WorkflowRunList(TypedDict):
    workflow_runs: list[WorkflowRun]


class Commit(TypedDict):
    sha: str
    author: str
    branch: str
    message: str


class Review(TypedDict):
    id: int
    user: str
    state: str


class ReviewList(TypedDict):
    reviews: list[Review]


class DependabotAlert(TypedDict):
    number: int
    state: str
    severity: str
    package: str
    ecosystem: str
    summary: str


class DependabotAlertList(TypedDict):
    alerts: list[DependabotAlert]
