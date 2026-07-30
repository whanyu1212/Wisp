"""Bounded lifecycle management for shell processes."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar
from uuid import uuid4

import anyio

from wisp.tools.process import (
    ProcessResult,
    _collect_limited_output,
    _create_shell_process,
    _kill_process_tree_and_wait,
    _OutputBudget,
    _terminate_process_tree,
)
from wisp.tools.result import ToolError

ProcessState = Literal["running", "completed", "failed", "timed_out", "cancelled"]
_T = TypeVar("_T")

DEFAULT_MAX_MANAGED_PROCESSES = 8
DEFAULT_MAX_RETAINED_BYTES = 50_000
DEFAULT_MAX_RETAINED_LINES = 2_000
POST_TERMINATION_DRAIN_TIMEOUT = 0.25
PROCESS_TREE_CLEANUP_ERROR = "Failed to terminate process tree"


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
    error: str | None = None


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
    error: str | None = None
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
        self._one_shot: dict[
            asyncio.subprocess.Process,
            asyncio.Task[tuple[bytes, bytes]],
        ] = {}
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
            budget = _OutputBudget(max_bytes=max_output_bytes, max_lines=max_output_lines)
            process = await self._spawn(command, cwd=cwd)
            capture_task = asyncio.create_task(_collect_limited_output(process, budget))
            self._one_shot[process] = capture_task

        release_ownership = False
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    capture_task,
                    timeout=timeout,
                )
            except TimeoutError as exc:
                cleanup_task = asyncio.create_task(_kill_process_tree_and_wait(process))
                try:
                    cleanup_succeeded = await self._await_task_before_propagating_cancellation(
                        cleanup_task
                    )
                except asyncio.CancelledError:
                    release_ownership = bool(
                        cleanup_task.done()
                        and not cleanup_task.cancelled()
                        and cleanup_task.result()
                    )
                    raise
                release_ownership = cleanup_succeeded
                raise ToolError(f"Command timed out after {timeout:g} seconds") from exc
            except BaseException:
                cleanup_task = asyncio.create_task(_kill_process_tree_and_wait(process))
                try:
                    cleanup_succeeded = await self._await_task_before_propagating_cancellation(
                        cleanup_task
                    )
                except asyncio.CancelledError:
                    release_ownership = bool(
                        cleanup_task.done()
                        and not cleanup_task.cancelled()
                        and cleanup_task.result()
                    )
                    raise
                release_ownership = cleanup_succeeded
                raise

            cleanup_task = asyncio.create_task(_terminate_process_tree(process))
            try:
                cleanup_succeeded = await self._await_task_before_propagating_cancellation(
                    cleanup_task
                )
            except asyncio.CancelledError:
                release_ownership = bool(
                    cleanup_task.done() and not cleanup_task.cancelled() and cleanup_task.result()
                )
                raise
            if not cleanup_succeeded:
                raise ToolError("Failed to terminate process tree")
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
                release_task = asyncio.create_task(self._release_one_shot(process))
                await self._await_task_before_propagating_cancellation(release_task)

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
        if wait_seconds > 0:
            should_wait = False
            async with managed.operation_lock:
                if managed.state == "running" and not managed.has_pending_output():
                    managed.changed.clear()
                    should_wait = managed.state == "running" and not managed.has_pending_output()
            if should_wait:
                try:
                    await asyncio.wait_for(managed.changed.wait(), timeout=wait_seconds)
                except TimeoutError:
                    pass
        async with managed.operation_lock:
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

        # Keep shutdown attached to this call even when its caller is cancelled.
        # Frontends call aclose() from their finalizers and may tear down the event
        # loop as soon as cancellation propagates, so merely leaving close_task
        # running in the background is not sufficient.
        init_task = asyncio.create_task(self._initialize_close_task())
        try:
            close_task = await asyncio.shield(init_task)
        except asyncio.CancelledError:
            close_task = await self._await_task_after_cancellation(init_task)
            await self._await_task_after_cancellation(close_task)
            raise

        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await self._await_task_after_cancellation(close_task)
            raise

    async def _initialize_close_task(self) -> asyncio.Task[None]:
        async with self._lock:
            close_failed = (
                self._close_task is not None
                and self._close_task.done()
                and not self._close_task.cancelled()
                and self._close_task.exception() is not None
            )
            if self._close_task is None or close_failed:
                self._closed = True
                self._close_task = asyncio.create_task(self._close_owned_processes())
            assert self._close_task is not None
            return self._close_task

    async def _release_one_shot(self, process: asyncio.subprocess.Process) -> None:
        async with self._lock:
            self._one_shot.pop(process, None)

    async def _await_task_before_propagating_cancellation(self, task: asyncio.Task[_T]) -> _T:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._await_task_after_cancellation(task)
            raise

    async def _await_task_after_cancellation(self, task: asyncio.Task[_T]) -> _T:
        while True:
            if task.done():
                return task.result()
            try:
                with anyio.CancelScope(shield=True):
                    result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A caller may issue more than one direct asyncio cancellation
                # while cleanup is in progress.
                continue
            return result

    async def _close_owned_processes(self) -> None:
        async with self._lock:
            managed = tuple(self._managed.values())
            one_shot = tuple(self._one_shot.items())
        managed_to_cleanup = tuple(
            item
            for item in managed
            if item.state == "running" or item.error == PROCESS_TREE_CLEANUP_ERROR
        )
        for item in managed_to_cleanup:
            if item.state == "running":
                item.terminal_override = "cancelled"
        managed_results = await asyncio.gather(
            *(_terminate_process_tree(item.process) for item in managed_to_cleanup),
            return_exceptions=True,
        )
        one_shot_results = await asyncio.gather(
            *(_terminate_process_tree(process) for process, _capture_task in one_shot),
            return_exceptions=True,
        )
        failed_managed_ids = {
            item.process_id
            for item, result in zip(managed_to_cleanup, managed_results, strict=True)
            if result is not True
        }
        failed_one_shot_processes = {
            process
            for (process, _capture_task), result in zip(one_shot, one_shot_results, strict=True)
            if result is not True
        }
        await asyncio.gather(
            *(
                self._finish_terminated_io(item)
                for item in managed_to_cleanup
                if item.process_id not in failed_managed_ids
            )
        )
        await asyncio.gather(
            *(
                self._finish_one_shot_capture(process, capture_task)
                for process, capture_task in one_shot
                if process not in failed_one_shot_processes
            )
        )

        await asyncio.gather(
            *(
                item.completion_task
                for item in managed
                if item.completion_task is not None and item.process_id not in failed_managed_ids
            ),
            return_exceptions=True,
        )

        async with self._lock:
            if failed_managed_ids or failed_one_shot_processes:
                self._managed = {
                    process_id: item
                    for process_id, item in self._managed.items()
                    if process_id in failed_managed_ids
                }
                self._one_shot = {
                    process: capture_task
                    for process, capture_task in self._one_shot.items()
                    if process in failed_one_shot_processes
                }
            else:
                self._managed.clear()
                self._one_shot.clear()

        if failed_managed_ids or failed_one_shot_processes:
            for item in managed_to_cleanup:
                if item.process_id in failed_managed_ids:
                    item.error = PROCESS_TREE_CLEANUP_ERROR
                    item.state = "failed"
                    item.changed.set()
            raise ToolError(PROCESS_TREE_CLEANUP_ERROR)

    async def _spawn(self, command: str, *, cwd: Path) -> asyncio.subprocess.Process:
        """Spawn while the caller holds the supervisor lock."""

        return await _create_shell_process(command, cwd=cwd)

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
        assert managed.stdout_task is not None
        assert managed.stderr_task is not None
        stream_tasks = (managed.stdout_task, managed.stderr_task)

        def first_reader_failure(results: Iterable[object]) -> BaseException | None:
            return next(
                (
                    result
                    for result in results
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ),
                None,
            )

        def reader_task_failure(task: asyncio.Task[None]) -> BaseException | None:
            if task.cancelled():
                return None
            failure = task.exception()
            if failure is not None and not isinstance(failure, asyncio.CancelledError):
                return failure
            return None

        async def wait_for_reader_failure() -> BaseException | None:
            pending = set(stream_tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                failure = first_reader_failure(reader_task_failure(task) for task in done)
                if failure is not None:
                    return failure
            return None

        async def wait_for_process_and_streams() -> BaseException | None:
            await managed.process.wait()
            results = await asyncio.gather(*stream_tasks, return_exceptions=True)
            return first_reader_failure(results)

        async def wait_for_process_completion_or_reader_failure() -> BaseException | None:
            process_and_streams = asyncio.create_task(wait_for_process_and_streams())
            reader_failure = asyncio.create_task(wait_for_reader_failure())
            try:
                pending_tasks = {process_and_streams, reader_failure}
                while True:
                    done, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if reader_failure in done:
                        failure = reader_failure.result()
                        if failure is not None:
                            return failure
                    if process_and_streams in done:
                        return process_and_streams.result()
            finally:
                for task in (process_and_streams, reader_failure):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(process_and_streams, reader_failure, return_exceptions=True)

        completion = asyncio.create_task(wait_for_process_completion_or_reader_failure())
        reader_failure: BaseException | None = None
        cleanup_succeeded = True
        try:
            try:
                reader_failure = await asyncio.wait_for(
                    asyncio.shield(completion),
                    timeout=timeout,
                )
            except TimeoutError:
                managed.terminal_override = managed.terminal_override or "timed_out"
                cleanup_succeeded = await _terminate_process_tree(managed.process)
                await self._finish_terminated_io(managed)
                reader_failure = await completion
            if reader_failure is not None:
                cleanup_succeeded = await _terminate_process_tree(managed.process)
                await self._finish_terminated_io(managed)
            # A shell can exit after launching descendants whose output is
            # redirected away from its pipes. The leader and both readers are
            # then finished even though the owned process group is not. Reap
            # that remaining group before publishing a terminal handle.
            else:
                cleanup_succeeded = await _terminate_process_tree(managed.process)
            managed.exit_code = managed.process.returncode
            if not cleanup_succeeded:
                managed.error = PROCESS_TREE_CLEANUP_ERROR
                managed.state = "failed"
            elif managed.terminal_override is not None:
                managed.state = managed.terminal_override
            elif reader_failure is not None:
                managed.error = "Failed to read process output"
                managed.state = "failed"
            else:
                managed.state = "completed"
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

    async def _finish_one_shot_capture(
        self,
        process: asyncio.subprocess.Process,
        capture_task: asyncio.Task[tuple[bytes, bytes]],
    ) -> None:
        """Bound one-shot pipe draining after the owned process tree has been terminated."""

        await process.wait()
        if not capture_task.done():
            _, pending = await asyncio.wait(
                (capture_task,),
                timeout=POST_TERMINATION_DRAIN_TIMEOUT,
            )
            for task in pending:
                task.cancel()
        await asyncio.gather(capture_task, return_exceptions=True)

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
            error=managed.error,
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
