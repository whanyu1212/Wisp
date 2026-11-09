from __future__ import annotations

import base64
from collections.abc import Sequence
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import build_rpc_executor_fixture, guard_rpc_command_serialization
from wisp.events import AgentStarted, QueueUpdated, RpcCommandFinished
from wisp.rpc.coordinator import _RpcCommandCompleted, _RpcRunningCommand
from wisp.sessions.jsonl import JsonlSession

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
PNG = (_FIXTURES / "pixel.png").read_bytes()


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


async def _run_prompt(
    tmp_path: Path, images: list[str]
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
