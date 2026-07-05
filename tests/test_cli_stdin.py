# ruff: noqa: F403,F405

from __future__ import annotations

from tests.cli_support import *


def test_rpc_stdin_reader_dispatches_buffered_pipe_lines(monkeypatch: MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)

    async def run_reader() -> None:
        stop_reader = anyio.Event()
        send, receive = anyio.create_memory_object_stream[object](10)
        async with receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(cli_module._read_rpc_stdin, send, stop_reader)
                os.write(
                    write_fd,
                    b'{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
                    b'{"id":"shutdown-1","type":"shutdown"}\n',
                )
                with anyio.fail_after(1):
                    first = await receive.receive()
                    second = await receive.receive()
                assert isinstance(first, cli_module._RpcInputCommand)
                assert isinstance(second, cli_module._RpcInputCommand)
                assert first.command["id"] == "cancel-1"
                assert second.command["id"] == "shutdown-1"
                stop_reader.set()
                task_group.cancel_scope.cancel()

    try:
        anyio.run(run_reader)
    finally:
        os.close(write_fd)
        stdin.close()


def test_rpc_thread_stdin_reader_uses_bounded_queue(monkeypatch: MonkeyPatch) -> None:
    created_queue_sizes: list[int] = []

    class RecordingQueue(Queue[str | Exception]):
        def __init__(self, maxsize: int = 0) -> None:
            created_queue_sizes.append(maxsize)
            super().__init__(maxsize=maxsize)

    monkeypatch.setattr(cli_module, "Queue", RecordingQueue)

    async def run_reader() -> None:
        stop_reader = anyio.Event()
        stop_reader.set()
        send, receive = anyio.create_memory_object_stream[object](10)
        async with receive:
            await cli_module._read_rpc_thread_stdin(send, stop_reader)

    anyio.run(run_reader)

    assert created_queue_sizes == [cli_module._STDIN_THREAD_QUEUE_SIZE]
    assert created_queue_sizes[0] > 0


def test_rpc_stdin_reader_uses_thread_reader_for_windows_pipe(
    monkeypatch: MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(cli_module, "_rpc_stdin_needs_thread_reader", lambda _mode: True)

    async def fail_wait_readable(_fd: int) -> None:
        raise AssertionError("wait_readable should not be used for Windows pipe stdin")

    monkeypatch.setattr(cli_module.anyio, "wait_readable", fail_wait_readable)

    async def run_reader() -> None:
        stop_reader = anyio.Event()
        send, receive = anyio.create_memory_object_stream[object](10)
        async with receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(cli_module._read_rpc_stdin, send, stop_reader)
                os.write(
                    write_fd,
                    b'{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
                    b'{"id":"shutdown-1","type":"shutdown"}\n',
                )
                with anyio.fail_after(1):
                    first = await receive.receive()
                    second = await receive.receive()
                assert isinstance(first, cli_module._RpcInputCommand)
                assert isinstance(second, cli_module._RpcInputCommand)
                assert first.command["id"] == "prompt-1"
                assert second.command["id"] == "shutdown-1"
                stop_reader.set()
                task_group.cancel_scope.cancel()

    try:
        anyio.run(run_reader)
    finally:
        os.close(write_fd)
        stdin.close()


def test_rpc_stdin_reader_handles_regular_file_stdin(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    input_path = tmp_path / "commands.jsonl"
    input_path.write_text(
        '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
        '{"id":"shutdown-1","type":"shutdown"}\n',
        encoding="utf-8",
    )
    stdin = input_path.open("r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)

    async def run_reader() -> None:
        stop_reader = anyio.Event()
        send, receive = anyio.create_memory_object_stream[object](10)
        async with receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(cli_module._read_rpc_stdin, send, stop_reader)
                with anyio.fail_after(1):
                    first = await receive.receive()
                    second = await receive.receive()
                    closed = await receive.receive()
                assert isinstance(first, cli_module._RpcInputCommand)
                assert isinstance(second, cli_module._RpcInputCommand)
                assert isinstance(closed, cli_module._RpcInputClosed)
                assert first.command["id"] == "prompt-1"
                assert second.command["id"] == "shutdown-1"
                stop_reader.set()
                task_group.cancel_scope.cancel()

    try:
        anyio.run(run_reader)
    finally:
        stdin.close()
