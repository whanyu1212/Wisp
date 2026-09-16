"""Verify an RC2 public PyPI installation and its native/pure lifecycle."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
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
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.2.0rc2"
TAGS = (
    "py3-none-any",
    "py3-none-macosx_11_0_arm64",
    "py3-none-macosx_11_0_x86_64",
    "py3-none-manylinux_2_28_x86_64",
)


def verify_textual(wisp: Path, work: Path, environment: dict[str, str]) -> None:
    """Exercise explicit Textual startup, a fake prompt, exit, and terminal restoration."""
    master, slave = pty.openpty()
    original = termios.tcgetattr(master)
    # Keep the readiness footer visible even with a long hosted-runner cwd.
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 240, 0, 0))
    child_environment = {
        **environment,
        "TERM": "xterm-256color",
        "WISP_PROVIDER": "fake",
        "WISP_MODEL": "",
        "WISP_TRUST": "1",
        "WISP_AUTO_COMPACTION": "0",
    }
    process = subprocess.Popen(
        [str(wisp), "tui", "--renderer", "textual", "--session-dir", str(work / "textual")],
        cwd=work,
        env=child_environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    typed_offset: int | None = None
    submitted = False
    response_seen = False
    quit_typed_offset: int | None = None
    quit_sent = False
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output)
            if typed_offset is None and "send · / commands".encode() in plain:
                typed_offset = len(output)
                os.write(master, b"public fallback smoke")
            # Observe the editor update before sending a distinct Enter event.
            if (
                typed_offset is not None
                and not submitted
                and b"public fallback smoke" in output[typed_offset:]
            ):
                os.write(master, b"\r")
                submitted = True
            if b"fake response to: public fallback smoke" in plain:
                response_seen = True
            if response_seen and quit_typed_offset is None:
                quit_typed_offset = len(output)
                os.write(master, b"/quit")
            if (
                quit_typed_offset is not None
                and not quit_sent
                and b"/quit" in output[quit_typed_offset:]
            ):
                os.write(master, b"\r")
                quit_sent = True
            if process.poll() is not None:
                assert process.returncode == 0, bytes(output[-4000:])
                assert response_seen, bytes(output[-4000:])
                assert termios.tcgetattr(master) == original
                return
        (work / "textual-terminal.bin").write_bytes(output)
        raise RuntimeError(
            f"Textual smoke timed out: submitted={submitted}, response={response_seen}, "
            f"quit={quit_sent}, terminal={termios.tcgetattr(master)!r}; "
            f"output: {work / 'textual-terminal.bin'}"
        )
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        os.close(master)


def main() -> None:
    """Download verified public wheels and exercise an isolated PyPI install."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", choices=TAGS[1:], required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--release-source", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    artifacts = work / "artifacts"
    artifacts.mkdir()
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "WISP_TUI_RENDERER", "WISP_RUST_TUI_BINARY"):
        environment.pop(name, None)
    environment["PATH"] = "/usr/bin:/bin"
    environment["HOME"] = str(work / "home")
    Path(environment["HOME"]).mkdir()
    environment["UV_CACHE_DIR"] = str(work / "uv-cache")
    # The lifecycle probe intentionally damages its installed executable.
    # Isolate that mutation from uv's cache before testing offline reinstall.
    environment["UV_LINK_MODE"] = "copy"

    def run(*command: str | Path, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(part) for part in command],
            cwd=work,
            env=environment,
            check=True,
            text=True,
            capture_output=capture,
            timeout=600,
        )

    metadata_url = f"https://pypi.org/pypi/wisp-ai/{VERSION}/json"
    with urllib.request.urlopen(metadata_url, timeout=60) as response:
        metadata = json.load(response)
    expected = {f"wisp_ai-{VERSION}-{tag}.whl" for tag in TAGS} | {f"wisp_ai-{VERSION}.tar.gz"}
    assert {item["filename"] for item in metadata["urls"]} == expected
    assert len(metadata["urls"]) == len(expected)
    assert metadata["info"]["version"] == VERSION
    (artifacts / "pypi.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    names = [f"wisp_ai-{VERSION}-{tag}.whl" for tag in (args.tag, TAGS[0])]
    downloaded: list[Path] = []
    for name in names:
        item = next(item for item in metadata["urls"] if item["filename"] == name)
        assert not item["yanked"]
        url = urllib.parse.urlparse(item["url"])
        assert url.scheme == "https" and url.hostname == "files.pythonhosted.org"
        with urllib.request.urlopen(item["url"], timeout=60) as response:
            data = response.read()
        assert hashlib.sha256(data).hexdigest() == item["digests"]["sha256"]
        assert len(data) == item["size"]
        path = artifacts / name
        path.write_bytes(data)
        downloaded.append(path)
    native, pure = downloaded
    run("/bin/sh", "-c", "! command -v cargo")
    venv = work / "environment"
    run(args.uv, "venv", "--python", sys.executable, venv)
    python = venv / "bin/python"
    run(
        args.uv,
        "--no-cache",
        "pip",
        "install",
        "--python",
        python,
        "--default-index",
        "https://pypi.org/simple",
        "--only-binary",
        ":all:",
        f"wisp-ai=={VERSION}",
    )
    installed = run(
        python,
        "-c",
        "import importlib.metadata as m, json, sys; "
        "d=m.distribution('wisp-ai'); "
        "assert d.version==sys.argv[1]; "
        "assert ('Tag: '+sys.argv[2]) in d.read_text('WHEEL'); "
        "assert d.read_text('direct_url.json') is None; "
        "print(json.dumps({'version':d.version,'wheel':d.read_text('WHEEL')}))",
        VERSION,
        args.tag,
        capture=True,
    )
    (artifacts / "selected-install.json").write_text(installed.stdout, encoding="utf-8")
    scripts = args.release_source.resolve() / "scripts"
    run(python, scripts / "verify_sdk_install.py")
    run(
        python,
        scripts / "verify_rust_tui_wheel.py",
        native,
        "--expected-tag",
        args.tag,
        "--reference-wheel",
        pure,
    )
    run(venv / "bin/wisp", "--version")
    run(venv / "bin/wisp-tui", "--version")
    verify_textual(venv / "bin/wisp", work, environment)
    (artifacts / "textual-smoke.json").write_text(
        json.dumps({"explicit_textual_prompt_exit_and_restore": True}), encoding="utf-8"
    )
    run(
        python,
        scripts / "verify_rust_tui_install_lifecycle.py",
        "--environment",
        venv,
        "--uv",
        args.uv,
        "--native-wheel",
        native,
        "--pure-wheel",
        pure,
        "--smoke-script",
        scripts / "smoke_installed_rust_tui.py",
        "--work-dir",
        work / "lifecycle",
        "--evidence",
        artifacts / "install-evidence.json",
    )
    evidence = json.loads((artifacts / "install-evidence.json").read_text(encoding="utf-8"))
    assert evidence["long_history"]["history_messages"] == 10_001
    print(json.dumps({"tag": args.tag, "source": metadata_url, "evidence": evidence}))


if __name__ == "__main__":
    main()
