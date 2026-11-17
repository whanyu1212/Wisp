"""RPC scenarios use the ordinary eval isolation, scoring and result format."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO

import pytest

from evals.tool_use import runner
from evals.tool_use.credentials import Credentials
from evals.tool_use.runner import ModelSpec, RunContext, detect_build, run_one
from evals.tool_use.task_definitions import TaskError, load_task


def _task(tmp_path: Path, *, expectation: str = "fake response to: final") -> Path:
    folder = tmp_path / "scenario-demo"
    (folder / "repo").mkdir(parents=True)
    (folder / "repo" / "note.txt").write_text("fixture\n")
    (folder / "task.toml").write_text(f'''
id = "scenario-demo"
title = "RPC session transitions"
tags = ["scenario"]
verify = "true"
tools = ["read"]
opt_in = true
timeout_seconds = 30
[[scenario.steps]]
type = "prompt"
prompt = "first"
[[scenario.steps]]
type = "prompt"
prompt = "acknowledge"
[[scenario.steps]]
type = "checkpoint"
name = "base"
[[scenario.steps]]
type = "prompt"
prompt = "discarded branch"
[[scenario.steps]]
type = "navigate"
checkpoint = "base"
[[scenario.steps]]
type = "resume"
[[scenario.steps]]
type = "prompt"
prompt = "final"
expect_text = "{expectation}"
''')
    return folder


@pytest.mark.process
@pytest.mark.parametrize(
    "expectation,passed", [("fake response to: final", True), ("absent answer", False)]
)
def test_subprocess_scenario_is_scored(tmp_path: Path, expectation: str, passed: bool) -> None:
    task = load_task(_task(tmp_path, expectation=expectation))
    context = RunContext(
        build=detect_build(Path(__file__).resolve().parents[2]),
        credentials=Credentials(auth_file=tmp_path / "auth.json", sources={"fake": "none"}),
        results_dir=tmp_path / "results",
        effort="medium",
    )
    row = run_one(task, ModelSpec("fake", "fake"), 1, context)
    assert row.status == ("passed" if passed else "failed"), row.error or row.failed_checks
    assert row.scenario_steps_expected == 7
    assert row.turns == 4
    assert len(row.task_sha256) == len(row.fixture_sha256) == 64
    assert row.effort == "medium"
    run_dir = context.results_dir / row.run_dir
    config = json.loads((run_dir / "scenario-config.json").read_text())
    assert "--yes" not in config["command"]
    assert "--effort" not in config["command"]
    assert "rpc" in config["command"]
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(event["type"] == "rpc.session.selected" for event in events)
    assert any(event["type"] == "rpc.session.tree.navigated" for event in events)


@pytest.mark.process
def test_compaction_failure_stops_scenario(tmp_path: Path) -> None:
    folder = _task(tmp_path)
    definition = folder / "task.toml"
    definition.write_text(
        definition.read_text().replace(
            'type = "prompt"\nprompt = "final"',
            'type = "compact"\n[[scenario.steps]]\ntype = "prompt"\nprompt = "final"',
        )
    )
    context = RunContext(
        build=detect_build(Path(__file__).resolve().parents[2]),
        credentials=Credentials(auth_file=tmp_path / "auth.json", sources={"fake": "none"}),
        results_dir=tmp_path / "results",
    )
    row = run_one(load_task(folder), ModelSpec("fake", "fake"), 1, context)
    # The fake provider returns prose, not a valid compaction summary. The final
    # prompt must never run and make this failed transition appear successful.
    assert row.status == "failed"
    assert row.turns == 3
    assert any("compact" in check for check in row.failed_checks)
    assert row.scenario_steps_completed < row.scenario_steps_expected


def test_approval_stop_is_task_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = load_task(_task(tmp_path))

    def launch(
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdout: IO[str],
        stderr: IO[str],
        timeout: float,
    ) -> int:
        if command[0] != "bash":
            config = json.loads(Path(command[-1]).read_text())
            Path(config["result_path"]).write_text(
                json.dumps(
                    {
                        "completed_steps": 0,
                        "failed_checks": ["unexpected or mismatched approval for 'write'"],
                    }
                )
            )
        return 0

    monkeypatch.setattr(runner, "run_in_own_group", launch)
    context = RunContext(
        build=runner.Build(wisp=Path("wisp"), version="test", git_commit="test"),
        credentials=Credentials(auth_file=tmp_path / "auth.json"),
        results_dir=tmp_path / "results",
    )
    row = run_one(task, ModelSpec("fake", "fake"), 1, context)
    assert row.status == "failed"
    assert row.outcome is None  # The backend closes before agent.completed.
    assert "unexpected" in row.failed_checks[0]


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda text: 'prompt = "also"\n' + text, "exactly one"),
        (lambda text: text.replace("opt_in = true", "opt_in = false"), "must be opt_in"),
        (
            lambda text: text.replace('checkpoint = "base"', 'checkpoint = "missing"'),
            "unknown earlier checkpoint",
        ),
    ],
)
def test_invalid_scenario_is_rejected(
    tmp_path: Path, change: Callable[[str], str], error: str
) -> None:
    folder = _task(tmp_path)
    path = folder / "task.toml"
    path.write_text(change(path.read_text()))
    with pytest.raises(TaskError, match=error):
        load_task(folder)
