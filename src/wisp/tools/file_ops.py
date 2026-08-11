"""File-oriented built-in tools."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

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
            selected, line_count, selected_count, stream_truncated = _read_line_slice(
                path,
                offset=offset,
                limit=limit,
                max_bytes=context.max_output_bytes,
                max_lines=context.max_output_lines,
            )
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_tool_path(path, context)}") from exc

        truncated = _truncate_text(selected, context=context, force_truncated=stream_truncated)
        return ToolResult(
            text=truncated.text,
            data={
                "path": display_tool_path(path, context),
                # ``line_count`` is the whole file; ``selected_count`` is the lines the
                # offset/limit slice returned. The summary needs the latter so it
                # doesn't claim the whole file was read when only a slice was.
                # (Truncation reaches the summary via ToolResult.truncated below.)
                "line_count": line_count,
                "selected_count": selected_count,
                "offset": offset,
                "limit": limit,
            },
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
        try:
            with path.open("w" if overwrite else "x", encoding="utf-8", newline="") as file:
                file.write(content)
        except FileExistsError as exc:
            raise ToolError(f"File already exists: {selected_path}") from exc
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


def _read_line_slice(
    path: Path,
    *,
    offset: int,
    limit: int | None,
    max_bytes: int,
    max_lines: int,
) -> tuple[str, int, int, bool]:
    selected_parts: list[str] = []
    line_count = 0
    selected_count = 0
    buffered_bytes = 0
    buffered_lines = 0
    truncated = False
    buffering = True

    with path.open("r", encoding="utf-8", newline="") as file:
        for line in file:
            line_count += 1
            if line_count < offset:
                continue
            if limit is not None and selected_count >= limit:
                continue

            selected_count += 1
            if not buffering:
                continue
            if max_bytes <= 0 or max_lines <= 0 or buffered_lines >= max_lines:
                truncated = True
                buffering = False
                continue

            encoded_line = line.encode("utf-8")
            remaining_bytes = max_bytes - buffered_bytes
            if remaining_bytes <= 0:
                truncated = True
                buffering = False
                continue
            if len(encoded_line) > remaining_bytes:
                selected_parts.append(
                    encoded_line[:remaining_bytes].decode("utf-8", errors="ignore")
                )
                truncated = True
                buffering = False
                continue

            selected_parts.append(line)
            buffered_bytes += len(encoded_line)
            buffered_lines += 1

    return "".join(selected_parts), line_count, selected_count, truncated


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
