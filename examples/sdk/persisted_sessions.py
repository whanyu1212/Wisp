"""Create, resume, inspect, clone, and fork persisted SDK sessions."""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio

from examples.sdk.common import events_until_finished
from wisp.config import WispConfig
from wisp.events import (
    RpcMessagesReported,
    RpcSessionCloned,
    RpcSessionForked,
    RpcSessionsReported,
)
from wisp.sdk import InProcessOptions, InProcessWisp


async def persisted_session_workflow(workspace: Path, session_dir: Path) -> dict[str, str]:
    """Run a complete persisted-session workflow using only public SDK events."""

    workspace.mkdir(parents=True, exist_ok=True)
    config = WispConfig(
        provider="fake",
        session_dir=session_dir,
        update_check_enabled=False,
    )
    options = InProcessOptions(
        startup_trusted=True,
        project_context_root=workspace,
    )

    first = await InProcessWisp.start(config, options=options)
    async with first:
        events = first.events()
        prompt_id = await first.prompt("persist this prompt")
        await events_until_finished(events, prompt_id)
        sessions_id = await first.get_sessions()
        session_events = await events_until_finished(events, sessions_id)
        sessions_report = next(
            event for event in session_events if isinstance(event, RpcSessionsReported)
        )
        source_id = sessions_report.selected_session_id
        if source_id is None:
            raise RuntimeError("Prompt completed without selecting a persisted session")

    resumed = await InProcessWisp.start(
        config,
        options=InProcessOptions(
            startup_trusted=True,
            project_context_root=workspace,
            resume=source_id,
        ),
    )
    async with resumed:
        events = resumed.events()
        messages_id = await resumed.get_messages()
        message_events = await events_until_finished(events, messages_id)
        messages_report = next(
            event for event in message_events if isinstance(event, RpcMessagesReported)
        )
        source_prompt = next(
            message for message in messages_report.messages if message.role == "user"
        )

        clone_id = await resumed.clone_session()
        clone_events = await events_until_finished(events, clone_id)
        clone_report = next(event for event in clone_events if isinstance(event, RpcSessionCloned))

        select_id = await resumed.select_session(source_id)
        await events_until_finished(events, select_id)
        fork_id = await resumed.fork_session(source_prompt.entry_id)
        fork_events = await events_until_finished(events, fork_id)
        fork_report = next(event for event in fork_events if isinstance(event, RpcSessionForked))

    return {
        "source": source_id,
        "clone": clone_report.session_id,
        "fork": fork_report.session_id,
        "editable_prompt": fork_report.selected_prompt,
    }


async def main() -> None:
    """Run the persisted workflow in a temporary session directory."""

    with tempfile.TemporaryDirectory(prefix="wisp-sdk-sessions-") as temporary_directory:
        root = Path(temporary_directory)
        result = await persisted_session_workflow(root / "workspace", root / "sessions")
    for name, value in result.items():
        print(f"{name}: {value}")


if __name__ == "__main__":
    anyio.run(main)
