"""Bounded previews and recoverable text from a codemode script."""

from __future__ import annotations

from wisp.tools.context import ToolContext
from wisp.tools.output_store import (
    OutputStore,
    SavedOutput,
    SaveStatus,
    UnsavedOutput,
    saved_output_notice,
)
from wisp.tools.truncation import truncate_text


async def save_output(text: str, store: OutputStore | None) -> SaveStatus:
    """Save bounded script text, discarding any unreported file on cancellation.

    Args:
        text (str): Nonempty output already bounded by the script's hard cap.
        store (OutputStore | None): The runtime's readable output store, if offered.

    Returns:
        SaveStatus: The completed artifact or an explicit recovery failure.
    """

    if store is None:
        return UnsavedOutput("saved-output recovery is unavailable")
    stream = store.open_stream("codemode script output", spill_threshold=64 * 1024)
    reported = False
    try:
        for offset in range(0, len(text), 16 * 1024):
            await stream.append(text[offset : offset + 16 * 1024].encode("utf-8"))
        stream.close()
        status = await stream.save()
        reported = isinstance(status, SavedOutput)
        return status or UnsavedOutput("no output was retained")
    finally:
        stream.close()
        if not reported:
            stream.discard()


def with_footer(
    body: str, footer: str, context: ToolContext, status: SaveStatus | None = None
) -> tuple[str, bool]:
    """Fit a script preview, preserving its recovery reference and call summary.

    Args:
        body (str): The complete bounded script output.
        footer (str): The call summary.
        context (ToolContext): Preview byte and line budgets.
        status (SaveStatus | None): Where omitted output was saved, or why not.

    Returns:
        tuple[str, bool]: The preview and whether any output was shortened.
    """

    max_bytes = max(0, context.max_output_bytes)
    max_lines = max(0, context.max_output_lines)
    notice = saved_output_notice([("script", status)])
    metadata = footer if notice is None else f"{notice}\n{footer}"
    if not _fits(metadata, max_bytes, max_lines):
        candidates: list[str] = []
        if isinstance(status, SavedOutput):
            compact = f"[output_id={status.output_id}; read_output]"
            candidates.extend((f"{compact}\n{footer}", compact, f"output_id={status.output_id}"))
        elif notice is not None:
            candidates.extend((notice, "[Output not saved]"))
        candidates.append(footer)
        metadata = next(
            (candidate for candidate in candidates if _fits(candidate, max_bytes, max_lines)),
            "",
        )
    if not metadata:
        return "", bool(body or footer or notice)
    bounded = truncate_text(
        body,
        max_bytes=max(0, max_bytes - len(metadata.encode("utf-8")) - 1),
        max_lines=max(0, max_lines - len(metadata.splitlines()) - 1),
    )
    text = f"{bounded.text}\n{metadata}" if bounded.text else metadata
    if not _fits(text, max_bytes, max_lines):
        return metadata, bool(body)
    return text, bounded.truncated


def _fits(text: str, max_bytes: int, max_lines: int) -> bool:
    return len(text.encode("utf-8")) <= max_bytes and len(text.splitlines()) <= max_lines
