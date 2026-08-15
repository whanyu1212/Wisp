"""Visual context-budget status for the Textual frontend."""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Label, ProgressBar, Static

from wisp.coding.costs import format_cost_summary
from wisp.events import SessionCostSummary, SessionStats, TokenUsage


@dataclass(frozen=True)
class ContextStatusPresentation:
    """Literal, renderer-ready values derived from authoritative session stats."""

    current_tokens: int
    context_window: int | None
    percentage: float | None
    source: str
    reserve: str
    remaining: str
    trigger: str
    compaction: str
    eligibility: str
    overflow_recovery: str
    prompt_cache: str
    cost: str
    over_budget: bool


def context_status_presentation(stats: SessionStats) -> ContextStatusPresentation:
    """Derive context display values without coupling them to widget layout."""

    context = stats.context
    observed = context.observed_is_current and context.observed_tokens is not None
    fallback_tokens = (
        context.observed_tokens
        if observed and context.observed_tokens is not None
        else context.estimate.total_tokens
    )
    current_tokens = (
        context.effective_tokens
        if context.accounting_method == "provider_observed_plus_estimate"
        and context.effective_tokens is not None
        else fallback_tokens
    )
    window = context.context_window
    percentage = current_tokens / window * 100 if window is not None else None
    trigger_tokens = (
        window - context.reserve_tokens
        if window is not None and context.reserve_tokens < window
        else None
    )
    remaining_tokens = (
        window - context.reserve_tokens - current_tokens if window is not None else None
    )
    policy = stats.compaction
    eligibility = (
        "unavailable · status unavailable"
        if policy is None
        else (
            "eligible"
            if policy.threshold_eligible
            else f"unavailable · {policy.threshold_ineligible_reason or 'not available'}"
        )
    )
    return ContextStatusPresentation(
        current_tokens=current_tokens,
        context_window=window,
        percentage=percentage,
        source=(
            "provider observation + trailing estimate"
            if context.accounting_method == "provider_observed_plus_estimate"
            else "provider observation"
            if observed
            else "deterministic estimate (approximate)"
        ),
        reserve=_format_tokens(context.reserve_tokens),
        remaining=(_format_tokens(remaining_tokens) if remaining_tokens is not None else "unknown"),
        trigger=(
            f">{_format_tokens(trigger_tokens)}" if trigger_tokens is not None else "unavailable"
        ),
        compaction=(
            "unavailable" if policy is None else ("on" if policy.auto_compaction_enabled else "off")
        ),
        eligibility=eligibility,
        overflow_recovery=(
            "unavailable"
            if policy is None
            else ("on" if policy.overflow_recovery_enabled else "off")
        ),
        prompt_cache=format_prompt_cache_usage(stats.usage),
        cost=_format_cost(stats.cost),
        over_budget=bool(window is not None and current_tokens >= window - context.reserve_tokens),
    )


def _format_tokens(value: int) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        formatted = f"{magnitude / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"
        return sign + formatted
    if magnitude >= 1_000:
        formatted = f"{magnitude / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
        return sign + formatted
    return str(value)


def _format_cost(cost: SessionCostSummary) -> str:
    summary = format_cost_summary(cost)
    if not summary:
        return "unavailable"
    return summary.removeprefix("cost ")


def format_prompt_cache_usage(usage: TokenUsage) -> str:
    """Format provider-reported cache reads and writes without inventing missing data."""

    cache_read = usage.cache_read_input_tokens
    cache_write = usage.cache_write_input_tokens
    if cache_read is None and cache_write is None:
        return "unavailable"
    read = f"{_format_tokens(cache_read)} read" if cache_read is not None else "reads unreported"
    write = (
        f"{_format_tokens(cache_write)} written" if cache_write is not None else "writes unreported"
    )
    return f"{read} · {write}"


class ContextStatusOverlay(Vertical):
    """Dismissible visual report for one explicitly requested context snapshot."""

    BINDING_GROUP_TITLE = "Context status"
    HELP = """
    # Context status

    This report shows the latest authoritative token usage, reserve, compaction
    policy, overflow recovery state, and estimated cost. It is informational and
    does not compact or otherwise change the active session. Escape closes it.
    """
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    DEFAULT_CSS = """
    ContextStatusOverlay {
        overlay: screen;
        display: none;
        width: 100%;
        height: 100%;
        align: center middle;
        background: transparent;
    }

    ContextStatusOverlay #context-status-panel {
        width: 48;
        max-width: 94%;
        height: auto;
        max-height: 94%;
        padding: 0 2;
        border: heavy $accent;
        background: $panel;
    }

    ContextStatusOverlay #context-status-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    ContextStatusOverlay #context-status-progress {
        height: 1;
        margin-top: 1;
    }

    ContextStatusOverlay .context-status-row {
        height: auto;
        min-height: 1;
        color: $text;
    }

    ContextStatusOverlay #context-status-source,
    ContextStatusOverlay #context-status-detail,
    ContextStatusOverlay #context-status-cost {
        color: $text-muted;
    }

    ContextStatusOverlay #context-status-warning {
        display: none;
        height: 1;
        color: $warning;
        text-style: bold;
    }

    ContextStatusOverlay #context-status-actions {
        height: 3;
        align-horizontal: right;
    }

    ContextStatusOverlay #context-status-close {
        min-width: 10;
    }
    """

    class Cancelled(Message):
        """The reader dismissed the context report."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 - Textual API
        super().__init__(id=id)
        self._progress = ProgressBar(
            total=100,
            show_eta=False,
            id="context-status-progress",
        )
        self._usage = Label("", classes="context-status-row", id="context-status-usage")
        self._source = Label("", classes="context-status-row", id="context-status-source")
        self._detail = Label("", classes="context-status-row", id="context-status-detail")
        self._policy = Label("", classes="context-status-row", id="context-status-policy")
        self._eligibility = Label("", classes="context-status-row", id="context-status-eligibility")
        self._recovery = Label("", classes="context-status-row", id="context-status-recovery")
        self._prompt_cache = Label(
            "", classes="context-status-row", id="context-status-prompt-cache"
        )
        self._cost = Label("", classes="context-status-row", id="context-status-cost")
        self._warning = Static("Context threshold reached", id="context-status-warning")
        self._close = Button("Close", id="context-status-close")

    def compose(self) -> ComposeResult:
        with Vertical(id="context-status-panel"):
            yield Static("Context", id="context-status-title")
            yield self._progress
            yield self._usage
            yield self._source
            yield self._detail
            yield self._warning
            yield self._policy
            yield self._eligibility
            yield self._recovery
            yield self._prompt_cache
            yield self._cost
            with Horizontal(id="context-status-actions"):
                yield self._close

    @property
    def is_open(self) -> bool:
        return self.display

    def show_stats(self, stats: SessionStats) -> None:
        """Render and show one authoritative session-stat snapshot."""

        view = context_status_presentation(stats)
        if view.percentage is None:
            self._progress.display = False
            percent = "unknown"
        else:
            self._progress.display = True
            self._progress.update(progress=min(100.0, max(0.0, view.percentage)))
            percent = f"{view.percentage:.0f}%"
        window = (
            _format_tokens(view.context_window) if view.context_window is not None else "unknown"
        )
        self._usage.update(f"Usage: {_format_tokens(view.current_tokens)} / {window} · {percent}")
        self._source.update(f"Source: {view.source}")
        self._detail.update(f"Reserve: {view.reserve} · Remaining: {view.remaining}")
        self._policy.update(f"Automatic compaction: {view.compaction} · Trigger: {view.trigger}")
        self._eligibility.update(f"Threshold: {view.eligibility}")
        self._recovery.update(f"Overflow recovery: {view.overflow_recovery}")
        self._prompt_cache.update(f"Prompt cache (reported): {view.prompt_cache}")
        self._cost.update(f"Cost: {view.cost}")
        self._warning.display = view.over_budget
        self.display = True
        self._close.focus()

    def hide(self) -> None:
        self.display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self._close:
            event.stop()
            self.post_message(self.Cancelled())

    def action_close(self) -> None:
        if self.is_open:
            self.post_message(self.Cancelled())
