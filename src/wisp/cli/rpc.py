"""RPC output mode for Wisp's CLI.

This module hosts the JSONL RPC subsystem that was previously inlined in
``wisp.cli.__init__``. ``wisp.cli`` re-exports these names via compatibility
aliases so callers and tests keep importing them from ``wisp.cli``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import cast
from uuid import uuid4

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.loop import Agent
from wisp.agent.messages import Message
from wisp.agent.prompt import resolve_project_context_root
from wisp.config import WispConfig
from wisp.events import (
    ErrorEvent,
    ProjectConfigApplied,
    RpcCommandFinished,
    RpcCommandStarted,
    TrustRequested,
    TrustResolved,
)
from wisp.providers.base import ProviderError
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownProviderError, UnknownToolError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionError
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.trust import is_trusted, record_trust

from . import output as _cli_output
from . import tools as _cli_tools
from . import trust as _cli_trust
from .types import _JsonOutputModeError

_render_json_events = _cli_output._render_json_events
_write_json_event = _cli_output._write_json_event

_print_mode_tool_approval_policy = _cli_tools._print_mode_tool_approval_policy
_print_mode_tool_registry = _cli_tools._print_mode_tool_registry
_session_for_print_run = _cli_tools._session_for_print_run


async def _build_runtime_for_config(config: WispConfig) -> WispRuntime:
    # Preserve compatibility with embedders and tests that replace the runtime
    # factory with an older, narrower callable. Do not conceal TypeErrors raised
    # inside a compatible factory.
    for kwargs in (
        {"auth_path": config.auth_path, "retry_policy": config.retry_policy},
        {"auth_path": config.auth_path},
        {},
    ):
        try:
            return await build_runtime(**kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc) or not kwargs:
                raise
    raise AssertionError("runtime factory compatibility loop exhausted")


@dataclass(frozen=True)
class _RpcInputCommand:
    command: dict[str, object]


@dataclass(frozen=True)
class _RpcInputClosed:
    pass


@dataclass(frozen=True)
class _RpcPromptCompleted:
    command_id: str
    ok: bool
    history: tuple[Message, ...] | None
    entry_count: int


@dataclass
class _RpcSessionState:
    session: JsonlSession | None
    history: tuple[Message, ...]
    entry_count: int


@dataclass(frozen=True)
class _RpcRunningPrompt:
    command_id: str
    cancel_scope: anyio.CancelScope


@dataclass
class _RpcPendingApproval:
    call_id: str
    event: anyio.Event
    approved: bool | None = None
    reason: str | None = None
    resolved: bool = False


type _RpcControlEvent = _RpcInputCommand | _RpcInputClosed | _RpcPromptCompleted


_STDIN_READ_CHUNK_SIZE = 64 * 1024
_STDIN_THREAD_POLL_INTERVAL = 0.01
_STDIN_THREAD_QUEUE_SIZE = 100
_MAX_QUEUED_RPC_COMMANDS = 100


class _RpcToolApprovalPolicy(ToolApprovalPolicy):
    """Tool approval policy that can wait for RPC approval responses."""

    def __init__(self, fallback: ToolApprovalPolicy) -> None:
        super().__init__(
            approved_tools=fallback.approved_tools,
            approved_safety=fallback.approved_safety,
        )
        self._pending: dict[str, _RpcPendingApproval] = {}
        self._input_closed_reason: str | None = None

    def prepare_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> None:
        if self.approves(tool):
            return
        pending = _RpcPendingApproval(call_id=call_id, event=anyio.Event())
        self._pending[call_id] = pending
        if self._input_closed_reason is not None:
            self._resolve_pending(
                pending,
                approved=False,
                reason=self._input_closed_reason,
            )

    async def await_approval(
        self,
        tool: Tool,
        *,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalDecision:
        if self.approves(tool):
            return ToolApprovalDecision(approved=True)
        pending = self._pending.get(call_id)
        if pending is None:
            return ToolApprovalDecision(approved=False, reason=self.block_reason(tool))
        try:
            await pending.event.wait()
            approved = pending.approved is True
            reason = None if approved else pending.reason or "Tool execution was denied"
            return ToolApprovalDecision(approved=approved, reason=reason)
        finally:
            self._pending.pop(call_id, None)

    def resolve_approval(
        self,
        *,
        call_id: str,
        approved: bool,
        reason: str | None = None,
    ) -> bool:
        pending = self._pending.get(call_id)
        if pending is None or pending.resolved:
            return False
        self._resolve_pending(pending, approved=approved, reason=reason)
        return True

    def deny_pending_on_input_closed(self) -> None:
        reason = "RPC input closed before approval response"
        self._input_closed_reason = reason
        for pending in tuple(self._pending.values()):
            if not pending.resolved:
                self._resolve_pending(pending, approved=False, reason=reason)

    def _resolve_pending(
        self,
        pending: _RpcPendingApproval,
        *,
        approved: bool,
        reason: str | None,
    ) -> None:
        pending.resolved = True
        pending.approved = approved
        pending.reason = None if approved else reason
        pending.event.set()


@dataclass
class _RpcPendingTrust:
    request_id: str
    event: anyio.Event
    trusted: bool | None = None
    reason: str | None = None
    transient: bool = False
    resolved: bool = False


class _RpcTrustGate:
    """Resolves project trust once, prompting the RPC client when undecided.

    Mirrors the approval policy's pending-request pattern: the project-trust
    question is a yes/no keyed by ``request_id`` that the client answers with a
    ``TrustCommand``. The decision is resolved lazily on first need (before the
    first prompt runs) and cached, so a project is prompted for at most once per
    process. Precedence: a ``WISP_TRUST`` override and a stored decision are
    honored without prompting; only an undecided project emits ``TrustRequested``
    and blocks. Input closing before an answer resolves to untrusted (safe).
    """

    def __init__(
        self,
        project_path: Path,
        *,
        on_first_trusted: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._project_path = project_path
        # Invoked at most once, the first time trust resolves to True, so a first-run
        # session that approves trust can rebuild the config-derived runtime with the
        # project's now-trusted ``.wisp/settings.json`` before the first turn runs.
        self._on_first_trusted = on_first_trusted
        self._pending: _RpcPendingTrust | None = None
        self._decision: bool | None = None
        self._resolved = False
        self._input_closed = False

    async def resolve(self) -> bool:
        """Return whether the project is trusted, prompting the client if needed."""

        if self._resolved:
            assert self._decision is not None
            return self._decision

        override = _cli_trust.trust_override_from_env()
        if override is not None:
            return await self._finish(override)

        stored = is_trusted(self._project_path)
        if stored is not None:
            return await self._finish(stored)

        if self._input_closed:
            return await self._finish(False)

        # Undecided: prompt the client. Only this path emits trust events, so a
        # project resolved by env override or a stored decision produces a clean
        # event stream.
        pending = _RpcPendingTrust(request_id=uuid4().hex, event=anyio.Event())
        self._pending = pending
        _write_json_event(
            TrustRequested(request_id=pending.request_id, project_path=self._project_path)
        )
        await pending.event.wait()
        trusted = pending.trusted is True
        if trusted:
            record_trust(self._project_path, True)
        elif not pending.transient:
            # Explicit "no" answers are persisted even when they carry explanatory
            # text. Forced UI/input-close denials opt into transient behavior.
            record_trust(self._project_path, False)
        self._pending = None
        _write_json_event(
            TrustResolved(
                request_id=pending.request_id,
                project_path=self._project_path,
                trusted=trusted,
                reason=pending.reason,
            )
        )
        return await self._finish(trusted)

    def resolve_request(
        self,
        *,
        request_id: str,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
    ) -> bool:
        pending = self._pending
        if pending is None or pending.resolved or pending.request_id != request_id:
            return False
        pending.resolved = True
        pending.trusted = trusted
        pending.reason = reason
        pending.transient = transient
        pending.event.set()
        return True

    def deny_pending_on_input_closed(self) -> None:
        self._input_closed = True
        pending = self._pending
        if pending is not None and not pending.resolved:
            pending.resolved = True
            pending.trusted = False
            pending.reason = "RPC input closed before trust response"
            pending.transient = True
            pending.event.set()

    async def _finish(self, trusted: bool) -> bool:
        # Run the first-trusted rebuild BEFORE caching the decision. If the rebuild
        # raises (e.g. a trusted project's settings.json names an unknown provider), we
        # must NOT mark the gate resolved: otherwise the caller reports this prompt as
        # failed, but every later prompt would return the cached decision, skip the
        # rebuild, and silently run with the stale untrusted startup config. Leaving the
        # gate unresolved makes each subsequent prompt re-attempt the rebuild and fail
        # the same way, instead of quietly succeeding with the wrong provider.
        if trusted and self._on_first_trusted is not None and not self._resolved:
            await self._on_first_trusted()
        self._decision = trusted
        self._resolved = True
        return trusted


@dataclass(frozen=True)
class _ConfigOverrides:
    """The explicit CLI overrides used to build the RPC config.

    Retained so a first-run session that approves trust can rebuild the config from the
    *same* inputs with the project ``.wisp/settings.json`` layer applied, rather than
    trying to reconstruct the original arguments from the already-merged config.
    """

    provider: str | None = None
    model: str | None = None
    session_dir: Path | None = None
    auth_path: Path | None = None

    def build(self, *, trusted: bool, project_dir: Path | None = None) -> WispConfig:
        return WispConfig.from_env(
            provider=self.provider,
            model=self.model,
            session_dir=self.session_dir,
            auth_path=self.auth_path,
            project_dir=project_dir,
            trusted=trusted,
        )


@dataclass
class _RpcConfigureOverrides:
    """Successful in-session RPC configure choices that outrank project settings."""

    provider: str | None = None
    model: str | None = None
    has_model: bool = False

    def effective_provider(self, default: str) -> str:
        return self.provider or default

    def effective_model(self, default: str | None) -> str | None:
        return self.model if self.has_model else default


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
) -> None:
    runtime = await _build_runtime_for_config(config)
    provider = runtime.providers.get(config.provider)
    sessions = JsonlSessionStore(config.session_dir)
    session = _session_for_print_run(sessions, resume=resume, continue_latest=continue_latest)
    session_state = _rpc_session_state(session)
    approval_policy = _RpcToolApprovalPolicy(_print_mode_tool_approval_policy(approve_unsafe_tools))
    configure_overrides = _RpcConfigureOverrides()
    selected_project_context_root = project_context_root or resolve_project_context_root(Path.cwd())

    async def _rebuild_agent_for_trusted_project() -> None:
        # First-run RPC/TUI: trust was undecided at startup, so ``config`` was built
        # untrusted and the project's ``.wisp/settings.json`` was skipped. Now that the
        # client has approved trust — and we are still before the first turn — re-derive
        # config from the *same* explicit overrides with the project layer applied and
        # swap the pieces the agent reads fresh each turn (provider, model, tool policy)
        # so this session honors the trusted settings instead of running with defaults.
        #
        # The enclosing ``runtime`` also adopts the trusted provider registry (built
        # from the trusted ``auth_path``) that the RPC loop passes to later
        # ``configure`` / ``/provider`` switches. Without this, a subsequent provider
        # switch would resolve against the untrusted-startup credential store and could
        # silently drop the trusted project's auth file.
        #
        # Only ``providers`` is swapped, via ``replace`` — NOT the whole runtime. A
        # rebuilt runtime has its own fresh ``EventBus``/``ExtensionAPI``; wholesale
        # reassignment would leave the loop's runtime pointing at a different bus than
        # the one the already-built agent emits on, splitting the event stream. Keeping
        # the original ``events``/``api``/``tools`` preserves a single shared bus.
        #
        # ``session_dir`` is intentionally NOT relocated here: the session store is owned
        # by the RPC loop (which already created the first prompt's session before trust
        # resolved), so a mid-run swap would be both incomplete and racy. A trusted
        # project's ``session_dir`` takes effect on the next launch — the trust decision
        # persists, so this is a one-time lag.
        nonlocal runtime
        if startup_trusted or config_overrides is None:
            return  # config already reflects the trusted project; nothing to rebuild.
        trusted_config = config_overrides.build(
            trusted=True,
            project_dir=selected_project_context_root,
        )
        if trusted_config == config:
            return  # the project has no settings.json; the trusted build is identical.
        trusted_runtime = await _build_runtime_for_config(trusted_config)
        effective_provider = configure_overrides.effective_provider(trusted_config.provider)
        effective_model = configure_overrides.effective_model(trusted_config.model)
        agent.provider = trusted_runtime.providers.get(effective_provider)
        agent.model = effective_model
        agent.tool_context = ToolContext.from_config(trusted_config)
        runtime = replace(runtime, providers=trusted_runtime.providers)
        # Tell an out-of-process front-end (the TUI) the config it displays/mutates
        # changed, so its header and /provider,/model,/auth,/login stop showing the
        # untrusted-startup values.
        _write_json_event(
            ProjectConfigApplied(
                provider=effective_provider,
                model=effective_model,
                auth_path=trusted_config.auth_path,
            )
        )

    trust_gate = _RpcTrustGate(
        selected_project_context_root,
        on_first_trusted=_rebuild_agent_for_trusted_project,
    )
    agent = Agent(
        provider=provider,
        sessions=sessions,
        events=runtime.events,
        model=config.model,
        tool_registry=_print_mode_tool_registry(
            runtime.tools,
            all_tools=all_tools,
            allow_read_tools=allow_read_tools,
            allowed_tools=allowed_tools,
        ),
        tool_context=ToolContext.from_config(config),
        tool_approval_policy=approval_policy,
        max_tool_iterations=max_tool_iterations,
        project_context_root=selected_project_context_root,
    )

    queued_commands: deque[dict[str, object]] = deque()
    running_prompt: _RpcRunningPrompt | None = None
    stdin_closed = False
    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](100)
    stop_reader = anyio.Event()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_read_rpc_stdin, send.clone(), stop_reader)
        async with send, receive:
            while True:
                if running_prompt is None and queued_commands:
                    command = queued_commands.popleft()
                    running_prompt, should_shutdown = _dispatch_rpc_command(
                        command,
                        agent=agent,
                        runtime=runtime,
                        sessions=sessions,
                        session_state=session_state,
                        task_group=task_group,
                        send=send,
                        running_prompt=running_prompt,
                        approval_policy=approval_policy,
                        trust_gate=trust_gate,
                        configure_overrides=configure_overrides,
                    )
                    if should_shutdown:
                        stop_reader.set()
                        task_group.cancel_scope.cancel()
                        return
                    continue
                if stdin_closed and running_prompt is None and not queued_commands:
                    stop_reader.set()
                    task_group.cancel_scope.cancel()
                    return

                control_event = await receive.receive()
                if isinstance(control_event, _RpcInputClosed):
                    stdin_closed = True
                    approval_policy.deny_pending_on_input_closed()
                    trust_gate.deny_pending_on_input_closed()
                    continue
                if isinstance(control_event, _RpcPromptCompleted):
                    if (
                        running_prompt is not None
                        and control_event.command_id == running_prompt.command_id
                    ):
                        running_prompt = None
                        session_state.entry_count = control_event.entry_count
                        if control_event.history is not None:
                            session_state.history = control_event.history
                    continue

                command = control_event.command
                command_type = _rpc_command_type(command)
                if running_prompt is not None and command_type not in {
                    "approval",
                    "cancel",
                    "trust",
                }:
                    if len(queued_commands) >= _MAX_QUEUED_RPC_COMMANDS:
                        _reject_rpc_command(
                            command,
                            message="RPC command queue is full while a prompt is running",
                        )
                        continue
                    queued_commands.append(command)
                    continue
                running_prompt, should_shutdown = _dispatch_rpc_command(
                    command,
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=session_state,
                    task_group=task_group,
                    send=send,
                    running_prompt=running_prompt,
                    approval_policy=approval_policy,
                    trust_gate=trust_gate,
                    configure_overrides=configure_overrides,
                )
                if should_shutdown:
                    stop_reader.set()
                    task_group.cancel_scope.cancel()
                    return


def _dispatch_rpc_command(
    command: dict[str, object],
    *,
    agent: Agent,
    runtime: WispRuntime,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    running_prompt: _RpcRunningPrompt | None,
    approval_policy: _RpcToolApprovalPolicy,
    trust_gate: _RpcTrustGate,
    configure_overrides: _RpcConfigureOverrides,
) -> tuple[_RpcRunningPrompt | None, bool]:
    command_type = _rpc_command_type(command)
    if command_type == "prompt":
        new_running_prompt, new_session = _start_rpc_prompt_command(
            command,
            agent=agent,
            sessions=sessions,
            session_state=session_state,
            task_group=task_group,
            send=send,
            trust_gate=trust_gate,
        )
        if new_session is not None:
            session_state.session = new_session
        return new_running_prompt, False
    should_shutdown = _handle_rpc_control_command(
        command,
        agent=agent,
        runtime=runtime,
        running_prompt=running_prompt,
        approval_policy=approval_policy,
        trust_gate=trust_gate,
        configure_overrides=configure_overrides,
    )
    return running_prompt, should_shutdown


def _rpc_session_state(session: JsonlSession | None) -> _RpcSessionState:
    if session is None or not session.path.is_file():
        return _RpcSessionState(session=session, history=(), entry_count=0)
    return _RpcSessionState(
        session=session,
        history=session.read_messages(),
        entry_count=len(session.read_entries()),
    )


async def _read_rpc_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    async with send:
        try:
            fd = sys.stdin.fileno()
            stdin_mode = os.fstat(fd).st_mode
        except (AttributeError, OSError, ValueError):
            await _read_rpc_text_stdin(send, stop_reader)
            return
        if stat.S_ISREG(stdin_mode):
            await _read_rpc_text_stdin(send, stop_reader)
            return
        if _rpc_stdin_needs_thread_reader(stdin_mode):
            await _read_rpc_thread_stdin(send, stop_reader)
            return
        await _read_rpc_fd_stdin(send, stop_reader, fd)


def _rpc_stdin_needs_thread_reader(stdin_mode: int) -> bool:
    return os.name != "posix" and not stat.S_ISREG(stdin_mode)


async def _read_rpc_text_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    while not stop_reader.is_set():
        raw_line = await anyio.to_thread.run_sync(sys.stdin.readline)
        if raw_line == "":
            await send.send(_RpcInputClosed())
            return
        await _send_rpc_input_line(send, raw_line)


async def _read_rpc_thread_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
) -> None:
    lines: Queue[str | Exception] = Queue(maxsize=_STDIN_THREAD_QUEUE_SIZE)
    stdin = sys.stdin

    def read_lines() -> None:
        try:
            while True:
                raw_line = stdin.readline()
                lines.put(raw_line)
                if raw_line == "":
                    return
        except Exception as exc:  # noqa: BLE001 - surface stdin reader failures as RPC errors
            lines.put(exc)

    Thread(target=read_lines, name="wisp-rpc-stdin-reader", daemon=True).start()
    while not stop_reader.is_set():
        try:
            item = lines.get_nowait()
        except Empty:
            await anyio.sleep(_STDIN_THREAD_POLL_INTERVAL)
            continue
        if isinstance(item, Exception):
            _write_json_event(ErrorEvent(message=f"Failed to read RPC stdin: {item}"))
            await send.send(_RpcInputClosed())
            return
        if item == "":
            await send.send(_RpcInputClosed())
            return
        await _send_rpc_input_line(send, item)


async def _read_rpc_fd_stdin(
    send: MemoryObjectSendStream[_RpcControlEvent],
    stop_reader: anyio.Event,
    fd: int,
) -> None:
    buffer = bytearray()
    while not stop_reader.is_set():
        await anyio.wait_readable(fd)
        if stop_reader.is_set():
            return
        try:
            chunk = os.read(fd, _STDIN_READ_CHUNK_SIZE)
        except BlockingIOError:
            continue
        if chunk == b"":
            if buffer:
                await _send_rpc_input_line(send, _decode_rpc_stdin_line(buffer))
            await send.send(_RpcInputClosed())
            return
        buffer.extend(chunk)
        while True:
            newline_index = buffer.find(b"\n")
            if newline_index < 0:
                break
            line = _decode_rpc_stdin_line(buffer[:newline_index])
            del buffer[: newline_index + 1]
            await _send_rpc_input_line(send, line)


async def _send_rpc_input_line(
    send: MemoryObjectSendStream[_RpcControlEvent],
    raw_line: str,
) -> None:
    line = raw_line.strip()
    if not line:
        return
    command = _parse_rpc_command(line)
    if command is not None:
        await send.send(_RpcInputCommand(command=command))


def _decode_rpc_stdin_line(raw_line: bytes | bytearray) -> str:
    return bytes(raw_line).decode("utf-8", errors="replace")


def _start_rpc_prompt_command(
    command: dict[str, object],
    *,
    agent: Agent,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: _RpcTrustGate,
) -> tuple[_RpcRunningPrompt | None, JsonlSession | None]:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
        )
        return None, session_state.session

    prompt = command.get("prompt")
    if not isinstance(prompt, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC prompt command requires string field: prompt",
        )
        return None, session_state.session

    selected_session = session_state.session or sessions.create()
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        _run_rpc_prompt_command,
        agent,
        selected_session,
        session_state.history,
        session_state.entry_count,
        prompt,
        command_id,
        command_type,
        cancel_scope,
        send.clone(),
        trust_gate,
    )
    return _RpcRunningPrompt(command_id=command_id, cancel_scope=cancel_scope), selected_session


async def _run_rpc_prompt_command(
    agent: Agent,
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
    prompt: str,
    command_id: str,
    command_type: str,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    trust_gate: _RpcTrustGate,
) -> None:
    error: str | None = None
    try:
        with cancel_scope:
            try:
                # Resolve project trust before the first turn runs; the decision is
                # cached so subsequent prompts don't re-prompt. Trust commands are
                # processed by the main loop while this awaits, so this cannot
                # deadlock the reader.
                agent.trusted = await trust_gate.resolve()
                await _render_json_events(
                    agent.run(prompt, session=session, history=committed_history)
                )
            except _JsonOutputModeError as exc:
                error = str(exc)
            except (ProviderError, SessionError, UnknownProviderError, UnknownToolError) as exc:
                # A first-run approval can rebuild the agent from a trusted project's
                # settings.json that names an unknown provider (or an otherwise bad
                # runtime); resolve() raises here, before agent.run(). Report it as a
                # normal command failure — matching startup and print modes — instead of
                # letting it escape and tear down the RPC process with a silent stream
                # end and no rpc.command.finished error.
                error = str(exc)
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
    finally:
        cancelled = error is not None and error.startswith("RPC command cancelled:")
        if cancelled:
            await session.truncate_entries(entry_start)
        entry_count = (
            entry_start
            if cancelled
            else len(session.read_entries())
            if session.path.is_file()
            else entry_start
        )
        updated_history = (
            None if cancelled else _updated_rpc_history(session, committed_history, entry_start)
        )
        async with send:
            if cancelled:
                assert error is not None
                _write_json_event(ErrorEvent(message=error))
            _write_json_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type=command_type,
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                _RpcPromptCompleted(
                    command_id=command_id,
                    ok=error is None,
                    history=updated_history,
                    entry_count=entry_count,
                )
            )


def _updated_rpc_history(
    session: JsonlSession,
    committed_history: tuple[Message, ...],
    entry_start: int,
) -> tuple[Message, ...]:
    if not session.path.is_file():
        return committed_history
    entries = session.read_entries()
    new_messages = tuple(
        entry.message
        for entry in entries[entry_start:]
        if entry.kind == "message" and entry.message is not None
    )
    return (*committed_history, *new_messages)


def _reject_rpc_command(command: dict[str, object], *, message: str) -> None:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    _write_rpc_command_error(
        command_id=command_id,
        command_type=command_type,
        message=id_error or message,
    )


def _handle_rpc_control_command(
    command: dict[str, object],
    *,
    running_prompt: _RpcRunningPrompt | None,
    approval_policy: _RpcToolApprovalPolicy,
    agent: Agent | None = None,
    runtime: WispRuntime | None = None,
    trust_gate: _RpcTrustGate | None = None,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> bool:
    command_type, command_id, id_error = _rpc_command_identity(command)
    _write_json_event(RpcCommandStarted(command_id=command_id, command_type=command_type))
    if id_error is not None:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=id_error,
        )
        return False
    if command_type == "shutdown":
        _write_json_event(
            RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True)
        )
        return True
    if command_type == "cancel":
        _handle_rpc_cancel_command(
            command,
            command_id=command_id,
            command_type=command_type,
            running_prompt=running_prompt,
        )
        return False
    if command_type == "approval":
        _handle_rpc_approval_command(
            command,
            command_id=command_id,
            command_type=command_type,
            approval_policy=approval_policy,
        )
        return False
    if command_type == "trust":
        if trust_gate is None:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC trust command requires an active trust gate",
            )
            return False
        _handle_rpc_trust_command(
            command,
            command_id=command_id,
            command_type=command_type,
            trust_gate=trust_gate,
        )
        return False
    if command_type == "configure":
        if agent is None or runtime is None:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message="RPC configure command requires an active agent runtime",
            )
            return False
        _handle_rpc_configure_command(
            command,
            command_id=command_id,
            command_type=command_type,
            agent=agent,
            runtime=runtime,
            configure_overrides=configure_overrides,
        )
        return False
    message = f"Unknown RPC command: {command_type}"
    _write_rpc_command_error(command_id=command_id, command_type=command_type, message=message)
    return False


def _handle_rpc_configure_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    agent: Agent,
    runtime: WispRuntime,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> None:
    provider = command.get("provider")
    model = command.get("model")
    has_provider = "provider" in command
    has_model = "model" in command
    if not has_provider and not has_model:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command requires provider or model",
        )
        return
    if provider is not None and not isinstance(provider, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field provider must be a string",
        )
        return
    if model is not None and not isinstance(model, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC configure command field model must be a string",
        )
        return
    if isinstance(provider, str):
        try:
            agent.provider = runtime.providers.get(provider)
        except UnknownProviderError as exc:
            _write_rpc_command_error(
                command_id=command_id,
                command_type=command_type,
                message=str(exc),
            )
            return
        if configure_overrides is not None:
            configure_overrides.provider = provider
        if not has_model:
            agent.model = None
            if configure_overrides is not None:
                configure_overrides.model = None
                configure_overrides.has_model = True
    if has_model:
        agent.model = model
        if configure_overrides is not None:
            configure_overrides.model = model
            configure_overrides.has_model = True
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_approval_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    approval_policy: _RpcToolApprovalPolicy,
) -> None:
    call_id = command.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires string field: call_id",
        )
        return
    approved = command.get("approved")
    if not isinstance(approved, bool):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command requires boolean field: approved",
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC approval command field reason must be a string",
        )
        return
    if not approval_policy.resolve_approval(
        call_id=call_id,
        approved=approved,
        reason=reason,
    ):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending tool approval with call_id: {call_id}",
        )
        return
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_trust_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    trust_gate: _RpcTrustGate,
) -> None:
    request_id = command.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires string field: request_id",
        )
        return
    trusted = command.get("trusted")
    if not isinstance(trusted, bool):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command requires boolean field: trusted",
        )
        return
    reason = command.get("reason")
    if reason is not None and not isinstance(reason, str):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field reason must be a string",
        )
        return
    transient = command.get("transient")
    if transient is not None and not isinstance(transient, bool):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC trust command field transient must be a boolean",
        )
        return
    if not trust_gate.resolve_request(
        request_id=request_id,
        trusted=trusted,
        reason=reason,
        transient=transient is True,
    ):
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No pending trust request with request_id: {request_id}",
        )
        return
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _handle_rpc_cancel_command(
    command: dict[str, object],
    *,
    command_id: str,
    command_type: str,
    running_prompt: _RpcRunningPrompt | None,
) -> None:
    target_id = command.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message="RPC cancel command requires string field: target_id",
        )
        return
    if running_prompt is None or running_prompt.command_id != target_id:
        _write_rpc_command_error(
            command_id=command_id,
            command_type=command_type,
            message=f"No running RPC command with id: {target_id}",
        )
        return
    running_prompt.cancel_scope.cancel()
    _write_json_event(RpcCommandFinished(command_id=command_id, command_type=command_type, ok=True))


def _write_rpc_command_error(*, command_id: str, command_type: str, message: str) -> None:
    _write_json_event(ErrorEvent(message=message))
    _write_json_event(
        RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=False,
            error=message,
        )
    )


def _rpc_command_identity(command: dict[str, object]) -> tuple[str, str, str | None]:
    command_type = _rpc_command_type(command)
    command_id, id_error = _rpc_command_id(command)
    return command_type, command_id, id_error


def _rpc_command_type(command: dict[str, object]) -> str:
    command_type = command.get("type")
    return command_type if isinstance(command_type, str) and command_type else "unknown"


def _rpc_command_id(command: dict[str, object]) -> tuple[str, str | None]:
    command_id = command.get("id")
    if command_id is None:
        return uuid4().hex, None
    if isinstance(command_id, str) and command_id:
        return command_id, None
    return uuid4().hex, "RPC command id must be a non-empty string"


def _parse_rpc_command(line: str) -> dict[str, object] | None:
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        _write_json_event(ErrorEvent(message=f"Invalid RPC JSON: {exc.msg}"))
        return None
    if not isinstance(command, dict):
        _write_json_event(ErrorEvent(message="RPC command must be a JSON object"))
        return None
    return cast(dict[str, object], command)
