"""Run one deterministic in-process prompt and print its typed text stream."""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio

from wisp.config import WispConfig
from wisp.events import MessageDelta, RpcCommandFinished
from wisp.sdk import InProcessOptions, InProcessWisp


async def prompt_once(workspace: Path, session_dir: Path) -> str:
    """Return one fake-provider response without credentials or network access."""

    workspace.mkdir(parents=True, exist_ok=True)
    controller = await InProcessWisp.start(
        WispConfig(
            provider="fake",
            session_dir=session_dir,
            update_check_enabled=False,
        ),
        options=InProcessOptions(
            startup_trusted=True,
            project_context_root=workspace,
        ),
    )
    async with controller:
        events = controller.events()
        command_id = await controller.prompt("hello from the SDK")
        observed = []
        async for event in events:
            observed.append(event)
            if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                if not event.ok:
                    raise RuntimeError(event.error or "Prompt failed")
                break
        else:
            raise RuntimeError("Event stream closed before the prompt finished")

    return "".join(
        event.delta
        for event in observed
        if isinstance(event, MessageDelta) and event.content_kind == "text"
    )


async def main() -> None:
    """Run the copy-paste example in caller-owned temporary directories."""

    with tempfile.TemporaryDirectory(prefix="wisp-sdk-") as temporary_directory:
        root = Path(temporary_directory)
        response = await prompt_once(root / "workspace", root / "sessions")
    print(response)


if __name__ == "__main__":
    anyio.run(main)
