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
from textual.await_complete import AwaitComplete
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Markdown, Static


class Transcript(VerticalScroll):
    """Scrollable message container that follows the newest output like `tail -f`.

    Auto-scroll is driven by a sticky ``_follow`` flag rather than a per-append
    "am I near the bottom?" measurement. That measurement is self-defeating while
    streaming: the growing content is what pushes the bottom away, so a snapshot
    taken as it grows reads "not at the bottom" and abandons following the very
    output it should track.

    Instead the flag tracks whether the viewport is resting at the bottom, updated
    only when the scroll position *settles* (``watch_scroll_y``):

    - Rest at the bottom → ``True`` (keep following new output).
    - The user scrolls up and away → ``False`` (they're reading history; don't
      yank them back). Scrolling back to the bottom flips it ``True`` again.

    Content growth alone never flips the flag: appends don't move ``scroll_y``,
    and ``follow_tail()``'s programmatic scroll lands *at* the end, which
    re-derives to ``True`` — self-consistent, so no guard is needed. After each
    append the app calls ``follow_tail()``, which scrolls to the end iff the flag
    is set.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._follow = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        # Textual updates scroll_y as the position settles (including at the end
        # of an animated user scroll). Re-derive follow intent from the resting
        # position: at the bottom means "keep following", anywhere above means
        # "the user is reading back, leave them there".
        super().watch_scroll_y(old_value, new_value)
        self._follow = self.is_vertical_scroll_end

    def follow_tail(self) -> None:
        """Scroll to the newest content iff the user hasn't scrolled away."""
        if self._follow:
            self.scroll_end(animate=False)


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

    def set_content(self, text: str) -> AwaitComplete:
        # Reconcile the Markdown to the authoritative buffer and return update()'s
        # AwaitComplete, which resolves once *this update's* block children have
        # mounted (batched, under a lock). The caller awaits it before following
        # the tail so the scroll lands on the fully-laid-out extent rather than a
        # partially-mounted one.
        #
        # Also keep Markdown's own _initial_markdown in sync: Markdown._on_mount
        # runs `update(self._initial_markdown or "")` on its Mount event, which is
        # a *separate* async path from this call. If a turn is finalized in the
        # same tick the widget mounts (delta then flush with no refresh between),
        # that mount can run after our update() and clobber the content back to "".
        # Seeding _initial_markdown means whichever path runs last applies our text.
        self._markdown._initial_markdown = text
        return self._markdown.update(text)
