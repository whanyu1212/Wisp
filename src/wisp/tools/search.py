"""Search and listing built-in tools."""

from __future__ import annotations

import codecs
import fnmatch
import heapq
import os
import stat
from collections import deque
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import regex as bounded_regex
from pathspec import GitIgnoreSpec

from wisp.tools.base import ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.common import _optional_bool, _optional_int, _optional_string, _required_string
from wisp.tools.context import ToolContext
from wisp.tools.paths import display_tool_path, is_protected_path
from wisp.tools.process_manager import ProcessSupervisor
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.secure_fs import SecureToolPath, open_directory, open_file, secure_tool_path
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
RG_MATCH_SEPARATOR = "\x1f"
RG_CONTEXT_SEPARATOR = "\x1e"
_REGEX_TIMEOUT_SECONDS = 0.05
_IGNORE_FILES = (".gitignore", ".ignore", ".rgignore")
_MAX_IGNORE_FILE_BYTES = 1_000_000
_MAX_IGNORE_FILE_PATTERNS = 10_000
_MAX_DIRECTORY_ENTRIES = 100_000


class _SearchInputLimitError(ToolError):
    """Raised when repository-controlled search input exceeds a safety budget."""


class _IgnoreFileLimitError(_SearchInputLimitError):
    """Raised when a repository ignore source exceeds its safety budget."""


class _DirectoryEntryLimitError(_SearchInputLimitError):
    """Raised when one repository directory exceeds its traversal budget."""


class _BinaryFileDetected(Exception):
    """Stop grep and discard pending output when a streamed file contains NUL."""


class GrepTool:
    """Search file contents."""

    name = "grep"
    safety: ToolSafety = "read"
    description = "Search UTF-8 text files without following symbolic links."
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
        path = secure_tool_path(_optional_string(arguments, "path"), context)
        glob = _optional_string(arguments, "glob")
        ignore_case = _optional_bool(arguments, "ignore_case", default=False)
        literal = _optional_bool(arguments, "literal", default=False)
        context_lines = _optional_int(arguments, "context", default=0)
        max_results = _optional_int(arguments, "max_results", default=100)
        if context_lines is None or context_lines < 0:
            raise ToolError("grep.context must be greater than or equal to 0")
        if max_results is None or max_results < 1:
            raise ToolError("grep.max_results must be greater than or equal to 1")

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
            ),
            abandon_on_cancel=True,
        )


class FindTool:
    """Find files by glob pattern."""

    name = "find"
    safety: ToolSafety = "read"
    description = "Find files by glob without following symbolic links."
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
        path = secure_tool_path(_optional_string(arguments, "path"), context)
        pattern = _optional_string(arguments, "pattern") or "*"
        max_results = _optional_int(arguments, "max_results", default=100)
        if max_results is None or max_results < 1:
            raise ToolError("find.max_results must be greater than or equal to 1")

        return await anyio.to_thread.run_sync(
            lambda: _python_find(
                path=path,
                pattern=pattern,
                max_results=max_results,
                context=context,
            ),
            abandon_on_cancel=True,
        )


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
        path = secure_tool_path(_optional_string(arguments, "path"), context)
        include_hidden = _optional_bool(arguments, "all", default=False)
        return await anyio.to_thread.run_sync(
            lambda: _python_ls(path=path, include_hidden=include_hidden, context=context),
            abandon_on_cancel=True,
        )


def _bounded_rg_context_lines(requested_context_lines: int, context: ToolContext) -> int:
    if requested_context_lines <= 0:
        return 0
    return min(requested_context_lines, max(0, (context.max_output_lines - 1) // 2))


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
) -> ToolResult:
    secure_path = _coerce_secure_path(path, context)

    matcher = _build_matcher(pattern, ignore_case=ignore_case, literal=literal)
    effective_context_lines = _bounded_rg_context_lines(context_lines, context)
    context_truncated = effective_context_lines < context_lines
    output: list[str] = []
    output_bytes = 0
    match_count = 0
    files = (
        _iter_files(secure_path, context)
        if glob is None
        else _iter_files(secure_path, context, ignore_override_glob=glob)
    )
    for file_path in files:
        if glob is not None and not _matches_glob(file_path, glob, context):
            continue
        file_match_start = match_count
        file_had_extra_match = False
        file_buffer = _BoundedGrepFileOutput(
            prior_lines=len(output),
            prior_bytes=output_bytes,
            prefix_separator=bool(output and effective_context_lines),
            max_lines=context.max_output_lines,
            max_bytes=context.max_output_bytes,
        )

        try:
            preceding: deque[tuple[int, str]] = deque(maxlen=effective_context_lines)
            group_end = 0
            last_emitted_line = 0
            candidate = secure_tool_path(str(file_path), context)
            with open_file(candidate) as descriptor:
                lines = _iter_utf8_splitlines(descriptor)
                for line_number, line in enumerate(lines, start=1):
                    if file_buffer.exhausted or file_had_extra_match:
                        break

                    is_match = matcher(line)
                    if is_match:
                        if match_count >= max_results:
                            file_had_extra_match = True
                        else:
                            match_count += 1
                            if line_number > group_end:
                                for number, text in preceding:
                                    if number > last_emitted_line:
                                        file_buffer.append(
                                            _format_grep_record(
                                                file_path, number, text, False, context
                                            )
                                        )
                                        last_emitted_line = number
                                        if file_buffer.exhausted:
                                            break
                            file_buffer.append(
                                _format_grep_record(file_path, line_number, line, True, context),
                                preserve_match=True,
                            )
                            last_emitted_line = line_number
                            group_end = max(group_end, line_number + effective_context_lines)
                    elif line_number <= group_end:
                        file_buffer.append(
                            _format_grep_record(file_path, line_number, line, False, context)
                        )
                        last_emitted_line = line_number
                    preceding.append((line_number, line))
                    if file_buffer.exhausted or file_had_extra_match:
                        break
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
            return _result_from_grep_lines(
                output,
                max_results=max_results,
                context=context,
                force_truncated=True,
            )

    if not output:
        return ToolResult(text="No matches", data={"count": 0, "matches": []})
    return _result_from_grep_lines(
        output,
        max_results=max_results,
        context=context,
        force_truncated=context_truncated,
    )


_PYTHON_GREP_CHUNK_BYTES = 64 * 1024
_PYTHON_GREP_MAX_LINE_CHARS = 1_000_000
_SPLITLINES_BOUNDARIES = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


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


def _iter_utf8_splitlines(
    path: Path | int, *, max_line_chars: int = _PYTHON_GREP_MAX_LINE_CHARS
) -> Iterator[str]:
    """Yield UTF-8 lines incrementally with ``str.splitlines()`` boundaries."""

    decoder = codecs.getincrementaldecoder("utf-8")()
    line_parts: list[str] = []
    pending_cr = False
    file_source = os.fdopen(os.dup(path), "rb") if isinstance(path, int) else path.open("rb")
    with file_source as file:
        while chunk := file.read(_PYTHON_GREP_CHUNK_BYTES):
            decoded = decoder.decode(chunk)
            if "\0" in decoded:
                raise _BinaryFileDetected
            if pending_cr:
                yield "".join(line_parts)
                line_parts.clear()
                pending_cr = False
                if decoded.startswith("\n"):
                    decoded = decoded[1:]
            pending_cr = yield from _yield_splitline_chunk(decoded, line_parts)
            if sum(map(len, line_parts)) > max_line_chars:
                raise ToolError(f"grep encountered a line longer than {max_line_chars} characters")
        decoded = decoder.decode(b"", final=True)
    if "\0" in decoded:
        raise _BinaryFileDetected
    if pending_cr:
        yield "".join(line_parts)
        line_parts.clear()
        if decoded.startswith("\n"):
            decoded = decoded[1:]
    _ = yield from _yield_splitline_chunk(decoded, line_parts, final=True)
    if sum(map(len, line_parts)) > max_line_chars:
        raise ToolError(f"grep encountered a line longer than {max_line_chars} characters")
    if line_parts:
        yield "".join(line_parts)


def _yield_splitline_chunk(
    text: str,
    line_parts: list[str],
    *,
    final: bool = False,
) -> Generator[str, None, bool]:
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character not in _SPLITLINES_BOUNDARIES:
            index += 1
            continue
        line_parts.append(text[start:index])
        if character == "\r" and index + 1 == len(text) and not final:
            return True
        yield "".join(line_parts)
        line_parts.clear()
        index += 2 if character == "\r" and text[index + 1 : index + 2] == "\n" else 1
        start = index
    if start < len(text):
        line_parts.append(text[start:])
    return False


def _format_grep_record(
    file_path: Path,
    line_number: int,
    line: str,
    is_match: bool,
    context: ToolContext,
) -> str:
    separator = ":" if is_match else "-"
    return f"{display_tool_path(file_path, context)}{separator}{line_number}{separator}{line}"


def _python_find(
    *,
    path: Path | SecureToolPath,
    pattern: str,
    max_results: int,
    context: ToolContext,
) -> ToolResult:
    secure_path = _coerce_secure_path(path, context)

    # Display paths resolve file symlinks, so a lexically late candidate may sort
    # before an earlier directory prefix. Scan the streamed candidates completely
    # to preserve historical global ordering, but retain only the sorted prefix and
    # one truncation lookahead in memory.
    matches = heapq.nsmallest(
        max_results + 1,
        (
            display_tool_path(candidate, context)
            for candidate in _iter_files(secure_path, context)
            if _matches_glob(candidate, pattern, context)
        ),
    )
    return _result_from_lines(
        matches, max_results=max_results, context=context, count_label="files"
    )


def _python_ls(
    *, path: Path | SecureToolPath, include_hidden: bool, context: ToolContext
) -> ToolResult:
    secure_path = _coerce_secure_path(path, context)

    entry_count = 0

    def eligible_entries(descriptor: int | Path) -> Iterator[tuple[str, bool]]:
        nonlocal entry_count
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not include_hidden and entry.name.startswith("."):
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
    ignore_override_glob: str | None = None,
) -> Iterable[Path]:
    """Yield regular files through a descriptor-relative, non-following walk."""

    secure_path = _coerce_secure_path(path, context)
    try:
        with open_directory(secure_path) as descriptor:
            yield from _walk_directory(
                descriptor,
                secure_path.path,
                context,
                ignore_specs=_ancestor_ignore_specs(secure_path.path, context),
                ignore_override_glob=ignore_override_glob,
            )
            return
    except ToolError as directory_error:
        try:
            with open_file(secure_path):
                if _is_path_within_tool_cwd(secure_path.path, context):
                    yield secure_path.path
                return
        except ToolError:
            raise directory_error from None


@dataclass(frozen=True, slots=True)
class _IgnoreSpec:
    base: Path
    spec: GitIgnoreSpec


def _ancestor_ignore_specs(path: Path, context: ToolContext) -> tuple[_IgnoreSpec, ...]:
    cwd = context.cwd.resolve(strict=False)
    try:
        relative = path.relative_to(cwd)
    except ValueError:
        return ()
    specs: list[_IgnoreSpec] = []
    current = cwd
    for part in relative.parts:
        selected = secure_tool_path(str(current), context)
        try:
            with open_directory(selected) as descriptor:
                specs.extend(_read_ignore_specs(descriptor, current, context))
        except _SearchInputLimitError:
            raise
        except ToolError:
            return tuple(specs)
        current /= part
    return tuple(specs)


def _walk_directory(
    descriptor: int | Path,
    path: Path,
    context: ToolContext,
    *,
    ignore_specs: tuple[_IgnoreSpec, ...],
    ignore_override_glob: str | None,
) -> Iterable[Path]:
    ignore_specs += _read_ignore_specs(descriptor, path, context)
    try:
        retained = _bounded_sorted_directory_entries(descriptor, path)
    except OSError as exc:
        raise ToolError(f"Could not list directory {path}: {exc}") from exc

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for entry in retained:
        name = entry.name
        candidate = path / name
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            if (_is_hidden(name) and ignore_override_glob is None) or (
                _is_ignored(candidate, is_directory=True, ignore_specs=ignore_specs)
                and not _may_reinclude_descendant(candidate, ignore_specs)
                and ignore_override_glob is None
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
                            ignore_override_glob=ignore_override_glob,
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
                    ignore_override_glob=ignore_override_glob,
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
                or (
                    ignore_override_glob is not None
                    and _matches_glob(candidate, ignore_override_glob, context)
                )
            )
            and _is_path_within_tool_cwd(candidate, context)
        ):
            yield candidate


def _bounded_sorted_directory_entries(
    descriptor: int | Path, source: Path
) -> list[os.DirEntry[str]]:
    retained: list[os.DirEntry[str]] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            retained.append(entry)
            if len(retained) > _MAX_DIRECTORY_ENTRIES:
                raise _DirectoryEntryLimitError(
                    f"Directory {source} exceeds {_MAX_DIRECTORY_ENTRIES} entries"
                )
    retained.sort(key=lambda entry: entry.name)
    return retained


def _read_bounded_ignore_lines(descriptor: int, source: str) -> tuple[str, ...]:
    with os.fdopen(os.dup(descriptor), "rb") as file:
        content = file.read(_MAX_IGNORE_FILE_BYTES + 1)
    if len(content) > _MAX_IGNORE_FILE_BYTES:
        raise _IgnoreFileLimitError(f"Ignore file {source} exceeds {_MAX_IGNORE_FILE_BYTES} bytes")
    lines = content.decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
    if len(lines) > _MAX_IGNORE_FILE_PATTERNS:
        raise _IgnoreFileLimitError(
            f"Ignore file {source} exceeds {_MAX_IGNORE_FILE_PATTERNS} patterns"
        )
    return tuple(lines)


def _read_ignore_specs(
    descriptor: int | Path, directory: Path, context: ToolContext
) -> tuple[_IgnoreSpec, ...]:
    specs: list[_IgnoreSpec] = []
    repository_exclude = _read_repository_exclude(descriptor, directory, context)
    if repository_exclude is not None:
        specs.append(repository_exclude)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for name in _IGNORE_FILES:
        try:
            if isinstance(descriptor, int):
                ignore_fd = os.open(name, flags, dir_fd=descriptor)
                try:
                    if not stat.S_ISREG(os.fstat(ignore_fd).st_mode):
                        continue
                    lines = _read_bounded_ignore_lines(ignore_fd, str(directory / name))
                finally:
                    os.close(ignore_fd)
            else:
                ignore_path = secure_tool_path(str(directory / name), context)
                with open_file(ignore_path) as ignore_fd:
                    lines = _read_bounded_ignore_lines(ignore_fd, str(directory / name))
        except _IgnoreFileLimitError:
            raise
        except (FileNotFoundError, OSError, ToolError, UnicodeDecodeError):
            continue
        specs.append(_IgnoreSpec(directory, GitIgnoreSpec.from_lines(lines)))
    return tuple(specs)


def _read_repository_exclude(
    descriptor: int | Path, directory: Path, context: ToolContext
) -> _IgnoreSpec | None:
    """Securely load a repository-local ``.git/info/exclude`` file."""

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
                        lines = _read_bounded_ignore_lines(
                            exclude_fd, str(directory / ".git" / "info" / "exclude")
                        )
                    finally:
                        os.close(exclude_fd)
                finally:
                    os.close(info_fd)
            finally:
                os.close(git_fd)
        else:
            exclude_path = secure_tool_path(str(directory / ".git" / "info" / "exclude"), context)
            with open_file(exclude_path) as exclude_fd:
                lines = _read_bounded_ignore_lines(exclude_fd, str(exclude_path.path))
    except _IgnoreFileLimitError:
        raise
    except (FileNotFoundError, OSError, ToolError, UnicodeDecodeError):
        return None
    return _IgnoreSpec(directory, GitIgnoreSpec.from_lines(lines))


def _is_ignored(path: Path, *, is_directory: bool, ignore_specs: tuple[_IgnoreSpec, ...]) -> bool:
    ignored = False
    for rules in ignore_specs:
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
    for rules in ignore_specs:
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
            exclusion_allows_reinclude = isinstance(source, str) and source.rstrip().endswith("/**")
    if not ignored or not exclusion_allows_reinclude:
        return False

    for rules in ignore_specs:
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
            if "/" not in negated.rstrip("/"):
                return True
            literal_prefix_chars: list[str] = []
            escaped = False
            for character in negated:
                if escaped:
                    literal_prefix_chars.append(character)
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character in "*?[":
                    break
                else:
                    literal_prefix_chars.append(character)
            literal_prefix = "".join(literal_prefix_chars)
            if not literal_prefix:
                return True
            if literal_prefix.startswith(relative_directory) or relative_directory.startswith(
                literal_prefix.rstrip("/") + "/"
            ):
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

    flags = bounded_regex.IGNORECASE if ignore_case else 0
    try:
        expression = bounded_regex.compile(pattern, flags=flags)
    except bounded_regex.error as exc:
        raise ToolError(f"Invalid grep pattern: {exc}") from exc

    def regex_matcher(line: str) -> bool:
        try:
            return expression.search(line, timeout=_REGEX_TIMEOUT_SECONDS) is not None
        except TimeoutError as exc:
            raise ToolError("grep pattern exceeded the regex evaluation time limit") from exc

    return regex_matcher


type CallableMatcher = Callable[[str], bool]


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
