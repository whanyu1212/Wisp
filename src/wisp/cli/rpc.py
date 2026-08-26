"""JSONL stdin/stdout adapter for Wisp's shared RPC command host."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import cast

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from wisp import __version__
from wisp.config import WispConfig
from wisp.events import WispEvent
from wisp.rpc.configuration import _ConfigOverrides
from wisp.rpc.coordinator import _RpcControlEvent, _RpcInputClosed, _RpcInputCommand
from wisp.rpc.framing import encode_rpc_frame
from wisp.rpc.host import InProcessOptions, RpcHost, build_runtime_for_config
from wisp.rpc.protocol import (
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    RpcHandshakeResponse,
    RpcTransportLimits,
)
from wisp.runtime.api import WispRuntime

from . import output as _cli_output
from . import rpc_transport as _rpc_transport

_RPC_CAPABILITIES: tuple[str, ...] = ()
_RPC_TRANSPORT_LIMITS = RpcTransportLimits(
    max_client_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES,
    max_server_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES,
)
# Commands may contain a full negotiated frame. Keep only one parsed control
# event ahead of the coordinator so the per-frame limit also bounds aggregate
# transport memory under downstream backpressure.
_RPC_CONTROL_STREAM_BUFFER_SIZE = 1


def _write_json_event(event: WispEvent) -> None:
    encode_rpc_frame(event, max_frame_bytes=_RPC_TRANSPORT_LIMITS.max_server_frame_bytes)
    _cli_output._write_json_event(event)


async def _render_json_events(events: AsyncIterator[WispEvent]) -> None:
    async def bounded_events() -> AsyncIterator[WispEvent]:
        async for event in events:
            encode_rpc_frame(event, max_frame_bytes=_RPC_TRANSPORT_LIMITS.max_server_frame_bytes)
            yield event

    await _cli_output._render_json_events(bounded_events())


async def _run_rpc(
    config: WispConfig,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    startup_trusted: bool = False,
    config_overrides: _ConfigOverrides | None = None,
    project_context_root: Path | None = None,
    *,
    handshake_complete: bool = False,
) -> None:
    if not handshake_complete and not await _negotiate_rpc_connection():
        return
    runtime = await build_runtime_for_config(config)
    try:
        await _run_rpc_with_runtime(
            config,
            runtime,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
            resume=resume,
            continue_latest=continue_latest,
            approve_unsafe_tools=approve_unsafe_tools,
            max_tool_iterations=max_tool_iterations,
            startup_trusted=startup_trusted,
            config_overrides=config_overrides,
            project_context_root=project_context_root,
        )
    finally:
        await runtime.aclose()


async def _negotiate_rpc_connection() -> bool:
    handshake = await _rpc_transport.read_rpc_stdin_handshake(
        _rpc_binary_stdin(),
        backend_package_version=__version__,
        supported_capabilities=_RPC_CAPABILITIES,
        limits=_RPC_TRANSPORT_LIMITS,
        write_response=_write_rpc_handshake,
    )
    return handshake is not None


async def _run_rpc_with_runtime(
    config: WispConfig,
    runtime: WispRuntime,
    *,
    all_tools: bool = False,
    allow_read_tools: bool = False,
    allowed_tools: tuple[str, ...] = (),
    resume: str | None = None,
    continue_latest: bool = False,
    approve_unsafe_tools: bool = False,
    max_tool_iterations: int | None = None,
    startup_trusted: bool = False,
    config_overrides: _ConfigOverrides | None = None,
    project_context_root: Path | None = None,
) -> None:
    """Run the CLI's stdin/stdout adapter over the shared RPC host."""

    host = await RpcHost.create(
        config,
        runtime,
        options=InProcessOptions(
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
            resume=resume,
            continue_latest=continue_latest,
            approve_unsafe_tools=approve_unsafe_tools,
            max_tool_iterations=max_tool_iterations,
            startup_trusted=startup_trusted,
            project_context_root=project_context_root,
            cwd=Path.cwd(),
        ),
        write_event=_write_json_event,
        render_events=_render_json_events,
        config_overrides=config_overrides,
        runtime_builder=build_runtime_for_config,
    )
    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](
        _RPC_CONTROL_STREAM_BUFFER_SIZE
    )
    stop_reader = anyio.Event()
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_read_rpc_stdin, send.clone(), stop_reader)
        async with send, receive:
            await host.run_with_streams(receive, send=send, task_group=task_group)
        stop_reader.set()
        task_group.cancel_scope.cancel()


async def _read_rpc_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    await _rpc_stdin_transport().read(send, stop_reader)


def _rpc_stdin_transport() -> _rpc_transport.RpcStdinTransport[_RpcControlEvent]:
    return _rpc_transport.RpcStdinTransport(
        stdin=_rpc_binary_stdin(),
        write_event=_write_json_event,
        input_command_factory=_RpcInputCommand,
        input_closed_factory=_RpcInputClosed,
        queue_factory=lambda maxsize: Queue(maxsize=maxsize),
        thread_factory=Thread,
        wait_readable=anyio.wait_readable,
        read_fd=os.read,
        needs_thread_reader=lambda _stdin_mode: True,
        max_frame_bytes=_RPC_TRANSPORT_LIMITS.max_client_frame_bytes,
    )


def _rpc_binary_stdin() -> _rpc_transport.RpcTextInput:
    return cast(_rpc_transport.RpcTextInput, getattr(sys.stdin, "buffer", sys.stdin))


def _write_rpc_handshake(response: RpcHandshakeResponse) -> None:
    frame = encode_rpc_frame(response, max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES)
    sys.stdout.write(frame.decode("utf-8"))
    sys.stdout.flush()
