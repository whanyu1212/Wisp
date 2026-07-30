"""Subprocess helpers for built-in tools."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import anyio

from wisp.tools.result import ToolError
from wisp.tools.truncation import TruncatedText, truncate_text_tail

_WINDOWS_JOB_HANDLE_ATTR = "_wisp_windows_job_handle"


class _CtypesFunction(Protocol):
    restype: object
    argtypes: Sequence[object] | None

    def __call__(self, *args: object) -> object: ...


class _WindowsKernel32(Protocol):
    CreateJobObjectW: _CtypesFunction

    AssignProcessToJobObject: _CtypesFunction

    TerminateJobObject: _CtypesFunction

    CloseHandle: _CtypesFunction


@dataclass(frozen=True)
class ProcessResult:
    """Captured subprocess output."""

    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_count: int = 0


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
    _attach_windows_job(process)

    budget = _OutputBudget(max_bytes=max_output_bytes, max_lines=max_output_lines)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            _collect_limited_output(process, budget),
            timeout=timeout,
        )
    except TimeoutError as exc:
        await _kill_process_tree_and_wait(process)
        raise ToolError(f"Command timed out after {timeout:g} seconds") from exc
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await _kill_process_tree_and_wait(process)
        raise

    with anyio.CancelScope(shield=True):
        await _terminate_process_tree(process)
    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated=budget.exhausted,
        stderr_truncated=budget.exhausted,
    )


async def _kill_process_tree_and_wait(process: asyncio.subprocess.Process) -> None:
    await _terminate_process_tree(process)
    await process.wait()
    await _drain_process_stream(process.stdout)
    await _drain_process_stream(process.stderr)
    await asyncio.sleep(0)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a process tree without blocking the event loop on Windows."""

    if os.name == "nt":
        await asyncio.to_thread(_kill_process_tree, process)
        return
    _kill_process_tree(process)


async def _drain_process_stream(stream: asyncio.StreamReader | None) -> None:
    if stream is None or stream.at_eof():
        return
    try:
        await asyncio.wait_for(stream.read(), timeout=1)
    except (OSError, RuntimeError, TimeoutError):
        return


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        # Every process created by this module starts a fresh session whose
        # process-group id is the leader pid. Descendants can outlive that leader
        # while still holding its stdout/stderr pipes open, so signal the group
        # even after asyncio has observed a return code for the leader.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            if process.returncode is not None:
                return
            try:
                process.kill()
            except (ProcessLookupError, PermissionError):
                return
        return
    if os.name == "nt":
        _terminate_windows_job(process)
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            _kill_leader_if_running(process)
        else:
            if completed.returncode != 0:
                _kill_leader_if_running(process)
    else:
        if process.returncode is not None:
            return
        process.kill()


def _kill_leader_if_running(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, PermissionError):
        return


def _attach_windows_job(process: asyncio.subprocess.Process) -> None:
    if os.name != "nt":
        return
    kernel32 = _windows_kernel32()
    process_handle = _windows_process_handle(process)
    if kernel32 is None or process_handle is None:
        return

    try:
        job_handle = kernel32.CreateJobObjectW(None, None)
    except (AttributeError, OSError):
        return
    if not job_handle:
        return
    try:
        if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
            _close_windows_handle(kernel32, job_handle)
            return
    except (AttributeError, OSError):
        _close_windows_handle(kernel32, job_handle)
        return
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, job_handle)


def _terminate_windows_job(process: asyncio.subprocess.Process) -> bool:
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    if job_handle is None:
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    kernel32 = _windows_kernel32()
    if kernel32 is None:
        return False
    terminated = False
    try:
        terminated = bool(kernel32.TerminateJobObject(job_handle, 1))
    except (AttributeError, OSError):
        terminated = False
    finally:
        _close_windows_handle(kernel32, job_handle)
    return terminated


def _close_windows_handle(kernel32: _WindowsKernel32, handle: object) -> None:
    try:
        kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return


def _windows_kernel32() -> _WindowsKernel32 | None:
    windll_factory = getattr(ctypes, "WinDLL", None)
    if windll_factory is None:
        return None
    try:
        return _configure_windows_job_api(windll_factory("kernel32", use_last_error=True))
    except (AttributeError, OSError):
        return None


def _configure_windows_job_api(kernel32: object) -> _WindowsKernel32:
    kernel32_api = cast(_WindowsKernel32, kernel32)

    create_job = kernel32_api.CreateJobObjectW
    create_job.restype = wintypes.HANDLE
    create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

    assign_process = kernel32_api.AssignProcessToJobObject
    assign_process.restype = wintypes.BOOL
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    terminate_job = kernel32_api.TerminateJobObject
    terminate_job.restype = wintypes.BOOL
    terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]

    close_handle = kernel32_api.CloseHandle
    close_handle.restype = wintypes.BOOL
    close_handle.argtypes = [wintypes.HANDLE]
    return kernel32_api


def _windows_process_handle(process: asyncio.subprocess.Process) -> object | None:
    direct_handle = getattr(process, "_handle", None)
    if direct_handle is not None:
        return cast(object, direct_handle)
    transport = getattr(process, "_transport", None)
    get_extra_info = getattr(transport, "get_extra_info", None)
    if get_extra_info is None:
        return None
    subprocess_handle = get_extra_info("subprocess")
    return cast(object | None, getattr(subprocess_handle, "_handle", None))


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
                await _terminate_process_tree(process)
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
    try:
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
                    await _terminate_process_tree(process)
                    break
                if max_buffered_stdout_bytes is not None:
                    remaining_bytes = max_buffered_stdout_bytes - buffered_stdout_bytes
                    if remaining_bytes <= 0:
                        if count_line:
                            stdout_count += 1
                        stdout_truncated = True
                        await _terminate_process_tree(process)
                        break
                    if len(line) > remaining_bytes:
                        stdout_lines.append(line[:remaining_bytes])
                        buffered_stdout_lines += 1
                        buffered_stdout_bytes += remaining_bytes
                        if count_line:
                            stdout_count += 1
                        stdout_truncated = True
                        await _terminate_process_tree(process)
                        break
                stdout_lines.append(line)
                buffered_stdout_lines += 1
                buffered_stdout_bytes += len(line)
                if count_line:
                    stdout_count += 1

        if stdout_count >= max_stdout_lines:
            stdout_truncated = True
            await _terminate_process_tree(process)

        await process.wait()
        stderr_bytes = await stderr_task
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            await _kill_process_tree_and_wait(process)
        raise
    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=b"".join(stdout_lines).decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_budget.exhausted if stderr_budget is not None else False,
        stdout_count=stdout_count,
    )


def _format_process_output(exit_code: int, stdout: str, stderr: str) -> str:
    parts = _process_output_parts(stdout, stderr)
    status = f"Command exited with code {exit_code}"
    if not parts:
        return status
    return f"{status}: {'\n'.join(parts)}"


def _format_process_output_bounded(
    exit_code: int,
    stdout: str,
    stderr: str,
    *,
    max_bytes: int,
    max_lines: int,
) -> TruncatedText:
    """Format process output within its budget while preserving diagnostics."""

    parts = _process_output_parts(stdout, stderr)
    status = f"Command exited with code {exit_code}"
    if not parts:
        # Completion evidence is fixed-size metadata, not captured command output.
        # Keep it intact even when an embedding deliberately selects a tiny body
        # budget; otherwise the model cannot distinguish success from failure.
        return TruncatedText(text=status, truncated=False)

    prefix = f"{status}: "
    if max_bytes <= 0 or max_lines <= 0:
        return TruncatedText(text=status, truncated=True)

    bounded_body = truncate_text_tail(
        "\n".join(parts),
        # The fixed status prefix is bounded metadata outside the configured
        # captured-output budget. This preserves both the complete exit code and
        # the diagnostic tail for every positive body budget.
        max_bytes=max_bytes,
        max_lines=max_lines,
    )
    return TruncatedText(
        text=f"{prefix}{bounded_body.text}",
        truncated=bounded_body.truncated,
    )


def _process_output_parts(stdout: str, stderr: str) -> list[str]:
    parts: list[str] = []
    if stdout:
        stripped_stdout = stdout.rstrip("\n")
        if stripped_stdout:
            parts.append(stripped_stdout)
    if stderr:
        stripped_stderr = stderr.rstrip("\n")
        if stripped_stderr:
            parts.append(stripped_stderr)
    return parts
