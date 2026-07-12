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
    data: Mapping[str, object],
) -> str:
    """Render terminal tool output into bounded, escaped card detail.

    ``name`` selects the renderer; ``arguments`` and ``data`` supply structured
    context when the tool is a recognized built-in. Unknown tools and
    unrecognized payloads fall back to :func:`render_generic`.
    """

    # PR A ships only the generic path + error rendering; built-in success
    # renderers (diffs, summaries) are added by later PRs, each routing here
    # first when it doesn't recognize the payload.
    if is_error:
        return render_error(output, data=data)
    return render_generic(output)


def render_error(output: str, *, data: Mapping[str, object]) -> str:
    """Render a failed tool call, surfacing exit status and the output tail.

    Errors are the biggest evidence gap today: the transcript shows only the
    first line, so a failure's actual cause (a stderr tail, a non-zero exit code)
    is lost. This renderer leads with the structured exit status when the tool
    supplied one, then shows the *tail* of the output (where errors live) rather
    than the head.

    The exit status comes from structured ``data`` (it isn't recoverable from the
    flat output without format-parsing); a zero code is suppressed as noise. The
    body is the *merged* output tail rather than the split-out ``stderr`` field,
    because a failing command's useful context often lives in stdout (a test
    runner printing failures, exit 1) and the merged output already contains
    stderr anyway.
    """

    lines: list[str] = []

    exit_code = data.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        lines.append(f"exit {exit_code}")

    tail = _tail_preview(output, max_lines=_ERROR_TAIL_LINES, max_bytes=_ERROR_TAIL_BYTES)
    if tail:
        lines.append(tail)

    return "\n".join(lines) if lines else "(no output)"


def _tail_preview(output: str, *, max_lines: int, max_bytes: int) -> str:
    """Bounded preview from the *tail* of output, with hidden-content metadata.

    Mirrors ``_preview_tool_output`` but keeps the last lines rather than the
    first, because tool failures surface at the end. Truncation stays honest: a
    ``... N earlier lines`` marker is prepended when content is dropped.
    """

    normalized = output.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized.strip():
        return ""

    lines = normalized.split("\n")
    kept = lines[-max(1, max_lines) :]
    preview = "\n".join(kept)

    encoded = preview.encode("utf-8")
    if len(encoded) > max_bytes:
        # Trim from the front of the byte window so the very tail survives.
        preview = encoded[-max(1, max_bytes) :].decode("utf-8", errors="ignore")

    hidden_lines = len(lines) - preview.count("\n") - 1
    if hidden_lines > 0:
        unit = "line" if hidden_lines == 1 else "lines"
        return f"... {hidden_lines} earlier {unit}\n{preview}"
    return preview


def render_generic(output: str) -> str:
    """Bounded, escaped fallback for unknown tools and recognized successes.

    The safety net every custom tool and every unrecognized payload lands on.
    Delegates to the existing bounded-preview helper at its default bounds so the
    generic case is byte-for-byte identical to the pre-#74 transcript.
    """

    return _preview_tool_output(output)
