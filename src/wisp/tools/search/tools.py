"""Search and listing built-in tools."""

from __future__ import annotations

import codecs
import errno
import heapq
import os
import re
import stat
import sys
import time
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from threading import Event
from typing import Literal, Protocol, cast

import anyio
import regex as bounded_regex
from pathspec import GitIgnoreSpec
from pathspec.patterns.gitignore import GitIgnorePatternError
from pathspec.patterns.gitignore.spec import GitIgnoreSpecPattern

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_bool, _optional_int, _optional_string, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.files.paths import (
    is_protected_path,
    matches_protected_pattern,
)
from wisp.tools.files.secure_fs import (
    SecureToolPath,
    open_directory,
    open_file,
    open_regular_file_at,
    secure_tool_path,
)
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult
from wisp.tools.truncation import truncate_text

# The TUI file picker imports this curated pruning set to keep indexing bounded.
# Recursive grep/find use repository ignore rules instead of pruning these names.
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
_REGEX_TIMEOUT_SECONDS = 0.05
# The regex timeout is wall-clock, so a CPU-starved thread can exceed it on a benign line.
# Each search may spend this much wall time re-trying timed-out lines before failing.
_REGEX_RETRY_BUDGET_SECONDS = 1.0
_IGNORE_FILES = (".gitignore", ".ignore", ".rgignore")
_MAX_IGNORE_FILE_BYTES = 1_000_000
_MAX_IGNORE_FILE_PATTERNS = 10_000
_MAX_DIRECTORY_ENTRIES = 100_000
_MAX_GIT_MARKER_BYTES = 4_096
_MAX_GLOB_ALTERNATIVES = 256


class _SearchInputLimitError(ToolError):
    """Raised when repository-controlled search input exceeds a safety budget."""


class _IgnoreFileLimitError(_SearchInputLimitError):
    """Raised when a repository ignore source exceeds its safety budget."""


class _DirectoryEntryLimitError(_SearchInputLimitError):
    """Raised when one repository directory exceeds its traversal budget."""


class _BinaryFileDetected(Exception):
    """Stop grep and discard pending output when a streamed file contains NUL."""


type _GrepScannerBackend = Literal["auto", "python", "native"]


class _NativeGrepCancellation(Protocol):
    """Cancellation flag shared with a native scan after it releases the GIL."""

    def cancel(self) -> None: ...


class _NativeGrepScanResult(Protocol):
    """Bounded per-file result returned by the optional native scanner."""

    @property
    def lines(self) -> list[str]: ...

    @property
    def byte_count(self) -> int: ...

    @property
    def match_count(self) -> int: ...

    @property
    def had_extra_match(self) -> bool: ...

    @property
    def exhausted(self) -> bool: ...

    @property
    def status(self) -> str: ...


class _NativeGrepModule(Protocol):
    """Subset of ``wisp._native`` used by descriptor-safe literal grep."""

    def GrepCancellation(self) -> _NativeGrepCancellation: ...

    def scan_literal_fd(
        self,
        fd: int,
        pattern: str,
        display_path: str,
        *,
        context_lines: int,
        remaining_matches: int,
        prior_lines: int,
        prior_bytes: int,
        prefix_separator: bool,
        max_output_lines: int,
        max_output_bytes: int,
        max_line_chars: int,
        max_display_chars: int,
        cancellation: _NativeGrepCancellation,
    ) -> _NativeGrepScanResult: ...


type _NativeWalkBatch = tuple[int, str, list[tuple[str, int, int, bool]]]


class _NativeWalk(Protocol):
    """Runs of files from the optional native walk, as ``(fd, relative, files)``.

    Each file is ``(name, st_dev, st_ino, hidden_or_ignored)``. A batch may be empty
    after many entries, so the caller can check for cancellation. Its descriptor
    stays open until the next batch is requested or the walk is closed.
    """

    def __iter__(self) -> Iterator[_NativeWalkBatch]: ...

    def close(self) -> None: ...


class _NativeWalkModule(Protocol):
    """Subset of ``wisp._native`` used by the descriptor-relative walk."""

    NativeWalkError: type[Exception]

    def NativeWalk(
        self,
        descriptor: int,
        *,
        ancestors: list[tuple[str, int, bytes]],
        in_git_repository: bool,
        include_ignored: bool,
        max_directory_entries: int,
        max_ignore_file_bytes: int,
        max_ignore_file_patterns: int,
        max_git_marker_bytes: int,
    ) -> _NativeWalk: ...


def _import_native_module() -> object | None:
    try:
        return import_module("wisp._native")
    except ModuleNotFoundError as exc:
        if exc.name == "wisp._native":
            return None
        raise


def _load_native_grep() -> _NativeGrepModule | None:
    """Load the optional native grep API without making pure wheels depend on it."""

    native_module = _import_native_module()
    if not hasattr(native_module, "GrepCancellation") or not hasattr(
        native_module, "scan_literal_fd"
    ):
        return None
    return cast(_NativeGrepModule, native_module)


_NATIVE_GREP = _load_native_grep()


def _load_native_walk() -> _NativeWalkModule | None:
    """Load the optional native walk where it decodes names as ``os.scandir`` does."""

    # The native walk matches names as UTF-8 with surrogateescape, which is how
    # Python decodes them only when the filesystem encoding is UTF-8.
    if (
        os.name == "nt"
        or sys.getfilesystemencoding() != "utf-8"
        or sys.getfilesystemencodeerrors() != "surrogateescape"
    ):
        return None
    native_module = _import_native_module()
    if not hasattr(native_module, "NativeWalk"):
        return None
    return cast(_NativeWalkModule, native_module)


_NATIVE_WALK = _load_native_walk()


def native_walk_available() -> bool:
    """Return whether grep and find walk directories natively, without holding the GIL."""

    return _NATIVE_WALK is not None


class GrepTool:
    """Search file contents."""

    name = "grep"
    safety: ToolSafety = "read"
    description = "Search UTF-8 text files. Symbolic links found while searching are skipped."
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

    def __init__(self, *, _scanner_backend: _GrepScannerBackend = "auto") -> None:
        if _scanner_backend not in {"auto", "python", "native"}:
            raise ValueError(f"unknown grep scanner backend: {_scanner_backend}")
        self._scanner_backend = _scanner_backend

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        pattern = _required_string(arguments, "pattern", allow_whitespace=True)
        path = secure_tool_path(_optional_string(arguments, "path"), context, follow_links=True)
        glob = _optional_string(arguments, "glob")
        ignore_case = _optional_bool(arguments, "ignore_case", default=False)
        literal = _optional_bool(arguments, "literal", default=False)
        context_lines = _optional_int(arguments, "context", default=0)
        max_results = _optional_int(arguments, "max_results", default=100)
        if context_lines is None or context_lines < 0:
            raise ToolArgumentError("grep.context must be greater than or equal to 0")
        if max_results is None or max_results < 1:
            raise ToolArgumentError("grep.max_results must be greater than or equal to 1")

        native_module = _select_native_grep(
            self._scanner_backend,
            literal=literal,
            ignore_case=ignore_case,
        )
        if not _native_arguments_supported(
            pattern,
            max_results,
            context_lines,
            context.max_output_lines,
            context.max_output_bytes,
        ):
            native_module = None
        native_cancellation = (
            native_module.GrepCancellation() if native_module is not None else None
        )
        cancel_event = Event()
        try:
            return await anyio.to_thread.run_sync(
                lambda: _python_grep(
                    pattern=pattern,
                    path=path,
                    glob=glob,
                    ignore_case=ignore_case,
                    literal=literal,
                    context_lines=context_lines,
                    max_results=max_results,
                    context=context,
                    cancel_event=cancel_event,
                    native_module=native_module,
                    native_cancellation=native_cancellation,
                ),
                abandon_on_cancel=True,
            )
        finally:
            cancel_event.set()
            if native_cancellation is not None:
                native_cancellation.cancel()


class FindTool:
    """Find files by glob pattern."""

    name = "find"
    safety: ToolSafety = "read"
    description = "Find files by glob. Symbolic links found while searching are skipped."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "default": "*"},
            "max_results": {"type": "integer", "minimum": 1, "default": 100},
        },
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = secure_tool_path(_optional_string(arguments, "path"), context, follow_links=True)
        pattern = _optional_string(arguments, "pattern") or "*"
        max_results = _optional_int(arguments, "max_results", default=100)
        if max_results is None or max_results < 1:
            raise ToolArgumentError("find.max_results must be greater than or equal to 1")

        cancel_event = Event()
        try:
            return await anyio.to_thread.run_sync(
                lambda: _python_find(
                    path=path,
                    pattern=pattern,
                    max_results=max_results,
                    context=context,
                    cancel_event=cancel_event,
                ),
                abandon_on_cancel=True,
            )
        finally:
            cancel_event.set()


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
        path = secure_tool_path(_optional_string(arguments, "path"), context, follow_links=True)
        include_hidden = _optional_bool(arguments, "all", default=False)
        return await anyio.to_thread.run_sync(
            lambda: _python_ls(path=path, include_hidden=include_hidden, context=context),
            abandon_on_cancel=True,
        )


def _bounded_context_lines(requested_context_lines: int, context: ToolContext) -> int:
    if requested_context_lines <= 0:
        return 0
    return min(requested_context_lines, max(0, (context.max_output_lines - 1) // 2))


def _select_native_grep(
    backend: _GrepScannerBackend,
    *,
    literal: bool,
    ignore_case: bool,
) -> _NativeGrepModule | None:
    """Select the native kernel only where its matching semantics are exact."""

    supported = literal and not ignore_case
    if backend == "python" or not supported:
        return None
    if _NATIVE_GREP is None:
        if backend == "native":
            raise RuntimeError("the native grep backend is not installed")
        return None
    return _NATIVE_GREP


def _native_arguments_supported(pattern: str, *limits: int) -> bool:
    """Return whether PyO3 can represent the scanner inputs without changing semantics."""

    try:
        pattern.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(0 <= limit <= sys.maxsize for limit in limits)


def _python_grep(
    *,
    pattern: str,
    path: Path | SecureToolPath,
    glob: str | None,
    ignore_case: bool,
    literal: bool,
    context_lines: int,
    max_results: int,
    context: ToolContext,
    cancel_event: Event | None = None,
    native_module: _NativeGrepModule | None = None,
    native_cancellation: _NativeGrepCancellation | None = None,
) -> ToolResult:
    secure_path = _coerce_secure_path(path, context)

    matcher = (
        None
        if native_module is not None
        else _build_matcher(pattern, ignore_case=ignore_case, literal=literal)
    )
    block_matcher = _build_block_matcher(pattern, ignore_case=ignore_case, literal=literal)
    effective_context_lines = _bounded_context_lines(context_lines, context)
    context_truncated = effective_context_lines < context_lines
    output: list[str] = []
    output_bytes = 0
    match_count = 0
    glob_matcher = _prepare_glob_matcher(glob) if glob is not None else None
    walked_display_root = context.cwd.resolve(strict=False)
    cached_file_path: Path | None = None
    cached_display_path = ""

    def display_path(file_path: Path) -> str:
        nonlocal cached_file_path, cached_display_path
        if file_path != cached_file_path:
            cached_file_path = file_path
            cached_display_path = _display_walked_path(file_path, walked_display_root)
        return cached_display_path

    ignore_override_matcher: PathMatcher | None = None
    if glob_matcher is not None and glob is not None and not _is_exclusion_glob(glob):

        def matches_ignore_override(candidate: Path) -> bool:
            return glob_matcher(display_path(candidate))

        ignore_override_matcher = matches_ignore_override

    files = (
        _iter_walked_files(secure_path, context, cancel_event=cancel_event)
        if ignore_override_matcher is None
        else _iter_walked_files(
            secure_path,
            context,
            ignore_override_matcher=ignore_override_matcher,
            cancel_event=cancel_event,
        )
    )
    with closing(files):
        for walked_file in files:
            file_path = walked_file.path
            if cancel_event is not None and cancel_event.is_set():
                return ToolResult(text="Search cancelled", data={"count": 0, "matches": []})
            file_display_path: str | None = None
            if glob_matcher is not None:
                file_display_path = display_path(file_path)
                if not glob_matcher(file_display_path):
                    continue

            file_match_start = match_count
            file_had_extra_match = False
            file_native_module = native_module
            if file_native_module is not None:
                if file_display_path is None:
                    file_display_path = _display_walked_path(file_path, walked_display_root)
                try:
                    file_display_path.encode("utf-8")
                except UnicodeEncodeError:
                    file_native_module = None
                    matcher = _build_matcher(pattern, ignore_case=ignore_case, literal=literal)
            file_buffer = _BoundedGrepFileOutput(
                prior_lines=len(output),
                prior_bytes=output_bytes,
                prefix_separator=bool(output and effective_context_lines),
                max_lines=context.max_output_lines,
                max_bytes=context.max_output_bytes,
            )

            try:
                with walked_file.open(context) as descriptor:
                    if file_native_module is not None:
                        if native_cancellation is None:  # pragma: no cover - internal invariant
                            raise RuntimeError("native grep cancellation was not initialized")
                        if file_display_path is None:  # pragma: no cover - internal invariant
                            raise RuntimeError("native grep display path was not initialized")
                        try:
                            native_result = file_native_module.scan_literal_fd(
                                descriptor,
                                pattern,
                                file_display_path,
                                context_lines=effective_context_lines,
                                remaining_matches=max_results - match_count,
                                prior_lines=len(output),
                                prior_bytes=output_bytes,
                                prefix_separator=bool(output and effective_context_lines),
                                max_output_lines=context.max_output_lines,
                                max_output_bytes=context.max_output_bytes,
                                max_line_chars=_PYTHON_GREP_MAX_LINE_CHARS,
                                max_display_chars=_GREP_MAX_DISPLAY_CHARS,
                                cancellation=native_cancellation,
                            )
                        except ValueError as exc:
                            raise ToolError(str(exc)) from exc
                        except (OSError, RuntimeError):
                            # Some Linux sandboxes do not mount /proc, and unsupported
                            # targets cannot duplicate a descriptor for Rust. Keep the
                            # optional accelerator transparent in those environments.
                            file_native_module = None
                            matcher = _build_matcher(
                                pattern, ignore_case=ignore_case, literal=literal
                            )
                        if file_native_module is not None:
                            if native_result.status == "cancelled":
                                return ToolResult(
                                    text="Search cancelled",
                                    data={"count": 0, "matches": []},
                                )
                            if native_result.status == "binary":
                                continue
                            if native_result.status != "complete":
                                raise RuntimeError(
                                    f"native grep returned unknown status: {native_result.status}"
                                )
                            match_count += native_result.match_count
                            file_had_extra_match = native_result.had_extra_match
                            file_buffer.lines = list(native_result.lines)
                            file_buffer.byte_count = native_result.byte_count
                            file_buffer.exhausted = native_result.exhausted
                    if file_native_module is None:
                        if matcher is None:  # pragma: no cover - internal invariant
                            raise RuntimeError("Python grep matcher was not initialized")
                        preceding: deque[tuple[int, str]] = deque(maxlen=effective_context_lines)
                        group_end = 0
                        last_emitted_line = 0
                        line_number = 0
                        for block in _iter_utf8_line_blocks(descriptor):
                            if cancel_event is not None and cancel_event.is_set():
                                return ToolResult(
                                    text="Search cancelled",
                                    data={"count": 0, "matches": []},
                                )
                            if (
                                block_matcher is not None
                                and line_number >= group_end
                                and not block_matcher(block)
                            ):
                                # No line here matches and no context is pending, so
                                # only the line count and the preceding context move.
                                tail = block[max(0, len(block) - effective_context_lines) :]
                                first = line_number + len(block) - len(tail) + 1
                                preceding.extend(enumerate(tail, start=first))
                                line_number += len(block)
                                continue
                            for line in block:
                                line_number += 1
                                if cancel_event is not None and cancel_event.is_set():
                                    return ToolResult(
                                        text="Search cancelled",
                                        data={"count": 0, "matches": []},
                                    )
                                is_match = matcher(line)
                                if is_match:
                                    if match_count >= max_results:
                                        file_had_extra_match = True
                                    else:
                                        match_count += 1
                                        if file_display_path is None:
                                            file_display_path = display_path(file_path)
                                        if line_number > group_end:
                                            for number, text in preceding:
                                                if number > last_emitted_line:
                                                    file_buffer.append(
                                                        _format_grep_record(
                                                            file_display_path, number, text, False
                                                        )
                                                    )
                                                    last_emitted_line = number
                                                    if file_buffer.exhausted:
                                                        break
                                        file_buffer.append(
                                            _format_grep_record(
                                                file_display_path, line_number, line, True
                                            ),
                                            preserve_match=True,
                                        )
                                        last_emitted_line = line_number
                                        group_end = max(
                                            group_end, line_number + effective_context_lines
                                        )
                                elif line_number <= group_end:
                                    if file_display_path is None:
                                        file_display_path = display_path(file_path)
                                    file_buffer.append(
                                        _format_grep_record(
                                            file_display_path, line_number, line, False
                                        )
                                    )
                                    last_emitted_line = line_number
                                preceding.append((line_number, line))
                                if file_buffer.exhausted or file_had_extra_match:
                                    break
                            if file_buffer.exhausted or file_had_extra_match:
                                break
            except _WalkedFileChanged:
                continue
            except (_BinaryFileDetected, UnicodeDecodeError):
                match_count = file_match_start
                continue

            if file_buffer.lines:
                if effective_context_lines and output:
                    output.append("--")
                    output_bytes += 3  # "\n--"
                output.extend(file_buffer.lines)
                output_bytes += file_buffer.byte_count

            if file_had_extra_match or file_buffer.exhausted:
                return _grep_result(
                    output,
                    match_count=match_count,
                    max_results=max_results,
                    context=context,
                    truncated=True,
                    match_limit_reached=file_had_extra_match,
                )

    return _grep_result(
        output,
        match_count=match_count,
        max_results=max_results,
        context=context,
        truncated=context_truncated and bool(output),
    )


_PYTHON_GREP_CHUNK_BYTES = 64 * 1024
_PYTHON_GREP_MAX_LINE_CHARS = 1_000_000
# Characters of a matched or context line shown in a record. Longer lines, such as
# minified code, are clipped so one line cannot fill the output budget.
_GREP_MAX_DISPLAY_CHARS = 500
_SPLITLINES_BOUNDARIES = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _display_walked_path(path: Path, resolved_cwd: Path) -> str:
    """Display a secure-walk candidate without resolving the same file again."""

    if os.name == "nt":
        # WindowsPath.relative_to ignores case, which a string prefix would not.
        try:
            return str(path.relative_to(resolved_cwd))
        except ValueError:
            return str(path)
    # Walked paths are built from the resolved root, so the prefix is lexical.
    # Path.relative_to costs more than the rest of the walk per file.
    return str(path).removeprefix(str(resolved_cwd).rstrip("/") + "/")


@dataclass(slots=True)
class _BoundedGrepFileOutput:
    prior_lines: int
    prior_bytes: int
    prefix_separator: bool
    max_lines: int
    max_bytes: int
    lines: list[str] = field(default_factory=list)
    byte_count: int = 0
    exhausted: bool = False

    def append(self, record: str, *, preserve_match: bool = False) -> None:
        """Retain bounded output, allowing one oversized matching-record lookahead."""

        separator_lines = 1 if self.prefix_separator and not self.lines else 0
        separator_bytes = 3 if separator_lines else 0  # "\n--"
        record_bytes = len(record.encode("utf-8"))
        newline_bytes = 1 if self.prior_lines or self.lines or separator_lines else 0
        total_lines = self.prior_lines + separator_lines + len(self.lines) + 1
        total_bytes = (
            self.prior_bytes + separator_bytes + self.byte_count + record_bytes + newline_bytes
        )
        would_exceed = total_lines > self.max_lines or total_bytes > self.max_bytes
        if would_exceed and not preserve_match:
            self.exhausted = True
            return
        self.lines.append(record)
        self.byte_count += record_bytes + newline_bytes
        self.exhausted = would_exceed


def _iter_utf8_line_blocks(
    path: Path | int, *, max_line_chars: int = _PYTHON_GREP_MAX_LINE_CHARS
) -> Iterator[list[str]]:
    """Yield UTF-8 lines with ``str.splitlines()`` boundaries, one list per chunk read.

    A line that continues past a chunk is carried into the block of the chunk that ends it.
    """

    decoder = codecs.getincrementaldecoder("utf-8")()
    line_parts: list[str] = []
    pending_cr = False
    file_source = os.fdopen(os.dup(path), "rb") if isinstance(path, int) else path.open("rb")
    with file_source as file:
        while chunk := file.read(_PYTHON_GREP_CHUNK_BYTES):
            decoded = decoder.decode(chunk)
            if "\0" in decoded:
                raise _BinaryFileDetected
            block: list[str] = []
            if pending_cr:
                block.append("".join(line_parts))
                line_parts.clear()
                pending_cr = False
                if decoded.startswith("\n"):
                    decoded = decoded[1:]
            pending_cr = _split_chunk_lines(
                decoded, line_parts, block, max_line_chars=max_line_chars
            )
            if sum(map(len, line_parts)) > max_line_chars:
                raise ToolError(f"grep encountered a line longer than {max_line_chars} characters")
            if block:
                yield block
        decoded = decoder.decode(b"", final=True)
    if "\0" in decoded:
        raise _BinaryFileDetected
    block = []
    if pending_cr:
        block.append("".join(line_parts))
        line_parts.clear()
        if decoded.startswith("\n"):
            decoded = decoded[1:]
    _split_chunk_lines(decoded, line_parts, block, max_line_chars=max_line_chars, final=True)
    if sum(map(len, line_parts)) > max_line_chars:
        raise ToolError(f"grep encountered a line longer than {max_line_chars} characters")
    if line_parts:
        block.append("".join(line_parts))
    if block:
        yield block


def _split_chunk_lines(
    text: str,
    line_parts: list[str],
    lines: list[str],
    *,
    max_line_chars: int,
    final: bool = False,
) -> bool:
    """Append the lines ``text`` completes to ``lines`` and keep the rest in ``line_parts``.

    Returns:
        bool: Whether ``text`` ended in a CR that may be the first half of a CRLF.
    """

    pieces = text.splitlines(keepends=True)
    last_index = len(pieces) - 1
    for index, piece in enumerate(pieces):
        if piece[-1] not in _SPLITLINES_BOUNDARIES:
            line_parts.append(piece)
            break
        line = piece[:-2] if piece.endswith("\r\n") else piece[:-1]
        if line_parts:
            line = "".join((*line_parts, line))
            line_parts.clear()
        if len(line) > max_line_chars:
            raise ToolError(f"grep encountered a line longer than {max_line_chars} characters")
        # A trailing CR may be the first half of a CRLF split across chunks.
        if index == last_index and piece[-1] == "\r" and not final:
            line_parts.append(line)
            return True
        lines.append(line)
    return False


def _format_grep_record(
    display_path: str,
    line_number: int,
    line: str,
    is_match: bool,
) -> str:
    separator = ":" if is_match else "-"
    if len(line) > _GREP_MAX_DISPLAY_CHARS:
        omitted = len(line) - _GREP_MAX_DISPLAY_CHARS
        line = f"{line[:_GREP_MAX_DISPLAY_CHARS]}... [{omitted} more characters]"
    return f"{display_path}{separator}{line_number}{separator}{line}"


def _python_find(
    *,
    path: Path | SecureToolPath,
    pattern: str,
    max_results: int,
    context: ToolContext,
    cancel_event: Event | None = None,
) -> ToolResult:
    secure_path = _coerce_secure_path(path, context)

    # The walk sorts each directory and descends in place, so "a/b" streams before
    # "a-c" although "a-c" sorts first. Scan the streamed candidates completely to
    # keep global ordering, but retain only the sorted prefix and one truncation
    # lookahead in memory.
    glob_matcher = _prepare_glob_matcher(pattern)
    resolved_cwd = context.cwd.resolve(strict=False)

    def matching_display_paths() -> Iterator[str]:
        for candidate in _iter_files(
            secure_path,
            context,
            cancel_event=cancel_event,
        ):
            display_path = _display_walked_path(candidate, resolved_cwd)
            if glob_matcher(display_path):
                yield display_path

    matches = heapq.nsmallest(
        max_results + 1,
        matching_display_paths(),
    )
    return _result_from_lines(
        matches, max_results=max_results, context=context, count_label="files"
    )


def _python_ls(
    *, path: Path | SecureToolPath, include_hidden: bool, context: ToolContext
) -> ToolResult:
    secure_path = _coerce_secure_path(path, context)

    entry_count = 0
    # A listed directory may be a link the caller named, and a path pattern such as
    # ".wisp/auth.json" must match through it, so check names against both paths.
    real_directory = Path(os.path.realpath(secure_path.path))

    def eligible_entries(descriptor: int | Path) -> Iterator[tuple[str, bool]]:
        nonlocal entry_count
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not include_hidden and entry.name.startswith("."):
                    continue
                if _is_protected_walked_file(
                    secure_path.path / entry.name, real_directory / entry.name, context
                ):
                    continue
                entry_count += 1
                yield entry.name, entry.is_dir(follow_symlinks=False)

    retained_limit = min(
        max(0, context.max_output_lines),
        max(0, context.max_output_bytes),
    )
    with open_directory(secure_path) as descriptor:
        retained = heapq.nsmallest(
            retained_limit + 1,
            eligible_entries(descriptor),
            key=lambda entry: entry[0].lower(),
        )
    names = [f"{name}/" if is_directory else name for name, is_directory in retained]
    truncated = truncate_text(
        "\n".join(names),
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    return ToolResult(
        text=truncated.text,
        data={
            "path": secure_path.display,
            "entries": names[:retained_limit],
            "entry_count": entry_count,
        },
        truncated=truncated.truncated,
    )


def _coerce_secure_path(path: Path | SecureToolPath, context: ToolContext) -> SecureToolPath:
    if isinstance(path, SecureToolPath):
        return path
    return secure_tool_path(str(path), context)


def _iter_files(
    path: Path | SecureToolPath,
    context: ToolContext,
    *,
    ignore_override_matcher: PathMatcher | None = None,
    cancel_event: Event | None = None,
) -> Iterable[Path]:
    """Yield regular files through a descriptor-relative, non-following walk."""

    for walked_file in _iter_walked_files(
        path,
        context,
        ignore_override_matcher=ignore_override_matcher,
        cancel_event=cancel_event,
    ):
        yield walked_file.path


@dataclass(frozen=True, slots=True)
class _WalkedFile:
    """A file candidate whose parent descriptor remains live until iteration resumes."""

    path: Path
    parent_descriptor: int | None = None
    leaf: str | None = None
    expected_identity: tuple[int, int] | None = None
    opened_descriptor: int | None = None

    @contextmanager
    def open(self, context: ToolContext) -> Iterator[int]:
        """Open this candidate through the strongest available descriptor path."""

        if self.opened_descriptor is not None:
            yield self.opened_descriptor
            return
        if (
            self.parent_descriptor is not None
            and self.leaf is not None
            and self.expected_identity is not None
        ):
            try:
                descriptor = open_regular_file_at(
                    self.parent_descriptor,
                    self.leaf,
                    display=str(self.path),
                    expected_identity=self.expected_identity,
                )
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                    raise _WalkedFileChanged from exc
                raise ToolError(f"Could not open file {self.path}: {exc}") from exc
            try:
                yield descriptor
            finally:
                os.close(descriptor)
            return
        candidate = secure_tool_path(str(self.path), context)
        with open_file(candidate) as descriptor:
            yield descriptor


class _WalkedFileChanged(Exception):
    """A previously eligible entry changed while it was being opened."""


def _iter_walked_files(
    path: Path | SecureToolPath,
    context: ToolContext,
    *,
    ignore_override_matcher: PathMatcher | None = None,
    cancel_event: Event | None = None,
) -> Generator[_WalkedFile, None, None]:
    """Yield grep candidates while their descriptor-relative parent stays open."""

    secure_path = _coerce_secure_path(path, context)
    if cancel_event is not None and cancel_event.is_set():
        return
    try:
        with open_directory(secure_path) as descriptor:
            ancestor_specs, in_git_repository = _ancestor_ignore_specs(
                secure_path.path,
                context,
                cancel_event=cancel_event,
            )
            resolved_cwd = context.cwd.resolve(strict=False)
            native = _NATIVE_WALK
            native_walk = (
                None
                if native is None
                else _open_native_walk(
                    native,
                    descriptor,
                    secure_path.path,
                    ancestor_specs,
                    in_git_repository=in_git_repository,
                    include_ignored=ignore_override_matcher is not None,
                )
            )
            if native is not None and native_walk is not None:
                yield from _native_walk_directory(
                    native,
                    native_walk,
                    secure_path.path,
                    context,
                    ignore_override_matcher=ignore_override_matcher,
                    resolved_cwd=resolved_cwd,
                    cancel_event=cancel_event,
                )
                return
            yield from _walk_directory(
                descriptor,
                secure_path.path,
                context,
                ignore_specs=ancestor_specs,
                ignore_override_matcher=ignore_override_matcher,
                resolved_cwd=resolved_cwd,
                in_git_repository=in_git_repository,
                cancel_event=cancel_event,
            )
            return
    except ToolError as directory_error:
        try:
            with open_file(secure_path) as descriptor:
                if _is_path_within_tool_cwd(secure_path.path, context):
                    yield _WalkedFile(
                        path=secure_path.path,
                        opened_descriptor=descriptor,
                    )
                return
        except ToolError:
            raise directory_error from None


def _open_native_walk(
    native: _NativeWalkModule,
    descriptor: int | Path,
    root: Path,
    ancestor_specs: tuple[_IgnoreSpec, ...],
    *,
    in_git_repository: bool,
    include_ignored: bool,
) -> _NativeWalk | None:
    """Start the native walk below ``descriptor``, or return ``None`` to walk in Python."""

    if isinstance(descriptor, Path):
        return None
    ancestors = [
        (_ancestor_prefix(root, spec.base), spec.priority, spec.content) for spec in ancestor_specs
    ]
    try:
        return native.NativeWalk(
            descriptor,
            ancestors=ancestors,
            in_git_repository=in_git_repository,
            include_ignored=include_ignored,
            max_directory_entries=_MAX_DIRECTORY_ENTRIES,
            max_ignore_file_bytes=_MAX_IGNORE_FILE_BYTES,
            max_ignore_file_patterns=_MAX_IGNORE_FILE_PATTERNS,
            max_git_marker_bytes=_MAX_GIT_MARKER_BYTES,
        )
    except OSError:
        # The walk duplicates the descriptor; a sandbox may refuse that.
        return None


def _ancestor_prefix(root: Path, base: Path) -> str:
    relative = root.relative_to(base)
    return "" if relative == Path() else f"{relative.as_posix()}/"


def _native_walk_directory(
    native: _NativeWalkModule,
    walk: _NativeWalk,
    root: Path,
    context: ToolContext,
    *,
    ignore_override_matcher: PathMatcher | None,
    resolved_cwd: Path,
    cancel_event: Event | None,
) -> Iterator[_WalkedFile]:
    """Yield the native walk's files, applying the checks that stay in Python.

    The walk has already skipped links, non-regular files, pruned directories and,
    unless ``ignore_override_matcher`` is set, hidden and ignored files.
    """

    # Entries opened through a descriptor are never links, so each one's real path
    # is the root's real path plus its relative path.
    real_root = Path(os.path.realpath(root))
    in_cwd = context.allow_outside_cwd or real_root.is_relative_to(resolved_cwd)
    cancelled = cancel_event is not None and cancel_event.is_set()
    if cancelled or not in_cwd:
        walk.close()
        return
    with closing(walk):
        try:
            for directory_descriptor, relative, files in walk:
                directory = root / relative
                real_directory = real_root / relative
                for leaf, device, inode, hidden_or_ignored in files:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    candidate = directory / leaf
                    if hidden_or_ignored and not (
                        ignore_override_matcher is not None and ignore_override_matcher(candidate)
                    ):
                        continue
                    if _is_protected_walked_file(candidate, real_directory / leaf, context):
                        continue
                    yield _WalkedFile(
                        path=candidate,
                        parent_descriptor=directory_descriptor,
                        leaf=leaf,
                        expected_identity=(device, inode),
                    )
                if cancel_event is not None and cancel_event.is_set():
                    return
        except native.NativeWalkError as exc:
            raise _native_walk_error(root, exc) from None


def _native_walk_error(root: Path, error: Exception) -> Exception:
    """Rebuild the Python walk's error for a native walk failure."""

    kind, relative, *details = error.args
    if kind == "stat":
        # DirEntry.stat raises the OSError itself, naming only the entry.
        (error_number,) = details
        return OSError(error_number, os.strerror(error_number), relative)
    path = root / relative
    if kind == "directory_entries":
        return _DirectoryEntryLimitError(
            f"Directory {path} exceeds {_MAX_DIRECTORY_ENTRIES} entries"
        )
    if kind == "ignore_file_bytes":
        return _IgnoreFileLimitError(f"Ignore file {path} exceeds {_MAX_IGNORE_FILE_BYTES} bytes")
    if kind == "ignore_file_patterns":
        return _IgnoreFileLimitError(
            f"Ignore file {path} exceeds {_MAX_IGNORE_FILE_PATTERNS} patterns"
        )
    (error_number,) = details
    error_text = OSError(error_number, os.strerror(error_number))
    return ToolError(f"Could not list directory {path}: {error_text}")


@dataclass(frozen=True, slots=True)
class _IgnoreSpec:
    base: Path
    spec: GitIgnoreSpec
    priority: int
    # The raw file, which the native walk compiles again for ancestor directories.
    content: bytes


def _ancestor_ignore_specs(
    path: Path,
    context: ToolContext,
    *,
    cancel_event: Event | None = None,
) -> tuple[tuple[_IgnoreSpec, ...], bool]:
    cwd = context.cwd.resolve(strict=False)
    try:
        relative = path.relative_to(cwd)
    except ValueError:
        return (), False
    specs: list[_IgnoreSpec] = []
    in_git_repository = False
    current = cwd
    for part in relative.parts:
        if cancel_event is not None and cancel_event.is_set():
            return tuple(specs), in_git_repository
        selected = secure_tool_path(str(current), context)
        try:
            with open_directory(selected) as descriptor:
                in_git_repository = in_git_repository or _is_git_repository_root(
                    descriptor,
                    current,
                    context,
                )
                specs.extend(
                    _read_ignore_specs(
                        descriptor,
                        current,
                        context,
                        include_gitignore=in_git_repository,
                        cancel_event=cancel_event,
                    )
                )
        except _SearchInputLimitError:
            raise
        except ToolError:
            return tuple(specs), in_git_repository
        current /= part
    return tuple(specs), in_git_repository


def _walk_directory(
    descriptor: int | Path,
    path: Path,
    context: ToolContext,
    *,
    ignore_specs: tuple[_IgnoreSpec, ...],
    ignore_override_matcher: PathMatcher | None,
    resolved_cwd: Path,
    real_directory: Path | None = None,
    in_git_repository: bool = False,
    cancel_event: Event | None = None,
) -> Iterable[_WalkedFile]:
    if cancel_event is not None and cancel_event.is_set():
        return
    in_git_repository = in_git_repository or _is_git_repository_root(
        descriptor,
        path,
        context,
    )
    ignore_specs += _read_ignore_specs(
        descriptor,
        path,
        context,
        include_gitignore=in_git_repository,
        cancel_event=cancel_event,
    )
    try:
        retained = _bounded_sorted_directory_entries(
            descriptor,
            path,
            cancel_event=cancel_event,
        )
    except OSError as exc:
        raise ToolError(f"Could not list directory {path}: {exc}") from exc

    # Entries opened through a descriptor are never links, so each one's real path is
    # this directory's real path plus its name. Only the walk root and directories
    # reached by path need resolving.
    if real_directory is None:
        real_directory = Path(os.path.realpath(path))
    directory_in_cwd = context.allow_outside_cwd or real_directory.is_relative_to(resolved_cwd)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for entry in retained:
        if cancel_event is not None and cancel_event.is_set():
            return
        name = entry.name
        candidate = path / name
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            if _is_hidden(name) or (
                _is_ignored(candidate, is_directory=True, ignore_specs=ignore_specs)
                and not _may_reinclude_descendant(candidate, ignore_specs)
                and ignore_override_matcher is None
            ):
                continue
            if isinstance(descriptor, Path):
                child_path = secure_tool_path(str(candidate), context)
                try:
                    with open_directory(child_path) as child:
                        yield from _walk_directory(
                            child,
                            candidate,
                            context,
                            ignore_specs=ignore_specs,
                            ignore_override_matcher=ignore_override_matcher,
                            resolved_cwd=resolved_cwd,
                            in_git_repository=in_git_repository,
                            cancel_event=cancel_event,
                        )
                except _SearchInputLimitError:
                    raise
                except ToolError:
                    continue
                continue
            try:
                child = os.open(name, directory_flags, dir_fd=descriptor)
            except OSError:
                # A rename, replacement, or newly introduced link invalidates this
                # candidate; skip it rather than falling back to path traversal.
                continue
            try:
                yield from _walk_directory(
                    child,
                    candidate,
                    context,
                    ignore_specs=ignore_specs,
                    ignore_override_matcher=ignore_override_matcher,
                    resolved_cwd=resolved_cwd,
                    real_directory=real_directory / name,
                    in_git_repository=in_git_repository,
                    cancel_event=cancel_event,
                )
            finally:
                os.close(child)
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and (
                (
                    not _is_hidden(name)
                    and not _is_ignored(
                        candidate,
                        is_directory=False,
                        ignore_specs=ignore_specs,
                    )
                )
                or (ignore_override_matcher is not None and ignore_override_matcher(candidate))
            )
            and directory_in_cwd
            and not _is_protected_walked_file(candidate, real_directory / name, context)
        ):
            yield _WalkedFile(
                path=candidate,
                parent_descriptor=descriptor if isinstance(descriptor, int) else None,
                leaf=name if isinstance(descriptor, int) else None,
                expected_identity=(
                    (info.st_dev, info.st_ino) if isinstance(descriptor, int) else None
                ),
            )


def _bounded_sorted_directory_entries(
    descriptor: int | Path,
    source: Path,
    *,
    cancel_event: Event | None = None,
) -> list[os.DirEntry[str]]:
    retained: list[os.DirEntry[str]] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if cancel_event is not None and cancel_event.is_set():
                break
            retained.append(entry)
            if len(retained) > _MAX_DIRECTORY_ENTRIES:
                raise _DirectoryEntryLimitError(
                    f"Directory {source} exceeds {_MAX_DIRECTORY_ENTRIES} entries"
                )
    retained.sort(key=lambda entry: entry.name)
    return retained


def _read_bounded_ignore_content(descriptor: int, source: str) -> bytes:
    with os.fdopen(os.dup(descriptor), "rb") as file:
        content = file.read(_MAX_IGNORE_FILE_BYTES + 1)
    if len(content) > _MAX_IGNORE_FILE_BYTES:
        raise _IgnoreFileLimitError(f"Ignore file {source} exceeds {_MAX_IGNORE_FILE_BYTES} bytes")
    return content


def _compile_ignore_file(
    directory: Path, content: bytes, source: str, priority: int
) -> _IgnoreSpec:
    lines = content.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    if len(lines) > _MAX_IGNORE_FILE_PATTERNS:
        raise _IgnoreFileLimitError(
            f"Ignore file {source} exceeds {_MAX_IGNORE_FILE_PATTERNS} patterns"
        )
    return _IgnoreSpec(directory, _compile_ignore_lines(lines), priority, content)


def _is_git_repository_root(
    descriptor: int | Path,
    directory: Path,
    context: ToolContext,
) -> bool:
    """Detect a non-symlink Git directory or worktree marker."""

    marker_fd: int | None = None
    try:
        if isinstance(descriptor, int):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            marker_fd = os.open(".git", flags, dir_fd=descriptor)
            info = os.fstat(marker_fd)
            if stat.S_ISDIR(info.st_mode):
                return True
            if not stat.S_ISREG(info.st_mode):
                return False
        else:
            marker = secure_tool_path(str(directory / ".git"), context)
            try:
                with open_directory(marker):
                    return True
            except ToolError:
                with open_file(marker) as opened:
                    marker_fd = os.dup(opened)
        with os.fdopen(marker_fd, "rb") as marker_file:
            marker_fd = None
            content = marker_file.read(_MAX_GIT_MARKER_BYTES + 1)
        if len(content) > _MAX_GIT_MARKER_BYTES:
            return False
        first_line = content.splitlines()[0] if content else b""
        return first_line.startswith(b"gitdir: ") and bool(
            first_line.removeprefix(b"gitdir: ").strip()
        )
    except (FileNotFoundError, OSError, ToolError):
        return False
    finally:
        if marker_fd is not None:
            os.close(marker_fd)


def _read_ignore_specs(
    descriptor: int | Path,
    directory: Path,
    context: ToolContext,
    *,
    include_gitignore: bool,
    cancel_event: Event | None = None,
) -> tuple[_IgnoreSpec, ...]:
    if cancel_event is not None and cancel_event.is_set():
        return ()
    specs: list[_IgnoreSpec] = []
    repository_exclude = (
        _read_repository_exclude(
            descriptor,
            directory,
            context,
            cancel_event=cancel_event,
        )
        if include_gitignore
        else None
    )
    if repository_exclude is not None:
        specs.append(repository_exclude)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for priority, name in enumerate(_IGNORE_FILES, start=1):
        if name == ".gitignore" and not include_gitignore:
            continue
        if cancel_event is not None and cancel_event.is_set():
            return tuple(specs)
        try:
            if isinstance(descriptor, int):
                ignore_fd = os.open(name, flags, dir_fd=descriptor)
                try:
                    if not stat.S_ISREG(os.fstat(ignore_fd).st_mode):
                        continue
                    content = _read_bounded_ignore_content(ignore_fd, str(directory / name))
                finally:
                    os.close(ignore_fd)
            else:
                ignore_path = secure_tool_path(str(directory / name), context)
                with open_file(ignore_path) as ignore_fd:
                    content = _read_bounded_ignore_content(ignore_fd, str(directory / name))
        except _IgnoreFileLimitError:
            raise
        except (FileNotFoundError, OSError, ToolError, UnicodeDecodeError):
            continue
        if cancel_event is not None and cancel_event.is_set():
            return tuple(specs)
        specs.append(_compile_ignore_file(directory, content, str(directory / name), priority))
    return tuple(specs)


def _read_repository_exclude(
    descriptor: int | Path,
    directory: Path,
    context: ToolContext,
    *,
    cancel_event: Event | None = None,
) -> _IgnoreSpec | None:
    """Securely load a repository-local ``.git/info/exclude`` file."""

    if cancel_event is not None and cancel_event.is_set():
        return None
    source = str(directory / ".git" / "info" / "exclude")
    try:
        if isinstance(descriptor, int):
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            git_fd = os.open(".git", directory_flags, dir_fd=descriptor)
            try:
                info_fd = os.open("info", directory_flags, dir_fd=git_fd)
                try:
                    exclude_fd = os.open("exclude", file_flags, dir_fd=info_fd)
                    try:
                        if not stat.S_ISREG(os.fstat(exclude_fd).st_mode):
                            return None
                        content = _read_bounded_ignore_content(exclude_fd, source)
                    finally:
                        os.close(exclude_fd)
                finally:
                    os.close(info_fd)
            finally:
                os.close(git_fd)
        else:
            exclude_path = secure_tool_path(source, context)
            source = str(exclude_path.path)
            with open_file(exclude_path) as exclude_fd:
                content = _read_bounded_ignore_content(exclude_fd, source)
    except _IgnoreFileLimitError:
        raise
    except (FileNotFoundError, OSError, ToolError, UnicodeDecodeError):
        return None
    if cancel_event is not None and cancel_event.is_set():
        return None
    return _compile_ignore_file(directory, content, source, 0)


def _ordered_ignore_specs(ignore_specs: tuple[_IgnoreSpec, ...]) -> list[_IgnoreSpec]:
    return sorted(ignore_specs, key=lambda rules: rules.priority)


def _is_ignored(path: Path, *, is_directory: bool, ignore_specs: tuple[_IgnoreSpec, ...]) -> bool:
    ignored = False
    for rules in _ordered_ignore_specs(ignore_specs):
        try:
            relative = path.relative_to(rules.base).as_posix()
        except ValueError:
            continue
        if is_directory:
            relative += "/"
        match = rules.spec.check_file(relative).include
        if match is not None:
            ignored = match
    return ignored


def _may_reinclude_descendant(path: Path, ignore_specs: tuple[_IgnoreSpec, ...]) -> bool:
    """Return whether a negated rule could make an entry below ``path`` visible."""

    ignored = False
    exclusion_allows_reinclude = False
    for rules in _ordered_ignore_specs(ignore_specs):
        try:
            relative = path.relative_to(rules.base).as_posix().rstrip("/") + "/"
        except ValueError:
            continue
        match = rules.spec.check_file(relative)
        if match.include is None:
            continue
        ignored = match.include
        exclusion_allows_reinclude = False
        if ignored and match.index is not None:
            source = rules.spec.patterns[match.index].pattern
            if isinstance(source, str) and source.rstrip().endswith("/**"):
                ignored_root = source.rstrip().removesuffix("/**").removeprefix("/")
                exclusion_allows_reinclude = _gitignore_glob_matches_exact(
                    relative,
                    f"/{ignored_root}/",
                )
    if not ignored or not exclusion_allows_reinclude:
        return False

    for rules in _ordered_ignore_specs(ignore_specs):
        try:
            relative_directory = path.relative_to(rules.base).as_posix().rstrip("/") + "/"
        except ValueError:
            continue
        for pattern in rules.spec.patterns:
            source = pattern.pattern
            if (
                pattern.include is not False
                or not isinstance(source, str)
                or not source.startswith("!")
            ):
                continue
            negated = source[1:].removeprefix("/")
            components = negated.split("/")
            if len(components) == 1:
                return True
            for end in range(1, len(components)):
                directory_pattern = "/" + "/".join(components[:end]) + "/"
                if _gitignore_glob_matches_exact(relative_directory, directory_pattern):
                    return True
    return False


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


def _is_protected_walked_file(path: Path, real_path: Path, context: ToolContext) -> bool:
    # is_protected_path for a file whose real path the walk already knows.
    patterns = context.protected_paths
    return matches_protected_pattern(path, patterns) or (
        real_path != path and matches_protected_pattern(real_path, patterns)
    )


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _is_exclusion_glob(pattern: str) -> bool:
    return pattern.startswith("!")


def _expand_brace_alternatives(pattern: str) -> tuple[str, ...]:
    expanded: list[str] = []

    def expand(candidate: str) -> None:
        escaped = False
        opening: int | None = None
        for index, character in enumerate(candidate):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "{":
                opening = index
                break
        if opening is None:
            expanded.append(candidate)
            if len(expanded) > _MAX_GLOB_ALTERNATIVES:
                raise ToolError(f"glob expands beyond {_MAX_GLOB_ALTERNATIVES} alternatives")
            return

        depth = 0
        escaped = False
        separators: list[int] = []
        closing: int | None = None
        for index in range(opening, len(candidate)):
            character = candidate[index]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
            elif character == "," and depth == 1:
                separators.append(index)
        if closing is None or not separators:
            expanded.append(candidate)
            return

        prefix = candidate[:opening]
        suffix = candidate[closing + 1 :]
        boundaries = [opening, *separators, closing]
        for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
            expand(prefix + candidate[start + 1 : end] + suffix)

    expand(pattern)
    return tuple(dict.fromkeys(expanded))


def _compile_ignore_lines(lines: Iterable[str]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(lines, _lenient_gitignore_pattern)


def _lenient_gitignore_pattern(line: str) -> GitIgnoreSpecPattern:
    # pathspec raises on a line it cannot translate, such as a trailing backslash or
    # an inverted range. Treat it as a blank line, as Git does for a trailing
    # backslash, so one bad line cannot fail the whole search.
    try:
        return GitIgnoreSpecPattern(line)
    except (GitIgnorePatternError, re.error):
        return GitIgnoreSpecPattern("")


@lru_cache(maxsize=512)
def _compiled_gitignore_glob(pattern: str) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines((pattern,))


def _gitignore_glob_matches_exact(path: str, pattern: str) -> bool:
    for compiled_pattern in _compiled_gitignore_glob(pattern).patterns:
        regex = compiled_pattern.regex
        if regex is not None and any(match.end() == len(path) for match in regex.finditer(path)):
            return True
    return False


def _prepare_glob_matcher(pattern: str) -> GlobMatcher:
    exclusion = _is_exclusion_glob(pattern)
    effective_pattern = pattern[1:] if exclusion else pattern
    alternatives = _expand_brace_alternatives(effective_pattern)
    for alternative in alternatives:
        try:
            GitIgnoreSpecPattern(alternative)
        except (GitIgnorePatternError, re.error):
            raise ToolArgumentError(f"Invalid glob pattern: {pattern!r}") from None
    compiled = tuple(_compiled_gitignore_glob(alternative) for alternative in alternatives)

    if exclusion:

        def matches_exclusion(display_path: str) -> bool:
            normalized = Path(display_path).as_posix()
            matched = any(spec.check_file(normalized).include is True for spec in compiled)
            return not matched

        return matches_exclusion

    def matches_inclusion(display_path: str) -> bool:
        normalized = Path(display_path).as_posix()
        for spec in compiled:
            for compiled_pattern in spec.patterns:
                regex = compiled_pattern.regex
                if regex is None:
                    continue
                for match in regex.finditer(normalized):
                    if match.groupdict().get("ps_d") != "/":
                        return True
        return False

    return matches_inclusion


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

    flags = bounded_regex.IGNORECASE if ignore_case else 0
    try:
        expression = bounded_regex.compile(pattern, flags=flags)
    except bounded_regex.error as exc:
        raise ToolError(
            f"Invalid grep pattern: {exc}",
            failure_code="invalid_pattern",
            retryable=True,
            recovery_hint="Retry with literal=true when searching for exact text.",
        ) from exc

    retry_budget = _REGEX_RETRY_BUDGET_SECONDS
    search = expression.search

    def time_limit_error() -> ToolError:
        return ToolError(
            "grep pattern exceeded the regex evaluation time limit",
            failure_code="invalid_pattern",
            retryable=True,
            recovery_hint="Retry with literal=true or a simpler regex pattern.",
        )

    def regex_matcher(line: str) -> bool:
        nonlocal retry_budget
        try:
            # By default regex releases the GIL for every search. Per line that turns
            # concurrent greps into a convoy of GIL handoffs, so the short attempt keeps it.
            # The arguments are positional, (string, pos, endpos, concurrent, partial,
            # timeout), because regex parses keywords slowly enough to cost a third of
            # each per-line search.
            return search(line, None, None, False, False, _REGEX_TIMEOUT_SECONDS) is not None
        except TimeoutError as exc:
            if retry_budget <= 0:
                raise time_limit_error() from exc
        # Catastrophic backtracking also times out on the longer attempt, while a line that
        # only lost the CPU finishes; the shared budget bounds patterns that are slow per line.
        started = time.monotonic()
        try:
            return expression.search(line, timeout=retry_budget) is not None
        except TimeoutError as exc:
            raise time_limit_error() from exc
        finally:
            retry_budget -= time.monotonic() - started

    return regex_matcher


def _build_block_matcher(
    pattern: str,
    *,
    ignore_case: bool,
    literal: bool,
) -> BlockMatcher | None:
    """Build a check that a block of lines may hold a match, if one can be exact.

    Searching a block's lines joined by newlines once is far cheaper than searching each
    line. Every match within one line is also a match in the block, so a block with no
    match needs no per-line search; a block match may still span lines and is confirmed
    line by line. With ``MULTILINE``, ``^`` and ``$`` match where a line starts and ends,
    as they do on the line alone.

    Returns:
        BlockMatcher | None: The check, or ``None`` when a block search could miss a
        line's match, so every line must be searched.
    """

    if literal:
        needle = pattern.casefold() if ignore_case else pattern

        def literal_block_matcher(lines: list[str]) -> bool:
            text = "\n".join(lines)
            return needle in (text.casefold() if ignore_case else text)

        return literal_block_matcher

    if not _can_search_blocks(pattern):
        return None
    flags = bounded_regex.MULTILINE | (bounded_regex.IGNORECASE if ignore_case else 0)
    search = bounded_regex.compile(pattern, flags=flags).search
    timed_out = False

    def regex_block_matcher(lines: list[str]) -> bool:
        nonlocal timed_out
        if timed_out:
            return True
        try:
            # Positional and GIL-holding for the reasons given in _build_matcher.
            text = "\n".join(lines)
            return search(text, None, None, False, False, _REGEX_TIMEOUT_SECONDS) is not None
        except TimeoutError:
            # A slow pattern: leave every line to the per-line search and its time budget.
            timed_out = True
            return True

    return regex_block_matcher


# Escapes that anchor to the whole string (\A, \Z, \z) or the search start (\G). Word
# boundaries need no entry: the newline joining lines is not a word character, so a line
# edge is a boundary in the block exactly when it is one on the line alone.
_CONTEXT_ESCAPES = frozenset("AZzG")
# Inline flags that keep a block search exact. Verbose mode (x) is excluded because its
# comments and whitespace would hide constructs from the scan below, and turning a flag
# off ("-m") would undo the MULTILINE that keeps ^ and $ at line edges.
_BLOCK_SAFE_INLINE_FLAGS = frozenset("aiLmsuf")
_REPEAT_COUNT_CHARACTERS = frozenset("0123456789,")
_BRACED_ESCAPES = frozenset("pPNx")


def _can_search_blocks(pattern: str) -> bool:
    """Return whether a regex matches a line exactly when it matches that line in a block.

    The block joins lines with newlines and is searched with ``MULTILINE``, so the pattern
    may use ``^``, ``$`` and word boundaries, but nothing else that looks past the text it
    covers. Nor may it commit to a match it cannot give back, as possessive quantifiers
    and atomic groups do: one that consumes the joining newline then fails where the line
    alone would match. The scan is conservative: string anchors, lookarounds, backtracking
    verbs, nested sets, fuzzy constraints and any group form it does not recognize fail it.
    """

    index = 0
    in_class = False
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            escaped = pattern[index + 1 : index + 2]
            if escaped in _CONTEXT_ESCAPES:
                return False
            index += 2
            # \p{L}, \N{name} and \x{41} take a braced argument, not a repeat count.
            if escaped in _BRACED_ESCAPES and pattern[index : index + 1] == "{":
                closing = pattern.find("}", index)
                if closing == -1:
                    return False
                index = closing + 1
            continue
        if in_class:
            if character == "[":
                return False
            if character == "]":
                in_class = False
            index += 1
            continue
        if character == "[":
            in_class = True
            index += 1
            # A leading "^" negates the set, and a "]" right after it is a member.
            if pattern[index : index + 1] == "^":
                index += 1
            if pattern[index : index + 1] == "]":
                index += 1
            continue
        if character in "*+?}" and pattern[index + 1 : index + 2] == "+":
            return False  # A possessive quantifier.
        if character == "{":
            closing = pattern.find("}", index)
            # Only a counted repeat; a fuzzy constraint such as {e<=1} is not checked.
            if closing == -1 or not set(pattern[index + 1 : closing]) <= _REPEAT_COUNT_CHARACTERS:
                return False
        if character == "(" and pattern[index + 1 : index + 2] in {"?", "*"}:
            opening = _plain_group_opening_length(pattern, index)
            if opening is None:
                return False
            index += opening
            continue
        index += 1
    return not in_class


def _plain_group_opening_length(pattern: str, index: int) -> int | None:
    """Return the length of a ``(?...`` group opening that only groups or sets flags."""

    rest = pattern[index:]
    if rest.startswith("(*"):
        return None  # A backtracking verb such as (*SKIP).
    for prefix in ("(?:", "(?P=", "(?P<", "(?<"):
        if not rest.startswith(prefix):
            continue
        if prefix in {"(?P<", "(?<"} and not rest[len(prefix) : len(prefix) + 1].isidentifier():
            return None  # (?<= and (?<! are lookbehinds.
        return len(prefix)
    end = 2
    while end < len(rest) and rest[end] in _BLOCK_SAFE_INLINE_FLAGS:
        end += 1
    if end > 2 and rest[end : end + 1] in {")", ":"}:
        return end + 1
    return None


type BlockMatcher = Callable[[list[str]], bool]
type CallableMatcher = Callable[[str], bool]
type GlobMatcher = Callable[[str], bool]
type PathMatcher = Callable[[Path], bool]


def _grep_result(
    records: Sequence[str],
    *,
    match_count: int,
    max_results: int,
    context: ToolContext,
    truncated: bool,
    match_limit_reached: bool = False,
) -> ToolResult:
    """Build grep's result from the records its scan kept.

    The scan skips protected files and stops at ``max_results``, so the records are
    reported as they are and ``match_count`` is the number of matches among them.

    Args:
        records (Sequence[str]): Match and context records, with ``--`` between files.
        match_count (int): Matching records among ``records``.
        max_results (int): The call's match limit, named in the truncation hint.
        context (ToolContext): Supplies the output budget.
        truncated (bool): Whether the scan stopped early or reduced context lines.
        match_limit_reached (bool): Whether ``max_results`` is what stopped it.

    Returns:
        ToolResult: The bounded text, with ``count`` and ``matches`` data.
    """

    if not records and not truncated:
        return ToolResult(text="No matches", data={"count": 0, "matches": []})
    text = "\n".join(records)
    if truncated:
        marker = _limit_marker(max_results, match_limit_reached)
        text = f"{text}\n{marker}" if text else marker
    bounded = truncate_text(
        text,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
    )
    return ToolResult(
        text=bounded.text,
        data={"count": match_count, "matches": list(records)},
        truncated=truncated or bounded.truncated,
    )


def _limit_marker(max_results: int, limit_reached: bool) -> str:
    """Mark a truncated search result, saying how to see more when max_results cut it."""

    if not limit_reached:
        return "[truncated]"
    return (
        f"[truncated] Stopped at max_results={max_results}. "
        "Narrow the search, or raise max_results to see more."
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
        marker = _limit_marker(max_results, total > max_results)
        text = f"{text}\n{marker}" if text else marker
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
