from __future__ import annotations

from pathlib import Path

import anyio

from scripts.live_image_smoke import Target, smoke, two_colour_png
from wisp.auth.storage import JsonAuthStore
from wisp.images import inspect_image
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


def _answer(text: str) -> list[object]:
    return [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content=text)]


def test_swatch_is_valid_png() -> None:
    image = inspect_image(two_colour_png("red", "blue", size=32))

    assert (image.media_type, image.width, image.height) == ("image/png", 32, 32)


def test_scripted_targets_pass(tmp_path: Path) -> None:
    """The three checks pass against a provider that answers as a vision model would."""

    read = ToolCall(call_id="call-1", name="read", arguments={"path": "swatch.png"})
    provider = ScriptedProvider(
        [
            _answer("Red"),
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=read),
                ProviderResponseCompleted(
                    content="", tool_calls=(read,), finish_reason="tool_calls"
                ),
            ],
            _answer("Green"),
            _answer("Yellow"),
        ]
    )
    target = Target("scripted", "scripted", "scripted", lambda _: provider, use_catalog=False)
    auth = StoredProviderAuthResolver(JsonAuthStore(tmp_path / "auth.json"))

    checks = anyio.run(smoke, target, auth, tmp_path)

    assert [(check.name, check.status) for check in checks] == [
        ("prompt", "pass"),
        ("tool", "pass"),
        ("resume", "pass"),
    ]
