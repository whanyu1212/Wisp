"""Share a saved session through the picker and read it in the current conversation."""

from __future__ import annotations

import fcntl
import os
import pty
import struct
import sys
import termios
from pathlib import Path

import anyio
import pytest

from tests.tui_e2e.support import screen_shows
from tests.tui_e2e.test_readiness import _rust_binary, _TuiProcess
from wisp import __version__
from wisp.messages import Message
from wisp.sessions.jsonl import JsonlSessionStore

_BACKEND = """
import sys
from pathlib import Path

import anyio

from wisp.cli.rpc import _negotiate_rpc_connection, _run_rpc_with_runtime
from wisp.config.runtime import WispConfig
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.host import build_session_runtime

root, project, active_id = sys.argv[1:]
config = WispConfig(provider="scripted", model="scripted-history", session_dir=Path(root))
call = ToolCall(call_id="read-history", name="read_saved_session", arguments={})
usage = ProviderUsage(input_tokens=3, output_tokens=2, total_tokens=5)

async def main():
    if not await _negotiate_rpc_connection():
        return
    runtime = await build_session_runtime(config, connect_mcp=False)
    runtime.providers.register(ScriptedProvider([
        [
            ProviderResponseStarted(model="scripted-history"),
            ProviderToolCallCompleted(tool_call=call),
            ProviderResponseCompleted(
                content="", tool_calls=(call,), finish_reason="tool_calls", usage=usage,
            ),
        ],
        [
            ProviderResponseStarted(model="scripted-history"),
            ProviderResponseCompleted(content="Finished reading history.", usage=usage),
        ],
    ]))
    try:
        await _run_rpc_with_runtime(
            config, runtime, all_tools=True, startup_trusted=True,
            project_context_root=Path(project), resume=active_id,
        )
    finally:
        await runtime.aclose()

anyio.run(main)
"""


@pytest.mark.process
def test_picker_shares_into_current_chat(tmp_path: Path) -> None:
    binary = _rust_binary()
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "sessions"
    store = JsonlSessionStore(root)
    source, active = store.create(), store.create()

    async def seed() -> None:
        await source.append_message(Message(role="user", content="Compare the two proposals."))
        await source.append_message(Message(role="assistant", content="We chose the quiet design."))
        await source.set_name("Earlier decision")
        await active.append_message(Message(role="user", content="Current conversation."))

    anyio.run(seed)
    incomplete_tail = b'{"interrupted":'
    original = source.path.read_bytes() + incomplete_tail
    backend = tmp_path / "backend.py"
    backend.write_text(_BACKEND, encoding="utf-8")

    child, fd = pty.fork()
    if child == 0:
        os.chdir(tmp_path)
        environment = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "WISP_TRUST": "1"}
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                str(root),
                str(tmp_path),
                active.session_id,
            ],
            environment,
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 26, 110, 0, 0))
    tui = _TuiProcess(child, fd, termios.tcgetattr(fd))
    try:
        tui.wait_until(
            lambda output: (
                screen_shows(output, "Current conversation.")
                and screen_shows(output, "scripted-history")
                and screen_shows(output, "ctx")
                and not screen_shows(output, "Refreshing context")
            ),
            failure="current session did not hydrate",
        )
        source.path.write_bytes(original)
        source.path.with_suffix(".jsonl.lock").unlink(missing_ok=True)
        unfinished_session = store.create().path
        unfinished_session.write_bytes(incomplete_tail)
        catalog_entries = set(root.iterdir())
        tui.send(b"/past-sessions\r")
        tui.wait_until(
            lambda output: (
                screen_shows(output, "share a saved session")
                and screen_shows(output, f"[{source.session_id[:12]}]")
            ),
            failure="share picker did not open",
        )
        assert source.path.read_bytes() == original
        assert unfinished_session.read_bytes() == incomplete_tail
        assert set(root.iterdir()) == catalog_entries
        tui.send(b"Earlier decision")
        tui.wait_until(
            lambda output: (
                screen_shows(output, "Earlier decision")
                and screen_shows(output, f"[{source.session_id[:12]}]")
                and screen_shows(output, "share for one turn")
                and not screen_shows(output, f"[{active.session_id[:12]}]")
            ),
            failure="the searched saved session did not paint",
        )
        assert source.path.read_bytes() == original
        assert set(root.iterdir()) == catalog_entries
        tui.send(b"\r")
        tui.wait_until(
            lambda output: (
                screen_shows(output, "Saved Earlier dec")
                and screen_shows(output, "Current conversation.")
                and not screen_shows(output, "share a saved session")
            ),
            failure="share did not attach to the current composer",
        )
        tui.send(b"What did we choose?")
        tui.submit(
            lambda output: screen_shows(output, "Finished reading history."),
            failure="the shared prompt did not complete",
        )
        tui.quit()
    finally:
        tui.close()

    messages = active.read_message_page().messages
    assert any(
        msg.role == "tool" and "We chose the quiet design." in msg.content for msg in messages
    )
    assert [msg.content for msg in messages if msg.role == "user"] == [
        "Current conversation.",
        "What did we choose?",
    ]
    assert source.path.read_bytes() == original
