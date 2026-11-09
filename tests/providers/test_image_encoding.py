"""Each adapter's wire shape for user messages that carry images."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import cast

import pytest
from google.genai import types as genai_types

from wisp.images import inspect_image
from wisp.messages import Message
from wisp.providers.anthropic import _messages_to_anthropic
from wisp.providers.base import Provider, image_placeholder_text, provider_supports_image_input
from wisp.providers.fake import FakeProvider
from wisp.providers.google import _messages_to_google
from wisp.providers.openai import _messages_to_response_input
from wisp.providers.openai_codex import _messages_to_codex_input
from wisp.providers.openai_compatible import _messages_to_chat

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
PNG = (_FIXTURES / "pixel.png").read_bytes()
JPEG = (_FIXTURES / "pixel.jpg").read_bytes()
PNG_PART = inspect_image(PNG)
JPEG_PART = inspect_image(JPEG)
PNG_B64 = base64.b64encode(PNG).decode()
JPEG_B64 = base64.b64encode(JPEG).decode()


def _message(content: str, *, loaded: bool = True) -> Message:
    return Message(
        role="user",
        content=content,
        images=(PNG_PART, JPEG_PART),
        image_bytes=(PNG, JPEG) if loaded else None,
    )


@pytest.mark.parametrize(
    ("content", "loaded", "expected"),
    [
        (
            "what is this?",
            True,
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG_B64},
                },
                {"type": "text", "text": "what is this?"},
            ],
        ),
        (
            "",
            True,
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": JPEG_B64},
                },
            ],
        ),
        (
            "",
            False,
            [
                {"type": "text", "text": image_placeholder_text(PNG_PART)},
                {"type": "text", "text": image_placeholder_text(JPEG_PART)},
            ],
        ),
    ],
    ids=["images-then-text", "image-only", "bytes-not-loaded"],
)
def test_anthropic_blocks(content: str, loaded: bool, expected: list[object]) -> None:
    (encoded,) = _messages_to_anthropic([_message(content, loaded=loaded)])

    assert encoded == {"role": "user", "content": expected}


@pytest.mark.parametrize(
    ("content", "loaded", "expected"),
    [
        (
            "what is this?",
            True,
            [
                {"type": "input_image", "image_url": f"data:image/png;base64,{PNG_B64}"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{JPEG_B64}"},
                {"type": "input_text", "text": "what is this?"},
            ],
        ),
        (
            "",
            True,
            [
                {"type": "input_image", "image_url": f"data:image/png;base64,{PNG_B64}"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{JPEG_B64}"},
            ],
        ),
        (
            "",
            False,
            [
                {"type": "input_text", "text": image_placeholder_text(PNG_PART)},
                {"type": "input_text", "text": image_placeholder_text(JPEG_PART)},
            ],
        ),
    ],
    ids=["images-then-text", "image-only", "bytes-not-loaded"],
)
def test_responses_parts(content: str, loaded: bool, expected: list[object]) -> None:
    """OpenAI (and xAI, which inherits it) and Codex share the Responses input format."""

    message = _message(content, loaded=loaded)

    assert _messages_to_response_input([message]) == [{"role": "user", "content": expected}]
    assert _messages_to_codex_input([message]) == [{"role": "user", "content": expected}]


@pytest.mark.parametrize(
    ("content", "loaded", "expected"),
    [
        (
            "what is this?",
            True,
            [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{JPEG_B64}"}},
                {"type": "text", "text": "what is this?"},
            ],
        ),
        (
            "",
            False,
            [
                {"type": "text", "text": image_placeholder_text(PNG_PART)},
                {"type": "text", "text": image_placeholder_text(JPEG_PART)},
            ],
        ),
    ],
    ids=["images-then-text", "bytes-not-loaded"],
)
def test_chat_parts(content: str, loaded: bool, expected: list[object]) -> None:
    assert _messages_to_chat([_message(content, loaded=loaded)]) == [
        {"role": "user", "content": expected}
    ]


@pytest.mark.parametrize(
    ("content", "loaded", "expected"),
    [
        (
            "what is this?",
            True,
            [
                genai_types.Part(inline_data=genai_types.Blob(mime_type="image/png", data=PNG)),
                genai_types.Part(inline_data=genai_types.Blob(mime_type="image/jpeg", data=JPEG)),
                genai_types.Part(text="what is this?"),
            ],
        ),
        (
            "",
            False,
            [
                genai_types.Part(text=image_placeholder_text(PNG_PART)),
                genai_types.Part(text=image_placeholder_text(JPEG_PART)),
            ],
        ),
    ],
    ids=["images-then-text", "bytes-not-loaded"],
)
def test_google_parts(content: str, loaded: bool, expected: list[genai_types.Part]) -> None:
    assert _messages_to_google([_message(content, loaded=loaded)]) == [
        genai_types.Content(role="user", parts=expected)
    ]


def test_text_messages_unchanged() -> None:
    """Messages without images keep the plain string content they always had."""

    message = Message(role="user", content="hello")

    assert _messages_to_response_input([message]) == [{"role": "user", "content": "hello"}]
    assert _messages_to_chat([message]) == [{"role": "user", "content": "hello"}]
    assert _messages_to_anthropic([message]) == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    ]


def test_capability_is_opt_in() -> None:
    """An adapter that never declares image input is treated as text-only."""

    class TextOnlyProvider:
        name = "text-only"
        default_model = None

    assert not provider_supports_image_input(cast(Provider, TextOnlyProvider()))
    assert provider_supports_image_input(cast(Provider, FakeProvider()))
