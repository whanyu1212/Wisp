from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.coding.session import CodingSession
from wisp.events import QueueMessageInjected, WispEvent
from wisp.images import ImageRejectedError
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.jsonl import JsonlSessionStore

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
PNG = (_FIXTURES / "pixel.png").read_bytes()
JPEG = (_FIXTURES / "pixel.jpg").read_bytes()


def _answers(count: int) -> ScriptedProvider:
    return ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content=f"answer {index}"),
            ]
            for index in range(count)
        ]
    )


def test_steering_keeps_its_images(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    event_bus = EventBus()
    agent = CodingSession(provider=_answers(2), sessions=store, events=event_bus)

    async def steer_with_image(event: WispEvent) -> None:
        await agent.steer("and this one?", images=[JPEG])

    event_bus.on("agent.started", steer_with_image)

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("look", images=[PNG], session=session)]

    events = anyio.run(run)

    prompt, steered = (message for message in session.read_messages() if message.role == "user")
    assert prompt.images is not None and steered.images is not None
    assert [image.media_type for image in (*prompt.images, *steered.images)] == [
        "image/png",
        "image/jpeg",
    ]
    (injected,) = [event for event in events if isinstance(event, QueueMessageInjected)]
    assert injected.images == steered.images
    assert anyio.run(session.read_image, steered.images[0]) == JPEG


def test_rejected_image_starts_nothing(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    agent = CodingSession(provider=_answers(1), sessions=store)

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("look", images=[PNG[:-12]], session=session)]

    with pytest.raises(ImageRejectedError):
        anyio.run(run)

    assert not session.path.exists()
