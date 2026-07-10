"""Extension activation helpers.

This module intentionally supports only built-in/static extension factories for
now. Loading arbitrary *project-local* extensions must be gated on project trust
(see :mod:`wisp.trust` and ``Agent.trusted``): an untrusted project's code must not
be loaded. That gate now exists; wiring project-local extension discovery behind it
is future work (roadmap ``Extension System``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from pathlib import Path

from wisp.auth.storage import JsonAuthStore
from wisp.config import default_auth_path
from wisp.extensions import builtin
from wisp.retry import RetryPolicy
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry

type ExtensionFactory = Callable[[ExtensionAPI], Awaitable[None] | None]


async def build_runtime(
    *,
    auth_path: Path | None = None,
    retry_policy: RetryPolicy | None = None,
) -> WispRuntime:
    """Create runtime state and activate built-in extensions."""

    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    runtime = WispRuntime(providers=providers, tools=tools, events=events, api=api)
    await activate_builtin_extensions(
        api,
        auth_store=JsonAuthStore(auth_path or default_auth_path()),
        retry_policy=retry_policy,
    )
    return runtime


async def activate_builtin_extensions(
    api: ExtensionAPI,
    *,
    auth_store: JsonAuthStore | None = None,
    retry_policy: RetryPolicy | None = None,
) -> None:
    """Activate extensions that ship with Wisp."""

    builtin.activate(api, auth_store=auth_store, retry_policy=retry_policy)


async def activate_extensions(api: ExtensionAPI, extensions: Sequence[ExtensionFactory]) -> None:
    """Activate extension factories in order."""

    for extension in extensions:
        result = extension(api)
        if isawaitable(result):
            await result
