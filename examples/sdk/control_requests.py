"""Construct typed steering, follow-up, cancellation, and compaction requests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import anyio

from wisp.events import KnownWispEvent
from wisp.rpc import RpcCommand, RpcController


class RecordingTransport:
    """Small public-protocol transport for inspecting controller requests offline."""

    def __init__(self) -> None:
        self.commands: list[RpcCommand] = []

    async def send(self, command: RpcCommand) -> None:
        self.commands.append(command)

    async def close(self) -> None:
        return None

    def events(self) -> AsyncIterator[KnownWispEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[KnownWispEvent]:
        if False:  # pragma: no cover - establishes the async iterator's item type
            yield cast(KnownWispEvent, None)


async def build_control_requests() -> tuple[RpcCommand, ...]:
    """Return the exact typed requests an interactive controller may submit."""

    transport = RecordingTransport()
    controller = RpcController(transport)
    prompt_id = await controller.prompt("inspect the project", command_id="prompt-1")
    await controller.steer("focus on the failing test", command_id="steer-1")
    await controller.follow_up("summarize the final diff", command_id="follow-up-1")
    await controller.cancel(prompt_id, command_id="cancel-1")
    await controller.compact("retain decisions and test results", command_id="compact-1")
    await controller.close()
    return tuple(transport.commands)


async def main() -> None:
    """Print JSONL requests suitable for the shared RPC command host."""

    for command in await build_control_requests():
        print(command.to_json_line(), end="")


if __name__ == "__main__":
    anyio.run(main)
