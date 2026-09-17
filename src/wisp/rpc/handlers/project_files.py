"""Host-owned project discovery policy, cancellation, and bounded publication."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from threading import Event, Lock

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.events import ProjectFilesInvalidated, RpcProjectFile, RpcProjectFilesReported
from wisp.project_files import (
    MAX_PROJECT_FILES_REPORT_BYTES,
    FileIndexConfig,
    ProjectDirectory,
    ProjectScanCancelled,
    ProjectScanTimedOut,
    ProjectSnapshot,
    collect_project_snapshot,
)
from wisp.rpc.commands import GetProjectFilesCommand
from wisp.rpc.coordinator import _RpcCommandCompleted, _RpcControlEvent, _RpcRunningCommand
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.tools.context import ToolContext

type ProjectFilesPublisher = Callable[[RpcProjectFilesReported, anyio.CancelScope], Awaitable[bool]]


def project_files_report(
    snapshot: ProjectSnapshot,
    *,
    command_id: str,
    generation: int,
    max_bytes: int = MAX_PROJECT_FILES_REPORT_BYTES,
) -> RpcProjectFilesReported:
    """Retain a sorted prefix within the complete encoded event's byte budget.

    Args:
        snapshot (ProjectSnapshot): Safe metadata sorted with parents before children.
        command_id (str): The requesting command's correlation ID.
        generation (int): Policy generation captured before scanning.
        max_bytes (int): Maximum encoded report size, including a JSONL newline.

    Returns:
        RpcProjectFilesReported: Bounded metadata with truncation made explicit.

    Raises:
        ValueError: Even an empty event exceeds the supplied budget.
    """

    report = RpcProjectFilesReported(
        command_id=command_id, generation=generation, entries=(), truncated=snapshot.truncated
    )
    size = len(report.model_dump_json().encode("utf-8")) + 1
    if size > max_bytes:
        raise ValueError("Project file report budget is too small")
    entries: list[RpcProjectFile] = []
    truncated = snapshot.truncated
    for entry in snapshot.entries:
        projected = RpcProjectFile(
            path=entry.path, kind="directory" if isinstance(entry, ProjectDirectory) else "file"
        )
        additional = len(projected.model_dump_json().encode("utf-8")) + bool(entries)
        if size + additional > max_bytes:
            truncated = True
            break
        entries.append(projected)
        size += additional
    return report.model_copy(update={"entries": tuple(entries), "truncated": truncated})


class RpcProjectFiles:
    """One host's discovery policy and single physical scan admission slot."""

    def __init__(self, context: ToolContext) -> None:
        self.generation = 1
        self._context = context
        self._reserved_paths = context.protected_paths
        self._ready = anyio.Event()
        self._ready.set()
        self._request_scope: anyio.CancelScope | None = None
        # Owned by the physical worker, not the awaiting task: abandoning a
        # blocked syscall must not admit another scan and accumulate threads.
        self._worker_lock = Lock()

    def begin_policy_transition(self) -> ProjectFilesInvalidated:
        """Invalidate under the host's publication lock before configuration awaits.

        Returns:
            ProjectFilesInvalidated: Notification to publish before applying new policy.
        """

        self.generation += 1
        self._ready = anyio.Event()
        if self._request_scope is not None:
            self._request_scope.cancel()
        return ProjectFilesInvalidated(generation=self.generation)

    def reserve_protected_paths(self, paths: tuple[str, ...]) -> None:
        """Keep candidate secrets protected even if runtime adoption later fails.

        Args:
            paths (tuple[str, ...]): Backend-resolved protection patterns to retain.
        """

        self._reserved_paths = tuple(dict.fromkeys((*self._reserved_paths, *paths)))

    def settle_policy(self, context: ToolContext) -> None:
        """Resume waiters with settled policy plus every protection already learned.

        Args:
            context (ToolContext): Active policy after successful or failed adoption.
        """

        self.reserve_protected_paths(context.protected_paths)
        self._context = replace(context, protected_paths=self._reserved_paths)
        self._ready.set()

    def is_current(self, generation: int) -> bool:
        """Check again under the host's publication lock, after any output wait.

        Args:
            generation (int): Policy generation captured by the scan.

        Returns:
            bool: Whether the scan still belongs to the settled policy.
        """

        return self._ready.is_set() and generation == self.generation

    def start(
        self,
        command: GetProjectFilesCommand,
        *,
        task_group: TaskGroup,
        send: MemoryObjectSendStream[_RpcControlEvent],
        write_event: RpcEventWriter,
        publish: ProjectFilesPublisher,
    ) -> _RpcRunningCommand | None:
        """Start one cancellable auxiliary request, or report bounded busy failure.

        Args:
            command (GetProjectFilesCommand): Validated request and correlation ID.
            task_group (TaskGroup): Host group gated on initial lifecycle publication.
            send (MemoryObjectSendStream): Coordinator completion channel.
            write_event (RpcEventWriter): Started and terminal lifecycle event sink.
            publish (ProjectFilesPublisher): Host publication gate checking current policy.

        Returns:
            _RpcRunningCommand | None: Cancellable request, or None after busy rejection.
        """

        lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
        if self._request_scope is not None or self._worker_lock.locked():
            lifecycle.fail("Project file discovery is busy; retry after the active scan stops")
            return None
        scope = anyio.CancelScope()
        self._request_scope = scope
        task_group.start_soon(self._run, lifecycle, scope, send.clone(), publish)
        return _RpcRunningCommand(
            command_id=lifecycle.command_id, command_type="get_project_files", cancel_scope=scope
        )

    def _collect(
        self, config: FileIndexConfig, cancelled: Event, command_id: str, generation: int
    ) -> RpcProjectFilesReported:
        if cancelled.is_set():
            raise ProjectScanCancelled
        # A cancelled task can leave the thread pool before its callable starts.
        # Late starters observe cancellation without taking an admission slot.
        if not self._worker_lock.acquire(blocking=False):
            raise ProjectScanCancelled
        try:
            snapshot = collect_project_snapshot(config, cancelled=cancelled)
            report = project_files_report(snapshot, command_id=command_id, generation=generation)
            if cancelled.is_set():
                raise ProjectScanCancelled
            return report
        finally:
            self._worker_lock.release()

    async def _run(
        self,
        lifecycle: RpcCommandLifecycle,
        scope: anyio.CancelScope,
        send: MemoryObjectSendStream[_RpcControlEvent],
        publish: ProjectFilesPublisher,
    ) -> None:
        cancelled = Event()
        ok = False
        error: str | None = None
        try:
            with scope:
                # Requests arriving during a trust transition wait for its outcome
                # rather than requiring the client to infer readiness from UI events.
                await self._ready.wait()
                generation = self.generation
                config = FileIndexConfig(root=self._context.cwd, context=self._context)
                with anyio.fail_after(config.timeout_seconds):
                    report = await anyio.to_thread.run_sync(
                        self._collect,
                        config,
                        cancelled,
                        lifecycle.command_id,
                        generation,
                        abandon_on_cancel=True,
                    )
                ok = await publish(report, scope)
        except (ProjectScanTimedOut, TimeoutError):
            error = "Project file discovery timed out; retry to refresh"
        except ProjectScanCancelled:
            error = "Project file discovery cancelled"
        except anyio.get_cancelled_exc_class():
            error = "Project file discovery cancelled"
        except Exception:
            # Neither protected names nor exceptions containing absolute paths
            # belong in the wire, terminal error text, or debug output.
            error = "Project file discovery unavailable"
        finally:
            cancelled.set()
            self._request_scope = None
            if not ok and error is None:
                error = "Project file discovery cancelled"
            lifecycle.finish(ok=ok, error=error)
            with anyio.CancelScope(shield=True):
                async with send:
                    await send.send(
                        _RpcCommandCompleted(
                            command_id=lifecycle.command_id,
                            command_type="get_project_files",
                            ok=ok,
                            history=None,
                            entry_count=0,
                        )
                    )
