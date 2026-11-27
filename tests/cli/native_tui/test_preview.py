"""Worker lifetime and the launcher's one-way backend handoff."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wisp.cli import app, application
from wisp.cli.native_tui import preview, rust_launcher
from wisp.cli.native_tui.launch import TuiOptions
from wisp.cli.startup_pipe import PreviewWorkspace
from wisp.config.runtime import WispConfig
from wisp.trust.flow import TrustDecision


@pytest.mark.parametrize("arguments", [["tui"], ["--mode", "tui"]])
@pytest.mark.parametrize("directory", ["root", "cwd", "root-selection"])
@pytest.mark.parametrize("phase", ["trust", "preflight"])
def test_replaced_workspace_skips_preview(
    arguments: list[str],
    directory: str,
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve() / "project"
    cwd = root / "nested"
    cwd.mkdir(parents=True)
    marker = root / "pyproject.toml"
    marker.touch()
    monkeypatch.chdir(cwd)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    if directory == "root":
        (alternate / marker.name).touch()
    calls: list[str | tuple[str, int | None]] = []

    def swap() -> None:
        if directory == "root-selection":
            # A nearer project marker changes which root owns the same cwd.
            nested_marker = cwd / marker.name
            if nested_marker.exists():
                nested_marker.unlink()
            else:
                nested_marker.touch()
            return
        selected = root if directory == "root" else cwd
        parked = selected.rename(tmp_path / "parked")
        alternate.rename(selected)
        parked.rename(alternate)
        if directory == "root":
            # Keep cwd's path and inode while replacing only the trusted ancestor.
            (alternate / cwd.name).rename(cwd)
        else:
            os.chdir(cwd)

    def trust(project: Path) -> TrustDecision:
        assert project == root
        if phase == "trust":
            swap()
        return TrustDecision(project, trusted=True)

    async def preflight(_: TuiOptions) -> Path:
        calls.append("preflight")
        # A second swap restores the original, isolating the preflight entry check.
        swap()
        return tmp_path / "selected.jsonl"

    monkeypatch.setattr(application, "_resolve_cli_trust", trust)
    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: Path("/unused"))
    monkeypatch.setattr(rust_launcher, "prewarm_preview", lambda _: nullcontext(object()))
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", preflight)
    monkeypatch.setattr(rust_launcher, "request_preview", lambda *_: calls.append("request") or 42)
    monkeypatch.setattr(
        rust_launcher, "_supervise_tui", lambda *args: calls.append(("launch", args[-1])) or 0
    )
    result = CliRunner().invoke(app, [*arguments, "--continue"], env={"WISP_PROVIDER": "fake"})
    assert result.exit_code == 0, result.exception
    assert calls == ["preflight", ("launch", None)]


@pytest.mark.parametrize(
    ("trusted", "resume", "tty"),
    [(True, None, True), (False, "saved", True), (None, "saved", True), (True, "saved", False)],
)
def test_skips_ineligible_launch(
    trusted: bool | None, resume: str | None, tty: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = TuiOptions(config=WispConfig(provider="fake"), project_trusted=trusted, resume=resume)
    monkeypatch.setattr(preview, "_stdio_is_interactive", lambda: tty)
    with preview.prewarm_preview(options) as worker:
        assert worker is None


def test_slow_worker_is_reaped_after_cleanup_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event

    reaped = Event()
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as child:
        wait = child.wait

        def delayed_wait(timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired(child.args, timeout)
            code = wait()
            reaped.set()
            return code

        monkeypatch.setattr(child, "wait", delayed_wait)
        preview.stop_worker(child)
        assert reaped.wait(timeout=3) and child.returncode is not None


def test_untrusted_session_reaps_import_only_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview, "_stdio_is_interactive", lambda: True)
    options = TuiOptions(config=WispConfig(provider="fake"), project_trusted=False, resume="saved")
    with preview.warm_cli_imports():
        child = preview._warmed.get()
        assert child is not None
        with preview.prewarm_preview(options) as worker:
            assert worker is None
            assert child.poll() is not None


@pytest.mark.parametrize("failure", ["preflight", "frontend"])
def test_reaps_worker_on_failure(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = TuiOptions(
        config=WispConfig(provider="fake"),
        project_trusted=True,
        continue_latest=True,
        preview_workspace=PreviewWorkspace.current(),
    )
    path = tmp_path / "selected.jsonl"
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(preview, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(rust_launcher, "resolve_rust_tui_binary", lambda: Path("/usr/bin/false"))
    monkeypatch.setattr(rust_launcher, "_snapshot_terminal", lambda: None)

    def spawn(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        if args[0] == "git":
            return real_popen(args, **kwargs)
        if not children:
            assert args[-2:] == ["-m", "wisp.sessions.startup_preview"]
            assert kwargs["cwd"] != Path.cwd()
            assert kwargs["env"] == {}
            # Stay alive until the supervisor kills us, even after request EOF.
            worker = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(worker)
            return worker
        (fd,) = kwargs["pass_fds"]
        separator = args.index("--")
        assert args[separator - 2 : separator] == ("--backend-pass-fd", str(fd))
        assert options.preview_workspace is not None
        assert options.preview_workspace.fd_argument(fd) in args[separator + 1 :]
        return real_popen([sys.executable, "-c", "raise SystemExit(42)"], **kwargs)

    async def preflight(_: TuiOptions) -> Path:
        assert children[0].poll() is None
        assert children[0].stdin is not None and not children[0].stdin.closed
        if failure == "preflight":
            raise ValueError("preflight failed")
        return path

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(rust_launcher, "_preflight_tui_options", preflight)
    if failure == "preflight":
        with pytest.raises(ValueError, match="preflight failed"):
            rust_launcher.run_rust_tui(options)
    else:
        assert rust_launcher.run_rust_tui(options) == 42
    worker = children[0]
    assert worker.returncode is not None
    assert worker.stdin is not None and worker.stdin.closed
    assert worker.stdout is not None and worker.stdout.closed


def test_sends_validated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    path = tmp_path / "file with spaces.jsonl"
    monkeypatch.chdir(tmp_path)
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as worker:
        fd = preview.request_preview(worker, Path(path.name), Path.cwd())
        assert fd is not None
        assert worker.wait(timeout=5) == 0
        assert json.loads(os.read(fd, 8192)) == [str(path), str(Path.cwd())]
