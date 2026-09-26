"""Tool executors, request-boundary hooks, and streams shared by the agent loop tests."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable

from wisp.agent.request_boundary import (
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
)
from wisp.agent.tool_contracts import (
    ToolExecutionEvent,
)
from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolExecutionEnded,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ToolCall,
)


class NeverToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class RecordingToolExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        arguments = dict(tool_call.arguments)
        yield ToolApprovalRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=arguments,
            safety="command",
        )
        yield ToolApprovalResolved(
            call_id=tool_call.call_id,
            name=tool_call.name,
            approved=True,
        )
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="tool output",
            is_error=False,
            exit_code=0,
            process_id="proc-1",
            process_state="completed",
            stdout="tool stdout\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_dropped_bytes=0,
            stderr_dropped_bytes=0,
        )


class RecordingRequestBoundaryHook:
    """Records each boundary snapshot and replays scripted decisions in order."""

    def __init__(self, decisions: Iterable[RequestBoundaryDecision]) -> None:
        self._decisions = deque(decisions)
        self.snapshots: list[RequestBoundarySnapshot] = []

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        self.snapshots.append(snapshot)
        if not self._decisions:
            return RequestBoundaryDecision(stop=True)
        return self._decisions.popleft()


def completed_stream(
    content: str, *, response_id: str | None = "scripted-response"
) -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="test", response_id=response_id),
        ProviderTextDelta(delta=content),
        ProviderResponseCompleted(content=content, response_id=response_id),
    ]


class RaisingRequestBoundaryHook:
    """A hook whose `before_next_request` always raises."""

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        raise RuntimeError("boundary hook failed")
