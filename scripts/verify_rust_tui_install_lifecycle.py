"""Verify installed Rust TUI wheel ownership and lifecycle behavior."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def _run(
    *command: str | Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _scripts(environment: Path) -> tuple[Path, Path, Path]:
    scripts = environment / "bin"
    return scripts / "python", scripts / "wisp", scripts / "wisp-tui"


def _consumer_environment(environment: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{environment / 'bin'}:/usr/bin:/bin",
    }


def _install(uv: Path, python: Path, wheel: Path, *, offline: bool = False) -> None:
    command = [
        str(uv),
        "pip",
        "install",
        "--python",
        str(python),
        "--reinstall",
        "--no-deps",
    ]
    if offline:
        command.append("--offline")
    command.append(str(wheel))
    _run(*command)


def _resolve_installed_binary(python: Path, expected: Path) -> None:
    completed = _run(
        python,
        "-c",
        "from wisp.tui.rust_launcher import resolve_rust_tui_binary; "
        "print(resolve_rust_tui_binary())",
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    if Path(completed.stdout.strip()).resolve() != expected.resolve():
        raise RuntimeError(f"launcher resolved unexpected Rust TUI: {completed.stdout!r}")


def _expect_resolution_failure(python: Path, message: str) -> None:
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "from wisp.tui.rust_launcher import resolve_rust_tui_binary; resolve_rust_tui_binary()",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode == 0 or message not in completed.stderr:
        raise RuntimeError(
            f"expected launcher failure containing {message!r}: "
            f"status={completed.returncode}, stderr={completed.stderr!r}"
        )


def _verify_frontend_selection(python: Path, *, native: bool) -> None:
    script = """
import sys
from unittest.mock import patch
from typer.testing import CliRunner
from wisp import cli as cli_module
from wisp.cli import app

for arguments in [[], ["tui"], ["--mode", "tui"], ["tui", "--renderer", "textual"]]:
    selected = {}
    with patch.object(cli_module, "_terminal_is_interactive", return_value=True), patch.object(
        cli_module, "_run_tui_from_cli_options",
        side_effect=lambda **kwargs: selected.update(kwargs),
    ):
        result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    expected = "textual" if "--renderer" in arguments else sys.argv[1]
    assert selected["renderer"].value == expected, (arguments, selected)
"""
    environment = {**os.environ, "PATH": "/usr/bin:/bin"}
    environment.pop("WISP_TUI_RENDERER", None)
    environment.pop("WISP_RUST_TUI_BINARY", None)
    _run(python, "-c", script, "rust" if native else "textual", env=environment)


def _expect_corrupt_launch_failure(wisp: Path, rust_tui: Path, environment: Path) -> None:
    rust_tui.write_bytes(b"not a native executable")
    rust_tui.chmod(0o700)
    completed = subprocess.run(
        [str(wisp), "tui"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **_consumer_environment(environment),
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_TRUST": "1",
        },
    )
    output = completed.stdout + completed.stderr
    if completed.returncode == 0 or "failed to start Rust TUI binary" not in output:
        raise RuntimeError(
            "corrupt Rust TUI did not fail clearly: "
            f"status={completed.returncode}, output={output!r}"
        )


def _smoke(
    python: Path,
    wisp: Path,
    smoke_script: Path,
    session_dir: Path,
    *,
    history_messages: int = 0,
) -> dict[str, float | int]:
    try:
        completed = _run(
            python,
            smoke_script,
            "--wisp",
            wisp,
            "--session-dir",
            session_dir,
            "--history-messages",
            str(history_messages),
            env=_consumer_environment(python.parent.parent),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Installed Rust TUI smoke failed ({exc.returncode}):\n{exc.stdout}\n{exc.stderr}"
        ) from exc
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise RuntimeError("installed Rust TUI smoke returned invalid measurements")
    return result


def verify(
    *,
    environment: Path,
    uv: Path,
    native_wheel: Path,
    pure_wheel: Path,
    smoke_script: Path,
    work_dir: Path,
    evidence: Path,
) -> None:
    """Exercise native/pure replacement, corruption, offline install, and uninstall.

    Args:
        environment: Isolated virtual environment with native wheel dependencies installed.
        uv: Absolute uv executable used for package mutations.
        native_wheel: Native Rust TUI platform wheel.
        pure_wheel: Pure fallback wheel for the same version.
        smoke_script: PTY smoke script from the source checkout.
        work_dir: Disposable directory for smoke sessions.
        evidence: JSON file receiving size, startup, and PTY measurements.

    Raises:
        RuntimeError: If any ownership or lifecycle invariant fails.
        subprocess.CalledProcessError: If a required package operation fails.
    """

    python, wisp, rust_tui = _scripts(environment)
    _resolve_installed_binary(python, rust_tui)
    binary_started = time.monotonic()
    _run(rust_tui, "--version", env=_consumer_environment(environment))
    binary_startup_seconds = time.monotonic() - binary_started
    _verify_frontend_selection(python, native=True)
    initial_smoke = _smoke(python, wisp, smoke_script, work_dir / "native-initial")

    long_history_smoke = _smoke(
        python,
        wisp,
        smoke_script,
        work_dir / "native-long-history",
        history_messages=10_000,
    )

    _install(uv, python, pure_wheel)
    if rust_tui.exists():
        raise RuntimeError("pure fallback replacement left an orphaned wisp-tui executable")
    _expect_resolution_failure(python, "active Python environment")
    _run(wisp, "--help", env=_consumer_environment(environment))
    _verify_frontend_selection(python, native=False)

    _install(uv, python, native_wheel)
    _resolve_installed_binary(python, rust_tui)
    _smoke(python, wisp, smoke_script, work_dir / "native-restored")

    original_mode = rust_tui.stat().st_mode
    rust_tui.chmod(0o600)
    try:
        _expect_resolution_failure(python, "not executable")
    finally:
        rust_tui.chmod(original_mode)

    _expect_corrupt_launch_failure(wisp, rust_tui, environment)
    _install(uv, python, native_wheel)
    _resolve_installed_binary(python, rust_tui)

    _run(uv, "pip", "uninstall", "--python", python, "wisp-ai")
    if wisp.exists() or rust_tui.exists():
        raise RuntimeError("uninstall left an orphaned Wisp executable")

    _install(uv, python, native_wheel, offline=True)
    _resolve_installed_binary(python, rust_tui)
    _smoke(python, wisp, smoke_script, work_dir / "native-offline")

    _run(uv, "pip", "uninstall", "--python", python, "wisp-ai")
    if wisp.exists() or rust_tui.exists():
        raise RuntimeError("final uninstall left an orphaned Wisp executable")

    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "wheel_bytes": native_wheel.stat().st_size,
                "binary_bytes": initial_smoke.get("binary_bytes", 0),
                "binary_startup_seconds": binary_startup_seconds,
                **initial_smoke,
                "long_history": long_history_smoke,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Run installed-wheel lifecycle verification from the command line."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--native-wheel", type=Path, required=True)
    parser.add_argument("--pure-wheel", type=Path, required=True)
    parser.add_argument("--smoke-script", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    verify(
        environment=arguments.environment,
        uv=arguments.uv,
        native_wheel=arguments.native_wheel,
        pure_wheel=arguments.pure_wheel,
        smoke_script=arguments.smoke_script,
        work_dir=arguments.work_dir,
        evidence=arguments.evidence,
    )


if __name__ == "__main__":
    main()
