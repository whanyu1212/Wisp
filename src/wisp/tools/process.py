"""Subprocess helpers for built-in tools."""

from __future__ import annotations

import asyncio
import ctypes
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import anyio

from wisp.tools.result import ToolError
from wisp.tools.truncation import TruncatedText, truncate_text_tail

_WINDOWS_JOB_HANDLE_ATTR = "_wisp_windows_job_handle"
_POSIX_JOBS_FILE_ATTR = "_wisp_posix_jobs_file"
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
_WINDOWS_RESUME_FAILED = 0xFFFFFFFF
_WINDOWS_TH32CS_SNAPTHREAD = 0x00000004
_WINDOWS_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_WAIT_FAILED = 0xFFFFFFFF
_WINDOWS_WAIT_TIMEOUT = 0x00000102
_LINUX_PR_SET_CHILD_SUBREAPER = 36
_POSIX_JOBS_FD_MIN = 100
_OUTPUT_LIMIT_EXIT_GRACE_SECONDS = 0.1
_POSIX_TERMINATION_GRACE_SECONDS = 0.05


class _WindowsThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _ProcessSupervisor(Protocol):
    async def _reserve_one_shot_start(self) -> None: ...

    async def _cancel_one_shot_start(self) -> None: ...

    async def _track_one_shot(
        self,
        process: asyncio.subprocess.Process,
        task: asyncio.Task[Any],
        *,
        reserved: bool = False,
    ) -> None: ...

    async def _terminate_one_shot(
        self,
        process: asyncio.subprocess.Process,
        *,
        wait: bool = False,
        force: bool = False,
    ) -> bool: ...

    async def _release_one_shot(self, process: asyncio.subprocess.Process) -> None: ...


async def _await_task_after_cancellation(task: asyncio.Future[Any]) -> Any:
    while True:
        if task.done():
            return task.result()
        try:
            with anyio.CancelScope(shield=True):
                return await asyncio.shield(task)
        except asyncio.CancelledError:
            continue


class _CtypesFunction(Protocol):
    restype: object
    argtypes: Sequence[object] | None

    def __call__(self, *args: object) -> object: ...


class _WindowsKernel32(Protocol):
    CreateJobObjectW: _CtypesFunction

    AssignProcessToJobObject: _CtypesFunction

    TerminateJobObject: _CtypesFunction

    CloseHandle: _CtypesFunction

    CreateToolhelp32Snapshot: _CtypesFunction

    OpenThread: _CtypesFunction

    ResumeThread: _CtypesFunction

    Thread32First: _CtypesFunction

    Thread32Next: _CtypesFunction

    WaitForSingleObject: _CtypesFunction


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
    process = await _create_shell_process(command, cwd=cwd)

    budget = _OutputBudget(max_bytes=max_output_bytes, max_lines=max_output_lines)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            _collect_limited_output(process, budget),
            timeout=timeout,
        )
    except TimeoutError as exc:
        cleanup_succeeded = await _await_process_cleanup(_kill_process_tree_and_wait(process))
        if not cleanup_succeeded:
            raise ToolError("Failed to terminate process tree") from exc
        raise ToolError(f"Command timed out after {timeout:g} seconds") from exc
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(_kill_process_tree_and_wait(process))
        await _await_task_after_cancellation(cleanup_task)
        raise

    cleanup_succeeded = await _await_process_cleanup(_terminate_process_tree(process))
    if not cleanup_succeeded:
        raise ToolError("Failed to terminate process tree")
    return ProcessResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated=budget.exhausted,
        stderr_truncated=budget.exhausted,
    )


async def _create_shell_process(command: str, *, cwd: Path) -> asyncio.subprocess.Process:
    posix_jobs_file: Path | None = None
    posix_jobs_fd: int | None = None
    if os.name == "posix":
        with tempfile.NamedTemporaryFile(prefix="wisp-jobs-", delete=False) as jobs_file:
            posix_jobs_file = Path(jobs_file.name)
        try:
            posix_jobs_fd = _open_posix_jobs_fd(posix_jobs_file)
        except OSError as exc:
            _remove_posix_jobs_file(posix_jobs_file)
            raise ToolError(f"Failed to start command: {exc}") from exc
        command = _wrap_posix_shell_command(command, posix_jobs_file)
    try:
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                start_new_session=False,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(
                    subprocess,
                    "CREATE_SUSPENDED",
                    _WINDOWS_CREATE_SUSPENDED,
                ),
            )
        else:
            process_kwargs: dict[str, Any] = {}
            if os.name == "posix":
                process_kwargs["preexec_fn"] = _prepare_posix_shell_child
                if posix_jobs_fd is not None:
                    process_kwargs["pass_fds"] = (posix_jobs_fd,)
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                start_new_session=os.name == "posix",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_kwargs,
            )
    except OSError as exc:
        if posix_jobs_fd is not None:
            _close_posix_fd(posix_jobs_fd)
        if posix_jobs_file is not None:
            _remove_posix_jobs_file(posix_jobs_file)
        raise ToolError(f"Failed to start command: {exc}") from exc
    except BaseException:
        if posix_jobs_fd is not None:
            _close_posix_fd(posix_jobs_fd)
        if posix_jobs_file is not None:
            _remove_posix_jobs_file(posix_jobs_file)
        raise

    if posix_jobs_fd is not None:
        _close_posix_fd(posix_jobs_fd)

    if posix_jobs_file is not None:
        setattr(process, _POSIX_JOBS_FILE_ATTR, posix_jobs_file)

    if os.name == "nt":
        try:
            setup_error = await _run_windows_process_setup(process)
        except Exception:
            with anyio.CancelScope(shield=True):
                cleanup_succeeded = await _cleanup_failed_windows_process_setup(process)
            if not cleanup_succeeded:
                raise ToolError("Failed to start command and terminate process tree") from None
            raise
        if setup_error is not None:
            with anyio.CancelScope(shield=True):
                cleanup_succeeded = await _cleanup_failed_windows_process_setup(process)
            if not cleanup_succeeded:
                raise ToolError(f"{setup_error}; failed to terminate process tree")
            raise ToolError(setup_error)
    return process


async def _await_process_cleanup(cleanup: Awaitable[bool]) -> bool:
    cleanup_task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await _await_task_after_cancellation(cleanup_task)
        raise


async def _cancel_one_shot_start_after_cancellation(
    process_supervisor: _ProcessSupervisor,
) -> None:
    cancel_task = asyncio.create_task(process_supervisor._cancel_one_shot_start())
    await _await_task_after_cancellation(cancel_task)


def _wrap_posix_shell_command(command: str, jobs_file: Path) -> str:
    quoted_jobs_file = shlex.quote(str(jobs_file))
    quoted_command = shlex.quote(command)
    return (
        "__wisp_shell_pid=$$; "
        "__wisp_recorded_pids=; "
        '__wisp_record_pid() { [ -n "$1" ] || return; '
        'case " $__wisp_recorded_pids " in *" $1 "*) return;; esac; '
        f"printf '%s\\n' \"$1\" >> {quoted_jobs_file} 2>/dev/null "
        '&& __wisp_recorded_pids="$__wisp_recorded_pids $1" || true; }; '
        "__wisp_child_pids() { "
        "if command -v pgrep >/dev/null 2>&1; then "
        'pgrep -P "$1" 2>/dev/null || true; '
        "else ps -eo pid=,ppid= 2>/dev/null | "
        "awk -v __wisp_ppid=\"$1\" '$2 == __wisp_ppid { print $1 }' "
        "2>/dev/null || true; fi; }; "
        "__wisp_record_descendants() { "
        "__wisp_pending=$1; "
        'while [ -n "$__wisp_pending" ]; do '
        "__wisp_next=; "
        "for __wisp_parent in $__wisp_pending; do "
        '__wisp_children=$(__wisp_child_pids "$__wisp_parent"); '
        'if [ -n "$__wisp_children" ]; then '
        "for __wisp_child in $__wisp_children; do "
        '__wisp_record_pid "$__wisp_child"; '
        "done; "
        '__wisp_next="$__wisp_next $__wisp_children"; '
        "fi; "
        "done; "
        '__wisp_pending="$__wisp_next"; '
        "done; }; "
        "__wisp_record_jobs() { "
        "for __wisp_job in $(jobs -pr 2>/dev/null || true); do "
        '__wisp_record_pid "$__wisp_job"; '
        "done; "
        '__wisp_record_descendants "$__wisp_shell_pid"; }; '
        "__wisp_record_loop() { while :; do __wisp_record_jobs; sleep 0.25; done; }; "
        "__wisp_record_loop & __wisp_recorder_pid=$!; "
        "trap '__wisp_record_jobs' EXIT HUP INT TERM; "
        "( trap '__wisp_record_jobs' EXIT HUP INT TERM; "
        f"eval {quoted_command} ); "
        "__wisp_status=$?; "
        "kill $__wisp_recorder_pid 2>/dev/null || true; "
        "wait $__wisp_recorder_pid 2>/dev/null || true; "
        "trap - EXIT HUP INT TERM; "
        "__wisp_record_jobs; "
        "exit $__wisp_status"
    )


def _open_posix_jobs_fd(jobs_file: Path) -> int:
    import fcntl

    fd = os.open(jobs_file, os.O_WRONLY | os.O_APPEND)
    try:
        jobs_fd = fcntl.fcntl(fd, fcntl.F_DUPFD, _POSIX_JOBS_FD_MIN)
    finally:
        _close_posix_fd(fd)
    return int(jobs_fd)


def _close_posix_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        return


def _prepare_posix_shell_child() -> None:
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl(_LINUX_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except Exception:
        return


async def _kill_process_tree_and_wait(process: asyncio.subprocess.Process) -> bool:
    terminated = await _terminate_process_tree(process)
    if not terminated:
        return False
    await process.wait()
    await _drain_process_stream(process.stdout)
    await _drain_process_stream(process.stderr)
    await asyncio.sleep(0)
    return terminated


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    force: bool = False,
) -> bool:
    """Terminate a process tree without blocking the event loop on Windows."""

    if force:
        if os.name == "nt":
            return await asyncio.to_thread(_kill_process_tree, process)
        if os.name == "posix":
            return await asyncio.to_thread(_force_kill_posix_process_tree, process)
        return _kill_process_tree(process)
    if os.name == "nt":
        return await asyncio.to_thread(_kill_process_tree, process)
    if os.name == "posix":
        return await _terminate_posix_process_tree(process)
    return _kill_process_tree(process)


async def _terminate_posix_process_tree(process: asyncio.subprocess.Process) -> bool:
    group_term_succeeded = await asyncio.to_thread(
        _signal_posix_process_group,
        process,
        signal.SIGTERM,
    )
    if not group_term_succeeded:
        return False
    await asyncio.sleep(_POSIX_TERMINATION_GRACE_SECONDS)
    return await asyncio.to_thread(_finish_posix_process_tree_termination, process)


def _force_kill_posix_process_tree(process: asyncio.subprocess.Process) -> bool:
    descendants_recorded = _record_posix_descendant_pids(process)
    if not descendants_recorded:
        return False
    return _kill_process_tree(process)


def _finish_posix_process_tree_termination(process: asyncio.subprocess.Process) -> bool:
    descendants_recorded = _record_posix_descendant_pids(process)
    if not descendants_recorded:
        return False
    group_kill_succeeded = _signal_posix_process_group(process, signal.SIGKILL)
    recorded_kill_succeeded = _signal_posix_recorded_jobs(process, signal.SIGKILL)
    if group_kill_succeeded and recorded_kill_succeeded:
        _remove_posix_jobs_file(getattr(process, _POSIX_JOBS_FILE_ATTR, None))
        return True
    return False


def _signal_posix_process_group(
    process: asyncio.subprocess.Process,
    selected_signal: signal.Signals,
) -> bool:
    if process.returncode is not None and not _posix_group_has_members_without_live_leader(
        process.pid
    ):
        return True
    try:
        os.killpg(process.pid, selected_signal)
    except ProcessLookupError:
        return True
    except PermissionError:
        try:
            if process.returncode is None:
                process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        return False
    return True


def _posix_group_has_members_without_live_leader(pgid: int) -> bool:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,pgid="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    has_member = False
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            selected_pgid = int(parts[1])
        except ValueError:
            continue
        if selected_pgid != pgid:
            continue
        if pid == pgid:
            return False
        has_member = True
    return has_member


def _signal_posix_recorded_jobs(
    process: asyncio.subprocess.Process,
    selected_signal: signal.Signals,
) -> bool:
    succeeded = True
    recorded_pids = _posix_recorded_job_pids(process)
    if not recorded_pids:
        return True
    current_owned_pids = _posix_current_owned_pids(process)
    if current_owned_pids is None:
        return False
    for pid in recorded_pids:
        if pid not in current_owned_pids:
            continue
        if not _signal_verified_posix_pid(process, pid, selected_signal):
            succeeded = False
    return succeeded


def _signal_verified_posix_pid(
    process: asyncio.subprocess.Process,
    pid: int,
    selected_signal: signal.Signals,
) -> bool:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(pidfd_open) and callable(pidfd_send_signal):
        try:
            pidfd = pidfd_open(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        try:
            current_owned_pids = _posix_current_owned_pids(process)
            if current_owned_pids is None:
                return False
            if pid not in current_owned_pids:
                return True
            try:
                pidfd_send_signal(pidfd, selected_signal, None, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            return True
        finally:
            _close_posix_fd(pidfd)

    current_owned_pids = _posix_current_owned_pids(process)
    if current_owned_pids is None:
        return False
    if pid not in current_owned_pids:
        return True
    try:
        os.kill(pid, selected_signal)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _posix_recorded_job_pids(process: asyncio.subprocess.Process) -> tuple[int, ...]:
    jobs_file = getattr(process, _POSIX_JOBS_FILE_ATTR, None)
    if not isinstance(jobs_file, Path):
        return ()
    try:
        lines = jobs_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    pids: list[int] = []
    for line in lines:
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid > 0 and pid != process.pid:
            pids.append(pid)
    return tuple(dict.fromkeys(pids))


def _posix_current_owned_pids(process: asyncio.subprocess.Process) -> set[int] | None:
    pids: list[int] = []
    if process.returncode is None:
        descendant_pids = _posix_descendant_pids(process.pid)
        if descendant_pids is not None:
            pids.extend(descendant_pids)
    jobs_file = getattr(process, _POSIX_JOBS_FILE_ATTR, None)
    if isinstance(jobs_file, Path):
        holder_pids = _posix_jobs_file_holder_pids(jobs_file)
        if holder_pids is None:
            return None
        pids.extend(holder_pids)
    return {pid for pid in pids if pid > 0 and pid != process.pid}


def _record_posix_descendant_pids(process: asyncio.subprocess.Process) -> bool:
    jobs_file = getattr(process, _POSIX_JOBS_FILE_ATTR, None)
    if not isinstance(jobs_file, Path):
        return True
    if process.returncode is None:
        descendant_pids = _posix_descendant_pids(process.pid)
        if descendant_pids is None:
            return False
    else:
        descendant_pids = ()
    holder_pids = _posix_jobs_file_holder_pids(jobs_file)
    if holder_pids is None:
        return False
    pids = tuple(dict.fromkeys((*descendant_pids, *holder_pids)))
    if not pids:
        return True
    try:
        with jobs_file.open("a", encoding="utf-8") as file:
            for pid in pids:
                file.write(f"{pid}\n")
    except OSError:
        return False
    return True


def _posix_jobs_file_holder_pids(jobs_file: Path) -> tuple[int, ...] | None:
    pids = _posix_jobs_file_holder_pids_from_proc(jobs_file)
    if pids is not None:
        return pids
    pids = _posix_jobs_file_holder_pids_from_lsof(jobs_file)
    if pids is not None:
        return pids
    pids = _posix_jobs_file_holder_pids_from_fuser(jobs_file)
    if pids is not None:
        return pids
    return None


def _posix_jobs_file_holder_pids_from_proc(jobs_file: Path) -> tuple[int, ...] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    try:
        jobs_file_path = str(jobs_file.resolve(strict=False))
    except OSError:
        jobs_file_path = str(jobs_file)
    pids: list[int] = []
    try:
        entries = tuple(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        fd_dir = entry / "fd"
        try:
            fds = tuple(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target == jobs_file_path:
                pids.append(pid)
                break
    return tuple(pids)


def _posix_jobs_file_holder_pids_from_lsof(jobs_file: Path) -> tuple[int, ...] | None:
    try:
        completed = subprocess.run(
            ["lsof", "-t", "--", str(jobs_file)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode not in (0, 1):
        return None
    return _parse_posix_pid_lines(completed.stdout.splitlines())


def _posix_jobs_file_holder_pids_from_fuser(jobs_file: Path) -> tuple[int, ...] | None:
    try:
        completed = subprocess.run(
            ["fuser", str(jobs_file)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode not in (0, 1):
        return None
    return _parse_posix_pid_tokens(f"{completed.stdout}\n{completed.stderr}".split())


def _posix_descendant_pids(root_pid: int) -> tuple[int, ...] | None:
    descendants: list[int] = []
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        child_pids = _posix_child_pids(parent_pid)
        if child_pids is None:
            return None
        descendants.extend(child_pids)
        pending.extend(child_pids)
    return tuple(dict.fromkeys(pid for pid in descendants if pid != root_pid))


def _posix_child_pids(parent_pid: int) -> tuple[int, ...] | None:
    try:
        completed = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _posix_child_pids_from_ps(parent_pid)
    if completed.returncode not in (0, 1):
        return _posix_child_pids_from_ps(parent_pid)
    return _parse_posix_pid_lines(completed.stdout.splitlines())


def _posix_child_pids_from_ps(parent_pid: int) -> tuple[int, ...] | None:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    child_pids: list[int] = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if ppid == parent_pid and pid > 0:
            child_pids.append(pid)
    return tuple(child_pids)


def _parse_posix_pid_lines(lines: Sequence[str]) -> tuple[int, ...]:
    pids: list[int] = []
    for line in lines:
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return tuple(pids)


def _parse_posix_pid_tokens(tokens: Sequence[str]) -> tuple[int, ...]:
    pids: list[int] = []
    for token in tokens:
        if not token.isdecimal():
            continue
        pid = int(token)
        if pid > 0:
            pids.append(pid)
    return tuple(pids)


def _remove_posix_jobs_file(jobs_file: object) -> None:
    if not isinstance(jobs_file, Path):
        return
    try:
        jobs_file.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


async def _drain_process_stream(stream: asyncio.StreamReader | None) -> None:
    if stream is None or stream.at_eof():
        return
    try:
        await asyncio.wait_for(stream.read(), timeout=1)
    except (OSError, RuntimeError, TimeoutError):
        return


def _kill_process_tree(process: asyncio.subprocess.Process) -> bool:
    if os.name == "posix":
        # Every process created by this module starts a fresh session whose
        # process-group id is the leader pid. Descendants can outlive that leader
        # while still holding its stdout/stderr pipes open, so signal the group
        # even after asyncio has observed a return code for the leader.
        group_succeeded = _signal_posix_process_group(process, signal.SIGKILL)
        recorded_succeeded = _signal_posix_recorded_jobs(process, signal.SIGKILL)
        if group_succeeded and recorded_succeeded:
            _remove_posix_jobs_file(getattr(process, _POSIX_JOBS_FILE_ATTR, None))
            return True
        return False
    if os.name == "nt":
        had_job = getattr(process, _WINDOWS_JOB_HANDLE_ATTR, None) is not None
        if _terminate_windows_job(process):
            return True
        leader_running = _windows_process_leader_is_running(process)
        if leader_running is False:
            return not had_job
        if leader_running is None:
            return _kill_leader_if_running(process) and not had_job
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _kill_leader_if_running(process) and not had_job
        else:
            if completed.returncode != 0:
                return _kill_leader_if_running(process) and not had_job
            return _close_windows_job_handle(process)
    else:
        if process.returncode is not None:
            return True
        process.kill()
        return True


def _kill_leader_if_running(process: asyncio.subprocess.Process) -> bool:
    if process.returncode is not None:
        return True
    try:
        process.kill()
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _cleanup_failed_windows_process_setup(process: asyncio.subprocess.Process) -> bool:
    cleanup_succeeded = await asyncio.to_thread(_kill_process_tree, process)
    if not cleanup_succeeded:
        return False
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except (OSError, RuntimeError, TimeoutError):
        return False
    return True


async def _run_windows_process_setup(process: asyncio.subprocess.Process) -> str | None:
    setup_task = asyncio.create_task(asyncio.to_thread(_prepare_windows_process_setup, process))
    try:
        return await asyncio.shield(setup_task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            try:
                await _await_windows_process_setup_after_cancellation(setup_task)
            except Exception:
                pass
            cleanup_task = asyncio.create_task(_cleanup_failed_windows_process_setup(process))
            cleanup_succeeded = await _await_task_after_cancellation(cleanup_task)
        if not cleanup_succeeded:
            raise ToolError("Failed to start command and terminate process tree") from None
        raise


async def _await_windows_process_setup_after_cancellation(
    task: asyncio.Task[str | None],
) -> str | None:
    while True:
        if task.done():
            return task.result()
        try:
            with anyio.CancelScope(shield=True):
                return await asyncio.shield(task)
        except asyncio.CancelledError:
            continue


def _prepare_windows_process_setup(process: asyncio.subprocess.Process) -> str | None:
    if not _attach_windows_job(process):
        return "Failed to attach command process to Windows job"
    if not _resume_windows_process(process):
        return "Failed to resume command after Windows process setup"
    return None


def _attach_windows_job(process: asyncio.subprocess.Process) -> bool:
    if os.name != "nt":
        return True
    kernel32 = _windows_kernel32()
    process_handle = _windows_process_handle(process)
    if kernel32 is None or process_handle is None:
        return False

    try:
        job_handle = kernel32.CreateJobObjectW(None, None)
    except (AttributeError, OSError):
        return False
    if not job_handle:
        return False
    try:
        if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
            _close_windows_handle(kernel32, job_handle)
            return False
    except (AttributeError, OSError):
        _close_windows_handle(kernel32, job_handle)
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, job_handle)
    return True


def _resume_windows_process(process: asyncio.subprocess.Process) -> bool:
    if os.name != "nt":
        return True
    kernel32 = _windows_kernel32()
    if kernel32 is None:
        return False
    thread_handle = _open_windows_process_thread(process, kernel32)
    if thread_handle is None:
        return False
    try:
        try:
            return kernel32.ResumeThread(thread_handle) != _WINDOWS_RESUME_FAILED
        except (AttributeError, OSError):
            return False
    finally:
        _close_windows_handle(kernel32, thread_handle)


def _open_windows_process_thread(
    process: asyncio.subprocess.Process,
    kernel32: _WindowsKernel32,
) -> object | None:
    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(_WINDOWS_TH32CS_SNAPTHREAD, 0)
    except (AttributeError, OSError):
        return None
    if not snapshot or snapshot == _WINDOWS_INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _WindowsThreadEntry32()
        entry.dwSize = ctypes.sizeof(_WindowsThreadEntry32)
        has_entry = bool(kernel32.Thread32First(snapshot, ctypes.pointer(entry)))
        while has_entry:
            if int(entry.th32OwnerProcessID) == process.pid:
                thread_handle = kernel32.OpenThread(
                    _WINDOWS_THREAD_SUSPEND_RESUME,
                    False,
                    entry.th32ThreadID,
                )
                if thread_handle:
                    return thread_handle
            has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.pointer(entry)))
    except (AttributeError, OSError):
        return None
    finally:
        _close_windows_handle(kernel32, snapshot)
    return None


def _terminate_windows_job(process: asyncio.subprocess.Process) -> bool:
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    if job_handle is None:
        return False
    kernel32 = _windows_kernel32()
    if kernel32 is None:
        return False
    terminated = False
    try:
        terminated = bool(kernel32.TerminateJobObject(job_handle, 1))
    except (AttributeError, OSError):
        terminated = False
    if not terminated:
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    _close_windows_handle(kernel32, job_handle)
    return terminated


def _close_windows_job_handle(process: asyncio.subprocess.Process) -> bool:
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    if job_handle is None:
        return True
    kernel32 = _windows_kernel32()
    if kernel32 is None:
        return False
    setattr(process, _WINDOWS_JOB_HANDLE_ATTR, None)
    _close_windows_handle(kernel32, job_handle)
    return True


def _windows_process_leader_is_running(process: asyncio.subprocess.Process) -> bool | None:
    if process.returncode is not None:
        return False
    kernel32 = _windows_kernel32()
    process_handle = _windows_process_handle(process)
    if kernel32 is None or process_handle is None:
        return None
    try:
        result = kernel32.WaitForSingleObject(process_handle, 0)
    except (AttributeError, OSError):
        return None
    if result == _WINDOWS_WAIT_TIMEOUT:
        return True
    if result == _WINDOWS_WAIT_FAILED:
        return None
    return False


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

    create_snapshot = kernel32_api.CreateToolhelp32Snapshot
    create_snapshot.restype = wintypes.HANDLE
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

    open_thread = kernel32_api.OpenThread
    open_thread.restype = wintypes.HANDLE
    open_thread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    resume_thread = kernel32_api.ResumeThread
    resume_thread.restype = wintypes.DWORD
    resume_thread.argtypes = [wintypes.HANDLE]

    thread32_first = kernel32_api.Thread32First
    thread32_first.restype = wintypes.BOOL
    thread32_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_WindowsThreadEntry32)]

    thread32_next = kernel32_api.Thread32Next
    thread32_next.restype = wintypes.BOOL
    thread32_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_WindowsThreadEntry32)]

    wait_for_single_object = kernel32_api.WaitForSingleObject
    wait_for_single_object.restype = wintypes.DWORD
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
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
    *,
    terminate: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[bytes, bytes]:
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_task = asyncio.create_task(
        _read_stream_limited(process.stdout, budget, process, terminate=terminate)
    )
    stderr_task = asyncio.create_task(
        _read_stream_limited(process.stderr, budget, process, terminate=terminate)
    )
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
    *,
    terminate: Callable[[], Awaitable[bool]] | None = None,
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
                await _terminate_for_output_limit(process, terminate=terminate)
            while await stream.read(8192):
                pass
            break
    return b"".join(chunks)


async def _terminate_for_output_limit(
    process: asyncio.subprocess.Process,
    *,
    terminate: Callable[[], Awaitable[bool]] | None = None,
) -> None:
    cleanup_succeeded = (
        await terminate()
        if terminate is not None
        else await _terminate_process_tree(process, force=True)
    )
    if cleanup_succeeded:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_OUTPUT_LIMIT_EXIT_GRACE_SECONDS)
    except TimeoutError as exc:
        raise ToolError("Failed to terminate process tree") from exc


async def _run_exec_limited_stdout(
    command: Sequence[str],
    *,
    cwd: Path,
    process_supervisor: _ProcessSupervisor,
    max_stdout_lines: int,
    stdout_line_filter: Callable[[str], bool] | None = None,
    stdout_count_filter: Callable[[str], bool] | None = None,
    max_buffered_stdout_bytes: int | None = None,
    max_buffered_stdout_lines: int | None = None,
    max_buffered_stderr_bytes: int | None = None,
    max_buffered_stderr_lines: int | None = None,
) -> ProcessResult:
    await process_supervisor._reserve_one_shot_start()
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            start_new_session=os.name == "posix",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        await _cancel_one_shot_start_after_cancellation(process_supervisor)
        raise ToolError(f"Failed to start command: {exc}") from exc
    except BaseException:
        await _cancel_one_shot_start_after_cancellation(process_supervisor)
        raise
    assert process is not None

    owner_task = asyncio.current_task()
    assert owner_task is not None
    release_ownership = False
    track_task = asyncio.create_task(
        process_supervisor._track_one_shot(process, owner_task, reserved=True)
    )
    try:
        await asyncio.shield(track_task)
    except asyncio.CancelledError:
        try:
            await _await_task_after_cancellation(track_task)
        except BaseException:
            pass
        else:
            cleanup_task = asyncio.create_task(
                process_supervisor._terminate_one_shot(process, wait=True)
            )
            release_ownership = bool(await _await_task_after_cancellation(cleanup_task))
            if release_ownership:
                release_task = asyncio.create_task(process_supervisor._release_one_shot(process))
                await _await_task_after_cancellation(release_task)
        raise

    async def terminate() -> bool:
        return await process_supervisor._terminate_one_shot(process)

    async def finish_failed_execution() -> bool:
        if not stderr_task.done():
            stderr_task.cancel()
        drain_task = asyncio.ensure_future(asyncio.gather(stderr_task, return_exceptions=True))
        await _await_task_after_cancellation(drain_task)
        cleanup_task = asyncio.create_task(
            process_supervisor._terminate_one_shot(process, wait=True)
        )
        return bool(await _await_task_after_cancellation(cleanup_task))

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_stream = process.stdout

    stderr_budget: _OutputBudget | None = None
    if max_buffered_stderr_bytes is not None or max_buffered_stderr_lines is not None:
        stderr_budget = _OutputBudget(
            max_bytes=max_buffered_stderr_bytes if max_buffered_stderr_bytes is not None else 2**63,
            max_lines=max_buffered_stderr_lines if max_buffered_stderr_lines is not None else 2**63,
        )
        stderr_task = asyncio.create_task(
            _read_stream_limited(process.stderr, stderr_budget, process, terminate=terminate)
        )
    else:
        stderr_task = asyncio.create_task(process.stderr.read())
    stdout_lines: list[bytes] = []
    stdout_count = 0
    buffered_stdout_bytes = 0
    buffered_stdout_lines = 0
    stdout_truncated = False

    async def read_stdout_line() -> bytes:
        if stderr_budget is None:
            return await stdout_stream.readline()
        if stderr_task.done():
            failure = stderr_task.exception()
            if failure is not None:
                raise failure
            return await stdout_stream.readline()

        stdout_task = asyncio.create_task(stdout_stream.readline())
        done, _pending = await asyncio.wait(
            (stdout_task, stderr_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stderr_task in done:
            failure = stderr_task.exception()
            if failure is not None:
                stdout_task.cancel()
                await asyncio.gather(stdout_task, return_exceptions=True)
                raise failure
        return await stdout_task

    try:
        while stdout_count < max_stdout_lines:
            line = await read_stdout_line()
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
                    await _terminate_for_output_limit(process, terminate=terminate)
                    break
                if max_buffered_stdout_bytes is not None:
                    remaining_bytes = max_buffered_stdout_bytes - buffered_stdout_bytes
                    if remaining_bytes <= 0:
                        if count_line:
                            stdout_count += 1
                        stdout_truncated = True
                        await _terminate_for_output_limit(process, terminate=terminate)
                        break
                    if len(line) > remaining_bytes:
                        stdout_lines.append(line[:remaining_bytes])
                        buffered_stdout_lines += 1
                        buffered_stdout_bytes += remaining_bytes
                        if count_line:
                            stdout_count += 1
                        stdout_truncated = True
                        await _terminate_for_output_limit(process, terminate=terminate)
                        break
                stdout_lines.append(line)
                buffered_stdout_lines += 1
                buffered_stdout_bytes += len(line)
                if count_line:
                    stdout_count += 1

        if stdout_count >= max_stdout_lines:
            stdout_truncated = True
            await _terminate_for_output_limit(process, terminate=terminate)

        await process.wait()
        stderr_bytes = await stderr_task
        if not await process_supervisor._terminate_one_shot(process):
            raise ToolError("Failed to terminate process tree")
    except asyncio.CancelledError:
        release_ownership = await finish_failed_execution()
        raise
    except BaseException:
        release_ownership = await finish_failed_execution()
        raise
    else:
        release_ownership = True
        return ProcessResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=b"".join(stdout_lines).decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_budget.exhausted if stderr_budget is not None else False,
            stdout_count=stdout_count,
        )
    finally:
        if release_ownership:
            release_task = asyncio.create_task(process_supervisor._release_one_shot(process))
            await _await_task_after_cancellation(release_task)


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
