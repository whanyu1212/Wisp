from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import anyio
import pytest
from anyio.abc import Process
from pytest import MonkeyPatch

from tests.support.rpc import RecordingTransport
from wisp import __version__
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
)
from wisp.rpc import (
    ClearQueueCommand,
    CloneSessionCommand,
    CompactCommand,
    ConfigureCommand,
    FollowUpCommand,
    ForkSessionCommand,
    GetCommandsCommand,
    GetMcpStatusCommand,
    GetMessagesCommand,
    GetModelCatalogCommand,
    GetQueueStateCommand,
    GetSessionsCommand,
    GetSessionStatsCommand,
    GetSessionTreeCommand,
    GetSkillsCommand,
    GetStateCommand,
    InitCommand,
    JsonlSubprocessRpcTransport,
    NavigateSessionTreeCommand,
    NewSessionCommand,
    PopQueueCommand,
    RpcController,
    RpcHandshakeError,
    RpcProtocolError,
    SelectSessionCommand,
    SetQueueModeCommand,
    SetSessionNameCommand,
    SteerCommand,
    UnrevertSessionTreeCommand,
)
from wisp.rpc import client as rpc_client_module
from wisp.rpc.commands import (
    ApprovalCommand,
    CancelCommand,
    PromptCommand,
    ShutdownCommand,
    rpc_command_from_json,
)
from wisp.rpc.protocol import RpcHandshakeRequest


def test_rpc_controller_sends_typed_commands_and_closes_transport() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        prompt_id = await controller.prompt("hello")
        init_id = await controller.init()
        compact_id = await controller.compact("Keep paths")
        stats_id = await controller.get_session_stats()
        state_id = await controller.get_state()
        commands_id = await controller.get_commands()
        model_catalog_id = await controller.get_model_catalog()
        skills_id = await controller.get_skills()
        mcp_id = await controller.get_mcp_status()
        messages_id = await controller.get_messages(
            session_id="session-1",
            limit=25,
            before_entry_id="entry-1",
        )
        sessions_id = await controller.get_sessions(limit=25)
        new_session_id = await controller.new_session()
        select_id = await controller.select_session("session-1")
        clone_id = await controller.clone_session()
        fork_id = await controller.fork_session("entry-1")
        tree_id = await controller.get_session_tree(limit=25, after_entry_id="entry-1")
        navigate_id = await controller.navigate_session_tree("entry-2")
        unrevert_id = await controller.unrevert_session_tree()
        name_id = await controller.set_session_name("Display", session_id="session-1")
        steer_id = await controller.steer("redirect")
        follow_up_id = await controller.follow_up("continue")
        queue_state_id = await controller.get_queue_state()
        queue_mode_id = await controller.set_queue_mode("steering", "all")
        queue_pop_id = await controller.pop_queue("steering")
        queue_clear_id = await controller.clear_queue("follow_up")
        cancel_id = await controller.cancel(prompt_id)
        approval_id = await controller.approve(
            "call-1",
            approved=True,
            scope="tool_session",
        )
        configure_id = await controller.configure(provider="openai-codex", model="gpt-5.5")
        shutdown_id = await controller.shutdown()
        await controller.close()

        assert [
            prompt_id,
            init_id,
            compact_id,
            stats_id,
            state_id,
            commands_id,
            model_catalog_id,
            skills_id,
            mcp_id,
            messages_id,
            sessions_id,
            new_session_id,
            select_id,
            clone_id,
            fork_id,
            tree_id,
            navigate_id,
            unrevert_id,
            name_id,
            steer_id,
            follow_up_id,
            queue_state_id,
            queue_mode_id,
            queue_pop_id,
            queue_clear_id,
            cancel_id,
            approval_id,
            configure_id,
            shutdown_id,
        ] == [
            "prompt-id",
            "init-id",
            "compact-id",
            "stats-id",
            "state-id",
            "commands-id",
            "model-catalog-id",
            "skills-id",
            "mcp-id",
            "messages-id",
            "sessions-id",
            "new-session-id",
            "select-session-id",
            "clone-session-id",
            "fork-session-id",
            "session-tree-id",
            "navigate-session-tree-id",
            "unrevert-session-tree-id",
            "set-session-name-id",
            "steer-id",
            "follow-up-id",
            "queue-state-id",
            "queue-mode-id",
            "queue-pop-id",
            "queue-clear-id",
            "cancel-id",
            "approval-id",
            "configure-id",
            "shutdown-id",
        ]
        assert transport.commands == [
            PromptCommand(id="prompt-id", prompt="hello"),
            InitCommand(id="init-id"),
            CompactCommand(id="compact-id", instructions="Keep paths"),
            GetSessionStatsCommand(id="stats-id"),
            GetStateCommand(id="state-id"),
            GetCommandsCommand(id="commands-id"),
            GetModelCatalogCommand(id="model-catalog-id"),
            GetSkillsCommand(id="skills-id"),
            GetMcpStatusCommand(id="mcp-id"),
            GetMessagesCommand(
                id="messages-id",
                session_id="session-1",
                limit=25,
                before_entry_id="entry-1",
            ),
            GetSessionsCommand(id="sessions-id", limit=25),
            NewSessionCommand(id="new-session-id"),
            SelectSessionCommand(id="select-session-id", session_id="session-1"),
            CloneSessionCommand(id="clone-session-id"),
            ForkSessionCommand(id="fork-session-id", entry_id="entry-1"),
            GetSessionTreeCommand(
                id="session-tree-id",
                limit=25,
                after_entry_id="entry-1",
            ),
            NavigateSessionTreeCommand(
                id="navigate-session-tree-id",
                entry_id="entry-2",
            ),
            UnrevertSessionTreeCommand(id="unrevert-session-tree-id"),
            SetSessionNameCommand(
                id="set-session-name-id",
                name="Display",
                session_id="session-1",
            ),
            SteerCommand(id="steer-id", content="redirect"),
            FollowUpCommand(id="follow-up-id", content="continue"),
            GetQueueStateCommand(id="queue-state-id"),
            SetQueueModeCommand(id="queue-mode-id", kind="steering", mode="all"),
            PopQueueCommand(id="queue-pop-id", kind="steering"),
            ClearQueueCommand(id="queue-clear-id", kind="follow_up"),
            CancelCommand(id="cancel-id", target_id="prompt-id"),
            ApprovalCommand(
                id="approval-id",
                call_id="call-1",
                approved=True,
                scope="tool_session",
            ),
            ConfigureCommand(id="configure-id", provider="openai-codex", model="gpt-5.5"),
            ShutdownCommand(id="shutdown-id"),
        ]
        assert transport.closed is True

    anyio.run(run)


def test_rpc_controller_configure_sends_effort() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(effort="high")

        assert transport.commands == [
            ConfigureCommand(id="configure-id", effort="high"),
        ]

    anyio.run(run)


def test_rpc_controller_configure_sends_agent_mode() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda _prefix: "configure-id",
        )

        await controller.configure(mode="plan")

        assert transport.commands == [
            ConfigureCommand(id="configure-id", mode="plan"),
        ]

    anyio.run(run)


def test_rpc_controller_configure_sends_auto_compaction_setting() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(auto_compaction_enabled=False)

        assert transport.commands == [
            ConfigureCommand(id="configure-id", auto_compaction_enabled=False),
        ]

    anyio.run(run)


def test_configure_command_serializes_effort_and_omits_when_unset() -> None:
    with_effort = ConfigureCommand(id="configure-1", effort="medium")

    line = with_effort.to_json_line()

    assert json.loads(line) == {
        "id": "configure-1",
        "type": "configure",
        "effort": "medium",
        "clear_effort": False,
        "persist_model_selection": False,
    }
    assert rpc_command_from_json(line) == with_effort

    without_effort = ConfigureCommand(id="configure-2", model="gpt-5.5")

    assert "effort" not in json.loads(without_effort.to_json_line())


def test_rpc_controller_configure_clear_effort() -> None:
    # Regression test: effort=None is indistinguishable on the wire from
    # never having set effort at all (exclude_none drops it), so a client
    # that previously set an effort tier has no way to reset it back to the
    # provider's own default without a distinct, always-serialized signal.
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(
            transport,
            command_id_factory=lambda prefix: f"{prefix}-id",
        )

        await controller.configure(clear_effort=True)

        assert transport.commands == [
            ConfigureCommand(id="configure-id", clear_effort=True),
        ]

    anyio.run(run)


def test_configure_command_always_serializes_clear_effort() -> None:
    # clear_effort's default (False) must never be silently dropped the way
    # effort=None is -- it is the only signal the server has to distinguish
    # "leave effort untouched" from "explicitly reset effort."
    command = ConfigureCommand(id="configure-1", model="gpt-5.5")

    assert json.loads(command.to_json_line())["clear_effort"] is False


def test_rpc_controller_exposes_transport_events() -> None:
    async def run() -> None:
        expected_events = [
            RpcCommandStarted(command_id="cmd-1", command_type="prompt"),
            RpcCommandFinished(command_id="cmd-1", command_type="prompt", ok=True),
        ]
        controller = RpcController(RecordingTransport(events=expected_events))

        events = [event async for event in controller.events()]

        assert events == expected_events

    anyio.run(run)


def _fake_backend(script: str, *, backend_version: str = __version__) -> str:
    """Prefix a scripted RPC backend with the package version it reports."""

    return f"BACKEND_VERSION = {backend_version!r}\n{script}"


def test_jsonl_subprocess_rpc_transport_times_out_while_writing_handshake(
    monkeypatch: MonkeyPatch,
) -> None:
    class BlockingSendStream:
        async def send(self, _item: bytes) -> None:
            await anyio.sleep_forever()

    class BlockingProcess:
        stdin = BlockingSendStream()
        stdout = object()

    async def run() -> None:
        request = RpcHandshakeRequest(
            frontend_name="fixture",
            frontend_version="0.1.0",
            supported_capabilities=(),
            required_capabilities=(),
        )
        transport = JsonlSubprocessRpcTransport(
            cast(Process, BlockingProcess()),
            request,
        )
        monkeypatch.setattr(rpc_client_module, "_SUBPROCESS_HANDSHAKE_TIMEOUT_SECONDS", 0.01)

        with anyio.fail_after(1):
            with pytest.raises(RpcHandshakeError, match="did not complete handshake in time"):
                await transport._perform_handshake()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_round_trips_events(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": BACKEND_VERSION,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
command = json.loads(sys.stdin.readline())
started = {
    "type": "rpc.command.started",
    "command_id": command["id"],
    "command_type": command["type"],
}
finished = {
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}
print(json.dumps(started), flush=True)
print(json.dumps(finished), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", _fake_backend(script)],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == [
            "rpc.command.started",
            "rpc.command.finished",
        ]
        assert isinstance(events[0], RpcCommandStarted)
        assert isinstance(events[1], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[1].ok is True

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_rejects_versioned_event_frame(
    tmp_path: Path,
) -> None:
    """A per-event schema_version is a pre-v9 wire shape and must fail closed."""

    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": BACKEND_VERSION,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
print(json.dumps({
    "schema_version": 6,
    "type": "rpc.command.started",
    "command_id": "command-1",
    "command_type": "prompt",
}), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", _fake_backend(script)],
            cwd=tmp_path,
        )
        with pytest.raises(RpcProtocolError, match="invalid RPC event"):
            await anext(transport.events())
        await transport.close()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_rejects_empty_event_frame(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": BACKEND_VERSION,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
print("", flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", _fake_backend(script)],
            cwd=tmp_path,
        )
        with pytest.raises(RpcProtocolError, match="empty RPC event frame"):
            await anext(transport.events())
        await transport.close()

    anyio.run(run)


_ACCEPTING_BACKEND = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": BACKEND_VERSION,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
sys.stdin.read()
"""


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_rejects_a_different_backend_release(
    tmp_path: Path,
) -> None:
    """Another release negotiates v9 but may decode events differently.

    Additive event changes are made in place within a protocol version, so the
    SDK fails at connect time rather than on the first event it cannot decode.
    """

    async def run() -> None:
        marker = tmp_path / "exited"
        script = _fake_backend(
            _ACCEPTING_BACKEND + f"open({str(marker)!r}, 'w').close()\n",
            backend_version="0.0.1",
        )
        with anyio.fail_after(10):
            with pytest.raises(RpcHandshakeError, match="'0.0.1' does not match") as error:
                await JsonlSubprocessRpcTransport.start([sys.executable, "-c", script])
        assert error.value.code == "backend_version_mismatch"
        assert __version__ in str(error.value)
        # The rejected backend is shut down, not left running: its stdin closes.
        assert marker.exists()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_can_opt_out_of_the_backend_release_check() -> None:
    async def run() -> None:
        script = _fake_backend(_ACCEPTING_BACKEND, backend_version="0.0.1")
        with anyio.fail_after(10):
            transport = await JsonlSubprocessRpcTransport.start(
                [sys.executable, "-c", script],
                expected_backend_version=None,
            )
            await transport.close()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_surfaces_handshake_rejection(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.rejected",
    "code": "unsupported_capability",
    "message": "A required frontend capability is unavailable.",
    "backend_package_version": BACKEND_VERSION,
}), flush=True)
"""
        with pytest.raises(RpcHandshakeError, match="capability is unavailable") as error:
            await JsonlSubprocessRpcTransport.start(
                [sys.executable, "-c", _fake_backend(script)],
                cwd=tmp_path,
            )
        assert error.value.code == "unsupported_capability"

    anyio.run(run)


@pytest.mark.process
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal behavior")
def test_jsonl_subprocess_rpc_transport_kills_sigterm_resistant_child(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run() -> None:
        script = """
import json
import signal
import sys
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": BACKEND_VERSION,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
sys.stdin.read()
time.sleep(60)
"""
        monkeypatch.setattr(rpc_client_module, "_SUBPROCESS_CLOSE_TIMEOUT_SECONDS", 0.05)
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", _fake_backend(script)],
            cwd=tmp_path,
        )
        with anyio.fail_after(1):
            await transport.close()
        assert transport._process.returncode is not None
        await transport.close()

    anyio.run(run)


@pytest.mark.process
def test_jsonl_subprocess_rpc_transport_does_not_block_on_stderr(tmp_path: Path) -> None:
    async def run() -> None:
        script = """
import json
import sys
sys.stderr.write("x" * 200000)
sys.stderr.flush()
json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": BACKEND_VERSION,
    "capabilities": [],
    "limits": {"max_client_frame_bytes": 67108864, "max_server_frame_bytes": 67108864},
}), flush=True)
command = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.command.finished",
    "command_id": command["id"],
    "command_type": command["type"],
    "ok": True,
}), flush=True)
"""
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-c", _fake_backend(script)],
            cwd=tmp_path,
        )
        controller = RpcController(transport, command_id_factory=lambda _prefix: "shutdown-1")

        await controller.shutdown()
        with anyio.fail_after(5):
            events = [event async for event in controller.events()]
        await controller.close()

        assert [event.type for event in events] == ["rpc.command.finished"]
        assert isinstance(events[0], RpcCommandFinished)
        assert events[0].command_id == "shutdown-1"
        assert events[0].ok is True

    anyio.run(run)


def test_search_toggle_serializes_false() -> None:
    async def run() -> None:
        transport = RecordingTransport()
        controller = RpcController(transport, command_id_factory=lambda prefix: f"{prefix}-id")
        await controller.configure(web_search=False)
        assert transport.commands == [ConfigureCommand(id="configure-id", web_search=False)]
        assert json.loads(transport.commands[0].to_json_line())["web_search"] is False

    anyio.run(run)
