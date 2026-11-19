"""Real toolchain compatibility without broadening the sandbox's secret boundary."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from wisp.config.runtime import WispConfig
from wisp.tools.context import ToolContext
from wisp.tools.shell.seatbelt import prepare_seatbelt
from wisp.tools.shell.tool import BashTool

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt")]


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    workspace, scratch = tmp_path / "workspace", tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    return replace(ToolContext.from_config(WispConfig(), cwd=workspace), scratch_root=scratch)


def execute(command: str, context: ToolContext) -> tuple[int | None, str]:
    async def run() -> tuple[int | None, str]:
        tool = BashTool()
        arguments = {"command": command, "timeout": 30}
        try:
            launch = prepare_seatbelt(arguments, context)
            result = await tool.run(arguments, replace(context, command_sandbox=launch))
            code = result.data.get("exit_code")
            assert code is None or isinstance(code, int)
            return code, result.text
        finally:
            await tool.aclose()

    return asyncio.run(run())


def test_cold_uv_cache_and_installed_provider_import(context: ToolContext) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    (context.cwd / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (context.cwd / "pyproject.toml").write_text(
        '[project]\nname="sandbox-fixture"\nversion="0.0.0"\nrequires-python=">=3.11"\n'
    )
    (context.cwd / "test_import.py").write_text(
        "import anthropic.lib.credentials\n\ndef test_import():\n"
        "    assert anthropic.lib.credentials.__name__ == 'anthropic.lib.credentials'\n"
    )
    assert context.scratch_root is not None
    assert not (context.scratch_root / "uv-cache").exists()
    code, output = execute("uv run --no-sync pytest -q test_import.py", context)
    assert code == 0, output
    assert "1 passed" in output


@pytest.mark.parametrize(
    ("marker", "allowed"),
    [
        ("$UV_CACHE_DIR/sdists-v9/.git", True),
        ("$UV_CACHE_DIR/sdists-v11/.git", True),
        ("$UV_CACHE_DIR/sdists-v/.git", False),
        ("$UV_CACHE_DIR/sdists-v11x/.git", False),
        ("$UV_CACHE_DIR/sdists-v11/nested/.git", False),
        ("$UV_CACHE_DIR/wheels-v11/.git", False),
        ("$UV_CACHE_DIR/../other/uv-cache/sdists-v11/.git", False),
        ("$UV_CACHE_DIR/sdists-v11/.git/config", False),
        ("{scratch}/uv-cache/sdists-v11/.git", False),
    ],
)
def test_uv_marker_bucket_versions(context: ToolContext, marker: str, allowed: bool) -> None:
    """UV renames its sdist bucket between releases; only this run's bucket marker is writable."""
    assert context.scratch_root is not None
    scratch = context.scratch_root
    target = marker.format(scratch=str(scratch))
    code, output = execute(
        f'mkdir -p "$(dirname "{target}")" && printf marker > "{target}" && test -f "{target}"',
        context,
    )
    assert (code == 0) is allowed, output


@pytest.mark.parametrize("moved", ["$UV_CACHE_DIR/sdists-v11", "$UV_CACHE_DIR"])
def test_uv_marker_cannot_be_planted_in_workspace(context: ToolContext, moved: str) -> None:
    """Renaming a directory moves the exempt marker without checking the marker's path."""
    code, output = execute(
        'mkdir -p "$UV_CACHE_DIR/sdists-v11" && '
        'printf "gitdir: ../evil" > "$UV_CACHE_DIR/sdists-v11/.git" && '
        'printf "WROTE_MARKER\\n" && '
        f'mv "{moved}" planted',
        context,
    )
    assert code != 0, output
    # The marker was written, so the failure is the move and not an earlier step.
    assert "WROTE_MARKER" in output
    assert not (context.cwd / "planted").exists()


def test_uv_cache_symlink_dangles_after_command_exits(context: ToolContext) -> None:
    """Per-command UV_CACHE_DIR cleanup neutralizes workspace symlinks on exit."""
    code, output = execute(
        'mkdir -p "$UV_CACHE_DIR/sdists-v11" && '
        'printf "gitdir: ../evil" > "$UV_CACHE_DIR/sdists-v11/.git" && '
        'ln -s "$UV_CACHE_DIR/sdists-v11" linked-cache',
        context,
    )
    assert code == 0, output
    link = context.cwd / "linked-cache"
    assert link.is_symlink()
    assert not link.exists()
    assert not (link / ".git").exists()


def test_uv_cache_contents_stay_removable(context: ToolContext) -> None:
    """Only the cache and its buckets are pinned; UV still manages what is inside them."""
    code, output = execute(
        'mkdir -p "$UV_CACHE_DIR/sdists-v11/build" && '
        'mv "$UV_CACHE_DIR/sdists-v11/build" "$UV_CACHE_DIR/sdists-v11/build-moved" && '
        'rmdir "$UV_CACHE_DIR/sdists-v11/build-moved"',
        context,
    )
    assert code == 0, output


def test_uv_marker_cannot_rewrite_workspace_gitfile(context: ToolContext) -> None:
    """A nested gitfile must not round-trip through the exempt bucket."""
    gitfile = context.cwd / "sub/.git"
    gitfile.parent.mkdir()
    gitfile.write_text("gitdir: ../real")
    execute(
        'mkdir -p "$UV_CACHE_DIR" && mv sub "$UV_CACHE_DIR/sdists-v11" && '
        'printf "gitdir: ../evil" > "$UV_CACHE_DIR/sdists-v11/.git"; '
        'mv "$UV_CACHE_DIR/sdists-v11" sub',
        context,
    )
    assert not gitfile.exists() or gitfile.read_text() == "gitdir: ../real"


@pytest.mark.parametrize("attack", ["directory", "elsewhere", "symlink", "hardlink", "explicit"])
def test_uv_marker_exception_cannot_modify_git_or_secrets(
    context: ToolContext, attack: str
) -> None:
    git = context.cwd / ".git"
    git.mkdir()
    config = git / "config"
    config.write_text("synthetic git config")
    if attack == "directory":
        command = 'mkdir -p "$UV_CACHE_DIR/sdists-v9" && mkdir "$UV_CACHE_DIR/sdists-v9/.git"'
    elif attack == "elsewhere":
        command = "printf changed > .git/config"
    elif attack == "symlink":
        command = (
            'mkdir -p "$UV_CACHE_DIR/sdists-v9" && '
            'ln -s "$PWD/.git/config" "$UV_CACHE_DIR/sdists-v9/.git" && '
            'printf changed > "$UV_CACHE_DIR/sdists-v9/.git"'
        )
    elif attack == "hardlink":
        command = (
            'mkdir -p "$UV_CACHE_DIR/sdists-v9" && '
            'ln .git/config "$UV_CACHE_DIR/sdists-v9/.git" && '
            'printf changed > "$UV_CACHE_DIR/sdists-v9/.git"'
        )
    else:
        context = replace(context, protected_paths=(*context.protected_paths, "sdists-v9/.git"))
        command = (
            'mkdir -p "$UV_CACHE_DIR/sdists-v9" && printf changed > "$UV_CACHE_DIR/sdists-v9/.git"'
        )
    code, output = execute(command, context)
    assert code != 0, output
    assert config.read_text() == "synthetic git config"


@pytest.fixture
def package(context: ToolContext) -> Path:
    prefix = context.cwd / ".venv"
    credentials = prefix / "lib/python3.12/site-packages/example/credentials"
    credentials.mkdir(parents=True)
    (prefix / "pyvenv.cfg").write_text("# installed environment fixture\n")
    (credentials / "__init__.py").write_text("# public library source\n")
    (credentials / "token").write_text("synthetic secret")
    (credentials / "credentials.json").write_text("synthetic credential data")
    return credentials


def test_public_credential_module_source_only(context: ToolContext, package: Path) -> None:
    source = shlex.quote(str(package / "__init__.py"))
    assert execute(f"cat {source}", context)[0] == 0
    for name in ("token", "credentials.json"):
        assert execute(f"cat {shlex.quote(str(package / name))}", context)[0] != 0
    protected = replace(context, protected_paths=(*context.protected_paths, "credentials"))
    assert execute(f"cat {source}", protected)[0] != 0
    ordinary = context.cwd / "credentials"
    ordinary.mkdir()
    (ordinary / "secret.py").write_text("synthetic secret")
    assert execute("cat credentials/secret.py", context)[0] != 0


@pytest.mark.parametrize("attack", ["write", "move-prefix", "move-parent", "move-secret", "link"])
def test_package_read_exception_cannot_be_repopulated(
    context: ToolContext, package: Path, attack: str
) -> None:
    secret = context.cwd / "credentials"
    secret.mkdir()
    (secret / "secret.py").write_text("synthetic secret")
    relative = package.relative_to(context.cwd).as_posix()
    command = {
        "write": f"printf changed > {relative}/__init__.py",
        "move-prefix": "mv .venv renamed",
        "move-parent": "mv .venv/lib renamed",
        "move-secret": f"mv credentials {relative}/moved",
        "link": f"ln credentials/secret.py {relative}/alias.py",
    }[attack]
    code, output = execute(command, context)
    assert code != 0, output
    assert (secret / "secret.py").read_text() == "synthetic secret"


def test_venv_cannot_be_planted_or_extended_across_commands(context: ToolContext) -> None:
    """Neither creating .venv nor adding a sibling python*/site-packages may expose secrets."""
    secret_dir = context.cwd / "sub/credentials"
    secret_dir.mkdir(parents=True)
    (secret_dir / "keys.py").write_text("SECRET = 'synthetic'\n")

    # Call 1 attempts to create a brand-new .venv and move workspace credentials into it.
    code, output = execute(
        "mkdir -p .venv/lib/python999/site-packages && touch .venv/pyvenv.cfg && "
        "mv sub .venv/lib/python999/site-packages/sub",
        context,
    )
    assert code != 0, output
    assert not (context.cwd / ".venv").exists()

    # Even with an existing .venv, adding a sibling python*/site-packages directory is denied.
    existing = context.cwd / ".venv/lib/python3.12/site-packages"
    existing.mkdir(parents=True)
    (context.cwd / ".venv/pyvenv.cfg").write_text("# existing venv\n")
    code, output = execute(
        "mkdir -p .venv/lib/python999/site-packages && "
        "mv sub .venv/lib/python999/site-packages/sub",
        context,
    )
    assert code != 0, output
    assert not (context.cwd / ".venv/lib/python999").exists()


def test_workspace_or_scratch_root_pyvenv_cfg_cannot_expose_credentials(
    context: ToolContext,
) -> None:
    """Writable workspace and scratch roots are never scanned as Python virtualenv prefixes."""
    assert context.scratch_root is not None
    secret_dir = context.cwd / "sub/credentials"
    secret_dir.mkdir(parents=True)
    (secret_dir / "keys.py").write_text("SECRET = 'synthetic'\n")

    # Plant pyvenv.cfg directly in workspace root on Call 1; Call 2 must still block reading it.
    code, output = execute(
        "mkdir -p lib/python999/site-packages && touch pyvenv.cfg && "
        "mv sub lib/python999/site-packages/sub",
        context,
    )
    assert code == 0, output
    code, output = execute("cat lib/python999/site-packages/sub/credentials/keys.py", context)
    assert code != 0, output
    assert "SECRET" not in output

    # Plant pyvenv.cfg directly in scratch root too.
    scratch_pkg = context.scratch_root / "lib/python999/site-packages/sub/credentials"
    scratch_pkg.mkdir(parents=True)
    (context.scratch_root / "pyvenv.cfg").write_text("# fake\n")
    (scratch_pkg / "keys.py").write_text("SECRET = 'scratch'\n")
    code, output = execute(f"cat {shlex.quote(str(scratch_pkg / 'keys.py'))}", context)
    assert code != 0, output
    assert "SECRET" not in output


def test_workspace_venv_symlink_into_writable_workspace_is_rejected(
    context: ToolContext,
) -> None:
    """A .venv symlink pointing back into the writable workspace must fail closed."""
    from wisp.tools.result import ToolError

    real_venv = context.cwd / "real_venv"
    (real_venv / "lib/python3.12/site-packages").mkdir(parents=True)
    (real_venv / "pyvenv.cfg").write_text("# fake\n")
    (context.cwd / ".venv").symlink_to(real_venv, target_is_directory=True)
    with pytest.raises(ToolError, match="writable workspace"):
        prepare_seatbelt({"command": "pytest"}, context)


def test_non_ascii_site_packages_stays_read_only(context: ToolContext, tmp_path: Path) -> None:
    """Non-ASCII resolved site-packages directories still enforce write denials."""
    venv = tmp_path / "café-venv"
    site = venv / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("# non-ascii venv\n")
    (site / "mod.py").write_text("# original\n")
    (context.cwd / ".venv").symlink_to(venv, target_is_directory=True)

    code, output = execute(f"printf TAMPERED > {shlex.quote(str(site / 'mod.py'))}", context)
    assert code != 0, output
    assert (site / "mod.py").read_text() == "# original\n"
