"""One-line success summaries for read-type tools (issue #74 PR C).

A summary is a concise, factual sentence describing what a successful tool call
produced — ``read 42 lines from foo.py``, ``grep: 3 matches``, ``ls: 12 entries in
src/`` — shown in place of a raw output dump on the resolved tool card. It is built
from the *structured facts the tool already computed* (its ``ToolResult.data``),
never by re-parsing the formatted output text, so it can't drift from the result.

Only read/grep/find/ls have summaries; diff tools (edit/write) show a diff and bash
shows its exit status + tail, so those are handled elsewhere. The summary crosses the
RPC wire as a promoted event field, so it is bounded here at the source.
"""

from __future__ import annotations

from collections.abc import Mapping

# The summary is a one-liner, but a data value it interpolates (a path) can be long.
# It rides the RPC wire, so bound it at the source like every other promoted field;
# past this it is clipped with an ellipsis rather than shipped unbounded.
_SUMMARY_MAX_CHARS = 200

# Longest a single interpolated path may be before it is middle-truncated, so the
# useful head and tail (drive + filename) both survive.
_PATH_MAX_CHARS = 80


def summarize_tool_result(
    name: str, data: Mapping[str, object], *, truncated: bool = False
) -> str | None:
    """Return a one-line summary for a successful read-type tool, or ``None``.

    ``truncated`` is the tool's own ``ToolResult.truncated`` flag — the authoritative
    signal that the result was capped (by ``max_results`` or the output budget). It
    is threaded here rather than read from ``data`` so the summary carries the same
    "there's more" cue the raw output's ``[truncated]`` marker did; without it, a
    capped ``grep: 100 matches`` would silently drop that the shown results were cut.

    ``None`` for any tool without a summary (diff/shell tools, unknown tools) or
    when the tool's ``data`` lacks the facts a summary needs — the caller then falls
    back to the generic output preview. Never raises: a malformed ``data`` degrades
    to ``None`` rather than breaking the result card.
    """

    builder = _BUILDERS.get(name)
    if builder is None:
        return None
    summary = builder(data, truncated)
    if summary is None:
        return None
    return _clip(summary, _SUMMARY_MAX_CHARS)


def _summarize_read(data: Mapping[str, object], truncated: bool) -> str | None:
    line_count = data.get("line_count")
    if not isinstance(line_count, int):
        return None
    path = _path(data)
    suffix = f" from {path}" if path else ""

    # Three counts matter, and the summary must not overstate what the model saw:
    #   line_count     — the whole file
    #   selected_count — lines matching the offset/limit slice (may be < the file)
    #   truncated      — the output budget clipped even that slice, so FEWER lines
    #                    than selected_count were actually returned
    # ``line_count`` alone (what a naive summary would show) claims the whole file was
    # read even for a two-line page of a huge file — the summary replaces the raw
    # output, so that would actively mislead.
    selected = data.get("selected_count")

    if truncated:
        # The budget clipped the output, so no line count is exactly honest; say the
        # read was truncated and name the file size for scale rather than a number
        # that overstates what came back.
        return f"read (truncated){suffix} — {_count(line_count, 'line', 'lines')} total"
    if isinstance(selected, int) and selected < line_count:
        # A page of a larger file: report the slice returned and the file total.
        return f"read {_count(selected, 'line', 'lines')} of {line_count}{suffix}"
    # Whole file (or an older data shape without selected_count): the simple form.
    returned = selected if isinstance(selected, int) else line_count
    return f"read {_count(returned, 'line', 'lines')}{suffix}"


def _summarize_grep(data: Mapping[str, object], truncated: bool) -> str | None:
    count = data.get("count")
    if not isinstance(count, int):
        return None
    if count == 0:
        return "grep: no matches"
    return f"grep: {_count(count, 'match', 'matches')}{_more(truncated)}"


def _summarize_find(data: Mapping[str, object], truncated: bool) -> str | None:
    count = data.get("count")
    if not isinstance(count, int):
        return None
    if count == 0:
        return "find: no files"
    return f"find: {_count(count, 'file', 'files')}{_more(truncated)}"


def _summarize_ls(data: Mapping[str, object], truncated: bool) -> str | None:
    entry_count = data.get("entry_count")
    if type(entry_count) is int:
        count = entry_count
    else:
        entries = data.get("entries")
        if not isinstance(entries, list):
            return None
        count = len(entries)
    path = _path(data)
    if count == 0:
        return f"ls: empty ({path})" if path else "ls: empty"
    # ``entries`` is the kept (possibly capped) list, so a truncated ls means "at
    # least this many" — the "+ more" marker says the count is a floor, not the total.
    entry_count = _count(count, "entry", "entries")
    body = f"{entry_count} in {path}" if path else entry_count
    return f"ls: {body}{_more(truncated)}"


_BUILDERS = {
    "read": _summarize_read,
    "grep": _summarize_grep,
    "find": _summarize_find,
    "ls": _summarize_ls,
}


def _count(n: int, singular: str, plural: str) -> str:
    """``"1 line"`` / ``"3 lines"`` — count with a correctly pluralized noun."""

    return f"{n} {singular if n == 1 else plural}"


def _more(truncated: bool) -> str:
    """`` (+ more)`` when the result was capped, else empty — the truncation cue the
    raw output's ``[truncated]`` marker carried, which the summary replaces."""

    return " (+ more)" if truncated else ""


def _path(data: Mapping[str, object]) -> str | None:
    """The tool's display path from ``data``, middle-clipped if very long."""

    path = data.get("path")
    if not isinstance(path, str) or not path:
        return None
    return _clip_middle(path, _PATH_MAX_CHARS)


def _clip(text: str, limit: int) -> str:
    """Clip to ``limit`` characters with a trailing ellipsis when over."""

    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _clip_middle(text: str, limit: int) -> str:
    """Clip a path in the middle so its head and tail both survive."""

    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}…{text[len(text) - tail :]}"


__all__ = ["summarize_tool_result"]
