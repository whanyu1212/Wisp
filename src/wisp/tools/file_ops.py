"""File-oriented built-in tools."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import anyio

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_bool, _optional_int, _required_string, _truncate_text
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.secure_fs import (
    OpenParent,
    SecureToolPath,
    file_version,
    open_file,
    open_parent,
    open_windows_parent,
    secure_tool_path,
    stat_leaf,
)

# A write can overwrite an arbitrarily large file. The before-snapshot rides the RPC
# wire to the TUI as an event field, so cap it at the tool layer: past this size a
# diff isn't worth rendering (it exceeds the renderer's own work guard anyway), so we
# drop the snapshot and let the write fall back to its plain summary. Matched to the
# renderer's per-hunk character ceiling so the two bounds agree.
_WRITE_SNAPSHOT_MAX_CHARS = 1_000_000


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
        path = secure_tool_path(_required_string(arguments, "path"), context)
        offset = _optional_int(arguments, "offset", default=1)
        limit = _optional_int(arguments, "limit")

        if offset is None or offset < 1:
            raise ToolError("read.offset must be greater than or equal to 1")
        if limit is not None and limit < 1:
            raise ToolError("read.limit must be greater than or equal to 1")
        try:
            slice_result = await anyio.to_thread.run_sync(
                lambda: _secure_read_line_slice(path, offset, limit, context),
                abandon_on_cancel=True,
            )
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {path.display}") from exc

        truncated = _truncate_text(
            slice_result.text,
            context=context,
            force_truncated=slice_result.truncated,
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
            truncated=truncated.truncated,
        )


class WriteTool:
    """Create or overwrite UTF-8 text files."""

    name = "write"
    safety: ToolSafety = "mutating"
    description = (
        "Create or overwrite a UTF-8 text file, creating parent directories. "
        "Set overwrite=false to fail if the target already exists."
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
        if context.require_create_only_writes and overwrite:
            raise ToolError("This operation requires write calls with overwrite=false")
        if context.require_non_empty_writes and not content:
            raise ToolError("This operation requires non-empty write content")
        path = secure_tool_path(selected_path, context, write=True)
        outcome = await anyio.to_thread.run_sync(
            lambda: _atomic_write(path, content, overwrite=overwrite, context=context)
        )
        if outcome.file_id is not None and context.create_only_write_receipt is not None:
            context.create_only_write_receipt.record(path.path, outcome.file_id)
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


@dataclass(frozen=True, slots=True)
class _ReplacementMetadata:
    mode: int
    uid: int
    gid: int
    xattrs: tuple[tuple[str, bytes], ...] = ()
    flags: int | None = None


def _check_conflicting_paths(context: ToolContext) -> None:
    for conflict in context.conflicting_write_paths:
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
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ToolError(f"Not a regular file: {parent.path.display}")
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


def _read_descriptor(descriptor: int) -> str:
    with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as file:
        return file.read()


def _write_existing_in_place(
    parent: OpenParent,
    content: str,
    *,
    expected: tuple[int, int, int, int, int],
) -> None:
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(parent.leaf, flags, dir_fd=parent.fd)
    except OSError as exc:
        raise ToolError(f"Could not open file {parent.path.display}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or file_version(opened) != expected:
            raise ToolError(f"File changed while writing: {parent.path.selected}")
        os.ftruncate(descriptor, 0)
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8", newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        current = stat_leaf(parent)
        if current is None or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ToolError(f"File changed while writing: {parent.path.selected}")
    except OSError as exc:
        raise ToolError(f"Could not write file {parent.path.selected}: {exc}") from exc
    finally:
        os.close(descriptor)


def _snapshot_descriptor(descriptor: int) -> str | None:
    try:
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="") as file:
            before = file.read(_WRITE_SNAPSHOT_MAX_CHARS + 1)
    except (OSError, UnicodeDecodeError):
        return None
    return before if len(before) <= _WRITE_SNAPSHOT_MAX_CHARS else None


def _allocate_temporary(parent: OpenParent) -> tuple[int, str]:
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
            return os.open(name, flags, 0o666, dir_fd=parent.fd), name
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
) -> tuple[str, tuple[int, int]]:
    descriptor, name = _allocate_temporary(parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            if metadata is not None:
                _apply_replacement_metadata(file.fileno(), metadata)
            os.fsync(file.fileno())
            info = os.fstat(file.fileno())
            file_id = (info.st_dev, info.st_ino)
        return name, file_id
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


def _atomic_write(
    path: SecureToolPath,
    content: str,
    *,
    overwrite: bool,
    context: ToolContext,
) -> _WriteOutcome:
    _check_conflicting_paths(context)
    if os.name == "nt":
        return _atomic_write_windows(path, content, overwrite=overwrite)
    with open_parent(path, create=True) as parent:
        initial = stat_leaf(parent)
        if initial is not None and stat.S_ISLNK(initial.st_mode):
            if overwrite:
                raise ToolError(f"Symbolic links are not allowed: {path.selected}")
            raise ToolError(f"File already exists: {path.selected}")
        if not overwrite and initial is not None:
            raise ToolError(f"File already exists: {path.selected}")

        before_text: str | None = None
        initial_version: tuple[int, int, int, int, int] | None = None
        metadata: _ReplacementMetadata | None = None
        if initial is not None:
            initial_version = file_version(initial)
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
                metadata_descriptor = _open_metadata_descriptor(parent, initial)
                if metadata_descriptor is not None:
                    try:
                        opened = os.fstat(metadata_descriptor)
                        initial_version = file_version(opened)
                        metadata = _snapshot_replacement_metadata(metadata_descriptor, opened)
                    finally:
                        os.close(metadata_descriptor)
            else:
                try:
                    opened = os.fstat(descriptor)
                    initial_version = file_version(opened)
                    metadata = _snapshot_replacement_metadata(descriptor, opened)
                    before_text = _snapshot_descriptor(descriptor)
                finally:
                    os.close(descriptor)

        try:
            temporary, file_id = _write_temporary(parent, content, metadata=metadata)
        except ToolError as exc:
            if (
                overwrite
                and initial_version is not None
                and isinstance(exc.__cause__, PermissionError)
            ):
                _write_existing_in_place(parent, content, expected=initial_version)
                return _WriteOutcome(False, before_text, None)
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
        return _WriteOutcome(initial is None, before_text, file_id if not overwrite else None)


def _atomic_edit(path: SecureToolPath, edits: list[tuple[str, str]]) -> None:
    if os.name == "nt":
        _atomic_edit_windows(path, edits)
        return
    with open_parent(path) as parent:
        initial = stat_leaf(parent)
        if initial is None:
            raise ToolError(f"File does not exist: {path.display}")
        descriptor = _open_existing(parent, initial)
        try:
            opened = os.fstat(descriptor)
            version = file_version(opened)
            metadata = _snapshot_replacement_metadata(descriptor, opened)
            try:
                original = _read_descriptor(descriptor)
            except UnicodeDecodeError as exc:
                raise ToolError(f"File is not valid UTF-8: {path.display}") from exc
        finally:
            os.close(descriptor)

        replacement = _apply_edits(original, edits)
        try:
            temporary, _file_id = _write_temporary(parent, replacement, metadata=metadata)
        except ToolError as exc:
            if isinstance(exc.__cause__, PermissionError):
                _write_existing_in_place(parent, replacement, expected=version)
                return
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
) -> tuple[Path, tuple[int, int]]:
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
            info = os.fstat(file.fileno())
        return temporary, (info.st_dev, info.st_ino)
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


def _atomic_write_windows(path: SecureToolPath, content: str, *, overwrite: bool) -> _WriteOutcome:
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
                    initial_version = file_version(opened)
                    mode = opened.st_mode
                    before_text = _snapshot_descriptor(descriptor)
            except ToolError as exc:
                if not isinstance(exc.__cause__, PermissionError):
                    raise
        temporary, file_id = _write_windows_temporary(path, content, mode=mode)
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
        return _WriteOutcome(initial is None, before_text, file_id if not overwrite else None)


def _atomic_edit_windows(path: SecureToolPath, edits: list[tuple[str, str]]) -> None:
    with open_windows_parent(path):
        initial = _windows_leaf_info(path)
        if initial is None:
            raise ToolError(f"File does not exist: {path.display}")
        with open_file(path) as descriptor:
            opened = os.fstat(descriptor)
            version = file_version(opened)
            mode = opened.st_mode
            try:
                original = _read_descriptor(descriptor)
            except UnicodeDecodeError as exc:
                raise ToolError(f"File is not valid UTF-8: {path.display}") from exc
        replacement = _apply_edits(original, edits)
        temporary, _file_id = _write_windows_temporary(path, replacement, mode=mode)
        published = False
        try:
            current = _windows_leaf_info(path)
            if current is None or file_version(current) != version:
                raise ToolError(f"File changed while editing: {path.selected}")
            os.replace(temporary, path.path)
            published = True
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(f"Could not edit file: {path.selected}: {exc}") from exc
        finally:
            _cleanup_windows_temporary(temporary, destination=path.path, published=published)


def _apply_edits(original: str, edits: list[tuple[str, str]]) -> str:
    replacements: list[tuple[int, int, str]] = []
    for old_text, new_text in edits:
        occurrences = _find_occurrences(original, old_text)
        if len(occurrences) != 1:
            raise ToolError(
                f"edit.oldText must match exactly once; found {len(occurrences)} matches"
            )
        start = occurrences[0]
        replacements.append((start, start + len(old_text), new_text))
    replacements.sort(key=lambda replacement: replacement[0])
    previous_end = -1
    for start, end, _new_text in replacements:
        if start < previous_end:
            raise ToolError("edit replacements must not overlap")
        previous_end = end
    parts: list[str] = []
    cursor = 0
    for start, end, new_text in replacements:
        parts.extend((original[cursor:start], new_text))
        cursor = end
    parts.append(original[cursor:])
    return "".join(parts)


class EditTool:
    """Apply exact text replacements to a file."""

    name = "edit"
    safety: ToolSafety = "mutating"
    description = "Apply unique, non-overlapping exact text replacements to a UTF-8 file."
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
        await anyio.to_thread.run_sync(lambda: _atomic_edit(path, edits))
        return ToolResult(
            text=f"Applied {len(edits)} edit(s) to {path.display}",
            data={"path": path.display, "edits": len(edits)},
        )


def _secure_read_line_slice(
    path: SecureToolPath,
    offset: int,
    limit: int | None,
    context: ToolContext,
) -> _ReadSlice:
    with open_file(path) as descriptor:
        return _read_line_slice(
            descriptor,
            offset=offset,
            limit=limit,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )


@dataclass(frozen=True, slots=True)
class _ReadSlice:
    text: str
    line_count: int | None
    selected_count: int
    truncated: bool


def _read_line_slice(
    descriptor: int | Path,
    *,
    offset: int,
    limit: int | None,
    max_bytes: int,
    max_lines: int,
) -> _ReadSlice:
    selected_parts: list[str] = []
    scanned_count = 0
    selected_count = 0
    buffered_bytes = 0
    buffered_lines = 0
    truncated = False

    file_source = (
        os.fdopen(os.dup(descriptor), "r", encoding="utf-8", newline="")
        if isinstance(descriptor, int)
        else descriptor.open("r", encoding="utf-8", newline="")
    )
    with file_source as file:
        while True:
            try:
                line = next(file)
            except StopIteration:
                return _ReadSlice("".join(selected_parts), scanned_count, selected_count, truncated)

            scanned_count += 1
            if scanned_count < offset:
                continue

            if limit is not None and selected_count >= limit:
                # One-line lookahead distinguishes a slice ending exactly at EOF from
                # a bounded page with an unread tail. The consumed lookahead is not
                # part of the selected slice and is never presented as a file total.
                return _ReadSlice("".join(selected_parts), None, selected_count, truncated)

            selected_count += 1
            if max_bytes <= 0 or max_lines <= 0 or buffered_lines >= max_lines:
                return _ReadSlice("".join(selected_parts), None, selected_count, True)

            encoded_line = line.encode("utf-8")
            remaining_bytes = max_bytes - buffered_bytes
            if remaining_bytes <= 0:
                return _ReadSlice("".join(selected_parts), None, selected_count, True)
            if len(encoded_line) > remaining_bytes:
                selected_parts.append(
                    encoded_line[:remaining_bytes].decode("utf-8", errors="ignore")
                )
                return _ReadSlice("".join(selected_parts), None, selected_count, True)

            selected_parts.append(line)
            buffered_bytes += len(encoded_line)
            buffered_lines += 1


def _parse_edits(arguments: Mapping[str, object]) -> list[tuple[str, str]]:
    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, list):
        raise ToolError("edits must be a list")
    if not raw_edits:
        raise ToolError("edits must not be empty")

    edits: list[tuple[str, str]] = []
    for raw_edit in raw_edits:
        if not isinstance(raw_edit, Mapping):
            raise ToolError("each edit must be an object")
        old_text = raw_edit.get("oldText")
        new_text = raw_edit.get("newText")
        if not isinstance(old_text, str) or old_text == "":
            raise ToolError("each edit.oldText must be a non-empty string")
        if not isinstance(new_text, str):
            raise ToolError("each edit.newText must be a string")
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
