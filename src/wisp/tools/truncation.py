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


def truncate_text_tail(text: str, *, max_bytes: int, max_lines: int) -> TruncatedText:
    """Limit text while preserving its diagnostic tail.

    Unlike :func:`truncate_text`, this helper discards from the beginning and
    places the truncation marker before the retained tail. It is intended for
    completed process output, where the final stderr or traceback lines carry
    more diagnostic value than the beginning.
    """

    lines = text.splitlines(keepends=True)
    truncated = len(lines) > max_lines
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
