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
        "uv run --no-sync",
        "uv run --no-sync cargo test",
        "uv run --no-sync npm test",
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
    code, stdout, stderr = execute(command, context)
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
