"""Per-message transcript widgets for the Textual TUI.

Stage 2 replaces the append-only ``RichLog`` transcript with a
``VerticalScroll`` of these widgets, one per turn/event. Two kinds:

- ``LineMessage`` — a role-styled single block for tool calls, results,
  approvals, errors, notices, and user input. Content is escaped Rich markup in
  a ``Static`` (never fed to the Markdown parser), preserving the
  escape-at-boundary invariant for untrusted tool/error payloads.
- ``StreamMessage`` — the streaming assistant turn, backed by a ``Markdown``
  widget so model output renders code blocks, lists, and emphasis. Its content
  is driven from an authoritative text buffer via ``set_content`` and reconciled
  with one coalesced refresh (see ``TextualTui`` streaming), which avoids the
  mount race where ``update``/``append`` on a not-yet-mounted widget drops text.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Markdown, Static

# CSS role classes are applied per message so Stage 3 can style cards purely in
# CSS; the role also names the border_title label.
_ROLE_LABELS: dict[str, str] = {
    "user": "you",
    "assistant": "assistant",
    "tool": "tool",
    "approved": "tool",
    "denied": "tool",
    "error": "error",
    "notice": "wisp",
    "dim": "",
    "session": "",
}


class LineMessage(Static):
    """A single role-styled transcript line for non-streamed content."""

    def __init__(self, markup: str, *, role: str) -> None:
        # `markup` is already-composed Rich markup (label styled, payload escaped
        # by the caller). Static renders it with markup enabled by default.
        super().__init__(markup)
        self.add_class("message", f"message--{role}")


class StreamMessage(Widget):
    """The streaming assistant turn, backed by a Markdown widget.

    Content is set from an external authoritative buffer; the widget never
    accumulates deltas itself, so it is safe against the mount race.
    """

    DEFAULT_CSS = """
    StreamMessage {
        height: auto;
    }
    StreamMessage > Markdown {
        height: auto;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_class("message", "message--assistant")
        self._markdown = Markdown()

    def compose(self) -> ComposeResult:
        yield self._markdown

    def set_content(self, text: str) -> None:
        # Reconcile the Markdown to the authoritative buffer. update() is safe
        # once mounted; the caller schedules this via call_after_refresh so it
        # never runs before the mount settles.
        self._markdown.update(text)
