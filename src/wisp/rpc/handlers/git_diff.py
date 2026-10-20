"""Bounded Git object and secure working-file reads for interactive review."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event, Thread
from typing import Literal

from wisp.events import RpcGitDiffFile, RpcGitDiffReported
from wisp.project_files import ProjectScanCancelled, ProjectScanTimedOut, is_display_safe_path
from wisp.tools.context import ToolContext
from wisp.tools.files.secure_fs import open_file, secure_tool_path
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
    if cancelled.is_set():
        raise ProjectScanCancelled
    if time.monotonic() >= deadline:
        raise ProjectScanTimedOut
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


def collect_git_diff(
    context: ToolContext, cancelled: Event, command_id: str, generation: int
) -> RpcGitDiffReported:
    """Capture authorized tracked file pairs and separate untracked metadata.

    Args:
        context (ToolContext): Settled backend-owned root and protection policy.
        cancelled (Event): Cancellation signal checked between reads.
        command_id (str): Request correlation identifier.
        generation (int): Policy generation checked again at publication.

    Returns:
        RpcGitDiffReported: Bounded snapshot; omissions are explicitly marked.

    Raises:
        GitReadUnavailable: Git is unavailable or metadata exceeds the budget.
        ProjectScanCancelled: The request was cancelled.
        ProjectScanTimedOut: The shared deadline expired.
    """
    deadline = time.monotonic() + 5
    root = Path(
        _git(context.cwd, ["rev-parse", "--show-toplevel"], cancelled, deadline).decode().strip()
    )
    head = _git(root, ["rev-parse", "--verify", "HEAD"], cancelled, deadline).decode().strip()
    # Only enumerate the index. Git commands that compare working-tree content,
    # including ls-files --modified, can execute repository clean filters.
    index = _git(root, ["ls-files", "--stage", "-z"], cancelled, deadline)
    autocrlf = _git(
        root, ["config", "--default", "false", "--get", "core.autocrlf"], cancelled, deadline
    ).strip().lower() in (b"true", b"input")
    filemode = (
        _git(root, ["config", "--default", "true", "--get", "core.filemode"], cancelled, deadline)
        .strip()
        .lower()
        != b"false"
    )
    untracked = _git(
        root, ["ls-files", "--others", "--exclude-standard", "-z"], cancelled, deadline
    )
    tree = _git(root, ["ls-tree", "-r", "-z", head], cancelled, deadline)
    blobs: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if record:
            meta, raw_path = record.split(b"\t", 1)
            mode, _, oid = meta.split(b" ")
            blobs[raw_path] = (mode, oid)
    indexed: dict[bytes, tuple[bytes, bytes]] = {}
    for record in index.split(b"\0"):
        if record:
            meta, raw_path = record.split(b"\t", 1)
            mode, oid, _ = meta.split(b" ")
            indexed[raw_path] = (mode, oid)
    flags = _git(root, ["ls-files", "-t", "-z"], cancelled, deadline)
    sparse = {record[2:] for record in flags.split(b"\0") if record.startswith(b"S ")}
    names = (blobs.keys() | indexed.keys()) - sparse
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
    read_budget = 64 * 1024 * 1024
    entries: list[RpcGitDiffFile] = []
    truncated = False
    for raw, is_untracked in [(p, False) for p in sorted(names)] + [
        (p, True) for p in sorted(set(untracked.split(b"\0")) - set(indexed)) if p
    ]:
        if cancelled.is_set():
            raise ProjectScanCancelled
        if time.monotonic() >= deadline:
            raise ProjectScanTimedOut
        try:
            path = raw.decode("utf-8")
            if not is_display_safe_path(path):
                truncated = True
                continue
            selected = secure_tool_path(str(root / path), context)
        except (UnicodeError, ToolError, OSError):
            # Neither protected names nor absolute paths belong in the report.
            truncated = True
            continue
        if len(entries) >= 256:
            truncated = True
            break
        before = after = notice = ""
        status: Literal["untracked", "modified", "added", "deleted"] = (
            "untracked" if is_untracked else "modified"
        )
        if is_untracked:
            notice = "Untracked file; excluded from tracked diff totals"
        else:
            try:
                blob = blobs.get(raw)
                if blob and blob[0] not in (b"100644", b"100755"):
                    truncated = True
                    continue
                if not blob:
                    status = "added"
                try:
                    # An index removal is a tracked deletion even if the same
                    # pathname remains on disk as a separate untracked file.
                    if raw not in indexed:
                        raise FileNotFoundError
                    selected.path.lstat()
                except FileNotFoundError:
                    if not blob:
                        continue
                    status = "deleted"
                    data = b""
                    executable = False
                else:
                    with open_file(selected) as fd:
                        file_size = os.fstat(fd).st_size
                        if file_size > min(2 * 1024 * 1024, read_budget):
                            truncated = True
                            continue
                        chunks: list[bytes] = []
                        remaining = min(2 * 1024 * 1024, read_budget) + 1
                        while remaining and (chunk := os.read(fd, min(65536, remaining))):
                            chunks.append(chunk)
                            remaining -= len(chunk)
                            if cancelled.is_set():
                                raise ProjectScanCancelled
                        data = b"".join(chunks)
                        read_budget -= len(data)
                        if remaining == 0:
                            truncated = True
                            continue
                        executable = bool(os.fstat(fd).st_mode & stat.S_IXUSR)
                # Attribute transformations cannot be reproduced safely by running
                # repository helpers. Omit such comparisons instead of reporting
                # clean smudged files (for example LFS) as edits.
                if status != "deleted":
                    attrs = attributes[raw]
                    if any(
                        attrs[name] not in (b"unspecified", b"unset")
                        for name in (b"filter", b"working-tree-encoding")
                    ):
                        truncated = True
                        continue
                    text = attrs[b"text"]
                    normalize = text != b"unset" and (
                        text in (b"set", b"auto") or attrs[b"eol"] in (b"lf", b"crlf") or autocrlf
                    )
                    if normalize and b"\0" not in data and b"\r" not in data.replace(b"\r\n", b""):
                        data = data.replace(b"\r\n", b"\n")
                    if attrs[b"ident"] == b"set":
                        data = re.sub(rb"\$Id:[^$\r\n]*\$", b"$Id$", data)
                    if blob:
                        digest = hashlib.new("sha1" if len(blob[1]) == 40 else "sha256")
                        digest.update(f"blob {len(data)}\0".encode("ascii"))
                        digest.update(data)
                        if digest.hexdigest().encode("ascii") == blob[1]:
                            if not filemode or executable == (blob[0] == b"100755"):
                                continue
                            notice = "Executable permission changed; no text changes"
                after = _text(data)
                if blob:
                    before = _text(
                        _git(
                            root,
                            ["cat-file", "blob", blob[1].decode("ascii")],
                            cancelled,
                            deadline,
                            _FILE_BYTES,
                        )
                    )
            except (GitReadUnavailable, UnicodeError, ToolError, OSError):
                before = after = ""
                notice = (
                    "Content unavailable: binary, oversized, symbolic link, "
                    "submodule, or unreadable file"
                )
        entry = RpcGitDiffFile(
            path=path, kind="file", status=status, before=before, after=after, notice=notice
        )
        candidate = RpcGitDiffReported(
            command_id=command_id,
            generation=generation,
            head=head,
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
        head=head,
        files=tuple(entries),
        truncated=truncated,
    )
