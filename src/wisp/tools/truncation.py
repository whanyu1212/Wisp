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


def _suffix_for(text: str, suffix: str) -> str:
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{separator}{suffix}"
