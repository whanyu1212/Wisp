"""Supported in-process embedding API for Wisp.

``InProcessWisp`` exposes the same typed command and event contract as
:class:`wisp.rpc.RpcController`, but drives the shared command host directly
instead of spawning ``wisp --mode rpc``.  It is deliberately presentation-free:
callers render events and answer approval/trust requests themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Self, cast

import anyio
import sniffio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from wisp.agent.prompt import resolve_project_context_root
from wisp.config import WispConfig
from wisp.events import KnownWispEvent, WispEvent
from wisp.rpc.client import RpcController, RpcTransport
from wisp.rpc.commands import RpcCommand
from wisp.rpc.configuration import _ConfigOverrides
from wisp.rpc.coordinator import _RpcControlEvent, _RpcInputClosed, _RpcInputCommand
from wisp.rpc.host import InProcessOptions, RpcHost, build_runtime_for_config
from wisp.runtime.api import WispRuntime
from wisp.trust import trusted_noninteractive

_EVENT_BUFFER_CAPACITY = 1_024
_CONTROL_BUFFER_CAPACITY = 100
_SYNC_EVENT_RESERVE = _CONTROL_BUFFER_CAPACITY * 3 + 16
_STREAM_EVENT_BUFFER_CAPACITY = _EVENT_BUFFER_CAPACITY - _SYNC_EVENT_RESERVE
_CLOSE_TIMEOUT_SECONDS = 2


class InProcessWisp(RpcController):
    """Async, in-process controller sharing the RPC command/event contract.

    Start it with an explicit :class:`~wisp.config.WispConfig` or use
    :meth:`from_environment` for the same safe startup trust behavior as the
    standalone RPC frontend.  Consume :meth:`events` from exactly one task and
    call :meth:`aclose` when finished.
    """

    def __init__(self, transport: _InProcessTransport) -> None:
        super().__init__(transport)
        self._in_process_transport = transport

    @classmethod
    async def start(
        cls,
        config: WispConfig,
        *,
        options: InProcessOptions | None = None,
    ) -> Self:
        """Start Wisp from caller-supplied configuration.

        Explicit configuration does not load a project settings layer on its own.
        Set ``options.startup_trusted`` only when the caller has already made a
        safe trust decision, or use :meth:`from_environment`.
        """

        return await cls._start(config, options=options or InProcessOptions())

    @classmethod
    async def from_environment(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        session_dir: Path | None = None,
        auth_path: Path | None = None,
        options: InProcessOptions | None = None,
    ) -> Self:
        """Start from environment/settings with the RPC trust boundary intact.

        Project settings are initially applied only when an environment override
        or an existing global trust decision allows them.  If trust is undecided,
        the first prompt emits ``trust.requested``; approving it rebuilds the
        trusted project configuration before that prompt starts.
        """

        selected_options = options or InProcessOptions()
        project_root = selected_options.project_context_root
        if project_root is None:
            project_root = await anyio.to_thread.run_sync(resolve_project_context_root, Path.cwd())
        startup_trusted = await anyio.to_thread.run_sync(trusted_noninteractive, project_root)
        overrides = _ConfigOverrides(
            provider=provider,
            model=model,
            session_dir=session_dir,
            auth_path=auth_path,
        )
        config = await anyio.to_thread.run_sync(
            partial(overrides.build, trusted=startup_trusted, project_dir=project_root)
        )
        return await cls._start(
            config,
            options=replace(
                selected_options,
                startup_trusted=startup_trusted,
                project_context_root=project_root,
            ),
            config_overrides=overrides,
        )

    @classmethod
    async def _start(
        cls,
        config: WispConfig,
        *,
        options: InProcessOptions,
        config_overrides: _ConfigOverrides | None = None,
    ) -> Self:
        _require_asyncio_backend()
        runtime = await build_runtime_for_config(config)
        try:
            transport = await _InProcessTransport.start(
                config,
                runtime,
                options=options,
                config_overrides=config_overrides,
            )
        except BaseException:
            await runtime.aclose()
            raise
        return cls(transport)

    async def aclose(self) -> None:
        """Stop command processing and release runtime-owned resources."""

        await self.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()


def _require_asyncio_backend() -> None:
    """Reject unsupported backends before constructing runtime resources."""

    backend = sniffio.current_async_library()
    if backend != "asyncio":
        raise RuntimeError(
            "In-process Wisp currently requires AnyIO's asyncio backend because built-in "
            "process tools use asyncio subprocesses; use JSONL RPC from other backends"
        )


class _BoundedEventOutput:
    """Ordered bounded event output with backpressure for streamed events."""

    def __init__(
        self,
        send: MemoryObjectSendStream[KnownWispEvent],
        receive: MemoryObjectReceiveStream[KnownWispEvent],
    ) -> None:
        self._send = send
        self._receive = receive
        self._drained = anyio.Event()

    def write_event(self, event: WispEvent) -> None:
        """Write one bounded synchronous command event.

        The SDK reserves output slots for synchronous command responses while
        streamed provider events use :meth:`render_events` to await capacity.
        """

        try:
            self._send.send_nowait(cast(KnownWispEvent, event))
        except anyio.WouldBlock as exc:  # pragma: no cover - guarded by reservation
            raise RuntimeError("In-process event output reservation was exhausted") from exc

    async def render_events(self, events: AsyncIterator[WispEvent]) -> None:
        """Deliver streamed events with real frontend backpressure."""

        async for event in events:
            await self._wait_for_stream_capacity()
            await self._send.send(cast(KnownWispEvent, event))

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield the event stream and notify blocked producers after each read."""

        return self._iter_events()

    async def aclose_send(self) -> None:
        """Close producer output after the owner stops."""

        await self._send.aclose()

    async def aclose_receive(self) -> None:
        """Close the consumer side during controller cleanup."""

        await self._receive.aclose()

    async def _wait_for_stream_capacity(self) -> None:
        while self._buffered_event_count() >= _STREAM_EVENT_BUFFER_CAPACITY:
            drained = self._drained
            await drained.wait()

    def _buffered_event_count(self) -> int:
        return self._send.statistics().current_buffer_used

    async def _iter_events(self) -> AsyncIterator[KnownWispEvent]:
        async for event in self._receive:
            # Replace the notification before yielding so a producer that wakes
            # immediately observes the current buffer size before sending again.
            self._drained.set()
            self._drained = anyio.Event()
            yield event


class _InProcessTransport(RpcTransport):
    """Memory transport that adapts the shared host to ``RpcController``.

    A backend-native owner task exclusively enters and exits the host's AnyIO
    task group. Public methods communicate with it through streams, so cleanup
    is safe from other tasks and nested cancel scopes.
    """

    def __init__(
        self,
        *,
        host: RpcHost,
        control_send: MemoryObjectSendStream[_RpcControlEvent],
        host_control_send: MemoryObjectSendStream[_RpcControlEvent],
        control_receive: MemoryObjectReceiveStream[_RpcControlEvent],
        event_output: _BoundedEventOutput,
    ) -> None:
        self._host = host
        self._control_send = control_send
        self._host_control_send = host_control_send
        self._control_receive = control_receive
        self._event_output = event_output
        self._asyncio_owner_task: asyncio.Task[None] | None = None
        self._owner_cancel_scope: anyio.CancelScope | None = None
        self._owner_started = anyio.Event()
        self._finished = anyio.Event()
        self._close_finished = anyio.Event()
        self._closed = False
        self._events_claimed = False
        self._run_error: BaseException | None = None

    @classmethod
    async def start(
        cls,
        config: WispConfig,
        runtime: WispRuntime,
        *,
        options: InProcessOptions,
        config_overrides: _ConfigOverrides | None,
    ) -> _InProcessTransport:
        event_send, event_receive = anyio.create_memory_object_stream[KnownWispEvent](
            _EVENT_BUFFER_CAPACITY
        )
        event_output = _BoundedEventOutput(event_send, event_receive)

        try:
            host = await RpcHost.create(
                config,
                runtime,
                options=options,
                write_event=event_output.write_event,
                render_events=event_output.render_events,
                config_overrides=config_overrides,
            )
        except BaseException:
            await event_output.aclose_send()
            await event_output.aclose_receive()
            raise

        control_send, control_receive = anyio.create_memory_object_stream[_RpcControlEvent](
            _CONTROL_BUFFER_CAPACITY
        )
        transport = cls(
            host=host,
            control_send=control_send,
            host_control_send=control_send.clone(),
            control_receive=control_receive,
            event_output=event_output,
        )
        try:
            transport._spawn_owner_task()
            await transport._owner_started.wait()
        except BaseException:
            await transport._abort_start()
            raise
        return transport

    async def send(self, command: RpcCommand) -> None:
        """Submit one already-validated typed command to the shared host."""

        if self._closed or self._finished.is_set():
            raise RuntimeError("In-process Wisp controller is closed")
        raw_command = cast(dict[str, object], command.model_dump(exclude_none=True))
        await self._control_send.send(_RpcInputCommand(command=raw_command))

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield the one ordered event stream produced by the host."""

        if self._events_claimed:
            raise RuntimeError("In-process Wisp events may only be consumed once")
        self._events_claimed = True
        return self._event_output.events()

    def _spawn_owner_task(self) -> None:
        """Start an owner task using the active AnyIO backend's native API."""

        backend = sniffio.current_async_library()
        if backend == "asyncio":
            self._asyncio_owner_task = asyncio.create_task(self._run_owner())
            return
        raise AssertionError(f"Unsupported in-process Wisp backend: {backend}")

    async def _run_owner(self) -> None:
        """Own the task group for all host command operations."""

        try:
            with anyio.CancelScope() as owner_cancel_scope:
                self._owner_cancel_scope = owner_cancel_scope
                self._owner_started.set()
                try:
                    async with self._control_receive, self._host_control_send:
                        async with anyio.create_task_group() as task_group:
                            await self._host.run_with_streams(
                                self._control_receive,
                                send=self._host_control_send,
                                task_group=task_group,
                            )
                            # An explicit shutdown returns immediately even if a
                            # command is active, matching the CLI adapter.
                            task_group.cancel_scope.cancel()
                finally:
                    self._owner_cancel_scope = None
        except BaseException as exc:
            if not isinstance(exc, anyio.get_cancelled_exc_class()):
                self._run_error = exc
        finally:
            await self._event_output.aclose_send()
            self._finished.set()

    async def close(self) -> None:
        """Safely stop the owner task and release runtime-owned resources."""

        if self._closed:
            with anyio.CancelScope(shield=True):
                await self._close_finished.wait()
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            try:
                if not self._finished.is_set():
                    with anyio.move_on_after(_CLOSE_TIMEOUT_SECONDS):
                        try:
                            await self._control_send.send(_RpcInputClosed())
                        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                            pass
                    with anyio.move_on_after(_CLOSE_TIMEOUT_SECONDS) as scope:
                        await self._finished.wait()
                    if scope.cancel_called:
                        owner_cancel_scope = self._owner_cancel_scope
                        if owner_cancel_scope is not None:
                            owner_cancel_scope.cancel()
                    await self._finished.wait()
                if self._run_error is not None:
                    raise self._run_error
            finally:
                try:
                    await self._control_send.aclose()
                    await self._event_output.aclose_receive()
                    await self._host.runtime.aclose()
                    await self._event_output.aclose_send()
                finally:
                    self._close_finished.set()

    async def _abort_start(self) -> None:
        """Stop a partially started owner before surfacing a startup failure."""

        with anyio.CancelScope(shield=True):
            owner_cancel_scope = self._owner_cancel_scope
            if owner_cancel_scope is not None:
                owner_cancel_scope.cancel()
                await self._finished.wait()
            await self._control_send.aclose()
            await self._host_control_send.aclose()
            await self._control_receive.aclose()
            await self._event_output.aclose_send()
            await self._event_output.aclose_receive()


__all__ = ["InProcessOptions", "InProcessWisp"]
