"""A realistic synthetic catalog: about 90 tools of a code-hosting API.

Real MCP servers, such as GitHub's, describe each tool in a few sentences and give
it several typed, partly nested parameters, so a tool costs a few hundred tokens of
definition. The 50-tool inventory catalog is much smaller per tool, and measuring
on-demand discovery with it alone would understate the saving. A few tools here
carry the data of the `mcp-triage` task; the rest answer plausibly, so the model
has to choose by reading. Nothing here may import Wisp or the MCP SDK.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from evals.tool_use.servers.model import Arguments, Catalog, CatalogTool, Parameter

# The triage task: in acme/web, the open `regression` issue with the most comments,
# the open pull request that fixes it, and that pull request's failing check.
TRIAGE_OWNER = "acme"
TRIAGE_REPO = "web"
TRIAGE_ISSUE = 412
TRIAGE_PR = 418
TRIAGE_CHECK = "e2e-safari"

# (number, state, labels, comments, title). #405 has more comments but is closed;
# #420 has more but is not a regression.
_ISSUES = (
    (405, "closed", ("regression",), 30, "Login fails on Safari 17"),
    (412, "open", ("regression", "checkout"), 14, "Cart totals wrong after a second coupon"),
    (398, "open", ("regression",), 9, "Search results flicker while typing"),
    (420, "open", ("bug",), 22, "Footer overlaps the cart button on small screens"),
    (415, "open", ("enhancement",), 3, "Remember the last used shipping address"),
)
# (number, state, title, body). #421's title mentions a checkout regression but it
# fixes #398; closed #409 only refers to #412.
_PULLS = (
    (
        418,
        "open",
        "Restore cart totals with stacked coupons",
        "Fixes #412. Totals are recomputed after each coupon.",
    ),
    (421, "open", "Fix checkout regression in search", "Fixes #398 by debouncing the search box."),
    (419, "open", "Bump dependencies", "Routine dependency update."),
    (409, "closed", "Revert coupon rounding", "Refs #412; superseded."),
)
_CHECKS = {
    418: (
        ("build", "success"),
        ("unit-tests", "success"),
        ("e2e-safari", "failure"),
        ("e2e-chrome", "success"),
        ("lint", "success"),
    ),
    421: (
        ("build", "success"),
        ("unit-tests", "success"),
        ("e2e-chrome", "failure"),
        ("lint", "success"),
    ),
}


def _repo(arguments: Arguments) -> tuple[str, str]:
    return (
        str(arguments.get("owner", "")).strip().lower(),
        str(arguments.get("repo", "")).strip().lower(),
    )


def _is_triage_repo(arguments: Arguments) -> bool:
    return _repo(arguments) == (TRIAGE_OWNER, TRIAGE_REPO)


def _list_issues(arguments: Arguments) -> str:
    if not _is_triage_repo(arguments):
        return _missing_repo(arguments)
    state = str(arguments.get("state") or "open").lower()
    raw_labels = arguments.get("labels")
    labels = {str(label).lower() for label in raw_labels} if isinstance(raw_labels, list) else set()
    matches = [
        issue
        for issue in _ISSUES
        if (state == "all" or issue[1] == state) and labels <= set(issue[2])
    ]
    by = (
        (lambda issue: issue[3])
        if arguments.get("sort") == "comments"
        else (lambda issue: issue[0])
    )
    matches.sort(key=by, reverse=arguments.get("direction") != "asc")
    rows = [
        f"#{number} [{issue_state}] {title} — labels: {', '.join(issue_labels)}; "
        f"{comments} comments"
        for number, issue_state, issue_labels, comments, title in _page(matches, arguments)
    ]
    return "\n".join(rows) or "No issues match."


def _get_issue(arguments: Arguments) -> str:
    number = _int(arguments.get("issue_number"))
    for issue_number, state, labels, comments, title in _ISSUES:
        if _is_triage_repo(arguments) and number == issue_number:
            return (
                f"#{issue_number} {title}\nstate: {state}\nlabels: {', '.join(labels)}\n"
                f"comments: {comments}\nopened by: dana-k"
            )
    return f"Issue #{number} not found."


def _list_pulls(arguments: Arguments) -> str:
    if not _is_triage_repo(arguments):
        return _missing_repo(arguments)
    state = str(arguments.get("state") or "open").lower()
    matches = sorted(
        (pull for pull in _PULLS if state == "all" or pull[1] == state),
        key=lambda pull: pull[0],
        reverse=arguments.get("direction") != "asc",
    )
    rows = [
        f"#{number} [{pull_state}] {title}"
        for number, pull_state, title, _body in _page(matches, arguments)
    ]
    return "\n".join(rows) or "No pull requests match."


def _get_pull(arguments: Arguments) -> str:
    number = _int(arguments.get("pull_number"))
    for pull_number, state, title, body in _PULLS:
        if _is_triage_repo(arguments) and number == pull_number:
            return f"#{pull_number} {title}\nstate: {state}\nbase: main\n\n{body}"
    return f"Pull request #{number} not found."


def _search(arguments: Arguments) -> str:
    query = str(arguments.get("query", "")).lower()
    filters = arguments.get("filters")
    state = str(filters.get("state", "")).lower() if isinstance(filters, dict) else ""
    rows = [
        f"pull request #{number} [{pull_state}] {title}"
        for number, pull_state, title, body in _PULLS
        if query
        and (query in body.lower() or query in title.lower())
        and (not state or state == pull_state)
    ]
    rows += [
        f"issue #{number} [{issue_state}] {title}"
        for number, issue_state, _labels, _comments, title in _ISSUES
        if query and query in title.lower() and (not state or state == issue_state)
    ]
    return "\n".join(rows) or "No results."


def _check_runs(arguments: Arguments) -> str:
    number = _int(arguments.get("pull_number"))
    checks = _CHECKS.get(number) if number is not None and _is_triage_repo(arguments) else None
    if checks is None:
        return "build: success\nunit-tests: success\nlint: success"
    return "\n".join(f"{name}: {conclusion}" for name, conclusion in checks)


def _int(value: object) -> int | None:
    # The server's schema has already turned numeric strings into integers.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _page[T](items: list[T], arguments: Arguments) -> list[T]:
    """The page of ``items`` that `page` and `per_page` select, as a real API pages."""

    per_page = _int(arguments.get("per_page")) or 30
    page = _int(arguments.get("page")) or 1
    start = (max(page, 1) - 1) * max(min(per_page, 100), 1)
    return items[start : start + max(min(per_page, 100), 1)]


def _missing_repo(arguments: Arguments) -> str:
    owner, repo = _repo(arguments)
    return (
        f"Repository {owner}/{repo} not found. Pass the owner and the repository name separately."
    )


def _decoy(tool: str, noun: str) -> Callable[[Arguments], str]:
    """A plausible answer that never helps with the triage task."""

    def respond(arguments: Arguments) -> str:
        owner, repo = _repo(arguments)
        digest = hashlib.sha256(f"{tool}/{owner}/{repo}".encode()).digest()
        count = digest[0] % 4
        if tool.startswith(("create_", "update_", "delete_", "add_", "remove_")):
            return f"{noun} change for {owner}/{repo} recorded (dry run): nothing was changed."
        if count == 0:
            return f"No {noun} found for {owner}/{repo}."
        return "\n".join(
            f"{noun} {digest[index + 1] % 90 + 10}: updated {index + 1}d ago"
            for index in range(count)
        )

    return respond


_OWNER = Parameter(
    "owner", description="The account that owns the repository, such as an organisation name."
)
_REPO = Parameter("repo", description="The repository name, without the owner.")
_PAGING = (
    Parameter(
        "per_page", "integer", "Results per page, from 1 to 100. Defaults to 30.", required=False
    ),
    Parameter("page", "integer", "The page of results to return, starting at 1.", required=False),
)
_SORT = (
    Parameter("sort", description="What to sort by: created, updated or comments.", required=False),
    Parameter(
        "direction",
        description="The sort direction: asc or desc. Defaults to desc.",
        required=False,
    ),
)
_DATE_RANGE = Parameter(
    "filters",
    "object",
    "Optional filters that narrow the results.",
    required=False,
    fields=(
        Parameter(
            "since",
            description="Only results updated at or after this ISO 8601 time.",
            required=False,
        ),
        Parameter(
            "until", description="Only results updated before this ISO 8601 time.", required=False
        ),
        Parameter("author", description="Only results created by this user login.", required=False),
        Parameter("state", description="open, closed or all.", required=False),
    ),
)
_STATE = Parameter(
    "state", description="Which to return: open, closed or all. Defaults to open.", required=False
)
_BODY = Parameter("body", description="The text, in Markdown.", required=False)


def _number(name: str, what: str) -> Parameter:
    return Parameter(name, "integer", f"The number that identifies the {what} in its repository.")


# The tools the triage task needs, described as a real server would.
_TASK_TOOLS = (
    CatalogTool(
        "list_issues",
        "List issues in a repository. Pull requests are not included. Results can be "
        "filtered by state and labels and are sorted by creation time unless `sort` is "
        "given; each result shows the issue's number, state, title, labels and comment count.",
        _list_issues,
        (
            _OWNER,
            _REPO,
            _STATE,
            Parameter(
                "labels",
                "string_list",
                "Only issues that carry every one of these labels.",
                required=False,
            ),
            *_SORT,
            *_PAGING,
        ),
    ),
    CatalogTool(
        "get_issue",
        "Get one issue by its number, with its state, labels, comment count and author. "
        "Use list_issues or search to find the number.",
        _get_issue,
        (_OWNER, _REPO, _number("issue_number", "issue")),
    ),
    CatalogTool(
        "list_pull_requests",
        "List pull requests in a repository by state. Each result shows the number, "
        "state and title; get_pull_request returns a pull request's description.",
        _list_pulls,
        (
            _OWNER,
            _REPO,
            _STATE,
            Parameter("base", description="Only pull requests into this branch.", required=False),
            *_SORT,
            *_PAGING,
        ),
    ),
    CatalogTool(
        "get_pull_request",
        "Get one pull request by its number, with its state, base branch and description. "
        'The description often names the issues it fixes, as in "Fixes #123".',
        _get_pull,
        (_OWNER, _REPO, _number("pull_number", "pull request")),
    ),
    CatalogTool(
        "search_issues_and_pull_requests",
        "Search issue and pull request titles and descriptions across repositories. "
        "Matches are case-insensitive substrings of the query.",
        _search,
        (
            Parameter("query", description='The text to search for, such as "#123" or a phrase.'),
            _DATE_RANGE,
            *_PAGING,
        ),
    ),
    CatalogTool(
        "list_check_runs_for_pull_request",
        "List the checks that ran on a pull request's latest commit, each with its "
        "conclusion: success, failure, cancelled or skipped.",
        _check_runs,
        (_OWNER, _REPO, _number("pull_number", "pull request"), *_PAGING),
    ),
)

# (resource, noun, extra parameters) for decoy tools, each listed, fetched and
# changed the way real APIs offer them.
_RESOURCES: tuple[tuple[str, str, tuple[Parameter, ...]], ...] = (
    (
        "commits",
        "commit",
        (
            Parameter(
                "sha",
                description="A branch name or commit SHA to start listing from.",
                required=False,
            ),
            _DATE_RANGE,
        ),
    ),
    (
        "branches",
        "branch",
        (Parameter("protected", "boolean", "Only protected branches when true.", required=False),),
    ),
    (
        "releases",
        "release",
        (Parameter("prerelease", "boolean", "Include pre-releases when true.", required=False),),
    ),
    ("tags", "tag", ()),
    (
        "workflow_runs",
        "workflow run",
        (
            Parameter(
                "workflow_id", description="The workflow file name or numeric id.", required=False
            ),
            Parameter("branch", description="Only runs on this branch.", required=False),
            _DATE_RANGE,
        ),
    ),
    ("workflows", "workflow", ()),
    (
        "deployments",
        "deployment",
        (
            Parameter(
                "environment", description="Only deployments to this environment.", required=False
            ),
        ),
    ),
    ("environments", "environment", ()),
    ("labels", "label", ()),
    ("milestones", "milestone", (_STATE,)),
    (
        "discussions",
        "discussion",
        (Parameter("category", description="Only discussions in this category.", required=False),),
    ),
    (
        "collaborators",
        "collaborator",
        (
            Parameter(
                "permission",
                description="Only people with this permission: pull, push or admin.",
                required=False,
            ),
        ),
    ),
    ("webhooks", "webhook", ()),
    (
        "code_scanning_alerts",
        "code scanning alert",
        (
            _STATE,
            Parameter(
                "severity", description="critical, high, medium, low or note.", required=False
            ),
        ),
    ),
    (
        "dependabot_alerts",
        "dependency alert",
        (_STATE, Parameter("ecosystem", description="npm, pip, cargo and so on.", required=False)),
    ),
    ("secret_scanning_alerts", "secret scanning alert", (_STATE,)),
    (
        "packages",
        "package",
        (Parameter("package_type", description="npm, container, maven or nuget."),),
    ),
    (
        "project_items",
        "project item",
        (Parameter("project_number", "integer", "The project board's number."),),
    ),
    ("review_comments", "review comment", (_number("pull_number", "pull request"),)),
    ("reviews", "review", (_number("pull_number", "pull request"),)),
    ("issue_comments", "comment", (_number("issue_number", "issue"),)),
    ("issue_events", "issue event", (_number("issue_number", "issue"),)),
)
_VERBS = (
    (
        "list",
        "List the {plural} of a repository. Results are paginated and sorted newest first.",
        True,
    ),
    ("get", "Get one {noun} of a repository by its identifier, with all of its details.", False),
    ("create", "Create a {noun} in a repository. Needs write access to the repository.", False),
    ("update", "Change a {noun} in a repository. Only the fields you pass are changed.", False),
)


def _decoy_tools() -> tuple[CatalogTool, ...]:
    tools: list[CatalogTool] = []
    for resource, noun, extra in _RESOURCES:
        for verb, template, paginated in _VERBS:
            name = f"{verb}_{resource}"
            parameters = [_OWNER, _REPO, *extra]
            if verb in {"get", "update"}:
                parameters.append(Parameter("id", description=f"The {noun}'s identifier."))
            if verb in {"create", "update"}:
                parameters.append(
                    Parameter(
                        "title",
                        description=f"The {noun}'s title or name.",
                        required=verb == "create",
                    )
                )
                parameters.append(_BODY)
            if paginated:
                parameters.extend(_PAGING)
            description = template.format(
                noun=noun, plural=f"{noun}s" if not noun.endswith("h") else f"{noun}es"
            )
            tools.append(
                CatalogTool(
                    name,
                    f"{description} Rate limits apply per token.",
                    _decoy(name, noun),
                    tuple(parameters),
                )
            )
    for name, description, own_parameters in (
        (
            "get_repository",
            "Get a repository's settings, default branch, topics and visibility.",
            (_OWNER, _REPO),
        ),
        (
            "list_repositories",
            "List repositories an account owns, newest first.",
            (_OWNER, *_PAGING),
        ),
        (
            "get_file_contents",
            "Get a file's contents at a branch, tag or commit.",
            (
                _OWNER,
                _REPO,
                Parameter("path", description="The file's path from the repository root."),
                Parameter("ref", description="A branch, tag or commit SHA.", required=False),
            ),
        ),
        (
            "search_code",
            "Search code across repositories. Matches file contents and paths.",
            (Parameter("query", description="The text to search for."), _DATE_RANGE, *_PAGING),
        ),
        (
            "search_commits",
            "Search commit messages across repositories.",
            (Parameter("query", description="The text to search for."), _DATE_RANGE, *_PAGING),
        ),
        (
            "search_users",
            "Search users and organisations by login or name.",
            (Parameter("query", description="The text to search for."), *_PAGING),
        ),
        (
            "list_notifications",
            "List the signed-in user's notifications, unread first.",
            (
                Parameter(
                    "all", "boolean", "Include read notifications when true.", required=False
                ),
                *_PAGING,
            ),
        ),
        ("get_rate_limit", "Show how many requests remain in the current rate-limit window.", ()),
    ):
        tools.append(
            CatalogTool(name, description, _decoy(name, name.split("_", 1)[-1]), own_parameters)
        )
    return tuple(tools)


DEVHUB = Catalog(parameters=(), tools=(*_TASK_TOOLS, *_decoy_tools()))
