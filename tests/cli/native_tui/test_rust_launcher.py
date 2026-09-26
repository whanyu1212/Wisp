from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.utils import strip_ansi
from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp import __version__
from wisp.cli import app
from wisp.cli import application as cli_module
from wisp.cli.native_tui import rust_binary, rust_launcher
from wisp.cli.native_tui.launch import TuiOptions
from wisp.cli.native_tui.rust_launcher import RustTuiLaunchError
from wisp.config.runtime import WispConfig
from wisp.config.settings import ResolvedSettings


def _cli_env() -> dict[str, str]:
    return {"WISP_PROVIDER": "fake", "WISP_MODEL": "", "WISP_TRUST": "1"}


def _options(tmp_path: Path) -> TuiOptions:
    return TuiOptions(
        config=WispConfig(provider="fake", session_dir=tmp_path),
        user_provider="fake",
        user_model="model-x",
        user_session_dir=tmp_path,
        resume="session-1",
        allow_read_tools=True,
        allowed_tools=("read",),
    )


@pytest.mark.parametrize("arguments", [[], ["tui"], ["--mode", "tui"]])
def test_default_frontend_selects_rust_even_without_native_distribution(
    arguments: list[str],
    monkeypatch: MonkeyPatch,
) -> None:
    launched: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli_module, "_run_tui_from_cli_options", lambda **kwargs: launched.update(kwargs)
    )
    monkeypatch.delenv("WISP_RUST_TUI_BINARY", raising=False)
    monkeypatch.setattr(
        rust_binary.metadata,
        "distribution",
        lambda _: SimpleNamespace(files=()),
    )
    result = CliRunner().invoke(app, arguments, env=_cli_env())
    assert result.exit_code == 0, result.output
    assert launched["config"].provider == "fake"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "arguments",
    [
        ["tui", "--renderer", "rust"],
        ["--mode", "tui", "--tui-renderer", "auto"],
    ],
)
def test_removed_renderer_options_are_rejected(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments, env=_cli_env())

    assert result.exit_code != 0
    assert "No such option" in strip_ansi(result.output)


@pytest.mark.parametrize(
    "arguments",
    [["tui", "--no-synchronized-output"], ["--mode", "tui", "--no-synchronized-output"]],
)
def test_removed_textual_only_option_is_rejected(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments, env=_cli_env())

    assert result.exit_code != 0
    assert "No such option: --no-synchronized-output" in strip_ansi(result.output)


@pytest.mark.parametrize(
    "arguments",
    [
        ["tui", "--renderer", "fullscreen"],
        ["--mode", "tui", "--tui-renderer", "line"],
        ["tui", "--line"],
    ],
)
def test_removed_python_frontend_options_are_rejected(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments, env=_cli_env())
    assert result.exit_code != 0


@pytest.mark.parametrize("failure", ["missing", "nonexecutable", "exit"])
def test_automatic_rust_failure_does_not_fall_back(
    failure: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    binary = tmp_path / "wisp-tui"
    if failure != "missing":
        binary.write_bytes(b"invalid executable")
        binary.chmod(0o600)
    monkeypatch.setattr(
        rust_binary.metadata,
        "distribution",
        lambda _: SimpleNamespace(files=(SimpleNamespace(name="wisp-tui", locate=lambda: binary),)),
    )
    monkeypatch.delenv("WISP_RUST_TUI_BINARY", raising=False)
    if failure == "exit":
        monkeypatch.setattr(rust_launcher, "run_rust_tui", lambda _: 23)
    result = CliRunner().invoke(app, ["tui"], env=_cli_env())
    assert result.exit_code == (23 if failure == "exit" else 1)
    if failure != "exit":
        assert "WISP_RUST_TUI_BINARY" in result.output or "Rust TUI binary" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--mode", "tui"],
        ["tui"],
    ],
)
def test_tui_entry_points_route_to_rust_launcher(
    arguments: list[str],
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[TuiOptions] = []
    monkeypatch.setattr(
        rust_launcher, "run_rust_tui", lambda options: captured.append(options) or 0
    )

    result = CliRunner().invoke(app, arguments, env=_cli_env())

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].config.provider == "fake"


def test_rust_branch_does_not_import_python_tui_app() -> None:
    script = """
import sys
from unittest.mock import patch

from typer.testing import CliRunner

from wisp.cli import app
from wisp.cli.native_tui import rust_launcher

with patch.object(rust_launcher, "run_rust_tui", return_value=0):
    result = CliRunner().invoke(app, ["tui"])
assert result.exit_code == 0, result.output
assert "wisp.tui" not in sys.modules
"""
    environment = {
        **os.environ,
        "WISP_PROVIDER": "fake",
        "WISP_MODEL": "",
        "WISP_TRUST": "1",
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_rust_failure_does_not_fall_back(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rust_launcher, "run_rust_tui", lambda _options: 23)

    result = CliRunner().invoke(app, ["tui"], env=_cli_env())

    assert result.exit_code == 23
    assert "Rust TUI exited with status 23" in result.output


def test_cli_missing_rust_binary_does_not_fall_back(tmp_path: Path) -> None:
    missing = tmp_path / "missing-wisp-tui"
    result = CliRunner().invoke(
        app,
        ["tui"],
        env={**_cli_env(), "WISP_RUST_TUI_BINARY": str(missing)},
    )

    assert result.exit_code == 1
    assert "was not found" in result.output
    assert "Rust TUI exited with status" not in result.output


def test_cli_missing_rust_binary_does_not_import_python_tui_app(tmp_path: Path) -> None:
    missing = tmp_path / "missing-wisp-tui"
    script = """
import sys
from typer.testing import CliRunner

from wisp.cli import app

result = CliRunner().invoke(app, ["tui"])
assert result.exit_code == 1, result.output
assert "was not found" in result.output
assert "wisp.tui" not in sys.modules
"""
    environment = {
        **os.environ,
        **_cli_env(),
        "WISP_RUST_TUI_BINARY": str(missing),
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_cli_nonexecutable_rust_binary_does_not_fall_back(tmp_path: Path) -> None:
    binary = tmp_path / "wisp-tui"
    binary.write_bytes(b"binary")
    binary.chmod(0o600)
    result = CliRunner().invoke(
        app,
        ["tui"],
        env={**_cli_env(), "WISP_RUST_TUI_BINARY": str(binary)},
    )

    assert result.exit_code == 1
    assert "is not executable" in result.output
    assert "Rust TUI exited with status" not in result.output


def test_binary_override_must_be_absolute(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("WISP_RUST_TUI_BINARY", "target/debug/wisp-tui")

    with pytest.raises(RustTuiLaunchError, match="must be an absolute executable path"):
        rust_launcher.resolve_rust_tui_binary()


def test_binary_override_must_exist_and_be_executable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    missing = tmp_path / "missing-wisp-tui"
    monkeypatch.setenv("WISP_RUST_TUI_BINARY", str(missing))
    with pytest.raises(RustTuiLaunchError, match="was not found"):
        rust_launcher.resolve_rust_tui_binary()

    binary = tmp_path / "wisp-tui"
    binary.write_bytes(b"binary")
    binary.chmod(0o600)
    monkeypatch.setenv("WISP_RUST_TUI_BINARY", str(binary))
    with pytest.raises(RustTuiLaunchError, match="is not executable"):
        rust_launcher.resolve_rust_tui_binary()

    binary.chmod(0o700)
    assert rust_launcher.resolve_rust_tui_binary() == binary.resolve()


def test_binary_resolution_uses_active_environment_scripts_not_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    scripts = tmp_path / "environment" / "bin"
    scripts.mkdir(parents=True)
    binary = scripts / "wisp-tui"
    binary.write_bytes(b"binary")
    binary.chmod(0o700)
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "wisp-tui").write_bytes(b"hostile")
    monkeypatch.delenv("WISP_RUST_TUI_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(hostile))
    installed_binary = SimpleNamespace(name="wisp-tui", locate=lambda: binary)
    monkeypatch.setattr(
        rust_binary.metadata,
        "distribution",
        lambda name: SimpleNamespace(files=(installed_binary,)),
    )

    assert rust_launcher.resolve_rust_tui_binary() == binary.resolve()


def test_missing_environment_binary_is_actionable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("WISP_RUST_TUI_BINARY", raising=False)
    monkeypatch.setattr(
        rust_binary.metadata,
        "distribution",
        lambda name: SimpleNamespace(files=()),
    )

    with pytest.raises(RustTuiLaunchError, match="no Rust TUI binary") as raised:
        rust_launcher.resolve_rust_tui_binary()

    assert "WISP_RUST_TUI_BINARY" in str(raised.value)


def test_windows_error_is_actionable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(RustTuiLaunchError, match="macOS and Linux") as raised:
        rust_launcher.resolve_rust_tui_binary()

    assert "Rust TUI" in str(raised.value)


def test_launcher_imports_without_posix_termios() -> None:
    script = """
import builtins
import sys

import wisp.cli

real_import = builtins.__import__
def import_without_termios(name, *args, **kwargs):
    if name == "termios":
        raise ModuleNotFoundError("No module named 'termios'")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_termios
sys.platform = "win32"
from wisp.cli.native_tui import rust_launcher

try:
    rust_launcher.resolve_rust_tui_binary()
except rust_launcher.RustTuiLaunchError as exc:
    assert "macOS and Linux" in str(exc)
else:
    raise AssertionError("unsupported platform unexpectedly launched the Rust TUI")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_rust_argv_forwards_exact_interpreter_and_opaque_backend_argv(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/wisp-venv/bin/python")
    binary = tmp_path / "wisp-tui"

    command = rust_launcher.rust_tui_command(binary, _options(tmp_path))

    assert command == (
        str(binary),
        "--expected-backend-version",
        __version__,
        "--",
        "/opt/wisp-venv/bin/python",
        "-m",
        "wisp",
        "--mode",
        "rpc",
        "--provider",
        "fake",
        "--model",
        "model-x",
        "--session-dir",
        str(tmp_path),
        "--resume",
        "session-1",
        "--allow-read-tools",
        "--allow-tool",
        "read",
    )


def test_launcher_spawns_without_shell_and_preserves_backend_environment(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    binary = tmp_path / "wisp-tui"
    calls: list[tuple[tuple[str, ...], dict[str, str], int]] = []

    class FakeProcess:
        pid = 4321

        def poll(self) -> int:
            return 0

    async def fake_preflight(_options: TuiOptions) -> None:
        return None

    def fake_popen(
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        process_group: int,
    ) -> FakeProcess:
        calls.append((argv, env, process_group))
        return FakeProcess()

    monkeypatch.setenv("WISP_LAUNCHER_TEST_SENTINEL", "inherited")
    monkeypatch.setenv("WISP_TUI_MOUSE", "1")
    monkeypatch.setenv("WISP_RUST_TUI_BINARY", str(binary))
    monkeypatch.setenv("WISP_RUST_TUI_BINDINGS_JSON", '{"inherited":["f12"]}')
    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: binary)
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", fake_preflight)
    monkeypatch.setattr(rust_launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rust_launcher, "_snapshot_terminal", lambda: None)
    monkeypatch.setattr(rust_launcher, "_cleanup_process_group", lambda *_args, **_kwargs: None)

    assert rust_launcher.run_rust_tui(_options(tmp_path)) == 0
    assert len(calls) == 1
    argv, environment, process_group = calls[0]
    assert argv == rust_launcher.rust_tui_command(binary, _options(tmp_path))
    assert environment["WISP_LAUNCHER_TEST_SENTINEL"] == "inherited"
    assert environment["WISP_TUI_MOUSE"] == "1"
    assert "WISP_RUST_TUI_BINARY" not in environment
    assert environment["WISP_RUST_TUI_BINDINGS_JSON"] == "{}"
    assert process_group == 0
    assert os.environ["WISP_LAUNCHER_TEST_SENTINEL"] == "inherited"


def test_launcher_forwards_resolved_user_keybindings_as_bounded_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    binary = tmp_path / "wisp-tui"
    environments: list[dict[str, str]] = []

    class FakeProcess:
        pid = 4321

        def poll(self) -> int:
            return 0

    async def fake_preflight(_options: TuiOptions) -> None:
        return None

    def fake_popen(
        _argv: tuple[str, ...],
        *,
        env: dict[str, str],
        process_group: int,
    ) -> FakeProcess:
        assert process_group == 0
        environments.append(env)
        return FakeProcess()

    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: binary)
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", fake_preflight)
    monkeypatch.setattr(
        rust_launcher,
        "resolve_settings",
        lambda **kwargs: (
            ResolvedSettings(
                tui_keybindings={
                    "prompt.submit": ["ctrl+enter"],
                    "theme.toggle": [],
                }
            )
            if kwargs == {"trust_project": False}
            else pytest.fail(f"unexpected settings resolution: {kwargs}")
        ),
    )
    monkeypatch.setattr(rust_launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rust_launcher, "_snapshot_terminal", lambda: None)
    monkeypatch.setattr(rust_launcher, "_cleanup_process_group", lambda *_args, **_kwargs: None)

    assert rust_launcher.run_rust_tui(_options(tmp_path)) == 0
    assert len(environments) == 1
    assert json.loads(environments[0]["WISP_RUST_TUI_BINDINGS_JSON"]) == {
        "prompt.submit": ["ctrl+enter"],
        "theme.toggle": [],
    }
