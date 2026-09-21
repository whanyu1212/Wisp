from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from wisp.rpc import protocol_history
from wisp.rpc.protocol_history import ArtifactChange, immutable_artifact_changes
from wisp.rpc.protocol_schema import modified_committed_protocol_artifacts

ROOT = "schemas/live-rpc"
OLD = f"{ROOT}/v8/events.schema.json"
CURRENT = f"{ROOT}/v9/events.schema.json"
NEW = f"{ROOT}/v10/events.schema.json"


@pytest.mark.parametrize(
    ("changes", "violations"),
    [
        ([ArtifactChange("modified", CURRENT)], ()),
        ([ArtifactChange("added", f"{ROOT}/v9/new.schema.json")], ()),
        ([ArtifactChange("added", NEW)], ()),
        ([ArtifactChange("modified", OLD)], (OLD,)),
        ([ArtifactChange("added", f"{ROOT}/v8/new.schema.json")], (f"{ROOT}/v8/new.schema.json",)),
        ([ArtifactChange("removed", OLD)], (OLD,)),
        ([ArtifactChange("removed", CURRENT)], (CURRENT,)),
        ([ArtifactChange("changed", CURRENT)], (CURRENT,)),
        ([ArtifactChange("renamed", NEW, OLD)], (NEW, OLD)),
        ([ArtifactChange("modified", CURRENT), ArtifactChange("added", NEW)], (CURRENT,)),
        ([ArtifactChange("modified", NEW)], (NEW,)),
        ([ArtifactChange("added", f"{ROOT}/v01/events.json")], (f"{ROOT}/v01/events.json",)),
        ([ArtifactChange("added", f"{ROOT}/v0/events.json")], (f"{ROOT}/v0/events.json",)),
        ([ArtifactChange("added", f"{ROOT}/future/events.json")], (f"{ROOT}/future/events.json",)),
        ([ArtifactChange("modified", "README.md")], ()),
    ],
)
def test_history_policy(changes: list[ArtifactChange], violations: tuple[str, ...]) -> None:
    assert immutable_artifact_changes(changes, [OLD, CURRENT]) == tuple(sorted(violations))


def test_missing_trusted_inventory_fails_closed() -> None:
    assert immutable_artifact_changes([ArtifactChange("modified", CURRENT)], ())


@pytest.mark.parametrize("stage", ["unstaged", "staged", "committed"])
def test_local_guard_checks_changes_before_and_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    monkeypatch.chdir(tmp_path)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    for path in [OLD, CURRENT]:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{}\n")
    git("add", ".")
    git("commit", "-qm", "trusted base")
    base = git("rev-parse", "HEAD")
    Path(CURRENT).write_text('{"additive": true}\n')
    assert modified_committed_protocol_artifacts(base) == ()
    Path(OLD).write_text('{"forbidden": true}\n')
    if stage in {"staged", "committed"}:
        git("add", ".")
    if stage == "committed":
        git("commit", "-qm", "change artifacts")
    assert modified_committed_protocol_artifacts(base) == (OLD,)
    Path(OLD).write_text("{}\n")
    if stage != "unstaged":
        git("add", OLD)
    assert modified_committed_protocol_artifacts(base) == ()
    # A new untracked historical artifact must not escape the local guard.
    untracked = Path(ROOT) / "v8" / "new.schema.json"
    untracked.write_text("{}\n")
    assert modified_committed_protocol_artifacts(base) == (untracked.as_posix(),)
    untracked.unlink()
    # Introducing the next version freezes the former current bundle too.
    Path(NEW).parent.mkdir()
    Path(NEW).write_text("{}\n")
    assert modified_committed_protocol_artifacts(base) == (CURRENT,)


@pytest.mark.parametrize(
    ("pages", "count", "success"),
    [
        ([[{"status": "modified", "filename": CURRENT}]], 1, True),
        ([[{"status": "modified", "filename": OLD}]], 1, False),
        (
            [[{"status": "modified", "filename": CURRENT}], [{"status": "added", "filename": NEW}]],
            2,
            False,
        ),
        ([[{"status": "modified", "filename": CURRENT}]], 2, False),
        ([[{"status": "modified", "filename": 123}]], 1, False),
        ([[{"status": "renamed", "filename": NEW, "previous_filename": OLD}]], 1, False),
        ({"not": "pages"}, 1, False),
        ([], 3001, False),
    ],
)
def test_trusted_standalone_guard_reads_only_metadata(
    tmp_path: Path, pages: object, count: int, success: bool
) -> None:
    for path in [OLD, CURRENT]:
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{}\n")
    metadata = tmp_path / "changes.json"
    metadata.write_text(json.dumps(pages))
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(Path(protocol_history.__file__).resolve()),
            "--changes-json",
            str(metadata),
            "--expected-files",
            str(count),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
