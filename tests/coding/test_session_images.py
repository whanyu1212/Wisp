from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.coding.session import CodingSession, ImageInputUnsupportedError
from wisp.events import QueueMessageInjected, WispEvent
from wisp.images import ImageRejectedError
from wisp.messages import Message
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted
from wisp.providers.fake import ProviderRequest, ScriptedProvider
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


def _registry(*, image_models: tuple[str, ...]) -> ModelRegistry:
    return ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=(
                ModelCatalogProviderEntry(
                    name="scripted",
                    display_name="Scripted",
                    default_model="scripted",
                    docs_url="https://example.com",
                    models=("scripted",),
                    image_input_models=image_models,
                ),
            ),
        )
    )


def _user_messages(request: ProviderRequest) -> list[Message]:
    return [message for message in request.messages if message.role == "user"]


def test_provider_receives_image_bytes(tmp_path: Path) -> None:
    """A new prompt's bytes reach the provider, and a later run reloads them."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    provider = _answers(2)
    agent = CodingSession(
        provider=provider, sessions=store, models=_registry(image_models=("scripted",))
    )

    async def two_runs() -> None:
        async for _ in agent.run("look", images=[PNG], session=session):
            pass
        async for _ in agent.run("again", session=session):
            pass

    anyio.run(two_runs)

    first, resumed = (_user_messages(request)[0] for request in provider.calls)
    assert first.image_bytes == (PNG,)
    assert resumed.images == first.images
    assert resumed.image_bytes == (PNG,)
    (line, *_) = session.path.read_text(encoding="utf-8").splitlines()
    assert "image_bytes" not in line


@pytest.mark.parametrize("image_models", [(), None], ids=["not-in-catalog", "adapter-opt-out"])
def test_unsupported_model_refuses_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_models: tuple[str, ...] | None
) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    provider = _answers(1)
    if image_models is None:
        monkeypatch.setattr(provider, "supports_image_input", False)
    agent = CodingSession(
        provider=provider,
        sessions=store,
        models=_registry(image_models=image_models or ()),
    )

    async def run() -> None:
        async for _ in agent.run("look", images=[PNG], session=session):
            pass

    with pytest.raises(ImageInputUnsupportedError, match="does not accept images"):
        anyio.run(run)

    assert provider.calls == []
    assert not session.path.exists()
    assert not session.images.directory.exists()


def test_history_images_become_placeholders(tmp_path: Path) -> None:
    """Switching to a model without image input sends earlier images as placeholders."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    provider = _answers(2)
    agent = CodingSession(
        provider=provider, sessions=store, models=_registry(image_models=("scripted",))
    )

    async def run(prompt: str, images: list[bytes]) -> None:
        async for _ in agent.run(prompt, images=images, session=session):
            pass

    anyio.run(run, "look", [PNG])
    agent.models = _registry(image_models=())
    anyio.run(run, "again", [])

    resumed = _user_messages(provider.calls[1])[0]
    assert resumed.images is not None
    assert resumed.image_bytes is None


def test_missing_image_does_not_fail(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    provider = _answers(2)
    agent = CodingSession(provider=provider, sessions=store)

    async def run(prompt: str, images: list[bytes]) -> None:
        async for _ in agent.run(prompt, images=images, session=session):
            pass

    anyio.run(run, "look", [PNG])
    for stored in session.images.directory.iterdir():
        stored.unlink()
    anyio.run(run, "again", [])

    resumed = _user_messages(provider.calls[1])[0]
    assert resumed.images is not None
    assert resumed.image_bytes is None
    assert "Sending image placeholders" in caplog.text


def test_budget_keeps_newest_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the per-request image budget, older images go as placeholders."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    provider = _answers(3)
    # Room for two of the three images: the newest prompt's and one before it.
    monkeypatch.setattr(provider, "max_request_image_bytes", len(PNG) + len(JPEG), raising=False)
    agent = CodingSession(provider=provider, sessions=store)

    async def run(prompt: str, image: bytes) -> None:
        async for _ in agent.run(prompt, images=[image], session=session):
            pass

    anyio.run(run, "first", PNG)
    anyio.run(run, "second", JPEG)
    anyio.run(run, "third", PNG)

    last_request = _user_messages(provider.calls[2])
    assert [message.image_bytes for message in last_request] == [None, (JPEG,), (PNG,)]


def test_queued_images_follow_model(tmp_path: Path) -> None:
    """A queue kept from a failed run is restored without bytes for a text-only model."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    event_bus = EventBus()
    provider = ScriptedProvider(
        [
            [RuntimeError("provider failed")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="one")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="two")],
        ]
    )
    agent = CodingSession(
        provider=provider,
        sessions=store,
        events=event_bus,
        models=_registry(image_models=("scripted",)),
    )
    queued_once: list[bool] = []

    async def queue_once(event: WispEvent) -> None:
        if not queued_once:
            queued_once.append(True)
            await agent.follow_up("later", images=[PNG])

    event_bus.on("agent.started", queue_once)

    async def run(prompt: str) -> None:
        async for _ in agent.run(prompt, session=session):
            pass

    with pytest.raises(RuntimeError, match="provider failed"):
        anyio.run(run, "look")
    assert agent.queue_state(session).follow_up == ("later",)
    agent.models = _registry(image_models=())
    anyio.run(run, "next")

    (queued,) = [
        message for message in _user_messages(provider.calls[-1]) if message.content == "later"
    ]
    assert queued.images is not None
    assert queued.image_bytes is None


def test_oversized_prompt_images_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new message's own images must fit one request; they are not trimmed."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    provider = _answers(1)
    monkeypatch.setattr(provider, "max_request_image_bytes", len(PNG), raising=False)
    agent = CodingSession(provider=provider, sessions=store)

    async def run() -> None:
        async for _ in agent.run("look", images=[PNG, JPEG], session=session):
            pass

    with pytest.raises(ImageInputUnsupportedError, match="Attach fewer or smaller images"):
        anyio.run(run)

    assert provider.calls == []
    assert not session.path.exists()
    assert not session.images.directory.exists()
