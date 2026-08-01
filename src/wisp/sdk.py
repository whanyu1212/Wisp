"""Supported in-process embedding API for Wisp.

``InProcessWisp`` exposes the same typed command and event contract as
:class:`wisp.rpc.RpcController`, but drives the shared command host directly
instead of spawning ``wisp --mode rpc``.  It is deliberately presentation-free:
callers render events and answer approval/trust requests themselves.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any, Self, cast

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
        project_root = selected_options.project_context_root or resolve_project_context_root(
            Path.cwd()
        )
        startup_trusted = trusted_noninteractive(project_root)
        overrides = _ConfigOverrides(
            provider=provider,
            model=model,
            session_dir=session_dir,
            auth_path=auth_path,
        )
        config = overrides.build(trusted=startup_trusted, project_dir=project_root)
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
        pending_event_send: MemoryObjectSendStream[KnownWispEvent],
        pending_event_receive: MemoryObjectReceiveStream[KnownWispEvent],
        event_send: MemoryObjectSendStream[KnownWispEvent],
        event_receive: MemoryObjectReceiveStream[KnownWispEvent],
    ) -> None:
        self._host = host
        self._control_send = control_send
        self._host_control_send = host_control_send
        self._control_receive = control_receive
        self._pending_event_send = pending_event_send
        self._pending_event_receive = pending_event_receive
        self._event_send = event_send
        self._event_receive = event_receive
        self._asyncio_owner_task: asyncio.Task[None] | None = None
        self._owner_cancel_scope: anyio.CancelScope | None = None
        self._owner_started = anyio.Event()
        self._relay_finished = anyio.Event()
        self._finished = anyio.Event()
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
        pending_event_send, pending_event_receive = anyio.create_memory_object_stream[
            KnownWispEvent
        ](math.inf)

        def write_event(event: WispEvent) -> None:
            # RPC command dispatch contains synchronous paths, so it cannot await
            # the bounded frontend stream. The relay below preserves event order
            # and applies backpressure at that boundary instead of dropping a run
            # when a frontend is temporarily slower than a provider.
            pending_event_send.send_nowait(cast(KnownWispEvent, event))

        async def render_events(events: AsyncIterator[WispEvent]) -> None:
            async for event in events:
                write_event(event)

        try:
            host = await RpcHost.create(
                config,
                runtime,
                options=options,
                write_event=write_event,
                render_events=render_events,
                config_overrides=config_overrides,
            )
        except BaseException:
            await pending_event_send.aclose()
            await pending_event_receive.aclose()
            await event_send.aclose()
            await event_receive.aclose()
            raise

        control_send, control_receive = anyio.create_memory_object_stream[_RpcControlEvent](
            _CONTROL_BUFFER_CAPACITY
        )
        transport = cls(
            host=host,
            control_send=control_send,
            host_control_send=control_send.clone(),
            control_receive=control_receive,
            pending_event_send=pending_event_send,
            pending_event_receive=pending_event_receive,
            event_send=event_send,
            event_receive=event_receive,
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
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[KnownWispEvent]:
        # The transport owns this receive stream. Do not close it when a caller
        # breaks from ``async for``: the host may still need to publish its
        # cancellation/trust-completion events while ``aclose()`` drains safely.
        async for event in self._event_receive:
            yield event

    def _spawn_owner_task(self) -> None:
        """Start an owner task using the active AnyIO backend's native API."""

        backend = sniffio.current_async_library()
        if backend == "asyncio":
            self._asyncio_owner_task = asyncio.create_task(self._run_owner())
            return
        if backend == "trio":
            trio = cast(Any, import_module("trio"))
            trio.lowlevel.spawn_system_task(self._run_owner)
            return
        raise RuntimeError(f"Unsupported AnyIO backend for in-process Wisp: {backend}")

    async def _run_owner(self) -> None:
        """Own the task groups and relay for all host command operations."""

        try:
            with anyio.CancelScope() as owner_cancel_scope:
                self._owner_cancel_scope = owner_cancel_scope
                self._owner_started.set()
                try:
                    async with self._control_receive, self._host_control_send:
                        async with anyio.create_task_group() as relay_group:
                            relay_group.start_soon(self._relay_events)
                            try:
                                async with anyio.create_task_group() as task_group:
                                    await self._host.run_with_streams(
                                        self._control_receive,
                                        send=self._host_control_send,
                                        task_group=task_group,
                                    )
                                    # An explicit shutdown returns immediately
                                    # even if a command is active, matching CLI.
                                    task_group.cancel_scope.cancel()
                            finally:
                                await self._pending_event_send.aclose()
                            await self._relay_finished.wait()
                finally:
                    self._owner_cancel_scope = None
        except BaseException as exc:
            if not isinstance(exc, anyio.get_cancelled_exc_class()):
                self._run_error = exc
        finally:
            await self._pending_event_send.aclose()
            await self._event_send.aclose()
            self._finished.set()

    async def _relay_events(self) -> None:
        """Deliver every queued event to the bounded consumer stream in order."""

        try:
            async with self._pending_event_receive:
                async for event in self._pending_event_receive:
                    await self._event_send.send(event)
        finally:
            self._relay_finished.set()

    async def close(self) -> None:
        """Safely stop the owner task and release runtime-owned resources."""

        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
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
            try:
                if self._run_error is not None:
                    raise self._run_error
            finally:
                await self._control_send.aclose()
                await self._event_receive.aclose()
                await self._host.runtime.aclose()
                await self._event_send.aclose()

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
            await self._pending_event_send.aclose()
            await self._pending_event_receive.aclose()
            await self._event_send.aclose()
            await self._event_receive.aclose()


__all__ = ["InProcessOptions", "InProcessWisp"]
