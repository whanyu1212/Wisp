"""Bounded lifecycle management for shell processes."""

from __future__ import annotations

import asyncio
import codecs
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import anyio

from wisp.tools.process import (
    ProcessResult,
    _collect_limited_output,
    _kill_process_tree_and_wait,
    _OutputBudget,
    _terminate_process_tree,
)
from wisp.tools.result import ToolError

ProcessState = Literal["running", "completed", "timed_out", "cancelled"]

DEFAULT_MAX_MANAGED_PROCESSES = 8
DEFAULT_MAX_RETAINED_BYTES = 50_000
DEFAULT_MAX_RETAINED_LINES = 2_000
POST_TERMINATION_DRAIN_TIMEOUT = 0.25


@dataclass(frozen=True, slots=True)
class ProcessUpdate:
    """One incremental snapshot of a managed process."""

    process_id: str
    state: ProcessState
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0


@dataclass
class _PendingText:
    max_bytes: int
    max_lines: int
    text: str = ""
    dropped_bytes: int = 0

    def append(self, value: str) -> None:
        if not value:
            return
        combined = f"{self.text}{value}"
        bounded, dropped = _bounded_text_tail(
            combined,
            max_bytes=self.max_bytes,
            max_lines=self.max_lines,
        )
        self.text = bounded
        self.dropped_bytes += dropped

    def drain(self) -> tuple[str, int]:
        text = self.text
        dropped_bytes = self.dropped_bytes
        self.text = ""
        self.dropped_bytes = 0
        return text, dropped_bytes


@dataclass
class _ManagedProcess:
    process_id: str
    process: asyncio.subprocess.Process
    stdout: _PendingText
    stderr: _PendingText
    state: ProcessState = "running"
    exit_code: int | None = None
    terminal_override: ProcessState | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    completion_task: asyncio.Task[None] | None = None
    terminal_reported: bool = False

    def has_pending_output(self) -> bool:
        return bool(
            self.stdout.text
            or self.stderr.text
            or self.stdout.dropped_bytes
            or self.stderr.dropped_bytes
        )


class ProcessSupervisor:
    """Own bounded shell-process handles and terminate them on shutdown."""

    def __init__(self, *, max_processes: int = DEFAULT_MAX_MANAGED_PROCESSES) -> None:
        if max_processes < 1:
            raise ValueError("max_processes must be greater than or equal to 1")
        self._max_processes = max_processes
        self._managed: dict[str, _ManagedProcess] = {}
        self._one_shot: set[asyncio.subprocess.Process] = set()
        self._closed = False
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def run_to_completion(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_output_bytes: int,
        max_output_lines: int,
    ) -> ProcessResult:
        """Run one compatibility command while tracking it for shutdown."""

        async with self._lock:
            self._ensure_open()
            self._evict_terminals_for_capacity()
            if len(self._managed) + len(self._one_shot) >= self._max_processes:
                raise ToolError(
                    f"Cannot start command: managed process limit ({self._max_processes}) reached"
                )
            process = await self._spawn(command, cwd=cwd)
            self._one_shot.add(process)

        budget = _OutputBudget(max_bytes=max_output_bytes, max_lines=max_output_lines)
        release_ownership = False
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    _collect_limited_output(process, budget),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                with anyio.CancelScope(shield=True):
                    await _kill_process_tree_and_wait(process)
                release_ownership = True
                raise ToolError(f"Command timed out after {timeout:g} seconds") from exc
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await _kill_process_tree_and_wait(process)
                release_ownership = True
                raise

            result = ProcessResult(
                exit_code=process.returncode if process.returncode is not None else -1,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                stdout_truncated=budget.exhausted,
                stderr_truncated=budget.exhausted,
            )
            release_ownership = True
            return result
        finally:
            if release_ownership:
                async with self._lock:
                    self._one_shot.discard(process)

    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        timeout: float,
        max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES,
        max_retained_lines: int = DEFAULT_MAX_RETAINED_LINES,
    ) -> str:
        """Start one resumable process and return an opaque runtime-local handle."""

        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retained_bytes < 0 or max_retained_lines < 0:
            raise ValueError("retained output limits must be non-negative")

        async with self._lock:
            self._ensure_open()
            self._evict_terminals_for_capacity()
            if len(self._managed) + len(self._one_shot) >= self._max_processes:
                raise ToolError(
                    f"Cannot start command: managed process limit ({self._max_processes}) reached"
                )
            process = await self._spawn(command, cwd=cwd)
            process_id = uuid4().hex
            managed = _ManagedProcess(
                process_id=process_id,
                process=process,
                stdout=_PendingText(max_retained_bytes, max_retained_lines),
                stderr=_PendingText(max_retained_bytes, max_retained_lines),
            )
            managed.stdout_task = asyncio.create_task(
                self._read_stream(managed, process.stdout, managed.stdout)
            )
            managed.stderr_task = asyncio.create_task(
                self._read_stream(managed, process.stderr, managed.stderr)
            )
            managed.completion_task = asyncio.create_task(
                self._wait_for_completion(managed, timeout)
            )
            self._managed[process_id] = managed
            return process_id

    async def poll(self, process_id: str, *, wait_seconds: float = 0) -> ProcessUpdate:
        """Return output not delivered by earlier polls and the current state."""

        if wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative")
        managed = await self._get(process_id)
        async with managed.operation_lock:
            if wait_seconds > 0 and managed.state == "running" and not managed.has_pending_output():
                managed.changed.clear()
                if managed.state == "running" and not managed.has_pending_output():
                    try:
                        await asyncio.wait_for(managed.changed.wait(), timeout=wait_seconds)
                    except TimeoutError:
                        pass
            return self._snapshot(managed)

    async def cancel(self, process_id: str) -> ProcessUpdate:
        """Terminate one managed process tree and return its final snapshot."""

        managed = await self._get(process_id)
        async with managed.operation_lock:
            if managed.state == "running":
                managed.terminal_override = "cancelled"
                await asyncio.shield(self._terminate_managed(managed))
            return self._snapshot(managed)

    async def aclose(self) -> None:
        """Terminate all owned process trees. Safe to call repeatedly or concurrently."""

        async with self._lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(self._close_owned_processes())
            close_task = self._close_task

        # Keep shutdown attached to this call even when its caller is cancelled.
        # Frontends call aclose() from their finalizers and may tear down the event
        # loop as soon as cancellation propagates, so merely leaving close_task
        # running in the background is not sufficient.
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                while not close_task.done():
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError:
                        # A caller may issue more than one direct asyncio
                        # cancellation while cleanup is in progress.
                        continue
                close_task.result()
            raise

    async def _close_owned_processes(self) -> None:
        async with self._lock:
            managed = tuple(self._managed.values())
            one_shot = tuple(self._one_shot)
        for item in managed:
            if item.state == "running":
                item.terminal_override = "cancelled"
        await asyncio.gather(
            *(_terminate_process_tree(item.process) for item in managed if item.state == "running"),
            *(_terminate_process_tree(process) for process in one_shot),
        )
        await asyncio.gather(
            *(self._finish_terminated_io(item) for item in managed if item.state == "running")
        )

        await asyncio.gather(
            *(item.completion_task for item in managed if item.completion_task is not None),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(process.wait() for process in one_shot),
            return_exceptions=True,
        )

        async with self._lock:
            self._managed.clear()
            self._one_shot.clear()

    async def _spawn(self, command: str, *, cwd: Path) -> asyncio.subprocess.Process:
        """Spawn while the caller holds the supervisor lock."""

        try:
            return await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                start_new_session=os.name == "posix",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolError(f"Failed to start command: {exc}") from exc

    async def _get(self, process_id: str) -> _ManagedProcess:
        async with self._lock:
            self._ensure_open()
            try:
                return self._managed[process_id]
            except KeyError as exc:
                raise ToolError(f"Unknown managed process: {process_id}") from exc

    async def _read_stream(
        self,
        managed: _ManagedProcess,
        stream: asyncio.StreamReader | None,
        output: _PendingText,
    ) -> None:
        if stream is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            output.append(decoder.decode(chunk))
            managed.changed.set()
        output.append(decoder.decode(b"", final=True))
        managed.changed.set()

    async def _wait_for_completion(self, managed: _ManagedProcess, timeout: float) -> None:
        async def wait_for_process_and_streams() -> None:
            await managed.process.wait()
            assert managed.stdout_task is not None
            assert managed.stderr_task is not None
            await asyncio.gather(
                managed.stdout_task,
                managed.stderr_task,
                return_exceptions=True,
            )

        completion = asyncio.create_task(wait_for_process_and_streams())
        try:
            try:
                await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
            except TimeoutError:
                managed.terminal_override = managed.terminal_override or "timed_out"
                await _terminate_process_tree(managed.process)
                await self._finish_terminated_io(managed)
                await completion
            managed.exit_code = managed.process.returncode
            managed.state = managed.terminal_override or "completed"
        finally:
            if not completion.done():
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
            managed.changed.set()

    async def _finish_terminated_io(self, managed: _ManagedProcess) -> None:
        """Bound pipe draining after the owned process tree has been terminated."""

        await managed.process.wait()
        stream_tasks = tuple(
            task for task in (managed.stdout_task, managed.stderr_task) if task is not None
        )
        if not stream_tasks:
            return
        _, pending = await asyncio.wait(
            stream_tasks,
            timeout=POST_TERMINATION_DRAIN_TIMEOUT,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*stream_tasks, return_exceptions=True)

    async def _terminate_managed(self, managed: _ManagedProcess) -> None:
        await _terminate_process_tree(managed.process)
        await self._finish_terminated_io(managed)
        assert managed.completion_task is not None
        await managed.completion_task

    def _snapshot(self, managed: _ManagedProcess) -> ProcessUpdate:
        stdout, stdout_dropped = managed.stdout.drain()
        stderr, stderr_dropped = managed.stderr.drain()
        if managed.state != "running":
            managed.terminal_reported = True
        return ProcessUpdate(
            process_id=managed.process_id,
            state=managed.state,
            stdout=stdout,
            stderr=stderr,
            exit_code=managed.exit_code if managed.state == "completed" else None,
            stdout_truncated=stdout_dropped > 0,
            stderr_truncated=stderr_dropped > 0,
            stdout_dropped_bytes=stdout_dropped,
            stderr_dropped_bytes=stderr_dropped,
        )

    def _evict_terminals_for_capacity(self) -> None:
        for process_id, managed in tuple(self._managed.items()):
            if managed.terminal_reported:
                del self._managed[process_id]
        managed_capacity = self._max_processes - len(self._one_shot)
        if len(self._managed) < managed_capacity:
            return
        for process_id, managed in tuple(self._managed.items()):
            if managed.state != "running":
                del self._managed[process_id]
                if len(self._managed) < managed_capacity:
                    return

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ProcessSupervisor is closed")


def _bounded_text_tail(text: str, *, max_bytes: int, max_lines: int) -> tuple[str, int]:
    encoded = text.encode("utf-8")
    if max_bytes <= 0 or max_lines <= 0:
        return "", len(encoded)

    lines = text.splitlines(keepends=True)
    bounded = "".join(lines[-max_lines:]) if len(lines) > max_lines else text
    bounded_bytes = bounded.encode("utf-8")
    if len(bounded_bytes) > max_bytes:
        bounded_bytes = bounded_bytes[-max_bytes:]
        while bounded_bytes:
            try:
                bounded = bounded_bytes.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                bounded_bytes = bounded_bytes[exc.start + 1 :]
        else:
            bounded = ""

    kept_bytes = len(bounded.encode("utf-8"))
    return bounded, len(encoded) - kept_bytes


__all__ = [
    "DEFAULT_MAX_MANAGED_PROCESSES",
    "DEFAULT_MAX_RETAINED_BYTES",
    "DEFAULT_MAX_RETAINED_LINES",
    "ProcessState",
    "ProcessSupervisor",
    "ProcessUpdate",
]
