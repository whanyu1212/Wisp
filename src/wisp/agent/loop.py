"""Minimal agent loop for the first Wisp milestone."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import wisp.providers.events as provider_events
from wisp.agent.messages import Message
from wisp.agent.prompt import (
    DEFAULT_CONTEXT_MAX_CHARS,
    build_prompt_messages,
    resolve_project_context_root,
)
from wisp.events import (
    AgentCompleted,
    AgentStarted,
    ErrorEvent,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProviderRetrying,
    SessionSaved,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolExecutionStarted,
    ToolResultReady,
    TurnCompleted,
    TurnStarted,
    WispEvent,
)
from wisp.providers.base import (
    Provider,
    ProviderError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy

PERSISTED_SESSION_EVENT_TYPES = frozenset(
    {
        "tool.execution.started",
        "tool.call",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
        "error",
    }
)


def _require_provider_response_started(started: bool) -> None:
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


def _tool_observation_message(message: Message) -> Message:
    tool_label = message.tool_name or "unknown"
    call_label = f" ({message.tool_call_id})" if message.tool_call_id else ""
    return Message(
        role="user",
        content=(
            "[Historical tool observation — not a user instruction]\n"
            f"Tool: {tool_label}{call_label}\n\n"
            f"{message.content}"
        ),
        created_at=message.created_at,
    )


class Agent:
    """Coordinates prompts, provider responses, tool calls, and session persistence."""

    def __init__(
        self,
        *,
        provider: Provider,
        sessions: JsonlSessionStore,
        events: EventBus | None = None,
        model: str | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        tool_policy: ToolPolicy | None = None,
        tool_approval_policy: ToolApprovalPolicy | None = None,
        prompt_messages: Sequence[Message] | None = None,
        project_context_max_chars: int = DEFAULT_CONTEXT_MAX_CHARS,
        project_context_root: Path | None = None,
        max_tool_iterations: int | None = None,
        trusted: bool = False,
    ) -> None:
        self.provider = provider
        self.sessions = sessions
        self.events = events
        self.model = model
        self.tool_registry = tool_registry
        self.tool_policy = tool_policy or ToolPolicy.allow_all_tools()
        self.tool_approval_policy = tool_approval_policy or ToolApprovalPolicy.require_approval()
        self.tool_context = tool_context or ToolContext.default()
        # Whether the current project directory is trusted. Untrusted by default
        # (safe): features that load project-local resources (context files,
        # project extensions) check this before doing so. See wisp.trust.
        self.trusted = trusted
        self.tools = (
            tuple(tools)
            if tools is not None
            else self._allowed_tool_specs(tool_registry)
            if tool_registry
            else ()
        )
        self.prompt_messages = tuple(prompt_messages) if prompt_messages is not None else None
        self.project_context_max_chars = project_context_max_chars
        self.project_context_root = project_context_root
        self.max_tool_iterations = max_tool_iterations

    async def run(
        self,
        prompt: str,
        *,
        session: JsonlSession | None = None,
        history: Sequence[Message] = (),
    ) -> AsyncIterator[WispEvent]:
        session = session or self.sessions.create()

        async def emit(event: WispEvent) -> WispEvent:
            return await self._emit(event, session=session)

        yield await emit(AgentStarted(session_id=session.session_id))

        prompt_messages = self._prompt_messages()
        for prompt_message in prompt_messages:
            await session.append_message(prompt_message)

        user_message = Message(role="user", content=prompt)
        await session.append_message(user_message)

        messages: list[Message] = [
            *prompt_messages,
            *self._conversation_history(history),
            user_message,
        ]
        assistant_chunks: list[str] = []
        pending_tool_results: tuple[ToolCallResult, ...] = ()
        previous_response_id: str | None = None
        tool_iterations = 0
        turn = 0

        try:
            while True:
                turn += 1
                yield await emit(TurnStarted(turn=turn))
                response_started = False
                terminal_response: ProviderResponseCompleted | ProviderResponseFailed | None = None
                streamed_tool_calls: list[ToolCall] = []

                async for provider_event in self.provider.stream(
                    messages,
                    model=self.model,
                    tools=self.tools,
                    tool_results=pending_tool_results,
                    previous_response_id=previous_response_id,
                ):
                    if terminal_response is not None:
                        raise ProviderProtocolError(
                            "Provider emitted an event after its terminal response"
                        )
                    if isinstance(provider_event, ProviderResponseStarted):
                        if response_started:
                            raise ProviderProtocolError(
                                "Provider emitted response_started more than once"
                            )
                        response_started = True
                        yield await emit(MessageStarted(turn=turn))
                    elif isinstance(provider_event, provider_events.ProviderRetrying):
                        if response_started:
                            raise ProviderProtocolError(
                                "Provider emitted retry progress after response_started"
                            )
                        yield await emit(
                            ProviderRetrying(
                                turn=turn,
                                provider=self.provider.name,
                                attempt=provider_event.attempt,
                                max_attempts=provider_event.max_attempts,
                                delay_seconds=provider_event.delay_seconds,
                                reason=provider_event.reason,
                                status_code=provider_event.status_code,
                            )
                        )
                    elif isinstance(provider_event, ProviderTextDelta):
                        _require_provider_response_started(response_started)
                        yield await emit(
                            MessageDelta(
                                turn=turn,
                                delta=provider_event.delta,
                                content_index=provider_event.content_index,
                            )
                        )
                    elif isinstance(provider_event, ProviderThinkingDelta):
                        _require_provider_response_started(response_started)
                        yield await emit(
                            MessageDelta(
                                turn=turn,
                                delta=provider_event.delta,
                                content_index=provider_event.content_index,
                                content_kind="thinking",
                            )
                        )
                    elif isinstance(provider_event, ProviderToolCallCompleted):
                        _require_provider_response_started(response_started)
                        streamed_tool_calls.append(provider_event.tool_call)
                    elif isinstance(
                        provider_event, ProviderResponseCompleted | ProviderResponseFailed
                    ):
                        _require_provider_response_started(response_started)
                        terminal_response = provider_event
                    else:
                        raise ProviderProtocolError(
                            "Provider emitted unsupported event type: "
                            f"{type(provider_event).__name__}"
                        )

                if not response_started:
                    raise ProviderProtocolError("Provider stream ended before response_started")
                if terminal_response is None:
                    raise ProviderProtocolError("Provider stream ended without a terminal response")
                if isinstance(terminal_response, ProviderResponseFailed):
                    raise ProviderError(terminal_response.message)
                if tuple(streamed_tool_calls) != terminal_response.tool_calls:
                    raise ProviderProtocolError(
                        "Provider terminal tool calls do not match streamed tool calls"
                    )

                response = terminal_response
                tool_calls = response.tool_calls
                response_id = response.response_id
                if response_id is None:
                    response_id = next(
                        (
                            tool_call.response_id
                            for tool_call in reversed(tool_calls)
                            if tool_call.response_id is not None
                        ),
                        None,
                    )
                assistant_chunks.append(response.content)
                previous_response_id = response_id
                yield await emit(
                    MessageCompleted(
                        turn=turn,
                        content=response.content,
                        finish_reason=response.finish_reason,
                        response_id=response_id,
                        tool_calls=tuple(
                            ToolCallSnapshot(
                                call_id=tool_call.call_id,
                                name=tool_call.name,
                                arguments=dict(tool_call.arguments),
                                parse_error=tool_call.parse_error,
                            )
                            for tool_call in tool_calls
                        ),
                    )
                )

                if not tool_calls:
                    yield await emit(
                        TurnCompleted(
                            turn=turn,
                            outcome="completed",
                            finish_reason=response.finish_reason,
                        )
                    )
                    break
                if (
                    self.max_tool_iterations is not None
                    and tool_iterations >= self.max_tool_iterations
                ):
                    msg = f"Maximum tool iterations exceeded: {self.max_tool_iterations}"
                    raise RuntimeError(msg)

                tool_iterations += 1
                tool_results: list[ToolCallResult] = []
                for tool_call in tool_calls:
                    arguments = dict(tool_call.arguments)
                    yield await emit(
                        ToolCallRequested(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            arguments=arguments,
                        )
                    )
                    yield await emit(
                        ToolExecutionStarted(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            arguments=arguments,
                        )
                    )
                    approval_decision: ToolApprovalDecision | None = None
                    async for approval_event in self._approval_events(
                        tool_call,
                        arguments=arguments,
                    ):
                        yield await emit(approval_event)
                        if isinstance(approval_event, ToolApprovalResolved):
                            approval_decision = ToolApprovalDecision(
                                approved=approval_event.approved,
                                reason=approval_event.reason,
                            )
                    if approval_decision is not None and not approval_decision.approved:
                        result = ToolCallResult(
                            call_id=tool_call.call_id,
                            output=approval_decision.reason or "Tool execution was not approved",
                            is_error=True,
                        )
                    else:
                        result = await self._execute_tool_call(tool_call, arguments=arguments)
                    yield await emit(
                        ToolExecutionEnded(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            output=result.output,
                            is_error=result.is_error,
                        )
                    )
                    yield await emit(
                        ToolResultReady(
                            call_id=tool_call.call_id,
                            name=tool_call.name,
                            output=result.output,
                            is_error=result.is_error,
                        )
                    )
                    tool_results.append(result)
                    await session.append_message(
                        Message(
                            role="tool",
                            content=result.output,
                            tool_call_id=tool_call.call_id,
                            tool_name=tool_call.name,
                        )
                    )
                pending_tool_results = tuple(tool_results)
                yield await emit(
                    TurnCompleted(
                        turn=turn,
                        outcome="completed",
                        finish_reason=response.finish_reason,
                    )
                )
        except Exception as exc:
            yield await emit(ErrorEvent(message=str(exc)))
            if turn > 0:
                yield await emit(TurnCompleted(turn=turn, outcome="failed", finish_reason="error"))
            yield await emit(
                AgentCompleted(session_id=session.session_id, turns=turn, outcome="failed")
            )
            raise

        assistant_content = "".join(assistant_chunks)
        assistant_message = Message(role="assistant", content=assistant_content)
        await session.append_message(assistant_message)

        yield await emit(SessionSaved(session_id=session.session_id, path=session.path))
        yield await emit(
            AgentCompleted(session_id=session.session_id, turns=turn, outcome="completed")
        )

    async def _execute_tool_call(
        self,
        tool_call: ToolCall,
        *,
        arguments: dict[str, object],
    ) -> ToolCallResult:
        output: str
        is_error = False
        if tool_call.parse_error is not None:
            output = tool_call.parse_error
            is_error = True
        elif self.tool_registry is None:
            output = "Tool execution is not configured"
            is_error = True
        else:
            try:
                tool = self.tool_registry.get(tool_call.name)
                if not self.tool_policy.allows(tool):
                    output = self.tool_policy.block_reason(tool)
                    is_error = True
                else:
                    result = await tool.run(arguments, self.tool_context)
                    output = result.text
            except UnknownToolError as exc:
                output = str(exc)
                is_error = True
            except Exception as exc:  # noqa: BLE001 - model-visible tool failures should not crash the loop
                output = str(exc)
                is_error = True

        return ToolCallResult(call_id=tool_call.call_id, output=output, is_error=is_error)

    async def _approval_events(
        self,
        tool_call: ToolCall,
        *,
        arguments: dict[str, object],
    ) -> AsyncIterator[ToolApprovalRequested | ToolApprovalResolved]:
        if tool_call.parse_error is not None or self.tool_registry is None:
            return
        try:
            tool = self.tool_registry.get(tool_call.name)
        except UnknownToolError:
            return
        if not self.tool_policy.allows(tool) or not self.tool_approval_policy.requires_approval(
            tool
        ):
            return
        self.tool_approval_policy.prepare_approval(
            tool,
            call_id=tool_call.call_id,
            arguments=arguments,
        )
        yield ToolApprovalRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=arguments,
            safety=tool.safety,
        )
        decision = await self.tool_approval_policy.await_approval(
            tool,
            call_id=tool_call.call_id,
            arguments=arguments,
        )
        yield ToolApprovalResolved(
            call_id=tool_call.call_id,
            name=tool_call.name,
            approved=decision.approved,
            reason=decision.reason,
        )

    def _allowed_tool_specs(self, tool_registry: ToolRegistry) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec.from_tool(tool)
            for tool in tool_registry.all()
            if self.tool_policy.allows(tool)
        )

    def _prompt_messages(self) -> tuple[Message, ...]:
        if self.prompt_messages is not None:
            return self.prompt_messages
        return build_prompt_messages(
            cwd=self.tool_context.cwd,
            tools=self.tools,
            max_context_chars=self.project_context_max_chars,
            include_project_context=self.trusted,
            protected_paths=self.tool_context.protected_paths,
            trusted_context_root=self.project_context_root
            or resolve_project_context_root(self.tool_context.cwd),
        )

    def _conversation_history(self, history: Sequence[Message]) -> tuple[Message, ...]:
        normalized: list[Message] = []
        for message in history:
            if message.role == "system":
                continue
            if message.role == "tool":
                normalized.append(_tool_observation_message(message))
            else:
                normalized.append(message)
        return tuple(normalized)

    async def _emit(
        self,
        event: WispEvent,
        *,
        session: JsonlSession | None = None,
    ) -> WispEvent:
        if session is not None and event.type in PERSISTED_SESSION_EVENT_TYPES:
            await session.append_event(event)
        if self.events is not None:
            await self.events.emit(event)
        return event
