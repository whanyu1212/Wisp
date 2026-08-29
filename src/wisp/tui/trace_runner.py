"""Deterministic runner for language-neutral TUI transition traces."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections import deque
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Literal

import anyio

from wisp.agent.mode import AgentMode
from wisp.events import (
    KnownWispEvent,
    KnownWispEventAdapter,
    MessageCompleted,
    RpcCommandFinished,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolResultReady,
    TrustRequested,
)
from wisp.rpc.commands import ApprovalScope
from wisp.tool_presentation import tool_result_status
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
    MAX_TRACE_CONTENT_CHARS,
    TraceFile,
    TraceFileAdapter,
    TraceInteractionProjection,
    TraceToolCardProjection,
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
        self.retained_text: str | None = None
        self.tool_cards: list[TraceToolCardProjection] = []
        self._active_tool_cards: dict[str, int] = {}
        self._active_tool_metadata: dict[str, tuple[str, str]] = {}
        self._process_call_ids: set[str] = set()
        self._active_process_metadata: dict[str, tuple[str, str]] = {}
        self._resolved_process_call_ids: set[str] = set()
        self._resolved_process_call_order: deque[str] = deque()
        self._resolved_process_call_bytes = 0
        self._trace_current_command_id: str | None = None
        self._streaming = False

    def view_updated(self, snapshot: Any) -> None:
        self.view_snapshots.append(snapshot)

    def notice(self, message: str) -> None:
        self.notices.append(message)

    def command_error(self, message: str) -> None:
        self.errors.append(message)

    def token_delta(self, delta: str) -> None:
        self.tokens.append(delta)
        if not self._streaming:
            self.retained_text = ""
            self._streaming = True
        retained_text = (self.retained_text or "") + delta
        if len(retained_text) > MAX_TRACE_CONTENT_CHARS:
            raise TraceReplayError(
                f"partial response exceeds {MAX_TRACE_CONTENT_CHARS} retained characters"
            )
        self.retained_text = retained_text

    def end_token_stream_with_content(self, content: str) -> None:
        self.retained_text = content
        self._streaming = False
        super().end_token_stream()

    def end_token_stream(self) -> None:
        self._streaming = False
        super().end_token_stream()

    def approval_request(self, event: ToolApprovalRequested) -> None:
        if event.call_id in self._resolved_process_call_ids:
            self._set_tool_card(
                event.call_id,
                event.name,
                "cancelled",
                arguments_available=True,
                lifecycle_start=True,
            )
            super().approval_request(event)
            return
        if _trace_process_call(event.name, event.arguments):
            if self._process_identity_was_resolved(event.call_id):
                self._set_tool_card(
                    event.call_id,
                    event.name,
                    "cancelled",
                    arguments_available=True,
                    lifecycle_start=True,
                )
            elif event.call_id in self._active_tool_cards:
                self._set_conflicting_tool_card(event.call_id)
            elif event.call_id in self._process_call_ids:
                if self._active_process_metadata.get(event.call_id) != _trace_process_identity(
                    event.name, event.arguments
                ):
                    self._mark_resolved_process_call(event.call_id)
            else:
                self._process_call_ids.add(event.call_id)
                identity = _trace_process_identity(event.name, event.arguments)
                assert identity is not None
                self._active_process_metadata[event.call_id] = identity
            super().approval_request(event)
            return
        if event.call_id in self._process_call_ids:
            self._mark_resolved_process_call(event.call_id)
            super().approval_request(event)
            return
        if self._tool_lifecycle_conflicts(event.call_id, event.name, event.arguments):
            self._set_conflicting_tool_card(event.call_id)
        else:
            index = self._active_tool_cards.get(event.call_id)
            current = self.tool_cards[index] if index is not None else None
            if current is None or current.status == "requested":
                self._set_tool_card(
                    event.call_id,
                    event.name,
                    "awaiting_approval",
                    arguments_available=True,
                    lifecycle_start=True,
                )
        super().approval_request(event)

    def event(self, event: KnownWispEvent) -> None:
        if isinstance(event, MessageCompleted):
            self.retained_text = event.content
            self._streaming = False
        elif isinstance(event, ToolCallRequested):
            if event.call_id in self._resolved_process_call_ids:
                self._set_tool_card(
                    event.call_id,
                    event.name,
                    "cancelled",
                    arguments_available=True,
                    lifecycle_start=True,
                )
                super().event(event)
                return
            if _trace_process_call(event.name, event.arguments):
                if self._process_identity_was_resolved(event.call_id):
                    self._set_tool_card(
                        event.call_id,
                        event.name,
                        "cancelled",
                        arguments_available=True,
                        lifecycle_start=True,
                    )
                elif event.call_id in self._active_tool_cards:
                    self._set_conflicting_tool_card(event.call_id)
                elif event.call_id in self._process_call_ids:
                    if self._active_process_metadata.get(event.call_id) != _trace_process_identity(
                        event.name, event.arguments
                    ):
                        self._mark_resolved_process_call(event.call_id)
                else:
                    self._process_call_ids.add(event.call_id)
                    identity = _trace_process_identity(event.name, event.arguments)
                    assert identity is not None
                    self._active_process_metadata[event.call_id] = identity
                super().event(event)
                return
            if event.call_id in self._process_call_ids:
                self._mark_resolved_process_call(event.call_id)
                super().event(event)
                return
            index = self._active_tool_cards.get(event.call_id)
            current = self.tool_cards[index] if index is not None else None
            if self._tool_lifecycle_conflicts(event.call_id, event.name, event.arguments):
                self._set_conflicting_tool_card(event.call_id)
            else:
                status = (
                    current.status
                    if current is not None
                    and current.status not in {"done", "error", "denied", "cancelled"}
                    else "requested"
                )
                self._set_tool_card(
                    event.call_id,
                    event.name,
                    status,
                    arguments_available=True,
                    lifecycle_start=True,
                )
        elif isinstance(event, ToolApprovalResolved):
            if event.call_id in self._resolved_process_call_ids:
                self._touch_resolved_process_call(event.call_id)
                super().event(event)
                return
            if event.call_id in self._process_call_ids:
                if not event.approved:
                    self._mark_resolved_process_call(event.call_id)
                # Keep denied process calls as tombstones through settlement;
                # otherwise synthetic or duplicate results become generic cards.
                super().event(event)
                return
            if event.call_id not in self._active_tool_cards:
                super().event(event)
                return
            self._set_tool_card(
                event.call_id,
                event.name,
                "running" if event.approved else "denied",
                arguments_available=True,
                lifecycle_start=False,
            )
        elif isinstance(event, ToolResultReady):
            if event.call_id in self._resolved_process_call_ids:
                self._touch_resolved_process_call(event.call_id)
                super().event(event)
                return
            if event.call_id in self._process_call_ids:
                # Keep the resolved process binding as a trace tombstone so
                # delayed duplicate results stay ignored like Rust call entries.
                self._mark_resolved_process_call(event.call_id)
                super().event(event)
                return
            index = self._active_tool_cards.get(event.call_id)
            current = self.tool_cards[index] if index is not None else None
            status = tool_result_status(
                event.is_error,
                event.exit_code,
                process_state=event.process_state,
            )
            self._set_tool_card(
                event.call_id,
                event.name,
                status,
                arguments_available=current.arguments_available if current is not None else False,
                lifecycle_start=False,
            )
        elif (
            isinstance(event, RpcCommandFinished)
            and event.command_type == "prompt"
            and event.command_id == self._trace_current_command_id
        ):
            self._settle_tool_cards()
        super().event(event)

    def rpc_stream_ended_before_command(self, command_id: str) -> None:
        self._settle_tool_cards()
        super().rpc_stream_ended_before_command(command_id)

    def rpc_stream_ended_unexpectedly(self) -> None:
        self._settle_tool_cards()
        super().rpc_stream_ended_unexpectedly()

    def _settle_tool_cards(self) -> None:
        self._process_call_ids.clear()
        self._active_process_metadata.clear()
        for index, current in enumerate(self.tool_cards):
            if current.status in {"done", "error", "denied", "cancelled"}:
                continue
            self.tool_cards[index] = current.model_copy(update={"status": "cancelled"})

    def _set_tool_card(
        self,
        call_id: str,
        name: str,
        status: Literal[
            "requested",
            "awaiting_approval",
            "running",
            "done",
            "error",
            "denied",
            "cancelled",
        ],
        *,
        arguments_available: bool,
        lifecycle_start: bool,
    ) -> None:
        index = self._active_tool_cards.get(call_id)
        current = self.tool_cards[index] if index is not None else None
        if (
            lifecycle_start
            and current is not None
            and current.status
            in {
                "done",
                "error",
                "denied",
                "cancelled",
            }
        ):
            # A result carries no generation beyond call_id, so reopening a
            # terminal identity would let a delayed duplicate resolve the new
            # lifecycle. Mirror the Rust TUI's explicit untracked conflict card.
            index = None
            current = None
            status = "cancelled"
        elif (
            not lifecycle_start
            and current is not None
            and current.status
            in {
                "done",
                "error",
                "denied",
                "cancelled",
            }
        ):
            return
        projection = TraceToolCardProjection(
            call_id=_clip_trace_card_id(call_id),
            name=current.name
            if current is not None and not lifecycle_start
            else _clip_trace_card_field(name),
            status=status,
            arguments_available=arguments_available,
        )
        if index is None:
            self.tool_cards.append(projection)
            self._active_tool_cards[call_id] = len(self.tool_cards) - 1
        else:
            self.tool_cards[index] = projection

    def _tool_lifecycle_conflicts(self, call_id: str, name: str, arguments: object) -> bool:
        index = self._active_tool_cards.get(call_id)
        current = self.tool_cards[index] if index is not None else None
        if current is not None and current.status in {"done", "error", "denied", "cancelled"}:
            return False
        metadata = _canonical_trace_tool_metadata(name, arguments)
        previous = self._active_tool_metadata.get(call_id)
        if previous is None:
            self._active_tool_metadata[call_id] = metadata
            return False
        return previous != metadata

    def _set_conflicting_tool_card(self, call_id: str) -> None:
        index = self._active_tool_cards[call_id]
        current = self.tool_cards[index]
        self._set_tool_card(
            call_id,
            current.name,
            "error",
            arguments_available=current.arguments_available,
            lifecycle_start=False,
        )

    def _mark_resolved_process_call(self, call_id: str) -> None:
        self._process_call_ids.discard(call_id)
        self._active_process_metadata.pop(call_id, None)
        if call_id in self._resolved_process_call_ids:
            self._touch_resolved_process_call(call_id)
            return
        self._resolved_process_call_ids.add(call_id)
        self._resolved_process_call_order.append(call_id)
        self._resolved_process_call_bytes += len(call_id.encode())
        while (
            len(self._resolved_process_call_ids) > 1_024
            or self._resolved_process_call_bytes > 1024 * 1024
        ):
            oldest = self._resolved_process_call_order.popleft()
            if oldest not in self._resolved_process_call_ids:
                continue
            self._resolved_process_call_ids.remove(oldest)
            self._resolved_process_call_bytes -= len(oldest.encode())

    def _touch_resolved_process_call(self, call_id: str) -> None:
        self._resolved_process_call_order = deque(
            candidate for candidate in self._resolved_process_call_order if candidate != call_id
        )
        self._resolved_process_call_order.append(call_id)

    def _process_identity_was_resolved(self, call_id: str) -> bool:
        if call_id in self._resolved_process_call_ids:
            self._touch_resolved_process_call(call_id)
            return True
        index = self._active_tool_cards.get(call_id)
        return index is not None and self.tool_cards[index].status in {
            "done",
            "error",
            "denied",
            "cancelled",
        }

    def tool_card_projection(self) -> tuple[TraceToolCardProjection, ...]:
        return tuple(self.tool_cards)


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
                "before_entry_id": before_entry_id,
                "after_entry_id": after_entry_id,
                "entry_ids": list(entry_ids),
                "complete_structure": complete_structure,
                "full_content": full_content,
                "allow_during_prompt": allow_during_prompt,
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
        mode: AgentMode | None = None,
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
                "clear_effort": clear_effort,
                "auto_compaction_enabled": auto_compaction_enabled,
                "mode": mode,
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
    tool_cards: tuple[TraceToolCardProjection, ...]
    notices: tuple[str, ...]
    errors: tuple[str, ...]
    tokens: tuple[str, ...]


_TRACE_CARD_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _clip_trace_card_id(value: str) -> str:
    if _TRACE_CARD_ID_RE.fullmatch(value):
        return value
    return f"h-{hashlib.sha256(value.encode()).hexdigest()}"


def _clip_trace_card_field(value: str) -> str:
    if not value:
        return "(unnamed)"
    if len(value) <= 128:
        return value
    return f"{value[:127]}…"


def _canonical_trace_tool_metadata(name: str, arguments: object) -> tuple[str, str]:
    bounded_name = _clip_trace_card_field(name)
    if not isinstance(arguments, dict):
        bounded_arguments: dict[str, object] = {}
    else:
        keys_by_name: dict[str, tuple[str, ...]] = {
            "read": ("path", "offset", "limit"),
            "grep": (
                "pattern",
                "path",
                "glob",
                "ignore_case",
                "literal",
                "context",
                "max_results",
            ),
            "find": ("pattern", "path", "max_results"),
            "ls": ("path", "all"),
            "bash": (
                "operation",
                "command",
                "process_id",
                "wait_seconds",
                "lifetime_seconds",
                "yield_seconds",
            ),
            "edit": ("path",),
            "write": ("path",),
        }
        selected = keys_by_name.get(name)
        if selected is None:
            bounded_arguments = {}
            for key in sorted(arguments)[:8]:
                bounded_arguments[_clip_trace_metadata(str(key), 64)] = _bounded_trace_argument(
                    arguments[key], 64
                )
        else:
            bounded_arguments = {}
            for key in selected:
                if key not in arguments:
                    continue
                if key == "process_id" and isinstance(arguments[key], str):
                    value: object = _bounded_trace_internal_identity(arguments[key])
                else:
                    value = _bounded_trace_argument(
                        arguments[key], 200 if key == "command" else 256
                    )
                bounded_arguments[key] = value
    return (bounded_name, _trace_action_arguments(name, bounded_arguments))


def _trace_action_arguments(name: str, arguments: dict[str, object]) -> str:
    if name == "read":
        output = _trace_path_value(arguments, "path", "<path>")
        offset = _trace_positive_int(arguments.get("offset"))
        limit = _trace_positive_int(arguments.get("limit"))
        if offset is not None or limit is not None:
            start = offset or 1
            output += f":{start}-"
            if limit is not None:
                output += str(min(start + limit - 1, 2**64 - 1))
        return _clip_trace_metadata(output, 200)
    if name == "grep":
        pattern = _trace_string_value(arguments, "pattern", "")
        path = _trace_path_value(arguments, "path", ".")
        return _clip_trace_metadata(
            f"/{_clip_trace_metadata(_trace_one_line(pattern), 64)}/ in {path}", 200
        )
    if name == "find":
        pattern = _trace_string_value(arguments, "pattern", "*")
        path = _trace_path_value(arguments, "path", ".")
        return _clip_trace_metadata(
            f"{_clip_trace_metadata(_trace_one_line(pattern), 64)} in {path}", 200
        )
    if name == "ls":
        return _clip_trace_metadata(_trace_path_value(arguments, "path", "."), 200)
    if name == "bash":
        operation = _trace_string_value(arguments, "operation", "run")
        if operation in {"poll", "cancel"}:
            process_id = _trace_string_value(arguments, "process_id", "<process>")
            return _clip_trace_metadata(
                f"{operation} {_clip_trace_metadata(_trace_one_line(process_id), 64)}", 200
            )
        command = _trace_string_value(arguments, "command", "")
        if operation == "start":
            rendered = f"start {_clip_trace_metadata(_trace_one_line(command), 180)}"
        else:
            rendered = _clip_trace_metadata(_trace_one_line(command), 190)
        return _clip_trace_metadata(rendered, 200)
    if name in {"edit", "write"}:
        return _clip_trace_metadata(_trace_path_value(arguments, "path", "<path>"), 200)
    parts = [
        f"{_clip_trace_metadata(_trace_one_line(key), 32)}="
        f"{_clip_trace_metadata(_trace_scalar_value(arguments[key]), 64)}"
        for key in sorted(arguments)[:8]
    ]
    return _clip_trace_metadata(" ".join(parts), 160)


def _trace_path_value(arguments: dict[str, object], key: str, default: str) -> str:
    value = _trace_one_line(_trace_string_value(arguments, key, default))
    if len(value) <= 80:
        return value
    left = 39
    right = 40
    return f"{value[:left]}…{value[-right:]}"


def _trace_string_value(arguments: dict[str, object], key: str, default: str) -> str:
    value = arguments.get(key)
    return value if isinstance(value, str) else default


def _trace_positive_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**64 - 1
        else None
    )


def _trace_one_line(value: str) -> str:
    return " ".join(value.split())


def _trace_scalar_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return _trace_one_line(value)
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return f"{{{len(value)} fields}}"
    return str(value)


def _bounded_trace_argument(value: object, max_chars: int) -> object:
    if isinstance(value, str):
        return _clip_trace_metadata(value, max_chars)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return f"{{{len(value)} fields}}"
    return _clip_trace_metadata(str(value), max_chars)


def _clip_trace_metadata(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}…"


def _bounded_trace_internal_identity(value: str) -> str:
    encoded = value.encode()
    if len(encoded) <= 4 * 1024:
        return f"r{len(encoded)}:{value}"
    return f"h:{hashlib.sha256(encoded).hexdigest()}"


def _trace_process_identity(name: str, arguments: object) -> tuple[str, str] | None:
    if name != "bash" or not isinstance(arguments, dict):
        return None
    operation = arguments.get("operation")
    process_id = arguments.get("process_id")
    if (
        operation not in {"poll", "cancel"}
        or not isinstance(process_id, str)
        or not process_id.strip()
    ):
        return None
    return process_id, operation


def _trace_process_call(name: str, arguments: object) -> bool:
    return _trace_process_identity(name, arguments) is not None


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
        mode=snap.mode,
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
            self.shell.view.input_ready = v.input_ready
            # mode and last_session flow through shell-owned state so later
            # _sync_view() calls keep replaying the seeded values faithfully.
            self.shell.current_mode = v.mode
            self.shell.view.mode = v.mode
            if v.last_session is not None:
                self.shell.view.last_session = v.last_session
        if trace.initial.interaction is not None:
            inter = trace.initial.interaction
            try:
                self.shell.state.status = TuiStatus(inter.status)
            except ValueError as exc:  # defensive for programmatically constructed traces
                raise TraceReplayError(
                    f"invalid initial interaction status: {inter.status!r}"
                ) from exc
            self.shell.state.current_command_id = inter.current_command_id
            self.shell.state.current_command_type = inter.current_command_type
            # Seed matching pending requests so a following local.approve/
            # local.trust resolves the exact encoded target instead of failing.
            if inter.pending_approval_call_id is not None:
                self.shell.state.pending_approval = ToolApprovalRequested(
                    call_id=inter.pending_approval_call_id,
                    name="trace",
                    arguments={},
                    safety="read",
                )
            if inter.pending_trust_request_id is not None:
                self.shell.state.pending_trust = TrustRequested(
                    request_id=inter.pending_trust_request_id,
                    project_path=Path("/workspace"),
                )
            self.shell.state.cancel_requested = inter.cancel_requested
            self.shell.state.exit_requested = inter.exit_requested
        if trace.initial.view is not None:
            self.shell._sync_view()

    async def run(self) -> TraceRunResult:
        _reset_submission_ids()
        # Ensure shell starts from a clean snapshot appropriate for traces:
        # Mark input ready (post-hydration) unless trace says otherwise.
        if self.trace.initial.view is None:
            self.shell.view.input_ready = True
            if self.trace.initial.interaction is None:
                self.shell.state.status = TuiStatus.idle
            self.shell._sync_view()

        inputs = list(self.trace.inputs)
        for index, inp in enumerate(inputs):
            clock_ms: int = int(getattr(inp, "clock_ms", 0))
            self.clock.advance_to(clock_ms)

            if inp.type == "local.submit":
                # Build a submission with deterministic id.
                sid = new_submission_id()
                from wisp.tui.input_types import TuiSubmission

                sub = TuiSubmission(
                    id=SubmissionId(int(sid)),
                    content=inp.content,
                    display=inp.content,
                    input_mode="idle",
                )
                should_exit = await self.shell._handle_input_line(
                    _InputLine(text=sub, mode=_InputMode.idle)
                )
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
                should_exit = await self.shell._handle_input_line(
                    _InputLine(text=slash_text, mode=_InputMode.idle)
                )
            elif inp.type == "local.cancel":
                should_exit = await self.shell._handle_input_cancelled(
                    _InputCancelled(mode=_InputMode.idle)
                )
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
                should_exit = await self.shell._answer_pending_approval(
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
                should_exit = await self.shell._answer_pending_trust(
                    "",
                    trusted=inp.trusted,
                    reason=None if inp.trusted else "Denied from trace",
                    transient=bool(inp.transient) if not inp.trusted else False,
                )
            elif inp.type == "rpc.event":
                event = KnownWispEventAdapter.validate_python(inp.event)
                self.renderer._trace_current_command_id = self.shell.state.current_command_id
                should_exit = await self.shell._handle_rpc_event(event)
            elif inp.type == "rpc.closed":
                should_exit = self.shell._handle_rpc_closed(_RpcEventsClosed(error=inp.error))
            else:  # pragma: no cover - schema discriminator prevents this
                raise RuntimeError(f"unknown trace input type: {inp.type}")

            # The live loop terminates on the shell's exit signal; honor it by
            # stopping replay and rejecting impossible trailing transitions.
            if should_exit:
                remaining = len(inputs) - index - 1
                if remaining:
                    raise TraceReplayError(
                        f"{remaining} trailing input(s) after the shell reported an exit; "
                        "the live loop would have terminated before consuming them"
                    )
                break

        view = _view_projection(self.shell)
        interaction = _interaction_projection(self.shell)
        return TraceRunResult(
            commands=tuple(self.controller.commands),
            view=view,
            interaction=interaction,
            retained_text=self.renderer.retained_text,
            tool_cards=self.renderer.tool_card_projection(),
            notices=tuple(self.renderer.notices),
            errors=tuple(self.renderer.errors),
            tokens=tuple(self.renderer.tokens),
        )


async def run_trace(trace: TraceFile) -> TraceRunResult:
    runner = TraceRunner(trace)
    return await runner.run()
