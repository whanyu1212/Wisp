# ruff: noqa: F403,F405

from __future__ import annotations

import pytest
from anyio.abc import Process

from tests.support.cli import *
from tests.support.rpc import guard_rpc_command_serialization
from wisp import __version__
from wisp.rpc.protocol import RpcHandshakeRequest

_BaseCliRunner = RawCliRunner


def _base_jsonl_records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines()]


_RPC_TEST_HANDSHAKE = (
    RpcHandshakeRequest(
        frontend_name="wisp-python-tests",
        frontend_version=__version__,
        supported_capabilities=(),
        required_capabilities=(),
    ).model_dump_json()
    + "\n"
)


def test_rpc_control_stream_buffers_at_most_one_parsed_event() -> None:
    assert cli_rpc_module._RPC_CONTROL_STREAM_BUFFER_SIZE == 1


def test_rpc_mode_requires_handshake_before_ordinary_commands(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def fail_build_runtime(_config: object) -> object:
        raise AssertionError("runtime must not start before negotiation")

    monkeypatch.setattr(cli_rpc_module, "build_runtime_for_config", fail_build_runtime)
    result = _BaseCliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"shutdown-1","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.rejected"]
    assert records[0]["code"] == "invalid_handshake"


def test_rpc_mode_accepts_handshake_before_any_runtime_event(tmp_path: Path) -> None:
    result = _BaseCliRunner().invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input=_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    records = _base_jsonl_records(result.stdout)
    assert records[0]["type"] == "rpc.handshake.accepted"
    assert [record["type"] for record in records[1:]] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]


def test_rpc_mode_selected_from_environment_skips_plaintext_help(tmp_path: Path) -> None:
    result = _BaseCliRunner().invoke(
        app,
        [],
        input=_RPC_TEST_HANDSHAKE + '{"id":"bye","type":"shutdown"}\n',
        env={
            "WISP_MODE": "rpc",
            "WISP_PROVIDER": "fake",
            "WISP_MODEL": "",
            "WISP_SESSION_DIR": str(tmp_path),
        },
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[-1]["command_id"] == "bye"
    assert records[-1]["ok"] is True


@pytest.mark.process
def test_rpc_mode_preserves_command_prefetched_with_handshake(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, object]]:
        process = await anyio.open_process(
            [
                sys.executable,
                "-m",
                "wisp",
                "--mode",
                "rpc",
                "--session-dir",
                str(tmp_path),
            ],
            env={**os.environ, "WISP_PROVIDER": "fake", "WISP_MODEL": ""},
        )
        assert process.stdin is not None
        assert process.stdout is not None
        await process.stdin.send(
            (_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n').encode()
        )
        await process.stdin.aclose()
        output = bytearray()
        with anyio.fail_after(10):
            while True:
                try:
                    output.extend(await process.stdout.receive())
                except anyio.EndOfStream:
                    break
            await process.wait()
        assert process.returncode == 0, await _read_stderr(process)
        return _base_jsonl_records(output.decode())

    records = anyio.run(scenario)
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.command.started",
        "rpc.command.finished",
    ]


@pytest.mark.process
def test_rpc_mode_exits_cleanly_on_shutdown_while_stdin_stays_open(tmp_path: Path) -> None:
    """A live frontend keeps stdin open after shutdown; the stdin reader must not abort exit."""

    async def scenario() -> tuple[int | None, str]:
        process = await anyio.open_process(
            [
                sys.executable,
                "-m",
                "wisp",
                "--mode",
                "rpc",
                "--session-dir",
                str(tmp_path),
            ],
            env={**os.environ, "WISP_PROVIDER": "fake", "WISP_MODEL": ""},
        )
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            await process.stdin.send(
                (_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n').encode()
            )
            with anyio.fail_after(10):
                while True:
                    try:
                        await process.stdout.receive()
                    except anyio.EndOfStream:
                        break
                await process.wait()
            return process.returncode, await _read_stderr(process)
        finally:
            await process.stdin.aclose()

    returncode, stderr = anyio.run(scenario)
    assert returncode == 0, stderr
    assert "Fatal Python error" not in stderr


async def _read_stderr(process: Process) -> str:
    assert process.stderr is not None
    stderr = bytearray()
    with anyio.fail_after(5):
        while True:
            try:
                stderr.extend(await process.stderr.receive())
            except anyio.EndOfStream:
                return stderr.decode(errors="replace")


@pytest.mark.process
def test_rpc_mode_writes_utf8_frames_with_non_utf8_stdio_encoding(tmp_path: Path) -> None:
    async def scenario() -> bytes:
        process = await anyio.open_process(
            [
                sys.executable,
                "-m",
                "wisp",
                "--mode",
                "rpc",
                "--session-dir",
                str(tmp_path),
            ],
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-16",
                "WISP_PROVIDER": "fake",
                "WISP_MODEL": "",
            },
        )
        assert process.stdin is not None
        assert process.stdout is not None
        await process.stdin.send(
            (_RPC_TEST_HANDSHAKE + '{"id":"shutdown-1","type":"shutdown"}\n').encode()
        )
        await process.stdin.aclose()
        output = bytearray()
        with anyio.fail_after(10):
            while True:
                try:
                    output.extend(await process.stdout.receive())
                except anyio.EndOfStream:
                    break
            await process.wait()
        assert process.returncode == 0
        return bytes(output)

    output = anyio.run(scenario)
    records = _base_jsonl_records(output.decode("utf-8"))
    assert [record["type"] for record in records] == [
        "rpc.handshake.accepted",
        "rpc.command.started",
        "rpc.command.finished",
    ]


def test_rpc_mode_rejects_cli_prompt(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["-p", "hello", "--mode", "rpc", "--session-dir", str(tmp_path)],
        input="",
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    assert records[1]["message"] == (
        "--prompt is not used with --mode rpc; send prompt commands on stdin"
    )


@pytest.mark.parametrize(
    ("arguments", "environment", "message"),
    [
        (
            ["--resume", "missing", "--continue"],
            {"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
            "use either --resume or --continue, not both",
        ),
        (
            [],
            {"WISP_PROVIDER": "fake", "WISP_AUTO_COMPACTION": "sometimes"},
            "WISP_AUTO_COMPACTION must be one of",
        ),
    ],
)
def test_rpc_mode_negotiates_before_startup_errors(
    tmp_path: Path,
    arguments: list[str],
    environment: dict[str, str],
    message: str,
) -> None:
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", *arguments, "--session-dir", str(tmp_path)],
        input="",
        env=environment,
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    assert message in records[1]["message"]


def test_rpc_mode_bounds_post_handshake_startup_errors(tmp_path: Path) -> None:
    provider = "x" * 5_000
    result = CliRunner().invoke(
        app,
        ["--mode", "rpc", "--provider", provider, "--session-dir", str(tmp_path)],
        input="",
        env={"WISP_MODEL": ""},
    )

    assert result.exit_code == 1, result.output
    assert result.stderr == ""
    records = _base_jsonl_records(result.stdout)
    assert [record["type"] for record in records] == ["rpc.handshake.accepted", "error"]
    message = records[1]["message"]
    assert isinstance(message, str)
    assert len(message) == cli_rpc_module._MAX_RPC_STARTUP_ERROR_CHARS
    assert message.startswith("Unknown provider: ")
    assert message.endswith("...")


def test_rpc_mode_shutdown_emits_lifecycle_and_exits(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["--mode", "rpc", "--session-dir", str(tmp_path)],
        input='{"id":"bye","type":"shutdown"}\n',
        env={"WISP_PROVIDER": "fake", "WISP_MODEL": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    records = _jsonl_records(result.stdout)
    assert [record["type"] for record in records] == [
        "rpc.command.started",
        "rpc.command.finished",
    ]
    assert records[0]["command_id"] == "bye"
    assert records[0]["command_type"] == "shutdown"
    assert records[1]["command_id"] == "bye"
    assert records[1]["command_type"] == "shutdown"
    assert records[1]["ok"] is True


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
