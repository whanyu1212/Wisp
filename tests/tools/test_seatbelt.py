"""Real macOS subprocess boundaries, independent of model approval."""

from __future__ import annotations

import asyncio
import os
import shlex
import socket
import sys
from pathlib import Path

import pytest

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError
from wisp.tools.shell.process import _run_shell
from wisp.tools.shell.seatbelt import (
    activate_seatbelt,
    prepare_seatbelt,
    sandbox_development_command,
)
from wisp.tools.shell.tool import BashTool


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests -q",
        "uv run --no-sync pytest tests",
        "python -m unittest",
        "cargo test --offline",
        "cargo check",
        "cargo build",
        "npm test",
        "npm run build",
    ],
)
def test_fast_path_forms(command: str) -> None:
    assert sandbox_development_command({"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "ruff check .",
        "uv run pytest",
        "npm run deploy",
        "pnpm test",
        "cargo fmt",
    ],
)
def test_other_forms_keep_model_review(command: str) -> None:
    assert not sandbox_development_command({"command": command})


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    return ToolContext(cwd=workspace, scratch_root=scratch.resolve())


@pytest.fixture
def macos() -> None:
    if sys.platform != "darwin":
        pytest.skip("Real Seatbelt enforcement requires macOS")


def execute(command: str, context: ToolContext) -> tuple[int, str, str]:
    launch = prepare_seatbelt({"command": command}, context)

    async def run() -> tuple[int, str, str]:
        with activate_seatbelt(launch):
            result = await _run_shell(command, cwd=context.cwd, timeout=10, max_output_bytes=4000)
        return result.exit_code, result.stdout, result.stderr

    return asyncio.run(run())


def test_unsupported_platform_fails_closed(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wisp.tools.shell.seatbelt.sys.platform", "linux")
    with pytest.raises(ToolError, match="require macOS"):
        prepare_seatbelt({"command": "pytest"}, context)


def test_launch_rejects_unsupported_platform_before_spawning(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wisp.tools.shell.seatbelt import SeatbeltLaunch

    launch = SeatbeltLaunch("echo must-not-run", context.cwd, "", (), ())

    async def run() -> None:
        with activate_seatbelt(launch):
            with pytest.raises(ToolError, match="Seatbelt is unavailable"):
                await _run_shell(launch.command, cwd=context.cwd, timeout=1, max_output_bytes=100)

    monkeypatch.setattr("wisp.tools.shell.process.sys.platform", "win32")
    asyncio.run(run())


def test_missing_scratch_fails_closed(macos: None, context: ToolContext) -> None:
    with pytest.raises(ToolError, match="scratch"):
        prepare_seatbelt({"command": "pytest"}, ToolContext(cwd=context.cwd))


@pytest.mark.process
@pytest.mark.parametrize(
    "attack",
    [
        "cat ../secret",
        "printf changed > ../secret",
        "cat escape",
        "cat .env",
        "printf changed > .env",
        "mv .env renamed",
        "ln .env alias",
        "cat .ENV",
        "mv ../workspace ../relocated",
        "printf changed > AGENTS.md",
        "mkdir .git",
        "mkdir -p sub/.git",
        "mkdir -p sub/.wisp",
    ],
)
def test_filesystem_denials(macos: None, context: ToolContext, attack: str) -> None:
    secret = context.cwd.parent / "secret"
    secret.write_text("synthetic secret")
    (context.cwd / "escape").symlink_to(secret)
    (context.cwd / ".env").write_text("synthetic environment")
    # Default macOS volumes are case-insensitive; separate file on case-sensitive ones.
    (context.cwd / ".ENV").write_text("synthetic environment")
    code, stdout, stderr = execute(attack, context)
    assert code != 0, (stdout, stderr)
    assert secret.read_text() == "synthetic secret"
    assert (context.cwd / ".env").read_text() == "synthetic environment"


@pytest.mark.process
def test_protected_ancestor_cannot_move(macos: None, context: ToolContext) -> None:
    from dataclasses import replace

    directory = context.cwd / "private"
    directory.mkdir()
    (directory / "secret.txt").write_text("canary")
    context = replace(context, protected_paths=("private/secret.txt",))
    code, _, stderr = execute("mv private public", context)
    assert code != 0, stderr
    assert (directory / "secret.txt").read_text() == "canary"


def test_unsupported_patterns_fail_closed(macos: None, context: ToolContext) -> None:
    from dataclasses import replace

    for pattern in ("private/*.txt", "[abc].txt", "**/secret"):
        with pytest.raises(ToolError, match="protected-path pattern"):
            prepare_seatbelt({"command": "pytest"}, replace(context, protected_paths=(pattern,)))


@pytest.mark.process
def test_children_and_environment(
    macos: None, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WISP_PROBE_SECRET", "must-not-be-inherited")
    secret = context.cwd.parent / "secret"
    secret.write_text("synthetic secret")
    command = (
        'test -z "$WISP_PROBE_SECRET" && printf ok > allowed && '
        "/bin/sh -c 'cat ../secret' > child-output 2> child-error & wait"
    )
    execute(command, context)
    assert (context.cwd / "allowed").read_text() == "ok"
    assert not (context.cwd / "child-output").read_text()
    assert "Operation not permitted" in (context.cwd / "child-error").read_text()


@pytest.mark.process
def test_network_denial(macos: None, context: ToolContext) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        script = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        code, _, stderr = execute(command, context)
        assert code != 0 and "Operation not permitted" in stderr


@pytest.mark.process
@pytest.mark.parametrize("outside", [False, True])
def test_preexisting_hardlink_is_a_documented_limit(
    macos: None, context: ToolContext, outside: bool
) -> None:
    protected = (context.cwd.parent if outside else context.cwd) / ".env"
    protected.write_text("disposable canary")
    os.link(protected, context.cwd / "alias")
    code, stdout, stderr = execute("cat alias", context)
    assert code == 0, stderr
    assert stdout == "disposable canary"
    code, _, stderr = execute("printf changed > alias", context)
    assert code == 0, stderr
    assert protected.read_text() == "changed"


@pytest.mark.process
def test_launch_is_bound_and_context_does_not_leak(macos: None, context: ToolContext) -> None:
    launch = prepare_seatbelt({"command": "echo original"}, context)

    async def run() -> None:
        with activate_seatbelt(launch):
            with pytest.raises(ToolError, match="no longer matches"):
                await _run_shell(
                    "echo substituted", cwd=context.cwd, timeout=3, max_output_bytes=4000
                )
        result = await _run_shell(
            "echo unrestricted", cwd=context.cwd, timeout=3, max_output_bytes=4000
        )
        assert result.exit_code == 0

    asyncio.run(run())


@pytest.mark.process
def test_timeout_reaps_sandboxed_child(macos: None, context: ToolContext) -> None:
    command = "sleep 20 & echo $! > child.pid; wait"
    launch = prepare_seatbelt({"command": command}, context)

    async def run() -> None:
        with activate_seatbelt(launch):
            with pytest.raises(ToolError, match="timed out"):
                await _run_shell(command, cwd=context.cwd, timeout=0.5, max_output_bytes=4000)

    asyncio.run(run())
    pid = int((context.cwd / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.process
def test_unix_socket_and_fcntl_denied(macos: None, context: ToolContext) -> None:
    # Relative socket names avoid sockaddr_un's small path limit.
    before = Path.cwd()
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            os.chdir(context.cwd)
            listener.bind("probe.sock")
            listener.listen()
        finally:
            os.chdir(before)
        script = "import socket; socket.socket(socket.AF_UNIX).connect('probe.sock')"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        code, _, stderr = execute(command, context)
        assert code != 0 and "Operation not permitted" in stderr
    (context.cwd / "canary").write_text("unchanged")
    script = (
        "import os,fcntl,errno; fd=os.open('canary',os.O_RDONLY)\n"
        "for operation in (80,110):\n"
        " try: fcntl.fcntl(fd,operation,0)\n"
        " except OSError as error: assert error.errno == errno.EPERM, error\n"
        " else: raise AssertionError('mutating fcntl allowed')\n"
    )
    code, _, stderr = execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}", context)
    assert code == 0, stderr
    assert (context.cwd / "canary").read_text() == "unchanged"


@pytest.mark.process
@pytest.mark.parametrize("runner", ["pytest", "cargo", "npm"])
def test_offline_development_workflows(
    macos: None, context: ToolContext, runner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd
    import shutil

    # The suite normally supplies a fake home. Discover the actual installed
    # toolchains here; the child still receives a scratch HOME and read-only grants.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    if shutil.which(runner) is None:
        pytest.skip(f"{runner} is not installed")
    if runner == "pytest":
        (context.cwd / "test_local.py").write_text("def test_ok():\n    assert 2 + 2 == 4\n")
        command = "pytest -q -p no:cacheprovider test_local.py"
    elif runner == "cargo":
        (context.cwd / "Cargo.toml").write_text(
            '[package]\nname="sandbox-probe"\nversion="0.1.0"\nedition="2021"\n'
        )
        (context.cwd / "src").mkdir()
        (context.cwd / "src/lib.rs").write_text("#[test]\nfn ok() { assert_eq!(2+2,4); }\n")
        command = "cargo test --offline"
    else:
        (context.cwd / "package.json").write_text(
            '{"name":"sandbox-probe","version":"1.0.0","scripts":{"test":"node --test test.cjs"}}'
        )
        (context.cwd / "test.cjs").write_text(
            "require('node:test')('ok',()=>require('node:assert').strictEqual(2+2,4));"
        )
        command = "npm test --offline"
    code, stdout, stderr = execute(command, context)
    assert code == 0, (stdout, stderr)


@pytest.mark.process
def test_background_retains_policy(macos: None, context: ToolContext) -> None:
    from dataclasses import replace

    command = "sleep 0.1; cat ../secret"
    (context.cwd.parent / "secret").write_text("synthetic secret")
    sandbox = prepare_seatbelt({"command": command}, context)
    tool = BashTool()

    async def run() -> None:
        try:
            start = await tool.run(
                {"operation": "start", "command": command, "yield_seconds": 0},
                replace(context, command_sandbox=sandbox),
            )
            assert start.data is not None
            result = await tool.run(
                {
                    "operation": "poll",
                    "process_id": start.data["process_id"],
                    "wait_seconds": 5,
                    "until": "exit",
                },
                context,
            )
            assert result.data is not None and result.data["exit_code"] != 0
            assert "synthetic secret" not in result.text
        finally:
            await tool.aclose()

    asyncio.run(run())
