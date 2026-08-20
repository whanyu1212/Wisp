"""Handle project trust and tool approvals with an explicit safe policy."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

import anyio

from wisp.config import WispConfig
from wisp.events import KnownWispEvent, RpcCommandFinished, ToolApprovalRequested, TrustRequested
from wisp.rpc import ApprovalScope
from wisp.sdk import InProcessOptions, InProcessWisp


class SafetyController(Protocol):
    """Controller methods required by the safety-request policy."""

    async def trust(
        self,
        request_id: str,
        *,
        trusted: bool,
        reason: str | None = None,
        transient: bool = False,
        command_id: str | None = None,
    ) -> str: ...

    async def approve(
        self,
        call_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
        scope: ApprovalScope | None = None,
        command_id: str | None = None,
    ) -> str: ...


async def resolve_safety_request(
    controller: SafetyController,
    event: KnownWispEvent,
) -> str | None:
    """Deny unresolved trust and unsafe execution unless the application opts in."""

    if isinstance(event, TrustRequested):
        return await controller.trust(
            event.request_id,
            trusted=False,
            transient=True,
            reason="The embedding application did not trust this project",
        )
    if isinstance(event, ToolApprovalRequested):
        return await controller.approve(
            event.call_id,
            approved=False,
            reason="The embedding application did not authorize this tool call",
        )
    return None


async def run_with_safe_defaults(workspace: Path, session_dir: Path) -> None:
    """Run offline while resolving re-entrant safety requests from the event loop."""

    workspace.mkdir(parents=True, exist_ok=True)
    controller = await InProcessWisp.start(
        WispConfig(
            provider="fake",
            session_dir=session_dir,
            update_check_enabled=False,
        ),
        options=InProcessOptions(project_context_root=workspace),
    )
    async with controller:
        prompt_id = await controller.prompt("respond without tools")
        async for event in controller.events():
            await resolve_safety_request(controller, event)
            if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                if not event.ok:
                    raise RuntimeError(event.error or "Prompt failed")
                return
    raise RuntimeError("Event stream closed before the prompt finished")


async def main() -> None:
    """Run the safety example without modifying the user's trust store."""

    with tempfile.TemporaryDirectory(prefix="wisp-sdk-safety-") as temporary_directory:
        root = Path(temporary_directory)
        await run_with_safe_defaults(root / "workspace", root / "sessions")
    print("prompt completed with safe defaults")


if __name__ == "__main__":
    anyio.run(main)
