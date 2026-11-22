"""Group adjacent user prompts only in the outgoing request, after image budgeting."""

from collections.abc import Callable, Iterator, Sequence
from typing import TypeVar

from wisp.messages import Message

_Part = TypeVar("_Part")


def request_groups(
    messages: Sequence[Message], *, group_tools: bool = False
) -> Iterator[Sequence[Message]]:
    """Yield consecutive user prompts, optionally consecutive paired results, and singletons.

    Args:
        messages (Sequence[Message]): Hydrated provider input, never modified.
        group_tools (bool): Whether the adapter encodes a batch of tool results together.

    Yields:
        Sequence[Message]: Messages in their original order; text-only user runs are
        joined with a blank line. Image runs retain each prompt's hydration state.
    """
    start = 0
    while start < len(messages):
        first = messages[start]
        end = start + 1
        if first.role == "user":
            while end < len(messages) and messages[end].role == "user":
                end += 1
        elif group_tools and first.role == "tool" and first.tool_call_id:
            while (
                end < len(messages) and messages[end].role == "tool" and messages[end].tool_call_id
            ):
                end += 1
        group = messages[start:end]
        if first.role == "user" and len(group) > 1 and not any(m.images for m in group):
            yield (first.model_copy(update={"content": "\n\n".join(m.content for m in group)}),)
        else:
            yield group
        start = end


def prompt_parts(
    group: Sequence[Message], encode: Callable[[Message], Sequence[_Part]]
) -> list[_Part]:
    """Encode each prompt's images/placeholders and text together, in submission order."""
    return [
        part
        for index, message in enumerate(group)
        for part in encode(
            message.model_copy(update={"content": message.content + "\n\n"})
            if message.content and index < len(group) - 1
            else message
        )
    ]
