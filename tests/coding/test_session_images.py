from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import pytest

from wisp.coding.session import CodingSession, ImageInputUnsupportedError
from wisp.events import QueueMessageInjected, QueueUpdated, ToolResultReady, WispEvent
from wisp.images import ImageRejectedError, inspect_image
from wisp.messages import Message
from wisp.providers.base import ToolCallResult
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ProviderRequest, ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.builtin import ReadTool
from wisp.tools.context import ToolContext

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

    queued: list[QueueUpdated] = []

    async def steer_with_image(event: WispEvent) -> None:
        queued.append(await agent.steer("and this one?", images=[JPEG]))

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
    (update,) = queued
    assert update.steering_images == (steered.images,)
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
    retained = agent.queue_state(session)
    assert retained.follow_up == ("later",)
    assert [image.sha256 for (image,) in retained.follow_up_images] == [inspect_image(PNG).sha256]
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


def _read_twice(
    tmp_path: Path,
    *,
    image_models: tuple[str, ...],
    budget: int | None = None,
    native_tool_history: bool = False,
) -> tuple[ScriptedProvider, list[WispEvent], Path]:
    """Run a turn where the model reads an image, then a second turn in the session."""

    project = tmp_path / "project"
    project.mkdir()
    (project / "shot.png").write_bytes(PNG)
    read = ToolCall(call_id="call-1", name="read", arguments={"path": "shot.png"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=read),
                ProviderResponseCompleted(
                    content="", tool_calls=(read,), finish_reason="tool_calls"
                ),
            ],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="seen")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="again")],
        ]
    )
    if budget is not None:
        provider.max_request_image_bytes = budget  # type: ignore[attr-defined]
    if native_tool_history:
        provider.supports_structured_tool_replacement = (  # type: ignore[attr-defined]
            lambda *, effort: True
        )
    tools = ToolRegistry()
    tools.register(ReadTool())
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    agent = CodingSession(
        provider=provider,
        sessions=store,
        tool_registry=tools,
        tool_context=ToolContext(cwd=project),
        models=_registry(image_models=image_models),
    )

    async def two_runs() -> list[WispEvent]:
        events = [event async for event in agent.run("look at shot.png", session=session)]
        async for _ in agent.run("and now?", session=session):
            pass
        return events

    return provider, anyio.run(two_runs), session.path


def test_read_tool_result_sends_image(tmp_path: Path) -> None:
    """An image `read` returns is stored, sent with its result, and named on resume."""

    provider, events, path = _read_twice(tmp_path, image_models=("scripted",))

    image = inspect_image(PNG)
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.images == (image,)
    assert result.output.startswith("Image file shot.png: PNG")
    (tool_message,) = [
        m for m in JsonlSessionStore(path.parent).load(path).read_messages() if m.role == "tool"
    ]
    assert tool_message.images == (image,)
    (sent,) = provider.calls[1].tool_results
    assert sent.images == (image,)
    assert sent.image_bytes == (PNG,)
    # The scripted adapter replays earlier tool calls as a summary, which names the
    # image instead of carrying it, so the resumed request loads no bytes for it.
    resumed = provider.calls[2].messages
    assert not any(message.image_bytes for message in resumed)
    assert any("images_not_shown" in message.content for message in resumed)


@pytest.mark.parametrize(
    ("image_models", "budget"),
    [((), None), (("scripted",), len(PNG) - 1)],
    ids=["text-only-model", "over-budget"],
)
def test_unsendable_tool_image_is_named(
    tmp_path: Path, image_models: tuple[str, ...], budget: int | None
) -> None:
    provider, _, _ = _read_twice(tmp_path, image_models=image_models, budget=budget)

    (sent,) = provider.calls[1].tool_results
    assert sent.images == (inspect_image(PNG),)
    assert sent.image_bytes is None


def test_native_replay_resends_tool_image(tmp_path: Path) -> None:
    """A provider that replays tool calls natively gets the image again on resume."""

    provider, _, _ = _read_twice(tmp_path, image_models=("scripted",), native_tool_history=True)

    (resumed_tool,) = [m for m in provider.calls[2].messages if m.role == "tool"]
    assert resumed_tool.image_bytes == (PNG,)


def test_missing_tool_image_is_named(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A tool image gone from the store goes as a placeholder rather than failing."""

    store = JsonlSessionStore(tmp_path)
    session = store.create()
    agent = CodingSession(
        provider=_answers(0), sessions=store, models=_registry(image_models=("scripted",))
    )
    agent._start_tool_image_allowance(())
    missing = ToolCallResult(call_id="call-1", output="x", images=(inspect_image(PNG),))

    (loaded,) = anyio.run(agent._load_tool_result_images, session, [missing])

    assert loaded.image_bytes is None
    assert "Sending tool image placeholders" in caplog.text


def test_tool_images_share_run_budget(tmp_path: Path) -> None:
    """Each tool batch's images come out of what the run's requests still carry."""

    project = tmp_path / "project"
    project.mkdir()
    (project / "shot.png").write_bytes(PNG)

    def read_turn(call_id: str) -> list[object]:
        read = ToolCall(call_id=call_id, name="read", arguments={"path": "shot.png"})
        return [
            ProviderResponseStarted(model="test"),
            ProviderToolCallCompleted(tool_call=read),
            ProviderResponseCompleted(content="", tool_calls=(read,), finish_reason="tool_calls"),
        ]

    provider = ScriptedProvider(
        [
            read_turn("call-1"),
            read_turn("call-2"),
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")],
        ]
    )
    provider.max_request_image_bytes = len(PNG) + len(PNG) // 2  # type: ignore[attr-defined]
    tools = ToolRegistry()
    tools.register(ReadTool())
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=tools,
        tool_context=ToolContext(cwd=project),
        models=_registry(image_models=("scripted",)),
    )

    async def run() -> None:
        async for _ in agent.run("look twice"):
            pass

    anyio.run(run)

    (first,) = provider.calls[1].tool_results
    (second,) = provider.calls[2].tool_results
    assert first.image_bytes == (PNG,)
    assert second.images == first.images
    assert second.image_bytes is None


def _queue_during_run(
    tmp_path: Path, *, budget: int, queue: Callable[[CodingSession], Awaitable[None]]
) -> None:
    """Run a prompt carrying PNG and call ``queue`` once the run accepts messages."""

    store = JsonlSessionStore(tmp_path)
    event_bus = EventBus()
    provider = _answers(3)
    provider.max_request_image_bytes = budget  # type: ignore[attr-defined]
    agent = CodingSession(provider=provider, sessions=store, events=event_bus)

    async def on_started(event: WispEvent) -> None:
        await queue(agent)

    event_bus.on("agent.started", on_started)

    async def run() -> None:
        async for _ in agent.run("look", images=[PNG], session=store.create()):
            pass

    anyio.run(run)


@pytest.mark.parametrize("room", [0, -1], ids=["fits", "too-big"])
def test_queued_image_fits_run(tmp_path: Path, room: int) -> None:
    """A queued message's images must fit beside those the run already sends."""

    outcomes: list[str] = []

    async def steer(agent: CodingSession) -> None:
        try:
            await agent.steer("and this?", images=[JPEG])
        except ImageInputUnsupportedError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("queued")

    _queue_during_run(tmp_path, budget=len(PNG) + len(JPEG) + room, queue=steer)

    if room == 0:
        assert outcomes == ["queued"]
    else:
        (rejection,) = outcomes
        assert f"room for {len(JPEG) - 1} more bytes" in rejection


@pytest.mark.parametrize("remove", ["pop", "clear"])
def test_removed_queue_frees_budget(tmp_path: Path, remove: str) -> None:
    """Images removed from the queue unsent stop holding the run's budget."""

    async def requeue(agent: CodingSession) -> None:
        await agent.follow_up("later", images=[JPEG])
        if remove == "pop":
            agent.pop_queue("follow_up")
        else:
            agent.clear_queue()
        await agent.follow_up("later", images=[JPEG])

    _queue_during_run(tmp_path, budget=len(PNG) + len(JPEG), queue=requeue)


def test_restored_queue_fits_budget(tmp_path: Path) -> None:
    """A queue kept from a failed run drops image bytes the next run cannot fit."""

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
    provider.max_request_image_bytes = len(PNG) + len(JPEG) - 1  # type: ignore[attr-defined]
    agent = CodingSession(provider=provider, sessions=store, events=event_bus)
    queued_once: list[bool] = []

    async def queue_once(event: WispEvent) -> None:
        if not queued_once:
            queued_once.append(True)
            await agent.follow_up("later", images=[PNG])

    event_bus.on("agent.started", queue_once)

    async def run(prompt: str, images: list[bytes]) -> None:
        async for _ in agent.run(prompt, images=images, session=session):
            pass

    with pytest.raises(RuntimeError, match="provider failed"):
        anyio.run(run, "look", [])
    anyio.run(run, "next", [JPEG])

    request = _user_messages(provider.calls[-1])
    assert [message.image_bytes for message in request] == [None, (JPEG,), None]
    assert request[-1].content == "later"
    assert request[-1].images is not None
