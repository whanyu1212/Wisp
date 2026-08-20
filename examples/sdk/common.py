"""Shared helpers for the canonical SDK examples."""

from __future__ import annotations

from collections.abc import AsyncIterator

from wisp.events import KnownWispEvent, RpcCommandFinished


async def events_until_finished(
    events: AsyncIterator[KnownWispEvent],
    command_id: str,
) -> tuple[KnownWispEvent, ...]:
    """Read one shared event iterator through a command's terminal event."""

    observed: list[KnownWispEvent] = []
    async for event in events:
        observed.append(event)
        if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
            if not event.ok:
                raise RuntimeError(event.error or f"Command {command_id} failed")
            return tuple(observed)
    raise RuntimeError(f"Event stream closed before command {command_id} finished")
