"""Read output a tool saved because its result showed only part of it."""

from __future__ import annotations

from typing import Final

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_int, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.output_store import OutputPage, OutputStore
from wisp.tools.result import ToolArgumentError, ToolResult
from wisp.tools.truncation import truncate_text

# Room kept for the header and closing notice: their fixed wording plus the
# stream label (at most a short command summary) and a few numbers.
_NOTICE_RESERVED_BYTES: Final = 400
_NOTICE_RESERVED_LINES: Final = 2


class ReadOutputTool:
    """Page through output that a truncated tool result saved."""

    name = "read_output"
    safety: ToolSafety = "read"
    description = (
        "Read the full output a truncated tool result saved, by the output_id that "
        "result reported. Supports 1-indexed offset and line limit. Saved output lasts "
        "while this Wisp process runs."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "output_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["output_id"],
    }

    def __init__(self, store: OutputStore) -> None:
        self._store = store

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        output_id = _required_string(arguments, "output_id").strip()
        offset = _optional_int(arguments, "offset", default=1)
        limit = _optional_int(arguments, "limit")
        if offset is None or offset < 1:
            raise ToolArgumentError("read_output.offset must be greater than or equal to 1")
        if limit is not None and limit < 1:
            raise ToolArgumentError("read_output.limit must be greater than or equal to 1")

        page = await self._store.read_page(
            output_id,
            offset=offset,
            limit=limit,
            max_bytes=max(1, context.max_output_bytes - _NOTICE_RESERVED_BYTES),
            max_lines=max(1, context.max_output_lines - _NOTICE_RESERVED_LINES),
        )
        parts = [_page_header(page), *page.lines]
        if notice := _page_notice(page):
            parts.append(notice)
        bounded = truncate_text(
            "\n".join(parts),
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=bounded.text,
            data={
                "output_id": output_id,
                "offset": offset,
                "limit": limit,
                "selected_count": len(page.lines),
                "line_count": page.line_count,
            },
            truncated=bounded.truncated or page.next_offset is not None or page.line_truncated,
        )


def _page_header(page: OutputPage) -> str:
    if not page.lines:
        return f"[{page.label}: no lines]"
    last = page.offset + len(page.lines) - 1
    return f"[{page.label}: lines {page.offset}-{last} of {page.line_count}]"


def _page_notice(page: OutputPage) -> str | None:
    if page.next_offset is not None:
        return f"[More lines follow. Use offset={page.next_offset} to continue.]"
    if page.omitted_bytes:
        return (
            f"[{page.omitted_bytes} later bytes were not saved because a saved-output "
            "limit was reached. The command's own result shows how the output ended.]"
        )
    if not page.complete:
        return "[The command is still running; more output may be saved later.]"
    return None
