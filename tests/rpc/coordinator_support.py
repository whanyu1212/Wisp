"""Input feeds and command builders shared by the RPC coordinator tests."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable

import anyio
from pydantic import ValidationError

from wisp.rpc.commands import ParsedRpcCommand, RpcCommandAdapter
from wisp.rpc.coordinator import _RpcInputCommand


class Receiver:
    def __init__(self, events: list[object]) -> None:
        self.events = deque(events)

    async def receive(self) -> object:
        await anyio.sleep(0)
        return self.events.popleft()

    def receive_nowait(self) -> object:
        if not self.events:
            raise anyio.WouldBlock
        return self.events.popleft()


def parsed_command(payload: dict[str, object]) -> ParsedRpcCommand:
    try:
        command = RpcCommandAdapter.validate_json(json.dumps(payload))
    except ValidationError as exc:
        if isinstance(payload.get("type"), str) and any(
            error["type"] == "union_tag_invalid" for error in exc.errors(include_input=False)
        ):
            return ParsedRpcCommand.from_unknown(payload)
        raise
    return ParsedRpcCommand.from_known(command, payload=payload)


def input_command(payload: dict[str, object]) -> _RpcInputCommand:
    return _RpcInputCommand(parsed_command(payload))


def expected_commands(payloads: Iterable[dict[str, object]]) -> list[ParsedRpcCommand]:
    return [parsed_command(payload) for payload in payloads]


def expected_rejections(
    pairs: Iterable[tuple[dict[str, object], str]],
) -> list[tuple[ParsedRpcCommand, str]]:
    return [(parsed_command(payload), message) for payload, message in pairs]


async def ignore_reject(_command: ParsedRpcCommand, _message: str) -> None:
    return None
