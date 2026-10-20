"""Real repositories exercise safe cumulative diff snapshots."""

import subprocess
import time
from pathlib import Path
from threading import Event

import pytest

from wisp.rpc.handlers.git_diff import GitReadUnavailable, _git, collect_git_diff
from wisp.tools.context import ToolContext


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "file.py").write_text("original\n")
    git(tmp_path, "add", "file.py")
    git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def snapshot(root: Path):
    return collect_git_diff(ToolContext(cwd=root), Event(), "diff-1", 1)


def test_cumulative_staged_unstaged_deleted_and_untracked(repo: Path) -> None:
    assert not snapshot(repo).files
    (repo / "file.py").write_text("staged\n")
    git(repo, "add", "file.py")
    (repo / "file.py").write_text("working\n")
    (repo / "new.txt").write_text("not included\n")
    files = {f.path: f for f in snapshot(repo).files}
    assert files["file.py"].before == "original\n"
    assert files["file.py"].after == "working\n"
    assert files["new.txt"].status == "untracked"
    assert files["new.txt"].before == files["new.txt"].after == ""
    (repo / "file.py").write_text("original\n")
    assert all(f.path != "file.py" for f in snapshot(repo).files)
    (repo / "file.py").unlink()
    deleted = next(f for f in snapshot(repo).files if f.path == "file.py")
    assert deleted.status == "deleted" and deleted.after == ""


def test_protected_and_symlink_paths_do_not_expose_content(repo: Path) -> None:
    (repo / ".env").write_text("SECRET\n")
    git(repo, "add", ".env")
    git(repo, "commit", "-m", "secret")
    (repo / ".env").write_text("NEW SECRET\n")
    (repo / "alias").symlink_to(repo / ".env")
    report = snapshot(repo)
    assert all(f.path not in {".env", "alias"} for f in report.files)
    assert "SECRET" not in report.model_dump_json()
    (repo / "file.py").unlink()
    (repo / "file.py").symlink_to(repo / ".env")
    assert all(f.path != "file.py" for f in snapshot(repo).files)


def test_git_configured_helpers_never_run(repo: Path) -> None:
    marker = repo / "EXECUTED"
    hook = repo / "hook.sh"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
    hook.chmod(0o755)
    git(repo, "config", "diff.external", str(hook))
    git(repo, "config", "diff.bad.textconv", str(hook))
    git(repo, "config", "filter.bad.clean", str(hook))
    git(repo, "config", "core.fsmonitor", str(hook))
    (repo / ".gitattributes").write_text("*.py diff=bad filter=bad\n")
    (repo / "file.py").write_text("replaced\n")
    report = snapshot(repo)
    assert all(f.path != "file.py" for f in report.files)
    assert report.truncated
    assert not marker.exists()


def test_binary_oversized_and_unsafe_names_are_bounded(repo: Path) -> None:
    (repo / "file.py").write_bytes(b"\x00binary")
    assert next(f for f in snapshot(repo).files if f.path == "file.py").notice
    (repo / "file.py").write_text("x" * 60_000)
    entry = next(f for f in snapshot(repo).files if f.path == "file.py")
    assert entry.notice and entry.after == ""
    (repo / "bad\nname").write_text("x")
    report = snapshot(repo)
    assert report.truncated and all("\n" not in f.path for f in report.files)
    with pytest.raises(GitReadUnavailable):
        _git(repo, ["show", "HEAD:file.py"], Event(), time.monotonic() + 3, 2)


def test_nested_scope_does_not_read_outside_cwd(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / "local.py").write_text("local\n")
    (repo / "file.py").write_text("outside\n")
    assert {f.path for f in snapshot(repo / "sub").files} == {"sub/local.py"}


def test_host_correlates_diff_and_gates_policy(repo: Path) -> None:
    import anyio

    from tests.rpc.handlers.test_project_files import running_host, wait_finished
    from wisp.events import RpcGitDiffReported

    (repo / "file.py").write_text("changed\n")

    async def scenario() -> None:
        async with running_host(repo) as (host, command, events):
            invalidated = host.project_files.begin_policy_transition()
            host.project_files.reserve_protected_paths((str(repo / "file.py"),))
            await command(type="get_git_diff", id="diff")
            await command(type="get_state", id="state")
            await wait_finished(events, "state")
            assert not any(isinstance(e, RpcGitDiffReported) for e in events)
            host.project_files.settle_policy(host.agent.tool_context)
            assert (await wait_finished(events, "diff")).ok
            report = next(e for e in events if isinstance(e, RpcGitDiffReported))
            assert report.command_id == "diff" and report.generation == invalidated.generation
            assert all(f.path != "file.py" for f in report.files)

    anyio.run(scenario)


def test_owner_executable_mode_change_is_reported(repo: Path) -> None:
    file = repo / "file.py"
    file.chmod(0o755)
    git(repo, "add", "file.py")
    git(repo, "commit", "-m", "executable")
    file.chmod(0o655)
    report = snapshot(repo)
    entry = next(f for f in report.files if f.path == "file.py")
    assert "Executable permission changed" in entry.notice


def test_clean_smudged_file_is_omitted_without_running_filter(repo: Path) -> None:
    git(repo, "config", "filter.norm.clean", "sed s/WORK/CLEAN/g")
    (repo / ".gitattributes").write_text("*.py filter=norm\n")
    (repo / "file.py").write_text("WORK\n")
    git(repo, "add", ".gitattributes", "file.py")
    git(repo, "commit", "-m", "normalized")
    report = snapshot(repo)
    assert report.truncated
    assert all(f.path != "file.py" for f in report.files)


@pytest.mark.parametrize("attributes", ["*.py text eol=crlf", "*.py ident", ""])
def test_builtin_normalizations_do_not_report_clean_files(repo: Path, attributes: str) -> None:
    (repo / ".gitattributes").write_text(attributes + "\n")
    if not attributes:
        git(repo, "config", "core.autocrlf", "true")
    original = "$Id$\n" if "ident" in attributes else "first\nsecond\n"
    (repo / "file.py").write_text(original)
    git(repo, "add", ".gitattributes", "file.py")
    git(repo, "commit", "-m", "attributes")
    working = "$Id: abc123 $\n" if "ident" in attributes else "first\r\nsecond\r\n"
    (repo / "file.py").write_bytes(working.encode())
    assert all(f.path != "file.py" for f in snapshot(repo).files)


def test_deleted_filtered_file_remains_visible(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.py filter=norm\n")
    (repo / "file.py").unlink()
    entry = next(f for f in snapshot(repo).files if f.path == "file.py")
    assert entry.status == "deleted" and entry.before == "original\n"


def test_attribute_change_marks_uncompared_files_partial(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.py filter=norm\n")
    git(repo, "config", "filter.norm.clean", "sed s/original/normalized/g")
    report = snapshot(repo)
    assert report.truncated
    assert all(f.path != "file.py" for f in report.files)


def test_filemode_false_ignores_permission_only_change(repo: Path) -> None:
    git(repo, "config", "core.filemode", "false")
    (repo / "file.py").chmod(0o755)
    assert not snapshot(repo).files


def test_sparse_checkout_does_not_report_unmaterialized_files_as_deleted(repo: Path) -> None:
    for folder in ("inside", "outside"):
        (repo / folder).mkdir()
        (repo / folder / "file.py").write_text("original\n")
    git(repo, "add", "inside", "outside")
    git(repo, "commit", "-m", "directories")
    git(repo, "sparse-checkout", "init", "--cone")
    git(repo, "sparse-checkout", "set", "inside")
    assert not (repo / "outside" / "file.py").exists()
    assert not snapshot(repo).files
    (repo / "inside" / "file.py").write_text("changed\n")
    assert {file.path for file in snapshot(repo).files} == {"inside/file.py"}


@pytest.mark.parametrize("contents", ["original\n", "changed\n"])
def test_index_removal_and_remaining_untracked_file_are_distinct(repo: Path, contents: str) -> None:
    git(repo, "rm", "--cached", "file.py")
    (repo / "file.py").write_text(contents)
    entries = [file for file in snapshot(repo).files if file.path == "file.py"]
    assert [file.status for file in entries] == ["deleted", "untracked"]
    assert entries[0].before == "original\n" and entries[0].after == ""
    assert entries[1].before == entries[1].after == ""
