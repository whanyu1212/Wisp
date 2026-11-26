"""Startup preview verification against the RPC host's authoritative selection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event
from typing import Any, BinaryIO

import pytest

from tests.support.cli import CliRunner
from wisp.cli import app, rpc, rpc_preview
from wisp.cli.startup_pipe import PreviewWorkspace, StartupPreviewPipe
from wisp.messages import Message
from wisp.sessions.entries import MessageSessionEntry, session_entry_to_json
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionRunSnapshot
from wisp.sessions.pagination import DEFAULT_SESSION_MESSAGE_PAGE_LIMIT
from wisp.sessions.startup_preview import prepare_startup_preview

pytestmark = pytest.mark.skipif(os.name != "posix", reason="native TUI requires POSIX")


@pytest.fixture
def history(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "selected.jsonl"
    with path.open("w") as stream:
        for index in range(DEFAULT_SESSION_MESSAGE_PAGE_LIMIT + 1):
            entry = MessageSessionEntry(
                id=f"m{index}",
                parent_id=f"m{index - 1}" if index else None,
                session_id="selected",
                message=Message(role="user", content=f"line {index}"),
            )
            stream.write(session_entry_to_json(entry) + "\n")
    return path


def _resume(history: Path, stream: BinaryIO | None, *, exit_code: int = 0) -> list[dict[str, Any]]:
    workspace = PreviewWorkspace.current()
    assert workspace is not None
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--continue", "--session-dir", str(history.parent)],
        input='{"id":"page","type":"get_messages","limit":1}\n',
        env={"WISP_PROVIDER": "fake", "WISP_TRUST": "1"},
        obj=StartupPreviewPipe(stream, workspace.token()) if stream is not None else None,
    )
    assert result.exit_code == exit_code, result.output
    return [json.loads(line) for line in result.stdout.splitlines()]


@pytest.mark.parametrize("preview", ["valid", "corrupt", "absent", "replaced"])
def test_pins_latest_before_replay(
    history: Path, preview: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = prepare_startup_preview(history, Path.cwd())
    assert packet is not None
    if preview == "corrupt":
        packet = json.dumps({**json.loads(packet), "sha256": "0" * 64}).encode()

    latest = JsonlSessionStore.latest
    selections: list[Path] = []

    def switch_latest(store: JsonlSessionStore) -> JsonlSession:
        selected = latest(store)
        selections.append(selected.path)
        newer = store.root / "newer.jsonl"
        newer.write_bytes(history.read_bytes().replace(b'"selected"', b'"newer"'))
        timestamp = history.stat().st_mtime_ns + 1_000_000_000
        os.utime(newer, ns=(timestamp, timestamp))
        if preview == "valid":
            # Nothing is available to read until the authoritative choice is made.
            output.write(packet)
            output.close()
        elif preview == "replaced":
            # The worker snapshots a different session at the exact path the host selected.
            newer.replace(selected.path)
            replacement = prepare_startup_preview(selected.path, Path.cwd())
            assert replacement is not None
            output.write(replacement)
            output.close()
        return selected

    observed: list[str] = []
    write_frame = rpc._write_rpc_frame
    snapshot = JsonlSession.read_run_snapshot

    def record_frame(frame: bytes) -> None:
        write_frame(frame)
        observed.append(json.loads(frame)["type"])

    def record_replay(session: JsonlSession) -> SessionRunSnapshot:
        observed.append("replay")
        return snapshot(session)

    monkeypatch.setattr(JsonlSessionStore, "latest", switch_latest)
    monkeypatch.setattr(rpc, "_write_rpc_frame", record_frame)
    monkeypatch.setattr(JsonlSession, "read_run_snapshot", record_replay)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as stream, os.fdopen(writer, "wb") as output:
        if preview == "corrupt":
            output.write(packet)
            output.close()
        records = _resume(history, stream if preview != "absent" else None)

    assert selections == [history]
    expected_start = [
        "rpc.handshake.accepted",
        *(["rpc.startup.history_preview"] if preview == "valid" else []),
    ]
    assert observed[: observed.index("replay")] == expected_start
    assert [record["type"] for record in records] == [
        *expected_start,
        "rpc.workspace",
        "rpc.command.started",
        "rpc.messages",
        "rpc.command.finished",
    ]
    if preview == "valid":
        assert records[1]["lines"][-1] == f"User: line {DEFAULT_SESSION_MESSAGE_PAGE_LIMIT}"
    assert records[-2]["session_id"] == "selected"
    assert records[-2]["session_path"] == str(history)
    assert records[-1]["ok"]


def test_project_changed_during_verification_discards_excerpt(
    history: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    packet = prepare_startup_preview(history, project)
    assert packet is not None
    verify = rpc_preview.verify_startup_preview

    def verify_then_change_root(
        data: bytes, selected: Path, session_id: str, cwd: Path
    ) -> tuple[str, ...] | None:
        result = verify(data, selected, session_id, cwd)
        assert result is not None
        monkeypatch.setattr(PreviewWorkspace, "current", classmethod(lambda cls: None))
        return result

    monkeypatch.setattr(rpc_preview, "verify_startup_preview", verify_then_change_root)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as stream:
        with os.fdopen(writer, "wb") as output:
            output.write(packet)
        records = _resume(history, stream)
    assert "rpc.startup.history_preview" not in [event["type"] for event in records]
    assert records[-2]["session_path"] == str(history)


def test_preserves_corrupt_replay_error(history: Path) -> None:
    packet = prepare_startup_preview(history, Path.cwd())
    assert packet is not None
    with history.open("ab") as session:
        session.write(b"broken entry\n")
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as stream:
        with os.fdopen(writer, "wb") as output:
            output.write(packet)
        records = _resume(history, stream, exit_code=1)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    assert "Malformed session entry JSON" in records[1]["message"]


@pytest.mark.parametrize("stall", ["hash", "workspace"])
def test_skips_stalled_verification(
    history: Path, monkeypatch: pytest.MonkeyPatch, stall: str
) -> None:
    entered, release, finished = Event(), Event(), Event()
    workspace = PreviewWorkspace.current()
    assert workspace is not None

    def wait_for_release() -> None:
        entered.set()
        release.wait(timeout=10)
        finished.set()

    def slow_workspace(cls: type[PreviewWorkspace]) -> PreviewWorkspace | None:
        wait_for_release()
        return workspace

    def verify_packet(_data: bytes, _path: Path, _session_id: str, _cwd: Path) -> tuple[str, ...]:
        if stall == "hash":
            wait_for_release()
        else:
            monkeypatch.setattr(PreviewWorkspace, "current", classmethod(slow_workspace))
        return ("User: late preview",)

    monkeypatch.setattr(rpc_preview, "verify_startup_preview", verify_packet)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as stream:
        with os.fdopen(writer, "wb") as output:
            output.write(b"packet")
        try:
            records = _resume(history, stream)
            assert entered.is_set() and not finished.is_set()
        finally:
            release.set()
    assert finished.wait(timeout=2)
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.workspace",
        "rpc.command.started",
        "rpc.messages",
        "rpc.command.finished",
    ]
    assert records[-2]["session_id"] == "selected" and records[-1]["ok"]
