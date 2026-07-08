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

from collections.abc import Mapping

from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option

from wisp.tui.commands import SLASH_COMMAND_SPECS, SlashCommandSpec
from wisp.tui.rendering import _markup_escape


def _format_duration(seconds: float) -> str:
    """Human-terse elapsed time for a tool card: `0.3s`, `1.2s`, `12s`, `1m03s`.

    Sub-10s calls keep one decimal (a file read is often ~0.3s and the decimal is
    meaningful there); past 10s the decimal is noise, so it's dropped; past a
    minute it rolls to `Nm SSs`. Negative inputs (clock skew across the RPC
    boundary) clamp to 0.
    """

    seconds = max(seconds, 0.0)
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _summarize_arguments(arguments: object, *, limit: int = 48) -> str:
    """Render a tool call's arguments as a terse `k=v, k=v` summary.

    Values are stringified and clipped so a card stays one line; a long single
    value (a pasted blob, a big path) is truncated with an ellipsis rather than
    wrapping the card. Non-mapping arguments fall back to their repr.
    """

    if not isinstance(arguments, Mapping):
        text = str(arguments)
        return text if len(text) <= limit else f"{text[: limit - 1]}…"
    parts: list[str] = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > limit:
            text = f"{text[: limit - 1]}…"
        parts.append(f"{key}={text}")
    return ", ".join(parts)


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


class SlashSuggest(OptionList):
    """Inline slash-command completion menu, Claude-Code style.

    A non-modal dropdown anchored above the input: when the line starts with `/`,
    it lists the matching commands and filters live as the user types. The input
    is never touched — this widget is a hint + completion shortcut layer. It floats
    on the overlay layer so it doesn't reflow the transcript.

    The command table is `SLASH_COMMAND_SPECS` (shared with the parser), so the
    menu, Tab-completion, and `/command` execution all derive from one source. Each
    option's id is the command spelling (`/model`), so the highlighted spec is
    recovered by id — no parallel index to keep in sync.
    """

    # overlay: screen floats the menu over the transcript WITHOUT reflowing it,
    # while keeping its natural compose position (just above #input, where it's
    # yielded). It is deliberately NOT put on a separate `layer:` — a lone child on
    # the overlay layer gets laid out at the TOP of the app by that layer's own
    # vertical layout, detaching it from the prompt (the bug Codex caught).
    # constrain: inside keeps it fully on-screen at any terminal size.
    DEFAULT_CSS = """
    SlashSuggest {
        overlay: screen;
        constrain: inside;
        display: none;
        width: auto;
        max-width: 60;
        height: auto;
        max-height: 8;
        border: round $accent;
        background: $panel;
        padding: 0 1;
    }
    SlashSuggest > .option-list--option-highlighted {
        background: $accent 30%;
    }
    """

    def __init__(self, id: str | None = None) -> None:  # noqa: A002 - Textual's param name
        super().__init__(id=id)
        # spelling → spec, so the highlighted option's id maps back to its command.
        self._by_command: dict[str, SlashCommandSpec] = {
            spec.command: spec for spec in SLASH_COMMAND_SPECS
        }
        self._visible_specs: tuple[SlashCommandSpec, ...] = ()

    @staticmethod
    def query_from_value(value: str) -> str | None:
        """The command token to filter on, or None if the value isn't a bare `/…`.

        A menu is warranted only while the *first* token is a slash word still
        being typed: the value starts with `/` and has no space yet (a space means
        the user has moved on to arguments or prose). Returns the lowercased token
        including the leading slash, e.g. `/mo`.
        """

        if not value.startswith("/") or " " in value:
            return None
        return value.lower()

    def matches(self, query: str) -> tuple[SlashCommandSpec, ...]:
        """Specs whose command starts with `query` (prefix match on the spelling)."""

        return tuple(spec for spec in SLASH_COMMAND_SPECS if spec.command.startswith(query))

    def show_for(self, value: str) -> int:
        """Filter and display the menu for the current input value.

        Returns the number of matches. Hides the menu (returns 0) when the value
        isn't a bare slash token or nothing matches — the caller relies on the
        count to know whether the menu is live.
        """

        query = self.query_from_value(value)
        specs = self.matches(query) if query is not None else ()
        self._visible_specs = specs
        self.clear_options()
        if not specs:
            self.display = False
            return 0
        self.add_options(
            [Option(f"{spec.command}  {spec.description}", id=spec.command) for spec in specs]
        )
        self.highlighted = 0
        self.display = True
        return len(specs)

    def hide(self) -> None:
        self.display = False
        self._visible_specs = ()

    @property
    def is_open(self) -> bool:
        return self.display

    def highlighted_spec(self) -> SlashCommandSpec | None:
        """The spec under the highlight, for Tab-completion; None if menu empty."""

        if self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        return self._by_command.get(option.id or "")


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
        # The role label is a fixed literal from _ROLE_LABELS — never untrusted
        # payload — so it's safe as border chrome. Quiet meta roles (dim/session)
        # map to "" and get no title, staying borderless per the card CSS.
        label = _ROLE_LABELS.get(role, "")
        if label:
            self.border_title = label


class ToolCard(Static):
    """One evolving transcript card for a single tool call, keyed by call_id.

    A tool call emits up to three events sharing a call_id — request, an optional
    approval resolution (only for safety-gated tools), and a result. Rather than
    mint a separate line per event, one ``ToolCard`` is mounted on the request and
    then *mutated in place* as the later events arrive. The card carries its status
    in a leading glyph plus the role CSS class (which colors the left rule), so the
    whole lifecycle reads as one line transitioning pending → running → done/error
    instead of three stacked cards the reader has to reconcile.

    Parallel calls each own a stable card regardless of finish order, because the
    registry (in ``TextualTui``) routes every event to the card for its call_id.
    """

    # status → (leading glyph, role class). The role class drives the left-rule
    # color via the shared `.message--{role}` CSS in TextualTui.
    _STATUS: dict[str, tuple[str, str]] = {
        "pending": ("⋯", "tool"),
        "denied": ("✗", "denied"),
        "error": ("✗", "denied"),
        "cancelled": ("⊘", "denied"),
        "done": ("✓", "approved"),
    }
    _TICK = 1.0  # the running counter only needs whole-second granularity

    def __init__(self, name: str, arguments: object) -> None:
        super().__init__("")
        self._name = name
        self._summary = _summarize_arguments(arguments)
        self._detail = ""
        self._role = ""
        self._glyph = "⋯"
        # While running, `_elapsed` is a live whole-second tick count (looks alive,
        # exact precision doesn't matter mid-flight). On resolve it's replaced by
        # the true wall-clock duration derived from event timestamps (see
        # `set_state(elapsed=…)`), so the number that rests on screen is honest.
        self._elapsed: float | None = None
        self._timer: Timer | None = None
        self.set_state("pending")

    def on_mount(self) -> None:
        # A pending card ticks a running counter; a card that mounts already
        # resolved (e.g. rebuilt from history) has no timer to start.
        if self._role == "tool":
            self._elapsed = 0.0
            self._timer = self.set_interval(self._TICK, self._tick)
            self._repaint()

    def on_unmount(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._elapsed = (self._elapsed or 0.0) + self._TICK
        self._repaint()

    def set_state(self, status: str, *, detail: str = "", elapsed: float | None = None) -> None:
        """Transition the card to a new status, swapping glyph, color, and detail.

        ``detail`` overrides the argument summary (used to show a denial reason or
        a one-line result). ``elapsed`` is the true wall-clock duration (from the
        request/result event timestamps); passing it freezes the live counter at
        the honest value and stops the per-card timer. The role CSS class is
        swapped rather than added so the left-rule color reflects only the current
        state.
        """

        glyph, role = self._STATUS.get(status, self._STATUS["pending"])
        self._glyph = glyph
        if detail:
            self._detail = detail
        if status != "pending":
            # Any terminal state (done/error/denied/cancelled) ends the call: stop
            # the live counter so a resolved card can never keep ticking. Freeze at
            # the true wall-clock duration when we have it; otherwise leave the last
            # ticked value (e.g. a cancel with no result timestamp to diff against).
            if elapsed is not None:
                self._elapsed = elapsed
            self._stop_timer()
        if role != self._role:
            if self._role:
                self.remove_class(f"message--{self._role}")
            self.add_class("message", f"message--{role}")
            self._role = role
        self.border_title = _ROLE_LABELS.get(role, "tool")
        self._repaint()

    def _repaint(self) -> None:
        # name is a fixed tool identifier; summary/detail are escaped as untrusted
        # payload (a path or output line the model or a file supplied), preserving
        # the escape-at-boundary invariant the transcript relies on.
        body = self._detail or self._summary
        text = f"{self._glyph} [b]{_markup_escape(self._name)}[/b]"
        if body:
            text += f"  [dim]{_markup_escape(body)}[/dim]"
        if self._elapsed is not None:
            text += f" [dim]· {_format_duration(self._elapsed)}[/dim]"
        self.update(text)


class WorkingMessage(Static):
    """Transient working indicator: a spinner, a steady label, and an elapsed timer.

    The spinner is the classic 10-frame braille cycle, whose lit dots rotate
    around a single cell so the eye reads smooth rotation rather than a blink.
    Elapsed seconds are derived from the tick count (frames × interval), not a
    wall clock — keeping the TUI layer clock-free and the counter monotonic. The
    spinner animates every frame; the label re-renders only when the whole-second
    count changes, so the counter ticks once a second without stuttering the
    spinner.
    """

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    _INTERVAL = 0.08  # ~12.5 fps: fluid braille rotation without churning the CPU

    def __init__(self) -> None:
        super().__init__("")
        self.add_class("message", "message--dim")
        # A single monotonic tick counter drives everything: the spinner frame is
        # ticks % len(frames), and elapsed seconds is ticks × interval.
        self._ticks = 0
        self._timer: Timer | None = None
        self._render_frame()

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._INTERVAL, self._tick)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._ticks += 1
        self._render_frame()

    def _render_frame(self) -> None:
        spinner = self._FRAMES[self._ticks % len(self._FRAMES)]
        seconds = int(self._ticks * self._INTERVAL)
        self.update(f"[$accent]{spinner}[/$accent] Working… [dim]{seconds}s[/dim]")


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
        # Match the finalized-assistant card so the streamed and settled turns
        # look identical (same role label + card CSS).
        self.border_title = _ROLE_LABELS["assistant"]
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
