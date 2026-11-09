"""Each adapter's wire shape for tool results that carry images."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from google.genai import types as genai_types

from wisp.events import ToolCallSnapshot
from wisp.images import inspect_image
from wisp.messages import Message
from wisp.messages.history import normalize_provider_history
from wisp.providers.anthropic import _messages_to_anthropic, _tool_results_to_message
from wisp.providers.base import (
    ToolCallResult,
    image_placeholder_text,
    tool_images_preamble,
)
from wisp.providers.google import _function_responses_content, _messages_to_google
from wisp.providers.openai import _messages_to_response_input, _tool_results_to_response_input
from wisp.providers.openai_codex import _messages_to_codex_input, _tool_results_to_codex_input
from wisp.providers.openai_compatible import _messages_to_chat, _tool_results_to_messages

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
PNG = (_FIXTURES / "pixel.png").read_bytes()
PNG_PART = inspect_image(PNG)
PNG_B64 = base64.b64encode(PNG).decode()
PNG_URL = f"data:image/png;base64,{PNG_B64}"
OUTPUT = "Image file shot.png: PNG"


def _result(*, loaded: bool = True, call_id: str = "call-1") -> ToolCallResult:
    return ToolCallResult(
        call_id=call_id,
        output=OUTPUT,
        images=(PNG_PART,),
        image_bytes=(PNG,) if loaded else None,
    )


def _plain(call_id: str = "call-2") -> ToolCallResult:
    return ToolCallResult(call_id=call_id, output="plain text")


def _history() -> list[Message]:
    """An assistant turn calling two tools, the first of which returned an image."""

    calls = (
        ToolCallSnapshot(call_id="call-1", name="read", arguments={"path": "shot.png"}),
        ToolCallSnapshot(call_id="call-2", name="ls", arguments={}),
    )
    return [
        Message(role="assistant", content="", tool_calls=calls),
        Message(
            role="tool",
            content=OUTPUT,
            tool_call_id="call-1",
            tool_name="read",
            images=(PNG_PART,),
            image_bytes=(PNG,),
        ),
        Message(role="tool", content="plain text", tool_call_id="call-2", tool_name="ls"),
    ]


ANTHROPIC_IMAGE = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
}


@pytest.mark.parametrize(
    ("result", "content"),
    [
        (_result(), [{"type": "text", "text": OUTPUT}, ANTHROPIC_IMAGE]),
        (
            _result(loaded=False),
            [
                {"type": "text", "text": OUTPUT},
                {"type": "text", "text": image_placeholder_text(PNG_PART)},
            ],
        ),
        (_plain(), "plain text"),
    ],
    ids=["loaded", "not-loaded", "no-images"],
)
def test_anthropic_tool_result(result: ToolCallResult, content: object) -> None:
    (block,) = _tool_results_to_message([result])["content"]  # type: ignore[misc]

    assert block["content"] == content  # type: ignore[index]


def test_anthropic_replays_tool_images() -> None:
    _, results = _messages_to_anthropic(_history())

    assert results["content"][0]["content"] == [  # type: ignore[index]
        {"type": "text", "text": OUTPUT},
        ANTHROPIC_IMAGE,
    ]
    assert results["content"][1]["content"] == "plain text"  # type: ignore[index]


@pytest.mark.parametrize(
    ("result", "output"),
    [
        (
            _result(),
            [
                {"type": "input_text", "text": OUTPUT},
                {"type": "input_image", "image_url": PNG_URL},
            ],
        ),
        (
            _result(loaded=False),
            [
                {"type": "input_text", "text": OUTPUT},
                {"type": "input_text", "text": image_placeholder_text(PNG_PART)},
            ],
        ),
        (_plain(), "plain text"),
    ],
    ids=["loaded", "not-loaded", "no-images"],
)
def test_responses_tool_output(result: ToolCallResult, output: object) -> None:
    for encode in (_tool_results_to_response_input, _tool_results_to_codex_input):
        (item,) = encode([result])
        assert item["output"] == output  # type: ignore[typeddict-item]


def test_responses_replay_tool_images() -> None:
    expected = [
        {"type": "input_text", "text": OUTPUT},
        {"type": "input_image", "image_url": PNG_URL},
    ]
    for encoded in (
        _messages_to_response_input(_history()),
        _messages_to_codex_input(_history()),
    ):
        outputs = [item for item in encoded if item.get("type") == "function_call_output"]
        assert outputs[0]["output"] == expected  # type: ignore[typeddict-item]
        assert outputs[1]["output"] == "plain text"  # type: ignore[typeddict-item]


def test_chat_images_follow_tool_run() -> None:
    """Every tool message follows the call uninterrupted; the images come after."""

    expected_tool_messages = [
        {"role": "tool", "tool_call_id": "call-1", "content": OUTPUT},
        {"role": "tool", "tool_call_id": "call-2", "content": "plain text"},
    ]
    expected_images = {
        "role": "user",
        "content": [
            {"type": "text", "text": tool_images_preamble(_result())},
            {"type": "image_url", "image_url": {"url": PNG_URL}},
        ],
    }

    live = list(_tool_results_to_messages([_result(), _plain()]))
    replayed = _messages_to_chat(_history())

    assert live == [*expected_tool_messages, expected_images]
    assert replayed[0]["role"] == "assistant"
    assert replayed[1:] == [*expected_tool_messages, expected_images]


def test_chat_names_unsent_images() -> None:
    messages = list(_tool_results_to_messages([_result(loaded=False)]))

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": f"{OUTPUT}\n{image_placeholder_text(PNG_PART)}",
        }
    ]


def _function_response(content: genai_types.Content, index: int) -> genai_types.FunctionResponse:
    response = (content.parts or [])[index].function_response
    assert response is not None
    return response


def test_gemini_3_nests_images() -> None:
    content = _function_responses_content(
        [(_result(), "id-1", "read"), (_plain(), None, "ls")], model="gemini-3.8-flash"
    )

    assert len(content.parts or []) == 2
    first = _function_response(content, 0)
    assert first.response == {"output": OUTPUT}
    assert first.parts == [
        genai_types.FunctionResponsePart(
            inline_data=genai_types.FunctionResponseBlob(mime_type="image/png", data=PNG)
        )
    ]
    assert _function_response(content, 1).parts is None


def test_gemini_2_images_follow_responses() -> None:
    content = _function_responses_content(
        [(_result(), "id-1", "read"), (_plain(), None, "ls")], model="gemini-2.5-flash"
    )
    parts = content.parts or []

    assert content.role == "user"
    assert _function_response(content, 0).parts is None
    assert parts[1].function_response is not None
    assert parts[2].text == tool_images_preamble(_result())
    assert parts[3].inline_data == genai_types.Blob(mime_type="image/png", data=PNG)


def test_gemini_names_unsent_images() -> None:
    content = _function_responses_content(
        [(_result(loaded=False), None, "read")], model="gemini-3.8-flash"
    )

    response = _function_response(content, 0)
    assert response.response == {"output": f"{OUTPUT}\n{image_placeholder_text(PNG_PART)}"}
    assert response.parts is None


def test_gemini_replays_tool_images() -> None:
    contents = _messages_to_google(_history(), model="gemini-3.8-flash")

    assert _function_response(contents[1], 0).parts is not None


def test_summary_names_unshown_images() -> None:
    """A provider without native tool replay gets the images named, not sent."""

    (summary,) = normalize_provider_history(_history(), native_tool_history=False)
    (plain_summary,) = normalize_provider_history(
        [_history()[0], _history()[2].model_copy(update={"tool_call_id": "call-1"})],
        native_tool_history=False,
    )

    calls = json.loads(summary.content)["calls"]
    assert summary.role == "assistant"
    assert summary.images is None
    assert calls[0]["result"]["images_not_shown"] == [
        f"image/png, {PNG_PART.width}x{PNG_PART.height}"
    ]
    assert "images_not_shown" not in calls[1]["result"]
    assert "images_not_shown" not in plain_summary.content
