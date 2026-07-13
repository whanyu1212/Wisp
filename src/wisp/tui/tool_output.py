"""Tool-aware rendering of terminal tool results for the TUI transcript.

This module turns a finished tool call into the bounded, escaped detail string
shown under a ``ToolCard``. It is the single seam introduced by issue #74: the
Textual renderer hands every ``ToolResultReady`` here instead of formatting the
raw output itself.

Design — strict core, tolerant edge:

* Built-in tools are a small, known set whose result shape we own. They get
  explicit rendering from promoted, typed facts — a shell ``exit_code``, an edit or
  write before/after diff, and a read-type tool's one-line summary — for
  high-fidelity detail.
* Custom / third-party tools are an open set we do not control. They fall through
  to a permissive generic renderer that never assumes a shape and degrades
  gracefully to bounded output.

The structured facts reach these functions as narrow, typed parameters (e.g.
``exit_code: int | None``) promoted agent-side for recognized tools, not as a raw
result mapping — so an unrelated tool can never drive failure styling, and the
signal stays bounded and serialization-safe across the RPC transport.

Return types and the untrusted-content rule:

* The error/generic renderers return a bounded plain ``str``. The widget renders
  it as literal, un-styled text (``Content.styled(..., "dim")``), so it is never
  parsed as markup — bracket characters in tool output stay literal.
* The diff renderers (edit and write) return a Textual ``Content`` they built
  themselves, adding every line of file content with ``Content.styled`` (literal
  text, out-of-band theme style). File content is likewise never parsed as markup,
  so a diff line containing ``[red]`` or ``[/]`` cannot inject or break a color span.

Either way, untrusted content is bounded and reaches the screen as literal text
with styles applied out-of-band — not through a markup parser.
"""

from __future__ import annotations

import difflib
import signal
from collections.abc import Mapping, Sequence

from textual.content import Content

from wisp.tui.widgets import (
    _TOOL_OUTPUT_PREVIEW_BYTES,
    _TOOL_OUTPUT_PREVIEW_LINES,
    _preview_tool_output,
)

# Theme style variables (resolved per active theme, so light/dark and any future
# a11y theme follow) applied to diff lines. Used with Content.styled, which keeps
# the underlying text literal — untrusted file content is never parsed as markup,
# so the color cannot be injected or escaped by the file's contents.
_DIFF_ADD_STYLE = "$success"
_DIFF_DEL_STYLE = "$error"
_DIFF_META_STYLE = "$text-muted"

# A colored diff is bounded like the other previews so one giant edit can't flood
# the transcript; the tail metadata stays honest about what was hidden.
_DIFF_PREVIEW_LINES = _TOOL_OUTPUT_PREVIEW_LINES
_DIFF_PREVIEW_BYTES = _TOOL_OUTPUT_PREVIEW_BYTES

# Upfront guard on the *work* of diffing, distinct from the display bounds above.
# difflib.unified_diff runs an O(n*m) SequenceMatcher whose full cost is paid the
# moment its generator is first advanced — so early-stopping its output cannot
# bound it; only refusing to start can. The dominant cost scales with *line count*
# (n and m), not bytes: one 2 MB line diffs in O(1), while tens of thousands of
# short lines is the expensive case. So we gate on per-side line count and, above
# the ceiling, skip difflib entirely and fall back to the generic summary. A
# whole-file replacement (the case this guards: e.g. 50k lines) is one giant hunk
# far over the ceiling and never reaches the matcher; an 8-line preview of a
# 50k-line rewrite is not review signal anyway.
#
# This bounds the matcher's *dimensions* and the transient diff-line count, which
# is what the reported flooding needed. It bounds the matcher's *time* too, but only
# to a sub-second ceiling, not a per-frame one. SequenceMatcher is O(n*m) in
# comparisons and each comparison is cheap line-equality, so typical at-ceiling
# inputs diff in tens of milliseconds; an adversarial arrangement can still reach a
# few hundred ms of synchronous work before difflib first yields (measurements of
# the pathological shapes vary by input and machine — assume up to ~1 s at the
# ceiling, not the low-ms of the common case). That is bounded and non-recurring, so
# it is acceptable inline for now; a hard per-frame latency bound would require
# off-thread diffing and is deliberately out of scope. The 4000-line ceiling is
# chosen to keep even that worst case sub-second while comfortably admitting any
# real human edit.
_DIFF_MAX_HUNK_LINES = 4000

# Many hunks each just under the per-hunk ceiling still sum to unbounded work, so
# cap the total lines diffed across all hunks of one edit call as a backstop.
_DIFF_MAX_TOTAL_LINES = 8000

# The line ceilings bound the matcher's *time* (SequenceMatcher is line-count
# driven), but not the *memory* to split and materialize diff lines. That memory is
# the input strings plus the ``splitlines`` list and the ``-``/``+`` line strings —
# all Python ``str``, whose size scales with character count (at most 4 bytes/char
# via PEP 393), never the UTF-8-encoded length. A minified or generated file is a
# few enormous lines — it sails past the line ceiling yet makes those allocations
# balloon before the display cap trims them. So we cap per-side *characters*: 1 M
# chars comfortably accepts a 4000-line file up to ~250 chars/line (well past any
# human edit) while rejecting the multi-MB single-line case. We deliberately count
# characters (``len``) rather than encoded bytes — the diff path never encodes the
# input, so char count is the faithful proxy for its allocation, and measuring
# ``len(text.encode())`` would itself allocate the very buffer we are guarding
# against.
_DIFF_MAX_HUNK_CHARS = 1_000_000

# Many hunks each just under the per-hunk char ceiling still sum to unbounded
# allocation (thousands of near-1M-char single-line changes), so cap the aggregate
# too — the character mirror of _DIFF_MAX_TOTAL_LINES.
_DIFF_MAX_TOTAL_CHARS = 2_000_000

# The single-character boundaries ``str.splitlines`` breaks on. The work guard
# below must count boundaries the SAME way the diff later splits them, or an input
# that splits into many lines slips past an undercounting guard and still starts
# the matcher. These are the ten single-code-point separators CPython's
# ``splitlines`` recognizes (``\r\n`` is handled separately as a two-char
# sequence): LF, CR, vertical tab, form feed, the file/group/record separators
# (but NOT the unit separator U+001F, which splitlines does not break on), NEL,
# and the Unicode line/paragraph separators. Counting each with ``str.count`` stays
# allocation-free — we never build the line list ``splitlines`` would.
_SPLITLINES_SEPARATORS = (
    "\n",  # line feed
    "\r",  # carriage return (lone; \r\n corrected below)
    "\v",  # vertical tab
    "\f",  # form feed
    "\x1c",  # file separator
    "\x1d",  # group separator
    "\x1e",  # record separator
    "\x85",  # next line (NEL)
    "\u2028",  # line separator
    "\u2029",  # paragraph separator
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
    before_text: str | None = None,
    created: bool = False,
    summary: str | None = None,
) -> str | Content:
    """Render terminal tool output into bounded card detail.

    ``name`` selects the renderer; ``arguments`` supplies structured context for
    recognized built-ins. ``exit_code`` is the promoted shell exit status, or None
    for tools without exit-code semantics. ``before_text`` is the promoted pre-write
    file snapshot for the write tool, or None; ``created`` says whether that write
    made a new file, which disambiguates a None snapshot (create vs. uncapturable
    overwrite). ``summary`` is the promoted one-line success summary for read-type
    tools (read/grep/find/ls), or None.

    Returns a plain ``str`` for the error/generic/summary paths (the widget escapes
    it as untrusted markup) or a Textual ``Content`` for a colored diff (already
    styled with literal, unparsed text — the widget renders it directly). Unknown
    tools and successful results without a summary fall back to
    :func:`render_generic`.
    """

    if tool_result_failed(is_error, exit_code):
        return render_error(output, exit_code=exit_code)
    # A successful edit carries its before/after text in the tool-call arguments
    # (oldText/newText per hunk); a write carries the "after" in its arguments and
    # the "before" in the promoted snapshot. Render either as a colored unified
    # diff. Anything unrecognized or malformed falls through to the generic preview.
    if name == "edit":
        diff = render_edit_diff(arguments)
        if diff is not None:
            return diff
    elif name == "write":
        diff = render_write_diff(before_text, arguments, created=created)
        if diff is not None:
            return diff
    # A read-type tool (read/grep/find/ls) carries a one-line success summary the
    # executor built from its structured data. The summary is only ever set for
    # those tools, so its presence is the signal — no per-tool check needed here.
    # It replaces the raw output dump; the full output returns via expand/collapse.
    if summary is not None:
        return summary
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

    status = _exit_status_line(exit_code)
    if status is not None:
        lines.append(status)

    # When a shell command produces no stdout/stderr, its output is a synthetic
    # "Command exited with code N" restatement of *this* exit code (see
    # _format_process_output). With a structured status line already shown, that
    # tail is pure duplication — and for a signal it would even restate the raw
    # negative code (`... code -15`), reintroducing the wording the status line
    # replaces. Drop it in that case, but only when the restated code matches the
    # promoted exit code, so a command whose genuine output merely resembles the
    # fallback (with a different number) is preserved.
    body = "" if status is not None and _is_exit_restatement(output, exit_code) else output
    tail = _tail_preview(body, max_lines=_ERROR_TAIL_LINES, max_bytes=_ERROR_TAIL_BYTES)
    if tail:
        lines.append(tail)

    return "\n".join(lines) if lines else "(no output)"


_EXIT_RESTATEMENT_PREFIX = "Command exited with code "


def _is_exit_restatement(output: str, exit_code: int | None) -> bool:
    """Whether output is solely the shell's synthetic restatement of exit_code.

    Mirrors the fallback in ``wisp.tools.process._format_process_output``, which
    emits exactly ``f"Command exited with code {exit_code}"`` when a command has
    no stdout/stderr. The match is exact except for a trailing newline (the only
    whitespace ``_tail_preview`` itself normalizes): genuine output that merely
    resembles the fallback — a different number, surrounding whitespace, or extra
    content — is never suppressed.

    Known residual ambiguity: a command whose *sole genuine* output is exactly
    this string while it exits with the matching code is indistinguishable from
    the synthetic fallback by text alone, so its output is suppressed. Fully
    resolving this needs an explicit synthetic-output flag propagated from the
    tool, which is deferred (see the truncation follow-up — same shape of
    cross-cutting field propagation). The collision is vanishingly rare and the
    cost is only a duplicated status line, so text matching is the right tradeoff
    for now.
    """

    if exit_code is None:
        return False
    return output.rstrip("\n") == f"{_EXIT_RESTATEMENT_PREFIX}{exit_code}"


def _exit_status_line(exit_code: int | None) -> str | None:
    """Human-readable status for a process exit code, or None to omit.

    A zero code is success (suppressed as noise). A negative code is POSIX
    signal termination — asyncio reports ``-N`` when the process was killed by
    signal ``N`` (including Wisp's own SIGKILL when a command exhausts the output
    budget) — so render it as the signal rather than a nonsensical ``exit -9``.
    """

    if exit_code is None or exit_code == 0:
        return None
    if exit_code < 0:
        return f"killed by {_signal_name(-exit_code)}"
    return f"exit {exit_code}"


def _signal_name(number: int) -> str:
    """`signal 9 (SIGKILL)` when the number is known, else `signal N`."""

    try:
        return f"signal {number} ({signal.Signals(number).name})"
    except ValueError:
        return f"signal {number}"


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
        # Trim from the front of the byte window so the very tail survives. If the
        # window begins mid-line (the byte just before it is not a newline), drop
        # the leading partial-line remnant up to the first newline so the preview
        # starts at a clean boundary — otherwise a trim landing on a separator
        # would leave a spurious blank first line and skew the line count. But when
        # the window happens to begin right after a newline, that first line is
        # complete and must be kept.
        window = max(1, max_bytes)
        starts_mid_line = encoded[-window - 1 : -window] != b"\n"
        clipped = encoded[-window:].decode("utf-8", errors="ignore")
        newline = clipped.find("\n")
        if starts_mid_line and newline != -1:
            clipped = clipped[newline + 1 :]
        preview = clipped

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


def render_edit_diff(arguments: Mapping[str, object]) -> Content | None:
    """Render an ``edit`` tool call's hunks as a colored, bounded unified diff.

    The before/after text lives in the tool-call arguments as
    ``edits[i]["oldText"]``/``["newText"]``. Returns a Textual ``Content`` with
    add/delete lines styled via theme variables, or ``None`` when the arguments
    are missing or malformed so the caller can fall back to the generic preview.

    Safety: every line of file content is added with :meth:`Content.styled`, which
    treats the text as literal and applies the style out-of-band. File content is
    never parsed as markup, so it cannot inject or escape a color span — no
    ``_markup_escape`` is needed because the text never reaches a markup parser.
    """

    edits = _parse_edit_hunks(arguments)
    # Filter no-op hunks once, here, so the work guard and the diff builder below
    # operate on the *same* changed-hunk list rather than each re-deciding which
    # hunks change. A single source of truth keeps the guard's accounting exactly
    # aligned with the work the diff actually does. Each changed hunk keeps its
    # original 1-based position so the per-hunk label names the user's edit, not a
    # re-indexed position; ``multi`` reflects the original edit count.
    multi = len(edits) > 1
    changed = [(index + 1, old, new) for index, (old, new) in enumerate(edits) if old != new]
    if not changed:
        # No hunks, or every hunk is a no-op: nothing to diff. Let the caller show
        # the generic "Applied N edit(s)" summary.
        return None

    path = arguments.get("path")
    return _render_diff_content(changed, multi=multi, path=path)


def render_write_diff(
    before: str | None,
    arguments: Mapping[str, object],
    *,
    created: bool = False,
) -> Content | None:
    """Render a ``write`` tool call's before/after text as a colored unified diff.

    ``before`` is the file's prior contents, captured by the tool before it
    overwrote them and carried on the result event. It is ``None`` in two very
    different cases that ``created`` disambiguates: a brand-new file (``created``
    True), rendered as a pure addition of the new content; or an overwrite whose
    prior text couldn't be captured — binary, oversize, or unreadable (``created``
    False), which must NOT masquerade as a create, so it returns ``None`` and the
    caller shows the plain "Wrote N bytes" summary. ``arguments`` supplies the new
    ``content`` (and ``path``). Also returns ``None`` on malformed arguments or a
    no-op write.

    Same safety property as :func:`render_edit_diff`: content is styled out-of-band
    via :meth:`Content.styled`, never parsed as markup.
    """

    after = arguments.get("content")
    if not isinstance(after, str):
        return None
    if before is None and not created:
        # Overwrote an existing file but couldn't snapshot its prior text (binary,
        # oversize, or unreadable). Rendering additions here would falsely imply the
        # file was created; fall back to the generic summary instead.
        return None
    old = str.__str__(before) if isinstance(before, str) else ""
    new = str.__str__(after)
    if old == new:
        # Content unchanged (e.g. rewriting a file with identical bytes): nothing to
        # diff. Fall back to the generic summary.
        return None

    path = arguments.get("path")
    return _render_diff_content([(1, old, new)], multi=False, path=path)


def _render_diff_content(
    changed: Sequence[tuple[int, str, str]],
    *,
    multi: bool,
    path: object,
) -> Content | None:
    """Bound, build, and header a colored unified diff from changed hunks.

    Shared tail of :func:`render_edit_diff` and :func:`render_write_diff`: the two
    differ only in how they derive the ``(label, old, new)`` hunks, so the work
    guard, the diff build, and the path header live here as a single source of
    truth. Returns ``None`` when the input is too large to diff or diffs to nothing.
    """

    # Bound the *work* before starting it: difflib's matcher cost is paid on first
    # consumption and cannot be recovered by trimming its output, so a whole-file
    # replacement must never reach it. Above the ceilings, return None and let the
    # caller show the generic summary instead of a diff.
    if _edit_input_too_large([(old, new) for _, old, new in changed]):
        return None

    diff_lines = _unified_diff_lines(changed, multi=multi)
    if not diff_lines:
        # Every changed hunk diffed to nothing (only line-terminator-invisible
        # differences difflib collapses). Fall back to the generic summary.
        return None

    diff = _content_from_diff_lines(diff_lines)

    # Lead with the file path so a resolved card names its file — the diff replaces
    # the argument summary, which otherwise carried the path. It's metadata, not a
    # diff line, so style it explicitly rather than routing it through the
    # marker-prefix styler (a bare path has no diff prefix).
    if isinstance(path, str) and path:
        return Content.styled(path, _DIFF_META_STYLE) + Content("\n") + diff
    return diff


def _parse_edit_hunks(arguments: Mapping[str, object]) -> list[tuple[str, str]]:
    """Extract ``(old_text, new_text)`` hunks from edit arguments, defensively.

    Returns an empty list on any structural surprise (missing/renamed keys, wrong
    types, non-string text) rather than raising, so a malformed payload degrades
    to the generic preview instead of crashing the transcript.

    Each side is normalized to a built-in ``str``. Arguments already arrive as
    JSON-decoded built-ins on every real path, but coercing here means the guard
    and the diff both operate on plain strings, so a ``str`` subclass with a
    stateful ``__eq__`` (which could answer the no-op check differently across the
    two passes and let unbounded work slip past the guard) cannot reach either.
    ``str.__str__`` is called unbound rather than ``str(old)`` so the result is an
    exact built-in even for a subclass whose ``__str__`` returns ``self``.
    """

    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, Sequence) or isinstance(raw_edits, str | bytes):
        return []
    hunks: list[tuple[str, str]] = []
    for item in raw_edits:
        if not isinstance(item, Mapping):
            return []
        old = item.get("oldText")
        new = item.get("newText")
        if not isinstance(old, str) or not isinstance(new, str):
            return []
        hunks.append((str.__str__(old), str.__str__(new)))
    return hunks


def _line_boundary_count(text: str) -> int:
    """Line-boundary count consistent with the ``splitlines`` used to diff.

    The guard must count boundaries the *same way* ``_single_hunk_lines`` later
    splits, or an input that splits into many lines slips past an undercounting
    guard and still starts the matcher. ``_single_hunk_lines`` splits with
    ``str.splitlines``, which breaks on ten single-code-point separators (the set
    in :data:`_SPLITLINES_SEPARATORS`: LF, CR, VT, FF, the file/group/record
    separators, NEL, and the Unicode line/paragraph separators) plus the two-char
    ``\\r\\n``. A guard that counted only ``\\n`` would let a large VT- or
    U+2028-delimited edit through — the same event-loop stall the guard exists to
    prevent — so we count *every* separator.

    Each separator is counted with allocation-free ``str.count`` (a C scan; ten
    of them still beat a Python per-character loop, and we never build the line
    list ``splitlines`` would). ``\\r\\n`` is subtracted once because the standalone
    ``\\r`` and ``\\n`` counts each already counted it, and ``splitlines`` treats it
    as a single boundary.
    """

    total = sum(text.count(sep) for sep in _SPLITLINES_SEPARATORS)
    return total - text.count("\r\n")


def _line_count(text: str) -> int:
    """The number of lines ``str.splitlines`` yields for ``text``, allocation-free.

    Equal to the separator count plus one for the trailing run of text — unless
    the text ends in a separator, since ``splitlines`` treats a terminator as
    *ending* a line, not starting a new one (``"a\\n"`` is one line, not two). An
    empty string is zero lines. Exact agreement with ``splitlines`` matters at the
    ceiling: an ordinary file ending in a newline would otherwise be miscounted
    by one and lose its diff a line early.

    Distinct from :func:`_line_boundary_count` — that returns separators; this
    returns lines — because the aggregate guard must count each hunk's real
    per-hunk cost. ``_unified_diff_lines`` runs difflib once per changed hunk, so
    a single-line hunk costs one run; counting *boundaries* there would score it
    free and let thousands of tiny hunks accumulate unbounded work. Counting
    *lines* charges each non-empty side at least one.
    """

    if not text:
        return 0
    boundaries = _line_boundary_count(text)
    return boundaries if text.endswith(_SPLITLINES_SEPARATORS) else boundaries + 1


def _edit_input_too_large(hunks: Sequence[tuple[str, str]]) -> bool:
    """Whether these changed hunks are too large to diff on the event loop.

    ``hunks`` are the *changed* hunks (``old != new``), filtered once by the caller
    so this guard and :func:`_unified_diff_lines` bound and do the exact same work
    — the guard can never charge for a hunk the diff skips, or vice versa.

    difflib's matcher is O(n*m) in *line count* and pays its full cost the moment
    its generator is first advanced, so the decision to diff has to be made before
    calling it. Cost has two independent axes — how many lines, and how many
    characters — and this guards both:

    * Line count — a single huge hunk is bounded per side against
      :data:`_DIFF_MAX_HUNK_LINES`, and the aggregate line count across many hunks
      (``_unified_diff_lines`` runs difflib once per changed hunk, so a batch of
      tiny replacements is as expensive as one giant hunk) against
      :data:`_DIFF_MAX_TOTAL_LINES`. Each hunk is charged at least one unit so even
      single-character edits accumulate toward the total.
    * Character count — the line ceilings bound the matcher's *time* but not the
      *memory* to split and materialize diff lines. That memory is Python ``str``
      (the input, the ``splitlines`` list, the ``-``/``+`` lines), so it scales with
      character count, not encoded bytes. A minified file is a few enormous lines
      (few lines, many characters), so each side is also bounded against
      :data:`_DIFF_MAX_HUNK_CHARS`.

    Both checks short-circuit, so an oversize input is rejected on its first scan.
    Line counts come from :func:`_line_count` and the character check from ``len`` —
    both allocation-free, so the decision never materializes what ``splitlines``
    would, nor encodes the input just to size it.
    """

    total_lines = 0
    total_chars = 0
    for old, new in hunks:
        if len(old) > _DIFF_MAX_HUNK_CHARS or len(new) > _DIFF_MAX_HUNK_CHARS:
            return True
        total_chars += len(old) + len(new)
        if total_chars > _DIFF_MAX_TOTAL_CHARS:
            return True
        old_lines = _line_count(old)
        new_lines = _line_count(new)
        if old_lines > _DIFF_MAX_HUNK_LINES or new_lines > _DIFF_MAX_HUNK_LINES:
            return True
        # A changed hunk always has a non-empty side, so old_lines + new_lines is
        # already >= 1; max keeps that floor explicit — difflib runs once per hunk
        # regardless of size, so every hunk must cost at least one aggregate unit.
        total_lines += max(1, old_lines + new_lines)
        if total_lines > _DIFF_MAX_TOTAL_LINES:
            return True
    return False


def _unified_diff_lines(hunks: Sequence[tuple[int, str, str]], *, multi: bool) -> list[str]:
    """Unified-diff lines across changed hunks, with a per-hunk label when multiple.

    ``hunks`` are ``(label, old, new)`` triples for the *changed* hunks only,
    pre-filtered by the caller (so this never diffs a no-op). Each hunk is diffed
    independently — the edit tool applies them independently — so a multi-edit
    call yields several small diffs. Lines keep their unified ``+``/``-``/`` ``/``
    @@`` prefixes; styling happens later off the prefix. ``label`` is the hunk's
    original 1-based position, and ``multi`` reflects whether the original call had
    more than one edit, so a label names the user's edit rather than a re-indexed
    changed-only position.

    A hunk whose difference is invisible to difflib (only a collapsed line
    terminator) contributes nothing — not even a label — so it is skipped here too.
    """

    lines: list[str] = []
    for label, old, new in hunks:
        body = _single_hunk_lines(old, new)
        if not body:
            continue  # difflib found nothing to show — no label, no body
        if multi:
            lines.append(f"@@ edit {label} @@")
        lines.extend(body)
    return lines


def _single_hunk_lines(old: str, new: str) -> list[str]:
    """Unified-diff body lines for one hunk, or an empty list when unchanged.

    Splits with ``keepends=True`` so a change confined to line terminators — a
    dropped trailing newline, a CRLF↔LF conversion — makes the affected lines
    differ and therefore show in the diff, instead of collapsing to identical
    line sequences and looking like a no-op. The kept terminator is stripped from
    each rendered line (so the display stays one entry per line) but its *kind*
    is annotated when notable, so ``a`` → ``a\\n`` reads differently from its
    reverse instead of both rendering as an unexplained ``-a`` / ``+a``.
    """

    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        lineterm="",
        n=2,
    )
    body: list[str] = []
    seen_hunk = False
    for line in diff:
        if line.startswith("@@"):
            # The first ``@@`` opens the hunk body; everything before it is the
            # ``---``/``+++`` file-header pair, which we drop. We must not skip by
            # prefix — a deleted content line such as ``--- comment`` (a source
            # line ``-- comment`` with difflib's ``-`` marker) shares that prefix.
            seen_hunk = True
            body.append(line)
            continue
        if not seen_hunk:
            continue  # difflib's blank file headers, positionally before any hunk
        # A +/-/space body line: strip the kept terminator from the content (its
        # job of making the line compare as changed is done), but annotate the
        # terminator's kind on changed lines so a newline-only edit stays
        # self-describing.
        marker = line[:1]
        content = line[1:]
        body.append(marker + content.rstrip("\r\n") + _terminator_note(marker, content))
    return body


def _terminator_note(marker: str, content: str) -> str:
    """A compact, literal annotation of a changed line's stripped terminator.

    Returns ``""`` for the ordinary LF ending (the common case, no noise) or a
    git-style marker for a notable one: ``⏎ CRLF`` (Windows), ``⏎ CR`` (a lone
    carriage return — classic Mac, which ``splitlines`` also treats as a line
    end), or ``⏎ no newline`` when the line had no terminator at all. Only
    ``+``/``-`` lines are annotated: an unchanged context line's terminator is
    the same before and after, so marking it would be noise. Annotating the
    changed sides is what lets a newline-only edit show its direction — ``a`` →
    ``a\\n`` annotates the deleted side ``no newline`` while the reverse annotates
    the added side, so the two differ.

    The endings are checked longest-first so ``\\r\\n`` is never misread as a
    lone ``\\r`` (it ends in ``\\n``, so only the explicit ``\\r\\n`` test matches
    it).
    """

    if marker not in ("+", "-"):
        return ""
    if content.endswith("\r\n"):
        return "  ⏎ CRLF"
    if content.endswith("\n"):
        return ""
    if content.endswith("\r"):
        return "  ⏎ CR"
    return "  ⏎ no newline"


def _content_from_diff_lines(diff_lines: Sequence[str]) -> Content:
    """Bounded ``Content`` from prefixed diff lines, colored by add/delete/meta.

    Bounds the diff on *both* axes — line count and byte size — so neither a
    diff with very many lines nor one with a single enormous line (a minified
    file, generated content) can flood the transcript. Each kept line's text is
    added with :meth:`Content.styled` so it stays literal, and an honest trailer
    reports whatever was dropped.
    """

    kept: list[str] = []
    used_bytes = 0
    for line in diff_lines[:_DIFF_PREVIEW_LINES]:
        # Count the newline that will join this line to the previous one, so the
        # budget bounds the whole rendered body — separators included — not just
        # the line text.
        separator = 1 if kept else 0
        remaining = _DIFF_PREVIEW_BYTES - used_bytes - separator
        if remaining <= 0:
            break
        clipped = _clip_line_to_bytes(line, remaining)
        kept.append(clipped)
        used_bytes += separator + len(clipped.encode("utf-8"))
        if clipped != line:
            break  # this line hit the byte budget; the rest is hidden

    hidden_lines = len(diff_lines) - len(kept)
    kept_bytes = sum(len(line.encode("utf-8")) for line in kept)
    total_bytes = sum(len(line.encode("utf-8")) for line in diff_lines)
    hidden_bytes = max(0, total_bytes - kept_bytes)

    content = Content("")
    for offset, line in enumerate(kept):
        if offset:
            content += Content("\n")
        content += Content.styled(line, _diff_line_style(line))

    trailer = _hidden_trailer(hidden_lines, hidden_bytes)
    if trailer is not None:
        content += Content("\n") + Content.styled(trailer, _DIFF_META_STYLE)
    return content


def _clip_line_to_bytes(line: str, max_bytes: int) -> str:
    """``line`` truncated to at most ``max_bytes`` UTF-8 bytes, on a char boundary.

    Returns the line unchanged when it already fits. Truncation decodes with
    ``errors="ignore"`` so a cut landing mid-multibyte-character drops that
    partial character rather than emitting invalid UTF-8, keeping the diff line
    literal and well-formed.
    """

    encoded = line.encode("utf-8")
    if len(encoded) <= max_bytes:
        return line
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _hidden_trailer(hidden_lines: int, hidden_bytes: int) -> str | None:
    """A ``... N more lines, M bytes hidden`` trailer, or None when nothing hid.

    Mirrors the honest-truncation trailer of :func:`_tail_preview`: it reports
    dropped lines and dropped bytes together so a diff clipped by the byte budget
    (a single giant line) is never shown as if it were complete.
    """

    if hidden_lines <= 0 and hidden_bytes <= 0:
        return None
    parts: list[str] = []
    if hidden_lines > 0:
        unit = "line" if hidden_lines == 1 else "lines"
        parts.append(f"{hidden_lines} more {unit}")
    if hidden_bytes > 0:
        parts.append(f"{hidden_bytes} bytes hidden")
    return f"... {', '.join(parts)}"


def _diff_line_style(line: str) -> str:
    """Theme style for a unified-diff line, chosen by its leading marker.

    ``@@`` range markers are metadata; ``+``/``-`` are additions/deletions;
    everything else is unchanged context (no style, so it reads as body text).
    """

    if line.startswith("@@"):
        return _DIFF_META_STYLE
    if line.startswith("+"):
        return _DIFF_ADD_STYLE
    if line.startswith("-"):
        return _DIFF_DEL_STYLE
    return ""
