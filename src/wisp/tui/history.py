"""Renderer-neutral transcript hydration helpers for TUI resume flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wisp.events import RpcMessageSnapshot

TUI_HISTORY_MESSAGE_LIMIT = 500
_TRUNCATED_SUFFIX = "[content truncated]"


@dataclass(frozen=True)
class HistoricalTranscriptMessage:
    """One historical user/assistant line ready for TUI rendering."""

    role: Literal["user", "assistant"]
    content: str


def history_from_rpc_messages(
    messages: tuple[RpcMessageSnapshot, ...],
) -> tuple[HistoricalTranscriptMessage, ...]:
    """Convert bounded RPC transcript messages into TUI-visible history."""

    rendered: list[HistoricalTranscriptMessage] = []
    for message in messages:
        if message.role not in {"user", "assistant"}:
            continue
        if message.role == "assistant" and not message.content and not message.content_truncated:
            continue
        role: Literal["user", "assistant"] = "user" if message.role == "user" else "assistant"
        rendered.append(
            HistoricalTranscriptMessage(
                role=role,
                content=_content_for_history(message),
            )
        )
    return tuple(rendered)


def _content_for_history(message: RpcMessageSnapshot) -> str:
    content = message.content
    if not message.content_truncated:
        return content
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{_TRUNCATED_SUFFIX}"
