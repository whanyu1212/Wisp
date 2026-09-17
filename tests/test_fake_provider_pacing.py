from __future__ import annotations

import pytest

from wisp.agent.messages import Message
from wisp.providers import fake as fake_module
from wisp.providers.events import ProviderResponseCompleted, ProviderTextDelta


@pytest.mark.asyncio
async def test_fake_provider_paces_each_word_without_changing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setenv("WISP_FAKE_STREAM_INTERVAL_MS", "25")
    monkeypatch.delenv("WISP_FAKE_RESPONSE_SUFFIX", raising=False)
    monkeypatch.setattr(fake_module.anyio, "sleep", record_sleep)
    events = [
        event
        async for event in fake_module.FakeProvider().stream(
            (Message(role="user", content="alpha beta"),)
        )
    ]

    deltas = [event.delta for event in events if isinstance(event, ProviderTextDelta)]
    assert "".join(deltas) == "fake response to: alpha beta"
    assert delays == [0.025] * len(deltas)
    assert isinstance(events[-1], ProviderResponseCompleted)
    assert events[-1].content == "".join(deltas)


@pytest.mark.asyncio
async def test_fake_provider_can_append_response_only_benchmark_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_FAKE_RESPONSE_SUFFIX", "BENCHMARK_RESPONSE_DONE")

    events = [
        event
        async for event in fake_module.FakeProvider().stream(
            (Message(role="user", content="alpha"),)
        )
    ]

    deltas = [event.delta for event in events if isinstance(event, ProviderTextDelta)]
    assert "".join(deltas) == "fake response to: alpha BENCHMARK_RESPONSE_DONE"
    assert isinstance(events[-1], ProviderResponseCompleted)
    assert events[-1].content == "".join(deltas)


@pytest.mark.parametrize("value", ["-1", "not-a-number"])
def test_fake_provider_rejects_invalid_pacing(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("WISP_FAKE_STREAM_INTERVAL_MS", value)

    with pytest.raises(ValueError, match="nonnegative integer"):
        fake_module.FakeProvider._stream_interval_seconds()
