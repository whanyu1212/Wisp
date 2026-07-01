"""Built-in local tools registered by Wisp."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from wisp.tools.base import Tool, ToolArguments, ToolInputSchema, ToolSafety
from wisp.tools.context import ToolContext
from wisp.tools.paths import display_tool_path, resolve_tool_path
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.truncation import TruncatedText, truncate_text

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
RG_SANDBOX_ARGS = ("--no-config", "--no-follow")


@dataclass(frozen=True)
class ProcessResult:
    """Captured subprocess output."""

    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_count: int = 0


class ReadTool:
    """Read text files with optional line slicing."""

    name = "read"
    safety: ToolSafety = "read"
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
            selected, line_count, stream_truncated = _read_line_slice(
                path,
                offset=offset,
                limit=limit,
                max_bytes=context.max_output_bytes,
                max_lines=context.max_output_lines,
            )
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_tool_path(path, context)}") from exc

        truncated = _truncate_text(selected, context=context, force_truncated=stream_truncated)
        return ToolResult(
            text=truncated.text,
            data={
                "path": display_tool_path(path, context),
                "line_count": line_count,
                "offset": offset,
                "limit": limit,
            },
            truncated=truncated.truncated,
        )


class WriteTool:
    """Create or overwrite UTF-8 text files."""

    name = "write"
    safety: ToolSafety = "mutating"
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
        with path.open("w", encoding="utf-8", newline="") as file:
            file.write(content)
        byte_count = len(content.encode("utf-8"))
        return ToolResult(
            text=f"Wrote {byte_count} bytes to {display_tool_path(path, context)}",
            data={"path": display_tool_path(path, context), "bytes": byte_count},
        )


class EditTool:
    """Apply exact text replacements to a file."""

    name = "edit"
    safety: ToolSafety = "mutating"
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
    safety: ToolSafety = "command"
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


def _read_line_slice(
    path: Path,
    *,
    offset: int,
    limit: int | None,
    max_bytes: int,
    max_lines: int,
) -> tuple[str, int, bool]:
    selected_parts: list[str] = []
    line_count = 0
    selected_count = 0
    buffered_bytes = 0
    buffered_lines = 0
    truncated = False
    buffering = True

    with path.open("r", encoding="utf-8", newline="") as file:
        for line in file:
            line_count += 1
            if line_count < offset:
                continue
            if limit is not None and selected_count >= limit:
                continue

            selected_count += 1
            if not buffering:
                continue
            if max_bytes <= 0 or max_lines <= 0 or buffered_lines >= max_lines:
                truncated = True
                buffering = False
                continue

            encoded_line = line.encode("utf-8")
            remaining_bytes = max_bytes - buffered_bytes
            if remaining_bytes <= 0:
                truncated = True
                buffering = False
                continue
            if len(encoded_line) > remaining_bytes:
                selected_parts.append(
                    encoded_line[:remaining_bytes].decode("utf-8", errors="ignore")
                )
                truncated = True
                buffering = False
                continue

            selected_parts.append(line)
            buffered_bytes += len(encoded_line)
            buffered_lines += 1

    return "".join(selected_parts), line_count, truncated


def _truncate_text(
    text: str,
    *,
    context: ToolContext,
    force_truncated: bool = False,
) -> TruncatedText:
    if force_truncated:
        separator = "" if not text or text.endswith("\n") else "\n"
        text = f"{text}{separator}[truncated]"
    return truncate_text(
        text,
        max_bytes=context.max_output_bytes,
        max_lines=context.max_output_lines,
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
    max_buffered_stderr_bytes: int | None = None,
    max_buffered_stderr_lines: int | None = None,
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

    stderr_budget: _OutputBudget | None = None
    if max_buffered_stderr_bytes is not None or max_buffered_stderr_lines is not None:
        stderr_budget = _OutputBudget(
            max_bytes=max_buffered_stderr_bytes if max_buffered_stderr_bytes is not None else 2**63,
            max_lines=max_buffered_stderr_lines if max_buffered_stderr_lines is not None else 2**63,
        )
        stderr_task = asyncio.create_task(
            _read_stream_limited(process.stderr, stderr_budget, process)
        )
    else:
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
            count_line = stdout_count_filter is None or stdout_count_filter(decoded_line)
            if (
                max_buffered_stdout_lines is not None
                and buffered_stdout_lines >= max_buffered_stdout_lines
            ):
                if count_line:
                    stdout_count += 1
                stdout_truncated = True
                _kill_process_tree(process)
                break
            if max_buffered_stdout_bytes is not None:
                remaining_bytes = max_buffered_stdout_bytes - buffered_stdout_bytes
                if remaining_bytes <= 0:
                    if count_line:
                        stdout_count += 1
                    stdout_truncated = True
                    _kill_process_tree(process)
                    break
                if len(line) > remaining_bytes:
                    stdout_lines.append(line[:remaining_bytes])
                    buffered_stdout_lines += 1
                    buffered_stdout_bytes += remaining_bytes
                    if count_line:
                        stdout_count += 1
                    stdout_truncated = True
                    _kill_process_tree(process)
                    break
            stdout_lines.append(line)
            buffered_stdout_lines += 1
            buffered_stdout_bytes += len(line)
            if count_line:
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
        stderr_truncated=stderr_budget.exhausted if stderr_budget is not None else False,
        stdout_count=stdout_count,
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
        *RG_SANDBOX_ARGS,
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

    result = await _run_exec_limited_stdout(
        command,
        cwd=context.cwd,
        max_stdout_lines=max_results + 1,
        stdout_count_filter=_is_grep_match_line,
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
) -> ToolResult:
    if path.is_file():
        candidates = [path]
    else:
        result = await _run_exec_limited_stdout(
            [rg_path, *RG_SANDBOX_ARGS, "--files", "--", _command_path(path, context)],
            cwd=context.cwd,
            max_stdout_lines=max_results + 1,
            stdout_line_filter=lambda line: _matches_glob(
                _path_from_rg_line(line, context),
                pattern,
                context,
            ),
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
    return path.resolve(strict=False)


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
