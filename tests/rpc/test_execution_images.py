from __future__ import annotations

import base64
from collections.abc import Sequence
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
    preserve_running_command,
    reject_unexpected_command,
)
from wisp.agent.harness import QueuedMessages
from wisp.events import (
    AgentStarted,
    ImagePart,
    QueueItemsRemoved,
    QueueUpdated,
    RpcCommandFinished,
    RpcSessionTreeNavigated,
)
from wisp.messages import Message
from wisp.rpc.coordinator import _RpcCommandCompleted, _RpcRunningCommand
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
PNG = (_FIXTURES / "pixel.png").read_bytes()


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


async def _run_prompt(
    tmp_path: Path, images: list[object]
) -> tuple[_RpcCommandCompleted, JsonlSession, list[object]]:
    fixture = await build_rpc_executor_fixture(tmp_path)
    send, receive = anyio.create_memory_object_stream(10)
    async with send, receive, anyio.create_task_group() as task_group:
        executor = fixture.executor(task_group=task_group, send=send)
        result = await dispatch_parsed(
            executor,
            {"id": "prompt-1", "type": "prompt", "prompt": "what is this?", "images": images},
        )
        # A prompt that starts its run reports readiness before it completes.
        completed = await receive.receive()
        while not isinstance(completed, _RpcCommandCompleted):
            completed = await receive.receive()
        task_group.cancel_scope.cancel()
    assert isinstance(completed, _RpcCommandCompleted)
    assert result.selected_session is not None
    return completed, result.selected_session, list(fixture.events)


def test_prompt_stores_attached_images(tmp_path: Path) -> None:
    completed, session, _ = anyio.run(_run_prompt, tmp_path, [base64.b64encode(PNG).decode()])

    assert completed.ok is True
    user = next(message for message in session.read_messages() if message.role == "user")
    assert user.images is not None
    (image,) = user.images
    assert anyio.run(session.read_image, image) == PNG
    (snapshot,) = [
        message for message in session.read_message_page().messages if message.role == "user"
    ]
    assert snapshot.images == (image,)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ("not base64!", "Image 1 rejected (invalid_base64)"),
        (base64.b64encode(PNG[:-12]).decode(), "Image 1 rejected (malformed)"),
        (base64.b64encode(b"plain text").decode(), "Image 1 rejected (unsupported_format)"),
    ],
    ids=["invalid-base64", "truncated-png", "not-an-image"],
)
def test_rejected_image_fails_before_run(tmp_path: Path, data: str, error: str) -> None:
    completed, session, events = anyio.run(_run_prompt, tmp_path, [data])

    assert completed.ok is False
    (finished,) = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert finished.error is not None
    assert finished.error.startswith(error)
    assert not any(isinstance(event, AgentStarted) for event in events)
    assert not session.path.exists()


def test_later_bad_image_rejects_all(tmp_path: Path) -> None:
    """One bad attachment fails the command and stores none of its images."""

    completed, session, events = anyio.run(
        _run_prompt, tmp_path, [base64.b64encode(PNG).decode(), "%%%"]
    )

    assert completed.ok is False
    (finished,) = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert finished.error is not None
    assert finished.error.startswith("Image 2 rejected (invalid_base64)")
    assert not session.images.directory.exists()


@pytest.mark.parametrize("command_type", ["steer", "follow_up"])
def test_queued_message_decodes_images(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str
) -> None:
    received: list[tuple[str, tuple[bytes, ...]]] = []

    async def queue(content: str, *, images: Sequence[bytes] = ()) -> QueueUpdated:
        received.append((content, tuple(images)))
        return QueueUpdated()

    async def scenario() -> list[object]:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(fixture.agent, command_type, queue)
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            for command_id, images in (("good", [base64.b64encode(PNG).decode()]), ("bad", ["%"])):
                await dispatch_parsed(
                    executor,
                    {"id": command_id, "type": command_type, "content": "see", "images": images},
                    running,
                )
        return list(fixture.events)

    events = anyio.run(scenario)

    assert received == [("see", (PNG,))]
    finished = {
        event.command_id: event for event in events if isinstance(event, RpcCommandFinished)
    }
    assert finished["good"].ok is True
    assert finished["bad"].ok is False
    assert (finished["bad"].error or "").startswith("Image 1 rejected (invalid_base64)")


def _stored_reference(tmp_path: Path, **overrides: object) -> dict[str, object]:
    """Store PNG in the sessions directory's shared image store and reference it."""

    image = anyio.run(JsonlSessionStore(tmp_path).create().store_image, PNG)
    return {**image.model_dump(mode="json"), **overrides}


def test_prompt_reattaches_stored_image(tmp_path: Path) -> None:
    """A reference, such as a forked message's selected image, re-attaches it."""

    reference = _stored_reference(tmp_path)

    completed, session, _ = anyio.run(_run_prompt, tmp_path, [reference])

    assert completed.ok is True
    user = next(message for message in session.read_messages() if message.role == "user")
    assert user.images is not None
    assert user.images[0].sha256 == reference["sha256"]


def test_reference_metadata_is_rederived(tmp_path: Path) -> None:
    """Only a reference's digest is trusted; format and size come from the bytes."""

    reference = _stored_reference(tmp_path, width=7999, height=7999)

    completed, session, _ = anyio.run(_run_prompt, tmp_path, [reference])

    assert completed.ok is True
    user = next(message for message in session.read_messages() if message.role == "user")
    assert user.images is not None
    assert (user.images[0].width, user.images[0].height) == (3, 2)


def test_unknown_reference_fails_before_run(tmp_path: Path) -> None:
    reference = {
        "media_type": "image/png",
        "sha256": "0" * 64,
        "byte_size": len(PNG),
        "width": 3,
        "height": 2,
    }

    completed, session, events = anyio.run(_run_prompt, tmp_path, [reference])

    assert completed.ok is False
    (finished,) = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert (finished.error or "").startswith("Image 1 rejected (not_stored)")
    assert not any(isinstance(event, AgentStarted) for event in events)
    assert not session.path.exists()


def test_queue_removal_returns_images(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Pop and clear report the removed messages' images for re-attaching."""

    image = anyio.run(JsonlSessionStore(tmp_path).create().store_image, PNG)
    with_image = Message(role="user", content="look", images=(image,))
    text_only = Message(role="user", content="later")

    async def scenario() -> list[object]:
        fixture = await build_rpc_executor_fixture(tmp_path)
        monkeypatch.setattr(fixture.agent, "pop_queue", lambda kind: (with_image, QueueUpdated()))
        monkeypatch.setattr(
            fixture.agent,
            "clear_queue",
            lambda kind: (
                QueuedMessages(steering=(with_image,), follow_up=(text_only,)),
                QueueUpdated(),
            ),
        )
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(
                executor, {"id": "pop", "type": "pop_queue", "kind": "steering"}, running
            )
            await dispatch_parsed(executor, {"id": "clear", "type": "clear_queue"}, running)
        return list(fixture.events)

    removed = [event for event in anyio.run(scenario) if isinstance(event, QueueItemsRemoved)]

    assert [(event.steering, event.steering_images) for event in removed] == [
        (("look",), ((image,),)),
        (("look",), ((image,),)),
    ]
    assert removed[1].follow_up == ("later",)
    assert removed[1].follow_up_images == ()


@pytest.mark.parametrize("selected", [True, False], ids=["selected-session", "no-session"])
def test_steer_resolves_reference(tmp_path: Path, monkeypatch: MonkeyPatch, selected: bool) -> None:
    """A queued message's reference is read from the selected session's store."""

    reference = _stored_reference(tmp_path)
    received: list[tuple[bytes, ...]] = []

    async def steer(content: str, *, images: Sequence[bytes] = ()) -> QueueUpdated:
        received.append(tuple(images))
        return QueueUpdated()

    async def scenario() -> list[object]:
        fixture = await build_rpc_executor_fixture(tmp_path)
        if selected:
            fixture.session_state.session = fixture.sessions.create()
        monkeypatch.setattr(fixture.agent, "steer", steer)
        running = _RpcRunningCommand("active", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            await dispatch_parsed(
                executor,
                {"id": "steer", "type": "steer", "content": "again", "images": [reference]},
                running,
            )
        return list(fixture.events)

    events = anyio.run(scenario)

    (finished,) = [event for event in events if isinstance(event, RpcCommandFinished)]
    if selected:
        assert finished.ok is True
        assert received == [(PNG,)]
    else:
        assert finished.ok is False
        assert (finished.error or "").startswith("Image 1 rejected (not_stored)")
        assert received == []


def test_tree_navigation_returns_images(tmp_path: Path) -> None:
    """Navigating to a user message reports its images for the editor."""

    async def scenario() -> tuple[ImagePart, RpcSessionTreeNavigated]:
        fixture = await build_rpc_executor_fixture(tmp_path)
        session = fixture.sessions.create()
        image = await session.store_image(PNG)
        selected = await session.append_message(
            Message(role="user", content="look", images=(image,))
        )
        await session.append_message(Message(role="assistant", content="a pixel"))
        fixture.session_state.session = session
        fixture.session_state.history = session.read_context_messages()
        fixture.session_state.entry_count = 2
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor,
                {"id": "navigate", "type": "navigate_session_tree", "entry_id": selected.id},
            )
            completed = await receive.receive()
            fixture.coordinator.running_command = result.running_command
            await fixture.coordinator.handle_event(
                completed,
                dispatch=preserve_running_command,
                reject=reject_unexpected_command,
            )
            task_group.cancel_scope.cancel()
        return image, next(
            event for event in fixture.events if isinstance(event, RpcSessionTreeNavigated)
        )

    image, navigated = anyio.run(scenario)

    assert navigated.editor_text == "look"
    assert navigated.editor_images == (image,)
