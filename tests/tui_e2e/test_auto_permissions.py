from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.tui_e2e.support import (
    context_stats_ready,
    reap,
    run_settled,
    screen_rows,
    screen_shows,
    welcome_ready,
)
from wisp import __version__

_BACKEND_DRIVER = """
import sys
from pathlib import Path

import anyio

from wisp.cli.rpc import _negotiate_rpc_connection, _run_rpc_with_runtime, _shutdown_signal_receiver
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

project = Path(sys.argv[1])
session_dir = Path(sys.argv[2])
run_commands = sys.argv[3] == "1"

write_call = ToolCall(
    call_id="write-helper",
    name="write",
    arguments={
        "path": "helper.py",
        "content": "def answer() -> int:\\n    return 42\\n",
    },
)
turns = [
    [
        ProviderResponseStarted(model="scripted-auto"),
        ProviderToolCallCompleted(tool_call=write_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(write_call,),
            finish_reason="tool_calls",
            usage=ProviderUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ),
    ],
]
if run_commands:
    test_call = ToolCall(
        call_id="run-tests",
        name="bash",
        arguments={"command": "python -m pytest -rN -q test_app.py"},
    )
    turns.append(
        [
            ProviderResponseStarted(model="scripted-auto"),
            ProviderToolCallCompleted(tool_call=test_call),
            ProviderResponseCompleted(
                content="",
                tool_calls=(test_call,),
                finish_reason="tool_calls",
                usage=ProviderUsage(input_tokens=12, output_tokens=6, total_tokens=18),
            ),
        ]
    )
turns.append(
    [
        ProviderResponseStarted(model="scripted-auto"),
        ProviderResponseCompleted(
            content="AUTO_WORKFLOW_COMPLETE",
            usage=ProviderUsage(input_tokens=8, output_tokens=4, total_tokens=12),
        ),
    ]
)

config = WispConfig(
    provider="scripted",
    model="scripted-auto",
    session_dir=session_dir,
)


async def main() -> None:
    if not await _negotiate_rpc_connection():
        return
    runtime = await build_session_runtime(config, connect_mcp=False)
    runtime.providers.register(ScriptedProvider(turns))
    with _shutdown_signal_receiver() as shutdown_signals:
        try:
            await _run_rpc_with_runtime(
                config,
                runtime,
                all_tools=True,
                startup_trusted=True,
                project_context_root=project,
                shutdown_signals=shutdown_signals,
            )
        finally:
            await runtime.aclose()


anyio.run(main)
"""


@pytest.mark.process
def test_rust_tui_auto_permissions_workflow_on_standalone_project(tmp_path: Path) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    project = tmp_path / "sample-app"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "sample-app"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (project / ".env").write_text("SECRET_TOKEN=must-stay-blocked\n", encoding="utf-8")
    (project / "test_app.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "import helper\n"
        "\n"
        "def test_helper_and_sandbox(tmp_path: Path) -> None:\n"
        "    assert helper.answer() == 42\n"
        "    # Synthetic fixtures inside TMPDIR succeed on the read-only test fast path.\n"
        "    (tmp_path / '.git').mkdir()\n"
        "    (tmp_path / '.wisp').mkdir()\n"
        "    (tmp_path / '.env').write_text('FAKE=1\\n')\n"
        "    assert (tmp_path / '.env').read_text() == 'FAKE=1\\n'\n"
        "    # Workspace stays read-only and real secrets stay unreadable.\n"
        "    with pytest.raises(PermissionError):\n"
        "        Path('mutated.txt').write_text('blocked')\n"
        "    with pytest.raises(PermissionError):\n"
        "        Path('.env').read_text()\n",
        encoding="utf-8",
    )

    session_dir = tmp_path / "sessions"
    driver = tmp_path / "auto_backend.py"
    driver.write_text(_BACKEND_DRIVER, encoding="utf-8")
    run_commands = sys.platform == "darwin"

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(driver),
                str(project),
                str(session_dir),
                "1" if run_commands else "0",
            ],
            os.environ,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    painted = bytearray()
    status: int | None = None
    phase = "startup"
    prompt_offset = 0
    context_redrawn = False
    deadline = time.monotonic() + 30
    expected_mode_label = "Auto (commands)" if run_commands else "Auto (edits)"
    expected_choice_key = b"3" if run_commands else b"2"
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    painted.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if not context_redrawn and context_stats_ready(output):
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 121, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if phase == "startup" and context_redrawn and welcome_ready(output):
                os.write(terminal_fd, b"/permissions\r")
                phase = "panel"
                output.clear()
            elif (
                phase == "panel"
                and screen_shows(painted, "Project permissions")
                and screen_shows(painted, "2  Auto edits")
                and screen_shows(painted, "3  Auto commands")
            ):
                assert not screen_shows(painted, "(experimental)"), bytes(painted)
                os.write(terminal_fd, expected_choice_key)
                phase = "mode_applied"
                output.clear()
            elif phase == "mode_applied" and screen_shows(painted, expected_mode_label):
                os.write(terminal_fd, b"\x1b")
                phase = "panel_closing"
                output.clear()
            elif phase == "panel_closing" and not screen_shows(painted, "Project permissions"):
                prompt_offset = len(painted)
                os.write(terminal_fd, b"update helper and run tests\r")
                phase = "running"
                output.clear()
            elif phase == "running" and run_settled(
                painted, prompt_offset, b"AUTO_WORKFLOW_COMPLETE"
            ):
                assert not screen_shows(painted, "Allow once"), bytes(painted)
                os.write(terminal_fd, b"\x03\x03")
                phase = "quitting"
                output.clear()
            waited, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited == child_pid:
                status = waited_status
                break
        if status is None:
            raise AssertionError(
                f"TUI did not finish in phase={phase!r}\n" + "\n".join(screen_rows(painted))
            )
    finally:
        if status is None:
            reap(child_pid, terminal_fd)
        else:
            os.close(terminal_fd)

    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
        status,
        phase,
        bytes(painted)[-1000:],
    )
    assert initial_terminal is not None
    assert (project / "helper.py").read_text(encoding="utf-8") == (
        "def answer() -> int:\n    return 42\n"
    )
    assert not (project / "mutated.txt").exists()

    # Verify persisted permission scope and session audit records.
    permission_files = list((Path.home() / ".wisp" / "permissions").glob("*.json"))
    assert len(permission_files) == 1
    saved = json.loads(permission_files[0].read_text(encoding="utf-8"))
    if run_commands:
        assert saved["mode"] == "auto_commands"
        assert saved["auto_scope"] == "sandbox_commands_v1"
    else:
        assert saved["mode"] == "auto"
        assert saved["auto_scope"] == "workspace_edit_v1"

    session_files = list(session_dir.glob("*.jsonl"))
    assert len(session_files) == 1
    transcript = session_files[0].read_text(encoding="utf-8")
    assert '"rule_id":"workspace_edit_v1"' in transcript
    if run_commands:
        assert '"rule_id":"sandbox_development_v1"' in transcript
        assert '"exit_code":0' in transcript
        assert "1 passed" in transcript
        assert list(session_dir.rglob("run-*")) == []
