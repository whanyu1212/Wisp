"""Built-in local tools registered by Wisp."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from wisp.tools.base import Tool, ToolArguments, ToolInputSchema
from wisp.tools.context import ToolContext
from wisp.tools.paths import display_tool_path, resolve_tool_path
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


@dataclass(frozen=True)
class ProcessResult:
    """Captured subprocess output."""

    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class ReadTool:
    """Read text files with optional line slicing."""

    name = "read"
    description = "Read a UTF-8 text file. Supports 1-indexed offset and line limit."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = resolve_tool_path(_required_string(arguments, "path"), context)
        offset = _optional_int(arguments, "offset", default=1)
        limit = _optional_int(arguments, "limit")

        if offset is None or offset < 1:
            raise ToolError("read.offset must be greater than or equal to 1")
        if limit is not None and limit < 1:
            raise ToolError("read.limit must be greater than or equal to 1")
        if not path.is_file():
            raise ToolError(f"File does not exist: {display_tool_path(path, context)}")

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_tool_path(path, context)}") from exc

        lines = text.splitlines(keepends=True)
        start = offset - 1
        end = start + limit if limit is not None else None
        selected = "".join(lines[start:end])
        truncated = truncate_text(
            selected,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=truncated.text,
            data={
                "path": display_tool_path(path, context),
                "line_count": len(lines),
                "offset": offset,
                "limit": limit,
            },
            truncated=truncated.truncated,
        )


class WriteTool:
    """Create or overwrite UTF-8 text files."""

    name = "write"
    description = "Create or overwrite a UTF-8 text file, creating parent directories."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = resolve_tool_path(_required_string(arguments, "path"), context)
        content = _required_string(arguments, "content", allow_empty=True)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        byte_count = len(content.encode("utf-8"))
        return ToolResult(
            text=f"Wrote {byte_count} bytes to {display_tool_path(path, context)}",
            data={"path": display_tool_path(path, context), "bytes": byte_count},
        )


class EditTool:
    """Apply exact text replacements to a file."""

    name = "edit"
    description = "Apply unique, non-overlapping exact text replacements to a UTF-8 file."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "oldText": {"type": "string"},
                        "newText": {"type": "string"},
                    },
                    "required": ["oldText", "newText"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        path = resolve_tool_path(_required_string(arguments, "path"), context)
        edits = _parse_edits(arguments)
        if not path.is_file():
            raise ToolError(f"File does not exist: {display_tool_path(path, context)}")

        try:
            with path.open("r", encoding="utf-8", newline="") as file:
                original = file.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_tool_path(path, context)}") from exc

        replacements: list[tuple[int, int, str]] = []
        for old_text, new_text in edits:
            occurrences = _find_occurrences(original, old_text)
            if len(occurrences) != 1:
                raise ToolError(
                    f"edit.oldText must match exactly once; found {len(occurrences)} matches"
                )
            start = occurrences[0]
            replacements.append((start, start + len(old_text), new_text))

        replacements.sort(key=lambda replacement: replacement[0])
        previous_end = -1
        for start, end, _new_text in replacements:
            if start < previous_end:
                raise ToolError("edit replacements must not overlap")
            previous_end = end

        parts: list[str] = []
        cursor = 0
        for start, end, new_text in replacements:
            parts.append(original[cursor:start])
            parts.append(new_text)
            cursor = end
        parts.append(original[cursor:])

        with path.open("w", encoding="utf-8", newline="") as file:
            file.write("".join(parts))
        return ToolResult(
            text=f"Applied {len(edits)} edit(s) to {display_tool_path(path, context)}",
            data={"path": display_tool_path(path, context), "edits": len(edits)},
        )


class BashTool:
    """Run shell commands in the tool working directory."""

    name = "bash"
    description = "Run a shell command and capture stdout, stderr, and exit code."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "default": 30},
        },
        "required": ["command"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        command = _required_string(arguments, "command")
        timeout = _optional_int(arguments, "timeout", default=30)
        if timeout is None or timeout < 1:
            raise ToolError("bash.timeout must be greater than or equal to 1")

        result = await _run_shell(
            command,
            cwd=context.cwd,
            timeout=float(timeout),
            max_output_bytes=context.max_output_bytes,
            max_output_lines=context.max_output_lines,
        )
        stdout = truncate_text(
            result.stdout,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        stderr = truncate_text(
            result.stderr,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        output = _format_process_output(result.exit_code, stdout.text, stderr.text)
        truncated_output = truncate_text(
            output,
            max_bytes=context.max_output_bytes,
            max_lines=context.max_output_lines,
        )
        return ToolResult(
            text=truncated_output.text,
            data={
                "exit_code": result.exit_code,
                "stdout": stdout.text,
                "stderr": stderr.text,
            },
            truncated=(
                result.stdout_truncated
                or result.stderr_truncated
                or stdout.truncated
                or stderr.truncated
                or truncated_output.truncated
            ),
        )


class GrepTool:
    """Search file contents."""

    name = "grep"
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
    description = "Find files with ripgrep when available, falling back to Python walking."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "pattern": {"type": "string", "default": "*"},
            "max_results": {"type": "integer", "minimum": 1, "default": 100},
        },
    }

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
            )

        return _python_find(path=path, pattern=pattern, max_results=max_results, context=context)


class LsTool:
    """List directory entries."""

    name = "ls"
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


def builtin_tools() -> tuple[Tool, ...]:
    """Return Wisp's built-in local tools."""

    return (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GrepTool(),
        FindTool(),
        LsTool(),
    )


def _required_string(
    arguments: Mapping[str, object],
    name: str,
    *,
    allow_empty: bool = False,
    allow_whitespace: bool = False,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    if not allow_empty:
        is_empty = value == "" if allow_whitespace else not value.strip()
        if is_empty:
            raise ToolError(f"{name} must not be empty")
    return value


def _optional_string(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    return value


def _optional_int(
    arguments: Mapping[str, object],
    name: str,
    *,
    default: int | None = None,
) -> int | None:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} must be an integer")
    return value


def _optional_bool(arguments: Mapping[str, object], name: str, *, default: bool) -> bool:
    value = arguments.get(name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolError(f"{name} must be a boolean")
    return value


def _parse_edits(arguments: Mapping[str, object]) -> list[tuple[str, str]]:
    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, list):
        raise ToolError("edits must be a list")
    if not raw_edits:
        raise ToolError("edits must not be empty")

    edits: list[tuple[str, str]] = []
    for raw_edit in raw_edits:
        if not isinstance(raw_edit, Mapping):
            raise ToolError("each edit must be an object")
        old_text = raw_edit.get("oldText")
        new_text = raw_edit.get("newText")
        if not isinstance(old_text, str) or old_text == "":
            raise ToolError("each edit.oldText must be a non-empty string")
        if not isinstance(new_text, str):
            raise ToolError("each edit.newText must be a string")
        edits.append((old_text, new_text))
    return edits


def _find_occurrences(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        position = text.find(needle, start)
        if position == -1:
            return positions
        positions.append(position)
        start = position + 1


class _OutputBudget:
    def __init__(self, *, max_bytes: int, max_lines: int) -> None:
        self._remaining_bytes = max(0, max_bytes)
        self._remaining_lines = max(0, max_lines)
        self._lock = asyncio.Lock()
        self._kill_requested = False
        self.exhausted = self._remaining_bytes == 0 or self._remaining_lines == 0

    async def take(self, chunk: bytes) -> tuple[bytes, bool]:
        async with self._lock:
            if self.exhausted:
                return b"", True
            if self._remaining_bytes <= 0 or self._remaining_lines <= 0:
                self.exhausted = True
                return b"", True

            accepted = chunk[: self._remaining_bytes]
            if accepted.count(b"\n") >= self._remaining_lines:
                accepted = accepted[: _offset_after_nth_newline(accepted, self._remaining_lines)]

            self._remaining_bytes -= len(accepted)
            self._remaining_lines -= accepted.count(b"\n")
            if len(accepted) < len(chunk):
                self.exhausted = True
            return accepted, self.exhausted

    async def request_kill_once(self) -> bool:
        async with self._lock:
            if self._kill_requested:
                return False
            self._kill_requested = True
            return True


def _offset_after_nth_newline(chunk: bytes, newline_count: int) -> int:
    position = -1
    for _ in range(newline_count):
        position = chunk.find(b"\n", position + 1)
        if position == -1:
            return len(chunk)
    return position + 1


async def _run_shell(
    command: str,
    *,
    cwd: Path,
    timeout: float,
    max_output_bytes: int,
    max_output_lines: int,
) -> ProcessResult:
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            start_new_session=os.name == "posix",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ToolError(f"Failed to start command: {exc}") from exc

    budget = _OutputBudget(max_bytes=max_output_bytes, max_lines=max_output_lines)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            _collect_limited_output(process, budget),
            timeout=timeout,
        )
    except TimeoutError as exc:
        _kill_process_tree(process)
        await process.wait()
        raise ToolError(f"Command timed out after {timeout:g} seconds") from exc

    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated=budget.exhausted,
        stderr_truncated=budget.exhausted,
    )


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                process.kill()
            except (ProcessLookupError, PermissionError):
                return
    elif os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        else:
            if completed.returncode != 0:
                process.kill()
    else:
        process.kill()


async def _collect_limited_output(
    process: asyncio.subprocess.Process,
    budget: _OutputBudget,
) -> tuple[bytes, bytes]:
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_task = asyncio.create_task(_read_stream_limited(process.stdout, budget, process))
    stderr_task = asyncio.create_task(_read_stream_limited(process.stderr, budget, process))
    try:
        stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
        await process.wait()
        return stdout_bytes, stderr_bytes
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


async def _read_stream_limited(
    stream: asyncio.StreamReader,
    budget: _OutputBudget,
    process: asyncio.subprocess.Process,
) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        accepted, exhausted = await budget.take(chunk)
        if accepted:
            chunks.append(accepted)
        if exhausted:
            if await budget.request_kill_once():
                _kill_process_tree(process)
            while await stream.read(8192):
                pass
            break
    return b"".join(chunks)


async def _run_exec_limited_stdout(
    command: Sequence[str],
    *,
    cwd: Path,
    max_stdout_lines: int,
    stdout_line_filter: Callable[[str], bool] | None = None,
    stdout_count_filter: Callable[[str], bool] | None = None,
    max_buffered_stdout_bytes: int | None = None,
    max_buffered_stdout_lines: int | None = None,
) -> ProcessResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            start_new_session=os.name == "posix",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ToolError(f"Failed to start command: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None

    stderr_task = asyncio.create_task(process.stderr.read())
    stdout_lines: list[bytes] = []
    stdout_count = 0
    buffered_stdout_bytes = 0
    buffered_stdout_lines = 0
    stdout_truncated = False
    while stdout_count < max_stdout_lines:
        line = await process.stdout.readline()
        if not line:
            break
        decoded_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if stdout_line_filter is None or stdout_line_filter(decoded_line):
            if (
                max_buffered_stdout_lines is not None
                and buffered_stdout_lines >= max_buffered_stdout_lines
            ) or (
                max_buffered_stdout_bytes is not None
                and buffered_stdout_bytes + len(line) > max_buffered_stdout_bytes
            ):
                stdout_truncated = True
                _kill_process_tree(process)
                break
            stdout_lines.append(line)
            buffered_stdout_lines += 1
            buffered_stdout_bytes += len(line)
            if stdout_count_filter is None or stdout_count_filter(decoded_line):
                stdout_count += 1

    if stdout_count >= max_stdout_lines:
        stdout_truncated = True
        _kill_process_tree(process)

    await process.wait()
    stderr_bytes = await stderr_task
    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=b"".join(stdout_lines).decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated=stdout_truncated,
    )


def _format_process_output(exit_code: int, stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(stderr.rstrip("\n"))
    if not parts:
        return f"Command exited with code {exit_code}"
    return "\n".join(parts)


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
) -> ToolResult:
    effective_context_lines = _bounded_rg_context_lines(context_lines, context)
    context_truncated = effective_context_lines < context_lines
    command = [
        rg_path,
        "--line-number",
        "--no-heading",
        "--color=never",
        "--with-filename",
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

    result = await _run_exec_limited_stdout(
        command,
        cwd=context.cwd,
        max_stdout_lines=max_results + 1,
        stdout_count_filter=_is_grep_match_line,
        max_buffered_stdout_bytes=max(0, context.max_output_bytes),
        max_buffered_stdout_lines=max(0, context.max_output_lines),
    )
    if result.exit_code == 1 and not result.stdout.strip():
        return ToolResult(text="No matches", data={"count": 0, "matches": []})
    if result.exit_code != 0 and not result.stdout_truncated:
        raise ToolError(result.stderr.strip() or f"rg failed with exit code {result.exit_code}")

    return _result_from_grep_lines(
        [_normalize_rg_line(line) for line in result.stdout.splitlines()],
        max_results=max_results,
        context=context,
        force_truncated=result.stdout_truncated or context_truncated,
    )


def _bounded_rg_context_lines(requested_context_lines: int, context: ToolContext) -> int:
    if requested_context_lines <= 0:
        return 0
    return min(requested_context_lines, max(0, (context.max_output_lines - 1) // 2))


async def _run_rg_find(
    rg_path: str,
    *,
    path: Path,
    pattern: str,
    max_results: int,
    context: ToolContext,
) -> ToolResult:
    if path.is_file():
        candidates = [path]
    else:
        result = await _run_exec_limited_stdout(
            [rg_path, "--files", "--", _command_path(path, context)],
            cwd=context.cwd,
            max_stdout_lines=max_results + 1,
            stdout_line_filter=lambda line: _matches_glob(
                _path_from_rg_line(line, context),
                pattern,
                context,
            ),
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
        if _matches_glob(candidate, pattern, context)
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
    for file_path in _iter_files(path):
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
        for candidate in _iter_files(path)
        if _matches_glob(candidate, pattern, context)
    ]
    matches.sort()
    return _result_from_lines(
        matches, max_results=max_results, context=context, count_label="files"
    )


def _iter_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return

    for root, dir_names, file_names in os.walk(path):
        dir_names[:] = sorted(
            name for name in dir_names if name not in IGNORED_DIRS and not _is_hidden(name)
        )
        for file_name in sorted(name for name in file_names if not _is_hidden(name)):
            yield Path(root) / file_name


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
    if line.startswith("./"):
        return line[2:]
    return line


def _is_grep_match_line(line: str) -> bool:
    return re.match(r"^.+:\d+:", line) is not None


def _is_grep_context_line(line: str) -> bool:
    return re.match(r"^.+-\d+-", line) is not None


def _path_from_rg_line(line: str, context: ToolContext) -> Path:
    path = Path(line.rstrip("\r\n"))
    if not path.is_absolute():
        path = context.cwd / path
    return path.resolve(strict=False)


def _result_from_grep_lines(
    lines: Sequence[str],
    *,
    max_results: int,
    context: ToolContext,
    force_truncated: bool = False,
) -> ToolResult:
    kept: list[str] = []
    match_count = 0
    truncated_by_count = force_truncated
    for line in lines:
        if line == "--" and match_count >= max_results:
            truncated_by_count = True
            break
        if _is_grep_match_line(line):
            if match_count >= max_results:
                truncated_by_count = True
                break
            match_count += 1
        elif match_count >= max_results and not _is_grep_context_line(line):
            truncated_by_count = True
            break
        kept.append(line)

    if not kept:
        return ToolResult(text="No matches", data={"count": 0, "matches": []})

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
        data={"count": match_count, "matches": kept},
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
