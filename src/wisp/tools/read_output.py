"""Read output a tool saved because its result showed only part of it."""

from __future__ import annotations

from typing import Final

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_int, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.output_store import CharacterOutputPage, OutputPage, OutputStore
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult
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
        "Read saved output by the output_id a truncated tool result reported. "
        "Use 1-based offset and limit for lines, or 0-based character_offset and "
        "character_limit for exact Unicode character paging, including oversized lines. "
        "Do not mix line and character arguments. Saved output lasts while this Wisp "
        "process runs; saving limits may retain only its prefix."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "output_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
            "character_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based Unicode character offset; preserves original line endings.",
            },
            "character_limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum characters; output byte and line budgets still apply.",
            },
        },
        "required": ["output_id"],
    }

    def __init__(self, store: OutputStore) -> None:
        self._store = store

    @property
    def output_store(self) -> OutputStore:
        """Return the runtime store shared with tools that save output."""

        return self._store

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        output_id = _required_string(arguments, "output_id").strip()
        if "character_offset" in arguments or "character_limit" in arguments:
            if "offset" in arguments or "limit" in arguments:
                raise ToolArgumentError("Do not mix line and character pagination arguments")
            character_offset = _optional_int(arguments, "character_offset", default=0)
            character_limit = _optional_int(arguments, "character_limit")
            if character_offset is None or character_offset < 0:
                raise ToolArgumentError("read_output.character_offset must be nonnegative")
            if character_limit is not None and character_limit < 1:
                raise ToolArgumentError("read_output.character_limit must be positive")
            return await self._read_characters(
                output_id, character_offset, character_limit, context
            )
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
                **({"next_character_offset": page.character_offset} if page.line_truncated else {}),
            },
            truncated=bounded.truncated or page.line_truncated or _cut_short(page, limit),
        )

    async def _read_characters(
        self,
        output_id: str,
        character_offset: int,
        character_limit: int | None,
        context: ToolContext,
    ) -> ToolResult:
        page = await self._store.read_character_page(
            output_id,
            character_offset=character_offset,
            character_limit=character_limit,
            max_bytes=context.max_output_bytes - _NOTICE_RESERVED_BYTES,
            max_lines=context.max_output_lines - _NOTICE_RESERVED_LINES,
        )
        text = _character_page_text(page)
        # Never bound this text after computing its cursor: doing so would skip the
        # characters removed by that final bounding on the next page.
        if (
            len(text.encode("utf-8")) > context.max_output_bytes
            or len(text.splitlines()) > context.max_output_lines
        ):
            raise ToolError(
                "read_output output budget cannot fit the page and continuation notice",
                failure_code="invalid_arguments",
                recovery_hint="Increase the output byte and line budgets before continuing.",
            )
        return ToolResult(
            text=text,
            data={
                "output_id": output_id,
                "text": page.text,
                "character_offset": character_offset,
                "character_limit": character_limit,
                "selected_count": len(page.text),
                "character_count": page.character_count,
                "next_character_offset": page.next_character_offset,
            },
            truncated=page.next_character_offset is not None
            and (character_limit is None or len(page.text) < character_limit),
        )


def _cut_short(page: OutputPage, limit: int | None) -> bool:
    # As with `read`, a page that returned the lines the caller asked for is whole,
    # even when more follow; one the output budget stopped first is not.
    return page.next_offset is not None and (limit is None or len(page.lines) < limit)


def _page_header(page: OutputPage) -> str:
    if not page.lines:
        return f"[{page.label}: no lines]"
    last = page.offset + len(page.lines) - 1
    return f"[{page.label}: lines {page.offset}-{last} of {page.line_count}]"


def _page_notice(page: OutputPage) -> str | None:
    if page.line_truncated:
        return (
            "[Line shortened. Read its complete text with "
            f"character_offset={page.character_offset} and character_limit.]"
        )
    if page.next_offset is not None:
        return f"[More lines follow. Use offset={page.next_offset} to continue.]"
    return _stream_notice(page.omitted_bytes, page.complete)


def _stream_notice(omitted_bytes: int, complete: bool) -> str | None:
    if omitted_bytes:
        return (
            f"[{omitted_bytes} later bytes were not saved because a saved-output "
            "limit was reached. The original tool result may show how the output ended.]"
        )
    if not complete:
        return "[The tool is still running; more output may be saved later.]"
    return None


def _character_page_text(page: CharacterOutputPage) -> str:
    end = page.character_offset + len(page.text)
    text = (
        f"[{page.label}: characters {page.character_offset}-{end} of "
        f"{page.character_count} (0-based, end exclusive)]\n{page.text}"
    )
    notice: str | None
    if page.next_character_offset is not None:
        notice = (
            "[More characters follow. Use "
            f"character_offset={page.next_character_offset} to continue.]"
        )
    else:
        notice = _stream_notice(page.omitted_bytes, page.complete)
    if notice:
        separator = "" if text.endswith(tuple("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")) else "\n"
        text = f"{text}{separator}{notice}"
    return text
