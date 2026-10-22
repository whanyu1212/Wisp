"""Real repositories exercise safe cumulative diff snapshots."""

import subprocess
import time
from pathlib import Path
from threading import Event

import anyio
import pytest

from wisp.events import RpcGitDiffEdit, ToolCallSnapshot
from wisp.messages import Message
from wisp.rpc.handlers.git_diff import (
    GitReadUnavailable,
    _git,
    collect_git_diff,
    session_edits,
)
from wisp.sessions import JsonlSession, JsonlSessionStore, MessageSessionEntry
from wisp.sessions.entries import ToolResultPresentationSnapshot
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


def test_sparse_checkout_preserves_staged_out_of_pattern_edits(repo: Path) -> None:
    for folder in ("inside", "outside"):
        (repo / folder).mkdir()
        (repo / folder / "file.py").write_text("original\n")
    git(repo, "add", "inside", "outside")
    git(repo, "commit", "-m", "directories")
    (repo / "outside" / "file.py").write_text("staged\n")
    (repo / "outside" / "added.py").write_text("added\n")
    git(repo, "add", "outside")
    git(repo, "sparse-checkout", "init", "--cone")
    git(repo, "sparse-checkout", "set", "inside")
    assert not (repo / "outside" / "file.py").exists()
    entries = {file.path: file for file in snapshot(repo).files}
    assert set(entries) == {"outside/file.py", "outside/added.py"}
    assert entries["outside/file.py"].before == "original\n"
    assert entries["outside/file.py"].after == "staged\n"
    assert entries["outside/added.py"].status == "added"
    assert entries["outside/added.py"].after == "added\n"


def test_filemode_false_preserves_staged_permission_change(repo: Path) -> None:
    git(repo, "config", "core.filemode", "false")
    git(repo, "update-index", "--chmod=+x", "file.py")
    entry = next(file for file in snapshot(repo).files if file.path == "file.py")
    assert "Executable permission changed" in entry.notice


def test_deleted_empty_file_remains_visible(repo: Path) -> None:
    (repo / "empty").touch()
    git(repo, "add", "empty")
    git(repo, "commit", "-m", "empty")
    (repo / "empty").unlink()
    assert [(file.path, file.status) for file in snapshot(repo).files] == [("empty", "deleted")]


def edit_call(call_id: str, name: str, path: str) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=(ToolCallSnapshot(call_id=call_id, name=name, arguments={"path": path}),),
    )


def tool_result(call_id: str, name: str, *, error: bool = False) -> Message:
    return Message(
        role="tool", content="done", tool_call_id=call_id, tool_name=name, is_error=error
    )


def ran_in(cwd: Path, messages: list[Message]) -> list[MessageSessionEntry]:
    """Session entries for `messages`, each tool result recording the folder it ran in."""
    return [
        MessageSessionEntry(
            session_id="s",
            message=message,
            tool_result=(
                ToolResultPresentationSnapshot(cwd=str(cwd)) if message.role == "tool" else None
            ),
        )
        for message in messages
    ]


def calls(edits: tuple[RpcGitDiffEdit, ...]) -> list[tuple[str, str]]:
    return [(edit.call_id, edit.tool) for edit in edits]


def saved_session(directory: Path, entries: list[MessageSessionEntry]) -> JsonlSession:
    """Write the entries to a session file and reopen it, as resuming does."""
    store = JsonlSessionStore(directory)
    session = store.create()

    async def append() -> None:
        for entry in entries:
            await session.append_entry(entry.model_copy(update={"session_id": session.session_id}))

    anyio.run(append)
    return store.load(session.path)


def test_marks_only_wisp_edits(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    for name in ("external.py", "failed.py"):
        (repo / name).write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "more")
    session = saved_session(
        tmp_path_factory.mktemp("sessions"),
        ran_in(
            repo,
            [
                edit_call("edit-1", "edit", "file.py"),
                tool_result("edit-1", "edit"),
                edit_call("write-1", "write", str(repo / "new.py")),
                tool_result("write-1", "write"),
                edit_call("edit-2", "edit", "failed.py"),
                tool_result("edit-2", "edit", error=True),
                edit_call("read-1", "read", "external.py"),
                tool_result("read-1", "read"),
                edit_call("edit-3", "edit", "file.py"),
                tool_result("edit-3", "edit"),
            ],
        ),
    )
    # Wisp's edits land, then the same file and others change outside the session.
    (repo / "file.py").write_text("by wisp\nand by hand\n")
    (repo / "new.py").write_text("written\n")
    (repo / "external.py").write_text("by hand\n")
    (repo / "failed.py").write_text("by hand\n")
    context = ToolContext(cwd=repo)
    edits = session_edits(session.read_active_path(), context)
    files = {f.path: f for f in collect_git_diff(context, Event(), "d", 1, edits).files}
    assert calls(files["file.py"].edits) == [("edit-1", "edit"), ("edit-3", "edit")]
    assert calls(files["new.py"].edits) == [("write-1", "write")]
    assert files["external.py"].edits == ()
    assert files["failed.py"].edits == ()
    # Each edit names the session entry holding its result.
    results = {
        entry.message.tool_call_id: entry.id
        for entry in session.read_active_path()
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "tool"
    }
    assert files["new.py"].edits[0].entry_id == results["write-1"]


def test_edit_paths_resolve_from_cwd(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / "local.py").write_text("local\n")
    entries = ran_in(
        repo / "sub",
        [
            edit_call("inside", "write", "local.py"),
            tool_result("inside", "write"),
            edit_call("escape", "write", "../file.py"),
            tool_result("escape", "write"),
        ],
    )
    context = ToolContext(cwd=repo / "sub")
    edits = session_edits(entries, context)
    assert list(edits) == [repo / "sub" / "local.py"]
    report = collect_git_diff(context, Event(), "d", 1, edits)
    assert [f.edits[0].call_id for f in report.files] == ["inside"]


def test_other_project_marks_nothing(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    # A session resumed here from another project: its relative paths name that
    # project's files, not this one's.
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    entries = ran_in(
        elsewhere, [edit_call("edit-1", "edit", "file.py"), tool_result("edit-1", "edit")]
    )
    (repo / "file.py").write_text("changed\n")
    context = ToolContext(cwd=repo)
    report = collect_git_diff(context, Event(), "d", 1, session_edits(entries, context))
    assert [(f.path, f.edits) for f in report.files] == [("file.py", ())]


def test_unrecorded_folder_marks_nothing(repo: Path) -> None:
    messages = [edit_call("edit-1", "edit", "file.py"), tool_result("edit-1", "edit")]
    entries = [MessageSessionEntry(session_id="s", message=m) for m in messages]
    assert session_edits(entries, ToolContext(cwd=repo)) == {}


def test_unresolvable_path_is_skipped(repo: Path) -> None:
    entries = ran_in(
        repo,
        [
            edit_call("home", "write", "~nosuchuser-wisp/x.py"),
            tool_result("home", "write"),
            edit_call("ok", "edit", "file.py"),
            tool_result("ok", "edit"),
        ],
    )
    assert list(session_edits(entries, ToolContext(cwd=repo))) == [repo / "file.py"]


def test_reused_call_ids_stay_distinct(repo: Path) -> None:
    (repo / "other.py").write_text("x\n")
    both = Message(
        role="assistant",
        content="",
        tool_calls=(
            ToolCallSnapshot(call_id="same", name="edit", arguments={"path": "file.py"}),
            ToolCallSnapshot(call_id="same", name="write", arguments={"path": "other.py"}),
        ),
    )
    entries = ran_in(
        repo,
        [
            both,
            tool_result("same", "edit"),
            tool_result("same", "write"),
            edit_call("same", "edit", "file.py"),
            tool_result("same", "edit", error=True),
        ],
    )
    edits = session_edits(entries, ToolContext(cwd=repo))
    # Results pair with calls in order, and each edit names its own result entry,
    # so a reused call ID still finds the right card.
    assert edits[repo / "file.py"][0].entry_id == entries[1].id
    assert edits[repo / "other.py"][0].entry_id == entries[2].id
    assert calls(edits[repo / "file.py"]) == [("same", "edit")]


def test_host_reports_session_edits(repo: Path) -> None:
    from tests.rpc.handlers.test_project_files import running_host, wait_finished
    from wisp.events import RpcGitDiffReported
    from wisp.rpc.coordinator import _RpcSessionState

    (repo / "file.py").write_text("changed\n")

    async def scenario() -> None:
        async with running_host(repo) as (host, command, events):
            session = host.sessions.create()
            for entry in ran_in(
                repo, [edit_call("edit-1", "edit", "file.py"), tool_result("edit-1", "edit")]
            ):
                await session.append_entry(
                    entry.model_copy(update={"session_id": session.session_id})
                )
            host.coordinator.session_state = _RpcSessionState(session, (), 2)
            await command(type="get_git_diff", id="diff")
            assert (await wait_finished(events, "diff")).ok
            report = next(e for e in events if isinstance(e, RpcGitDiffReported))
            changed = next(f for f in report.files if f.path == "file.py")
            assert [e.call_id for e in changed.edits] == ["edit-1"]

    anyio.run(scenario)


def test_marks_survive_compaction(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    from wisp.messages import CompactionRecord
    from wisp.sessions import CompactionSessionEntry

    session = saved_session(
        tmp_path_factory.mktemp("sessions"),
        ran_in(
            repo,
            [
                Message(role="user", content="edit file.py"),
                edit_call("edit-1", "edit", "file.py"),
                tool_result("edit-1", "edit"),
                Message(role="assistant", content="Done."),
                Message(role="user", content="thanks"),
                Message(role="assistant", content="Welcome."),
            ],
        ),
    )
    path = session.read_active_path()
    replaced = tuple(entry.id for entry in path[:4])

    async def compact() -> None:
        await session.append_compaction_entry(
            CompactionSessionEntry(
                session_id=session.session_id,
                parent_id=path[-1].id,
                compaction=CompactionRecord(
                    summary="edited file.py", replaced_entry_ids=replaced, provider="fake"
                ),
            ),
            expected_context_entry_ids=tuple(entry.id for entry in path),
        )

    anyio.run(compact)
    assert not any(m.tool_calls for m in session.read_context_messages())
    edits = session_edits(session.read_active_path(), ToolContext(cwd=repo))
    assert calls(edits[repo / "file.py"]) == [("edit-1", "edit")]
