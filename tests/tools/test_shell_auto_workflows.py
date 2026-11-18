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


@pytest.mark.parametrize("attack", ["directory", "elsewhere", "symlink", "hardlink", "explicit"])
def test_uv_marker_exception_cannot_modify_git_or_secrets(
    context: ToolContext, attack: str
) -> None:
    assert context.scratch_root is not None
    cache = context.scratch_root / "uv-cache/sdists-v9"
    cache.mkdir(parents=True)
    marker = cache / ".git"
    git = context.cwd / ".git"
    git.mkdir()
    config = git / "config"
    config.write_text("synthetic git config")
    quoted = shlex.quote(str(marker))
    if attack == "directory":
        command = f"mkdir {quoted}"
    elif attack == "elsewhere":
        command = "printf changed > .git/config"
    elif attack == "symlink":
        marker.symlink_to(config)
        command = f"printf changed > {quoted}"
    elif attack == "hardlink":
        command = f"ln .git/config {quoted} && printf changed > {quoted}"
    else:
        context = replace(context, protected_paths=(*context.protected_paths, str(marker)))
        command = f"printf changed > {quoted}"
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
