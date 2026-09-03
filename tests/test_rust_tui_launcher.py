from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

from wisp import __version__
from wisp import cli as cli_module
from wisp.cli import app
from wisp.cli.types import TuiFrontendKind
from wisp.config import WispConfig
from wisp.tui import rust_launcher
from wisp.tui.launch import TuiOptions
from wisp.tui.rust_launcher import RustTuiLaunchError


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


def test_bare_interactive_wisp_defaults_to_textual(
    monkeypatch: MonkeyPatch,
) -> None:
    launched: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_run_tui_from_cli_options",
        lambda **kwargs: launched.update(kwargs),
    )

    result = CliRunner().invoke(app, [], env=_cli_env())

    assert result.exit_code == 0, result.output
    assert launched["renderer"] is TuiFrontendKind.textual


def test_bare_interactive_wisp_honors_rust_environment_selection(
    monkeypatch: MonkeyPatch,
) -> None:
    launched: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_terminal_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_run_tui_from_cli_options",
        lambda **kwargs: launched.update(kwargs),
    )

    result = CliRunner().invoke(app, [], env={**_cli_env(), "WISP_TUI_RENDERER": "rust"})

    assert result.exit_code == 0, result.output
    assert launched["renderer"] is TuiFrontendKind.rust


@pytest.mark.parametrize(
    "arguments",
    [
        ["--mode", "tui", "--tui-renderer", "rust"],
        ["tui", "--renderer", "rust"],
    ],
)
def test_explicit_rust_frontend_routes_to_rust_launcher(
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
from wisp.tui import rust_launcher

with patch.object(rust_launcher, "run_rust_tui", return_value=0):
    result = CliRunner().invoke(app, ["tui", "--renderer", "rust"])
assert result.exit_code == 0, result.output
assert "wisp.tui.app" not in sys.modules
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


def test_explicit_root_renderer_wins_over_environment(monkeypatch: MonkeyPatch) -> None:
    launched: dict[str, object] = {}
    monkeypatch.setattr(
        cli_module,
        "_run_tui_from_cli_options",
        lambda **kwargs: launched.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        ["--mode", "tui", "--tui-renderer", "textual"],
        env={**_cli_env(), "WISP_TUI_RENDERER": "rust"},
    )

    assert result.exit_code == 0, result.output
    assert launched["renderer"] is TuiFrontendKind.textual


def test_tui_line_alias_is_preserved(monkeypatch: MonkeyPatch) -> None:
    launched: dict[str, object] = {}
    monkeypatch.setattr(
        cli_module,
        "_run_tui_from_cli_options",
        lambda **kwargs: launched.update(kwargs),
    )

    result = CliRunner().invoke(app, ["tui", "--line"], env=_cli_env())

    assert result.exit_code == 0, result.output
    assert launched["renderer"] is TuiFrontendKind.line


def test_tui_line_alias_conflicts_with_renderer() -> None:
    result = CliRunner().invoke(app, ["tui", "--line", "--renderer", "rust"])

    assert result.exit_code == 1
    assert "use either --line or --renderer, not both" in result.output


def test_explicit_rust_failure_does_not_fall_back(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rust_launcher, "run_rust_tui", lambda _options: 23)

    result = CliRunner().invoke(app, ["tui", "--renderer", "rust"], env=_cli_env())

    assert result.exit_code == 23
    assert "Rust TUI exited with status 23" in result.output


def test_cli_missing_rust_binary_does_not_fall_back(tmp_path: Path) -> None:
    missing = tmp_path / "missing-wisp-tui"
    result = CliRunner().invoke(
        app,
        ["tui", "--renderer", "rust"],
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

result = CliRunner().invoke(app, ["tui", "--renderer", "rust"])
assert result.exit_code == 1, result.output
assert "was not found" in result.output
assert "wisp.tui.app" not in sys.modules
assert "wisp.tui.textual_app" not in sys.modules
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
        ["tui", "--renderer", "rust"],
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


def test_binary_resolution_uses_package_owned_location(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    binary = tmp_path / "wisp-tui"
    binary.write_bytes(b"binary")
    binary.chmod(0o700)
    monkeypatch.delenv("WISP_RUST_TUI_BINARY", raising=False)
    monkeypatch.setattr(rust_launcher, "_PACKAGED_BINARY", binary)

    assert rust_launcher.resolve_rust_tui_binary() == binary.resolve()


def test_windows_error_is_actionable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(RustTuiLaunchError, match="macOS and Linux") as raised:
        rust_launcher.resolve_rust_tui_binary()

    assert "wisp tui --renderer textual" in str(raised.value)


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
    monkeypatch.setenv("WISP_RUST_TUI_BINARY", str(binary))
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
    assert "WISP_RUST_TUI_BINARY" not in environment
    assert process_group == 0
    assert os.environ["WISP_LAUNCHER_TEST_SENTINEL"] == "inherited"
