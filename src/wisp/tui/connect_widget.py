"""Native Textual provider connection and credential-entry panel."""

from __future__ import annotations

import time
from typing import Literal

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from wisp.tui.connections import (
    ConnectionMethodStatus,
    ConnectionProviderStatus,
)

type ConnectPanelMode = Literal["connect", "disconnect"]


class ConnectPanel(Vertical):
    """Multi-step provider picker with masked API-key entry."""

    BINDING_GROUP_TITLE = "Provider connection"
    HELP = """
    # Provider connection

    Select a provider and authentication method with the arrow keys and Enter.
    API keys are masked and never enter the transcript. Escape cancels.
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ConnectPanel {
        display: none;
        height: auto;
        max-height: 20;
        margin: 0 1;
        padding: 0 1;
        border-left: heavy $accent;
        background: $surface;
    }

    ConnectPanel #connect-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    ConnectPanel #connect-options {
        height: auto;
        max-height: 14;
        border: none;
        background: transparent;
        padding: 0;
    }

    ConnectPanel #connect-options > .option-list--option-highlighted {
        background: $accent 30%;
    }

    ConnectPanel #connect-api-key {
        display: none;
        height: 3;
        border: tall $accent;
        background: transparent;
    }

    ConnectPanel #connect-hint {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    class MethodSelected(Message):
        """A non-secret authentication method selected by the user."""

        def __init__(self, method: ConnectionMethodStatus) -> None:
            super().__init__()
            self.method = method

    class ApiKeySubmitted(Message):
        """A redacted signal that a key is ready for one-time retrieval."""

        def __init__(self, provider: str) -> None:
            super().__init__()
            self.provider = provider

    class DisconnectSelected(Message):
        """A provider credential selected for removal."""

        def __init__(self, provider: str) -> None:
            super().__init__()
            self.provider = provider

    class Cancelled(Message):
        """The connection workflow was dismissed."""

    def __init__(self, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._title = Static("Connect a provider", id="connect-title", markup=False)
        self._options = OptionList(id="connect-options")
        self._api_key = Input(
            password=True,
            placeholder="Paste API key",
            id="connect-api-key",
        )
        self._hint = Static("enter select · esc cancel", id="connect-hint", markup=False)
        self._providers: tuple[ConnectionProviderStatus, ...] = ()
        self._rows: list[ConnectionProviderStatus | ConnectionMethodStatus] = []
        self._mode: ConnectPanelMode = "connect"
        self._selected_method: ConnectionMethodStatus | None = None
        self._pending_api_key: str | None = None
        self._opened_at = 0.0

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._options
        yield self._api_key
        yield self._hint

    @property
    def is_open(self) -> bool:
        return self.display

    def show(
        self,
        providers: tuple[ConnectionProviderStatus, ...],
        *,
        mode: ConnectPanelMode = "connect",
        provider: str | None = None,
    ) -> None:
        self._providers = providers
        self._mode = mode
        self._selected_method = None
        self._pending_api_key = None
        self._api_key.value = ""
        self._api_key.display = False
        self._options.display = True
        self._opened_at = time.monotonic()
        self.display = True
        if provider is None:
            self._show_providers()
        else:
            method = self._method_for_provider(provider)
            if method is None:
                self._show_providers()
            elif mode == "disconnect":
                self.post_message(self.DisconnectSelected(method.provider))
            else:
                self._select_method(method)

    def hide(self) -> None:
        self.display = False
        self._api_key.value = ""
        self._pending_api_key = None
        self._selected_method = None

    def take_api_key(self) -> str | None:
        """Return and forget the submitted key without putting it on a Message."""

        api_key = self._pending_api_key
        self._pending_api_key = None
        return api_key

    def show_error(self, message: str) -> None:
        """Keep a recoverable failure visible inside the active workflow."""

        self._title.update("Connection failed")
        self._options.display = False
        self._api_key.display = False
        self._hint.update(f"{message} · esc close")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._options:
            return
        event.stop()
        if event.time < self._opened_at:
            return
        index = event.option_index
        if not 0 <= index < len(self._rows):
            return
        row = self._rows[index]
        if isinstance(row, ConnectionProviderStatus):
            if self._mode == "disconnect":
                connected = tuple(method for method in row.methods if method.connected)
                if len(connected) == 1:
                    self.post_message(self.DisconnectSelected(connected[0].provider))
                else:
                    self._show_methods(row, connected_only=True)
            elif len(row.methods) == 1:
                self._select_method(row.methods[0])
            else:
                self._show_methods(row)
            return
        if self._mode == "disconnect":
            self.post_message(self.DisconnectSelected(row.provider))
        else:
            self._select_method(row)

    @on(Input.Submitted, "#connect-api-key")
    def on_api_key_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        method = self._selected_method
        api_key = event.value.strip()
        self._api_key.value = ""
        if method is None or not api_key:
            self._hint.update("API key cannot be empty · esc cancel")
            return
        self._pending_api_key = api_key
        self.post_message(self.ApiKeySubmitted(method.provider))

    def on_key(self, event: events.Key) -> None:
        if self.is_open and event.time < self._opened_at:
            event.prevent_default()
            event.stop()

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())

    def _show_providers(self) -> None:
        self._title.update("Disconnect a provider" if self._mode == "disconnect" else "Connect")
        self._rows = [
            provider
            for provider in self._providers
            if self._mode == "connect" or any(method.connected for method in provider.methods)
        ]
        self._options.clear_options()
        for provider in self._rows:
            assert isinstance(provider, ConnectionProviderStatus)
            connected = sum(method.connected for method in provider.methods)
            status = f"{connected} connected" if connected else "not connected"
            self._options.add_option(Option(Content(f"{provider.label} · {status}")))
        if not self._rows:
            self._options.add_option(Option("No stored connections.", disabled=True))
        else:
            self._options.highlighted = 0
        self._options.display = True
        self._api_key.display = False
        self._hint.update("enter select · esc cancel")
        self._options.focus()

    def _show_methods(
        self,
        provider: ConnectionProviderStatus,
        *,
        connected_only: bool = False,
    ) -> None:
        methods = tuple(
            method for method in provider.methods if not connected_only or method.connected
        )
        self._title.update(provider.label)
        self._rows = list(methods)
        self._options.clear_options()
        for method in methods:
            status = _source_label(method)
            self._options.add_option(Option(Content(f"{method.label} · {status}")))
        if methods:
            self._options.highlighted = 0
        self._hint.update("enter select · esc cancel")
        self._options.focus()

    def _select_method(self, method: ConnectionMethodStatus) -> None:
        if method.kind == "device_code":
            self._selected_method = method
            self._title.update(method.label)
            self._options.display = False
            self._hint.update("starting device authorization · esc cancel")
            self.post_message(self.MethodSelected(method))
            return
        self._selected_method = method
        self._title.update(method.label)
        self._options.display = False
        self._api_key.display = True
        self._hint.update("enter save · esc cancel")
        self._api_key.focus()

    def _method_for_provider(self, provider: str) -> ConnectionMethodStatus | None:
        return next(
            (
                method
                for family in self._providers
                for method in family.methods
                if method.provider == provider
            ),
            None,
        )


def _source_label(method: ConnectionMethodStatus) -> str:
    if method.source == "environment":
        return f"connected via {method.environment_variable}"
    if method.source == "stored":
        return "connected"
    return "not connected"


__all__ = ["ConnectPanel", "ConnectPanelMode"]
