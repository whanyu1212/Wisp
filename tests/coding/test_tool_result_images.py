"""The executor stores a tool's images before its result references them."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from wisp.coding.tool_execution import ConfiguredToolExecutor, ImageStore
from wisp.events import ImagePart, ToolExecutionEnded, ToolResultReady
from wisp.messages import Message, message_from_completion_event
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.images import SessionImageStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult

PNG = (Path(__file__).parents[1] / "fixtures" / "images" / "pixel.png").read_bytes()


class _Bytes(bytes):
    """A bytes subclass an extension tool could return in place of real bytes."""


class _ImageTool:
    name = "snapshot"
    safety = "read"
    description = "Returns images."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, images: object) -> None:
        self._images = images

    async def run(self, arguments: object, context: object) -> ToolResult:
        return ToolResult(text="a picture", images=self._images)  # type: ignore[arg-type]


def _run(images: object, image_store: ImageStore | None) -> ToolExecutionEnded:
    registry = ToolRegistry()
    registry.register(_ImageTool(images))  # type: ignore[arg-type]
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=Path.cwd(), protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        image_store=image_store,
    )

    async def run() -> ToolExecutionEnded:
        events = [event async for event in executor.execute(ToolCall("c1", "snapshot", {}))]
        return next(event for event in events if isinstance(event, ToolExecutionEnded))

    return anyio.run(run)


def _store_in(directory: Path) -> tuple[SessionImageStore, ImageStore]:
    store = SessionImageStore(directory)

    async def store_image(data: bytes) -> ImagePart:
        return store.store(data)

    return store, store_image


def test_result_images_are_stored(tmp_path: Path) -> None:
    store, store_image = _store_in(tmp_path / "images")

    ended = _run((PNG,), store_image)

    assert ended.is_error is False
    assert [image.media_type for image in ended.images] == ["image/png"]
    # The reference resolves before anything sees it.
    assert store.read(ended.images[0]) == PNG
    ready = ToolResultReady.from_execution_ended(ended)
    message = message_from_completion_event(ended)
    assert ready.images == ended.images
    assert message.role == "tool"
    assert message.images == ended.images


def test_missing_store_says_so() -> None:
    ended = _run((PNG,), None)

    assert ended.images == ()
    assert ended.output == "a picture\n[The image could not be attached to this result.]"


@pytest.mark.parametrize(
    ("images", "output", "code"),
    [
        ((b"not an image",), "Tool returned an image Wisp cannot use", "invalid_result"),
        ([PNG], "Tool returned an invalid result", "invalid_result"),
        (("text",), "Tool returned an invalid result", "invalid_result"),
        ((PNG,) * 7, "Tool returned an invalid result", "invalid_result"),
        ((_Bytes(PNG),), "Tool returned an invalid result", "invalid_result"),
    ],
)
def test_bad_images_fail_the_call(tmp_path: Path, images: object, output: str, code: str) -> None:
    store, store_image = _store_in(tmp_path / "images")

    ended = _run(images, store_image)

    assert ended.is_error is True
    assert ended.output.startswith(output)
    assert ended.failure_code == code
    assert ended.images == ()
    assert not store.directory.exists() or not any(store.directory.iterdir())


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_store_failure_fails_the_call(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A store that cannot create its directory fails the call, not the run."""

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _, store_image = _store_in(sessions / "images")
    sessions.chmod(0o500)
    try:
        ended = _run((PNG,), store_image)
    finally:
        sessions.chmod(0o700)

    assert ended.is_error is True
    assert ended.output == "Could not store the image the tool returned"
    assert ended.failure_code == "internal_error"
    assert "Could not store a tool result's image" in caplog.text


def test_tool_messages_may_carry_images() -> None:
    image = ImagePart(media_type="image/png", sha256="a" * 64, byte_size=4, width=1, height=1)

    tool_message = Message(role="tool", content="x", tool_call_id="c1", images=(image,))
    assert tool_message.images == (image,)
    with pytest.raises(ValueError, match="needs its tool_call_id"):
        Message(role="tool", content="x", images=(image,))
    with pytest.raises(ValueError, match="valid only on user and tool messages"):
        Message(role="assistant", content="x", images=(image,))
    with pytest.raises(ValueError, match="carries no images"):
        ToolExecutionEnded(call_id="c1", name="read", output="x", is_error=True, images=(image,))
