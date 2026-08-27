"""Deterministic runner for language-neutral TUI transition traces."""

from __future__ import annotations

import json
import shlex
from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, cast

import anyio

from wisp.agent.mode import AgentMode
from wisp.events import KnownWispEventAdapter
from wisp.rpc.commands import ApprovalScope
from wisp.tui.input_types import SubmissionId, new_submission_id
from wisp.tui.rendering import LineTuiRenderer
from wisp.tui.shell import TuiShell
from wisp.tui.state import (
    TuiStatus,
    _InputCancelled,
    _InputLine,
    _InputMode,
    _RpcEventsClosed,
)
from wisp.tui.trace_schema import (
    TraceFile,
    TraceFileAdapter,
    TraceInteractionProjection,
    TraceViewProjection,
)

# Re-export for tests.
__all__ = [
    "DeterministicClock",
    "DeterministicIdFactory",
    "RecordingTraceRenderer",
    "TraceController",
    "TraceReplayError",
    "TraceRunResult",
    "TraceRunner",
    "load_trace",
    "run_trace",
]


class TraceReplayError(RuntimeError):
    """A trace action cannot be applied to the live shell state."""


class DeterministicClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._ms = start_ms

    def monotonic(self) -> float:
        return self._ms / 1000.0

    def advance_to(self, ms: int) -> None:
        if ms < self._ms:
            raise ValueError(f"clock cannot go backwards: {ms} < {self._ms}")
        self._ms = ms

    @property
    def ms(self) -> int:
        return self._ms


class DeterministicIdFactory:
    def __init__(self) -> None:
        self._counters: dict[str, count[int]] = {}

    def next(self, prefix: str) -> str:
        counter = self._counters.get(prefix)
        if counter is None:
            counter = count(1)
            self._counters[prefix] = counter
        return f"{prefix}-{next(counter)}"


class RecordingTraceRenderer(LineTuiRenderer):
    def __init__(self) -> None:
        # Use a non-terminal console so tests don't depend on tty.
        import io

        from rich.console import Console

        super().__init__(Console(file=io.StringIO(), width=80))
        self.view_snapshots: list[Any] = []
        self.notices: list[str] = []
        self.errors: list[str] = []
        self.tokens: list[str] = []

    def view_updated(self, snapshot: Any) -> None:
        self.view_snapshots.append(snapshot)

    def notice(self, message: str) -> None:
        self.notices.append(message)

    def command_error(self, message: str) -> None:
        self.errors.append(message)

    def token_delta(self, delta: str) -> None:
        self.tokens.append(delta)


class TraceController:
    """Fake controller that records outbound commands deterministically."""

    def __init__(self, id_factory: DeterministicIdFactory) -> None:
        self._id_factory = id_factory
        self.commands: list[dict[str, Any]] = []
        self._events: deque[Any] = deque()

    def _next_id(self, prefix: str) -> str:
        return self._id_factory.next(prefix)

    async def prompt(self, prompt: str, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("prompt")
        self.commands.append({"type": "prompt", "id": cid, "prompt": prompt})
        return cid

    async def init(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("init")
        self.commands.append({"type": "init", "id": cid})
        return cid

    async def compact(
        self, instructions: str | None = None, *, command_id: str | None = None
    ) -> str:  # noqa: E501
        cid = command_id or self._next_id("compact")
        self.commands.append({"type": "compact", "id": cid, "instructions": instructions})
        return cid

    async def get_session_stats(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("get_session_stats")
        self.commands.append({"type": "get_session_stats", "id": cid})
        return cid

    async def get_commands(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("get_commands")
        self.commands.append({"type": "get_commands", "id": cid})
        return cid

    async def get_skills(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("get_skills")
        self.commands.append({"type": "get_skills", "id": cid})
        return cid

    async def get_mcp_status(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("get_mcp_status")
        self.commands.append({"type": "get_mcp_status", "id": cid})
        return cid

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
        cid = command_id or self._next_id("get_messages")
        self.commands.append(
            {
                "type": "get_messages",
                "id": cid,
                "session_id": session_id,
                "limit": limit,
            }
        )
        return cid

    async def get_sessions(self, *, limit: int = 50, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("get_sessions")
        self.commands.append({"type": "get_sessions", "id": cid, "limit": limit})
        return cid

    async def new_session(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("new_session")
        self.commands.append({"type": "new_session", "id": cid})
        return cid

    async def select_session(self, session_id: str, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("select_session")
        self.commands.append({"type": "select_session", "id": cid, "session_id": session_id})
        return cid

    async def steer(self, content: str, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("steer")
        self.commands.append({"type": "steer", "id": cid, "content": content})
        return cid

    async def follow_up(self, content: str, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("follow_up")
        self.commands.append({"type": "follow_up", "id": cid, "content": content})
        return cid

    async def get_queue_state(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("get_queue_state")
        self.commands.append({"type": "get_queue_state", "id": cid})
        return cid

    async def pop_queue(self, kind: str, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("pop_queue")
        self.commands.append({"type": "pop_queue", "id": cid, "kind": kind})
        return cid

    async def cancel(self, target_id: str, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("cancel")
        self.commands.append({"type": "cancel", "id": cid, "target_id": target_id})
        return cid

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str:
        cid = command_id or self._next_id("approval")
        self.commands.append(
            {
                "type": "approval",
                "id": cid,
                "call_id": call_id,
                "approved": approved,
                "reason": reason,
                "scope": scope,
            }
        )
        return cid

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str:
        cid = command_id or self._next_id("trust")
        self.commands.append(
            {
                "type": "trust",
                "id": cid,
                "request_id": request_id,
                "trusted": trusted,
                "reason": reason,
                "transient": transient,
            }
        )
        return cid

    async def shutdown(self, *, command_id: str | None = None) -> str:
        cid = command_id or self._next_id("shutdown")
        self.commands.append({"type": "shutdown", "id": cid})
        return cid

    async def configure(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear_effort: bool = False,
        auto_compaction_enabled: bool | None = None,
        mode: Any | None = None,
        command_id: str | None = None,
    ) -> str:
        cid = command_id or self._next_id("configure")
        self.commands.append(
            {
                "type": "configure",
                "id": cid,
                "provider": provider,
                "model": model,
                "effort": effort,
            }
        )
        return cid

    def events(self) -> Any:
        async def _gen() -> Any:
            # For trace runner we don't use the live event stream; events are
            # injected via _handle_rpc_event directly. This keeps determinism.
            if False:  # pragma: no cover - never yields
                yield None
            await anyio.sleep(0)

        return _gen()

    async def close(self) -> None:
        return None


@dataclass(frozen=True)
class TraceRunResult:
    commands: tuple[dict[str, Any], ...]
    view: TraceViewProjection
    interaction: TraceInteractionProjection
    retained_text: str | None
    notices: tuple[str, ...]
    errors: tuple[str, ...]
    tokens: tuple[str, ...]


def _reset_submission_ids() -> None:
    """Reset global submission counter so each trace starts from 1."""

    import wisp.tui.input_types as input_types

    input_types._SUBMISSION_IDS = count(1)


def _view_projection(shell: TuiShell) -> TraceViewProjection:
    snap = shell.view.snapshot()
    return TraceViewProjection(
        status=snap.status,
        input_mode=snap.input_mode,
        input_ready=snap.input_ready,
        queued_steering=snap.queued_steering,
        queued_follow_ups=snap.queued_follow_ups,
        provider=snap.provider,
        model=snap.model,
        mode=str(snap.mode),
        last_session=snap.last_session,
    )


def _interaction_projection(shell: TuiShell) -> TraceInteractionProjection:
    state = shell.state
    approval_id = state.pending_approval.call_id if state.pending_approval is not None else None
    trust_id = state.pending_trust.request_id if state.pending_trust is not None else None
    return TraceInteractionProjection(
        status=state.status.value,
        current_command_id=state.current_command_id,
        current_command_type=state.current_command_type,
        pending_approval_call_id=approval_id,
        pending_trust_request_id=trust_id,
        cancel_requested=state.cancel_requested,
        exit_requested=state.exit_requested,
    )


def load_trace(path: Path) -> TraceFile:
    data = json.loads(path.read_text(encoding="utf-8"))
    return TraceFileAdapter.validate_python(data)


class TraceRunner:
    def __init__(self, trace: TraceFile) -> None:
        self.trace = trace
        self.clock = DeterministicClock()
        self.id_factory = DeterministicIdFactory()
        self.controller = TraceController(self.id_factory)
        self.renderer = RecordingTraceRenderer()
        # Shell uses the same factory and clock for determinism.
        self.shell = TuiShell(
            self.controller,
            renderer=self.renderer,
            provider=trace.initial.provider,
            model=trace.initial.model,
            effort=trace.initial.effort,
            command_id_factory=self.controller._next_id,
            clock=self.clock.monotonic,
        )
        # Seed view/interaction if provided.
        if trace.initial.view is not None:
            v = trace.initial.view
            self.shell.view.status = v.status
            self.shell.view.input_mode = v.input_mode
            self.shell.view.input_ready = v.input_ready
            self.shell.view.queued_steering = v.queued_steering
            self.shell.view.queued_follow_ups = v.queued_follow_ups
            self.shell.view.provider = v.provider
            self.shell.view.model = v.model
            # mode and last_session flow through shell-owned state so later
            # _sync_view() calls keep replaying the seeded values faithfully.
            if v.mode not in ("build", "plan"):
                raise TraceReplayError(f"invalid initial view mode: {v.mode!r}")
            self.shell.current_mode = cast(AgentMode, v.mode)
            self.shell.view.mode = cast(AgentMode, v.mode)
            if v.last_session is not None:
                self.shell.view.last_session = v.last_session
        if trace.initial.interaction is not None:
            inter = trace.initial.interaction
            # Map string status to TuiStatus if possible; fallback to keep.
            try:
                self.shell.state.status = TuiStatus(inter.status)
            except ValueError:
                self.shell.state.status = TuiStatus.idle
            self.shell.state.current_command_id = inter.current_command_id
            if inter.current_command_type is not None:
                # Literal narrow: prompt|init|compact; keep if matches else None.
                if inter.current_command_type in {"prompt", "init", "compact"}:
                    self.shell.state.current_command_type = inter.current_command_type  # type: ignore[assignment]
            # Pending approval/trust are set via rpc events, not initial mock.
            self.shell.state.cancel_requested = inter.cancel_requested
            self.shell.state.exit_requested = inter.exit_requested

    async def run(self) -> TraceRunResult:
        _reset_submission_ids()
        # Ensure shell starts from a clean snapshot appropriate for traces:
        # Mark input ready (post-hydration) unless trace says otherwise.
        if self.trace.initial.view is None:
            self.shell.view.input_ready = True
            self.shell.view.status = "idle"
            self.shell.state.status = TuiStatus.idle
            self.shell._sync_view()

        for inp in self.trace.inputs:
            clock_ms: int = int(getattr(inp, "clock_ms", 0))
            self.clock.advance_to(clock_ms)

            if inp.type == "local.submit":
                # Build a submission with deterministic id.
                sid = new_submission_id()
                # Use the input's content; queue_kind defaults to auto.
                from wisp.tui.input_types import TuiSubmission

                sub = TuiSubmission(
                    id=SubmissionId(int(sid)),
                    content=inp.content,
                    display=inp.content,
                    input_mode="idle",
                )
                await self.shell._handle_input_line(_InputLine(text=sub, mode=_InputMode.idle))
            elif inp.type == "local.slash":
                # For slash commands, synthesize an input line with slash text.
                # parse_tui_slash_command only recognizes "/"-prefixed text, so
                # normalize unprefixed names instead of submitting them as prompts.
                command = inp.command if inp.command.startswith("/") else f"/{inp.command}"
                slash_text = command
                if inp.args:
                    # shlex-quote each token so argument boundaries survive the
                    # shell's shlex.split re-tokenization unchanged.
                    slash_text += " " + " ".join(shlex.quote(arg) for arg in inp.args)
                await self.shell._handle_input_line(
                    _InputLine(text=slash_text, mode=_InputMode.idle)
                )
            elif inp.type == "local.cancel":
                await self.shell._handle_input_cancelled(_InputCancelled(mode=_InputMode.idle))
            elif inp.type == "local.approve":
                # The trace encodes the exact request it answers; refuse to
                # resolve a stale or mis-correlated approval target.
                approval = self.shell.state.pending_approval
                if approval is None or approval.call_id != inp.call_id:
                    pending = approval.call_id if approval is not None else None
                    raise TraceReplayError(
                        f"local.approve targets call_id {inp.call_id!r} but {pending!r} is pending"
                    )
                scope_val: ApprovalScope | None = inp.scope
                await self.shell._answer_pending_approval(
                    "",
                    approved=inp.approved,
                    reason=inp.reason,
                    scope=scope_val,
                    exit_after_denial=False,
                )
            elif inp.type == "local.trust":
                trust = self.shell.state.pending_trust
                if trust is None or trust.request_id != inp.request_id:
                    pending_id = trust.request_id if trust is not None else None
                    raise TraceReplayError(
                        f"local.trust targets request_id {inp.request_id!r} but "
                        f"{pending_id!r} is pending"
                    )
                await self.shell._answer_pending_trust(
                    "",
                    trusted=inp.trusted,
                    reason=None if inp.trusted else "Denied from trace",
                    transient=bool(inp.transient) if not inp.trusted else False,
                )
            elif inp.type == "rpc.event":
                # Validate and dispatch typed event.
                event = KnownWispEventAdapter.validate_python(inp.event)
                await self.shell._handle_rpc_event(event)
            elif inp.type == "rpc.closed":
                self.shell._handle_rpc_closed(_RpcEventsClosed(error=inp.error))
            else:
                raise RuntimeError(f"unknown trace input type: {inp.type}")

        view = _view_projection(self.shell)
        interaction = _interaction_projection(self.shell)
        retained = "".join(self.renderer.tokens) if self.renderer.tokens else None
        return TraceRunResult(
            commands=tuple(self.controller.commands),
            view=view,
            interaction=interaction,
            retained_text=retained,
            notices=tuple(self.renderer.notices),
            errors=tuple(self.renderer.errors),
            tokens=tuple(self.renderer.tokens),
        )


async def run_trace(trace: TraceFile) -> TraceRunResult:
    runner = TraceRunner(trace)
    return await runner.run()
