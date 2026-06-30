"""Extension activation helpers.

This module intentionally supports only built-in/static extension factories for
now. Loading arbitrary project code belongs behind a future trust boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable

from wisp.extensions import builtin
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry

type ExtensionFactory = Callable[[ExtensionAPI], Awaitable[None] | None]


async def build_runtime() -> WispRuntime:
    """Create runtime state and activate built-in extensions."""

    providers = ProviderRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, events=events)
    runtime = WispRuntime(providers=providers, events=events, api=api)
    await activate_builtin_extensions(api)
    return runtime


async def activate_builtin_extensions(api: ExtensionAPI) -> None:
    """Activate extensions that ship with Wisp."""

    await activate_extensions(api, [builtin.activate])


async def activate_extensions(api: ExtensionAPI, extensions: Sequence[ExtensionFactory]) -> None:
    """Activate extension factories in order."""

    for extension in extensions:
        result = extension(api)
        if isawaitable(result):
            await result
