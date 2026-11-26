"""Private startup IPC and the RPC handshake through the two installed entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

import anyio
import pytest

from tests.support.cli import _RPC_TEST_HANDSHAKE, CliRunner
from wisp.cli import app, application
from wisp.cli.rpc_preview import read_startup_preview
from wisp.cli.startup_pipe import PREVIEW_FD_PREFIX, PreviewWorkspace, StartupPreviewPipe
from wisp.messages import Message
from wisp.sessions.entries import MessageSessionEntry, session_entry_to_json
from wisp.sessions.pagination import DEFAULT_SESSION_MESSAGE_PAGE_LIMIT
from wisp.sessions.startup_preview import prepare_startup_preview

pytestmark = pytest.mark.skipif(os.name != "posix", reason="native TUI requires POSIX")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "history.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for i in range(DEFAULT_SESSION_MESSAGE_PAGE_LIMIT + 1):
            entry = MessageSessionEntry(
                id=f"m{i}",
                session_id="session",
                parent_id=f"m{i - 1}" if i else None,
                message=Message(role="user", content=f"previous message {i}"),
            )
            stream.write(session_entry_to_json(entry) + "\n")
    return path


def test_only_inherited_reader(tmp_path: Path) -> None:
    workspace = PreviewWorkspace.current()
    assert workspace is not None
    reader, writer = os.pipe()
    with ExitStack() as stack:
        stack.callback(os.close, writer)
        file = stack.enter_context((tmp_path / "regular").open("wb"))
        for descriptor in (reader, 0, 1, 2):
            assert StartupPreviewPipe.claim(workspace.fd_argument(descriptor)) is None
        for source in (writer, file.fileno()):
            fd = os.dup(source)
            os.set_inheritable(fd, True)
            with os.fdopen(fd, "wb", buffering=0):
                assert StartupPreviewPipe.claim(workspace.fd_argument(fd)) is None
                assert os.get_inheritable(fd)
                assert os.get_blocking(fd)

        os.set_inheritable(reader, True)
        for suffix in ("", ":", ":bad"):
            assert StartupPreviewPipe.claim(f"{PREVIEW_FD_PREFIX}{reader}{suffix}") is None
            assert os.get_inheritable(reader)
            assert os.get_blocking(reader)
        pipe = StartupPreviewPipe.claim(workspace.fd_argument(reader))
        assert pipe is not None
        stream = stack.enter_context(pipe.stream)
        assert not os.get_inheritable(stream.fileno())
        assert not os.get_blocking(stream.fileno())


def test_rejects_duplicate_stdin() -> None:
    script = """
import os
from wisp.cli.startup_pipe import PreviewWorkspace, StartupPreviewPipe
fd = os.dup(0)
os.set_inheritable(fd, True)
assert StartupPreviewPipe.claim(PreviewWorkspace.current().fd_argument(fd)) is None
assert os.get_inheritable(fd)
assert os.get_blocking(fd)
assert os.read(0, 3) == b"rpc"
os.close(fd)
"""
    subprocess.run([sys.executable, "-c", script], input=b"rpc", check=True, timeout=10)


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires Linux O_PATH")
def test_preserves_path_only_descriptor(tmp_path: Path) -> None:
    workspace = PreviewWorkspace.current()
    assert workspace is not None
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_PATH)
    try:
        os.set_inheritable(fd, True)
        assert StartupPreviewPipe.claim(workspace.fd_argument(fd)) is None
        assert os.get_inheritable(fd)
        assert os.path.samestat(os.fstat(fd), fifo.stat())
    finally:
        os.close(fd)


def test_backend_root_changes(history: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = history.parent
    cwd = root / "nested"
    cwd.mkdir()
    (root / "pyproject.toml").touch()
    monkeypatch.chdir(cwd)
    workspace = PreviewWorkspace.current()
    assert workspace is not None and workspace.project_root == root

    def trust(project: Path) -> bool:
        assert project == root
        (cwd / "pyproject.toml").touch()
        return True

    monkeypatch.setattr(application._cli_trust, "trusted_noninteractive", trust)
    packet = prepare_startup_preview(history, cwd)
    assert packet is not None
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as stream:
        with os.fdopen(writer, "wb") as output:
            output.write(packet)
        result = CliRunner().invoke(
            app,
            ["--mode", "rpc", "--resume", str(history), "--session-dir", str(root)],
            obj=StartupPreviewPipe(stream, workspace.token()),
            env={"WISP_PROVIDER": "fake"},
            input='{"type":"shutdown","id":"bye"}\n',
        )
    assert result.exit_code == 0, result.exception
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in events] == [
        "rpc.handshake.accepted",
        "rpc.workspace",
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert events[-1]["command_id"] == "bye" and events[-1]["ok"]


@pytest.mark.parametrize(
    ("entrypoint", "trusted", "selection", "expected"),
    [
        ("console", True, "continue", True),
        ("module", True, "resume", True),
        ("module", False, "continue", False),
        ("console", True, "fresh", False),
        ("console", True, "different", False),
        ("module", True, "replaced-root", False),
        ("module", True, "replaced-cwd", False),
    ],
)
@pytest.mark.process
def test_preview_precedes_commands(
    history: Path,
    entrypoint: str,
    trusted: bool,
    selection: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = history.parent / "project"
    cwd = root / "nested"
    cwd.mkdir(parents=True)
    (root / "pyproject.toml").touch()
    monkeypatch.chdir(cwd)
    workspace = PreviewWorkspace.capture(root, cwd)
    assert workspace is not None
    if selection == "replaced-root":
        moved = root.rename(root.with_name("original"))
        root.mkdir()
        (moved / cwd.name).rename(cwd)
        (moved / "pyproject.toml").rename(root / "pyproject.toml")
    elif selection == "replaced-cwd":
        cwd.rename(root / "original")
        cwd.mkdir()
        os.chdir(cwd)
    # Even a valid packet produced after the replacement must be declined.
    packet = prepare_startup_preview(history, cwd)
    assert packet is not None
    args = [sys.executable, "-m", "wisp"]
    if entrypoint == "console":
        args = [str(Path(sys.executable).with_name("wisp"))]
    selected = history
    if selection == "different":
        selected = cwd / "another.jsonl"
        selected.write_bytes(history.read_bytes())
    options = {"continue": ["--continue"], "fresh": []}.get(selection, ["--resume", str(selected)])
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as stream:
        with os.fdopen(writer, "wb") as output:
            output.write(packet)
        result = subprocess.run(
            [
                *args,
                "--mode",
                "rpc",
                workspace.fd_argument(stream.fileno()),
                "--provider",
                "fake",
                "--session-dir",
                str(history.parent),
                *options,
            ],
            pass_fds=(stream.fileno(),),
            cwd=cwd,
            env={**os.environ, "WISP_TRUST": str(int(trusted)), "WISP_MODEL": ""},
            input=_RPC_TEST_HANDSHAKE + '{"type":"shutdown","id":"bye"}\n',
            capture_output=True,
            text=True,
            timeout=15,
        )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in records] == [
        "rpc.handshake.accepted",
        *(["rpc.startup.history_preview"] if expected else []),
        "rpc.workspace",
        "rpc.command.started",
        "rpc.command.finished",
    ]
    if expected:
        assert (
            records[1]["lines"][-1]
            == f"User: previous message {DEFAULT_SESSION_MESSAGE_PAGE_LIMIT}"
        )
    assert records[-1]["command_id"] == "bye" and records[-1]["ok"]


@pytest.mark.parametrize("failure", ["missing-eof", "overflow", "invalid", "empty"])
def test_skips_incomplete_packets(history: Path, failure: str) -> None:
    packet = prepare_startup_preview(history, history.parent)
    assert packet is not None
    workspace = PreviewWorkspace.current()
    assert workspace is not None

    async def collect(stream: BinaryIO, output: BinaryIO) -> None:
        async def send_overflow() -> None:
            left = 512 * 1024 + 1
            while left:
                await anyio.wait_writable(output.fileno())
                left -= os.write(output.fileno(), b" " * min(left, 4096))
            output.close()

        async with anyio.create_task_group() as group:
            if failure == "overflow":
                group.start_soon(send_overflow)
            else:
                output.write({"missing-eof": packet, "invalid": b"?", "empty": b""}[failure])
                if failure != "missing-eof":
                    output.close()
            with anyio.fail_after(2):
                preview = StartupPreviewPipe(stream, workspace.token())
                event = await read_startup_preview(preview, history, "session", history.parent)
            assert event is None
            group.cancel_scope.cancel()

    reader, writer = os.pipe()
    os.set_blocking(reader, False)
    with os.fdopen(reader, "rb") as stream, os.fdopen(writer, "wb", buffering=0) as output:
        anyio.run(collect, stream, output)


@pytest.mark.parametrize(
    ("entrypoint", "selection"), [("console", ["tui", "--continue"]), ("module", ["--mode", "tui"])]
)
@pytest.mark.process
def test_launcher_worker_round_trip(
    history: Path, entrypoint: str, selection: list[str], tmp_path: Path
) -> None:
    import pty

    # Stand in for the native frontend. Observe its exact opaque backend argv
    # and hand it the reader, a handshake, and an already-queued shutdown.
    frontend = tmp_path / "frontend"
    frontend.write_text(
        f"""#!{sys.executable}
import json
import os
import subprocess
import sys

args = sys.argv[1:]
fd = int(args[args.index("--backend-pass-fd") + 1])
backend = args[args.index("--") + 1:]
assert os.get_inheritable(fd)
assert any(arg.startswith({PREVIEW_FD_PREFIX!r} + str(fd) + ":") for arg in backend)
result = subprocess.run(
    backend, pass_fds=(fd,), input={_RPC_TEST_HANDSHAKE!r} + '{{"type":"shutdown","id":"bye"}}\\n',
    capture_output=True, text=True, timeout=15,
)
assert result.returncode == 0, result.stderr
events = [json.loads(line) for line in result.stdout.splitlines()]
assert events[1]["type"] == "rpc.startup.history_preview", events
assert events[1]["lines"][-1] == "User: previous message {DEFAULT_SESSION_MESSAGE_PAGE_LIMIT}"
assert events[-1]["command_id"] == "bye" and events[-1]["ok"]
""",
        encoding="utf-8",
    )
    frontend.chmod(0o700)
    args = [str(Path(sys.executable).with_name("wisp")), *selection]
    if entrypoint == "module":
        args = [sys.executable, "-m", "wisp", *selection, "--resume", history.name]
    # A pty enables interactive eligibility without requiring a controlling terminal.
    master, slave = pty.openpty()
    with os.fdopen(master, "rb"), os.fdopen(slave, "rb") as terminal:
        result = subprocess.run(
            [*args, "--session-dir", str(history.parent)],
            cwd=history.parent,
            env={
                **os.environ,
                "WISP_RUST_TUI_BINARY": str(frontend),
                "WISP_TRUST": "1",
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
            },
            stdin=terminal,
            stdout=terminal,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
    assert result.returncode == 0, result.stderr
