"""Command review grants only the exact call and preserves other approval boundaries."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import anyio
import pytest

from tests.agent.loop.support import tool_call_stream
from wisp.coding.command_review import ModelCommandReviewer
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    AgentCompleted,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolApprovalReviewed,
    ToolExecutionEnded,
    WispEvent,
)
from wisp.messages import Message
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult
from wisp.tools.shell.tool import BashTool

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Command review is POSIX-only")


def _review_stream(verdict: str = "allow") -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="review-model"),
        ProviderResponseCompleted(
            content=f'{{"verdict":"{verdict}","reason":"Requested workspace tests."}}',
            usage=ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13),
        ),
    ]


def _provider(verdict: str = "allow") -> ScriptedProvider:
    return ScriptedProvider([_review_stream(verdict)])


def _executor(
    tmp_path: Path,
    provider: ScriptedProvider,
    policy: ToolApprovalPolicy,
    *,
    tool: Tool | None = None,
) -> ConfiguredToolExecutor:
    registry = ToolRegistry()
    registry.register(tool or BashTool())
    return ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=policy,
        command_reviewer=ModelCommandReviewer(
            provider=provider, model=None, user_request=lambda: "Fix the bug and run tests"
        ),
    )


def _call(command: str = "pytest tests -q") -> ToolCall:
    return ToolCall(call_id="command", name="bash", arguments={"command": command})


@pytest.fixture
def executions(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def run(
        self: BashTool, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolResult:
        calls.append(dict(arguments))
        return ToolResult(text="tests completed")

    monkeypatch.setattr(BashTool, "run", run)
    return calls


@pytest.mark.parametrize("verdict", ["allow", "ask", "deny"])
def test_review_decision_precedes_execution_and_never_grants_future_calls(
    tmp_path: Path, executions: list[dict[str, object]], verdict: str
) -> None:
    policy = ToolApprovalPolicy.auto_commands()
    executor = _executor(tmp_path, _provider(verdict), policy)

    async def run() -> list[WispEvent]:
        events: list[WispEvent] = []
        async for event in executor.execute(_call()):
            if not isinstance(event, ToolExecutionEnded):
                assert not executions
            events.append(event)
        return events

    events = anyio.run(run)
    assert isinstance(events[0], ToolApprovalReviewed) and events[0].verdict == verdict
    requested = [event for event in events if isinstance(event, ToolApprovalRequested)]
    assert bool(requested) is (verdict == "ask")
    result = events[-1]
    assert isinstance(result, ToolExecutionEnded) and result.is_error is (verdict != "allow")
    assert bool(executions) is (verdict == "allow")
    assert (
        not policy.approves(BashTool()) and not policy.approved_tools and not policy.approved_safety
    )
    if verdict != "ask":
        resolved = next(event for event in events if isinstance(event, ToolApprovalResolved))
        assert resolved.source == "auto" and resolved.rule_id == "command_review_v1"


@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_existing_consent_never_widens_to_commands(
    tmp_path: Path, executions: list[dict[str, object]], mode: str
) -> None:
    provider = _provider()
    policy = (
        ToolApprovalPolicy.auto_edits() if mode == "auto" else ToolApprovalPolicy.require_approval()
    )
    executor = _executor(tmp_path, provider, policy)

    async def run() -> list[WispEvent]:
        return [event async for event in executor.execute(_call())]

    events = anyio.run(run)
    assert isinstance(events[0], ToolApprovalRequested)
    assert not provider.calls and not executions


@pytest.mark.parametrize(
    "command", ["git push origin main", "pytest && git push", "pytest .env", "python -c unsafe"]
)
def test_deterministic_gate_cannot_be_overridden_by_an_allow_reply(
    tmp_path: Path, executions: list[dict[str, object]], command: str
) -> None:
    provider = _provider()
    executor = _executor(tmp_path, provider, ToolApprovalPolicy.auto_commands())

    async def run() -> list[WispEvent]:
        return [event async for event in executor.execute(_call(command))]

    events = anyio.run(run)
    assert isinstance(events[0], ToolApprovalReviewed) and events[0].verdict == "ask"
    assert isinstance(events[1], ToolApprovalRequested)
    assert not provider.calls and not executions


class _ReplacedBash(BashTool):
    pass


def test_only_the_exact_builtin_can_qualify(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    provider = _provider()
    executor = _executor(
        tmp_path, provider, ToolApprovalPolicy.auto_commands(), tool=_ReplacedBash()
    )

    async def run() -> list[WispEvent]:
        return [event async for event in executor.execute(_call())]

    assert isinstance(anyio.run(run)[0], ToolApprovalRequested)
    assert not provider.calls and not executions


def test_explicit_grant_during_ask_review_still_settles_the_lifecycle(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    from wisp.agent.loop.tool_lifecycle import ToolExecutionLifecycle

    policy = ToolApprovalPolicy.auto_commands()
    executor = _executor(tmp_path, _provider("ask"), policy)

    async def run() -> None:
        lifecycle = ToolExecutionLifecycle(_call())
        async for event in executor.execute(_call()):
            lifecycle.accept(event)
            if isinstance(event, ToolApprovalReviewed):
                policy.approved_tools = frozenset({"bash"})
        assert not lifecycle.finish().is_error

    anyio.run(run)
    assert len(executions) == 1


def test_revocation_after_assessment_publication_stops_the_unissued_grant(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    policy = RpcToolApprovalPolicy(ToolApprovalPolicy.auto_commands(), project_path=tmp_path)
    executor = _executor(tmp_path, _provider(), policy)

    async def run() -> list[WispEvent]:
        events: list[WispEvent] = []
        async for event in executor.execute(_call()):
            events.append(event)
            if isinstance(event, ToolApprovalReviewed):
                policy.set_permissions("ask")
                # Re-enabling the same mode must not revive an old assessment.
                policy.set_permissions("auto_commands")
            if isinstance(event, ToolApprovalRequested):
                policy.resolve_approval(call_id=event.call_id, approved=False)
        return events

    events = anyio.run(run)
    assert not any(isinstance(event, ToolApprovalRequested) for event in events)
    assert not any(isinstance(event, ToolApprovalResolved) and event.approved for event in events)
    assert isinstance(events[-1], ToolExecutionEnded) and events[-1].is_error
    assert not executions


def test_mandatory_confirmation_never_calls_the_reviewer(
    tmp_path: Path,
    executions: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    tool = BashTool()
    monkeypatch.setattr(tool, "confirm_every_call", True, raising=False)
    executor = _executor(tmp_path, provider, ToolApprovalPolicy.auto_commands(), tool=tool)

    async def run() -> list[WispEvent]:
        return [event async for event in executor.execute(_call())]

    events = anyio.run(run)
    assert isinstance(events[0], ToolApprovalRequested) and not events[0].grants_allowed
    assert not provider.calls and not executions


def test_subagent_restrictions_precede_command_review(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    provider = _provider()
    executor = _executor(tmp_path, provider, ToolApprovalPolicy.auto_commands())

    async def run() -> None:
        result = await executor.run_nested(_call())
        assert result.is_error and "read-only" in result.text

    anyio.run(run)
    assert not provider.calls and not executions


def test_bare_policy_or_missing_nested_channel_never_grants_commands(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    provider = _provider()
    executor = _executor(tmp_path, provider, ToolApprovalPolicy.auto_commands())

    async def run() -> None:
        result = await executor.run_codemode(_call())
        assert result.is_error

    anyio.run(run)
    assert not provider.calls and not executions


def test_missing_reviewer_keeps_the_ordinary_flow_without_an_assessment(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(BashTool())
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.auto_commands(),
    )

    async def run() -> list[WispEvent]:
        return [event async for event in executor.execute(_call())]

    events = anyio.run(run)
    assert isinstance(events[0], ToolApprovalRequested)
    assert not any(isinstance(event, ToolApprovalReviewed) for event in events)


def test_proxied_calls_cannot_qualify_even_if_the_target_is_a_builtin(tmp_path: Path) -> None:
    provider = _provider()
    executor = _executor(tmp_path, provider, ToolApprovalPolicy.auto_commands())

    async def run() -> None:
        evaluation = await executor._automatic_approval(
            _call(),
            BashTool(),
            _call().arguments,
            "bash",
        )
        assert evaluation.review is None and evaluation.resolution is None

    anyio.run(run)
    assert not provider.calls


def test_full_session_review_usage_and_reason_survive_reload(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    provider = ScriptedProvider(
        [
            tool_call_stream(_call()),
            [
                ProviderResponseStarted(model="review-model"),
                ProviderResponseCompleted(
                    content='{"verdict":"allow","reason":"Workspace test requested."}',
                    usage=ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13),
                ),
            ],
            [
                ProviderResponseStarted(model="main-model"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    store = JsonlSessionStore(tmp_path / "sessions")
    session = CodingSession(
        provider=provider,
        sessions=store,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.auto_commands(),
    )

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("Run workspace tests")]

    events = anyio.run(run)
    assert not any(isinstance(event, ToolApprovalRequested) for event in events)
    review = next(event for event in events if isinstance(event, ToolApprovalReviewed))
    assert review.usage is not None and review.usage.total_tokens == 13
    completed = next(event for event in events if isinstance(event, AgentCompleted))
    assert completed.output_tokens == 3
    persisted = store.latest()
    assert persisted is not None
    snapshot = next(
        message for message in persisted.read_message_page().messages if message.role == "tool"
    )
    assert snapshot.auto_approved and snapshot.approval_note is not None
    assert "Workspace test requested" in snapshot.approval_note
    # One independent request, not the main transcript or the tool's continuation.
    request = provider.calls[1]
    assert (
        request.tools == () and len(request.messages) == 2 and request.previous_response_id is None
    )
    assert len(executions) == 1
    stats = anyio.run(session.get_session_stats, persisted)
    assert stats.usage_record_count == 1 and stats.usage.total_tokens == 13
    assert stats.cost is not None and stats.cost.unpriced_record_count == 3


def test_nested_review_has_one_durable_cost_and_restored_child_reason(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    pytest.importorskip("pydantic_monty")
    from tests.coding.test_codemode import _done, _script

    provider = ScriptedProvider(
        [
            _script('await bash(command="pytest tests -q")'),
            _review_stream(),
            _done(),
        ]
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    store = JsonlSessionStore(tmp_path / "sessions")
    timeline: list[WispEvent] = []

    async def live(event: WispEvent) -> None:
        timeline.append(event)

    session = CodingSession(
        provider=provider,
        sessions=store,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.auto_commands(),
        codemode=True,
    )

    async def run() -> list[WispEvent]:
        return [event async for event in session.run("Run tests", live_event_sink=live)]

    events = anyio.run(run)
    assert not any(isinstance(event, ToolApprovalRequested) for event in [*events, *timeline])
    persisted = store.latest()
    assert persisted is not None
    snapshot = next(
        message for message in persisted.read_message_page().messages if message.role == "tool"
    )
    assert snapshot.tool_result is not None and snapshot.tool_result.nested_calls
    child = snapshot.tool_result.nested_calls[0]
    assert child.auto_approved and child.approval_note is not None
    assert "command review" in child.approval_note
    reviews = [
        entry
        for entry in persisted.read_entries()
        if entry.kind == "event" and entry.event.payload.get("type") == "tool.approval.reviewed"
    ]
    assert len(reviews) == 1
    assert anyio.run(session.get_session_stats, persisted).usage.total_tokens == 13
    assert len(executions) == 1


class _WaitingReviewProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([tool_call_stream(_call())])
        self.opened = anyio.Event()
        self.review_closed = False
        self.requests = 0

    async def stream(
        self, messages: Sequence[Message], **kwargs: object
    ) -> AsyncIterator[ProviderEvent]:
        self.requests += 1
        if self.requests == 2:
            try:
                self.opened.set()
                yield ProviderResponseStarted(model="review-model")
                await anyio.sleep_forever()
            finally:
                self.review_closed = True
        else:
            async for event in super().stream(messages, **kwargs):
                yield event


def test_cancelled_review_is_durably_unpriced_not_silently_free(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    provider = _WaitingReviewProvider()
    registry = ToolRegistry()
    registry.register(BashTool())
    store = JsonlSessionStore(tmp_path / "sessions")
    session = CodingSession(
        provider=provider,
        sessions=store,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.auto_commands(),
    )

    async def run() -> list[WispEvent]:
        async def cancel() -> None:
            await provider.opened.wait()
            assert session.cancel_run()

        async with anyio.create_task_group() as group:
            group.start_soon(cancel)
            events = [event async for event in session.run("Run tests")]
        return events

    events = anyio.run(run)
    assert any(
        isinstance(event, AgentCompleted) and event.outcome == "cancelled" for event in events
    )
    assert provider.review_closed and not executions
    persisted = store.latest()
    assert persisted is not None
    reviews = [
        entry.event.payload
        for entry in persisted.read_entries()
        if entry.kind == "event" and entry.event.payload.get("type") == "tool.approval.reviewed"
    ]
    assert len(reviews) == 1 and "cancelled" in str(reviews[0]["reason"])
    stats = anyio.run(session.get_session_stats, persisted)
    assert (
        stats.cost is not None and not stats.cost.complete and stats.cost.unpriced_record_count == 2
    )


def test_audit_delivery_failure_cannot_execute_or_double_count_the_request(
    tmp_path: Path, executions: list[dict[str, object]]
) -> None:
    provider = ScriptedProvider([tool_call_stream(_call()), _review_stream()])
    registry = ToolRegistry()
    registry.register(BashTool())
    bus = EventBus()

    def fail(event: WispEvent) -> None:
        raise RuntimeError("audit delivery failed")

    bus.on("tool.approval.reviewed", fail)
    store = JsonlSessionStore(tmp_path / "sessions")
    session = CodingSession(
        provider=provider,
        sessions=store,
        events=bus,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
        tool_policy=ToolPolicy.allow_all_tools(),
        tool_approval_policy=ToolApprovalPolicy.auto_commands(),
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="audit delivery failed"):
            async for _ in session.run("Run tests"):
                pass

    anyio.run(run)
    assert not executions
    persisted = store.latest()
    assert persisted is not None
    reviews = [
        entry
        for entry in persisted.read_entries()
        if entry.kind == "event" and entry.event.payload.get("type") == "tool.approval.reviewed"
    ]
    assert len(reviews) == 1
    assert anyio.run(session.get_session_stats, persisted).usage.total_tokens == 13
