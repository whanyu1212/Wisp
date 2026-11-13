"""The RPC host announces background-process exits and hands them to the next prompt."""

from __future__ import annotations

import io
import json
import shlex
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from tests.support.cli import _read_rpc_test_handshake
from wisp.cli import rpc
from wisp.config.runtime import WispConfig
from wisp.messages import Message
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.commands import ParsedRpcCommand, RpcCommandAdapter
from wisp.rpc.configuration import _ConfigOverrides
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore


def _python(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _reply(content: str, *calls: ToolCall) -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="scripted"),
        *(ProviderToolCallCompleted(tool_call=call) for call in calls),
        ProviderResponseCompleted(
            content=content,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
        ),
    ]


def _bash(call_id: str, **arguments: object) -> ToolCall:
    return ToolCall(call_id=call_id, name="bash", arguments=dict(arguments))


def _serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ScriptedProvider,
    commands: list[dict[str, object]],
) -> list[dict[str, Any]]:
    """Run the RPC backend over ``commands`` and return every event it wrote.

    A command with an ``after`` key is sent only once the command it names has
    finished; the key itself is not sent. An entry with only an ``await`` key sends
    nothing and waits until an event has every field it gives.
    """

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("WISP_PROVIDER", "scripted")
    monkeypatch.setenv("WISP_MODEL", "")
    session_dir = tmp_path / "sessions"
    config = WispConfig.from_env(
        provider="scripted", session_dir=session_dir, project_dir=project, trusted=True
    )

    async def build_scripted_runtime(_config: WispConfig, *, connect_mcp: bool) -> WispRuntime:
        runtime = await build_runtime(connect_mcp=connect_mcp)
        runtime.providers.register(provider)
        return runtime

    output = io.StringIO()

    def seen(fields: dict[str, object]) -> bool:
        return any(
            all(record.get(key) == value for key, value in fields.items())
            for record in map(json.loads, output.getvalue().splitlines())
        )

    def finished(command_id: object) -> bool:
        return seen({"type": "rpc.command.finished", "command_id": command_id})

    async def send_commands(send: Any, _stop_reader: Any, _write_event: Any) -> None:
        async with send:
            for entry in commands:
                if "await" in entry:
                    with anyio.fail_after(30):
                        while not seen(cast(dict[str, object], entry["await"])):
                            await anyio.sleep(0.01)
                    continue
                payload = {key: value for key, value in entry.items() if key != "after"}
                if "after" in entry:
                    with anyio.fail_after(30):
                        while not finished(entry["after"]):
                            await anyio.sleep(0.01)
                command = RpcCommandAdapter.validate_python(payload)
                await send.send(
                    rpc._RpcInputCommand(ParsedRpcCommand.from_known(command, payload=payload))
                )
            await send.send(rpc._RpcInputClosed())

    monkeypatch.setattr(rpc, "build_session_runtime", build_scripted_runtime)
    monkeypatch.setattr(rpc, "_read_rpc_stdin", send_commands)
    monkeypatch.setattr(rpc._rpc_transport, "read_rpc_stdin_handshake", _read_rpc_test_handshake)

    async def scenario() -> None:
        with redirect_stdout(output):
            await rpc._run_rpc(
                config,
                all_tools=True,
                continue_latest=False,
                startup_trusted=True,
                config_overrides=_ConfigOverrides(provider="scripted", session_dir=session_dir),
                project_context_root=project,
            )

    anyio.run(scenario)
    return [json.loads(line) for line in output.getvalue().splitlines() if line]


@pytest.mark.process
@pytest.mark.parametrize(
    ("notify", "switch_session", "told"),
    [(True, False, True), (False, False, False), (True, True, False)],
    ids=["notify", "quiet", "switched-session"],
)
def test_exit_reaches_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notify: bool, switch_session: bool, told: bool
) -> None:
    """A process that ends unpolled is published, and told to the model if it asked.

    The first run starts a short process, then runs a longer foreground command,
    so the started one ends while the run is still going and nothing polls it.
    Switching to another session in between drops the notice: the process
    belonged to the session that was left.
    """

    # Without waking, the notice waits for the user's next prompt.
    monkeypatch.setenv("WISP_WAKE_ON_PROCESS_EXIT", "0")
    provider = ScriptedProvider(
        [
            _reply(
                "",
                _bash(
                    "start",
                    operation="start",
                    command=_python("import sys; sys.exit(4)"),
                    notify_on_exit=notify,
                    yield_seconds=0,
                ),
            ),
            _reply("", _bash("wait", command=_python("import time; time.sleep(1.5)"))),
            _reply("started"),
            _reply("noted"),
        ]
    )

    other = JsonlSessionStore(tmp_path / "sessions").create()
    anyio.run(other.append_message, Message(role="user", content="earlier work"))

    records = _serve(
        tmp_path,
        monkeypatch,
        provider,
        [
            {"id": "yolo", "type": "set_permissions", "mode": "yolo"},
            {"id": "p1", "type": "prompt", "prompt": "run CI"},
            *(
                [{"id": "switch", "type": "select_session", "session_id": other.session_id}]
                if switch_session
                else []
            ),
            {"id": "p2", "type": "prompt", "prompt": "anything new?"},
        ],
    )

    ended = [record for record in records if record["type"] == "process.ended"]
    assert [(e["state"], e["exit_code"], e["notify"]) for e in ended] == [("completed", 4, notify)]
    next_prompt = [m.content for m in provider.calls[-1].messages if m.role == "user"][-1]
    if told:
        assert next_prompt.startswith("[Wisp: background processes")
        assert f"process_id={ended[0]['process_id']}: exit code 4:" in next_prompt
        assert next_prompt.endswith("\n\nanything new?")
    else:
        assert next_prompt == "anything new?"


@pytest.mark.process
@pytest.mark.parametrize("switch_session", [False, True], ids=["same-session", "switched"])
def test_state_lists_background_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch_session: bool
) -> None:
    """``get_state`` lists the session's running processes; a switch stops them."""

    sleeper = _python("import time; time.sleep(30)")
    provider = ScriptedProvider(
        [
            _reply(
                "",
                _bash(
                    "start",
                    operation="start",
                    command=sleeper,
                    notify_on_exit=True,
                    yield_seconds=0,
                ),
            ),
            _reply("started"),
        ]
    )
    other = JsonlSessionStore(tmp_path / "sessions").create()
    anyio.run(other.append_message, Message(role="user", content="earlier work"))

    records = _serve(
        tmp_path,
        monkeypatch,
        provider,
        [
            {"id": "yolo", "type": "set_permissions", "mode": "yolo"},
            {"id": "p1", "type": "prompt", "prompt": "start the server"},
            *(
                [{"id": "switch", "type": "select_session", "session_id": other.session_id}]
                if switch_session
                else []
            ),
            {"id": "state", "type": "get_state", "after": "switch" if switch_session else "p1"},
        ],
    )

    (state,) = [r["state"] for r in records if r["type"] == "rpc.state"]
    listed = [(p["command"], p["notify"]) for p in state["background_processes"]]
    assert listed == ([] if switch_session else [(sleeper, True)])
    if not switch_session:
        assert state["background_processes"][0]["started_at_ms"] > 0


@pytest.mark.process
@pytest.mark.parametrize("exit_during_run", [False, True], ids=["idle", "mid-run"])
def test_exit_wakes_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_during_run: bool
) -> None:
    """An exit nobody polled starts one run that carries only its notice.

    The process ends either after the run that started it, or during a longer
    foreground command in that run; either way the wake follows the run.
    """

    lifetime = "time.sleep(0.3); " if not exit_during_run else ""
    script = [
        _reply(
            "",
            _bash(
                "start",
                operation="start",
                command=_python(f"import sys, time; {lifetime}sys.exit(4)"),
                notify_on_exit=True,
                yield_seconds=0,
            ),
        ),
        *(
            [_reply("", _bash("wait", command=_python("import time; time.sleep(1.5)")))]
            if exit_during_run
            else []
        ),
        _reply("started"),
        _reply("CI is red"),
    ]
    provider = ScriptedProvider(script)

    records = _serve(
        tmp_path,
        monkeypatch,
        provider,
        [
            {"id": "yolo", "type": "set_permissions", "mode": "yolo"},
            {"id": "p1", "type": "prompt", "prompt": "run CI"},
            {"await": {"type": "rpc.command.finished", "command_type": "process_wake"}},
            {"id": "history", "type": "get_messages"},
        ],
    )

    runs = [
        r["command_type"]
        for r in records
        if r["type"] == "rpc.command.started" and r["command_type"] in {"prompt", "process_wake"}
    ]
    assert runs == ["prompt", "process_wake"]
    woken = provider.calls[-1].messages[-1]
    assert woken.role == "user"
    assert woken.content.startswith("[Wisp: background processes")
    assert ": exit code 4: " in woken.content
    assert len(provider.calls) == len(script)
    (page,) = [r for r in records if r.get("command_id") == "history" and "messages" in r]
    prompts = [(m["content"], m["process_wake"]) for m in page["messages"] if m["role"] == "user"]
    assert prompts == [("run CI", False), ("", True)]
