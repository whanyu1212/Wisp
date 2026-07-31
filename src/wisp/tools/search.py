"""Search and listing built-in tools."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_bool, _optional_int, _optional_string, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.paths import display_tool_path, is_protected_path, resolve_tool_path
from wisp.tools.process import ProcessResult as ProcessResult
from wisp.tools.process import _run_exec_limited_stdout
from wisp.tools.process_manager import ProcessSupervisor
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import truncate_text

IGNORED_DIRS = {
    ".git",
    ".codegraph",
    ".cocoindex_code",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
RG_MATCH_SEPARATOR = "\x1f"
RG_CONTEXT_SEPARATOR = "\x1e"


class GrepTool:
    """Search file contents."""

    name = "grep"
    safety: ToolSafety = "read"
    description = "Search text files with ripgrep when available, falling back to Python."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string"},
            "ignore_case": {"type": "boolean", "default": False},
            "literal": {"type": "boolean", "default": False},
            "context": {"type": "integer", "minimum": 0, "default": 0},
            "max_results": {"type": "integer", "minimum": 1, "default": 100},
        },
        "required": ["pattern"],
    }

    def __init__(self, process_supervisor: ProcessSupervisor | None = None) -> None:
        self._process_supervisor = process_supervisor or ProcessSupervisor()

    async def aclose(self) -> None:
        """Retry and release any retained search-process cleanup."""

        await self._process_supervisor.aclose()

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        pattern = _required_string(arguments, "pattern", allow_whitespace=True)
        path = resolve_tool_path(_optional_string(arguments, "path"), context)
        glob = _optional_string(arguments, "glob")
        ignore_case = _optional_bool(arguments, "ignore_case", default=False)
        literal = _optional_bool(arguments, "literal", default=False)
        context_lines = _optional_int(arguments, "context", default=0)
        max_results = _optional_int(arguments, "max_results", default=100)
        if context_lines is None or context_lines < 0:
            raise ToolError("grep.context must be greater than or equal to 0")
        if max_results is None or max_results < 1:
            raise ToolError("grep.max_results must be greater than or equal to 1")

        rg_path = shutil.which("rg")
        if rg_path is not None:
            return await _run_rg_grep(
                rg_path,
                pattern=pattern,
                path=path,
                glob=glob,
                ignore_case=ignore_case,
                literal=literal,
                context_lines=context_lines,
                max_results=max_results,
                context=context,
                process_supervisor=self._process_supervisor,
            )

        return _python_grep(
            pattern=pattern,
            path=path,
            glob=glob,
            ignore_case=ignore_case,
            literal=literal,
            context_lines=context_lines,
            max_results=max_results,
            context=context,
        )


class FindTool:
    """Find files by glob pattern."""

    name = "find"
    safety: ToolSafety = "read"
    description = "Find files with ripgrep when available, falling back to Python walking."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "default": "*"},
            "max_results": {"type": "integer", "minimum": 1, "default": 100},
        },
    }

    def __init__(self, process_supervisor: ProcessSupervisor | None = None) -> None:
        self._process_supervisor = process_supervisor or ProcessSupervisor()

    async def aclose(self) -> None:
        """Retry and release any retained search-process cleanup."""

        await self._process_supervisor.aclose()

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = resolve_tool_path(_optional_string(arguments, "path"), context)
        pattern = _optional_string(arguments, "pattern") or "*"
        max_results = _optional_int(arguments, "max_results", default=100)
        if max_results is None or max_results < 1:
            raise ToolError("find.max_results must be greater than or equal to 1")

        rg_path = shutil.which("rg")
        if rg_path is not None:
            return await _run_rg_find(
                rg_path,
                path=path,
                pattern=pattern,
                max_results=max_results,
                context=context,
                process_supervisor=self._process_supervisor,
            )

        return _python_find(path=path, pattern=pattern, max_results=max_results, context=context)


class LsTool:
    """List directory entries."""

    name = "ls"
    safety: ToolSafety = "read"
    description = "List a directory with sorted entries and '/' suffixes for directories."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "all": {"type": "boolean", "default": False},
        },
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = resolve_tool_path(_optional_string(arguments, "path"), context)
        include_hidden = _optional_bool(arguments, "all", default=False)
        if not path.is_dir():
            raise ToolError(f"Directory does not exist: {display_tool_path(path, context)}")

        entries = [
            entry for entry in path.iterdir() if include_hidden or not entry.name.startswith(".")
        ]
        entries.sort(key=lambda entry: entry.name.lower())
        names = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries]
        truncated = truncate_text(
            "\n".join(names),
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=truncated.text,
            data={"path": display_tool_path(path, context), "entries": names},
            truncated=truncated.truncated,
        )


async def _run_rg_grep(
    rg_path: str,
    *,
    pattern: str,
    path: Path,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context_lines: int,
    max_results: int,
    context: ToolContext,
    process_supervisor: ProcessSupervisor,
) -> ToolResult:
    effective_context_lines = _bounded_rg_context_lines(context_lines, context)
    context_truncated = effective_context_lines < context_lines
    command = [
        rg_path,
        *_rg_sandbox_args(context),
        "--line-number",
        "--no-heading",
        "--color=never",
        "--with-filename",
        "--field-match-separator",
        RG_MATCH_SEPARATOR,
        "--field-context-separator",
        RG_CONTEXT_SEPARATOR,
        "--max-columns",
        str(max(1, context.max_output_bytes)),
    ]
    if literal:
        command.append("--fixed-strings")
    if ignore_case:
        command.append("--ignore-case")
    if effective_context_lines:
        command.extend(("--context", str(effective_context_lines)))
    if glob:
        command.extend(("--glob", glob))
    command.extend(("--", pattern, _command_path(path, context)))

    # Group-aware, stateful pre-buffer filter. rg emits records (match/context,
    # each carrying the file path) fenced by bare "--" group separators when
    # context > 0. We must drop protected records BEFORE buffering — rg can still
    # emit them (a case-variant name its case-sensitive glob missed, or a caller
    # glob that re-includes a secret) and buffering them would let a large protected
    # match set exhaust the stdout buffer and kill rg before a later ordinary match
    # is read. The "--" separators carry no path, so they must be suppressed by
    # tracking whether the group they close actually kept anything; otherwise a run
    # of all-protected groups floods the buffer with separators alone.
    kept_since_separator = False

    def _keep_line(line: str) -> bool:
        nonlocal kept_since_separator
        if line == "--":
            # Emit a separator only after a group that kept content; drop it when the
            # preceding group was entirely protected (or already separated).
            emit = kept_since_separator
            kept_since_separator = False
            return emit
        if _rg_grep_line_is_protected(line, context):
            return False
        kept_since_separator = True
        return True

    def _is_reportable_match(line: str) -> bool:
        # Of the lines that survive _keep_line, count only match records so the
        # reported count and truncation flag are accurate.
        return _is_grep_match_line(line)

    result = await _run_exec_limited_stdout(
        command,
        cwd=context.cwd,
        process_supervisor=process_supervisor,
        max_stdout_lines=max_results + 1,
        stdout_line_filter=_keep_line,
        stdout_count_filter=_is_reportable_match,
        max_buffered_stdout_bytes=max(0, context.max_output_bytes),
        max_buffered_stdout_lines=max(0, context.max_output_lines),
        max_buffered_stderr_bytes=max(0, context.max_output_bytes),
        max_buffered_stderr_lines=max(0, context.max_output_lines),
    )
    if result.exit_code == 1 and not result.stdout.strip():
        return ToolResult(text="No matches", data={"count": 0, "matches": []})
    if result.exit_code != 0 and not result.stdout_truncated:
        raise ToolError(result.stderr.strip() or f"rg failed with exit code {result.exit_code}")

    return _result_from_grep_lines(
        _split_stdout_records(result.stdout),
        max_results=max_results,
        context=context,
        force_truncated=result.stdout_truncated or context_truncated,
        known_match_count=min(result.stdout_count, max_results),
    )


def _rg_sandbox_args(context: ToolContext) -> tuple[str, ...]:
    base = ("--no-config", "--follow" if context.allow_outside_cwd else "--no-follow")
    return base + _rg_protected_exclusions(context)


def _rg_protected_exclusions(context: ToolContext) -> tuple[str, ...]:
    """Emit ``--glob '!pattern'`` args so rg never reads protected secrets.

    rg's ``--glob`` uses gitignore-style patterns; a leading ``!`` excludes.
    Applying these at the rg level means a protected file's contents are never
    streamed into our buffer, mirroring the Python walk's ``is_protected_path``
    skip. Bare patterns are also emitted as ``**/pattern`` so they exclude at any
    depth, matching :func:`is_protected_path` semantics.
    """

    args: list[str] = []
    for pattern in context.protected_paths:
        normalized = pattern.replace("\\", "/")
        args.extend(("--glob", f"!{normalized}"))
        if "/" not in normalized:
            args.extend(("--glob", f"!**/{normalized}"))
    return tuple(args)


def _bounded_rg_context_lines(requested_context_lines: int, context: ToolContext) -> int:
    if requested_context_lines <= 0:
        return 0
    return min(requested_context_lines, max(0, (context.max_output_lines - 1) // 2))


def _split_stdout_records(stdout: str) -> list[str]:
    records = stdout.split("\n")
    if records and records[-1] == "":
        records.pop()
    return records


async def _run_rg_find(
    rg_path: str,
    *,
    path: Path,
    pattern: str,
    max_results: int,
    context: ToolContext,
    process_supervisor: ProcessSupervisor,
) -> ToolResult:
    if path.is_file():
        candidates = [path]
    else:

        def _reportable_file(line: str) -> bool:
            # Filter protected paths at the subprocess boundary so they never
            # consume a slot in the ``max_results + 1`` line budget — otherwise a
            # run of protected files could exhaust the budget before any reportable
            # file is seen, yielding a false "no matches".
            candidate = _path_from_rg_line(line, context)
            return _matches_glob(candidate, pattern, context) and not is_protected_path(
                candidate, context
            )

        result = await _run_exec_limited_stdout(
            [rg_path, *_rg_sandbox_args(context), "--files", "--", _command_path(path, context)],
            cwd=context.cwd,
            process_supervisor=process_supervisor,
            max_stdout_lines=max_results + 1,
            stdout_line_filter=_reportable_file,
            max_buffered_stderr_bytes=max(0, context.max_output_bytes),
            max_buffered_stderr_lines=max(0, context.max_output_lines),
        )
        if result.exit_code == 1 and not result.stdout.strip() and not result.stderr.strip():
            candidates = []
        elif result.exit_code != 0 and not result.stdout_truncated:
            raise ToolError(
                result.stderr.strip() or f"rg --files failed with exit code {result.exit_code}"
            )
        else:
            candidates = [_path_from_rg_line(line, context) for line in result.stdout.splitlines()]

    matches = [
        display_tool_path(candidate, context)
        for candidate in candidates
        if _matches_glob(candidate, pattern, context) and not is_protected_path(candidate, context)
    ]
    matches.sort()
    return _result_from_lines(
        matches, max_results=max_results, context=context, count_label="files"
    )


def _python_grep(
    *,
    pattern: str,
    path: Path,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context_lines: int,
    max_results: int,
    context: ToolContext,
) -> ToolResult:
    if not path.exists():
        raise ToolError(f"Path does not exist: {display_tool_path(path, context)}")

    matcher = _build_matcher(pattern, ignore_case=ignore_case, literal=literal)
    output: list[str] = []
    match_count = 0
    for file_path in _iter_files(path, context):
        if glob is not None and not _matches_glob(file_path, glob, context):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for index, line in enumerate(lines):
            if not matcher(line):
                continue
            if match_count >= max_results:
                return _result_from_grep_lines(
                    output,
                    max_results=max_results,
                    context=context,
                    force_truncated=True,
                )
            match_count += 1
            if context_lines:
                output.extend(
                    _format_context_lines(file_path, lines, index, context_lines, context)
                )
            else:
                output.append(f"{display_tool_path(file_path, context)}:{index + 1}:{line}")

    if not output:
        return ToolResult(text="No matches", data={"count": 0, "matches": []})
    return _result_from_grep_lines(
        output,
        max_results=max_results,
        context=context,
    )


def _python_find(
    *,
    path: Path,
    pattern: str,
    max_results: int,
    context: ToolContext,
) -> ToolResult:
    if not path.exists():
        raise ToolError(f"Path does not exist: {display_tool_path(path, context)}")
    matches = [
        display_tool_path(candidate, context)
        for candidate in _iter_files(path, context)
        if _matches_glob(candidate, pattern, context)
    ]
    matches.sort()
    return _result_from_lines(
        matches, max_results=max_results, context=context, count_label="files"
    )


def _iter_files(path: Path, context: ToolContext) -> Iterable[Path]:
    if path.is_file():
        if _is_path_within_tool_cwd(path, context):
            yield path
        return
    if not path.is_dir():
        return

    for root, dir_names, file_names in os.walk(path):
        dir_names[:] = sorted(
            name for name in dir_names if name not in IGNORED_DIRS and not _is_hidden(name)
        )
        for file_name in sorted(name for name in file_names if not _is_hidden(name)):
            candidate = Path(root) / file_name
            if _is_path_within_tool_cwd(candidate, context):
                yield candidate


def _is_path_within_tool_cwd(path: Path, context: ToolContext) -> bool:
    # Protected secrets (.env, keys) are excluded from find/grep results at any
    # depth so their names and contents never surface, even inside the sandbox.
    if is_protected_path(path, context):
        return False
    if context.allow_outside_cwd:
        return True
    try:
        path.resolve(strict=False).relative_to(context.cwd.resolve(strict=False))
    except ValueError:
        return False
    return True


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _matches_glob(path: Path, pattern: str, context: ToolContext) -> bool:
    display_path = display_tool_path(path, context)
    return fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(display_path, pattern)


def _build_matcher(
    pattern: str,
    *,
    ignore_case: bool,
    literal: bool,
) -> CallableMatcher:
    if literal:
        needle = pattern.casefold() if ignore_case else pattern

        def literal_matcher(line: str) -> bool:
            haystack = line.casefold() if ignore_case else line
            return needle in haystack

        return literal_matcher

    flags = re.IGNORECASE if ignore_case else 0
    try:
        expression = re.compile(pattern, flags=flags)
    except re.error as exc:
        raise ToolError(f"Invalid grep pattern: {exc}") from exc

    def regex_matcher(line: str) -> bool:
        return expression.search(line) is not None

    return regex_matcher


type CallableMatcher = Callable[[str], bool]


def _format_context_lines(
    file_path: Path,
    lines: Sequence[str],
    match_index: int,
    context_lines: int,
    context: ToolContext,
) -> list[str]:
    start = max(0, match_index - context_lines)
    end = min(len(lines), match_index + context_lines + 1)
    formatted: list[str] = []
    for index in range(start, end):
        separator = ":" if index == match_index else "-"
        path_display = display_tool_path(file_path, context)
        formatted.append(f"{path_display}{separator}{index + 1}{separator}{lines[index]}")
    return formatted


def _command_path(path: Path, context: ToolContext) -> str:
    return display_tool_path(path, context)


def _normalize_rg_line(line: str) -> str:
    line = _replace_rg_field_separator(line, RG_MATCH_SEPARATOR, ":")
    line = _replace_rg_field_separator(line, RG_CONTEXT_SEPARATOR, "-")
    if line.startswith("./"):
        return line[2:]
    return line


def _replace_rg_field_separator(line: str, field_separator: str, output_separator: str) -> str:
    parts = line.split(field_separator, 2)
    if len(parts) == 3 and parts[1].isdigit():
        return f"{parts[0]}{output_separator}{parts[1]}{output_separator}{parts[2]}"
    return line


def _is_grep_match_line(line: str) -> bool:
    return _grep_line_kind(line) == "match"


def _is_grep_context_line(line: str) -> bool:
    return _grep_line_kind(line) == "context"


def _grep_line_kind(line: str, context: ToolContext | None = None) -> str | None:
    if _has_rg_field_separator(line, RG_MATCH_SEPARATOR):
        return "match"
    if _has_rg_field_separator(line, RG_CONTEXT_SEPARATOR):
        return "context"

    if context is not None:
        kind = _grep_line_kind_from_existing_path(line, context)
        if kind is not None:
            return kind

    match_index = _find_line_number_separator(line, ":")
    context_index = _find_line_number_separator(line, "-")
    if match_index == -1 and context_index == -1:
        return None
    if context_index != -1 and (match_index == -1 or context_index < match_index):
        return "context"
    return "match"


def _has_rg_field_separator(line: str, field_separator: str) -> bool:
    parts = line.split(field_separator, 2)
    return len(parts) == 3 and parts[1].isdigit()


def _find_line_number_separator(line: str, separator: str) -> int:
    return next(_iter_line_number_separators(line, separator), -1)


def _iter_line_number_separators(line: str, separator: str) -> Iterator[int]:
    search_start = 0
    while True:
        first = line.find(separator, search_start)
        if first == -1:
            return
        digit_start = first + len(separator)
        if digit_start < len(line) and line[digit_start].isdigit():
            digit_end = digit_start + 1
            while digit_end < len(line) and line[digit_end].isdigit():
                digit_end += 1
            if line.startswith(separator, digit_end):
                yield first
        search_start = first + len(separator)


def _grep_line_kind_from_existing_path(line: str, context: ToolContext) -> str | None:
    candidates: list[tuple[int, str]] = []
    for separator, kind in ((":", "match"), ("-", "context")):
        for separator_index in _iter_line_number_separators(line, separator):
            if _grep_record_path_exists(line[:separator_index], context):
                candidates.append((separator_index, kind))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _grep_record_path_exists(path_text: str, context: ToolContext) -> bool:
    if not path_text:
        return False
    path = Path(path_text)
    if not path.is_absolute():
        path = context.cwd / path
    try:
        return path.resolve(strict=False).is_file()
    except OSError:
        return False


def _path_from_rg_line(line: str, context: ToolContext) -> Path:
    path = Path(line.rstrip("\r\n"))
    if not path.is_absolute():
        path = context.cwd / path
    if context.allow_outside_cwd:
        return path
    return path.resolve(strict=False)


def _rg_grep_line_is_protected(line: str, context: ToolContext) -> bool:
    """Return whether a grep output line belongs to a protected file.

    The ``rg --glob '!...'`` exclusions are only a best-effort speedup: they use
    gitignore glob semantics (case-sensitive, last-match-wins) that differ from
    :func:`is_protected_path`, and a caller-supplied ``--glob`` can re-include an
    excluded file. So ``is_protected_path`` is the single source of truth: every
    emitted record is post-filtered by extracting its file path and dropping the
    line if that path is protected.

    Extraction **fails closed**. rg's field separators are not escape-safe — a
    filename may itself contain the separator byte, and buffering may truncate a
    record mid-field — so a record whose path cannot be extracted *unambiguously*
    is treated as protected and dropped. This trades a rare false drop (a genuine
    non-secret line with a pathological name) for a guarantee that no secret line
    leaks through an ambiguous parse. Lines that are plainly not file records
    (no path field at all) are kept.
    """

    if not context.protected_paths:
        return False

    extraction = _grep_line_path_text(line)
    if extraction is None:
        return False  # not a file record (e.g. a separator line); nothing to protect
    path_text, unambiguous = extraction
    if not unambiguous:
        return True  # fail closed: ambiguous/truncated record — assume protected
    path = Path(path_text)
    if not path.is_absolute():
        path = context.cwd / path
    return is_protected_path(path, context)


def _grep_line_path_text(line: str) -> tuple[str, bool] | None:
    """Extract the file-path field from a grep output line.

    Returns ``None`` when the line carries no path field (not a file record).
    Otherwise returns ``(path_text, unambiguous)`` where ``unambiguous`` is False
    if the record cannot be split cleanly — e.g. the field-separated form does not
    have exactly a ``path SEP linenumber SEP text`` shape (a filename containing
    the separator byte, or a record truncated by output buffering). Callers must
    treat an ambiguous result as protected (fail closed).

    Accepted trade-off: a legitimate match whose *text* happens to contain the raw
    separator byte (``\\x1f`` / ``\\x1e``, control chars that essentially never
    occur in source or config) is reported as ambiguous and dropped. Losing such a
    rare match is preferable to leaking a secret through a mis-parsed path.
    """

    for separator in (RG_MATCH_SEPARATOR, RG_CONTEXT_SEPARATOR):
        if separator not in line:
            continue
        parts = line.split(separator)
        # A well-formed field-separated record is exactly:
        #   path SEP linenumber SEP text
        # More separators than that means the filename (or text) contains the
        # separator byte -> the path boundary is ambiguous. Fewer means the record
        # was truncated before the text field arrived.
        if len(parts) == 3 and parts[1].isdigit():
            return parts[0], True
        return "", False
    for separator in (":", "-"):
        index = _find_line_number_separator(line, separator)
        if index != -1:
            return line[:index], True
    return None


def _result_from_grep_lines(
    lines: Sequence[str],
    *,
    max_results: int,
    context: ToolContext,
    force_truncated: bool = False,
    known_match_count: int | None = None,
) -> ToolResult:
    kept: list[str] = []
    pending_context_after_limit: list[str] = []
    match_count = 0
    truncated_by_count = force_truncated
    for line in lines:
        if line == "--" and match_count >= max_results:
            kept.extend(pending_context_after_limit)
            pending_context_after_limit.clear()
            truncated_by_count = True
            break
        # Drop any line from a protected file that rg's --glob exclusions failed to
        # filter (case mismatch, or a caller --glob re-including it). is_protected_path
        # is the authoritative gate for both search engines.
        if _rg_grep_line_is_protected(line, context):
            pending_context_after_limit.clear()
            continue
        # A bare "--" group separator is only meaningful between two kept groups.
        # Emit it only when real content precedes it; otherwise dropping protected
        # groups would leave orphaned separators (and a "--" with count == 0).
        if line == "--":
            if kept and kept[-1] != "--":
                kept.append("--")
            continue
        line_kind = _grep_line_kind(line, context)
        if line_kind == "match":
            if match_count >= max_results:
                pending_context_after_limit.clear()
                truncated_by_count = True
                break
            kept.extend(pending_context_after_limit)
            pending_context_after_limit.clear()
            match_count += 1
            kept.append(_normalize_rg_line(line))
            continue
        if match_count >= max_results and line_kind == "context":
            pending_context_after_limit.append(_normalize_rg_line(line))
            continue
        if match_count >= max_results:
            pending_context_after_limit.clear()
            truncated_by_count = True
            break
        kept.extend(pending_context_after_limit)
        pending_context_after_limit.clear()
        kept.append(_normalize_rg_line(line))
    kept.extend(pending_context_after_limit)
    # Drop a trailing orphan separator left when the final group was dropped.
    while kept and kept[-1] == "--":
        kept.pop()
    effective_match_count = max(match_count, known_match_count or 0)
    if effective_match_count > match_count:
        truncated_by_count = True

    if not kept:
        if effective_match_count == 0:
            return ToolResult(text="No matches", data={"count": 0, "matches": []})
        truncated = truncate_text(
            "[truncated]",
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=truncated.text,
            data={"count": effective_match_count, "matches": []},
            truncated=True,
        )

    text = "\n".join(kept)
    if truncated_by_count:
        text = f"{text}\n[truncated]"
    truncated = truncate_text(
        text,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    return ToolResult(
        text=truncated.text,
        data={"count": effective_match_count, "matches": kept},
        truncated=truncated.truncated or truncated_by_count,
    )


def _result_from_lines(
    lines: Sequence[str],
    *,
    max_results: int,
    context: ToolContext,
    count_label: str,
    force_truncated: bool = False,
) -> ToolResult:
    total = len(lines)
    limited = list(lines[:max_results])
    truncated_by_count = total > max_results or force_truncated
    text = "\n".join(limited)
    if truncated_by_count:
        text = f"{text}\n[truncated]" if text else "[truncated]"
    truncated = truncate_text(
        text,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    if not limited and not truncated_by_count:
        label = "matches" if count_label == "matches" else "files"
        return ToolResult(text=f"No {label} found", data={"count": 0, count_label: []})
    return ToolResult(
        text=truncated.text,
        data={"count": total, count_label: limited},
        truncated=truncated.truncated or truncated_by_count,
    )
