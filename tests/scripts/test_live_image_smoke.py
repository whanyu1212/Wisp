from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from scripts.live_image_smoke import Target, _AliasAuth, smoke, two_colour_png
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


@pytest.mark.parametrize(
    ("native_history", "replay_works", "resume_answer", "resume_status"),
    [
        (False, True, "Yellow", "pass"),
        # A summary-replay adapter cannot see the image again, which is expected.
        (False, True, "unknown", "pass-no-replay"),
        # An adapter with native tool history must resend the image and use it.
        (True, True, "unknown", "wrong-answer"),
        (True, False, "unknown", "no-replay"),
    ],
    ids=["summary-sees", "summary-unknown", "native-unknown", "native-not-resent"],
)
def test_scripted_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_history: bool,
    replay_works: bool,
    resume_answer: str,
    resume_status: str,
) -> None:
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
            _answer(resume_answer),
        ]
    )
    if native_history:
        provider.supports_structured_tool_replacement = (  # type: ignore[attr-defined]
            lambda *, effort: True
        )
    if not replay_works:
        # The session stops loading earlier tool images, as a replay regression would.
        monkeypatch.setattr(
            "wisp.coding.session.structured_tool_replacement_support",
            lambda provider, *, effort: False,
        )
    target = Target("scripted", "scripted", "scripted", lambda _: provider, use_catalog=False)
    auth = StoredProviderAuthResolver(JsonAuthStore(tmp_path / "auth.json"))

    checks = anyio.run(smoke, target, auth, tmp_path)

    assert [(check.name, check.status) for check in checks] == [
        ("prompt", "pass"),
        ("tool", "pass"),
        ("resume", resume_status),
    ]


def test_alias_reads_environment_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The xAI Chat target uses XAI_API_KEY when no xAI key is stored."""

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    auth = _AliasAuth(StoredProviderAuthResolver(JsonAuthStore(tmp_path / "auth.json")), "xai")

    assert anyio.run(auth.api_key, "xai-chat") == "test-key"
