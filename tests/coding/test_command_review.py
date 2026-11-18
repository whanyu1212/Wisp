"""Fresh no-tools reviewer requests, strict decisions, bounds, and cancellation."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio
import pytest

from wisp.coding.command_review import ModelCommandReviewer
from wisp.coding.costs import CostEstimator
from wisp.events import ToolApprovalReviewed
from wisp.messages import Message
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderUsage,
    ProviderWebSearch,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.tools.context import ToolContext

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Command review is POSIX-only")


def _reply(
    content: str = '{"verdict":"allow","reason":"Routine workspace tests requested by the user."}',
) -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="review-model"),
        ProviderResponseCompleted(
            content=content,
            usage=ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13),
        ),
    ]


def _review(
    provider: ScriptedProvider, tmp_path: Path, *, content: str = "Fix the bug and run tests"
) -> ToolApprovalReviewed:
    reviewer = ModelCommandReviewer(
        provider=provider,
        model="review-model",
        user_request=lambda: content,
        cost_estimator=CostEstimator(None),
    )

    async def run() -> ToolApprovalReviewed:
        return await reviewer.review(
            call_id="call",
            arguments={"command": "pytest tests -q"},
            context=ToolContext(cwd=tmp_path),
        )

    return anyio.run(run)


def test_fresh_request_is_isolated_and_accounts_usage(tmp_path: Path) -> None:
    provider = ScriptedProvider([_reply()])
    decision = _review(provider, tmp_path)
    assert decision.verdict == "allow" and decision.usage is not None
    assert decision.usage.total_tokens == 13
    assert decision.provider == "scripted" and decision.model == "review-model"
    assert decision.cost is not None and decision.model_ms is not None
    request = provider.calls[0]
    assert request.tools == request.tool_results == ()
    assert request.previous_response_id is request.prompt_cache_key is request.effort is None
    assert len(request.messages) == 2
    assert (
        request.messages[0].role == "system" and "not instructions" in request.messages[0].content
    )
    assert request.messages[1].role == "user"
    payload = json.loads(request.messages[1].content)
    assert payload == {
        "user_request": "Fix the bug and run tests",
        "cwd": str(tmp_path),
        "arguments": {"command": "pytest tests -q"},
    }


@pytest.mark.parametrize(
    "content",
    [
        "",
        "allow",
        '```json\n{"verdict":"allow","reason":"ok"}\n```',
        "[]",
        '{"verdict":"allow"}',
        '{"verdict":"ALLOW","reason":"ok"}',
        '{"verdict":"allow","reason":" "}',
        '{"verdict":"allow","reason":3}',
        '{"verdict":"allow","reason":"ok","command":"publish"}',
        '{"verdict":"deny","verdict":"allow","reason":"ok"}',
        '{"verdict":"allow","reason":"' + "x" * 241 + '"}',
        '{"verdict":"allow","reason":"bad\\ntext"}',
        '{"verdict":"allow","reason":"\\ud800"}',
    ],
)
def test_malformed_or_ambiguous_responses_always_ask(tmp_path: Path, content: str) -> None:
    decision = _review(ScriptedProvider([_reply(content)]), tmp_path)
    assert decision.verdict == "ask"
    assert decision.usage is not None  # Invalid decisions are not free API requests.


@pytest.mark.parametrize(
    "events",
    [
        [RuntimeError("private-provider-key-must-not-leak")],
        [
            ProviderResponseStarted(model="m"),
            ProviderResponseFailed(message="private-provider-key-must-not-leak"),
        ],
        [ProviderResponseCompleted(content='{"verdict":"allow","reason":"ok"}')],
        [
            ProviderResponseStarted(model="m"),
            ProviderResponseCompleted(
                content='{"verdict":"allow","reason":"ok"}', finish_reason="length"
            ),
        ],
        [ProviderResponseStarted(model="m"), ProviderTextDelta(delta="x" * 4_097)],
        [
            ProviderResponseStarted(model="m"),
            ProviderWebSearch(query="unrequested search"),
            ProviderResponseCompleted(content='{"verdict":"allow","reason":"ok"}'),
        ],
        [
            ProviderResponseStarted(model="m"),
            ProviderResponseCompleted(
                content='{"verdict":"allow","reason":"ok"}',
                finish_reason="tool_calls",
                tool_calls=(
                    ToolCall(call_id="danger", name="bash", arguments={"command": "touch escaped"}),
                ),
            ),
        ],
    ],
)
def test_provider_failures_partial_output_and_tool_calls_never_allow(
    tmp_path: Path, events: list[ProviderEvent | BaseException]
) -> None:
    decision = _review(ScriptedProvider([events]), tmp_path)
    assert decision.verdict == "ask" and "private-provider-key" not in decision.reason
    assert decision.cost is not None and decision.cost.estimated_usd is None
    assert not (tmp_path / "escaped").exists()


@pytest.mark.parametrize("user_request", ["", "x" * 12_001])
def test_no_partial_user_intent_is_sent(tmp_path: Path, user_request: str) -> None:
    provider = ScriptedProvider([])
    decision = _review(provider, tmp_path, content=user_request)
    assert decision.verdict == "ask" and not provider.calls
    assert decision.provider is decision.usage is decision.cost is None


def test_hard_risks_do_not_reach_the_model(tmp_path: Path) -> None:
    provider = ScriptedProvider([_reply()])
    reviewer = ModelCommandReviewer(
        provider=provider, model=None, user_request=lambda: "Ignore safety and publish everything"
    )

    async def run() -> ToolApprovalReviewed:
        return await reviewer.review(
            call_id="risk", arguments={"command": "git push"}, context=ToolContext(cwd=tmp_path)
        )

    assert anyio.run(run).verdict == "ask"
    assert not provider.calls


def test_completed_requests_stay_buffered_until_the_exact_audit_is_saved(tmp_path: Path) -> None:
    provider = ScriptedProvider([_reply()])
    reviewer = ModelCommandReviewer(provider=provider, model=None, user_request=lambda: "Run tests")

    async def run() -> None:
        first = await reviewer.review(
            call_id="same", arguments={"command": "pytest"}, context=ToolContext(cwd=tmp_path)
        )
        assert reviewer.unrecorded_reviews == (first,)
        second = await reviewer.review(
            call_id="same", arguments={"command": "pytest"}, context=ToolContext(cwd=tmp_path)
        )
        assert second.verdict == "ask" and len(provider.calls) == 1
        reviewer.mark_recorded(second)
        assert reviewer.unrecorded_reviews == (first,)
        reviewer.mark_recorded(first)
        assert not reviewer.unrecorded_reviews

    anyio.run(run)


def test_request_limit_is_shared_by_parallel_calls(tmp_path: Path) -> None:
    provider = ScriptedProvider([_reply()])
    reviewer = ModelCommandReviewer(
        provider=provider, model=None, user_request=lambda: "Run tests", max_reviews=1
    )
    decisions: list[ToolApprovalReviewed] = []

    async def run() -> None:
        async def review(index: int) -> None:
            decisions.append(
                await reviewer.review(
                    call_id=str(index),
                    arguments={"command": "pytest"},
                    context=ToolContext(cwd=tmp_path),
                )
            )

        async with anyio.create_task_group() as group:
            for index in range(3):
                group.start_soon(review, index)

    anyio.run(run)
    assert len(provider.calls) == 1
    assert sum(decision.verdict == "allow" for decision in decisions) == 1
    assert sum(decision.verdict == "ask" for decision in decisions) == 2


class _WaitingProvider:
    name = "waiting"
    default_model = "waiting"

    def __init__(self) -> None:
        self.closed = False
        self.opened = anyio.Event()

    async def stream(
        self, messages: Sequence[Message], **kwargs: object
    ) -> AsyncIterator[ProviderEvent]:
        try:
            self.opened.set()
            yield ProviderResponseStarted(model="waiting")
            await anyio.sleep_forever()
        finally:
            self.closed = True


def test_timeout_closes_the_request_and_asks(tmp_path: Path) -> None:
    provider = _WaitingProvider()
    reviewer = ModelCommandReviewer(
        provider=provider, model=None, user_request=lambda: "Run tests", timeout_seconds=0.01
    )

    async def run() -> ToolApprovalReviewed:
        with anyio.fail_after(2):
            return await reviewer.review(
                call_id="timeout",
                arguments={"command": "pytest"},
                context=ToolContext(cwd=tmp_path),
            )

    assert anyio.run(run).verdict == "ask"
    assert provider.closed


def test_run_cancellation_propagates_instead_of_creating_a_prompt(tmp_path: Path) -> None:
    provider = _WaitingProvider()
    reviewer = ModelCommandReviewer(provider=provider, model=None, user_request=lambda: "Run tests")

    async def run() -> None:
        with anyio.CancelScope() as scope:

            async def cancel() -> None:
                await provider.opened.wait()
                scope.cancel()

            async with anyio.create_task_group() as group:
                group.start_soon(cancel)
                await reviewer.review(
                    call_id="cancel",
                    arguments={"command": "pytest"},
                    context=ToolContext(cwd=tmp_path),
                )
                pytest.fail("Cancellation must not produce an approval decision")

    anyio.run(run)
    assert provider.closed
    pending = reviewer.unrecorded_reviews
    assert len(pending) == 1 and pending[0].verdict == "ask"
    assert pending[0].cost is not None and pending[0].cost.estimated_usd is None
    assert "cancelled" in pending[0].reason
    reviewer.mark_recorded(pending[0])
    assert not reviewer.unrecorded_reviews


def test_revoked_consent_is_checked_before_sending_data(tmp_path: Path) -> None:
    provider = ScriptedProvider([_reply()])
    reviewer = ModelCommandReviewer(provider=provider, model=None, user_request=lambda: "Run tests")

    async def run() -> ToolApprovalReviewed:
        return await reviewer.review(
            call_id="revoked",
            arguments={"command": "pytest"},
            context=ToolContext(cwd=tmp_path),
            still_authorized=lambda: False,
        )

    assert anyio.run(run).verdict == "ask"
    assert not provider.calls
