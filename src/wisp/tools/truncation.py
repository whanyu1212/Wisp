"""Helpers for bounded tool output."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TruncatedText:
    """Text plus whether it was shortened."""

    text: str
    truncated: bool


def truncate_text(text: str, *, max_bytes: int, max_lines: int) -> TruncatedText:
    """Limit text by line count and UTF-8 byte count."""

    truncated = False

    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        text = "".join(lines[:max_lines])
        truncated = True

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    if truncated:
        suffix = "[truncated]"
        suffix_text = _suffix_for(text, suffix)
        suffix_bytes = len(suffix_text.encode("utf-8"))
        if suffix_bytes > max_bytes:
            text = suffix.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            return TruncatedText(text=text, truncated=True)

        encoded = text.encode("utf-8")
        if len(encoded) + suffix_bytes > max_bytes:
            text = encoded[: max_bytes - suffix_bytes].decode("utf-8", errors="ignore")
            suffix_text = _suffix_for(text, suffix)
            suffix_bytes = len(suffix_text.encode("utf-8"))
            if suffix_bytes > max_bytes:
                text = suffix.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
                return TruncatedText(text=text, truncated=True)
            if len(text.encode("utf-8")) + suffix_bytes > max_bytes:
                text = text.encode("utf-8")[: max_bytes - suffix_bytes].decode(
                    "utf-8",
                    errors="ignore",
                )
        text += suffix_text

    return TruncatedText(text=text, truncated=truncated)


def truncate_text_tail(
    text: str,
    *,
    max_bytes: int,
    max_lines: int,
    input_truncated: bool = False,
) -> TruncatedText:
    """Limit text while preserving its diagnostic tail.

    Unlike :func:`truncate_text`, this helper discards from the beginning and
    places the truncation marker before the retained tail. It is intended for
    completed process output, where the final stderr or traceback lines carry
    more diagnostic value than the beginning. Set ``input_truncated`` when
    ``text`` is already a tail, so the marker appears even if it fits.
    """

    lines = text.splitlines(keepends=True)
    truncated = input_truncated or len(lines) > max_lines
    if truncated:
        text = "".join(lines[-max_lines:]) if max_lines > 0 else ""

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = True

    if not truncated:
        return TruncatedText(text=text, truncated=False)

    marker = "[truncated]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return TruncatedText(
            text=marker_bytes[:max_bytes].decode("utf-8", errors="ignore"),
            truncated=True,
        )

    tail_budget = max_bytes - len(marker_bytes) - 1
    if tail_budget <= 0:
        return TruncatedText(text=marker, truncated=True)
    tail_bytes = text.encode("utf-8")
    if len(tail_bytes) > tail_budget:
        tail_bytes = tail_bytes[-tail_budget:]
    tail = tail_bytes.decode("utf-8", errors="ignore")
    separator = " " if tail else ""
    return TruncatedText(text=f"{marker}{separator}{tail}", truncated=True)


def _suffix_for(text: str, suffix: str) -> str:
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{separator}{suffix}"


def truncate_text_middle(
    start: str,
    end: str,
    *,
    whole: bool,
    total_bytes: int,
    head_bytes: int,
    head_lines: int,
    tail_bytes: int,
    tail_lines: int,
    start_label: str = "",
) -> TruncatedText:
    """Keep the start and the end of long output and replace the middle with a marker.

    Long command output often carries its first error near the start and its summary
    or traceback at the end, with progress in between. The line budgets count every
    line break, carriage returns included, as the final bounding of each tool result
    does, so that bounding never cuts the tail kept here. The marker's line count is
    of newline-ended lines instead, an offset for `read_output`. The head and the tail
    keep whole lines where they can; a single line too long for its budget is cut.

    Args:
        start (str): Text from the output's first byte: all of the output, or a prefix.
        end (str): The output up to its last byte: all of it, or a suffix.
        whole (bool): Whether ``end`` is the entire output, so it is shown as is
            when it fits both budgets together.
        total_bytes (int): The whole output's UTF-8 size, for the marker's count.
        head_bytes (int): UTF-8 budget for the start.
        head_lines (int): Line budget for the start.
        tail_bytes (int): UTF-8 budget for the end.
        tail_lines (int): Line budget for the end.
        start_label (str): Names what ``start`` is the start of, such as `stdout`,
            when that differs from the whole output.

    Returns:
        TruncatedText: The whole output when allowed and it fits; otherwise its
        head, a `[truncated: ...]` line saying how many bytes were left out after
        how many lines, and its tail.

    Examples:
        >>> text = "".join(f"{n}\\n" for n in range(1, 11))
        >>> print(truncate_text_middle(text, text, whole=True, total_bytes=len(text),
        ...       head_bytes=99, head_lines=2, tail_bytes=99, tail_lines=2).text)
        1
        2
        [truncated: 12 bytes omitted after the first 2 lines]
        9
        10
        <BLANKLINE>
    """

    if whole and _fits(end, head_bytes + tail_bytes, head_lines + tail_lines):
        return TruncatedText(text=end, truncated=False)
    head = _leading_lines(start, max_bytes=head_bytes, max_lines=head_lines)
    tail = _trailing_lines(end, max_bytes=tail_bytes, max_lines=tail_lines)
    omitted = max(0, total_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8")))
    partial = bool(head) and not head.endswith("\n")
    shown = head.count("\n") + (1 if partial else 0)
    of = f" of {start_label}" if start_label else ""
    marker = f"[truncated: {omitted:,} bytes omitted after the first {shown} lines{of}]"
    separator = "\n" if partial else ""
    return TruncatedText(text=f"{head}{separator}{marker}\n{tail}", truncated=True)


_LINE_BREAKS = (b"\n", b"\r")


def _fits(text: str, max_bytes: int, max_lines: int) -> bool:
    return len(text.encode("utf-8")) <= max_bytes and len(text.splitlines()) <= max_lines


def _leading_lines(text: str, *, max_bytes: int, max_lines: int) -> str:
    """The first lines of ``text`` within both budgets, ending on a line break if it can."""

    kept = "".join(text.splitlines(keepends=True)[: max(0, max_lines)])
    encoded = kept.encode("utf-8")
    if len(encoded) <= max_bytes:
        return kept
    limit = max(0, max_bytes)
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    if encoded[limit : limit + 1] in _LINE_BREAKS:
        return cut  # The cut ends a complete line; only its line break is left out.
    line_end = max(cut.rfind("\n"), cut.rfind("\r"))
    return cut[: line_end + 1] if line_end >= 0 else cut


def _trailing_lines(text: str, *, max_bytes: int, max_lines: int) -> str:
    """The last lines of ``text`` within both budgets, starting on a line if it can."""

    lines = text.splitlines(keepends=True)
    kept = "".join(lines[-max_lines:]) if max_lines > 0 else ""
    encoded = kept.encode("utf-8")
    if len(encoded) <= max_bytes:
        return kept
    start = len(encoded) - max(0, max_bytes)
    while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
        start += 1
    cut = encoded[start:].decode("utf-8", errors="ignore")
    if encoded[start - 1 : start] in _LINE_BREAKS:
        return cut  # The cut starts a line.
    breaks = [index for index in (cut.find("\n"), cut.find("\r")) if index >= 0]
    line_start = min(breaks, default=-1)
    return cut[line_start + 1 :] if 0 <= line_start < len(cut) - 1 else cut
