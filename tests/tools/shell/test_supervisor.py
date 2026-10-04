from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import anyio
import pytest

import wisp.tools.shell.supervisor as process_manager_module
from tests.tools.shell.supervisor_support import (
    poll_until_terminal,
    python_command,
)
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import (
    ProcessSupervisor,
    ProcessUpdate,
    _bounded_text_tail,
    _pending_text_backends,
)


@pytest.mark.process
@pytest.mark.parametrize(
    "pending_text_backend",
    [name for name, _pending_text_type in _pending_text_backends()],
)
def test_managed_process_polling_delivers_incremental_output_once(
    tmp_path: Path,
    pending_text_backend: Any,
) -> None:
    async def run() -> tuple[ProcessUpdate, ...]:
        supervisor = ProcessSupervisor(_pending_text_backend=pending_text_backend)
        try:
            process_id = await supervisor.start(
                python_command(
                    "import sys,time;"
                    "print('first', flush=True);"
                    "time.sleep(0.15);"
                    "print('second', flush=True);"
                    "sys.stderr.write('warning\\n');"
                    "sys.stderr.flush()"
                ),
                cwd=tmp_path,
                timeout=2,
            )
            return await poll_until_terminal(supervisor, process_id)
        finally:
            await supervisor.aclose()

    updates = anyio.run(run)

    assert "".join(update.stdout for update in updates) == "first\nsecond\n"
    assert "".join(update.stderr for update in updates) == "warning\n"
    assert updates[-1].state == "completed"
    assert updates[-1].exit_code == 0


@pytest.mark.process
def test_managed_process_retention_is_bounded_and_utf8_safe(tmp_path: Path) -> None:
    async def run() -> tuple[ProcessUpdate, str, bool, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("print('🙂' * 50); print('tail')"),
                cwd=tmp_path,
                timeout=2,
                max_retained_bytes=20,
                max_retained_lines=1,
            )
            updates = await poll_until_terminal(supervisor, process_id)
            return (
                updates[-1],
                "".join(update.stdout for update in updates),
                any(update.stdout_truncated for update in updates),
                sum(update.stdout_dropped_bytes for update in updates),
            )
        finally:
            await supervisor.aclose()

    update, stdout, stdout_truncated, stdout_dropped_bytes = anyio.run(run)

    assert update.state == "completed"
    assert len(stdout.encode("utf-8")) <= 20
    assert stdout.endswith("tail\n")
    assert "\ufffd" not in stdout
    assert stdout_truncated is True
    assert stdout_dropped_bytes > 0


@pytest.mark.process
def test_managed_process_counts_source_bytes_when_utf8_tail_decodes_replacement(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[str, int, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command(
                    "import sys; sys.stdout.buffer.write(bytes([0xff, 0x61])); sys.stdout.flush()"
                ),
                cwd=tmp_path,
                timeout=2,
                max_retained_bytes=1,
                max_retained_lines=100,
            )
            updates = await poll_until_terminal(supervisor, process_id)
            return (
                "".join(update.stdout for update in updates),
                sum(update.stdout_dropped_bytes for update in updates),
                sum(update.stdout_retained_bytes or 0 for update in updates),
            )
        finally:
            await supervisor.aclose()

    stdout, stdout_dropped_bytes, stdout_retained_bytes = anyio.run(run)

    assert stdout == "a"
    assert stdout_dropped_bytes == 1
    assert stdout_retained_bytes == 1


@pytest.mark.process
def test_managed_process_preserves_valid_suffix_after_incomplete_utf8_lead(
    tmp_path: Path,
) -> None:
    async def run() -> tuple[str, int, int]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command(
                    "import sys; sys.stdout.buffer.write(bytes([0xe2, 0x41])); sys.stdout.flush()"
                ),
                cwd=tmp_path,
                timeout=2,
                max_retained_bytes=10,
                max_retained_lines=100,
            )
            updates = await poll_until_terminal(supervisor, process_id)
            return (
                "".join(update.stdout for update in updates),
                sum(update.stdout_dropped_bytes for update in updates),
                sum(update.stdout_retained_bytes or 0 for update in updates),
            )
        finally:
            await supervisor.aclose()

    stdout, stdout_dropped_bytes, stdout_retained_bytes = anyio.run(run)

    assert stdout == "\ufffdA"
    assert stdout_dropped_bytes == 0
    assert stdout_retained_bytes == 2


@pytest.mark.process
def test_managed_process_surfaces_established_malformed_utf8_before_exit(
    tmp_path: Path,
) -> None:
    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        process_id: str | None = None
        try:
            process_id = await supervisor.start(
                python_command(
                    "import sys,time; "
                    "sys.stdout.buffer.write(bytes([0xe2, 0x41])); "
                    "sys.stdout.flush(); "
                    "time.sleep(5)"
                ),
                cwd=tmp_path,
                timeout=10,
                max_retained_bytes=10,
                max_retained_lines=100,
            )
            with anyio.fail_after(2):
                while True:
                    update = await supervisor.poll(process_id, wait_seconds=0.1)
                    if update.stdout:
                        return update
        finally:
            if process_id is not None:
                await supervisor.cancel(process_id)
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "running"
    assert update.stdout == "\ufffdA"
    assert update.stdout_dropped_bytes == 0
    assert update.stdout_retained_bytes == 2


@pytest.mark.process
def test_managed_process_reports_stream_reader_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = ProcessSupervisor()

    async def fail_read(*_args: object) -> None:
        raise OSError("broken transport")

    monkeypatch.setattr(supervisor, "_read_stream", fail_read)

    async def run() -> ProcessUpdate:
        try:
            process_id = await supervisor.start(
                python_command("print('unread')"),
                cwd=tmp_path,
                timeout=2,
            )
            return (await poll_until_terminal(supervisor, process_id))[-1]
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "failed"
    assert update.exit_code is None
    assert update.error == "Failed to read process output"


@pytest.mark.process
def test_managed_process_reports_reader_failure_before_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = ProcessSupervisor()

    async def fail_read(*_args: object) -> None:
        raise OSError("broken transport")

    monkeypatch.setattr(supervisor, "_read_stream", fail_read)

    async def run() -> ProcessUpdate:
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(30)"),
                cwd=tmp_path,
                timeout=60,
            )
            return (await poll_until_terminal(supervisor, process_id, timeout=2))[-1]
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "failed"
    assert update.exit_code is None
    assert update.error == "Failed to read process output"


@pytest.mark.process
def test_cleanup_retry_restores_stream_reader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = ProcessSupervisor()
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_attempts = 0

    async def fail_read(*_args: object) -> None:
        raise OSError("broken transport")

    async def fail_first_cleanup(process: asyncio.subprocess.Process) -> bool:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        await original_terminate(process)
        return cleanup_attempts > 1

    monkeypatch.setattr(supervisor, "_read_stream", fail_read)
    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", fail_first_cleanup)

    async def run() -> tuple[ProcessUpdate, ProcessUpdate]:
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=60,
        )
        cleanup_failed = (await poll_until_terminal(supervisor, process_id))[-1]
        retried = await supervisor.cancel(process_id)
        await supervisor.aclose()
        return cleanup_failed, retried

    cleanup_failed, retried = anyio.run(run)

    assert cleanup_failed.state == "failed"
    assert cleanup_failed.error == "Failed to terminate process tree"
    assert retried.state == "failed"
    assert retried.exit_code is None
    assert retried.error == "Failed to read process output"


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n", "\u2028"])
def test_retention_counts_unterminated_trailing_logical_line(separator: str) -> None:
    text = f"first{separator}second"

    bounded, dropped_bytes = _bounded_text_tail(
        text,
        max_bytes=1_000,
        max_lines=1,
    )

    assert bounded == "second"
    assert dropped_bytes == len(f"first{separator}".encode())


@pytest.mark.parametrize(
    ("chunks", "max_bytes", "max_lines"),
    [
        ((b"first\r", b"\nsecond"), 1_000, 1),
        ((b"alpha\n", b"beta\n", b"gamma"), 1_000, 2),
        ((b"prefix\xf0\x9f", b"\x99\x82-tail"), 9, 10),
        ((b"discard",), 0, 10),
        ((b"discard",), 10, 0),
    ],
)
@pytest.mark.parametrize(
    ("_backend_name", "pending_text_type"),
    _pending_text_backends(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pending_text_incrementally_matches_tail_retention(
    chunks: tuple[bytes, ...],
    max_bytes: int,
    max_lines: int,
    _backend_name: str,
    pending_text_type: type[Any],
) -> None:
    pending = pending_text_type(max_bytes=max_bytes, max_lines=max_lines)
    source = b"".join(chunks)

    for chunk in chunks:
        pending.append_bytes(chunk)
    pending.append_bytes(b"", final=True)
    text, dropped_bytes, retained_source_bytes, source_byte_lengths = pending.drain()
    expected, expected_dropped_bytes = _bounded_text_tail(
        source.decode("utf-8"),
        max_bytes=max_bytes,
        max_lines=max_lines,
    )

    assert text == expected
    assert dropped_bytes == expected_dropped_bytes
    assert retained_source_bytes == len(text.encode("utf-8"))
    assert sum(source_byte_lengths) == retained_source_bytes
    assert dropped_bytes + retained_source_bytes == len(source)


@pytest.mark.parametrize(
    ("_backend_name", "pending_text_type"),
    _pending_text_backends(),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pending_text_applies_byte_cap_to_malformed_utf8(
    _backend_name: str,
    pending_text_type: type[Any],
) -> None:
    pending = pending_text_type(max_bytes=1, max_lines=10)

    pending.append_bytes(b"\xff", final=True)
    text, dropped_bytes, retained_source_bytes, source_byte_lengths = pending.drain()

    assert text == ""
    assert dropped_bytes == 1
    assert retained_source_bytes == 0
    assert source_byte_lengths == ()


@pytest.mark.process
def test_managed_process_timeout_is_not_an_exit_code(tmp_path: Path) -> None:
    async def run() -> ProcessUpdate:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("import time; time.sleep(10)"),
                cwd=tmp_path,
                timeout=0.25,
            )
            return (await poll_until_terminal(supervisor, process_id))[-1]
        finally:
            await supervisor.aclose()

    update = anyio.run(run)

    assert update.state == "timed_out"
    assert update.exit_code is None


@pytest.mark.process
def test_managed_timeout_serializes_with_explicit_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_terminate = process_manager_module._terminate_process_tree  # type: ignore[attr-defined]
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    active_cleanups = 0
    max_active_cleanups = 0

    async def delayed_terminate(process: asyncio.subprocess.Process) -> bool:
        nonlocal active_cleanups, max_active_cleanups
        active_cleanups += 1
        max_active_cleanups = max(max_active_cleanups, active_cleanups)
        cleanup_started.set()
        await allow_cleanup.wait()
        try:
            await original_terminate(process)
            return True
        finally:
            active_cleanups -= 1

    monkeypatch.setattr(process_manager_module, "_terminate_process_tree", delayed_terminate)

    async def run() -> None:
        supervisor = ProcessSupervisor()
        process_id = await supervisor.start(
            python_command("import time; time.sleep(30)"),
            cwd=tmp_path,
            timeout=0.01,
        )
        await cleanup_started.wait()
        cancel_call = asyncio.create_task(supervisor.cancel(process_id))
        await anyio.sleep(0.05)
        assert max_active_cleanups == 1

        allow_cleanup.set()
        await cancel_call
        await supervisor.aclose()

    anyio.run(run)

    assert max_active_cleanups == 1


@pytest.mark.process
def test_polling_terminal_process_does_not_repeat_output(tmp_path: Path) -> None:
    async def run() -> tuple[tuple[ProcessUpdate, ...], ProcessUpdate]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(
                python_command("print('once')"),
                cwd=tmp_path,
                timeout=2,
            )
            updates = await poll_until_terminal(supervisor, process_id)
            repeated = await supervisor.poll(process_id)
            return updates, repeated
        finally:
            await supervisor.aclose()

    updates, repeated = anyio.run(run)

    assert "".join(update.stdout for update in updates) == "once\n"
    assert repeated.state == "completed"
    assert repeated.stdout == ""
    assert repeated.stderr == ""


def test_closed_and_unknown_process_handles_fail_deterministically(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor = ProcessSupervisor()
        with pytest.raises(ToolError, match="Unknown managed process: missing"):
            await supervisor.poll("missing")
        await supervisor.aclose()
        with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
            await supervisor.start(
                python_command("print('no')"),
                cwd=tmp_path,
                timeout=2,
            )

    anyio.run(run)
