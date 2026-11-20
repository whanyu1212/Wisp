"""Real macOS subprocess boundaries, independent of model approval."""

from __future__ import annotations

import asyncio
import os
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError
from wisp.tools.shell.external_reads import external_command_read, resolve_auto_command_read_dir
from wisp.tools.shell.process import _run_shell
from wisp.tools.shell.seatbelt import (
    SeatbeltLaunch,
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
        "uv run --no-sync",
        "uv run --no-sync cargo test",
        "uv run --no-sync npm test",
        "",
        "'unterminated",
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


def execute(command: str, context: ToolContext, *, timeout: float = 10) -> tuple[int, str, str]:
    launch = prepare_seatbelt({"command": command}, context)

    async def run() -> tuple[int, str, str]:
        with activate_seatbelt(launch):
            result = await _run_shell(
                command, cwd=context.cwd, timeout=timeout, max_output_bytes=4000
            )
        return result.exit_code, result.stdout, result.stderr

    return asyncio.run(run())


@pytest.mark.parametrize("name", ["fixture.txt", "name with spaces.txt"])
def test_external_read_matches_file(context: ToolContext, tmp_path: Path, name: str) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    target = inputs / name
    target.write_text("granted input")
    root = resolve_auto_command_read_dir(inputs, context)

    read = external_command_read(shlex.join(("/bin/cat", str(target))), root, context)

    assert read is not None and read.target == target.resolve()
    assert external_command_read(f"/bin/ls {root}", root, context) is not None


def test_external_read_accepts_system_alias(macos: None, context: ToolContext) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        inputs = Path(folder)
        target = inputs / "fixture.txt"
        target.write_text("granted input")
        root = resolve_auto_command_read_dir(inputs, context)

        assert root == inputs.resolve()
        for operand in (target, target.resolve()):
            command = shlex.join(("/bin/cat", str(operand)))
            read = external_command_read(command, root, context)
            assert read is not None and read.target == target.resolve()


def test_external_read_rejects_other_inputs(context: ToolContext, tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    elsewhere = tmp_path / "other.txt"
    elsewhere.write_text("not granted")
    private = inputs / ".env"
    private.write_text("protected")
    (inputs / "symlink").symlink_to(elsewhere)
    os.link(elsewhere, inputs / "hardlink")
    root = resolve_auto_command_read_dir(inputs, context)

    data_file = inputs / "fixture.txt"
    data_file.write_text("granted input")
    (inputs / "local-link").symlink_to(data_file)
    nested = inputs / "nested"
    nested.mkdir()
    refused = (
        elsewhere,
        private,
        inputs / "symlink",
        inputs / "local-link",
        inputs / "hardlink",
        nested / ".." / data_file.name,
        inputs,
    )
    for target in refused:
        assert external_command_read(f"/bin/cat {target}", root, context) is None
    assert external_command_read(f"/bin/ls -a {root}", root, context) is None
    assert external_command_read(f"/bin/ls {root} {root}", root, context) is None


@pytest.mark.parametrize("path", ["workspace", "workspace/..", ".wisp", "/"])
def test_external_grant_rejects_unsafe_roots(
    context: ToolContext, tmp_path: Path, path: str
) -> None:
    (tmp_path / ".wisp").mkdir()
    with pytest.raises(ToolError, match="External inputs|Auto command read grant"):
        resolve_auto_command_read_dir(tmp_path / path, context)


def test_external_grant_rejects_workspace_alias(context: ToolContext) -> None:
    alias = context.cwd.with_name(context.cwd.name.upper())
    if not alias.is_dir() or not alias.samefile(context.cwd):
        pytest.skip("The test directory is on a case-sensitive filesystem")
    with pytest.raises(ToolError, match="outside the workspace"):
        resolve_auto_command_read_dir(alias, context)


@pytest.mark.process
@pytest.mark.parametrize("program", ["/bin/ls", "/bin/cat"])
def test_external_grant_is_literal(
    macos: None, context: ToolContext, tmp_path: Path, program: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    target = inputs / "fixture.txt"
    target.write_text("granted input")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("sibling input")
    root = resolve_auto_command_read_dir(inputs, context)
    operand = root if program == "/bin/ls" else target
    command = shlex.join((program, str(operand)))
    read = external_command_read(command, root, context)
    assert read is not None
    launch = prepare_seatbelt({"command": command}, context, external_read=read)

    def unexpected_shell_discovery() -> str:
        pytest.fail("An external read must use the system shell")

    monkeypatch.setattr("wisp.tools.shell.process._command_shell", unexpected_shell_discovery)

    async def run() -> tuple[int, str]:
        with activate_seatbelt(launch):
            result = await _run_shell(command, cwd=context.cwd, timeout=10, max_output_bytes=2000)
        return result.exit_code, result.stdout

    def profile_result(*argv: str) -> int:
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", launch.profile, *argv],
            cwd=context.cwd,
            capture_output=True,
            timeout=5,
        ).returncode

    try:
        assert profile_result("/bin/cat", str(sibling)) != 0
        assert profile_result("/usr/bin/touch", str(target)) != 0
        if program == "/bin/ls":
            assert profile_result("/bin/cat", str(target)) != 0
        code, stdout = asyncio.run(run())
        assert code == 0
        assert ("fixture.txt" if program == "/bin/ls" else "granted input") in stdout
    finally:
        launch.cleanup()


@pytest.mark.process
def test_external_read_uses_system_utilities(macos: None, context: ToolContext) -> None:
    inputs = context.cwd.parent / "inputs"
    inputs.mkdir()
    venv_bin = context.cwd / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    workspace_sleep = venv_bin / "sleep"
    workspace_sleep.write_text("#!/bin/sh\nexit 0\n")
    workspace_sleep.chmod(0o700)

    root = resolve_auto_command_read_dir(inputs, context)
    command = f"/bin/ls {root}"
    read = external_command_read(command, root, context)
    assert read is not None
    launch = prepare_seatbelt({"command": command}, context, external_read=read)

    try:
        probe = subprocess.run(
            ["/bin/sh", "-c", "command -v sleep"],
            env=dict(launch.environment),
            cwd=context.cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        assert Path(probe.stdout.strip()).parent in {Path("/bin"), Path("/usr/bin")}
    finally:
        launch.cleanup()


def test_external_read_refuses_workspace_python(
    macos: None, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = context.cwd.parent / "inputs"
    inputs.mkdir()
    interpreter = context.cwd / "python"
    interpreter.touch()

    root = resolve_auto_command_read_dir(inputs, context)
    command = f"/bin/ls {root}"
    read = external_command_read(command, root, context)
    assert read is not None
    monkeypatch.setattr("wisp.tools.shell.seatbelt.sys.executable", str(interpreter))
    with pytest.raises(ToolError, match="require Python outside"):
        prepare_seatbelt({"command": command}, context, external_read=read)


@pytest.mark.process
@pytest.mark.parametrize("program", ["/bin/ls", "/bin/cat"])
def test_external_grant_rejects_postcheck_replacement(
    macos: None, context: ToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: str
) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    directory = inputs / "directory"
    directory.mkdir()
    target = directory if program == "/bin/ls" else inputs / "fixture.txt"
    if program == "/bin/cat":
        target.write_text("authorized file")
    root = resolve_auto_command_read_dir(inputs, context)
    command = shlex.join((program, str(target)))
    read = external_command_read(command, root, context)
    assert read is not None
    launch = prepare_seatbelt({"command": command}, context, external_read=read)
    validate = SeatbeltLaunch.validate

    def replace_after_validation(self: SeatbeltLaunch, command: str, cwd: Path) -> None:
        validate(self, command, cwd)
        target.rename(inputs / "original")
        if program == "/bin/ls":
            target.mkdir()
            (target / "replacement.txt").touch()
        else:
            target.write_text("replacement file")

    monkeypatch.setattr(SeatbeltLaunch, "validate", replace_after_validation)

    async def run() -> tuple[int, str, str]:
        with activate_seatbelt(launch):
            result = await _run_shell(command, cwd=context.cwd, timeout=10, max_output_bytes=2000)
        return result.exit_code, result.stdout, result.stderr

    try:
        code, stdout, stderr = asyncio.run(run())
        assert code != 0
        assert "replacement" not in stdout
        assert "the input changed" in stderr
    finally:
        launch.cleanup()


def test_external_read_detects_replacement(context: ToolContext, tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    target = inputs / "fixture.txt"
    target.write_text("original")
    root = resolve_auto_command_read_dir(inputs, context)
    read = external_command_read(f"/bin/cat {target}", root, context)
    assert read is not None
    target.rename(inputs / "previous.txt")
    target.write_text("replacement")
    with pytest.raises(ToolError, match="External input changed"):
        read.validate()


def test_unsupported_platform_fails_closed(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wisp.tools.shell.seatbelt.sys.platform", "linux")
    with pytest.raises(ToolError, match="require macOS"):
        prepare_seatbelt({"command": "pytest"}, context)


def test_launch_rejects_unsupported_platform_before_spawning(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    for pattern in ("private/*.txt", "[abc].txt", "private/**/secret"):
        with pytest.raises(ToolError, match="protected-path pattern"):
            prepare_seatbelt({"command": "pytest"}, replace(context, protected_paths=(pattern,)))


@pytest.mark.process
@pytest.mark.parametrize("prefix", ["", "sub/"])
@pytest.mark.parametrize("operation", ["cat", "write", "move"])
def test_relative_protections_match_nested_suffixes(
    macos: None, context: ToolContext, prefix: str, operation: str
) -> None:
    from dataclasses import replace

    directory = context.cwd / prefix / "private"
    directory.mkdir(parents=True)
    (directory / "secret.txt").write_text("canary")
    context = replace(context, protected_paths=("private/secret.txt",))
    commands = {
        "cat": f"cat {prefix}private/secret.txt",
        "write": f"printf changed > {prefix}private/secret.txt",
        "move": f"mv {prefix}private {prefix}public",
    }
    code, _, stderr = execute(commands[operation], context)
    assert code != 0, stderr
    assert (directory / "secret.txt").read_text() == "canary"


@pytest.mark.process
def test_mandatory_subtree_pattern_protects_scratch(macos: None, context: ToolContext) -> None:
    from dataclasses import replace

    assert context.scratch_root is not None
    permissions = context.scratch_root / "permissions"
    permissions.mkdir()
    (permissions / "record.json").write_text("synthetic permission record")
    context = replace(context, protected_paths=(f"{permissions}/**",))
    for command in (
        f"cat {shlex.quote(str(permissions / 'record.json'))}",
        f"printf changed > {shlex.quote(str(permissions / 'new.json'))}",
    ):
        code, _, stderr = execute(command, context)
        assert code != 0, stderr


@pytest.mark.process
def test_production_context_runs_fast_path_without_review(
    macos: None, context: ToolContext
) -> None:
    from dataclasses import replace

    from wisp.coding.tool_execution import ConfiguredToolExecutor
    from wisp.config.runtime import WispConfig
    from wisp.events import (
        ToolApprovalRequested,
        ToolApprovalResolved,
        ToolApprovalReviewed,
        ToolExecutionEnded,
    )
    from wisp.providers.events import ToolCall
    from wisp.runtime.registry import ToolRegistry
    from wisp.tools.approval import ToolApprovalPolicy
    from wisp.tools.policy import ToolPolicy

    production = ToolContext.from_config(WispConfig(), cwd=context.cwd)
    production = replace(production, scratch_root=context.scratch_root)
    (context.cwd / "test_local.py").write_text("def test_ok():\n    assert 2+2 == 4\n")
    tool = BashTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=production,
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.auto_commands(),
    )

    async def run() -> None:
        try:
            call = ToolCall(
                call_id="production",
                name="bash",
                arguments={"command": "python -m pytest -q test_local.py"},
            )
            events = [event async for event in executor.execute(call)]
            assert not any(
                isinstance(event, ToolApprovalRequested | ToolApprovalReviewed) for event in events
            )
            assert any(
                isinstance(event, ToolApprovalResolved)
                and event.rule_id == "sandbox_development_v1"
                for event in events
            )
            assert isinstance(events[-1], ToolExecutionEnded) and not events[-1].is_error
            assert events[-1].exit_code == 0, events[-1].output
        finally:
            await tool.aclose()

    asyncio.run(run())


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
@pytest.mark.parametrize("revocation", ["mode", "generation"])
def test_shared_launcher_rechecks_consent(
    macos: None, context: ToolContext, revocation: str
) -> None:
    from dataclasses import replace

    from wisp.tools.approval import ToolApprovalPolicy
    from wisp.tools.shell.seatbelt import ProcessAuthority

    policy = ToolApprovalPolicy.auto_commands()
    authority = ProcessAuthority(
        context.process_owner, policy, policy.generation, context.boundary()
    )
    command = "printf launched > launched.txt"
    launch = replace(prepare_seatbelt({"command": command}, context), authority=authority)
    if revocation == "mode":
        policy.mode = "ask"
    else:
        policy.generation += 1

    async def run() -> None:
        with activate_seatbelt(launch):
            with pytest.raises(ToolError, match="consent was revoked"):
                await _run_shell(command, cwd=context.cwd, timeout=3, max_output_bytes=4000)

    asyncio.run(run())
    assert not (context.cwd / "launched.txt").exists()


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
    code, stdout, stderr = execute(command, context, timeout=30)
    if code != 0:
        print(f"stdout:\n{stdout}\nstderr:\n{stderr}")
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


@pytest.mark.process
def test_non_ascii_paths_enforce_protections_and_allow_workspace(
    macos: None, tmp_path: Path
) -> None:
    from dataclasses import replace

    workspace = tmp_path / "café-workspace"
    scratch = tmp_path / "café-scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = workspace / "café-secret"
    target.mkdir()
    (target / "secret.txt").write_text("non-ascii-canary")
    (workspace / "link").symlink_to(target, target_is_directory=True)
    context = replace(
        ToolContext(cwd=workspace, scratch_root=scratch.resolve()),
        protected_paths=("link/secret.txt",),
    )

    code, stdout, stderr = execute("printf ok > allowed.txt && cat allowed.txt", context)
    assert code == 0 and stdout == "ok", stderr

    for attack in (
        "cat café-secret/secret.txt",
        "mv café-secret moved && cat moved/secret.txt",
        "cat link/secret.txt",
    ):
        code, stdout, stderr = execute(attack, context)
        assert code != 0, (attack, stdout, stderr)
        assert "non-ascii-canary" not in stdout
    assert (target / "secret.txt").read_text() == "non-ascii-canary"


@pytest.mark.process
@pytest.mark.parametrize("name", [".env", ".env.local", "credentials.json"])
def test_bare_protected_symlinks_bind_their_targets(
    macos: None, context: ToolContext, name: str
) -> None:
    from dataclasses import replace

    real_file = context.cwd / "real_secret_data"
    real_file.write_text("bare-symlink-canary")
    (context.cwd / name).symlink_to(real_file)
    # Bare built-in protections (.env*, _PRIVATE_NAMES) must still bind symlink targets
    # even when custom protected_paths is empty.
    scoped = replace(context, protected_paths=())
    for attack in (f"cat {name}", "cat real_secret_data", f"printf changed > {name}"):
        code, stdout, stderr = execute(attack, scoped)
        assert code != 0, (attack, stdout, stderr)
        assert "bare-symlink-canary" not in stdout
    assert real_file.read_text() == "bare-symlink-canary"


def test_quotes_and_control_characters_in_roots_and_patterns_fail_closed(
    macos: None, tmp_path: Path, context: ToolContext
) -> None:
    from dataclasses import replace

    for pattern in ('bad"name', "bad\x01name", "bad\x7fname"):
        with pytest.raises(ToolError, match="protected-path pattern"):
            prepare_seatbelt({"command": "pytest"}, replace(context, protected_paths=(pattern,)))

    quoted_ws = tmp_path / 'ws"quoted'
    quoted_ws.mkdir()
    with pytest.raises(ToolError, match="unsupported characters"):
        prepare_seatbelt(
            {"command": "pytest"},
            ToolContext(cwd=quoted_ws, scratch_root=context.scratch_root),
        )


def test_symlinked_cwd_and_outside_workspace_root_fail_closed(macos: None, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    scratch = tmp_path / "scratch"
    for directory in (workspace, outside, scratch):
        directory.mkdir()
    escape = workspace / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError, match="symbolic links"):
        prepare_seatbelt(
            {"command": "pytest"},
            ToolContext(cwd=escape, workspace_root=workspace, scratch_root=scratch.resolve()),
        )
    with pytest.raises(ToolError, match="stay inside the workspace root"):
        prepare_seatbelt(
            {"command": "pytest"},
            ToolContext(cwd=outside, workspace_root=workspace, scratch_root=scratch.resolve()),
        )


@pytest.mark.process
def test_cargo_net_offline_is_set_in_sandbox_environment(macos: None, context: ToolContext) -> None:
    launch = prepare_seatbelt({"command": "cargo test"}, context)
    assert dict(launch.environment)["CARGO_NET_OFFLINE"] == "true"
    code, stdout, stderr = execute('printf "%s" "$CARGO_NET_OFFLINE"', context)
    assert code == 0 and stdout == "true", stderr


@pytest.mark.process
def test_nested_protected_and_control_symlinks_bind_targets(
    macos: None, context: ToolContext
) -> None:
    from dataclasses import replace

    sub = context.cwd / "sub"
    sub.mkdir()
    secret = context.cwd / "real_secret"
    secret.write_text("nested-secret-canary")
    (sub / ".env").symlink_to(secret)
    real_ssh = context.cwd / "real_ssh"
    real_ssh.mkdir()
    (real_ssh / "id_ed25519").write_text("nested-ssh-canary")
    (sub / ".ssh").symlink_to(real_ssh, target_is_directory=True)
    real_instructions = context.cwd / "real_instructions.md"
    real_instructions.write_text("keep-me")
    (sub / "AGENTS.md").symlink_to(real_instructions)

    scoped = replace(context, protected_paths=())
    for attack in (
        "cat sub/.env",
        "cat real_secret",
        "cat sub/.ssh/id_ed25519",
        "cat real_ssh/id_ed25519",
    ):
        code, stdout, stderr = execute(attack, scoped)
        assert code != 0, (attack, stdout, stderr)
        assert "canary" not in stdout

    # Control-file symlink remains readable, but writes through either name are denied.
    code, stdout, stderr = execute("cat sub/AGENTS.md", scoped)
    assert code == 0 and stdout == "keep-me", stderr
    for attack in ("printf pwned > sub/AGENTS.md", "printf pwned > real_instructions.md"):
        code, _, stderr = execute(attack, scoped)
        assert code != 0, (attack, stderr)
    assert real_instructions.read_text() == "keep-me"


@pytest.mark.process
@pytest.mark.parametrize("link_name", ["CAFÉ-1", "cafe\u0301-1"])
def test_non_ascii_case_and_nfd_wildcard_symlinks_bind_targets(
    macos: None, context: ToolContext, link_name: str
) -> None:
    from dataclasses import replace

    target = context.cwd / "target_secret"
    target.write_text("wildcard-unicode-canary")
    (context.cwd / link_name).symlink_to(target)
    scoped = replace(context, protected_paths=("café-*",))
    for attack in (f"cat {shlex.quote(link_name)}", "cat target_secret"):
        code, stdout, stderr = execute(attack, scoped)
        assert code != 0, (attack, stdout, stderr)
        assert "wildcard-unicode-canary" not in stdout


def test_symlink_loops_fail_closed_with_tool_error(macos: None, context: ToolContext) -> None:
    (context.cwd / ".env").symlink_to(context.cwd / "loop_b")
    (context.cwd / "loop_b").symlink_to(context.cwd / ".env")
    with pytest.raises(ToolError, match="resolved safely"):
        prepare_seatbelt({"command": "pytest"}, context)


def test_venv_symlink_loop_fails_closed_with_tool_error(macos: None, context: ToolContext) -> None:
    (context.cwd / ".venv").symlink_to(context.cwd / "venv_loop")
    (context.cwd / "venv_loop").symlink_to(context.cwd / ".venv")
    with pytest.raises(ToolError, match="resolved safely"):
        prepare_seatbelt({"command": "pytest"}, context)


@pytest.mark.process
def test_read_only_test_fast_path_allows_synthetic_tmpdir_fixtures_and_protects_workspace(
    macos: None, context: ToolContext
) -> None:
    from dataclasses import replace

    # Real workspace secrets must stay unreadable and workspace itself read-only.
    (context.cwd / ".env").write_text("REAL_ENV_SECRET")
    (context.cwd / ".wisp").mkdir()
    (context.cwd / ".wisp/settings.json").write_text('{"secret": true}')
    (context.cwd / "credentials").mkdir()
    (context.cwd / "credentials/keys.py").write_text("KEY = 'real'")
    (context.cwd / "test_sandbox.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "def test_synthetic_fixtures_and_workspace_isolation(tmp_path: Path) -> None:\n"
        "    # Synthetic protected/control fixtures in TMPDIR succeed.\n"
        "    (tmp_path / '.git').mkdir()\n"
        "    (tmp_path / '.git/HEAD').write_text('ref: refs/heads/main\\n')\n"
        "    (tmp_path / '.wisp').mkdir()\n"
        "    (tmp_path / '.wisp/settings.json').write_text('{}')\n"
        "    (tmp_path / 'AGENTS.md').write_text('# fixture\\n')\n"
        "    (tmp_path / '.env').write_text('FAKE=1\\n')\n"
        "    (tmp_path / 'credentials').mkdir()\n"
        "    (tmp_path / 'credentials/keys.py').write_text('FAKE = 1\\n')\n"
        "    assert (tmp_path / '.env').read_text() == 'FAKE=1\\n'\n"
        "    # Workspace is read-only.\n"
        "    with pytest.raises(PermissionError):\n"
        "        Path('mutated.txt').write_text('nope')\n"
        "    # Real workspace secrets stay blocked, even through a tmp_path symlink.\n"
        "    secrets = (Path('.env'), Path('.wisp/settings.json'), Path('credentials/keys.py'))\n"
        "    for secret in secrets:\n"
        "        with pytest.raises(PermissionError):\n"
        "            secret.read_text()\n"
        "    escape = tmp_path / 'link_to_real_env'\n"
        "    escape.symlink_to(Path('.env').resolve())\n"
        "    with pytest.raises(PermissionError):\n"
        "        escape.read_text()\n"
    )
    command = "python -m pytest -rN -q test_sandbox.py"
    launch = prepare_seatbelt({"command": command}, context)
    assert launch.read_only_workspace is True
    assert launch.cleanup_dir is not None and launch.cleanup_dir.is_dir()

    async def run() -> tuple[int, str, str]:
        with activate_seatbelt(launch):
            result = await _run_shell(command, cwd=context.cwd, timeout=20, max_output_bytes=8000)
        return result.exit_code, result.stdout, result.stderr

    code, stdout, stderr = asyncio.run(run())
    assert code == 0, (stdout, stderr)
    assert not (context.cwd / "mutated.txt").exists()
    # Ephemeral per-command directory is removed once the process tree exits.
    assert not launch.cleanup_dir.exists()
    # Non-test writable commands keep writable workspace.
    writable = prepare_seatbelt({"command": "npm run build"}, replace(context))
    try:
        assert writable.read_only_workspace is False
    finally:
        writable.cleanup()


@pytest.mark.process
def test_secure_fs_o_search_traversal_works_inside_seatbelt(
    macos: None, context: ToolContext
) -> None:
    from wisp.tools.files.secure_fs import _TRAVERSAL_FLAGS

    # Verify secure_fs's exact traversal flags walk every ancestor from / down to
    # TMPDIR under Seatbelt's (allow file-read-metadata) without file-read-data on /private.
    (context.cwd / "test_secure_fs_in_sandbox.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"FLAGS = {_TRAVERSAL_FLAGS!r}\n"
        "\n"
        "def test_o_search_traversal(tmp_path: Path) -> None:\n"
        "    target = (tmp_path / 'sub').resolve()\n"
        "    target.mkdir()\n"
        "    fd = os.open(target.anchor, FLAGS)\n"
        "    try:\n"
        "        for part in target.relative_to(target.anchor).parts:\n"
        "            nxt = os.open(part, FLAGS, dir_fd=fd)\n"
        "            os.close(fd)\n"
        "            fd = nxt\n"
        "        leaf = os.open('a.txt', os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd)\n"
        "        os.write(leaf, b'ok')\n"
        "        os.close(leaf)\n"
        "        os.fsync(fd)\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "    assert (target / 'a.txt').read_text() == 'ok'\n"
    )
    code, stdout, stderr = execute(
        "python -m pytest -q test_secure_fs_in_sandbox.py", context, timeout=20
    )
    assert code == 0, (stdout, stderr)


@pytest.mark.process
def test_unix_sockets_allowed_only_inside_ephemeral_tmpdir(
    macos: None, context: ToolContext
) -> None:
    before = Path.cwd()
    with socket.socket(socket.AF_UNIX) as outside_listener:
        try:
            os.chdir(context.cwd)
            outside_listener.bind("outside.sock")
            outside_listener.listen(1)
        finally:
            os.chdir(before)
        script = (
            "import os, socket, errno\n"
            "tmp = os.environ['TMPDIR']\n"
            "ws_sock = os.path.abspath('outside.sock')\n"
            "# Binding & connecting inside TMPDIR succeeds.\n"
            "os.chdir(tmp)\n"
            "srv = socket.socket(socket.AF_UNIX)\n"
            "srv.bind('ok.sock')\n"
            "srv.listen(1)\n"
            "cli = socket.socket(socket.AF_UNIX)\n"
            "cli.connect('ok.sock')\n"
            "conn, _ = srv.accept()\n"
            "conn.close(); cli.close(); srv.close()\n"
            "# Symlink inside TMPDIR pointing to outside Unix socket is still denied.\n"
            "os.symlink(ws_sock, 'escape.sock')\n"
            "try:\n"
            "    socket.socket(socket.AF_UNIX).connect('escape.sock')\n"
            "except OSError as exc:\n"
            "    assert exc.errno == errno.EPERM, exc\n"
            "else:\n"
            "    raise AssertionError('symlinked outside unix socket allowed')\n"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        code, stdout, stderr = execute(command, context)
        assert code == 0, (stdout, stderr)


@pytest.mark.process
def test_sandboxed_buffered_python_timeout_preserves_partial_output_and_cleans_up(
    macos: None, context: ToolContext
) -> None:
    from dataclasses import replace

    from wisp.tools.shell.supervisor import ProcessSupervisor

    command = (
        f"{shlex.quote(sys.executable)} -c "
        "'import time; print(\"partial-before-timeout\"); time.sleep(10)'"
    )
    # Direct _run_shell path
    launch_direct = prepare_seatbelt({"command": command}, context)
    assert dict(launch_direct.environment)["PYTHONUNBUFFERED"] == "1"
    assert launch_direct.cleanup_dir is not None

    async def run_direct() -> str:
        with activate_seatbelt(launch_direct):
            with pytest.raises(ToolError) as caught:
                await _run_shell(command, cwd=context.cwd, timeout=1.0, max_output_bytes=4000)
            return str(caught.value)

    direct_msg = asyncio.run(run_direct())
    assert "partial-before-timeout" in direct_msg
    assert not launch_direct.cleanup_dir.exists()

    # Supervised BashTool path
    launch_sup = prepare_seatbelt({"command": command}, context)
    assert launch_sup.cleanup_dir is not None

    async def run_supervised() -> str:
        tool = BashTool(ProcessSupervisor())
        try:
            with pytest.raises(ToolError) as caught:
                await tool.run(
                    {"command": command, "timeout": 1},
                    replace(context, command_sandbox=launch_sup),
                )
            return str(caught.value)
        finally:
            await tool.aclose()

    sup_msg = asyncio.run(run_supervised())
    assert "partial-before-timeout" in sup_msg
    assert not launch_sup.cleanup_dir.exists()


@pytest.mark.process
def test_cargo_test_with_lockfile_uses_read_only_workspace_and_scratch_target(
    macos: None, context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd
    import shutil
    import subprocess

    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")
    (context.cwd / "Cargo.toml").write_text(
        '[package]\nname="ro-probe"\nversion="0.1.0"\nedition="2021"\n'
    )
    (context.cwd / "src").mkdir()
    (context.cwd / "src/lib.rs").write_text(
        "#[test]\n"
        "fn temp_git_and_wisp_allowed_while_workspace_read_only() {\n"
        '    let dir = std::env::temp_dir().join("ro-probe-fixture");\n'
        '    std::fs::create_dir_all(dir.join(".git")).unwrap();\n'
        '    std::fs::create_dir_all(dir.join(".wisp")).unwrap();\n'
        '    std::fs::write(dir.join(".wisp/tui.json"), b"{}").unwrap();\n'
        '    assert!(std::fs::write("mutated.txt", b"no").is_err());\n'
        "}\n"
    )
    subprocess.run(
        ["cargo", "generate-lockfile", "--offline"],
        cwd=context.cwd,
        env={
            **os.environ,
            "CARGO_HOME": str(home / ".cargo"),
            "RUSTUP_HOME": str(home / ".rustup"),
        },
        check=True,
        capture_output=True,
    )
    command = "cargo test"
    launch = prepare_seatbelt({"command": command}, context)
    assert launch.read_only_workspace is True
    assert "CARGO_TARGET_DIR" in dict(launch.environment)

    async def run() -> tuple[int, str, str]:
        with activate_seatbelt(launch):
            result = await _run_shell(command, cwd=context.cwd, timeout=30, max_output_bytes=8000)
        return result.exit_code, result.stdout, result.stderr

    code, stdout, stderr = asyncio.run(run())
    assert code == 0, (stdout, stderr)
    assert not (context.cwd / "target").exists()
    assert not (context.cwd / "mutated.txt").exists()


def test_revoked_or_unapproved_launch_cleans_up_ephemeral_dir(
    macos: None, context: ToolContext
) -> None:
    from wisp.coding.tool_execution import ConfiguredToolExecutor
    from wisp.events import ToolApprovalReviewed
    from wisp.providers.events import ToolCall
    from wisp.runtime.registry import ToolRegistry
    from wisp.tools.approval import ToolApprovalPolicy
    from wisp.tools.policy import ToolPolicy

    assert context.scratch_root is not None

    class AskReviewer:
        async def review(self, *, call_id, arguments, context, still_authorized):
            return ToolApprovalReviewed(call_id=call_id, name="bash", verdict="ask", reason="ask")

    tool = BashTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=context,
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.auto_commands(),
        command_reviewer=AskReviewer(),
    )

    async def run() -> None:
        try:
            _ = [
                event
                async for event in executor.execute(
                    ToolCall(
                        call_id="ask-cleanup", name="bash", arguments={"command": "ruff check ."}
                    )
                )
            ]
        finally:
            await tool.aclose()

    asyncio.run(run())
    runs_root = context.scratch_root / ".seatbelt-runs"
    assert list(runs_root.glob("run-*")) == []


@pytest.mark.process
def test_cargo_target_is_isolated_from_non_cargo_and_writable_runs(
    macos: None, context: ToolContext
) -> None:
    """Neither non-Cargo tests nor writable-workspace commands may touch .seatbelt-cargo-target."""
    assert context.scratch_root is not None
    # Non-Cargo read-only tests do not get CARGO_TARGET_DIR.
    py_launch = prepare_seatbelt({"command": "pytest -q"}, context)
    try:
        assert py_launch.read_only_workspace is True
        assert "CARGO_TARGET_DIR" not in dict(py_launch.environment)
    finally:
        py_launch.cleanup()

    # Writable-workspace commands cannot plant a symlink at .seatbelt-cargo-target
    # or move a payload staged there by a prior cargo test run into the workspace.
    code, _, stderr = execute('ln -s "$PWD" "$WISP_SCRATCH_DIR/.seatbelt-cargo-target"', context)
    assert code != 0, stderr
    assert not (context.scratch_root / ".seatbelt-cargo-target").exists()

    staged = context.scratch_root / ".seatbelt-cargo-target/payload"
    staged.mkdir(parents=True)
    (staged / ".git").write_text("gitdir: ../evil")
    (staged / "AGENTS.md").write_text("evil")
    code, _, stderr = execute(
        'mv "$WISP_SCRATCH_DIR/.seatbelt-cargo-target/payload" "$PWD/planted"', context
    )
    assert code != 0, stderr
    assert not (context.cwd / "planted").exists()


@pytest.mark.process
def test_chmod_and_chflags_cannot_defeat_cleanup_or_cross_run_isolation(
    macos: None, context: ToolContext
) -> None:
    """Tightened permissions still clean up, chflags is denied, and other runs stay hidden."""
    assert context.scratch_root is not None
    # chflags uchg is denied by (deny file-write-flags).
    code, _, stderr = execute(
        'touch "$TMPDIR/flagtest" && chflags uchg "$TMPDIR/flagtest"', context
    )
    assert code != 0, stderr

    # A read-only test run that chmods a TMPDIR subtree to 0000/0500 is still cleaned up on exit.
    (context.cwd / "test_chmod_cleanup.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def test_lock_tmpdir(tmp_path: Path) -> None:\n"
        "    locked = tmp_path / 'locked'\n"
        "    locked.mkdir()\n"
        "    (locked / '.git').write_text('gitdir: ../evil')\n"
        "    os.chmod(locked, 0o500)\n"
    )
    command = "python -m pytest -q test_chmod_cleanup.py"
    launch = prepare_seatbelt({"command": command}, context)
    assert launch.cleanup_dir is not None

    async def run_ro() -> int:
        with activate_seatbelt(launch):
            result = await _run_shell(command, cwd=context.cwd, timeout=20, max_output_bytes=4000)
        return result.exit_code

    assert asyncio.run(run_ro()) == 0
    assert not launch.cleanup_dir.exists()

    # Even if another run directory is live under .seatbelt-runs, a writable-workspace
    # command cannot read, chmod, or move anything out of it into the workspace.
    other_run = context.scratch_root / ".seatbelt-runs/run-other/tmp/payload"
    other_run.mkdir(parents=True)
    (other_run / ".git").write_text("gitdir: ../evil")
    code, _, stderr = execute(
        'mv "$WISP_SCRATCH_DIR/.seatbelt-runs/run-other/tmp/payload" "$PWD/planted"', context
    )
    assert code != 0, stderr
    assert not (context.cwd / "planted").exists()


def test_unexecuted_prepared_call_and_prespawn_error_clean_up_ephemeral_dir(
    macos: None, context: ToolContext
) -> None:
    from dataclasses import replace

    from wisp.agent.tool_contracts import PreparedToolExecution
    from wisp.coding.tool_execution import ConfiguredToolExecutor
    from wisp.providers.events import ToolCall
    from wisp.runtime.registry import ToolRegistry
    from wisp.tools.approval import ToolApprovalPolicy
    from wisp.tools.policy import ToolPolicy

    assert context.scratch_root is not None
    runs_root = context.scratch_root / ".seatbelt-runs"

    # 1. Prepared call that never runs (e.g., batch cancelled) cleans up on close().
    tool = BashTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=context,
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.auto_commands(),
    )

    async def run_prepare_only() -> None:
        try:
            events = [
                event
                async for event in executor.prepare(
                    ToolCall(call_id="unrun", name="bash", arguments={"command": "pytest -q"})
                )
            ]
            prepared = events[-1]
            assert isinstance(prepared, PreparedToolExecution)
            assert len(list(runs_root.glob("run-*"))) == 1
            prepared.close()
        finally:
            await tool.aclose()

    asyncio.run(run_prepare_only())
    assert list(runs_root.glob("run-*")) == []

    # 2. Pre-spawn validation error in BashTool.run() cleans up the unclaimed launch.
    launch = prepare_seatbelt({"command": "pytest -q"}, context)
    assert launch.cleanup_dir is not None and launch.cleanup_dir.exists()

    async def run_prespawn_error() -> None:
        bash = BashTool()
        try:
            with pytest.raises(ToolError):
                await bash.run(
                    {"command": "pytest -q", "timeout": 0},
                    replace(context, command_sandbox=launch),
                )
        finally:
            await bash.aclose()

    asyncio.run(run_prespawn_error())
    assert not launch.cleanup_dir.exists()


def test_nested_cargo_subcrate_detects_ancestor_lockfile(macos: None, context: ToolContext) -> None:
    from dataclasses import replace

    nested = context.cwd / "rust/subcrate"
    nested.mkdir(parents=True)
    (context.cwd / "rust/Cargo.lock").write_text("version = 4\n")
    scoped = replace(context, cwd=nested, workspace_root=context.cwd)
    launch = prepare_seatbelt({"command": "cargo test"}, scoped)
    try:
        assert launch.read_only_workspace is True
        assert "CARGO_TARGET_DIR" in dict(launch.environment)
    finally:
        launch.cleanup()
