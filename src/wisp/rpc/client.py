"""Client/controller abstractions for Wisp JSONL RPC."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import anyio
from anyio.abc import Process
from pydantic import ValidationError

from wisp import __version__
from wisp.agent.mode import AgentMode
from wisp.events import (
    EVENT_SCHEMA_VERSION,
    KnownWispEvent,
    QueueKind,
    QueueMode,
    wisp_event_from_json,
)
from wisp.rpc.commands import (
    ApprovalCommand,
    ApprovalScope,
    CancelCommand,
    ClearQueueCommand,
    CloneSessionCommand,
    CompactCommand,
    ConfigureCommand,
    FollowUpCommand,
    ForkSessionCommand,
    GetCommandsCommand,
    GetMcpStatusCommand,
    GetMessagesCommand,
    GetQueueStateCommand,
    GetSessionsCommand,
    GetSessionStatsCommand,
    GetSessionTreeCommand,
    GetSkillsCommand,
    GetStateCommand,
    InitCommand,
    NavigateSessionTreeCommand,
    NewSessionCommand,
    PopQueueCommand,
    PromptCommand,
    RpcCommand,
    SelectSessionCommand,
    SetQueueModeCommand,
    SetSessionNameCommand,
    ShutdownCommand,
    SteerCommand,
    TrustCommand,
    UnrevertSessionTreeCommand,
)
from wisp.rpc.framing import (
    RpcFrameError,
    decode_rpc_object,
    encode_rpc_frame,
    pop_rpc_frame,
    require_complete_rpc_stream,
)
from wisp.rpc.protocol import (
    LIVE_RPC_PROTOCOL_VERSION,
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    RpcHandshakeAccepted,
    RpcHandshakeRejected,
    RpcHandshakeRequest,
    RpcHandshakeResponseAdapter,
    RpcTransportLimits,
    validate_rpc_handshake_response,
)


class RpcTransport(Protocol):
    """Transport used by `RpcController` to send commands and receive events."""

    async def send(self, command: RpcCommand) -> None:
        """Send one typed RPC command."""

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield typed Wisp events from the RPC event stream."""

    async def close(self) -> None:
        """Close the underlying transport."""


class RpcController:
    """High-level controller API for Wisp RPC commands."""

    def __init__(
        self,
        transport: RpcTransport,
        *,
        command_id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._transport = transport
        self._command_id_factory = command_id_factory or _default_command_id

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
        """Send a prompt command and return its command id."""

        selected_id = command_id or self._command_id_factory("prompt")
        await self._transport.send(PromptCommand(id=selected_id, prompt=prompt))
        return selected_id

    async def init(self, *, command_id: str | None = None) -> str:
        """Initialize project guidance and return the command id."""

        selected_id = command_id or self._command_id_factory("init")
        await self._transport.send(InitCommand(id=selected_id))
        return selected_id

    async def compact(
        self,
        instructions: str | None = None,
        *,
        command_id: str | None = None,
    ) -> str:
        """Compact the active session and return the command id."""

        selected_id = command_id or self._command_id_factory("compact")
        await self._transport.send(CompactCommand(id=selected_id, instructions=instructions))
        return selected_id

    async def get_session_stats(self, *, command_id: str | None = None) -> str:
        """Request a consistent session statistics snapshot."""

        selected_id = command_id or self._command_id_factory("stats")
        await self._transport.send(GetSessionStatsCommand(id=selected_id))
        return selected_id

    async def get_state(self, *, command_id: str | None = None) -> str:
        """Request an immediate in-memory RPC state snapshot."""

        selected_id = command_id or self._command_id_factory("state")
        await self._transport.send(GetStateCommand(id=selected_id))
        return selected_id

    async def get_commands(self, *, command_id: str | None = None) -> str:
        """Request an immediate in-memory command registry snapshot."""

        selected_id = command_id or self._command_id_factory("commands")
        await self._transport.send(GetCommandsCommand(id=selected_id))
        return selected_id

    async def get_skills(self, *, command_id: str | None = None) -> str:
        """Request the active immutable skill catalog snapshot."""

        selected_id = command_id or self._command_id_factory("skills")
        await self._transport.send(GetSkillsCommand(id=selected_id))
        return selected_id

    async def get_mcp_status(self, *, command_id: str | None = None) -> str:
        """Request sanitized MCP server and registered-tool status."""

        selected_id = command_id or self._command_id_factory("mcp")
        await self._transport.send(GetMcpStatusCommand(id=selected_id))
        return selected_id

    async def get_messages(
        self,
        *,
        session_id: str | None = None,
        limit: int = 200,
        before_entry_id: str | None = None,
        after_entry_id: str | None = None,
        entry_ids: tuple[str, ...] = (),
        complete_structure: bool = False,
        full_content: bool = False,
        allow_during_prompt: bool = False,
        command_id: str | None = None,
    ) -> str:
        """Request a bounded persisted transcript page."""

        selected_id = command_id or self._command_id_factory("messages")
        await self._transport.send(
            GetMessagesCommand(
                id=selected_id,
                session_id=session_id,
                limit=limit,
                before_entry_id=before_entry_id,
                after_entry_id=after_entry_id,
                entry_ids=entry_ids or None,
                complete_structure=True if complete_structure else None,
                full_content=True if full_content else None,
                allow_during_prompt=True if allow_during_prompt else None,
            )
        )
        return selected_id

    async def get_sessions(self, *, limit: int = 50, command_id: str | None = None) -> str:
        """Request a bounded persisted session catalog."""

        selected_id = command_id or self._command_id_factory("sessions")
        await self._transport.send(GetSessionsCommand(id=selected_id, limit=limit))
        return selected_id

    async def new_session(self, *, command_id: str | None = None) -> str:
        """Deselect the active session so the next prompt starts a fresh one."""

        selected_id = command_id or self._command_id_factory("new-session")
        await self._transport.send(NewSessionCommand(id=selected_id))
        return selected_id

    async def select_session(self, session_id: str, *, command_id: str | None = None) -> str:
        """Select a persisted session as the active RPC session."""

        selected_id = command_id or self._command_id_factory("select-session")
        await self._transport.send(SelectSessionCommand(id=selected_id, session_id=session_id))
        return selected_id

    async def clone_session(self, *, command_id: str | None = None) -> str:
        """Clone the selected session and select the clone."""

        selected_id = command_id or self._command_id_factory("clone-session")
        await self._transport.send(CloneSessionCommand(id=selected_id))
        return selected_id

    async def fork_session(self, entry_id: str, *, command_id: str | None = None) -> str:
        """Fork before a selected user message and select the fork."""

        selected_id = command_id or self._command_id_factory("fork-session")
        await self._transport.send(ForkSessionCommand(id=selected_id, entry_id=entry_id))
        return selected_id

    async def get_session_tree(
        self,
        *,
        limit: int = 200,
        after_entry_id: str | None = None,
        command_id: str | None = None,
    ) -> str:
        """Request a bounded page of the selected session's tree."""

        selected_id = command_id or self._command_id_factory("session-tree")
        await self._transport.send(
            GetSessionTreeCommand(
                id=selected_id,
                limit=limit,
                after_entry_id=after_entry_id,
            )
        )
        return selected_id

    async def navigate_session_tree(
        self,
        entry_id: str,
        *,
        command_id: str | None = None,
    ) -> str:
        """Navigate the selected session to one persisted tree entry."""

        selected_id = command_id or self._command_id_factory("navigate-session-tree")
        await self._transport.send(NavigateSessionTreeCommand(id=selected_id, entry_id=entry_id))
        return selected_id

    async def unrevert_session_tree(self, *, command_id: str | None = None) -> str:
        """Reverse the selected session's latest explicit tree navigation."""

        selected_id = command_id or self._command_id_factory("unrevert-session-tree")
        await self._transport.send(UnrevertSessionTreeCommand(id=selected_id))
        return selected_id

    async def set_session_name(
        self,
        name: str,
        *,
        session_id: str | None = None,
        command_id: str | None = None,
    ) -> str:
        """Set or clear one session's display name."""

        selected_id = command_id or self._command_id_factory("set-session-name")
        await self._transport.send(
            SetSessionNameCommand(
                id=selected_id,
                name=name,
                session_id=session_id,
            )
        )
        return selected_id

    async def steer(self, content: str, *, command_id: str | None = None) -> str:
        """Queue steering text for the active run."""

        selected_id = command_id or self._command_id_factory("steer")
        await self._transport.send(SteerCommand(id=selected_id, content=content))
        return selected_id

    async def follow_up(self, content: str, *, command_id: str | None = None) -> str:
        """Queue follow-up text for the active run."""

        selected_id = command_id or self._command_id_factory("follow-up")
        await self._transport.send(FollowUpCommand(id=selected_id, content=content))
        return selected_id

    async def get_queue_state(self, *, command_id: str | None = None) -> str:
        """Request the active or retained queue state."""

        selected_id = command_id or self._command_id_factory("queue-state")
        await self._transport.send(GetQueueStateCommand(id=selected_id))
        return selected_id

    async def set_queue_mode(
        self,
        kind: QueueKind,
        mode: QueueMode,
        *,
        command_id: str | None = None,
    ) -> str:
        """Set one active queue's drain mode."""

        selected_id = command_id or self._command_id_factory("queue-mode")
        await self._transport.send(SetQueueModeCommand(id=selected_id, kind=kind, mode=mode))
        return selected_id

    async def pop_queue(self, kind: QueueKind, *, command_id: str | None = None) -> str:
        """Remove the latest item from one active queue."""

        selected_id = command_id or self._command_id_factory("queue-pop")
        await self._transport.send(PopQueueCommand(id=selected_id, kind=kind))
        return selected_id

    async def clear_queue(
        self,
        kind: QueueKind | None = None,
        *,
        command_id: str | None = None,
    ) -> str:
        """Clear one or both active queues."""

        selected_id = command_id or self._command_id_factory("queue-clear")
        await self._transport.send(ClearQueueCommand(id=selected_id, kind=kind))
        return selected_id

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str:
        """Request cancellation of a running prompt or compact command."""

        selected_id = command_id or self._command_id_factory("cancel")
        await self._transport.send(CancelCommand(id=selected_id, target_id=target_id))
        return selected_id

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str:
        """Approve or deny a pending tool approval request."""

        selected_id = command_id or self._command_id_factory("approval")
        await self._transport.send(
            ApprovalCommand(
                id=selected_id,
                call_id=call_id,
                approved=approved,
                reason=reason,
                scope=scope,
            )
        )
        return selected_id

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str:
        """Resolve a pending project-trust request."""

        selected_id = command_id or self._command_id_factory("trust")
        await self._transport.send(
            TrustCommand(
                id=selected_id,
                request_id=request_id,
                trusted=trusted,
                reason=reason,
                transient=True if transient else None,
            )
        )
        return selected_id

    async def shutdown(self, *, command_id: str | None = None) -> str:
        """Ask the RPC process to exit cleanly."""

        selected_id = command_id or self._command_id_factory("shutdown")
        await self._transport.send(ShutdownCommand(id=selected_id))
        return selected_id

    async def configure(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear_effort: bool = False,
        auto_compaction_enabled: bool | None = None,
        mode: AgentMode | None = None,
        command_id: str | None = None,
    ) -> str:
        """Update runtime settings for future prompt commands.

        The RPC host preserves sequential command order: configuration waits
        behind active sequential work and is applied before later prompts.

        A model-only update keeps the current provider when it offers that
        model, otherwise it auto-switches to a unique catalog owner. If multiple
        providers claim the model and the current provider is not one of them,
        the command fails and the provider must be specified explicitly.
        Catalog-unknown model strings remain permissive for custom providers.

        ``effort=None`` (the default) leaves any previously configured effort
        tier untouched -- the command's JSON serialization omits unset fields,
        so there is no wire difference between "not specified" and "None."
        Pass ``clear_effort=True`` to explicitly reset effort back to the
        provider's own default.
        """

        selected_id = command_id or self._command_id_factory("configure")
        await self._transport.send(
            ConfigureCommand(
                id=selected_id,
                provider=provider,
                model=model,
                effort=effort,
                clear_effort=clear_effort,
                auto_compaction_enabled=auto_compaction_enabled,
                mode=mode,
            )
        )
        return selected_id

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield typed events from the underlying transport."""

        return self._transport.events()

    async def close(self) -> None:
        """Close the underlying transport."""

        await self._transport.close()


_SUBPROCESS_CLOSE_TIMEOUT_SECONDS = 2
_SUBPROCESS_HANDSHAKE_TIMEOUT_SECONDS = 5


class RpcHandshakeError(RuntimeError):
    """The external backend rejected or violated RPC negotiation."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class RpcProtocolError(RuntimeError):
    """The negotiated external RPC stream violated its framing or type contract."""


class JsonlSubprocessRpcTransport:
    """Subprocess transport for `wisp --mode rpc` JSONL stdin/stdout."""

    def __init__(self, process: Process, request: RpcHandshakeRequest) -> None:
        self._process = process
        self._request = request
        self._limits = RpcTransportLimits(
            max_client_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES,
            max_server_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES,
        )
        self._event_schema_version = request.max_event_schema_version
        self._stdout_buffer = bytearray()
        self._close_lock = anyio.Lock()
        self._closed = False
        self._close_error: RuntimeError | None = None

    @classmethod
    async def start(
        cls,
        command: Sequence[str] | None = None,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stderr: int | None = subprocess.DEVNULL,
        handshake_request: RpcHandshakeRequest | None = None,
    ) -> JsonlSubprocessRpcTransport:
        """Start a subprocess running Wisp RPC mode.

        Stderr defaults to ``DEVNULL`` so an undrained stderr pipe cannot block
        the RPC event stream. Pass ``stderr=subprocess.PIPE`` only if another
        task will drain it.
        """

        selected_command = tuple(command) if command is not None else _default_rpc_command()
        process = await anyio.open_process(
            selected_command,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stderr=stderr,
        )
        request = handshake_request or RpcHandshakeRequest(
            frontend_name="wisp-python",
            frontend_version=__version__,
            min_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
            max_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
            min_event_schema_version=EVENT_SCHEMA_VERSION,
            max_event_schema_version=EVENT_SCHEMA_VERSION,
            supported_capabilities=(),
            required_capabilities=(),
        )
        transport = cls(process, request)
        try:
            await transport._perform_handshake()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await transport.close()
            raise
        return transport

    async def send(self, command: RpcCommand) -> None:
        """Send one command as a JSONL line to subprocess stdin."""

        if self._process.stdin is None:
            raise RuntimeError("RPC subprocess stdin is closed")
        frame = command.to_json_line().encode("utf-8")
        payload = frame[:-1] if frame.endswith(b"\n") else frame
        if len(payload) > self._limits.max_client_frame_bytes:
            raise RpcProtocolError(
                f"RPC command exceeds the {self._limits.max_client_frame_bytes}-byte limit"
            )
        await self._process.stdin.send(frame)

    async def close(self) -> None:
        """Close the subprocess through bounded graceful, terminate, and kill stages."""

        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                if self._closed:
                    if self._close_error is not None:
                        raise self._close_error
                    return
                if self._process.stdin is not None:
                    await self._process.stdin.aclose()
                if await self._wait_for_exit():
                    self._closed = True
                    return
                self._signal_process(self._process.terminate)
                if await self._wait_for_exit():
                    self._closed = True
                    return
                self._signal_process(self._process.kill)
                if await self._wait_for_exit():
                    self._closed = True
                    return
                error = RuntimeError("RPC subprocess did not exit after kill")
                self._closed = True
                self._close_error = error
                raise error

    async def _wait_for_exit(self) -> bool:
        if self._process.returncode is not None:
            return True
        with anyio.move_on_after(_SUBPROCESS_CLOSE_TIMEOUT_SECONDS) as scope:
            await self._process.wait()
        return not scope.cancel_called

    def _signal_process(self, signal: Callable[[], None]) -> None:
        if self._process.returncode is not None:
            return
        try:
            signal()
        except ProcessLookupError:
            pass

    def events(self) -> AsyncIterator[KnownWispEvent]:
        """Yield typed events parsed from subprocess stdout."""

        return self._events()

    async def _events(self) -> AsyncIterator[KnownWispEvent]:
        if self._process.stdout is None:
            return
        buffer = self._stdout_buffer
        self._stdout_buffer = bytearray()
        while True:
            while True:
                try:
                    frame = pop_rpc_frame(
                        buffer,
                        max_frame_bytes=self._limits.max_server_frame_bytes,
                    )
                except RpcFrameError as exc:
                    raise RpcProtocolError(str(exc)) from exc
                if frame is None:
                    break
                if not frame:
                    continue
                try:
                    payload = decode_rpc_object(
                        frame,
                        max_frame_bytes=self._limits.max_server_frame_bytes,
                    )
                    if payload.get("schema_version") != self._event_schema_version:
                        raise RpcProtocolError(
                            "Backend event schema version does not match the negotiated version"
                        )
                    yield wisp_event_from_json(frame.decode("utf-8"))
                except RpcProtocolError:
                    raise
                except (RpcFrameError, ValidationError, ValueError) as exc:
                    raise RpcProtocolError("Backend emitted an invalid RPC event") from exc
            try:
                chunk = await self._process.stdout.receive()
            except anyio.EndOfStream:
                break
            if not chunk:
                break
            buffer.extend(chunk)
        try:
            require_complete_rpc_stream(buffer)
        except RpcFrameError as exc:
            raise RpcProtocolError(str(exc)) from exc

    async def _perform_handshake(self) -> None:
        if self._process.stdin is None or self._process.stdout is None:
            raise RpcHandshakeError("RPC subprocess has no handshake streams")
        try:
            with anyio.fail_after(_SUBPROCESS_HANDSHAKE_TIMEOUT_SECONDS):
                await self._process.stdin.send(
                    encode_rpc_frame(self._request, max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES)
                )
                while True:
                    try:
                        frame = pop_rpc_frame(
                            self._stdout_buffer,
                            max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES,
                        )
                    except RpcFrameError as exc:
                        raise RpcHandshakeError(str(exc)) from exc
                    if frame is not None:
                        break
                    try:
                        chunk = await self._process.stdout.receive()
                    except anyio.EndOfStream as exc:
                        raise RpcHandshakeError("RPC backend closed before handshake") from exc
                    if not chunk:
                        raise RpcHandshakeError("RPC backend closed before handshake")
                    self._stdout_buffer.extend(chunk)
        except TimeoutError as exc:
            raise RpcHandshakeError("RPC backend did not complete handshake in time") from exc
        try:
            decode_rpc_object(frame, max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES)
            response = RpcHandshakeResponseAdapter.validate_json(frame)
        except (RpcFrameError, ValidationError, ValueError) as exc:
            raise RpcHandshakeError("RPC backend returned an invalid handshake response") from exc
        if isinstance(response, RpcHandshakeRejected):
            raise RpcHandshakeError(response.message, code=response.code)
        assert isinstance(response, RpcHandshakeAccepted)
        try:
            validate_rpc_handshake_response(self._request, response)
        except ValueError as exc:
            raise RpcHandshakeError(str(exc)) from exc
        self._limits = response.limits
        self._event_schema_version = response.event_schema_version


def _default_command_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _default_rpc_command() -> tuple[str, ...]:
    return (sys.executable, "-m", "wisp", "--mode", "rpc")
