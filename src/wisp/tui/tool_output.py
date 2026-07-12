"""Tool-aware rendering of terminal tool results for the TUI transcript.

This module turns a finished tool call into the bounded, escaped detail string
shown under a ``ToolCard``. It is the single seam introduced by issue #74: the
Textual renderer hands every ``ToolResultReady`` here instead of formatting the
raw output itself.

Design — strict core, tolerant edge:

* Built-in tools are a small, known set whose ``data`` shape we own. They get
  explicit renderers that read structured fields (exit codes, match counts, edit
  old/new text) and produce high-fidelity detail (error tails, diffs, summaries).
* Custom / third-party tools are an open set we do not control. They fall through
  to a permissive generic renderer that never assumes a shape and degrades
  gracefully to bounded output.

Every renderer returns an already-bounded, ``_markup_escape``-safe string. Output
is treated as untrusted: it is escaped at this boundary and never handed to the
Markdown parser. ``data`` from a custom tool is likewise untrusted input — the
dispatcher only routes to a typed renderer when both the tool name and the
payload shape are recognized, and otherwise falls back.
"""

from __future__ import annotations

from collections.abc import Mapping

from wisp.tui.widgets import (
    _TOOL_OUTPUT_PREVIEW_BYTES,
    _TOOL_OUTPUT_PREVIEW_LINES,
    _preview_tool_output,
)

# Errors surface at the tail, so the error preview keeps the last few lines
# (a traceback's final frames, a stderr tail) rather than the first. Bounds
# track the generic preview so a failure card is never larger than a success
# card; if the shared preview budget is retuned, both follow.
_ERROR_TAIL_LINES = _TOOL_OUTPUT_PREVIEW_LINES
_ERROR_TAIL_BYTES = _TOOL_OUTPUT_PREVIEW_BYTES


def render_tool_result(
    name: str,
    arguments: Mapping[str, object],
    output: str,
    *,
    is_error: bool,
    exit_code: int | None,
) -> str:
    """Render terminal tool output into bounded, escaped card detail.

    ``name`` selects the renderer; ``arguments`` supplies structured context for
    recognized built-ins (used by later PRs). ``exit_code`` is the promoted shell
    exit status, or None for tools without exit-code semantics. Unknown tools and
    successful results fall back to :func:`render_generic`.
    """

    # PR A ships only the generic path + error rendering; built-in success
    # renderers (diffs, summaries) are added by later PRs, each routing here
    # first when it doesn't recognize the result.
    if tool_result_failed(is_error, exit_code):
        return render_error(output, exit_code=exit_code)
    return render_generic(output)


def tool_result_failed(is_error: bool, exit_code: int | None) -> bool:
    """Whether a tool result should be presented as a failure.

    This is a presentation judgment, distinct from the event's ``is_error`` flag,
    and the renderer uses it for *both* the card status glyph and the detail body
    so the two never disagree. ``is_error`` means the tool mechanism failed
    (denied, raised, unknown tool); a command that ran fine but exited nonzero (a
    failing ``bash``) is *not* an ``is_error`` — that stays a normal,
    model-visible result on the wire. But its card should still read as a failure
    and surface the exit status, so a nonzero ``exit_code`` counts as failed here
    without touching ``is_error``. ``exit_code`` is already gated to shell-like
    tools by the executor, so this never spuriously reddens an unrelated tool.
    """

    if is_error:
        return True
    return exit_code is not None and exit_code != 0


def render_error(output: str, *, exit_code: int | None) -> str:
    """Render a failed tool call, surfacing exit status and the output tail.

    Errors are the biggest evidence gap today: the transcript shows only the
    first line, so a failure's actual cause (a stderr tail, a non-zero exit code)
    is lost. This renderer leads with the exit status when the tool supplied one,
    then shows the *tail* of the output (where errors live) rather than the head.

    The exit status is the promoted scalar (it isn't recoverable from the flat
    output without format-parsing); a zero code is suppressed as noise. The body
    is the merged output tail, because a failing command's useful context often
    lives in stdout (a test runner printing failures, exit 1).
    """

    lines: list[str] = []

    if exit_code is not None and exit_code != 0:
        lines.append(f"exit {exit_code}")

    tail = _tail_preview(output, max_lines=_ERROR_TAIL_LINES, max_bytes=_ERROR_TAIL_BYTES)
    if tail:
        lines.append(tail)

    return "\n".join(lines) if lines else "(no output)"


def _tail_preview(output: str, *, max_lines: int, max_bytes: int) -> str:
    """Bounded preview from the *tail* of output, with hidden-content metadata.

    Mirrors ``_preview_tool_output`` but keeps the last lines rather than the
    first, because tool failures surface at the end. Truncation stays honest: a
    leading marker reports both dropped lines and dropped bytes, so a single
    over-long line clipped by the byte budget is never shown as if complete.
    """

    normalized = output.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized.strip():
        return ""

    total_bytes = len(normalized.encode("utf-8"))
    lines = normalized.split("\n")
    kept = lines[-max(1, max_lines) :]
    preview = "\n".join(kept)

    encoded = preview.encode("utf-8")
    if len(encoded) > max_bytes:
        # Trim from the front of the byte window so the very tail survives.
        preview = encoded[-max(1, max_bytes) :].decode("utf-8", errors="ignore")

    visible_bytes = len(preview.encode("utf-8"))
    hidden_bytes = max(0, total_bytes - visible_bytes)
    if hidden_bytes == 0:
        return preview

    hidden_lines = len(lines) - preview.count("\n") - 1
    parts: list[str] = []
    if hidden_lines > 0:
        unit = "line" if hidden_lines == 1 else "lines"
        parts.append(f"{hidden_lines} earlier {unit}")
    parts.append(f"{hidden_bytes} bytes hidden")
    return f"... {', '.join(parts)}\n{preview}"


def render_generic(output: str) -> str:
    """Bounded, escaped fallback for unknown tools and recognized successes.

    The safety net every custom tool and every unrecognized payload lands on.
    Delegates to the existing bounded-preview helper at its default bounds so the
    generic case is byte-for-byte identical to the pre-#74 transcript.
    """

    return _preview_tool_output(output)
