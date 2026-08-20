"""Drive Wisp through its process-isolated JSONL RPC transport."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import anyio

from examples.sdk.common import events_until_finished
from wisp.events import MessageDelta
from wisp.rpc import JsonlSubprocessRpcTransport, RpcController


async def prompt_in_subprocess(workspace: Path, session_dir: Path) -> str:
    """Run a fake-provider prompt in a child process and return its text."""

    workspace.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "WISP_PROVIDER": "fake",
            "WISP_SESSION_DIR": str(session_dir),
            "WISP_TRUST": "1",
            "WISP_UPDATE_CHECK": "0",
        }
    )
    transport = await JsonlSubprocessRpcTransport.start(cwd=workspace, env=environment)
    controller = RpcController(transport)
    try:
        events = controller.events()
        command_id = await controller.prompt("hello over JSONL RPC")
        observed = await events_until_finished(events, command_id)
    finally:
        await controller.close()

    return "".join(
        event.delta
        for event in observed
        if isinstance(event, MessageDelta) and event.content_kind == "text"
    )


async def main() -> None:
    """Run process-isolated Wisp without credentials or network access."""

    with tempfile.TemporaryDirectory(prefix="wisp-sdk-rpc-") as temporary_directory:
        root = Path(temporary_directory)
        response = await prompt_in_subprocess(root / "workspace", root / "sessions")
    print(response)


if __name__ == "__main__":
    anyio.run(main)
