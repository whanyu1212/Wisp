"""File-oriented built-in tools."""

from __future__ import annotations

import errno
import os
import shlex
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import TextIO
from uuid import uuid4

import anyio

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_bool, _optional_int, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.files.secure_fs import (
    OpenParent,
    SecureToolPath,
    file_version,
    open_file,
    open_parent,
    open_windows_parent,
    secure_tool_path,
    stat_leaf,
)
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult
from wisp.tools.seen_files import FileVersion, SeenFiles
from wisp.tools.truncation import truncate_text

# A write can overwrite an arbitrarily large file. The before-snapshot rides the RPC
# wire to the TUI as an event field, so cap it at the tool layer: past this size a
# diff isn't worth rendering (it exceeds the renderer's own work guard anyway), so we
# drop the snapshot and let the write fall back to its plain summary. Matched to the
# renderer's per-hunk character ceiling so the two bounds agree.
_WRITE_SNAPSHOT_MAX_CHARS = 1_000_000
# edit holds the whole file, its replacement and their encodings in memory at once.
_MAX_EDIT_BYTES = 16 * 1024 * 1024
# Room a read continuation notice needs within the output budget, besides the path it
# quotes. Budgets too small for that get a compact notice without the bash example.
_READ_NOTICE_RESERVED_BYTES = 256
_COMPACT_READ_NOTICE_BYTES = 64


class CreateOnlyWriteReceipt:
    """Operation-local identity of a successfully published create-only write."""

    __slots__ = ("file_id", "path")

    def __init__(self) -> None:
        self.path: Path | None = None
        self.file_id: tuple[int, int] | None = None

    def record(self, path: Path, file_id: tuple[int, int]) -> None:
        self.path = path
        self.file_id = file_id


class ReadTool:
    """Read text files with optional line slicing."""

    name = "read"
    safety: ToolSafety = "read"
    description = "Read a UTF-8 text file. Supports 1-indexed offset and line limit."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = secure_tool_path(_required_string(arguments, "path"), context, follow_links=True)
        offset = _optional_int(arguments, "offset", default=1)
        limit = _optional_int(arguments, "limit")

        if offset is None or offset < 1:
            raise ToolArgumentError("read.offset must be greater than or equal to 1")
        if limit is not None and limit < 1:
            raise ToolArgumentError("read.limit must be greater than or equal to 1")
        # The notice must fit inside the output budget: a later bounding pass would
        # otherwise cut it from the end, leaving the model no way to continue.
        quoted_path = shlex.quote(path.display)
        notice_bytes = _READ_NOTICE_RESERVED_BYTES + len(quoted_path.encode("utf-8"))
        compact_notice = notice_bytes > context.max_output_bytes // 2
        if compact_notice:
            notice_bytes = _COMPACT_READ_NOTICE_BYTES
        try:
            slice_result = await anyio.to_thread.run_sync(
                lambda: _secure_read_line_slice(
                    path,
                    offset,
                    limit,
                    max_bytes=context.max_output_bytes,
                    max_lines=context.max_output_lines,
                    notice_bytes=notice_bytes,
                ),
                abandon_on_cancel=True,
            )
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {path.display}") from exc
        line_count = slice_result.line_count
        if line_count is not None and offset > max(line_count, 1):
            raise ToolError(
                f"read.offset {offset} is beyond the end of {path.display} ({line_count} lines)",
                failure_code="invalid_arguments",
                retryable=True,
                recovery_hint=f"Use an offset from 1 to {max(line_count, 1)}.",
            )
        # A partial read counts: the model has seen this version of the file.
        if context.seen_files is not None and slice_result.version is not None:
            context.seen_files.record(path.path, slice_result.version)

        notice = _read_continuation_notice(
            slice_result,
            offset=offset,
            quoted_path=quoted_path,
            part_bytes=context.max_output_bytes - notice_bytes,
            compact=compact_notice,
        )
        text = slice_result.text
        if notice:
            separator = "" if not text or text.endswith("\n") else "\n"
            text = f"{text}{separator}{notice}"
        truncated = truncate_text(
            text,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        data: dict[str, object] = {
            "path": path.display,
            "selected_count": slice_result.selected_count,
            "offset": offset,
            "limit": limit,
        }
        # An exact whole-file count is available only when the bounded scan reaches
        # EOF. Omitting it is more honest than reporting the number scanned as a total.
        if slice_result.line_count is not None:
            data["line_count"] = slice_result.line_count
        return ToolResult(
            text=truncated.text,
            data=data,
            truncated=slice_result.truncated or truncated.truncated,
        )


def _read_continuation_notice(
    slice_result: _ReadSlice,
    *,
    offset: int,
    quoted_path: str,
    part_bytes: int,
    compact: bool,
) -> str | None:
    """Tell the model where the next page starts when a read stops early.

    Args:
        slice_result (_ReadSlice): The bounded slice that was read.
        offset (int): 1-indexed line the slice started at.
        quoted_path (str): Shell-quoted path for the oversized-line example.
        part_bytes (int): How much of an oversized line one read can show.
        compact (bool): Whether to use the short wording for a small output budget.

    Returns:
        str | None: The notice, or ``None`` when the slice reached the end of the
        file or the end of the requested limit.
    """

    next_offset = offset + slice_result.selected_count
    if slice_result.partial_line is not None:
        line = slice_result.partial_line
        if compact:
            return f"[truncated] Line {line} is too long; use offset={line + 1} to skip it."
        return (
            f"[truncated] Line {line} is longer than the output limit. Use offset={line + 1} "
            f"to skip it, or read it in parts with bash, for example: "
            f"sed -n '{line}p' {quoted_path} | cut -c1-{max(part_bytes, 1)}"
        )
    if slice_result.truncated:
        if compact:
            return f"[truncated] Use offset={next_offset} to continue."
        return f"[truncated] Output limit reached. Use offset={next_offset} to continue."
    if slice_result.line_count is None:
        return f"[More lines follow. Use offset={next_offset} to continue.]"
    return None


class WriteTool:
    """Create or overwrite UTF-8 text files."""

    name = "write"
    safety: ToolSafety = "mutating"
    description = (
        "Create or overwrite a UTF-8 text file, creating parent directories. "
        "Set overwrite=false to fail if the target already exists. Read an existing "
        "file before overwriting it; a file changed since you read it must be read again."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "overwrite": {"type": "boolean", "default": True},
        },
        "required": ["path", "content"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        selected_path = _required_string(arguments, "path")
        content = _required_string(arguments, "content", allow_empty=True)
        overwrite = _optional_bool(arguments, "overwrite", default=True)
        constraint = context.write_constraint
        if constraint is not None and constraint.create_only and overwrite:
            raise ToolError("This operation requires write calls with overwrite=false")
        if constraint is not None and constraint.non_empty and not content:
            raise ToolError("This operation requires non-empty write content")
        path = secure_tool_path(selected_path, context, write=True)
        outcome = await anyio.to_thread.run_sync(
            lambda: _atomic_write(path, content, overwrite=overwrite, context=context)
        )
        if context.seen_files is not None and outcome.version is not None:
            context.seen_files.record(path.path, outcome.version)
        if (
            outcome.file_id is not None
            and constraint is not None
            and constraint.receipt is not None
        ):
            constraint.receipt.record(path.path, outcome.file_id)
        byte_count = len(content.encode("utf-8"))
        data: dict[str, object] = {
            "path": path.display,
            "bytes": byte_count,
            "created": outcome.created,
        }
        if outcome.before_text is not None:
            data["before_text"] = outcome.before_text
        return ToolResult(
            text=f"Wrote {byte_count} bytes to {path.display}",
            data=data,
        )


@dataclass(frozen=True, slots=True)
class _WriteOutcome:
    created: bool
    before_text: str | None
    file_id: tuple[int, int] | None
    version: FileVersion | None
    """The published file's version, or None when it could not be verified as this write's."""


@dataclass(frozen=True, slots=True)
class _ReplacementMetadata:
    mode: int
    uid: int
    gid: int
    xattrs: tuple[tuple[str, bytes], ...] = ()
    flags: int | None = None


def _check_conflicting_paths(context: ToolContext) -> None:
    if context.write_constraint is None:
        return
    for conflict in context.write_constraint.conflicting_paths:
        candidate = secure_tool_path(str(conflict), context)
        try:
            if os.name == "nt":
                with open_windows_parent(candidate):
                    try:
                        candidate.path.lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        raise ToolError(f"Conflicting write path already exists: {conflict}")
                continue
            with open_parent(candidate) as parent:
                if stat_leaf(parent) is not None:
                    raise ToolError(f"Conflicting write path already exists: {conflict}")
        except ToolError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                continue
            raise


def _open_existing(parent: OpenParent, info: os.stat_result) -> int:
    if stat.S_ISLNK(info.st_mode):
        raise ToolError(f"Symbolic links are not allowed: {parent.path.selected}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(parent.leaf, flags, dir_fd=parent.fd)
    except OSError as exc:
        raise ToolError(f"Could not open file {parent.path.display}: {exc}") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise ToolError(f"Not a regular file: {parent.path.display}")
    if file_version(opened) != file_version(info):
        os.close(descriptor)
        raise ToolError(f"File changed while opening: {parent.path.selected}")
    return descriptor


def _open_metadata_descriptor(parent: OpenParent, expected: os.stat_result) -> int | None:
    if not hasattr(os, "O_PATH"):
        return None
    flags = os.O_PATH | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(parent.leaf, flags, dir_fd=parent.fd)
    except OSError as exc:
        raise ToolError(f"Could not open file metadata {parent.path.display}: {exc}") from exc
    opened = os.fstat(descriptor)
    if file_version(opened) != file_version(expected):
        os.close(descriptor)
        raise ToolError(f"File changed while opening: {parent.path.selected}")
    return descriptor


def _read_descriptor(descriptor: int, path: SecureToolPath) -> str:
    """Read a whole file for ``edit``, refusing one too large to rewrite in memory."""

    size = os.fstat(descriptor).st_size
    if size > _MAX_EDIT_BYTES:
        raise ToolError(
            f"{path.display} is {size} bytes, more than the {_MAX_EDIT_BYTES} bytes edit "
            "can change at once",
            recovery_hint="Change it with a bash command instead, for example sed -i.",
        )
    with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as file:
        return file.read()


def _write_existing_in_place(
    parent: OpenParent,
    content: str,
    *,
    expected: tuple[int, int, int, int, int],
) -> FileVersion:
    """Rewrite the open file in place and return the version this write produced."""

    encoded = memoryview(content.encode("utf-8"))
    # O_NONBLOCK keeps a FIFO swapped in after the type check from blocking the open; it
    # has no effect on writes to the regular file the check below requires.
    flags = (
        os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(parent.leaf, flags, dir_fd=parent.fd)
    except OSError as exc:
        raise ToolError(f"Could not open file {parent.path.display}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or file_version(opened) != expected:
            raise ToolError(f"File changed while writing: {parent.path.selected}")
        os.ftruncate(descriptor, 0)
        while encoded:
            written = os.write(descriptor, encoded)
            if written <= 0:
                raise OSError("write made no progress")
            encoded = encoded[written:]
        os.fsync(descriptor)
        produced = file_version(os.fstat(descriptor))
        current = stat_leaf(parent)
        if current is None or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ToolError(f"File changed while writing: {parent.path.selected}")
    except OSError as exc:
        raise ToolError(f"Could not write file {parent.path.selected}: {exc}") from exc
    finally:
        os.close(descriptor)
    return produced


def _write_path_existing_in_place(
    path: SecureToolPath,
    content: str,
    *,
    expected: tuple[int, int, int, int, int],
    operation: str,
) -> FileVersion:
    """Rewrite the file at ``path`` in place and return the version this write produced."""

    encoded = memoryview(content.encode("utf-8"))
    try:
        if os.name == "nt":
            from wisp.skills.filesystem import open_windows_writable_file

            descriptor = open_windows_writable_file(path.path)
        else:
            descriptor = os.open(path.path, os.O_WRONLY)
    except OSError as exc:
        raise ToolError(f"Could not open file {path.display}: {exc}") from exc
    try:
        if os.name == "nt":
            from wisp.skills.filesystem import resolved_open_file

            if resolved_open_file(descriptor, path=path.path) != path.path.resolve(strict=False):
                raise ToolError(f"File changed while {operation}: {path.selected}")
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or file_version(opened) != expected:
            raise ToolError(f"File changed while {operation}: {path.selected}")
        os.ftruncate(descriptor, 0)
        while encoded:
            written = os.write(descriptor, encoded)
            if written <= 0:
                raise OSError("write made no progress")
            encoded = encoded[written:]
        os.fsync(descriptor)
        produced = file_version(os.fstat(descriptor))
        current = _windows_leaf_info(path)
        if current is None or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ToolError(f"File changed while {operation}: {path.selected}")
    except OSError as exc:
        raise ToolError(f"Could not update file {path.selected}: {exc}") from exc
    finally:
        os.close(descriptor)
    return produced


def _snapshot_descriptor(descriptor: int) -> str | None:
    try:
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as file:
            before = file.read(_WRITE_SNAPSHOT_MAX_CHARS + 1)
    except (OSError, UnicodeDecodeError):
        return None
    return before if len(before) <= _WRITE_SNAPSHOT_MAX_CHARS else None


def _allocate_temporary(parent: OpenParent, *, private: bool) -> tuple[int, str]:
    """Create an empty temporary file beside the target.

    A private file is readable only by its owner, so content replacing a file with
    stricter permissions is never exposed before the original mode is applied.
    """

    mode = 0o600 if private else 0o666
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(10):
        name = f".wisp-write-{uuid4().hex}"
        try:
            return os.open(name, flags, mode, dir_fd=parent.fd), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise ToolError(f"Could not create file: {parent.path.selected}: {exc}") from exc
    raise ToolError(f"Could not allocate temporary file for write: {parent.path.selected}")


def _snapshot_replacement_metadata(
    descriptor: int,
    info: os.stat_result,
) -> _ReplacementMetadata:
    xattrs: list[tuple[str, bytes]] = []
    listxattr = getattr(os, "listxattr", None)
    getxattr = getattr(os, "getxattr", None)
    if listxattr is not None and getxattr is not None:
        source: int | str = descriptor
        try:
            names = listxattr(source)
        except OSError as exc:
            proc_descriptor = f"/proc/self/fd/{descriptor}"
            if exc.errno != errno.EBADF or not Path(proc_descriptor).exists():
                raise ToolError(f"Could not read existing file metadata: {exc}") from exc
            source = proc_descriptor
            names = listxattr(source)
        try:
            xattrs = [(name, getxattr(source, name)) for name in names]
        except OSError as exc:
            raise ToolError(f"Could not read existing file metadata: {exc}") from exc
    return _ReplacementMetadata(
        mode=info.st_mode,
        uid=info.st_uid,
        gid=info.st_gid,
        xattrs=tuple(xattrs),
        flags=getattr(info, "st_flags", None),
    )


def _apply_replacement_metadata(
    descriptor: int,
    metadata: _ReplacementMetadata,
) -> None:
    try:
        if hasattr(os, "fchown"):
            os.fchown(descriptor, metadata.uid, metadata.gid)
        os.fchmod(descriptor, stat.S_IMODE(metadata.mode))
        setxattr = getattr(os, "setxattr", None)
        if setxattr is not None:
            for name, value in metadata.xattrs:
                setxattr(descriptor, name, value)
        fchflags = getattr(os, "fchflags", None)
        if fchflags is not None and metadata.flags is not None:
            fchflags(descriptor, metadata.flags)
    except OSError as exc:
        raise ToolError(f"Could not preserve existing file metadata: {exc}") from exc


def _write_temporary(
    parent: OpenParent,
    content: str,
    *,
    metadata: _ReplacementMetadata | None,
) -> tuple[str, os.stat_result]:
    descriptor, name = _allocate_temporary(parent, private=metadata is not None)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            if metadata is not None:
                _apply_replacement_metadata(file.fileno(), metadata)
            os.fsync(file.fileno())
            written = os.fstat(file.fileno())
        return name, written
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent.fd)
        except FileNotFoundError:
            pass
        if isinstance(exc, OSError):
            raise ToolError(f"Could not create file: {parent.path.selected}: {exc}") from exc
        raise


def _cleanup_temporary(parent: OpenParent, name: str, *, published: bool) -> None:
    try:
        os.unlink(name, dir_fd=parent.fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if published:
            raise ToolError(
                "File was published but temporary-link cleanup failed; "
                f"destination: {parent.path.path}; temporary: {name}: {exc}"
            ) from exc
        raise ToolError(f"Could not clean up failed write {name}: {exc}") from exc


# Errors meaning the directory cannot be synced at all, rather than that syncing failed.
_DIRECTORY_SYNC_UNSUPPORTED = frozenset(
    {errno.EACCES, errno.EPERM, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)


def _sync_directory(parent: OpenParent) -> None:
    """Make a rename in ``parent`` survive a crash, where the filesystem allows it.

    The rename has already published the file, so a directory that cannot be opened
    or synced, such as on some network filesystems, is skipped rather than reported
    as a failed write.

    Raises:
        ToolError: When the sync itself fails, for example with an I/O error.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        # The parent descriptor may be O_PATH on Linux, which cannot be synced.
        descriptor = os.open(".", flags, dir_fd=parent.fd)
    except OSError as exc:
        if exc.errno in _DIRECTORY_SYNC_UNSUPPORTED:
            return
        raise _directory_sync_error(parent, exc) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _DIRECTORY_SYNC_UNSUPPORTED:
            raise _directory_sync_error(parent, exc) from exc
    finally:
        os.close(descriptor)


def _directory_sync_error(parent: OpenParent, error: OSError) -> ToolError:
    return ToolError(
        f"Wrote {parent.path.display} but could not sync its directory, so the change "
        f"may not survive a crash: {error}"
    )


def _atomic_write(
    path: SecureToolPath,
    content: str,
    *,
    overwrite: bool,
    context: ToolContext,
) -> _WriteOutcome:
    _check_conflicting_paths(context)
    if os.name == "nt":
        return _atomic_write_windows(
            path, content, overwrite=overwrite, seen_files=context.seen_files
        )
    with open_parent(path, create=True) as parent:
        initial = stat_leaf(parent)
        if initial is not None and stat.S_ISLNK(initial.st_mode):
            if overwrite:
                raise ToolError(f"Symbolic links are not allowed: {path.selected}")
            raise ToolError(f"File already exists: {path.selected}")
        if not overwrite and initial is not None:
            raise ToolError(f"File already exists: {path.selected}")
        # Opening a FIFO for writing waits for a reader, and replacing a device or socket
        # with a regular file would break whatever uses it.
        if initial is not None and not stat.S_ISREG(initial.st_mode):
            raise ToolError(f"Not a regular file: {path.display}")

        before_text: str | None = None
        initial_version: tuple[int, int, int, int, int] | None = None
        metadata: _ReplacementMetadata | None = None
        link_count = 0
        requires_in_place = False
        if initial is not None:
            initial_version = file_version(initial)
            link_count = initial.st_nlink
            metadata = _ReplacementMetadata(
                mode=initial.st_mode,
                uid=initial.st_uid,
                gid=initial.st_gid,
                flags=getattr(initial, "st_flags", None),
            )
            try:
                descriptor = _open_existing(parent, initial)
            except ToolError as exc:
                if not isinstance(exc.__cause__, PermissionError):
                    raise
                try:
                    metadata_descriptor = _open_metadata_descriptor(parent, initial)
                except ToolError as metadata_exc:
                    if not isinstance(metadata_exc.__cause__, PermissionError):
                        raise
                    metadata_descriptor = None
                    requires_in_place = True
                if metadata_descriptor is None:
                    requires_in_place = True
                else:
                    try:
                        opened = os.fstat(metadata_descriptor)
                        initial_version = file_version(opened)
                        link_count = opened.st_nlink
                        try:
                            metadata = _snapshot_replacement_metadata(metadata_descriptor, opened)
                        except ToolError as metadata_exc:
                            if not isinstance(metadata_exc.__cause__, PermissionError):
                                raise
                            requires_in_place = True
                    finally:
                        os.close(metadata_descriptor)
            else:
                try:
                    opened = os.fstat(descriptor)
                    initial_version = file_version(opened)
                    link_count = opened.st_nlink
                    metadata = _snapshot_replacement_metadata(descriptor, opened)
                    before_text = _snapshot_descriptor(descriptor)
                finally:
                    os.close(descriptor)

        if initial_version is not None and context.seen_files is not None:
            context.seen_files.require_current(path.path, initial_version, display=path.display)
        if overwrite and initial_version is not None and (link_count > 1 or requires_in_place):
            version = _write_existing_in_place(parent, content, expected=initial_version)
            return _WriteOutcome(False, before_text, None, version)

        try:
            temporary, written = _write_temporary(parent, content, metadata=metadata)
        except ToolError as exc:
            if (
                overwrite
                and initial_version is not None
                and isinstance(exc.__cause__, PermissionError)
            ):
                version = _write_existing_in_place(parent, content, expected=initial_version)
                return _WriteOutcome(False, before_text, None, version)
            raise
        published = False
        try:
            current = stat_leaf(parent)
            current_version = None if current is None else file_version(current)
            if current_version != initial_version:
                raise ToolError(f"File changed while writing: {path.selected}")
            if overwrite:
                os.replace(
                    temporary,
                    parent.leaf,
                    src_dir_fd=parent.fd,
                    dst_dir_fd=parent.fd,
                )
            else:
                try:
                    os.link(
                        temporary,
                        parent.leaf,
                        src_dir_fd=parent.fd,
                        dst_dir_fd=parent.fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise ToolError(f"File already exists: {path.selected}") from exc
            published = True
        except ToolError:
            raise
        except (OSError, TypeError) as exc:
            raise ToolError(f"Could not create file: {path.selected}: {exc}") from exc
        finally:
            _cleanup_temporary(parent, temporary, published=published)
        _sync_directory(parent)
        file_id = (written.st_dev, written.st_ino)
        return _WriteOutcome(
            initial is None,
            before_text,
            file_id if not overwrite else None,
            _published_version(stat_leaf(parent), written),
        )


def _atomic_edit(
    path: SecureToolPath, edits: list[tuple[str, str]], seen_files: SeenFiles | None = None
) -> FileVersion | None:
    """Apply ``edits`` and return the version they produced, if it could be verified."""

    if os.name == "nt":
        return _atomic_edit_windows(path, edits, seen_files)
    with open_parent(path) as parent:
        initial = stat_leaf(parent)
        if initial is None:
            raise ToolError(
                f"File does not exist: {path.display}",
                failure_code="not_found",
                retryable=True,
                recovery_hint="Check the path with find or ls, then retry.",
            )
        descriptor = _open_existing(parent, initial)
        try:
            opened = os.fstat(descriptor)
            version = file_version(opened)
            if seen_files is not None:
                seen_files.require_current(path.path, version, display=path.display)
            link_count = opened.st_nlink
            metadata = _snapshot_replacement_metadata(descriptor, opened)
            try:
                original = _read_descriptor(descriptor, path)
            except UnicodeDecodeError as exc:
                raise ToolError(f"File is not valid UTF-8: {path.display}") from exc
        finally:
            os.close(descriptor)

        replacement = _apply_edits(original, edits)
        if link_count > 1:
            return _write_existing_in_place(parent, replacement, expected=version)
        try:
            temporary, written = _write_temporary(parent, replacement, metadata=metadata)
        except ToolError as exc:
            if isinstance(exc.__cause__, PermissionError):
                return _write_existing_in_place(parent, replacement, expected=version)
            raise
        published = False
        try:
            current = stat_leaf(parent)
            if current is None or file_version(current) != version:
                raise ToolError(f"File changed while editing: {path.selected}")
            os.replace(
                temporary,
                parent.leaf,
                src_dir_fd=parent.fd,
                dst_dir_fd=parent.fd,
            )
            published = True
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(f"Could not edit file: {path.selected}: {exc}") from exc
        finally:
            _cleanup_temporary(parent, temporary, published=published)
        _sync_directory(parent)
        return _published_version(stat_leaf(parent), written)


def _windows_leaf_info(path: SecureToolPath) -> os.stat_result | None:
    try:
        info = path.path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ToolError(f"Could not inspect path {path.display}: {exc}") from exc
    if path.path.is_symlink() or path.path.is_junction():
        raise ToolError(f"Symbolic links and junctions are not allowed: {path.selected}")
    return info


def _write_windows_temporary(
    path: SecureToolPath, content: str, *, mode: int | None
) -> tuple[Path, os.stat_result]:
    for _attempt in range(10):
        temporary = path.path.parent / f".wisp-write-{uuid4().hex}"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise ToolError(f"Could not create file: {path.selected}: {exc}") from exc
    else:
        raise ToolError(f"Could not allocate temporary file for write: {path.selected}")
    try:
        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            written = os.fstat(file.fileno())
        return temporary, written
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise ToolError(f"Could not create file: {path.selected}: {exc}") from exc
        raise


def _cleanup_windows_temporary(temporary: Path, *, destination: Path, published: bool) -> None:
    try:
        temporary.unlink(missing_ok=True)
    except OSError as exc:
        if published:
            raise ToolError(
                "File was published but temporary-link cleanup failed; "
                f"destination: {destination}; temporary: {temporary}: {exc}"
            ) from exc
        raise ToolError(f"Could not clean up failed write {temporary}: {exc}") from exc


def _atomic_write_windows(
    path: SecureToolPath,
    content: str,
    *,
    overwrite: bool,
    seen_files: SeenFiles | None = None,
) -> _WriteOutcome:
    with open_windows_parent(path, create=True):
        initial = _windows_leaf_info(path)
        if not overwrite and initial is not None:
            raise ToolError(f"File already exists: {path.selected}")
        before_text: str | None = None
        initial_version: tuple[int, int, int, int, int] | None = None
        mode: int | None = None
        if initial is not None:
            initial_version = file_version(initial)
            mode = initial.st_mode
            try:
                with open_file(path) as descriptor:
                    opened = os.fstat(descriptor)
                    opened_version = file_version(opened)
                    if opened_version != initial_version:
                        raise ToolError(f"File changed while opening: {path.selected}")
                    initial_version = opened_version
                    mode = opened.st_mode
                    before_text = _snapshot_descriptor(descriptor)
            except ToolError as exc:
                if not isinstance(exc.__cause__, PermissionError):
                    raise
        if initial_version is not None and seen_files is not None:
            seen_files.require_current(path.path, initial_version, display=path.display)
        if overwrite and initial_version is not None:
            version = _write_path_existing_in_place(
                path,
                content,
                expected=initial_version,
                operation="writing",
            )
            return _WriteOutcome(False, before_text, None, version)
        temporary, written = _write_windows_temporary(path, content, mode=mode)
        published = False
        try:
            current = _windows_leaf_info(path)
            current_version = None if current is None else file_version(current)
            if current_version != initial_version:
                raise ToolError(f"File changed while writing: {path.selected}")
            if overwrite:
                os.replace(temporary, path.path)
            else:
                try:
                    os.link(temporary, path.path)
                except FileExistsError as exc:
                    raise ToolError(f"File already exists: {path.selected}") from exc
            published = True
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(f"Could not create file: {path.selected}: {exc}") from exc
        finally:
            _cleanup_windows_temporary(temporary, destination=path.path, published=published)
        file_id = (written.st_dev, written.st_ino)
        return _WriteOutcome(
            initial is None,
            before_text,
            file_id if not overwrite else None,
            _published_version(_windows_leaf_info(path), written),
        )


def _atomic_edit_windows(
    path: SecureToolPath, edits: list[tuple[str, str]], seen_files: SeenFiles | None = None
) -> FileVersion:
    with open_windows_parent(path):
        initial = _windows_leaf_info(path)
        if initial is None:
            raise ToolError(
                f"File does not exist: {path.display}",
                failure_code="not_found",
                retryable=True,
                recovery_hint="Check the path with find or ls, then retry.",
            )
        initial_version = file_version(initial)
        with open_file(path) as descriptor:
            opened = os.fstat(descriptor)
            version = file_version(opened)
            if version != initial_version:
                raise ToolError(f"File changed while opening: {path.selected}")
            if seen_files is not None:
                seen_files.require_current(path.path, version, display=path.display)
            try:
                original = _read_descriptor(descriptor, path)
            except UnicodeDecodeError as exc:
                raise ToolError(f"File is not valid UTF-8: {path.display}") from exc
        replacement = _apply_edits(original, edits)
        return _write_path_existing_in_place(
            path,
            replacement,
            expected=version,
            operation="editing",
        )


def _apply_edits(original: str, edits: list[tuple[str, str]]) -> str:
    """Replace each edit's ``oldText``, matched once in the original text.

    Edits are matched against the original rather than each other's results. An
    edit written with LF line endings is matched and written with CRLF where the
    file uses CRLF, so the model need not reproduce line endings it cannot see and
    lines outside the edits keep their exact bytes. An ``oldText`` with no exact
    match may still match whole lines that differ only in trailing whitespace;
    those lines are then replaced with ``newText``.

    Args:
        original (str): The file's current text.
        edits (list[tuple[str, str]]): ``(oldText, newText)`` pairs in call order.

    Returns:
        str: The edited text.

    Raises:
        ToolError: When an ``oldText`` is missing or ambiguous, two edits overlap,
            or the edits would leave the text unchanged.
    """

    replacements: list[tuple[int, int, str, int]] = []
    for index, (old_text, new_text) in enumerate(edits):
        old_text, new_text = _match_line_endings(original, old_text, new_text)
        start, end, new_text = _locate_edit(original, old_text, new_text, index)
        replacements.append((start, end, new_text, index))
    replacements.sort(key=lambda replacement: replacement[0])
    for previous, current in pairwise(replacements):
        if current[0] < previous[1]:
            first, second = sorted((previous[3], current[3]))
            raise ToolArgumentError(f"edits[{first}] and edits[{second}] overlap")
    parts: list[str] = []
    cursor = 0
    for start, end, new_text, _index in replacements:
        parts.extend((original[cursor:start], new_text))
        cursor = end
    parts.append(original[cursor:])
    edited = "".join(parts)
    if edited == original:
        raise ToolError(
            "The edits leave the file unchanged",
            failure_code="invalid_arguments",
            retryable=True,
            recovery_hint="Make newText differ from oldText, or skip the edit.",
        )
    return edited


def _locate_edit(original: str, old_text: str, new_text: str, index: int) -> tuple[int, int, str]:
    """Find where one edit applies and the text that replaces that span.

    Args:
        original (str): The file's current text.
        old_text (str): The edit's ``oldText``, already in the file's line endings.
        new_text (str): The edit's ``newText``, already in the file's line endings.
        index (int): The edit's position in the call, for error messages.

    Returns:
        tuple[int, int, str]: Start and end offsets in ``original`` and the
        replacement for that span.

    Raises:
        ToolError: When ``oldText`` matches nowhere, or more than one place.
    """

    occurrences = _find_occurrences(original, old_text)
    if len(occurrences) == 1:
        return occurrences[0], occurrences[0] + len(old_text), new_text
    if len(occurrences) > 1:
        raise _ambiguous_edit_error(index, len(occurrences))

    file_lines = _line_spans(original)
    old_lines, ends_with_newline = _edit_lines(old_text)
    windows = _matching_line_windows(original, file_lines, old_lines, str.rstrip)
    if len(windows) > 1:
        raise _ambiguous_edit_error(index, len(windows))
    if windows:
        matched = file_lines[windows[0] : windows[0] + len(old_lines)]
        endings = {original[line.end : line.end_with_newline] for line in matched}
        endings.discard("")
        if endings == {"\r\n"}:
            new_text = _to_crlf(new_text)
        end = matched[-1].end_with_newline if ends_with_newline else matched[-1].end
        return matched[0].start, end, new_text

    indented = _matching_line_windows(original, file_lines, old_lines, str.strip)
    if indented:
        raise ToolError(
            f"edits[{index}].oldText matches line {indented[0] + 1} only if indentation is ignored",
            failure_code="stale_input",
            retryable=True,
            recovery_hint="Reread those lines and copy their exact indentation into oldText.",
        )
    raise ToolError(
        f"edits[{index}].oldText was not found in the file",
        failure_code="stale_input",
        retryable=True,
        recovery_hint="Reread the relevant file range and retry with current exact text.",
    )


def _ambiguous_edit_error(index: int, count: int) -> ToolError:
    return ToolError(
        f"edits[{index}].oldText matches {count} places; it must match once",
        failure_code="invalid_arguments",
        retryable=True,
        recovery_hint="Include enough surrounding lines in oldText to make it unique.",
    )


@dataclass(frozen=True, slots=True)
class _LineSpan:
    start: int
    end: int
    """Offset where the line's content ends, before any LF or CRLF."""
    end_with_newline: int
    """Offset after the line's LF or CRLF, or ``end`` for an unterminated last line."""


def _line_spans(text: str) -> list[_LineSpan]:
    spans: list[_LineSpan] = []
    start = 0
    while (newline := text.find("\n", start)) != -1:
        end = newline - 1 if newline > start and text[newline - 1] == "\r" else newline
        spans.append(_LineSpan(start, end, newline + 1))
        start = newline + 1
    if start < len(text):
        spans.append(_LineSpan(start, len(text), len(text)))
    return spans


def _edit_lines(text: str) -> tuple[list[str], bool]:
    """Split edit text into lines and report whether it ended with a line ending."""

    ends_with_newline = text.endswith("\n")
    lines = text.removesuffix("\n").split("\n")
    return [line.removesuffix("\r") for line in lines], ends_with_newline


def _matching_line_windows(
    original: str,
    file_lines: list[_LineSpan],
    old_lines: list[str],
    normalize: Callable[[str], str],
) -> list[int]:
    """Return the first line of every run of file lines equal to ``old_lines``."""

    wanted = [normalize(line) for line in old_lines]
    if not any(wanted):
        return []
    keys = [normalize(original[line.start : line.end]) for line in file_lines]
    return [
        first
        for first in range(len(keys) - len(wanted) + 1)
        if keys[first : first + len(wanted)] == wanted
    ]


def _match_line_endings(original: str, old_text: str, new_text: str) -> tuple[str, str]:
    """Convert an edit to CRLF where the file uses it.

    A file with only CRLF endings gets every edit in CRLF. A file with mixed endings
    is converted only when the LF form of ``oldText`` is missing and the CRLF form
    is present.
    """

    if "\r\n" not in original:
        return old_text, new_text
    crlf_old_text = _to_crlf(old_text)
    only_crlf = original.count("\n") == original.count("\r\n")
    if only_crlf or (old_text not in original and crlf_old_text in original):
        return crlf_old_text, _to_crlf(new_text)
    return old_text, new_text


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


class EditTool:
    """Apply exact text replacements to a file."""

    name = "edit"
    safety: ToolSafety = "mutating"
    description = (
        "Apply unique, non-overlapping text replacements to a UTF-8 file. Each oldText "
        "must match the file exactly, apart from line endings and trailing whitespace. "
        "Read the file first; a file changed since you read it must be read again."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {"type": "string"},
                        "newText": {"type": "string"},
                    },
                    "required": ["oldText", "newText"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = secure_tool_path(_required_string(arguments, "path"), context, write=True)
        edits = _parse_edits(arguments)
        version = await anyio.to_thread.run_sync(
            lambda: _atomic_edit(path, edits, context.seen_files)
        )
        if context.seen_files is not None and version is not None:
            context.seen_files.record(path.path, version)
        return ToolResult(
            text=f"Applied {len(edits)} edit(s) to {path.display}",
            data={"path": path.display, "edits": len(edits)},
        )


def _published_version(
    current: os.stat_result | None, written: os.stat_result
) -> FileVersion | None:
    """Return the published file's version if it is still the file this write produced.

    Publishing by rename or link changes only the change time. Any other difference
    means another process replaced or rewrote the file after it was published; its
    content is then unseen, so no version is returned and the file stays stale.
    """

    if current is None:
        return None
    published = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
    if published != (written.st_dev, written.st_ino, written.st_size, written.st_mtime_ns):
        return None
    return file_version(current)


def _secure_read_line_slice(
    path: SecureToolPath,
    offset: int,
    limit: int | None,
    *,
    max_bytes: int,
    max_lines: int,
    notice_bytes: int,
) -> _ReadSlice:
    with open_file(path) as descriptor:
        before = file_version(os.fstat(descriptor))
        result = _read_line_slice(
            descriptor,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            max_lines=max_lines,
            notice_bytes=notice_bytes,
        )
        # A file rewritten while it was being read may return text from neither
        # version, so only a version that held for the whole read counts as seen.
        after = file_version(os.fstat(descriptor))
        return replace(result, version=before if before == after else None)


@dataclass(frozen=True, slots=True)
class _ReadSlice:
    text: str
    line_count: int | None
    """Total lines in the file, known only when the scan reached the end."""
    selected_count: int
    """Complete lines in ``text``."""
    truncated: bool
    partial_line: int | None = None
    """1-indexed line that ends ``text`` cut short because it exceeds the byte budget."""
    version: FileVersion | None = None
    """The read file's version, recorded as seen once the read succeeds."""


def _read_line_slice(
    descriptor: int | Path,
    *,
    offset: int,
    limit: int | None,
    max_bytes: int,
    max_lines: int,
    notice_bytes: int = 0,
) -> _ReadSlice:
    """Read whole lines from ``offset`` within a byte and line budget.

    A slice that stops before the end of the file gives back trailing lines until
    a continuation notice of ``notice_bytes`` fits on one more line of the same
    budget. A first line too long for the budget is returned cut short.
    """

    selected_parts: list[str] = []
    selected_bytes = 0
    scanned_count = 0

    def stop_before_eof(*, truncated: bool) -> _ReadSlice:
        nonlocal selected_bytes
        while len(selected_parts) > 1 and (
            len(selected_parts) + 1 > max_lines or selected_bytes + notice_bytes > max_bytes
        ):
            selected_bytes -= len(selected_parts.pop().encode("utf-8"))
            truncated = True
        if selected_parts and selected_bytes + notice_bytes > max_bytes:
            return _partial_line_slice(selected_parts[0], offset, max_bytes - notice_bytes)
        return _ReadSlice("".join(selected_parts), None, len(selected_parts), truncated)

    file_source = (
        os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="")
        if isinstance(descriptor, int)
        else descriptor.open("r", encoding="utf-8", newline="")
    )
    # Lines are read at most one character past the budget, so a huge line is never
    # held whole; cut that short, it still exceeds the budget as the whole line would.
    line_limit = max(0, max_bytes) + 1
    with file_source as file:
        readline = file.readline
        while line := readline(line_limit):
            if not line.endswith("\n") and not (line.endswith("\r") and len(line) < line_limit):
                _read_past_line(file, line, line_limit)
            scanned_count += 1
            if scanned_count < offset:
                continue

            if limit is not None and len(selected_parts) >= limit:
                # One-line lookahead distinguishes a slice ending exactly at EOF from
                # a bounded page with an unread tail. The consumed lookahead is not
                # part of the selected slice and is never presented as a file total.
                return stop_before_eof(truncated=False)

            if max_bytes <= 0 or max_lines <= 0 or len(selected_parts) >= max_lines:
                return stop_before_eof(truncated=True)

            encoded_line = line.encode("utf-8")
            if selected_bytes + len(encoded_line) > max_bytes:
                if selected_parts:
                    # The line may fit a page of its own, so the next read starts there.
                    return stop_before_eof(truncated=True)
                return _partial_line_slice(line, scanned_count, max_bytes - notice_bytes)

            selected_parts.append(line)
            selected_bytes += len(encoded_line)
    return _ReadSlice("".join(selected_parts), scanned_count, len(selected_parts), False)


def _read_past_line(file: TextIO, piece: str, limit: int) -> None:
    """Consume the rest of a line after ``readline(limit)`` returned ``piece``.

    ``piece`` lacks a line ending when the line is longer than ``limit``, and a CR that
    ends it at exactly ``limit`` characters may be the first half of a CRLF.
    """

    while not piece.endswith(("\n", "\r")):
        piece = file.readline(limit)
        if not piece:
            return
    if piece.endswith("\r") and len(piece) == limit:
        position = file.tell()
        if file.read(1) != "\n":
            file.seek(position)


def _partial_line_slice(line: str, line_number: int, max_bytes: int) -> _ReadSlice:
    text = line.encode("utf-8")[: max(0, max_bytes)].decode("utf-8", errors="ignore")
    return _ReadSlice(text, None, 0, True, partial_line=line_number)


def _parse_edits(arguments: Mapping[str, object]) -> list[tuple[str, str]]:
    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, list):
        raise ToolArgumentError("edits must be a list")
    if not raw_edits:
        raise ToolArgumentError("edits must not be empty")

    edits: list[tuple[str, str]] = []
    for raw_edit in raw_edits:
        if not isinstance(raw_edit, Mapping):
            raise ToolArgumentError("each edit must be an object")
        old_text = raw_edit.get("oldText")
        new_text = raw_edit.get("newText")
        if not isinstance(old_text, str) or old_text == "":
            raise ToolArgumentError("each edit.oldText must be a non-empty string")
        if not isinstance(new_text, str):
            raise ToolArgumentError("each edit.newText must be a string")
        edits.append((old_text, new_text))
    return edits


def _find_occurrences(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        position = text.find(needle, start)
        if position == -1:
            return positions
        positions.append(position)
        start = position + 1
