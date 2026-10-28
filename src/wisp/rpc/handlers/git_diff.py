"""Bounded Git object and secure working-file reads for interactive review."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Thread
from typing import Literal

from wisp.events import RpcGitDiffEdit, RpcGitDiffFile, RpcGitDiffReported
from wisp.events.rpc import MAX_GIT_DIFF_FILE_EDITS
from wisp.project_files import ProjectScanCancelled, ProjectScanTimedOut, is_display_safe_path
from wisp.sessions import MessageSessionEntry, SessionEntry
from wisp.tools.context import ToolContext
from wisp.tools.files.secure_fs import SecureToolPath, open_file, secure_tool_path
from wisp.tools.result import ToolError

_FILE_BYTES = 50_000
_REPORT_BYTES = 900_000


class GitReadUnavailable(Exception):
    """A Git read failed or exceeded its bounded output budget."""


def _git(
    root: Path,
    args: list[str],
    cancelled: Event,
    deadline: float,
    limit: int = 2_000_000,
    input_data: bytes = b"",
) -> bytes:
    # No shell, optional index writes, fsmonitor, pager, external diff, or textconv.
    # A reader caps retained bytes while the waiting worker owns cancellation.
    env = dict(
        os.environ,
        GIT_OPTIONAL_LOCKS="0",
        GIT_LITERAL_PATHSPECS="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
    )
    _raise_if_stopped(cancelled, deadline)
    output = bytearray()
    overflow = Event()
    with tempfile.TemporaryFile() as source:
        source.write(input_data)
        source.seek(0)
        with subprocess.Popen(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=source,
            env=env,
        ) as process:
            assert process.stdout is not None
            pipe = process.stdout

            def read() -> None:
                while chunk := pipe.read(8192):
                    if len(output) + len(chunk) > limit:
                        overflow.set()
                        process.kill()
                        return
                    output.extend(chunk)

            reader = Thread(target=read, daemon=True)
            reader.start()
            try:
                while process.poll() is None:
                    if cancelled.wait(0.01):
                        raise ProjectScanCancelled
                    if time.monotonic() >= deadline:
                        raise ProjectScanTimedOut
                if process.returncode or overflow.is_set():
                    raise GitReadUnavailable
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                reader.join()
    if cancelled.is_set():
        raise ProjectScanCancelled
    if overflow.is_set():
        raise GitReadUnavailable
    return bytes(output)


def _text(data: bytes) -> str:
    if len(data) > _FILE_BYTES or b"\0" in data:
        raise GitReadUnavailable
    return data.decode("utf-8")


def session_edits(
    entries: Iterable[SessionEntry], context: ToolContext
) -> dict[Path, tuple[RpcGitDiffEdit, ...]]:
    """Find the files the session's own edit and write calls changed.

    A call counts only once its tool result is recorded without an error, so a
    rejected or failed edit never marks a file. Paths resolve the way the tools
    resolve them, lexically against the folder the call ran in, which the result
    records; sessions live in one folder for every project, so a session resumed in
    another project marks nothing there. A result recorded without its folder is
    left out rather than guessed.

    Args:
        entries (Iterable[SessionEntry]): The active session path, oldest first.
            Compacted messages stay on this path, so attribution outlives compaction.
        context (ToolContext): The current protection policy, applied with each
            call's own folder.

    Returns:
        dict[Path, tuple[RpcGitDiffEdit, ...]]: Each edited file's most recent
        calls, oldest first.
    """
    # Calls awaiting a result, by call ID. A provider may reuse an ID within one
    # message, so each ID keeps its calls in order and results pair first to first.
    requested: dict[str, list[tuple[str, Literal["edit", "write"]]]] = {}
    edits: dict[Path, list[RpcGitDiffEdit]] = {}
    for entry in entries:
        if not isinstance(entry, MessageSessionEntry):
            continue
        message = entry.message
        for call in message.tool_calls or ():
            target = call.arguments.get("path")
            if call.name not in ("edit", "write") or not isinstance(target, str):
                continue
            if not 0 < len(call.call_id) <= 256:
                continue
            tool: Literal["edit", "write"] = "edit" if call.name == "edit" else "write"
            requested.setdefault(call.call_id, []).append((target, tool))
        if message.role != "tool" or message.tool_call_id not in requested:
            continue
        pending = requested[message.tool_call_id]
        target, tool = pending.pop(0)
        if not pending:
            del requested[message.tool_call_id]
        ran_in = entry.tool_result.cwd if entry.tool_result is not None else None
        if message.is_error or ran_in is None or len(entry.id) > 256:
            continue
        try:
            path = secure_tool_path(target, replace(context, cwd=Path(ran_in))).path
        except (ToolError, OSError, ValueError, RuntimeError):
            # Model-chosen paths include ones `~user` expansion cannot resolve.
            continue
        edits.setdefault(path, []).append(
            RpcGitDiffEdit(call_id=message.tool_call_id, entry_id=entry.id, tool=tool)
        )
    return {path: tuple(calls[-MAX_GIT_DIFF_FILE_EDITS:]) for path, calls in edits.items()}


_REGULAR_FILE_MODES = (b"100644", b"100755")
_EXECUTABLE_MODE = b"100755"
_WORKING_FILE_BYTES = 2 * 1024 * 1024
_MAX_REPORT_FILES = 256
_UNTRACKED_NOTICE = "Untracked file; excluded from tracked diff totals"
_EXECUTABLE_CHANGED_NOTICE = "Executable permission changed; no text changes"
_CONTENT_UNAVAILABLE_NOTICE = (
    "Content unavailable: binary, oversized, symbolic link, submodule, or unreadable file"
)

type _FileStatus = Literal["untracked", "modified", "added", "deleted"]
# "unchanged" files leave the report silently; "withheld" ones mark it truncated.
type _Omission = Literal["unchanged", "withheld"]


@dataclass(frozen=True, slots=True)
class _Repository:
    """What one report reads from Git before comparing any file.

    `head` and `index` map each path to its `(mode, object id)`.
    """

    root: Path
    head_commit: str
    head: dict[bytes, tuple[bytes, bytes]]
    index: dict[bytes, tuple[bytes, bytes]]
    sparse: set[bytes]
    attributes: dict[bytes, dict[bytes, bytes]]
    autocrlf: bool
    filemode: bool
    tracked: tuple[bytes, ...]
    untracked: tuple[bytes, ...]


@dataclass(slots=True)
class _ReadBudget:
    """Working-file bytes one report may still read, shared across its files."""

    remaining: int = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _CurrentFile:
    data: bytes
    executable: bool
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class _Contents:
    status: _FileStatus
    before: str = ""
    after: str = ""
    notice: str = ""


def collect_git_diff(
    context: ToolContext,
    cancelled: Event,
    command_id: str,
    generation: int,
    edits: Mapping[Path, tuple[RpcGitDiffEdit, ...]] | None = None,
) -> RpcGitDiffReported:
    """Capture authorized tracked file pairs and separate untracked metadata.

    Args:
        context (ToolContext): Settled backend-owned root and protection policy.
        cancelled (Event): Cancellation signal checked between reads.
        command_id (str): Request correlation identifier.
        generation (int): Policy generation checked again at publication.
        edits (Mapping[Path, tuple[RpcGitDiffEdit, ...]] | None): The session's own
            calls by file, from `session_edits`; files without any are reported as
            changed outside the session.

    Returns:
        RpcGitDiffReported: Bounded snapshot; omissions are explicitly marked.

    Raises:
        GitReadUnavailable: Git is unavailable or metadata exceeds the budget.
        ProjectScanCancelled: The request was cancelled.
        ProjectScanTimedOut: The shared deadline expired.
    """
    deadline = time.monotonic() + 5
    repository = _read_repository(context, cancelled, deadline)
    budget = _ReadBudget()
    entries: list[RpcGitDiffFile] = []
    truncated = False
    candidates = [(raw, False) for raw in repository.tracked] + [
        (raw, True) for raw in repository.untracked
    ]
    for raw, is_untracked in candidates:
        _raise_if_stopped(cancelled, deadline)
        try:
            path = raw.decode("utf-8")
            if not is_display_safe_path(path):
                truncated = True
                continue
            selected = secure_tool_path(str(repository.root / path), context)
        except (UnicodeError, ToolError, OSError):
            # Neither protected names nor absolute paths belong in the report.
            truncated = True
            continue
        if len(entries) >= _MAX_REPORT_FILES:
            truncated = True
            break
        if is_untracked:
            contents = _Contents("untracked", notice=_UNTRACKED_NOTICE)
        else:
            compared = _compare_tracked(raw, selected, repository, budget, cancelled, deadline)
            if compared == "unchanged":
                continue
            if compared == "withheld":
                truncated = True
                continue
            contents = compared
        entry = RpcGitDiffFile(
            path=path,
            kind="file",
            status=contents.status,
            before=contents.before,
            after=contents.after,
            notice=contents.notice,
            edits=(edits or {}).get(selected.path, ()),
        )
        candidate = RpcGitDiffReported(
            command_id=command_id,
            generation=generation,
            head=repository.head_commit,
            files=tuple([*entries, entry]),
            truncated=truncated,
        )
        if len(candidate.model_dump_json().encode()) > _REPORT_BYTES:
            truncated = True
            break
        entries.append(entry)
    return RpcGitDiffReported(
        command_id=command_id,
        generation=generation,
        head=repository.head_commit,
        files=tuple(entries),
        truncated=truncated,
    )


def _raise_if_stopped(cancelled: Event, deadline: float) -> None:
    if cancelled.is_set():
        raise ProjectScanCancelled
    if time.monotonic() >= deadline:
        raise ProjectScanTimedOut


def _read_repository(context: ToolContext, cancelled: Event, deadline: float) -> _Repository:
    root = Path(
        _git(context.cwd, ["rev-parse", "--show-toplevel"], cancelled, deadline).decode().strip()
    )
    head_commit = (
        _git(root, ["rev-parse", "--verify", "HEAD"], cancelled, deadline).decode().strip()
    )
    # Only enumerate the index. Git commands that compare working-tree content,
    # including ls-files --modified, can execute repository clean filters.
    index_listing = _git(root, ["ls-files", "--stage", "-z"], cancelled, deadline)
    autocrlf = _git(
        root, ["config", "--default", "false", "--get", "core.autocrlf"], cancelled, deadline
    ).strip().lower() in (b"true", b"input")
    filemode = (
        _git(root, ["config", "--default", "true", "--get", "core.filemode"], cancelled, deadline)
        .strip()
        .lower()
        != b"false"
    )
    untracked_listing = _git(
        root, ["ls-files", "--others", "--exclude-standard", "-z"], cancelled, deadline
    )
    tree = _git(root, ["ls-tree", "-r", "-z", head_commit], cancelled, deadline)
    head: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if record:
            meta, raw_path = record.split(b"\t", 1)
            mode, _, oid = meta.split(b" ")
            head[raw_path] = (mode, oid)
    index: dict[bytes, tuple[bytes, bytes]] = {}
    for record in index_listing.split(b"\0"):
        if record:
            meta, raw_path = record.split(b"\t", 1)
            mode, oid, _ = meta.split(b" ")
            index[raw_path] = (mode, oid)
    flags = _git(root, ["ls-files", "-t", "-z"], cancelled, deadline)
    sparse = {record[2:] for record in flags.split(b"\0") if record.startswith(b"S ")}
    unchanged_sparse = {path for path in sparse if index.get(path) == head.get(path)}
    names = (head.keys() | index.keys()) - unchanged_sparse
    attributes: dict[bytes, dict[bytes, bytes]] = {}
    if names:
        values = _git(
            root,
            [
                "check-attr",
                "-z",
                "--stdin",
                "filter",
                "working-tree-encoding",
                "text",
                "eol",
                "ident",
            ],
            cancelled,
            deadline,
            input_data=b"\0".join(sorted(names)) + b"\0",
        ).split(b"\0")
        for raw_path, name, value in zip(values[0:-1:3], values[1::3], values[2::3], strict=True):
            attributes.setdefault(raw_path, {})[name] = value
    return _Repository(
        root=root,
        head_commit=head_commit,
        head=head,
        index=index,
        sparse=sparse,
        attributes=attributes,
        autocrlf=autocrlf,
        filemode=filemode,
        tracked=tuple(sorted(names)),
        untracked=tuple(
            path for path in sorted(set(untracked_listing.split(b"\0")) - set(index)) if path
        ),
    )


def _compare_tracked(
    raw: bytes,
    selected: SecureToolPath,
    repository: _Repository,
    budget: _ReadBudget,
    cancelled: Event,
    deadline: float,
) -> _Contents | _Omission:
    """Compare one tracked file's current bytes with its HEAD blob.

    A read that fails keeps the status already decided and reports the content
    as unavailable, so the file still appears in the report.
    """
    blob = repository.head.get(raw)
    if blob and blob[0] not in _REGULAR_FILE_MODES:
        return "withheld"
    status: _FileStatus = "modified" if blob else "added"
    notice = ""
    try:
        current = _read_current(raw, selected, repository, budget, cancelled, deadline)
        if current == "unchanged" or current == "withheld":
            return current
        data = current.data
        if current.deleted:
            status = "deleted"
        elif raw not in repository.sparse:
            checked_out = _undo_checkout_attributes(repository, raw, data)
            if checked_out is None:
                return "withheld"
            data = checked_out
        if blob and not current.deleted and _is_blob(blob[1], data):
            if current.executable == (blob[0] == _EXECUTABLE_MODE):
                return "unchanged"
            notice = _EXECUTABLE_CHANGED_NOTICE
        after = _text(data)
        before = ""
        if blob:
            before = _text(
                _git(
                    repository.root,
                    ["cat-file", "blob", blob[1].decode("ascii")],
                    cancelled,
                    deadline,
                    _FILE_BYTES,
                )
            )
    except (GitReadUnavailable, UnicodeError, ToolError, OSError):
        return _Contents(status, notice=_CONTENT_UNAVAILABLE_NOTICE)
    return _Contents(status, before=before, after=after, notice=notice)


def _read_current(
    raw: bytes,
    selected: SecureToolPath,
    repository: _Repository,
    budget: _ReadBudget,
    cancelled: Event,
    deadline: float,
) -> _CurrentFile | _Omission:
    if raw in repository.sparse:
        # Sparse paths use the index as their effective working tree.
        # Its bytes are already canonical; do not apply attributes twice.
        mode, oid = repository.index[raw]
        if mode not in _REGULAR_FILE_MODES:
            raise GitReadUnavailable
        data = _git(
            repository.root,
            ["cat-file", "blob", oid.decode("ascii")],
            cancelled,
            deadline,
            _FILE_BYTES,
        )
        return _CurrentFile(data, executable=mode == _EXECUTABLE_MODE)
    try:
        # An index removal is a tracked deletion even if the same
        # pathname remains on disk as a separate untracked file.
        if raw not in repository.index:
            raise FileNotFoundError
        selected.path.lstat()
    except FileNotFoundError:
        if raw not in repository.head:
            return "unchanged"
        current = _CurrentFile(b"", executable=False, deleted=True)
    else:
        working = _read_working_file(selected, budget, cancelled)
        if working is None:
            return "withheld"
        current = working
    if not repository.filemode and raw in repository.index:
        current = replace(current, executable=repository.index[raw][0] == _EXECUTABLE_MODE)
    return current


def _read_working_file(
    selected: SecureToolPath, budget: _ReadBudget, cancelled: Event
) -> _CurrentFile | None:
    """Read a working file within the shared budget; None when it does not fit."""
    limit = min(_WORKING_FILE_BYTES, budget.remaining)
    with open_file(selected) as fd:
        if os.fstat(fd).st_size > limit:
            return None
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining and (chunk := os.read(fd, min(65536, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
            if cancelled.is_set():
                raise ProjectScanCancelled
        data = b"".join(chunks)
        budget.remaining -= len(data)
        if remaining == 0:
            return None
        return _CurrentFile(data, executable=bool(os.fstat(fd).st_mode & stat.S_IXUSR))


def _undo_checkout_attributes(repository: _Repository, raw: bytes, data: bytes) -> bytes | None:
    """Return working bytes as Git would store them, or None when that needs a helper."""
    # Attribute transformations cannot be reproduced safely by running
    # repository helpers. Omit such comparisons instead of reporting
    # clean smudged files (for example LFS) as edits.
    attrs = repository.attributes[raw]
    if any(
        attrs[name] not in (b"unspecified", b"unset")
        for name in (b"filter", b"working-tree-encoding")
    ):
        return None
    text = attrs[b"text"]
    normalize = text != b"unset" and (
        text in (b"set", b"auto") or attrs[b"eol"] in (b"lf", b"crlf") or repository.autocrlf
    )
    if normalize and b"\0" not in data and b"\r" not in data.replace(b"\r\n", b""):
        data = data.replace(b"\r\n", b"\n")
    if attrs[b"ident"] == b"set":
        data = re.sub(rb"\$Id:[^$\r\n]*\$", b"$Id$", data)
    return data


def _is_blob(oid: bytes, data: bytes) -> bool:
    digest = hashlib.new("sha1" if len(oid) == 40 else "sha256")
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest().encode("ascii") == oid
