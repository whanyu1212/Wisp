"""Labelled prompt text, recorded while the prompt is assembled."""

from __future__ import annotations

from dataclasses import dataclass

from wisp.events import ContextSource
from wisp.messages import Message


@dataclass(frozen=True, slots=True)
class PromptPart:
    """One labelled piece of a system message.

    The prompt builder records the parts of each message it writes, so the session
    can report how much of the context each source uses without parsing the prompt
    afterwards. A message's parts are weights: a truncated message is measured as a
    whole and split across its parts in proportion to their text.

    Attributes:
        source (ContextSource): Breakdown row the text counts toward.
        label (str): Name shown for this part within its source, such as a file path.
        text (str): The text as written, before any whole-message truncation.
    """

    source: ContextSource
    label: str
    text: str


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """System messages with the labelled parts each one was built from.

    Attributes:
        messages (tuple[Message, ...]): Provider-facing system messages, in order.
        parts (tuple[tuple[PromptPart, ...], ...]): Parts of each message, aligned
            with `messages` by index.
    """

    messages: tuple[Message, ...]
    parts: tuple[tuple[PromptPart, ...], ...]
