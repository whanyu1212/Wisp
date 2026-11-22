"""Tests for `run_shell` RPC command validation, scheduling, policy boundaries, and execution."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import build_rpc_executor_fixture
from wisp.coding import CodingSession
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    RpcShellCompleted,
    RpcShellProgress,
)
from wisp.rpc.commands import RpcCommandAdapter, RunShellCommand
from wisp.rpc.coordinator import _RpcControlEvent
from wisp.tools.context import ToolContext


def test_run_shell_command_validation_rejects_blank() -> None:
    parsed = RpcCommandAdapter.validate_json(
        json.dumps({"type": "run_shell", "id": "s1", "command": "git status"})
    )
    assert isinstance(parsed, RunShellCommand)
    assert parsed.command == "git status"

    with pytest.raises(ValidationError):
        RpcCommandAdapter.validate_json(json.dumps({"type": "run_shell", "command": "   \t  "}))

    with pytest.raises(ValidationError):
        RpcCommandAdapter.validate_json(json.dumps({"type": "run_shell", "command": ""}))


@pytest.mark.anyio
async def test_run_shell_executes_streams_progress_and_skips_session_history(
    tmp_path: Path,
) -> None:
    fixture = await build_rpc_executor_fixture(tmp_path)
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tool_context=ToolContext(cwd=tmp_path),
        trusted=True,
    )
    script = (
        f"{sys.executable} -c "
        '\'import sys, time; print("first", flush=True); time.sleep(0.35); '
        'print("second", flush=True)\''
    )

    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](8)
    async with anyio.create_task_group() as task_group:
        executor = fixture.executor(task_group=task_group, send=send)
        result = await dispatch_parsed(
            executor,
            {"type": "run_shell", "id": "shell-1", "command": script},
        )
        assert result.running_command is not None
        completed = await receive.receive()
        assert completed.command_id == "shell-1"
        assert completed.command_type == "run_shell"
        assert completed.ok is True
        assert completed.history is None

    await fixture.runtime.aclose()

    started = [e for e in fixture.events if isinstance(e, RpcCommandStarted)]
    progress = [e for e in fixture.events if isinstance(e, RpcShellProgress)]
    done = [e for e in fixture.events if isinstance(e, RpcShellCompleted)]
    finished = [e for e in fixture.events if isinstance(e, RpcCommandFinished)]

    assert len(started) == 1
    assert len(progress) >= 1
    assert "first" in progress[0].stdout_tail
    assert len(done) == 1
    assert done[0].exit_code == 0
    assert "first" in done[0].stdout
    assert "second" in done[0].stdout
    assert len(finished) == 1
    assert finished[0].ok is True
    # Local `!` shell commands do not create or append to session JSONL files.
    assert fixture.sessions.summaries() == ()


@pytest.mark.anyio
async def test_run_shell_reports_nonzero_exit_and_timeout(tmp_path: Path) -> None:
    fixture = await build_rpc_executor_fixture(tmp_path)
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tool_context=ToolContext(cwd=tmp_path),
        trusted=True,
    )

    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](8)
    async with anyio.create_task_group() as task_group:
        executor = fixture.executor(task_group=task_group, send=send)
        await dispatch_parsed(
            executor,
            {
                "type": "run_shell",
                "id": "shell-fail",
                "command": (
                    f"{sys.executable} -c "
                    "'import sys; print(\"oops\", file=sys.stderr); sys.exit(7)'"
                ),
            },
        )
        completed_fail = await receive.receive()
        assert completed_fail.ok is True

        await dispatch_parsed(
            executor,
            {
                "type": "run_shell",
                "id": "shell-timeout",
                "command": f"{sys.executable} -c 'import time; time.sleep(5)'",
                "timeout": 1,
            },
        )
        completed_timeout = await receive.receive()
        assert completed_timeout.ok is True

    await fixture.runtime.aclose()

    shell_events = [e for e in fixture.events if isinstance(e, RpcShellCompleted)]
    assert len(shell_events) == 2

    nonzero, timed_out = shell_events
    assert nonzero.command_id == "shell-fail"
    assert nonzero.exit_code == 7
    assert "oops" in nonzero.stderr
    assert nonzero.timed_out is False

    assert timed_out.command_id == "shell-timeout"
    assert timed_out.exit_code is None
    assert timed_out.timed_out is True
    assert timed_out.error is not None


@pytest.mark.anyio
async def test_run_shell_cancel_terminates_process(tmp_path: Path) -> None:
    fixture = await build_rpc_executor_fixture(tmp_path)
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tool_context=ToolContext(cwd=tmp_path),
        trusted=True,
    )
    pid_file = tmp_path / "child.pid"
    cmd = (
        f"{sys.executable} -c "
        f"'import os, pathlib, time; "
        f"pathlib.Path({json.dumps(str(pid_file))}).write_text(str(os.getpid())); "
        f"time.sleep(30)'"
    )

    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](8)
    async with anyio.create_task_group() as task_group:
        executor = fixture.executor(task_group=task_group, send=send)
        dispatch_result = await dispatch_parsed(
            executor,
            {"type": "run_shell", "id": "shell-cancel", "command": cmd},
        )
        assert dispatch_result.running_command is not None

        for _ in range(100):
            if pid_file.exists():
                break
            await anyio.sleep(0.05)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        dispatch_result.running_command.cancel_scope.cancel()
        completed = await receive.receive()
        assert completed.ok is False

    await fixture.runtime.aclose()

    shell_events = [e for e in fixture.events if isinstance(e, RpcShellCompleted)]
    assert len(shell_events) == 1
    assert shell_events[0].cancelled is True
    finished = [e for e in fixture.events if isinstance(e, RpcCommandFinished)]
    assert finished[-1].ok is False
    assert finished[-1].error is not None and "cancelled" in finished[-1].error.lower()

    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.anyio
async def test_run_shell_rejects_untrusted_and_protected_paths(tmp_path: Path) -> None:
    fixture = await build_rpc_executor_fixture(tmp_path)
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tool_context=ToolContext(
            cwd=tmp_path, protected_paths=(".env", ".wisp/browser-profile", "*.secret")
        ),
        trusted=False,
    )

    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](8)
    async with anyio.create_task_group() as task_group:
        executor = fixture.executor(task_group=task_group, send=send)
        await dispatch_parsed(
            executor,
            {"type": "run_shell", "id": "shell-untrusted", "command": "echo hi"},
        )
        untrusted_done = await receive.receive()
        assert untrusted_done.ok is False

        fixture.agent.trusted = True
        for idx, blocked_cmd in enumerate(
            (
                "cat .env",
                "ls ~/.wisp/browser-profile",
                "cat notes.secret",
                "ENV_FILE=.env printenv",
                "cat <.env",
                "cat .env;echo ok",
                "cat .env|head",
                'cat ".env";ls',
                '"cat .env;echo',
            ),
            start=1,
        ):
            await dispatch_parsed(
                executor,
                {"type": "run_shell", "id": f"shell-prot-{idx}", "command": blocked_cmd},
            )
            prot_done = await receive.receive()
            assert prot_done.ok is False

    await fixture.runtime.aclose()

    assert not [e for e in fixture.events if isinstance(e, RpcShellCompleted)]
    finished = [e for e in fixture.events if isinstance(e, RpcCommandFinished)]
    assert len(finished) == 10
    assert all(not item.ok for item in finished)
    assert "trusted" in (finished[0].error or "").lower()
    assert all("protected" in (item.error or "").lower() for item in finished[1:])


@pytest.mark.anyio
async def test_run_shell_withholds_provider_credentials_and_sets_scratch_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-do-not-leak")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    fixture = await build_rpc_executor_fixture(tmp_path)
    fixture.agent = CodingSession(
        provider=fixture.runtime.providers.get("fake"),
        sessions=fixture.sessions,
        tool_context=replace(
            ToolContext(cwd=tmp_path, scratch_root=scratch),
            withheld_environment=frozenset({"OPENAI_API_KEY"}),
        ),
        trusted=True,
    )

    send, receive = anyio.create_memory_object_stream[_RpcControlEvent](8)
    async with anyio.create_task_group() as task_group:
        executor = fixture.executor(task_group=task_group, send=send)
        await dispatch_parsed(
            executor,
            {
                "type": "run_shell",
                "id": "shell-env",
                "command": (
                    f"{sys.executable} -c "
                    '\'import os; print(os.environ.get("OPENAI_API_KEY", "<withheld>"), '
                    'os.environ.get("WISP_SCRATCH_DIR", ""))\''
                ),
            },
        )
        completed = await receive.receive()
        assert completed.ok is True

    await fixture.runtime.aclose()

    done = [e for e in fixture.events if isinstance(e, RpcShellCompleted)]
    assert len(done) == 1
    assert "sk-test-secret-do-not-leak" not in done[0].stdout
    assert "<withheld>" in done[0].stdout
    assert str(scratch) in done[0].stdout
