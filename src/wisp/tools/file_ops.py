"""File-oriented built-in tools."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import anyio

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_bool, _optional_int, _required_string, _truncate_text
from wisp.tools.context import ToolContext
from wisp.tools.paths import display_tool_path, resolve_tool_path
from wisp.tools.result import ToolError, ToolResult

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
        path = resolve_tool_path(_required_string(arguments, "path"), context)
        offset = _optional_int(arguments, "offset", default=1)
        limit = _optional_int(arguments, "limit")

        if offset is None or offset < 1:
            raise ToolError("read.offset must be greater than or equal to 1")
        if limit is not None and limit < 1:
            raise ToolError("read.limit must be greater than or equal to 1")
        if not path.is_file():
            raise ToolError(f"File does not exist: {display_tool_path(path, context)}")

        try:
            slice_result = await anyio.to_thread.run_sync(
                lambda: _read_line_slice(
                    path,
                    offset=offset,
                    limit=limit,
                    max_bytes=context.max_output_bytes,
                    max_lines=context.max_output_lines,
                ),
                abandon_on_cancel=True,
            )
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_tool_path(path, context)}") from exc

        truncated = _truncate_text(
            slice_result.text,
            context=context,
            force_truncated=slice_result.truncated,
        )
        data: dict[str, object] = {
            "path": display_tool_path(path, context),
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
        path = resolve_tool_path(
            selected_path,
            context,
            follow_leaf_symlink=overwrite,
        )
        if context.allowed_write_paths is not None and path not in {
            allowed.resolve(strict=False) for allowed in context.allowed_write_paths
        }:
            raise ToolError(f"Write path is not allowed for this operation: {selected_path}")

        # Distinguish a create from an overwrite *before* the write, so the renderer
        # can tell "brand-new file" (show its content as a pure-addition diff) from
        # "overwrote an existing file whose prior text we couldn't capture" (fall back
        # to the plain summary — never imply a create by rendering pure additions).
        created = not path.exists() if overwrite else True
        # Snapshot the prior contents *before* the write clobbers them, so the TUI can
        # render a before/after diff. This is the only moment the "before" exists: the
        # open("w") below destroys it and the tool args carry only the new content. The
        # snapshot is None for a create AND for an unreadable/non-UTF-8/oversize prior
        # file; ``created`` is what separates those, since only a real create should
        # still render (as additions). Bounding here (not renderer-side) keeps the
        # snapshot off the RPC wire when it would be too large to diff anyway.
        before_text = _snapshot_before_write(path) if overwrite else None

        path.parent.mkdir(parents=True, exist_ok=True)
        for conflict in context.conflicting_write_paths:
            candidate = conflict if conflict.is_absolute() else context.cwd / conflict
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ToolError(f"Could not inspect conflicting write path: {conflict}") from exc
            raise ToolError(f"Conflicting write path already exists: {conflict}")
        if overwrite:
            with path.open("w", encoding="utf-8", newline="") as file:
                file.write(content)
        else:
            file_id = _write_create_only(path, content, selected_path=selected_path)
            if context.create_only_write_receipt is not None:
                context.create_only_write_receipt.record(path, file_id)
        byte_count = len(content.encode("utf-8"))
        data: dict[str, object] = {
            "path": display_tool_path(path, context),
            "bytes": byte_count,
            "created": created,
        }
        if before_text is not None:
            data["before_text"] = before_text
        return ToolResult(
            text=f"Wrote {byte_count} bytes to {display_tool_path(path, context)}",
            data=data,
        )


def _write_create_only(
    path: Path,
    content: str,
    *,
    selected_path: str,
) -> tuple[int, int]:
    """Publish complete content without exposing an empty or partial target."""

    descriptor, temporary = _open_write_temporary(path.parent, selected_path=selected_path)
    published = False
    file_id: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            descriptor = -1
            file.write(content)
            info = os.fstat(file.fileno())
            file_id = (info.st_dev, info.st_ino)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ToolError(f"File already exists: {selected_path}") from exc
        except OSError as exc:
            raise ToolError(f"Could not create file: {selected_path}: {exc}") from exc
        published = True
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError(f"Could not create file: {selected_path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            if not published:
                raise ToolError(
                    f"Could not clean up failed create-only write {temporary}: {exc}"
                ) from exc
            raise ToolError(
                "Create-only file was published but temporary-link cleanup failed; "
                f"destination: {path}; temporary: {temporary}: {exc}"
            ) from exc
    if file_id is None:
        raise ToolError(f"Could not record created file identity: {selected_path}")
    return file_id


def _open_write_temporary(directory: Path, *, selected_path: str) -> tuple[int, Path]:
    for _attempt in range(10):
        temporary = directory / f".wisp-write-{uuid4().hex}"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ToolError(f"Could not create file: {selected_path}: {exc}") from exc
        return descriptor, temporary
    raise ToolError(f"Could not allocate temporary file for create-only write: {selected_path}")


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
        path = resolve_tool_path(_required_string(arguments, "path"), context)
        edits = _parse_edits(arguments)
        if not path.is_file():
            raise ToolError(f"File does not exist: {display_tool_path(path, context)}")

        try:
            with path.open("r", encoding="utf-8", newline="") as file:
                original = file.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_tool_path(path, context)}") from exc

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
            parts.append(original[cursor:start])
            parts.append(new_text)
            cursor = end
        parts.append(original[cursor:])

        with path.open("w", encoding="utf-8", newline="") as file:
            file.write("".join(parts))
        return ToolResult(
            text=f"Applied {len(edits)} edit(s) to {display_tool_path(path, context)}",
            data={"path": display_tool_path(path, context), "edits": len(edits)},
        )


def _snapshot_before_write(path: Path) -> str | None:
    """Return the file's current text for a before/after write diff, or None.

    None means "no usable snapshot" — the file doesn't exist (a create, which the
    renderer shows as a pure addition), isn't valid UTF-8 (binary — a diff would be
    garbage), is too large to diff (``_WRITE_SNAPSHOT_MAX_CHARS``), or can't be read.
    Reads with ``newline=""`` so line terminators are preserved exactly, matching the
    write path — the diff must reflect real CRLF/LF changes, not translated ones.
    Never raises: a snapshot failure must not fail the write itself.
    """

    try:
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8", newline="") as file:
            before = file.read(_WRITE_SNAPSHOT_MAX_CHARS + 1)
    except (OSError, UnicodeDecodeError):
        return None
    if len(before) > _WRITE_SNAPSHOT_MAX_CHARS:
        return None
    return before


@dataclass(frozen=True, slots=True)
class _ReadSlice:
    text: str
    line_count: int | None
    selected_count: int
    truncated: bool


def _read_line_slice(
    path: Path,
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

    with path.open("r", encoding="utf-8", newline="") as file:
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
