"""Native theme catalog, readable palettes, and real launcher/preference handoff."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from wisp.tui.theme import WISP_THEME_SPECS, WispThemeSpec, contrast_ratio
from wisp.tui.theme_preference import load_theme_state

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "rust/wisp-tui/src/theme_catalog.json"


@pytest.mark.tui
def test_native_theme_catalog_is_current() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_tui_themes.py"), "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    catalog = json.loads(CATALOG.read_text())
    assert [entry["name"] for entry in catalog["themes"]] == [
        spec.name for spec in WISP_THEME_SPECS
    ]
    assert catalog["command"]["slash_command"] == "/theme"


@pytest.mark.tui
@pytest.mark.parametrize("spec", WISP_THEME_SPECS, ids=lambda spec: spec.slug)
def test_native_semantic_colors_are_readable(spec: WispThemeSpec) -> None:
    catalog = json.loads(CATALOG.read_text())
    colors = next(entry["colors"] for entry in catalog["themes"] if entry["name"] == spec.name)
    for background in ("background", "surface"):
        for foreground in (
            "foreground",
            "muted",
            "primary",
            "secondary",
            "accent",
            "warning",
            "error",
            "success",
        ):
            assert contrast_ratio(colors[foreground], colors[background]) >= 4.5, (
                spec.slug,
                foreground,
                background,
            )
    for operation in ("addition", "deletion"):
        assert contrast_ratio(colors[operation], colors[f"{operation}_background"]) >= 4.5


def _background(name: str, no_color: bool) -> tuple[int, int, int]:
    """Return the expected embedded RGB background for a persisted theme name.

    Args:
        name (str): Canonical Wisp theme name.
        no_color (bool): Whether the frontend should convert the palette to monochrome.

    Returns:
        tuple[int, int, int]: The RGB triplet expected in terminal output.
    """
    catalog = json.loads(CATALOG.read_text())
    value = next(
        entry["colors"]["background"] for entry in catalog["themes"] if entry["name"] == name
    )
    rgb = (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    if no_color:
        gray = int(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] + 0.5)
        return gray, gray, gray
    return rgb


def _run_theme_session(
    binary: Path, project: Path, session_dir: Path, *, no_color: bool, preview: bool
) -> None:
    """Exercise native preview/toggle without sending a prompt or touching real user state.

    Args:
        binary (Path): Built native frontend.
        project (Path): Isolated project working directory.
        session_dir (Path): Isolated backend session directory.
        no_color (bool): Whether NO_COLOR is set before frontend startup.
        preview (bool): Test preview/cancel before toggling on the first launch.
    """
    initial = "wisp-wave" if preview else "wisp-light"
    toggled = "wisp-light" if preview else "wisp-wave"
    environment = {
        **os.environ,
        "WISP_PROVIDER": "fake",
        "WISP_MODEL": "",
        "WISP_RUST_TUI_BINARY": str(binary),
        "WISP_TRUST": "1",
    }
    environment.pop("NO_COLOR", None)
    if no_color:
        environment["NO_COLOR"] = "1"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-m",
                "wisp",
                "tui",
                "--renderer",
                "rust",
                "--session-dir",
                str(session_dir),
            ],
            environment,
        )
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 120, 0, 0))
    initial_terminal = termios.tcgetattr(terminal_fd)
    output = bytearray()
    all_output = bytearray()
    status: int | None = None
    phase = "startup"
    context_redrawn = False
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([terminal_fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(terminal_fd, 65536)
                    output.extend(chunk)
                    all_output.extend(chunk)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            backgrounds = {
                tuple(map(int, match)) for match in re.findall(rb"48;2;(\d+);(\d+);(\d+)", output)
            }
            if not context_redrawn and b"ctx" in output and b"~" in output:
                fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 121, 0, 0))
                context_redrawn = True
                output.clear()
                continue
            if (
                phase == "startup"
                and context_redrawn
                and b"Type a prompt or / for commands." in output
            ):
                assert _background(initial, no_color) in backgrounds
                os.write(terminal_fd, b"/theme\r" if preview else b"\x14")
                phase = "picker" if preview else "toggled"
                output.clear()
            elif phase == "picker" and b"Themes" in output:
                os.write(terminal_fd, b"\x1b[B")  # Wave -> Paper, preview only.
                phase = "preview"
                output.clear()
            elif phase == "preview" and _background("wisp-light", no_color) in backgrounds:
                assert load_theme_state().active_theme == "wisp-wave"
                os.write(terminal_fd, b"\x14\x1b")  # Toggle is ignored while preview owns focus.
                phase = "cancelled"
                output.clear()
            elif phase == "cancelled" and _background("wisp-wave", no_color) in backgrounds:
                assert load_theme_state().active_theme == "wisp-wave"
                os.write(terminal_fd, b"\x14")
                phase = "toggled"
                output.clear()
            elif phase == "toggled" and _background(toggled, no_color) in backgrounds:
                state = load_theme_state()
                assert state.active_theme == toggled
                assert state.last_dark_theme == "wisp-wave"
                os.write(terminal_fd, b"\x03")
                phase = "quit"
            waited_pid, waited_status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                status = waited_status
                break
        if status is None:
            pytest.fail(f"Rust theme workflow timed out in {phase!r}: {bytes(output)!r}")
        assert phase == "quit", (phase, bytes(output))
        assert os.waitstatus_to_exitcode(status) == 0, bytes(output)
        assert termios.tcgetattr(terminal_fd) == initial_terminal
        if no_color:
            colors = re.findall(rb"(?:38|48);2;(\d+);(\d+);(\d+)", all_output)
            assert colors
            assert all(r == g == b for r, g, b in colors)
        assert not list(session_dir.glob("*.jsonl")), "theme actions must remain frontend-local"
    finally:
        if status is None:
            try:
                os.killpg(os.tcgetpgrp(terminal_fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(terminal_fd)
        if status is None:
            # Closing the master releases Darwin PTY writes before reaping. Never
            # let failure cleanup hide the original assertion behind an endless wait.
            cleanup_deadline = time.monotonic() + 5
            while time.monotonic() < cleanup_deadline:
                waited_pid, _ = os.waitpid(child_pid, os.WNOHANG)
                if waited_pid == child_pid:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError(f"Timed out reaping theme-test child {child_pid}")


@pytest.mark.process
@pytest.mark.parametrize("no_color", [False, True], ids=["color", "monochrome"])
def test_rust_theme_preferences_interoperate_with_textual_and_survive_restart(
    tmp_path: Path, no_color: bool
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    project = tmp_path / "project"
    project.mkdir()
    path = Path.home() / ".wisp/tui.json"  # conftest provides an isolated HOME.
    path.parent.mkdir(parents=True, exist_ok=True)
    unrelated = {"preserve": True, "large_integer": 2**128 + 1, "nested": [-(2**127) - 1]}
    path.write_text(
        json.dumps({"theme": "wisp-wave", "last_dark_theme": "wisp-wave", "unrelated": unrelated})
    )
    for preview in (True, False):
        _run_theme_session(
            binary, project, tmp_path / "sessions", no_color=no_color, preview=preview
        )
        assert json.loads(path.read_text())["unrelated"] == unrelated
    assert load_theme_state().active_theme == "wisp-wave"
