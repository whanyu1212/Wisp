"""Large MCP audit results retain late evidence and admit pagination recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.tool_use.servers import devhub_large_results as audit
from evals.tool_use.servers.catalogs import CATALOGS
from evals.tool_use.task_definitions import select_tasks

_TASK = select_tasks(ids=["mcp-large-result-filter"])[0]
_TOOL = next(tool for tool in CATALOGS["devhub"].tools if tool.name == "list_issues")
_ARGS: dict[str, object] = {"owner": audit.OWNER, "repo": audit.REPO}


def test_late_evidence_survives() -> None:
    structured = _TOOL.structured
    assert structured is not None
    reply = structured.reply(_ARGS)
    assert reply.value is not None
    assert reply.text == _TOOL.respond(_ARGS)
    encoded = json.dumps(reply.value).encode()
    assert 50_000 < len(reply.text.encode()) < 512 * 1024
    assert 50_000 < len(encoded) < 512 * 1024
    assert reply.text.encode().index(b"#3033 ") > 50_000
    assert encoded.index(b'"number": 3033') > 50_000
    # Even returning all records in both MCP representations fits the wire budget.
    largest = structured.reply({**_ARGS, "state": "all", "per_page": 100})
    assert len(json.dumps({"text": largest.text, "value": largest.value}).encode()) < 512 * 1024


@pytest.mark.parametrize("per_page", [10, 30, 100])
def test_pages_give_complete_answer(per_page: int) -> None:
    found: list[int] = []
    for page in range(1, 8):
        reply = _TOOL.respond({**_ARGS, "per_page": per_page, "page": page})
        if per_page == 10:
            assert len(reply.encode()) < 50_000
        for line in reply.splitlines():
            if (
                "decision=block — labels: release-gate, region:eu-west;" in line
                and "[open]" in line
            ):
                found.append(int(line.split()[0].removeprefix("#")))
    assert sorted(found) == list(audit.BLOCKED_ISSUES)


@pytest.mark.parametrize(
    ("answer", "passes"),
    [
        ("3003\n3017\n3033\n", True),
        ("- #3033\n- `3017`\n- 3003\n", True),
        ("3017\n3033\n", False),
        ("3003\n3017\n3033\n3032\n", False),
        ("3003\n3017\n3033\n3034\n", False),
        ("3003\n3017\n3033\n3035\n", False),
        ("3003\n3017\n3033\n9999\n", False),
        ("3003\n3017\n3033\nNo other issues\n", False),
        ("", False),
    ],
)
def test_verifier_rejects_traps(tmp_path: Path, answer: str, passes: bool) -> None:
    (tmp_path / "ANSWER.txt").write_text(answer, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_TASK.folder / "verify.py")], cwd=tmp_path, check=False
    )
    assert (result.returncode == 0) is passes


def test_task_is_opt_in() -> None:
    assert _TASK.definition.opt_in
    assert _TASK.id not in {task.id for task in select_tasks()}
    assert (_TASK.folder / "solution" / "ANSWER.txt").read_text().split() == [
        str(number) for number in audit.BLOCKED_ISSUES
    ]
